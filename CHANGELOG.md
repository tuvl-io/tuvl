# Changelog

All notable changes to **tuvl** are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [PEP 440](https://peps.python.org/pep-0440/) calendar
versioning (`YEAR.MAJOR.MINOR`), with a semver-shaped git tag form (`v2026.2.4`)
normalised at publish time. See `release-please-config.json` for the automation
contract.

---

## [2026.3.2.0] — 2026-07-18

Security release — two fixes from an internal full-codebase audit.

### Security

- **Token revocation is now enforced on gRPC transports.** The gRPC verifiers
  checked signature and expiry but never the revocation blacklist, so a token
  revoked via `Logout`/`RefreshToken` was still accepted over gRPC (the Insight
  UI's primary transport) until its natural expiry. Both servicers now verify
  through a shared helper that delegates to the same chain as REST — signature,
  revocation, and expiry checks can no longer drift per transport.
- **`tuvl init` no longer writes the database password into
  `datasources/postgres.yaml`.** The generated DataSource YAML now references
  `${POSTGRES_PASSWORD}` (env-only — loading fails closed when unset) and
  `${POSTGRES_HOST:<default>}`-style references for the non-secret connection
  fields, so the committable YAML and `tuvl ship`-built images carry no
  secrets, and the Helm chart's `env`/`existingSecret` injection takes effect
  in-cluster. Existing projects: replace the literal `password:` in
  `datasources/postgres.yaml` with `${POSTGRES_PASSWORD}` (the value already
  lives in `.env`).

### Changed

- Marketing portal (`tuvl.io`) and docs site (`tuvl.dev`) align to
  `v2026.3.2.0`; the TypeScript SDK `@tuvl/client` bumps to `2026.3.2`
  (semver form) in lockstep.

---

## [2026.3.1.0] — 2026-07-13

Adds a first-class path from a validated project to a production deployment.

### Added

- **`tuvl ship`** — package a project for production in one command. It runs the
  full `tuvl validate` pass (errors abort the ship; `--strict` also blocks on
  warnings), generates a production `Dockerfile` and `.dockerignore` plus a Helm
  chart under `deploy/chart/<name>/`, then builds the container image. Flags:
  `--tag` sets the image reference (defaults to `<name>:<version>`), `--no-build`
  writes the artifacts only, `--push` pushes after a successful build, and
  `--force` regenerates files that already exist (otherwise hand-edited artifacts
  are left untouched).
- The generated image is production-shaped by construction: a multi-stage `uv`
  build, a non-root runtime user, `TUVL_ENV=production` (no dev routes, no Insight
  UI, JSON logs, telemetry on), a `/health` HEALTHCHECK, and `tuvl run` as the
  entrypoint. The Helm chart wires liveness/readiness probes to `/health` and
  reads secrets (`TUVL_BISCUIT_PRIVATE_KEY`, `POSTGRES_PASSWORD`, LLM keys) from a
  referenced Kubernetes Secret.

### Changed

- The validation pass is now exposed as a reusable `run_validation()` entry point
  shared by `tuvl validate` and `tuvl ship`.
- Marketing portal (`tuvl.io`) and docs site (`tuvl.dev`) align to `v2026.3.1.0`;
  the TypeScript SDK `@tuvl/client` bumps to `2026.3.1` (semver form) in lockstep.
- **Positioning moves from "beta" to "early stable."** The API and YAML schemas
  are now treated as stable and versioned; the PyPI classifier is
  `Development Status :: 5 - Production/Stable`. "Early" signals the release is
  still maturing quickly with additive changes, not that it is pre-production.

---

## [2026.2.6.1] — 2026-07-05

Hardening patch from the first real-world shakedown: every fix below was found
by building the public example projects against 2026.2.6.

### Fixed

- **HITL resume broke workflows using the dict context form.** Resume rebuilt
  the model allowlist with inline logic that only understood the string/list
  forms, so `context: {models: [{name, version}]}` yielded an empty allowlist —
  every repository call after resume raised `PermissionError`. Resume now uses
  the same context extractors as a fresh run and also restores the
  **version-pin map**.
- **ISO-string writes crashed typed columns.** Table-model classes bypass
  pydantic coercion, so LLM-extracted JSON (`"2026-06-01"` for a `date` column,
  numeric strings, UUID strings) failed inside asyncpg. Repository `create`/
  `update` now coerce string values against the column type; unparseable
  strings pass through to the DB layer's real error.
- **Non-JSON-native values crashed responses and suspensions.** Workflow
  response envelopes are serialized with `jsonable_encoder` (date/Decimal/UUID
  survive), and the HITL context-snapshot serialiser handles `date` and
  `Decimal`.
- **`tuvl test` ran with an empty node registry** — project configs, custom
  `nodes/*.py`, and the built-in RAG runners are now loaded before test
  execution, matching the server lifespan.
- The root route declares `response_model=None`, fixing startup with FastAPI
  versions that reject the `RedirectResponse | dict` inference.

### Security

- **SQL statements and bound parameters no longer reach clients.** SQLAlchemy
  `DBAPIError` text embeds the full statement and parameters (which can carry
  PII); `safe_detail` strips everything from the `[SQL:` marker, and every step
  runner now records `_last_error` through `safe_detail` instead of raw
  exception text.

### Changed

- Embedding calls pass the registry-declared `dimensions` to the provider, so
  Matryoshka-capable models (e.g. `gemini-embedding-001`) are truncated to the
  collection's declared dimension instead of returning their native size.

---

## [2026.2.6] — 2026-07-03

Hardening release from the full-codebase review: all High and Medium findings
fixed, plus new coverage that pins them.

### Security

- **Auth no longer blocks the event loop.** bcrypt verification/hashing runs in
  the threadpool on both the REST and gRPC auth surfaces, so a login spray can't
  stall the server. Federation admin file-IO and the dev evaluate-trace judge
  call also moved off the loop.
- **Untrusted content is fenced out of instruction positions.** RAG
  `context_injection` reaches the model as a delimited user-role message (never
  system); the test-suite evaluator states its instruction before the fenced step
  data; supervisor steer text is templated; the supervisor judge only sees
  declared tool names (undeclared calls surface as a count).
- **Tenant isolation enforced end-to-end.** `bind_principal_context` is now an
  async dependency (as a sync one its ContextVar writes never reached the
  endpoint), workflow trigger routes and the operator API bind the principal, and
  cross-worker control directives are re-checked against the run's tenant.

### Fixed

- **Every MCP step call crashed** — the step passed a float where `mcp` requires
  a `timedelta` read timeout, and the failure was masked as the error route.
- **A supervisor/operator abort no longer crashes an unrouted workflow** — an
  unmapped `aborted` exit ends the run cleanly, and `tuvl validate` warns when it
  isn't mapped.
- **Cross-worker agent control survives quiet channels and Redis restarts** —
  the control subscriber previously died seconds after boot (socket timeout on a
  quiet pub/sub) and never recovered.
- Typed workflow triggers return **422** (not 500) when the body fails the
  declared input schema; a Router condition on a missing context field no longer
  raises `TypeError` on ordered comparisons.

### Added

- **Agent runtime ceilings** (all configurable): a paused run escalates to abort
  after `TUVL_AGENT_PAUSE_MAX_S` (default 300s) instead of pinning its DB
  connection forever; each tool call is bounded by `TUVL_AGENT_TOOL_TIMEOUT_S`
  (default 300s, a ceiling hit ends the run); the supervisor's LLM judge is
  bounded by `TUVL_AGENT_JUDGE_TIMEOUT_S` (default 30s) and runs as a subtask so
  it can't stall deterministic rules.
- **`supervisor.on_judge_error: ignore | pause | abort`** — opt-in fail-closed
  LLM supervision when the judge errors or times out (default stays fail-open).
- Abort directives now land between tool calls, so a multi-tool turn can't
  outlive them.
- **HITL `auth.required_group` is now enforced on resume.** When the paused
  step declares it, only members of that group (or `iam:admin`) can submit the
  response — the requester can no longer approve their own request. Previously
  the block was only a UI hint and resume was owner-or-admin.

### Changed

- `POST /api/agents/runs/{id}/steer` validates its body with a model — a
  missing/empty `message` returns 422 (was 400).
- The unshipped `set_budget`/`set_max_iterations` control channel was removed.
- Published dependency metadata now carries compatible ranges instead of exact
  pins (`uv.lock` keeps the audited pins); dev tooling moved to a PEP 735
  dependency group (`uv sync --group dev`); `sse-starlette` dropped (unused).
- CI runs on `develop` pushes and PRs (previously `main` only).

---

## [2026.2.5.1] — 2026-07-02

### Fixed

- **`tuvl validate` now accepts the spec-wrapped document form.** The validator
  read `Workflow` (`steps`/`context`/`trigger`) and `AgentModel` (`model`) fields
  at the document root, so the spec-wrapped form the runtime supports (and the
  scaffolded samples/skills use) failed validation ("No steps defined",
  "'model' field is required") even though it ran fine. The validator now promotes
  `spec.*` to root exactly like the loaders. It also sources its field-type set
  from the model loader's `TYPE_MAP`, so `type: enum` (and any future type) is
  accepted instead of rejected as "unknown type".

---

## [2026.2.5] — 2026-07-01

### ⚠️ Breaking — AutonomousAgent `goal` is now `steering`

The inline `agent.goal` field is renamed to **`agent.steering`** (no alias — the
old key is ignored). Update any `kind: AutonomousAgent` steps. `steering` is the
agent's persistent, always-injected instruction.

### Added

- **Per-agent steering & skills.** `agent.steering_files` (always injected) and
  `agent.skills` (injected when relevant) are now per-agent markdown scoped to
  `agents/<workflow>__<stepId>/{steering,skills}/`. Files are enforced to that
  directory, so same-named files never collide across agents and an agent can only
  read its own. Authored/edited in the Insight canvas; `tuvl validate` checks
  scope + existence.
- **Agent Orchestrator / Supervisor.** An optional per-workflow `spec.supervisor`
  block watches each `AutonomousAgent` run live and can **pause / abort / steer** it
  mid-loop (at the cooperative iteration boundary) via deterministic `rules`
  (`tool_repeated` / `budget_fraction` / `iteration_reached`) and/or an LLM
  `criteria` judge (gated by `every_n_iterations`, reusing the shared judge). New
  reserved exit `"aborted"`. The LLM policy can live inline (`criteria`) or in a
  scoped `.md` file (`criteria_file`, under `agents/<workflow>__supervisor/steering/`).
- **Supervisor as a visual node.** In the Insight canvas the supervisor is a
  first-class **off-spine node** — add it from the palette (one per workflow) and
  configure it inline (judge-model dropdown, criteria or a criteria `.md` editor,
  `on_violation`, `every_n_iterations`, rules). Every field serializes to
  `spec.supervisor` and round-trips back onto the node.
- **Operator API + Agents dashboard.** `GET /api/agents/runs`, `…/{id}`,
  `…/{id}/trace`, and `POST …/{id}/{abort,pause,resume,steer}` (scopes
  `agent:observe` / `agent:control`, tenant-scoped); a live Insight **Agents** page
  with an iteration timeline and controls. Cross-worker via an optional Redis mirror
  (no-op without Redis).
- **Agent observability.** OTel `MeterProvider` + counters (`agent.iterations`,
  `tool_calls`, `aborts`, `budget_exceeded`, `supervisor_actions`); per-run
  `AgentTrace`; Spectrum now captures per-iteration frames (`agent_trace` on
  `TraceStep`) instead of one opaque step.
- **`tuvl keys generate`.** Generates the production `TUVL_BISCUIT_PRIVATE_KEY`
  Ed25519 key (`--write` to update `.env`); production `tuvl run` requires it.

### Fixed

- **Insight canvas:** manually-authored `@node("name")` code now syncs to the
  Runner field, so hand-written Functional nodes save (previously only AI-generated
  code synced).

---

## [2026.2.4.1] — 2026-06-30

### Fixed

- **OpenAPI / Swagger now documents workflow request bodies.** A workflow trigger
  with a `trigger.input_schema` previously rendered no request body in Swagger
  (and "Try it out" sent an empty payload), because workflow handlers read the
  body manually (`request.json()`) and override their signature to
  `(request, session)` — leaving FastAPI nothing to introspect. `mount_workflows`
  now injects the resolved input model's JSON schema into the route via
  `openapi_extra`, so the body renders and is editable. Runtime input validation
  is unchanged; GET / no-input workflows still document no request body.
  (`core/api/manager.py`)

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
