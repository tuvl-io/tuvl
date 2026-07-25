# Contributing

Thank you for your interest in contributing to tuvl!

## Development Setup

### Prerequisites

- Python 3.13 (`requires-python = ">=3.13,<3.14"`)
- [uv](https://docs.astral.sh/uv/) package manager
- PostgreSQL 16+
- Node.js 20+ and pnpm (only for Insight UI development)

### Clone and Install

```bash
git clone https://github.com/tuvl-io/tuvl.git
cd tuvl

# Install Python dependencies and apply vendored patches
make setup

# Install UI node_modules (run once, only if touching ui/)
make ui-install
```

`make setup` runs `uv sync` followed by `make apply-patches`, which re-applies the
vendored fixes in `patches/` to sonora after every sync. If you add a new dependency
with `uv add`, re-run `make apply-patches` (or just `make setup`).

### Run Development Servers

```bash
# Engine only (headless)
make dev-core DIR=/path/to/your/project

# Engine + Tuvl Insight dashboard
make dev-ui DIR=/path/to/your/project

# Engine + Vite hot-reload dev server (UI development)
make dev DIR=/path/to/your/project
```

The server starts on `http://localhost:8000`; the Insight console is at
`http://localhost:8000/insight` in dev mode.

### Proto codegen

If you modify any `.proto` file, regenerate the Python stubs:

```bash
make proto
```

### Vendored patches

`patches/sonora-asgi-fixes.patch` contains two bug fixes for sonora 0.2.3:

1. **Trailer bytes format** — gRPC-Web trailer keys/values must be plain strings, not
   bytes tuples, for `pack_trailers()` to produce a valid ASCII frame.
2. **Content-Type echo** — restricts sonora's response `Content-Type` to known gRPC-Web
   MIME types so clients never receive `Content-Type: */*`.

Applied automatically by `make setup`. To apply manually: `make apply-patches`.

## Code Style

### Python

`ruff` handles linting and formatting; `make check` is the gate CI runs:

```bash
make fmt      # format src/ and tests/
make lint     # ruff check
make check    # lint + format --check (must pass before a PR)
```

Keep comments minimal and about the non-obvious *why* — no narration of the
code or of the change history.

### TypeScript (Insight UI)

```bash
cd ui
pnpm lint
```

UI changes must be bundled to reach the wheel: `make build-ui` copies the built
assets into `src/tuvl_insight/static/`.

## Testing

```bash
make test              # full suite
uv run pytest tests/core/test_engine.py -q   # a single file
```

Place tests under `tests/` (mirroring `tests/core/`, `tests/cli/`). Shared
LiteLLM stubs for agent tests live in `tests/llm_stubs.py` — use them instead of
rolling new fakes. Node-registration tests import real files into the global
`NODE_REGISTRY`: use unique node names and clean up in a fixture.

## Pull Request Process

1. **Fork** the repository
2. **Create a branch**: `git checkout -b feat/my-feature`
3. **Make your changes** with clear commit messages
4. **Add tests** for new functionality
5. **Run** `make check && make test`
6. **Submit a PR** with a clear description

### Commit Messages

Use conventional commits:

```
feat: add email notification node
fix: correct validation logic in router
docs: update workflow configuration guide
test: add tests for bulk import node
refactor: simplify repository pattern
```

## Project Structure

```
tuvl/
├── src/tuvl/             # Engine + CLI (one package)
│   ├── cli/              # typer CLI (init, dev, run, validate, test, ship, …)
│   ├── core/             # API routers, auth, engine, artifacts, orchestrator, …
│   ├── datasources/      # Postgres / Redis connections, RLS
│   ├── models/           # YAML document loaders
│   └── nodes/            # Built-in nodes (RAG, …) + @node registry
├── src/tuvl_insight/     # Insight wheel (serves the built UI)
├── ui/                   # Insight React/Vite source
├── tests/                # pytest suite (core/, cli/)
├── docs/                 # Agentic manual + subsystem deep-dives
└── patches/              # Vendored dependency fixes
```

Key invariants to respect (the agentic manual's golden rules are the contract):

- **Closed sets.** YAML `kind:` values and workflow step kinds are closed sets —
  new ones are a design discussion first, not a PR.
- **Custom nodes:** exactly one `@node("name")` per file; the filename must equal
  the decorator argument.
- **Step-kind dispatch** is centralized (`WorkflowEngine._run_kind`); never add a
  parallel dispatch site.

## Documentation

- `docs/tuvl-agentic-manual.md` is the ground-truth YAML/agent contract — any
  behavior change that touches the contract must update it in the same PR.
- The docs site ([tuvl.io/docs](https://tuvl.io/docs)) mirrors `docs/` from this
  repo; it is built with MkDocs in the separate docs repository.

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/tuvl-io/tuvl/issues)
- **Discussions**: [GitHub Discussions](https://github.com/tuvl-io/tuvl/discussions)
- **Security reports**: see [SECURITY.md](SECURITY.md) — never a public issue

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
