.PHONY: help setup setup-all setup-min dev-core dev-ui install install-dev lint fmt check typecheck typecheck-gate test test-verbose build-ui build smoke publish publish-dry clean ui-install

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
	@echo "  typecheck      Run mypy over the whole repo (advisory — not a CI gate yet)"
	@echo "  typecheck-gate Run mypy over the known-clean subset (fails on regression)"
	@echo "  test         Run the pytest suite (quiet)"
	@echo "  test-verbose Run the pytest suite (verbose)"
	@echo "  build-ui     Compile React SPA and copy assets into tuvl-insight wheel"
	@echo "  build        build-ui → build tuvl wheel → build tuvl-insight wheel"
	@echo "  smoke        build → install both wheels in /tmp venv → verify tuvl --version + insight static assets"
	@echo "  publish-dry  smoke → list what would be uploaded (no actual push)"
	@echo "  publish      smoke → upload both wheels + sdists to PyPI (set UV_PUBLISH_TOKEN)"
	@echo "  clean        Remove build artifacts"

# ── Install ───────────────────────────────────────────────────────────────────
# Default: sync core + Tuvl Insight + dev tools so contributors can run tests
# and lint immediately after cloning.
setup:
	@echo "→ Syncing core + Tuvl Insight + dev tools..."
	$(UV) sync --extra standard --group dev

# Alias kept for legacy docs/scripts — equivalent to `make setup`.
setup-all: setup

# Production-style install: runtime engine deps only, no Insight or dev tools.
# Use this when baking a container image from source.
setup-min:
	$(UV) sync --no-dev

install: setup
install-dev: setup

ui-install:
	cd ui && pnpm install

# ── Dev ───────────────────────────────────────────────────────────────────────
DIR ?= .

dev-core: ## Run the dev server without opening a browser (Insight still mounts in dev mode)
	@echo "→ Starting tuvl engine in dev mode — project: $(DIR)"
	$(UV) run tuvl dev --project-dir "$(DIR)" --no-browser --port 8885

dev-ui: ## Run the server with Tuvl Insight dashboard active
	@echo "→ Starting tuvl engine + Tuvl Insight dashboard — project: $(DIR)"
	$(UV) run tuvl dev --project-dir "$(DIR)" --no-browser --port 8885

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
# SQLModel dynamic-class code paths) and is *not*
# treated as a release blocker.  Run ``make typecheck`` separately to see
# the current type debt; clean it down between releases.
check:
	$(RUFF) check src/ tests/ --output-format=concise
	$(RUFF) format --check src/ tests/

typecheck:
	@echo "→ mypy (advisory, full repo — not currently a CI gate)"
	$(MYPY) src/ || true

# Modules that are currently mypy-clean.  This is a ratchet, not a wall: it
# must stay green.  When you clean up a new module, add it here so it can't
# silently regress; never remove an entry to make room for new errors.
MYPY_GATED_PATHS := \
  src/tuvl/api \
  src/tuvl/testing \
  src/tuvl/spec \
  src/tuvl/codegen \
  src/tuvl/lock \
  src/tuvl/devmcp \
  src/tuvl/contract \
  src/tuvl/validate \
  src/tuvl/runtime \
  src/tuvl/tenancy \
  src/tuvl/cli/main.py \
  src/tuvl/core/system_config.py \
  src/tuvl/core/context.py \
  src/tuvl/core/config_loader.py \
  src/tuvl/core/errors.py

typecheck-gate:
	@echo "→ mypy (gated subset — must pass, no || true)"
	$(MYPY) $(MYPY_GATED_PATHS)

# ── Test ──────────────────────────────────────────────────────────────────────
test:
	$(UV) run pytest -q tests/

test-verbose:
	$(UV) run pytest -v tests/

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

