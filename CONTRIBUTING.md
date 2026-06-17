# Contributing

Thank you for your interest in contributing to tuvl!

## Development Setup

### Prerequisites

- Python 3.12+
- uv package manager
- PostgreSQL 16+
- Node.js 20+ and pnpm (for UI development)

### Clone and Install

```bash
# Clone repository
git clone https://github.com/tuvl-io/tuvl.git
cd tuvl

# Install Python dependencies and apply vendored patches
make setup

# Install UI node_modules (run once after clone)
make ui-install
```

`make setup` runs `uv sync` followed by `make apply-patches`, which re-applies the
vendored fixes in `patches/` to sonora after every sync. If you add a new dependency
with `uv add`, re-run `make apply-patches` (or just `make setup`).

### Run Development Servers

```bash
# Engine only (headless)
make dev-core DIR=/path/to/your/project

# Engine + hot-reload Vite dev server
make dev DIR=/path/to/your/project
```

The server starts on `http://localhost:8000`. The tuvl dev console is at
`http://localhost:8000/insight` when `TUVL_DEV_MODE=true`.

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

Applied automatically by `make setup` / `make setup-all`. To apply manually:

```bash
make apply-patches
```

## Code Style

### Python

We use `ruff` for linting and formatting:

```bash
# Format code
uv run ruff format .

# Check linting
uv run ruff check .

# Fix auto-fixable issues
uv run ruff check --fix .
```

### TypeScript

We use ESLint and Prettier:

```bash
# Format
npm run format

# Lint
npm run lint
```

## Testing

### Python Tests

```bash
cd engine
uv run pytest

# With coverage
uv run pytest --cov=tuvl_engine
```

### Writing Tests

Place tests in `tests/` directory:

```python
# tests/test_nodes.py
import pytest
from tuvl_engine.nodes.base import node, NODE_REGISTRY

def test_node_registration():
    @node("test_node")
    async def my_node(ctx):
        return ctx
    
    assert "test_node" in NODE_REGISTRY
```

## Pull Request Process

1. **Fork** the repository
2. **Create a branch** for your feature: `git checkout -b feature/my-feature`
3. **Make your changes** with clear commit messages
4. **Add tests** for new functionality
5. **Run tests** to ensure everything passes
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

### PR Description Template

```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Refactoring

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests pass
- [ ] Manual testing performed

## Checklist
- [ ] Code follows project style
- [ ] Self-reviewed code
- [ ] Documentation updated
- [ ] No breaking changes (or documented)
```

## Project Structure

```
tuvl/
├── engine/               # Core Python engine
│   ├── src/tuvl_engine/
│   │   ├── api/         # FastAPI routes
│   │   ├── core/        # Configuration, logging
│   │   ├── datasources/ # Database connections
│   │   ├── engine/      # Workflow engine
│   │   ├── models/      # Model loading
│   │   ├── nodes/       # Node registry
│   │   └── repositories/# Data access
│   └── tests/
├── cli/                  # CLI tool
│   └── src/tuvl_cli/
│       └── commands/
├── ui/                   # React UI (optional)
│   └── src/
└── documentation/        # This documentation
    └── docs/
```

## Adding Features

### New Node Types

1. Create node in `engine/src/tuvl_engine/nodes/`
2. Register with `@node("name")` decorator
3. Add tests in `engine/tests/`
4. Document in `docs/concepts/nodes.md`

### New Step Kinds

1. Add handler in `engine/src/tuvl_engine/engine/runner.py`
2. Update `_run_*_step` pattern
3. Add tests
4. Document in `docs/concepts/workflows.md`

### New Configuration Types

1. Add loader in appropriate module
2. Update `load_all_*` function
3. Add validation
4. Document in `docs/configuration/`

## Documentation

Documentation uses MkDocs with Material theme.

### Build Locally

```bash
cd documentation
pip install mkdocs-material mkdocstrings[python]
mkdocs serve
```

### Writing Docs

- Use clear, concise language
- Include code examples
- Add diagrams with Mermaid
- Cross-reference related pages

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/tuvl-io/tuvl/issues)
- **Discussions**: [GitHub Discussions](https://github.com/tuvl-io/tuvl/discussions)
- **Discord**: [Join our Discord](https://discord.gg/tuvl)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
