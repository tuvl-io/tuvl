# tuvl — rules for agents working on this repository

This repository is the **tuvl 2.0 engine**: typed YAML workflows of agents, run by a durable,
journaled runtime, with spec-driven tooling and the Insight UI. It is not a tuvl *project* — for the
rules that apply to projects built with tuvl, see `src/tuvl/skills/AGENTS.md` (scaffolded into every
project) and `docs/tuvl-agentic-manual.md`.

## 1. Where things live

| Path | What |
|---|---|
| `src/tuvl/contract/` | The 2.0 document model: `schema.py` (Pydantic for Workflow, Test, TaskPlan, artifacts), `types.py` (type language), `tel.py` (expression language), `registry.py`, `surface.py` (input/end schemas for OpenAPI and MCP) |
| `src/tuvl/validate/` | `tuvl validate`: per-engine signals, data flow, types, policy, profiles; rule codes in `codes.py` (V001–V026) |
| `src/tuvl/runtime/` | The durable runner (`runner.py`), queue and leases (`queue.py`), journal (`journal.py`), host (`host.py`), engines (`code.py`, `tool.py`, `llm.py`, `decide.py`, `decision.py`, `loop.py`, `human.py`), judges, dev controls (`devtools.py`) |
| `src/tuvl/api/` | REST: triggers, runs/events/approvals (`runs.py`), auth helpers, MCP export (`mcp_export.py`), the dev API for Insight (`dev.py`) |
| `src/tuvl/spec/`, `codegen/`, `lock/`, `testing/`, `devmcp/` | Spec analysis and status, codegen, `tuvl.lock`, the offline test runner, `tuvl mcp` |
| `src/tuvl/cli/` | Typer commands; `--json` shapes in `cli/json_output.py` |
| `src/tuvl/core/` | Carried-forward platform: config, models/loader, datasources, auth/IAM (Biscuit), artifacts, telemetry, tenancy |
| `src/tuvl/skills/` | Package-data skills + `AGENTS.md` scaffolded into projects |
| `ui/` | Insight (React + Vite); `pnpm build:cli` writes `src/tuvl_insight/static/` |
| `docs/` | Subsystem docs, mirrored to the docs site by `make sync-internals` there |
| `proposals/` (workspace) | The 2.0 specification and implementation plan |

## 2. Invariants

- **Closed sets.** Engines: `pending code tool decide llm loop human`. Document kinds and artifact
  types are listed in the manual. Never add one without changing the spec, the schema, validate,
  codegen, Insight and the docs together.
- **The YAML is the single source of truth.** Insight and coding agents edit the same files; Insight
  edits keep comments (`yaml` Documents).
- **Contracts are enforced twice:** statically by `tuvl validate` and at runtime by the runner
  (outputs validated against the contract → `contract_violation`).
- **Durability:** run state lives in Postgres (`tuvl_system_*` tables); nothing is kept only in
  worker memory. Per-agent writes commit with the checkpoint.
- **No silent fallbacks** (e.g. an unavailable decision model is `error`, never rules or a default);
  guardrail judges fail closed.
- **Dev surfaces are dev-only:** `/dev/*`, Insight, `tuvl mcp`, breakpoints and step mode never run
  with `TUVL_ENV=production`.
- **Security:** Biscuit tokens; workflow triggers default-deny (`public: true` opt-in); scopes per
  `docs/auth.md`; `secure: true` fields never reach models, logs or exports.

## 3. Working on the code

- Python 3.13, `uv`. Run tests with Postgres: `TUVL_TEST_PG_URL=postgresql+asyncpg://… uv run pytest`.
  Tests that need Postgres skip without it.
- Before pushing: `make check` (ruff format + lint on `src/`) and `make typecheck-gate` (mypy on the
  gated packages, which must stay clean). Format and fix only files you touched.
- UI: `cd ui && pnpm exec tsc -b && pnpm test`; browser tests with `scripts/insight-smoke.sh`.
- Keep comments minimal: only the non-obvious *why*. No narration or patch history.
- Update `CHANGELOG.md` (the `[2.0.0]` section) and the relevant `docs/` page with any user-visible
  change.

## 4. Useful commands

```bash
uv run tuvl init demo --sample -y        # a project that passes the CI gate
uv run tuvl dev -d demo --auto-login     # Insight at http://localhost:8885/insight
uv run tuvl validate -d demo --strict --json
uv run pytest tests/runtime -q
```