# ── Pre-publish smoke ────────────────────────────────────────────────────────
# Install the *actual* artefacts we're about to publish into a throwaway venv
# and prove three things before we ship them to PyPI:
#
#   1. `tuvl --version` resolves and matches the stamped pyproject version
#      (catches packaging metadata bugs).
#   2. `import tuvl_insight` works (catches the empty-wheel bug that shipped
#      in 2026.2.3b1 — hatchling silently produced a metadata-only wheel).
#   3. tuvl_insight's compiled React SPA is bundled — static/index.html
#      exists in the installed package directory.
#
# This mirrors the `Smoke-test built wheel` step in `.github/workflows/publish.yml`
# and runs locally before `make publish` so we never push a broken artefact.
smoke: build
	@echo "→ Smoke-testing built wheels in a throwaway venv..."
	@PYPROJECT_VERSION=$$(grep '^version' pyproject.toml | head -1 | sed 's/.*= *"\(.*\)"/\1/'); \
	  rm -rf /tmp/tuvl-publish-smoke; \
	  python3 -m venv /tmp/tuvl-publish-smoke >/dev/null; \
	  /tmp/tuvl-publish-smoke/bin/pip install --quiet \
	    dist/tuvl-$$PYPROJECT_VERSION-py3-none-any.whl \
	    dist/tuvl_insight-$$PYPROJECT_VERSION-py3-none-any.whl 2>&1 | tail -3; \
	  REPORTED=$$(/tmp/tuvl-publish-smoke/bin/tuvl --version | awk '{print $$NF}' | sed 's/^v//'); \
	  echo "  pyproject version: $$PYPROJECT_VERSION"; \
	  echo "  tuvl --version  : $$REPORTED"; \
	  if [ "$$REPORTED" != "$$PYPROJECT_VERSION" ]; then \
	    echo "::error:: Built wheel reports $$REPORTED but pyproject is $$PYPROJECT_VERSION"; \
	    rm -rf /tmp/tuvl-publish-smoke; \
	    exit 1; \
	  fi; \
	  /tmp/tuvl-publish-smoke/bin/python -c '\
import tuvl_insight, pathlib;\
p = pathlib.Path(tuvl_insight.__file__).parent / "static" / "index.html";\
assert p.exists(), f"static/index.html missing in tuvl_insight wheel — empty wheel bug returned!";\
print(f"  tuvl_insight static/: {p.parent} ({len(list(p.parent.iterdir()))} files)")\
' || { rm -rf /tmp/tuvl-publish-smoke; exit 1; }; \
	  rm -rf /tmp/tuvl-publish-smoke
	@echo "✓ Smoke test passed"

# ── Publish ──────────────────────────────────────────────────────────────────
# publish requires `smoke` (which requires `build`) to pass first so we never
# upload an artefact we haven't proven importable.  Smoke also enforces the
# pyproject ↔ `tuvl --version` match.
#
# Both .whl AND .tar.gz are uploaded so the sdist is available as a fallback
# for systems that can't use the platform-independent wheel.  Engine first,
# then tuvl-insight (which has `tuvl>=...` as a hard dep, so it must follow).
#
# Set UV_PUBLISH_TOKEN to a PyPI API token before running:
#   export UV_PUBLISH_TOKEN=pypi-XXXX
publish: smoke
	@if [ -z "$$UV_PUBLISH_TOKEN" ]; then \
	  echo "::error:: UV_PUBLISH_TOKEN not set. Generate a token at"; \
	  echo "          https://pypi.org/manage/account/token/  then run:"; \
	  echo "          export UV_PUBLISH_TOKEN=pypi-..."; \
	  exit 1; \
	fi
	@PYPROJECT_VERSION=$$(grep '^version' pyproject.toml | head -1 | sed 's/.*= *"\(.*\)"/\1/'); \
	  echo "→ Publishing tuvl $$PYPROJECT_VERSION to PyPI (wheel + sdist)..."; \
	  $(UV) publish dist/tuvl-$$PYPROJECT_VERSION-py3-none-any.whl dist/tuvl-$$PYPROJECT_VERSION.tar.gz; \
	  echo "→ Publishing tuvl-insight $$PYPROJECT_VERSION to PyPI (wheel + sdist)..."; \
	  $(UV) publish dist/tuvl_insight-$$PYPROJECT_VERSION-py3-none-any.whl dist/tuvl_insight-$$PYPROJECT_VERSION.tar.gz
	@echo "✓ Both packages published"
	@echo ""
	@echo "Verify on PyPI:"
	@echo "  https://pypi.org/project/tuvl/"
	@echo "  https://pypi.org/project/tuvl-insight/"

# ── Publish dry-run — what would be uploaded without actually pushing ──────
publish-dry: smoke
	@PYPROJECT_VERSION=$$(grep '^version' pyproject.toml | head -1 | sed 's/.*= *"\(.*\)"/\1/'); \
	  echo "→ Dry-run — would publish these to PyPI:"; \
	  ls -la dist/tuvl-$$PYPROJECT_VERSION* dist/tuvl_insight-$$PYPROJECT_VERSION*

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	rm -rf dist/ .mypy_cache/ .ruff_cache/ src/tuvl.egg-info/
	rm -rf src/tuvl_insight/static/
	find . -type d -name __pycache__ -exec rm -rf {} +
