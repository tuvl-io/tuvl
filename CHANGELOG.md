# Changelog

All notable changes to **tuvl** are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [PEP 440](https://peps.python.org/pep-0440/) calendar
versioning (`YEAR.MAJOR.MINOR`), with a semver-shaped git tag form (`v2026.2.4`)
normalised at publish time. See `release-please-config.json` for the automation
contract.

---

## [2026.2.4] — 2026-06-25

**First stable release.** tuvl leaves beta. The earlier `2026.2.3bN` /
`2026.2.4b1` pre-release line has been withdrawn from PyPI / npm and removed
from the repository — `2026.2.4` is the first supported release and the
baseline for everything that follows. There is no upgrade path from the
withdrawn betas other than the one breaking change noted below.

tuvl is a YAML-driven workflow orchestration engine: declare your data models
and multi-step AI workflows in plain YAML and tuvl mounts them as
production-ready FastAPI endpoints — no backend boilerplate, local-first, with
zero lock-in.

### ⚠️ Breaking — workflow step `kind` names are now PascalCase

Every workflow step `kind` now follows a single, consistent **PascalCase**
convention, matching the document-level kinds (`Workflow`, `ModelDefinition`,
`AgentModel`, …). This is a hard rename: the previous mixed
lowercase / `snake_case` / `kebab-case` names are **rejected at load time** by
closed-set validation.

| Old `kind` | New `kind` |
|---|---|
| `functional` | `Functional` |
| `agent` | `Agent` |
| `router` | `Router` |
| `api_call` | `APICall` |
| `mcp` | `MCP` |
| `model-op` | `ModelOp` |
| `response` | `Response` |
| `HumanInTheLoop` | `HumanInTheLoop` *(unchanged)* |
| `AutonomousAgent` | `AutonomousAgent` *(unchanged)* |

Acronyms stay upper-case (`MCP`, `APICall`) to match the existing
`LLMJudgeConfig` document kind.

**Migration.** Update the `kind:` value of every step in your
`workflows/*.yaml`. Nothing else changes — step **config keys** (`agent:`,
`http:`, `mcp:`, `routes:`, `operation:`, `runner:`, …) are unaffected, only the
`kind:` value. `tuvl validate` flags any step still using an old name.

### Workflow engine

- **Eight composable step kinds**, a finite closed set validated by Pydantic at
  load time so any LLM can target the contract reliably:
  - `Functional` — run a registered Python node (your escape hatch).
  - `Agent` — a single LLM call with a prompt template and structured output.
  - `Router` — branch on a condition, including a multi-way **`match:` switch**
    that emits a context field's value as the route signal for data-driven
    branching without pushing logic into an LLM.
  - `APICall` — outbound HTTP request mapped into context.
  - `MCP` — invoke a tool over the Model Context Protocol (stdio or SSE).
  - `ModelOp` — create / read / update / delete a data model, no Python.
  - `Response` — shape and return the final HTTP response.
  - `HumanInTheLoop` — pause for human approval and resume where it left off.
- **`AutonomousAgent` — a bounded ReAct tool-loop step.** The model is given a
  goal and a declared set of tools (each `agent.tools[].ref` names another step
  in the workflow), autonomously chooses which to call, observes results, and
  re-decides until it emits one of a declared `outcome.enum` — routed like any
  other step. Autonomy is bounded by the workflow contract: tools and exits are
  closed author-declared sets, and the loop is capped by `max_iterations`
  (default 8) / optional `token_budget`. Tools default to returning their
  result to the agent only; `writes_context: true` opts a tool into mutating the
  shared context. Reserved abnormal exits: `max_iterations` /
  `budget_exceeded` / `error`. Built on LiteLLM native tool-calling — no new
  dependency.
- **Stateless ASGI execution** — sub-second cold start, a shared in-process
  `context` dict, and linear scaling with `--workers N`.

### Data & models

- YAML `ModelDefinition` → SQLModel tables, Pydantic schemas, and
  auto-generated CRUD REST endpoints, with per-model scope enforcement.
- Model and workflow **versioning** with composite-keyed tables to prevent
  cross-version collisions.

### Tuvl Insight — developer portal

Installed via `tuvl[standard]`, active only when `TUVL_DEV_MODE=true`, never
served in production:

- Visual workflow editor with live graph + canvas test mode, form-driven model
  / datasource / LLM designers, and full IAM / federation management.
- **Tuvl Lens** (run one node in isolation) and **Tuvl Spectrum**
  (full per-step execution trace).
- **Insight AI Chat** — an in-editor assistant for the Workflow Canvas that can
  draft and revise custom `Functional` node implementations; dev-mode-only and
  guarded by the dev key, so it adds zero surface to a deployed engine.

### Security

- **Biscuit token** auth with cryptographic offline verification and
  per-endpoint scope enforcement; fails closed in production (refuses the dev
  sentinel key and `TUVL_DEV_MODE` on a production host).
- **PII masking** — fields declared `secure: true` arrive as `"*****"` at every
  streamed `StepEvent` (SSE / gRPC / Spectrum), not just OTel spans.
- **gRPC token-expiry enforcement** at parity with REST (`exp()` honoured).
- **Multi-tenant mode** (`tuvl init --multi-tenant`) — `tenant()` binding,
  fail-closed data-access boundary, and Postgres RLS tooling
  (`tuvl db generate-rls` / `check-rls`).

### Observability

- Native **OpenTelemetry** spans across HTTP requests, workflow runs, individual
  steps, LLM calls, DB transactions, and the `AutonomousAgent` loop
  (`autonomous_agent.iteration` / `.tool_call`).
- Structured `structlog` JSON in production; a compact coloured logfmt console
  renderer under `TUVL_ENV=development`.

### SDKs

- **TypeScript `@tuvl/client`** — REST / SSE / gRPC-Web transports with
  automatic selection, HITL resume, version pinning, typed CRUD, and
  `agentProgress` / `AgentProgress` to surface `AutonomousAgent` live loop
  frames.

### Tooling & quality

- CLI: `tuvl init` (with `--sample` / `--multi-tenant`), `dev`, `run`, `test`
  (LLM-as-a-judge), `validate`, `db generate-rls` / `check-rls`,
  `stream-watch`.
- Full test suite green (sqlite-backed, `uv run --extra dev pytest`).

### Compatibility & dependency floor

- Python `>= 3.13`; Postgres `16+` recommended.
- Key runtime pins (`pyproject.toml`): `litellm[google] == 1.86.2`,
  `pydantic == 2.13.4`, `pydantic-settings == 2.14.1`,
  `python-dotenv == 1.2.2`, `typer == 0.26.5`.

### Version map

| Surface | Package | Version | Channel |
|---|---|---|---|
| Engine | `tuvl` | `2026.2.4` | PyPI |
| Developer portal | `tuvl-insight` | `2026.2.4` | PyPI (via `tuvl[standard]`) |
| TypeScript SDK | `@tuvl/client` | `2026.2.4` | npm |
| Documentation | `tuvl.dev` | `2026.2.4` (`latest`) | GitHub Pages |
| Marketing site | `tuvl.io` | `v2026.2.4` | static |

— Sooraj Rajagopalan, tuvl.io

[2026.2.4]: https://github.com/tuvl-io/tuvl/releases/tag/v2026.2.4
