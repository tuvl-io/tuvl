.PHONY: help setup setup-all setup-min apply-patches dev-core dev-ui install install-dev lint fmt check typecheck test test-verbose build-ui build publish clean ui-install proto

PYTHON := uv run python
RUFF   := uv run ruff
MYPY   := uv run mypy
UV     := uv
TUVL   := uv run tuvl

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "  setup        Sync core + Tuvl Insight + dev tools (recommended)"
	@echo "  setup-min    Sync core engine deps only (for production builds)"
	@echo "  dev-core     Run the server in dev mode (no UI, headless)"
	@echo "  dev-ui       Run server + Tuvl Insight dashboard with hot reload"
	@echo "  ui-install   Install React UI node_modules (run once after clone)"
	@echo "  lint         Run ruff check"
	@echo "  fmt          Run ruff formatter"
	@echo "  check        Run ruff check + ruff format check (CI gate)"
	@echo "  typecheck    Run mypy (advisory — not a CI gate yet)"
	@echo "  test         Run the pytest suite (quiet)"
	@echo "  test-verbose Run the pytest suite (verbose)"
	@echo "  build-ui     Compile React SPA and copy assets into tuvl-insight wheel"
	@echo "  build        build-ui → build tuvl wheel → build tuvl-insight wheel"
	@echo "  proto        Generate gRPC Python stubs from src/tuvl/core/grpc/execution.proto"
	@echo "  publish      build → publish both wheels to PyPI (set UV_PUBLISH_TOKEN)"
	@echo "  clean        Remove build artifacts"

# ── Install ───────────────────────────────────────────────────────────────────
# Default: sync core + Tuvl Insight + dev tools so contributors can run tests
# and lint immediately after cloning.
setup:
	@echo "→ Syncing core + Tuvl Insight + dev tools..."
	$(UV) sync --extra standard --extra dev
	$(MAKE) apply-patches

# Alias kept for legacy docs/scripts — equivalent to `make setup`.
setup-all: setup

# Production-style install: runtime engine deps only, no Insight or dev tools.
# Use this when baking a container image from source.
setup-min:
	$(UV) sync
	$(MAKE) apply-patches

install: setup
install-dev: setup

# ── Patches ───────────────────────────────────────────────────────────────────
# Vendored patches for third-party packages with upstream bugs.
# Re-applied after every uv sync to ensure the fixes persist.
apply-patches:
	@echo "→ Applying vendored patches to site-packages..."
	@SITE_PKGS=$$($(PYTHON) -c "import sonora, os; print(os.path.dirname(os.path.dirname(os.path.abspath(sonora.__file__))))") && \
	  if patch -p1 --forward --dry-run --reject-file=/dev/null -d "$$SITE_PKGS" < patches/sonora-asgi-fixes.patch >/dev/null 2>&1; then \
	    patch -p1 --forward --reject-file=/dev/null -d "$$SITE_PKGS" < patches/sonora-asgi-fixes.patch >/dev/null 2>&1 && \
	    echo "✓ sonora patches applied"; \
	  else \
	    echo "✓ sonora patches already applied (skipped)"; \
	  fi

ui-install:
	cd ui && pnpm install

# ── Dev ───────────────────────────────────────────────────────────────────────
DIR ?= .

dev-core: ## Run the server in dev mode without Tuvl Insight UI
	@echo "→ Starting tuvl engine in dev mode (headless) — project: $(DIR)"
	TUVL_UI_DIR="" $(UV) run tuvl dev --project-dir "$(DIR)" --no-browser --port 8000

dev-ui: ## Run the server with Tuvl Insight dashboard active
	@echo "→ Starting tuvl engine + Tuvl Insight dashboard — project: $(DIR)"
	$(UV) run tuvl dev --project-dir "$(DIR)" --no-browser --port 8000

dev: ## Run engine + Vite hot-reload dev server in parallel (legacy)
	@echo "→ Starting tuvl engine (project: $(DIR)) + Vite dev server"
	@echo "→ UI: http://localhost:5173  (live — always current code)"
	@echo "→ Ctrl-C to stop both"
	@trap 'kill 0' INT; \
	  $(TUVL) dev --project-dir "$(DIR)" --no-browser & \
	  (cd ui && pnpm dev --open) & \
	  wait

# ── Lint / Format ─────────────────────────────────────────────────────────────
lint:
	$(RUFF) check src/

fmt:
	$(RUFF) check src/ --fix
	$(RUFF) format src/

# `check` is the CI lint gate — ruff + format only.  mypy has a large backlog
# of pre-existing errors flagged by the code-quality audit (chiefly in the
# protobuf-generated and SQLModel dynamic-class code paths) and is *not*
# treated as a release blocker.  Run ``make typecheck`` separately to see
# the current type debt; clean it down between releases.
check:
	$(RUFF) check src/ --output-format=concise
	$(RUFF) format --check src/

typecheck:
	@echo "→ mypy (advisory — not currently a CI gate)"
	$(MYPY) src/ || true

# ── Test ──────────────────────────────────────────────────────────────────────
test:
	$(UV) run pytest -q tests/

test-verbose:
	$(UV) run pytest -v tests/

# ── Proto codegen ─────────────────────────────────────────────────────────────
PROTO_OUT  := src/tuvl/core/grpc

proto:
	@echo "→ Generating gRPC Python stubs from all .proto files in $(PROTO_OUT)/..."
	$(UV) run python -m grpc_tools.protoc \
	  -I $(PROTO_OUT) \
	  --python_out=$(PROTO_OUT) \
	  --grpc_python_out=$(PROTO_OUT) \
	  $(PROTO_OUT)/execution.proto \
	  $(PROTO_OUT)/dev.proto \
	  $(PROTO_OUT)/iam.proto
	@# Fix bare module imports → relative imports in generated grpc files
	sed -i '' 's/^import execution_pb2 as execution__pb2/from . import execution_pb2 as execution__pb2/' $(PROTO_OUT)/execution_pb2_grpc.py
	sed -i '' 's/^import dev_pb2 as dev__pb2/from . import dev_pb2 as dev__pb2/' $(PROTO_OUT)/dev_pb2_grpc.py
	sed -i '' 's/^import iam_pb2 as iam__pb2/from . import iam_pb2 as iam__pb2/' $(PROTO_OUT)/iam_pb2_grpc.py
	@echo "✓ Stubs written to $(PROTO_OUT)/"

# ── UI Build ──────────────────────────────────────────────────────────────────
build-ui:
	@echo "→ Compiling Tuvl Insight React SPA..."
	$(PYTHON) scripts/bundle_ui.py
	@echo "✓ Assets ready in src/tuvl_insight/static/"

# ── Python Build ──────────────────────────────────────────────────────────────
build: build-ui
	@echo "→ Building tuvl core wheel..."
	$(UV) build --package tuvl --out-dir dist/
	@echo "→ Building tuvl-insight wheel..."
	$(UV) build --package tuvl-insight --out-dir dist/
	@echo "✓ Both wheels ready in dist/"

# ── Publish ──────────────────────────────────────────────────────────────────
publish: build
	@echo "→ Publishing tuvl to PyPI..."
	$(UV) publish dist/tuvl-*.whl
	@echo "→ Publishing tuvl-insight to PyPI..."
	$(UV) publish dist/tuvl_insight-*.whl
	@echo "✓ Both packages published"

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	rm -rf dist/ .mypy_cache/ .ruff_cache/ src/tuvl.egg-info/
	rm -rf src/tuvl_insight/static/
	find . -type d -name __pycache__ -exec rm -rf {} +
