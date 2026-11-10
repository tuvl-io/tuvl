# Contributing

Thank you for your interest in contributing to tuvl! By taking part you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **A bug fix or a small improvement:** open a pull request.
- **A new feature, or a change to behaviour:** open an issue first and describe what you need. The document
  kinds, engines and artifact types are closed sets (see below), so a design discussion comes before code.
- **A security problem:** do not open an issue; follow [SECURITY.md](SECURITY.md).

## Development Setup

### Prerequisites

- Python 3.13 or 3.14 (`requires-python = ">=3.13,<3.15"`)
- [uv](https://docs.astral.sh/uv/) package manager
- PostgreSQL 16+
- Node.js 20+ and pnpm (only for Insight UI development)

### Clone and Install

```bash
git clone https://github.com/tuvl-io/tuvl.git
cd tuvl

# Install Python dependencies
make setup

# Install UI node_modules (run once, only if touching ui/)
make ui-install
```

### Run Development Servers

```bash
# Engine only (headless)
make dev-core DIR=/path/to/your/project

# Engine + Tuvl Insight dashboard
make dev-ui DIR=/path/to/your/project

# Engine + Vite hot-reload dev server (UI development)
make dev DIR=/path/to/your/project
```

The server starts on `http://localhost:8885`; the Insight console is at
`http://localhost:8885/insight` in dev mode.

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
assets into `insight/src/tuvl_insight/static/`.

## Testing

```bash
make test                                  # full suite
uv run pytest tests/runtime -q             # one area
uv run pytest tests/validate/test_map_rules.py -q   # one file
make typecheck                             # mypy over src/ (a CI gate)
```

For a change to the runner, journal or an engine, measure it: `uv run python scripts/bench_steps.py`
(needs a disposable Postgres database in `TUVL_TEST_PG_URL`, because it drops and recreates the runtime tables; the model is a local stub) prints runs/s, steps/s, step latency and SQL statements
per step. Quote the before and after in the pull request.

Tests live under `tests/`, one directory per `src/tuvl/` package (`contract/`, `validate/`, `runtime/`,
`api/`, `cli/`, …). Tests that need Postgres run when `TUVL_TEST_PG_URL` is set
(`postgresql+asyncpg://user:pass@localhost:5432/db`) and skip otherwise. Model-client stubs
(`tuvl.llm.acompletion`) for agent tests live in `tests/llm_stubs.py` — use them instead of rolling new fakes.

## Pull Request Process

1. **Fork** the repository
2. **Create a branch from `develop`**: `git checkout -b feat/my-feature develop`
3. **Make your changes** with clear commit messages, each [signed off](#sign-your-commits)
4. **Add tests** for new functionality
5. **Run** `make check && make test`
6. **Open the PR against `develop`**, not `main`, with a clear description linking the issue it closes

`develop` is where all work lands. `main` only receives a release from `develop`, and CI refuses a pull
request to `main` from any other branch.

A maintainer reviews the pull request on this repository. tuvl is developed in a private repository and each
release is published here as a tagged snapshot, so a merged change is applied there and appears in the next
tagged release; it keeps your authorship (`Author:` and `Signed-off-by`).

### Sign your commits

tuvl uses the [Developer Certificate of Origin](DCO). It says you wrote the change, or have the right to submit it,
under this project's license. Add a `Signed-off-by` line to each commit with `git commit -s`:

```
Signed-off-by: Your Name <you@example.com>
```

Use your real name and an address you can be reached at. A pull request whose commits are not signed off
will be asked to add it (`git rebase --signoff main`).

### Commit Messages

Use conventional commits:

```
feat: add a retry option to the tool engine
fix: correct signal routing in the runner
docs: update workflow configuration guide
test: cover the map engine's concurrency limit
refactor: simplify the journal writer
```

## Project Structure

```
tuvl/
├── src/tuvl/             # Engine + CLI (one package)
│   ├── contract/         # The document model: schema, types, TEL expressions, registry
│   ├── runtime/          # Durable runner, queue, journal, and one module per engine
│   ├── api/              # REST: triggers, runs, approvals, MCP export; dev/ is the live-state part of the dev API
│   ├── cli/              # The production CLI and the verb tree (verb-first: lint, run, serve, generate, …)
│   ├── llm/ storage/ webhooks/                 # Model client, blob stores, outbound webhooks
│   └── core/             # Config, datasources, auth/IAM, artifacts, telemetry
├── insight/              # the tuvl-insight package: the developer tooling and the built UI
│   └── src/tuvl_insight/ # validate/ (tuvl lint project), spec/ codegen/ lock/ testing/ devmcp/, cli/commands/, api/dev/
├── ui/                   # Insight React/Vite source
├── tests/                # pytest suite, mirroring src/tuvl/; tests/insight/ is the tooling's (needs tuvl-insight)
└── docs/                 # Agentic manual + subsystem deep-dives
```

Key invariants to respect (the agentic manual's golden rules are the contract):

- **Closed sets.** Engines (`pending code tool decide llm loop human map parallel`), document kinds and
  artifact types are closed sets — a new one is a design discussion first, not a PR. It touches the
  schema, validate, codegen, Insight and the docs together.
- **One dispatch site.** Engines are dispatched from the runner (`runtime/runner.py`, `runtime/engines.py`);
  never add a parallel dispatch path.
- **Contracts are enforced twice:** statically by `tuvl lint project`, at runtime by the runner.

## Documentation

- `docs/tuvl-agentic-manual.md` is the ground-truth YAML/agent contract — any
  behavior change that touches the contract must update it in the same PR.
- The docs site ([tuvl.io/docs](https://tuvl.io/docs)) mirrors `docs/` from this
  repo; it is built with MkDocs in the separate docs repository.

## Releasing

Maintainers cut releases by hand. All work lands on `develop`; `main` only receives a release from it, and a
`vX.Y.Z` tag on `main` of [tuvl-io/tuvl](https://github.com/tuvl-io/tuvl) starts the PyPI publish workflow (it runs nowhere else).

1. On `develop`, `make bump VERSION=2.0.1` sets the version in both `pyproject.toml` files, their cross-pins and
   the fallback pin in `tuvl init`, then refreshes `uv.lock`. `make check-version` (and the test
   suite) fail when those files disagree.
2. Move the `[Unreleased]` entries of `CHANGELOG.md` under the new version.
3. Commit on a branch, open a PR into `develop`, merge when CI is green.
4. Open a PR from `develop` to `main` and merge it with a **merge commit**, not a squash, so the two branches keep one history.
5. Tag the merge commit on `main` and push the tag (`git fetch && git tag vX.Y.Z origin/main && git push origin vX.Y.Z`). Pushing the tag
   starts the PyPI workflow, so confirm first.
6. In the other repositories: bump `@tuvl/client` in `tuvl-npm`, update the install page and
   "what's new" in the docs repository, then merge `develop` into `main` in the docs and portal repositories (a merge to `main` is live).

The publish workflow refuses a tag that is not on `main`, or whose version does not match `pyproject.toml`
(`v2.0.1-beta.2` is `2.0.1b2`), before it lints, tests, builds and uploads.

## Getting Help

- **Issues**: [GitHub Issues](https://github.com/tuvl-io/tuvl/issues)
- **Anything else**: [developer@tuvl.io](mailto:developer@tuvl.io)
- **Security reports**: see [SECURITY.md](SECURITY.md) — never a public issue

## License

tuvl is licensed under the Apache License, Version 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). By contributing you
agree that your contribution is licensed under it (Apache-2.0 §5: a contribution you submit is under these terms
unless you state otherwise), and your sign-off certifies the [DCO](DCO). You keep the copyright in your contribution.
New source files need no license header; the repository's `LICENSE` covers them.
