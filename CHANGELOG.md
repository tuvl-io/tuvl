# Changelog

All notable changes to **tuvl** are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [Semantic Versioning](https://semver.org) as of `1.0.0`.
See `release-please-config.json` for the automation contract.

---

## [2.0.0] — 2026-10-10

tuvl 2.0 is a redesign. Workflows are graphs of typed **agents**, every run is
**durable and journaled**, and projects are built **spec-first**. There is no
1.x compatibility layer: 1.x workflows (`steps:` with `kind:` step types) fail to
load with an error pointing at the 2.0 format.

### Highlights

- **Typed agents and seven engines.** Each agent declares `inputs`, `outputs`
  and the signals it emits; its engine is `code`, `tool` (db / http / mcp),
  `decide` (rules, then an optional decision model), `llm`, `loop` (bounded
  tool use), `human` or `pending`. Named ends replace `Response` steps. A typed
  type language and a statically checked expression language (TEL).
- **`tuvl validate`** checks contracts, data flow, types and policy statically
  (V001–V026) and prints each workflow's determinism profile and worst-case
  token cost.
- **Durable runtime.** Postgres-backed runs, journal and checkpoints; a work
  queue with leases (any worker continues any run); waits for people and tool
  approvals; compensation; operator pause/resume/abort/steer; replay;
  `tuvl run --role api | worker`.
- **Decisions and loops.** `decide` never falls back silently; a decision
  model reports its confidence (a missing one is an error, never 1.0) and can
  be a hosted model behind an OpenAI-compatible endpoint (`api_base` +
  `${ENV}` `api_key` on `type: decision`). **Laya and Jev** are built in over
  System One (`POST /v1/systemone`): `tuvl init --decision-model laya`
  scaffolds a self-hosted Laya (laya-serve container) and `tuvl ship` adds it
  to the Helm chart (`--with-laya/--without-laya`); `loop` tools are
  agents, child workflows or allow-listed MCP tools, with per-tool approval,
  budgets and a supervisor. **Judges** are calibrated, cached artifacts.
- **Spec-driven tooling.** `tuvl spec new | analyse | status | diff`, a derived
  TaskPlan, deterministic `tuvl codegen`, offline `tuvl test` with mocks and
  replayed fixtures, `tuvl lock`, the `tuvl mcp` dev server and 13 skills for
  coding agents.
- **Insight 2.0.** Specs as a row of tiles with the task board below and
  each spec editable in its own window, a canvas over the same YAML,
  runs with breakpoints, step mode and approvals, change-engine checks against
  pinned fixtures, judge calibration and a rules editor.
- **Surfaces.** REST + SSE (gRPC removed), OpenAPI generated from contracts,
  MCP export of workflows, `@tuvl/client` 2.0 with typed client generation.

- **`tuvl spec check`** (CLI, MCP `spec_check`, `GET /dev/specs/check`): a spec's `# Data`, `# Flows`
  and examples are now a small Markdown grammar, checked with zero tokens against rules S001–S016 —
  unrouted engine signals, unreachable ends, unsafe model steps, data-flow gaps, secure fields reaching
  a model, name collisions, missing access, bad rules and loop tools. It is the first step of the CI
  gate. `tuvl spec new` and the `--sample` spec use the grammar.
- **Fixes from the sdp-cop live demo.** `tuvl.data_search` failed on a `filter` rendered from a uuid
  input (`community_id: "{{ community_id }}"`) — filters are JSON-encoded like ingest metadata now.
  A code agent's input model was cached by workflow and agent id only, so an edited contract kept
  validating against the old one until restart; the contract is part of the key now.
- **Code agents are edited in Insight, with AI assist** (as in 1.0, rebuilt for 2.0). The Engine tab
  of a `code` agent is a Python editor (full-screen mode, ⌘S): saves are syntax-checked, normalized by
  codegen so the contract's signature survives, and hot-reloaded. **✦ Assist** uses a project LLM
  AgentModel with the agent's contract, its models' fields and the `agent-code` skill; it returns the
  whole file into the editor and never saves. `GET|PUT /dev/agents/{run}/source`,
  `POST /dev/agents/{run}/assist`, `GET /dev/agents/assist/models` (dev mode only).
- **Dogfooding fixes (sdp-cop built end to end with the skills).** Spec grammar: compensators
  (`compensate `x`` + `T` step) and `- tests: waived — why`; `spec check` resolves an `(existing)`
  model to the project's schema and flags reserved signals routed from `code` steps (as the validator
  does); `spec verify` compares defaults by value (`false` = `False`) and checks `compensate`; task
  status honours test waivers and counts compensators a rollback test ran. Tests record
  `compensated`; a mock that only injects `signal: error` is fault injection, not T001. The db tool
  stores a uuid/number/date rendered into a string column as text. `policy.model_only_decisions:
  allow` accepts a model-only decision feeding writes on purpose (V013), visibly in the YAML.
- **Insight referee views.** The Specs page shows `spec check` findings inline and in a Findings
  tab, and a Traceability tab (`spec verify`): step → agent → tests, example test results with their
  failure reasons; steps open their canvas node and the canvas links back. Run/Step are disabled
  while a model the workflow uses isn't configured. A test that ends somewhere unexpected now names
  the agent and error that ended it. Concurrent test runs in the dev server are serialized (a run
  swapped decision providers and a status reload cleared registries under another run).
- **The coding agent writes the task list; tuvl checks it.** `tuvl spec analyse` / `--apply` are
  removed (CLI, MCP `spec_analyse`/`spec_apply`, `/dev/specs/analyse|apply|analysis`, Insight's
  Analyse button and plan review), with the analysis model: `tuvl init` writes no `llms/analysis.yaml`
  or `config.yaml`, and `ProjectConfig.spec.analysis_model` is gone. `tuvl spec check` checks the
  task list (P001–P005: format, targets, coverage, no estimates or typed statuses, staleness) and its
  JSON lists the spec's section anchors and hashes. `TaskPlan` tasks lose `effort` and gain
  `example` targets. Task status now also follows `spec check` (blocking findings block every task)
  and `spec verify` (a target that contradicts the spec is `in_progress`). The `tuvl-plan` skill is
  rewritten for the agent-written list; `tuvl-task-loop` builds from the spec and loops on
  `spec verify`. The `--sample` task list covers every model, step and example.
- **`tuvl spec verify`** (CLI, MCP `spec_verify`, `GET /dev/specs/verify`): compares each spec flow
  with its workflow, each Data model with its ModelDefinition and each example with its honest test
  result (R001–R010), and prints the traceability matrix spec step → agent → tests. Contradictions
  fail; unfinished work fails with `--strict`, which joins the CI gate.
- **Honest tests.** A test that only passes through unmocked `pending` placeholders is reported
  **STUBBED**, never counts towards task status, and fails `tuvl test --strict`. Tests seed rows with
  `given:` and set the caller with `as:`, so `code`, `tool db` and rule-based `decide` agents run for
  real; mocking them is warning T001. Estimates are no longer shown (`spec status`, Insight).
- **Model safeguards.** `tuvl validate` reports which models the workflows need and their
  environment variables; an unknown model reference is an error (V028) and undeclared credentials a
  warning (V029, an error for `tuvl ship`). A production server refuses to start while a model's
  variable is missing; `/health` reports model readiness; new `tuvl doctor [--live]`; `tuvl ship`
  lists the exact secrets the deployment needs.
- **Ship only runtime files.** `tuvl ship` writes `.dockerignore` as an allowlist, so specs,
  task lists, tests, agent rules and skills, compose files and any other file stay out of the image
  (V027 refuses development files; regenerate an older `.dockerignore` with `--force`). Ship now
  refuses `pending` agents (V010), and a production server never loads `Test`/`TaskPlan` documents.

### Upgrading from 1.x

Rewrite workflows as 2.0 agent graphs (`version: tuvl/v2`): `Functional` →
`code` agents (`@agent`, typed `In`/`Out` from `tuvl codegen`), `ModelOp` /
`APICall` / `MCP` → `tool` agents, `Router` → `decide`, `Agent` → `llm` or
`loop`, `HumanInTheLoop` → `human`, `Response` → named ends. `kind: test` files
become `kind: Test` documents. Model CRUD routes are off unless a model opts in
with `spec.api.crud`. The 2.0 examples (`tuvl-io/examples`) show each pattern.

### Removed

- **gRPC / gRPC-Web transport.** The `/grpc` mount (sonora) and its three
  services — `ExecutionService`, `DevService`, `IamService` — are gone, along
  with the `grpcio`, `sonora` and `grpcio-tools` dependencies, `make proto`,
  `make apply-patches` and the vendored sonora patch. REST + SSE is the only
  application transport. (OTLP span export to a collector still uses gRPC;
  that is telemetry, not the API.)
- The 1.x Insight AI code chat (`POST /dev/ai/chat`) and the UI's unused 1.x
  workflow, Lens and Spectrum types. Code agents are written in an editor or by
  a coding agent using `tuvl mcp`.
- Insight's hand-rolled protobuf codecs and the `@protobuf-ts/*` dependencies;
  the UI calls the existing REST endpoints directly.

### Added

- `tuvl.contract` — the 2.0 type language (`str`, `int`, `float`, `bool`,
  `date`, `datetime`, `uuid`, `json`, `enum[...]`, `list[T]`, `Model[.variant]`,
  `T?`, plus the `{type, description, default}` long form): parser with
  column-precise errors, canonical formatter, and `build_model()` compiling a
  field map to a strict Pydantic model.
- `tuvl.contract.tel` — TEL, the 2.0 expression language for decide rules,
  approval guards, test assertions and `{{ }}` templates: a strict parser,
  a static type checker against declared inputs (unknown fields, impossible
  enum values and type mismatches are compile-time errors), and a sandboxed
  evaluator that only walks mappings and lists.
- `tuvl.contract.schema` — Pydantic models for the 2.0 documents: `Workflow`
  (agents as a discriminated union over the seven engines, each engine's
  parameters under its own key, named outputs, policy, supervisor), `Test`
  and `TaskPlan`, plus the `judge`/`rules` artifact types and
  `AgentModel` `type: decision`. Unknown keys are rejected everywhere;
  embedded types, TEL conditions and cron expressions are syntax-checked.
- `tuvl schema export [--kind Workflow|Test|TaskPlan|all]` (hidden) prints the
  JSON Schema Insight's forms are rendered from.
- Artifact loading validates `judge` and `rules` artifacts; `AgentModel`
  rejects an unknown `spec.type` and validates decision providers.
- The config loader routes `version: tuvl/v2` documents (`Workflow`, `Test`,
  `TaskPlan`) to `tuvl.contract.registry`, validated against the 2.0 schema.
  Errors are reported in the author's terms (`spec.agents[triage].decide.rules[0].when`)
  with hints for YAML pitfalls (booleans from bare `on`/`off`, commas and `?`
  inside `{ }`). 1.x workflows still load into the 1.x engine until it is
  removed; the rejection path (with a migration pointer) is in place.
- `tuvl validate` shape-checks 2.0 documents and no longer warns "no
  workflows" for a 2.0-only project.

- `tuvl.validate` — the 2.0 workflow validator (spec Appendix B rules
  V002–V012, V014, V016–V019, V022): route targets, signal coverage per
  engine, must-available data flow with a witness path, producer/consumer
  type compatibility, TEL and template type checks against declared inputs,
  artifact and model references, unguarded cycles, and the determinism /
  worst-case-token profile. Models are typed from `ModelDefinition` YAML, so
  validation does not boot the runtime.
- `tuvl validate` reports 2.0 findings with `file:line` and rule codes, prints
  each workflow's profile, and gains `--json` for CI and coding agents.

- `tuvl.runtime.models` — durable run storage for the 2.0 runner:
  `tuvl_system_runs` (status, lease, sequence counter, control flags),
  `tuvl_system_run_events` (append-only journal), `tuvl_system_run_checkpoints`
  and `tuvl_system_approvals`, created on the primary datasource at boot and
  tenant-scoped in multi-tenant mode.
- `tuvl.runtime.journal` — the run journal: events and checkpoints share one
  per-run sequence allocated atomically (`UPDATE … RETURNING`), values are
  stored as JSON with `secure: true` fields redacted at any depth, oversized
  event payloads become size markers, and whole runs export/import as
  `tuvl.journal/v1` documents (the fixture and replay format).
- `tuvl.runtime.queue` — the run queue: `enqueue`, `claim` (oldest first,
  `FOR UPDATE SKIP LOCKED`; also re-claims runs whose lease expired, which is
  crash recovery), lease `heartbeat`, owner-guarded `release`, `requeue` for
  resumes; a `Worker` loop with bounded concurrency and heartbeats; and
  `run_inline` for the synchronous HTTP path (answer 202 once `sync_timeout`
  passes while the run continues). Lease times use the database clock.
- `tuvl.runtime.runner` — the 2.0 runner: walks the graph one
  agent at a time; checks inputs and the outputs declared for the emitted
  signal (`contract_violation`), applies `budget.timeout` and `budget.retry`,
  routes, renders named ends, honours pause/abort requests, and checkpoints
  every step. An agent's database writes, its checkpoint and a lease-ownership
  check commit in one transaction. Rollback ends, unhandled errors and aborts
  run compensators newest-first. Engines plug in via
  `tuvl.runtime.engines`; the `pending` engine returns typed stubs.
  `transaction: per_run` is refused for now.
- The `code` engine: `from tuvl import agent, Ctx`; `@agent("name")`
  registers an async function, `agents/*.py` load via
  `tuvl.runtime.code.load_agents` (project root on `sys.path` so
  `agents._generated` imports resolve). The function gets its inputs as one
  typed object (its annotated class, or a model built from the contract) and
  returns outputs, `(outputs, signal)` or a bare signal. `ctx.db`
  (`get`/`list`/`create`/`update`/`delete`) is limited to `spec.models`,
  refuses writes under `effect: read`, and writes inside the agent's
  transaction; `ctx.http`, `ctx.artifact(ref)`, `ctx.log`,
  `ctx.idempotency_key`.
- The `tool` engine. `db`: create/read/update/delete/list/upsert
  with rendered `where`/`values` (validated and coerced into the model),
  `not_found`/`conflict` signals, writes inside the agent's transaction; the
  result lands in the agent's single output (V007 checks the count
  and type). `http`: `${VAR}` env expansion, `expect.status`, `http_<status>`
  signals when routed, 429 → `rate_limit` and 5xx → `transient` (retryable),
  `map` from `response.*` paths, `idempotency: auto`. `mcp`: stdio/SSE servers
  from `type: mcp` artifacts; object results spread across declared outputs.
- The `llm` engine: `system`/`prompt`/`skills` (inline or
  artifacts; `{{ }}` in the prompt renders against the view), the inputs as a
  typed `## Input` block, and structured output from the agent's `outputs`
  (`response_format` json_schema, also stated in the system message). With
  more than one normal route the schema gains `outcome: enum[...]`, which
  picks the signal. Invalid JSON, a schema mismatch, an unrouted outcome or a
  missing per-signal field is `parse_error`; `finish_reason: length` is
  `budget_exceeded`; provider errors classify as `timeout`/`rate_limit`/
  `transient`/`llm_error` for `budget.retry`. Non-judge guardrails run on
  input and output; judge guardrails are refused (`not_supported`) until the
  judge runtime lands. Each call journals an `llm_turn`.
- The `human` engine: opens an approval request (rendered
  title/instruction/assignee, `show`n inputs, a form derived from `outputs`
  and decorated by `human.form`) in the agent's transaction and suspends the
  run as `waiting_human`. `tuvl.runtime.human.decide` validates the form
  against `outputs`, enforces the authorisation rules (required group, no
  self-approval, owner or `iam:admin` otherwise) and re-queues the run;
  `route_on` maps bool fields to `true`/`false` and enums to their values.
  Workers sweep expired requests, whose runs take the `expired` route.
- Step idempotency keys count finished steps (suspensions excluded), so they
  are stable across retries, crash re-execution and suspend/resume.
- **Breaking:** models expose no `/models/*` CRUD routes by default. Enable operations one by one with `spec.api.crud: [list, read, create,
  update, delete]`; each route requires the model's IAM permission for its
  tier (`read`, `write`, `delete`; `spec.access` overrides and group ACLs
  apply). The admin scope listing shows only exposed tiers.
  `spec.api.expose_model_crud` in `system.yaml` stays as a deployment-wide
  master off. `spec.schema` is removed — schemas are always generated, since
  typed contracts need them; `validate` warns where it is still set.
- The 2.0 REST surface: each workflow's `trigger.http`
  route is mounted from YAML (body or query plus path params form the input,
  validated against `trigger.input`; default-deny, `public: true`,
  `required_scope`/`required_group`). Runs execute inline up to
  `sync_timeout` and answer with the end's body and status, `202` with
  `status_url`/`events_url` (and the approval descriptor when waiting), or
  `500`/`409` with the run error. `POST /api/workflows/{name}/runs`,
  `GET /api/runs/{id}` (`runs:read`, own runs; `iam:admin` any),
  `GET /api/runs/{id}/events` (SSE: replay from `?from_seq=` or
  `Last-Event-ID`, then live), `POST /api/runs/{id}/{pause|resume|abort}`
  (`runs:control`; aborting a waiting run cancels its requests),
  `GET /api/approvals`, `POST /api/approvals/{id}` and
  `POST /api/runs/{id}/human/{agent_id}` (`approvals:decide` plus the
  agent's group rules). `steer` answers 501 until the supervisor lands.
- `tuvl run --role all|api|worker --concurrency N`; the app starts the 2.0
  runtime at boot (loads `agents/`, mounts triggers, starts the worker).
- The scheduler enqueues 2.0 runs instead of executing them (`trigger:
  schedule:<fire time>`); the check-and-enqueue runs under the advisory lock,
  so each fire is enqueued once across processes.
- Built-in code agents `tuvl.data_search` (hybrid RRF over pgvector and
  full-text) and `tuvl.data_ingest`, replacing the 1.x `DataSearch` /
  `DataIngest` nodes; `code.with` gives any code agent static, templated
  parameters (`ctx.params`). The `tuvl.` agent prefix is reserved.
- Validation: V025 checks built-in parameters; V015 warns on write or
  destructive steps without `compensate` when later steps can still fail,
  and on a declared tool effect weaker than the derived one.
- **Breaking — the 1.x engine is removed.** Workflows without
  `version: tuvl/v2` fail to load (and `validate`) with a migration hint; 1.x
  `kind: test` files are rejected. Removed: the step runner, step kinds and
  streaming, `nodes/` and `@node` (use `agents/` and `@agent`), the 1.x HITL
  router and instance table, the versioned `/{api_version}/run/{name}` route
  and workflow-version toggle/fork, `/api/agents/runs` (folded into
  `/api/runs`), Insight Lens/Spectrum (rebuilt on the journal in Insight 2.0),
  and `tuvl test` (Test 2.0 arrives with the spec tooling). The scheduler,
  system manifest, admin listing, boot-time artifact check and dev API read
  the 2.0 registry; `tuvl stream-watch` starts a run and follows its SSE
  events. `tuvl init --sample` writes a 2.0 ticket-triage project
  (`llm`, `code`, `human`, `tool` agents) that passes `validate --strict`;
  the scaffold's AGENTS.md describes 2.0 and the 1.x step skills are gone.
  `validate` checks `agents/` and that every `code` agent's `run` exists
  (V025).
- Runtime observability: one OTel span per run segment
  (`tuvl.run`) and per agent invocation (`tuvl.agent <id>`) with
  `tuvl.workflow`, `tuvl.agent`, `tuvl.engine`, `tuvl.determinism`,
  `tuvl.signal`, `tuvl.tokens`, `tuvl.attempt` and `tuvl.error_type` (error
  status); agent and workflow `hooks` fire `before_step`, `after_step` and
  `on_error`, observe-only.
- `llm` structured output lists every field as required (optional ones as
  nullable) and drops schema defaults: providers otherwise omit defaulted
  properties even when the value is in the input (seen with Gemini).
- `tool: http` bodies and query params, MCP args and ingest metadata are sent
  as JSON-safe values (uuid, date, decimal from typed inputs no longer fail
  serialisation).
- `transaction: per_run`: one data transaction for the whole
  run — each step in a savepoint (a failed or retried step drops only its own
  writes), no checkpoints (a crash reruns from the start), and the run's
  writes, completion and `run_completed` commit together; failures and
  rollback ends discard everything. Pause requests are ignored under
  `per_run`. V021 rejects `per_run` with human agents, approval-gated loop
  tools or a schedule trigger.
- Resilience suite (`tests/resilience`): a worker process SIGKILLed mid-run
  resumes on another worker from its last checkpoint with exactly-once
  writes and a stable `Idempotency-Key`; a human decision made after the
  requesting worker died resumes on another worker; a failing run
  compensates end to end over HTTP.
- The `decide` engine: ordered TEL rules (inline or a `rules`
  artifact; first match wins, the model is not called), then an optional
  decision model — confident → its value, below `min_confidence` →
  `low_confidence`, unavailable → `error / decision_model_unavailable` with no
  fallback; `shadow: true` journals the model beside the rules. Every
  invocation journals a `decision` record (`source: rule#n | model | rules`).
  Decision providers: built-in `litellm` (enum-constrained output, confidence
  from logprobs, retried without them when a model rejects the parameter);
  others register through the `tuvl.decision_providers` entry point. `/health`
  reports each decision model's last status. V013 (model-only decisions
  feeding writes, per `policy.model_only_decisions`) and V026 (provider
  installed, model pinned).
- The `loop` engine: a bounded tool-using agent. Tools are
  `agent:` (an off-spine agent; schema from its contract), `workflow:` (a
  child run with its own journal and gates, `parent_run_id`, tokens counted
  toward the loop, nesting capped by `policy.max_depth`) and `mcp:` (the
  server's `tools/list` filtered by `allow`). Arguments are validated before
  execution; invalid ones come back as an observation. The model ends by
  calling `finish` with the declared outputs (and `outcome`), with one repair
  attempt. Budgets `max_iterations`, `max_tokens`, `max_tool_calls` and per-tool
  `max_calls`; every turn and tool call is journaled; input, output and tool
  guardrails apply. In-loop approval: a matching call suspends the run as
  `waiting_approval` with the loop state in the request; approve, edit
  (re-validated) or reject (an observation) via `POST /api/approvals/{id}`
  resumes it on any worker. V014 rejects human and loop agents as loop tools.
- Loop supervision and live control: `loop.supervisor` (or
  `spec.supervisor`) rules — `tool_repeated`, `iteration_reached`,
  `budget_fraction`, `effect_count` — run every `every_n_iterations`; each
  fires once and acts by `abort` (exits through `aborted`), `steer` (injects
  a message) or `pause` (the loop state is saved; `POST /api/runs/{id}/resume`
  continues it on any worker). Operator requests reach a running loop
  mid-flight through the run's control flag: `pause`, `abort` and
  `steer` (`{message}`). Each action journals a `supervisor` event. Loop tool
  calls are bounded by `TUVL_AGENT_TOOL_TIMEOUT_S`. The 1.x orchestrator,
  `agent_core` and `agent_tools` are removed.
- Validation: V020 enforces `policy` statically — worst-case tokens against
  `max_tokens_per_run` (unbounded cycles included), `max_bounded_agents`,
  `require_human_before` (every path to a guarded effect passes a human; loop
  tools with that effect need `approval`), and `allow_secure_to_llm: false`
  (model-typed inputs carrying `secure: true` fields never reach llm, loop or
  model-backed decide agents). Schema errors are reported as V001.
- Judges: `type: judge` artifacts give typed verdicts
  `{verdict, score, reasons}` (a pass below `min_score` is a fail), cached in
  `.tuvl/judge-cache/` by `sha256(input ‖ judge content hash)` so re-runs are
  free and deterministic; `tuvl.runtime.judges.calibrate` measures agreement
  with the artifact's labelled examples. As guardrails they fail closed —
  `fail` → `guardrail_violation`, `uncertain` / unavailable → `error`
  (`judge_uncertain` / `judge_unavailable`) — and their tokens count toward
  the agent. A loop supervisor's `judge` reviews the trajectory every
  `every_n_iterations` (`on_violation`, `on_judge_error`). The 1.x guardrail
  `llm_judge` check is removed (validate points to judge artifacts).
- Loop fixes from the live support-triage demo: a failed `finish` ends as
  `error` (`parse_error`); when an approver edits a
  call's arguments the observation tells the model what ran (`requested` vs
  `executed`); each model turn has its own timeout with one retry, so a
  stalled provider no longer consumes the loop's whole budget.
- `tuvl test` (Test 2.0): runs `kind: Test` documents through the
  real runner in a throwaway in-memory database, offline by default. Mocks are
  checked against the agent's contract (a stale mock fails); `replay:` serves
  results from a pinned journal; unmocked `llm`, `loop`, `tool` and
  model-backed `decide` agents point to mocks, replay or `--live`, and `human`
  agents must be mocked. `expect` is evaluated structure → `output` (partial
  match) → TEL `assertions` → `judge` (cached; `--allow-uncertain`). Judges
  are calibrated before first use — untrusted ones are reported, `--strict`
  fails (V023). `--json`, `--only`. `tuvl runs pin <run_id>` writes a run's
  journal to `tests/fixtures/` via `GET /api/runs/{id}/export`. TEL
  `contains()` also tests an object's keys.
- Test `expect.wait: human | approval` (an unmocked human agent suspends the
  run) and `expect.path_includes`.
- Spec parsing (`tuvl.spec`): front matter, stable `#`/`##`
  anchors with content hashes (a section covers its sub-sections; whitespace
  edits don't move a hash), and deterministic extraction of `tuvl-example`
  blocks into `Test` documents with line-numbered errors.
- `tuvl codegen [<workflow>] [--check]` (deterministic): renders
  `agents/_generated/schemas.py` (typed inputs/outputs for every agent, trigger
  inputs, end names, referenced model variants); creates or **libcst-merges**
  `code` agent stubs — decorator, annotations, `async` and docstring follow the
  contract while the body and parameter names are preserved; prompt
  placeholders for missing `artifact://` prompts; `Test` files from spec
  `tuvl-example` blocks (stale generated tests removed, hand-written ones never
  touched); a `decide` rules skeleton for empty blocks. `--check` is the CI
  drift gate. New dependency: `libcst`.
- `tuvl lock [--check] [--no-mcp]`: `tuvl.lock` pins the tuvl
  version, AgentModel → model id, decision models (provider + package
  version), every `artifact://` reference (workflows and tests) → resolved
  version + sha256, judge models, and MCP tool input-schema hashes (listed
  live). `tuvl ship` fails on a missing or stale lock (V024) and its Dockerfile
  installs the locked tuvl version instead of floating with
  `--upgrade-package`. In production a loop logs `mcp.schema_drift` once per
  tool whose live schema differs from the lock (V014 W).
- `tuvl spec analyse <spec> [--model] [--apply [--only]] [--json]` (spec
  §17.3): the analysis model answers a structured `Plan` (never YAML); tuvl
  renders models, workflows (`pending`/`decide`/`human` agents, required
  `error` routes, reachable ends, policies), prose artifacts and the
  `<spec>.tasks.yaml` TaskPlan; validates with the plan applied and repairs at
  most twice on the errors the plan introduced; reports diff, notes, tasks,
  tokens and cost; applies the saved plan only on `--apply`. Re-analysis keeps
  implemented agents, existing models and task ids, and flags dropped tasks
  `obsolete` (new `Task.obsolete`). `ProjectConfig.spec.analysis_model`.
  Evaluation set of five reference specs (`tests/eval`, live with
  `TUVL_EVAL_LIVE=1`, nightly workflow).
- `tuvl spec status [<spec>] [--strict] [--no-tests] [--json]`:
  every TaskPlan task's status derived from the project — validation issues
  attributed to their workflow/agent/model, tests run offline, the lock
  checked — with `blocked`/`stale` overlays, obsolete tasks set aside and the
  remaining LLM-estimated hours. `tuvl spec diff` lists stale tasks and
  sections no task references. Analysis plans now target models and artifacts,
  keep task ids by title when a target moves, and drop judge/guardrail
  artifacts the model tries to write as prose.
- `tuvl runs list|get|tail|pin|replay` (`--url`/`--token`, `TUVL_URL` /
  `TUVL_TOKEN`) with a new `GET /api/runs` (newest first, `workflow`/`status`
  filters, the caller's own runs unless admin). `tail` follows the SSE stream
  and can start a run (`--workflow --payload`); `replay` re-runs pinned
  journals offline and fails when the end or output differ. **Removed:**
  `tuvl stream-watch` (use `tuvl runs tail`).
- `tuvl spec new <name>` writes a spec template.
- The `--json` outputs of `validate`, `test`, `runs replay`, `spec status`,
  `spec diff` and `spec analyse` are documented models
  (`tuvl schema export --kind cli`), snapshot-tested. `spec diff --json` is now
  an object: `{"specs": [...]}`.
- `tuvl init --preset gemini|openai|anthropic|ollama` writes current model ids
  for agents (`llms/default.yaml`) and analysis (`llms/analysis.yaml`,
  `config.yaml` `spec.analysis_model`); `-y/--yes` runs without prompts.
  `--sample` is now a 2.0 ticket-triage project with a spec, TaskPlan,
  generated tests and `tuvl.lock` that passes `validate --strict`,
  `codegen --check`, `lock --check`, `test` and `spec status --strict` as
  created (regression test runs the gate in subprocesses).
- `tuvl test`: `tool: db` agents run offline against the test database.
- `tuvl validate` no longer warns about a `config.yaml` without `dirs`.
- Commands given `-d <project>` from another directory now use that project's
  `.env` and directories (`use_project`); loading a project twice in one
  process no longer fails on already-defined model tables.
- `tuvl mcp [-d] [--allow-write]`: a stdio MCP server for coding
  agents. Read tools: `validate`, `spec_status`, `spec_diff`, `list_agents`,
  `get_contract` (with the generated class names), `explain_error`, `get_run`,
  `get_run_events`, `spec_analyse` (dry run), `codegen` (dry run), `test`.
  Write tools with `--allow-write`: `spec_apply`, `codegen_write`,
  `run_workflow`, `pin_fixture`. Refuses `TUVL_ENV=production`.
  `tuvl.validate.codes` holds the V-code table.
- Coding-agent skills for 2.0: `AGENTS.md` and 13 skills
  (`tuvl-spec`, `tuvl-plan`, `tuvl-task-loop`, `agent-code|tool|llm|loop|decide|human`,
  `tuvl-data`, `tuvl-test`, `tuvl-harden`, `tuvl-ship`) shipped with tuvl and
  installed by `tuvl init`; `tuvl skills update [--diff]` refreshes them, mirrors
  them to `.claude/skills/`, makes `CLAUDE.md` import `AGENTS.md`, and removes
  the nine 1.x skills.
- From the first P3 acceptance run (a coding agent built the refund spec to a
  green CI gate): `tool: db` `where` values take the column's type (a uuid
  string matches a uuid column; a bad value is a clear `tool_args_invalid`);
  analysis makes tasks with nothing to measure `manual` instead of leaving
  them `untracked` forever; skills document judge artifacts, calibration
  fixtures, guardrail checks and mocking db reads.
- From the KYC acceptance run: judge tasks the analysis model plans itself are
  dropped (tuvl derives one per acceptance bullet); a duplicate had become an
  unclosable manual task.
- Insight dev API v2 (`/dev/*`, dev mode): project summary, project files as
  text (YAML comments survive), validate / test / codegen / spec status, diff,
  new, analyse, apply in the CLI's `--json` shapes, schema export, canvas
  layout sidecar (`.tuvl/layout/<workflow>.json`). **Removed:** the `/dev/nodes`
  aliases and the YAML-parse-only `/dev/validate`.
- Insight: Spectrum, Lens and the Agents dashboard are gone, and so is the 1.x
  workflow canvas (it wrote 1.x YAML). Workflows is a YAML editor with inline
  validation and the workflow profile until the 2.0 canvas lands; the
  validation panel shows V-codes, lines and warnings.
- Insight **Runs** page: live run list with workflow/status filters, the
  journal timeline per agent (loop turns, tool calls with effects and
  arguments, decisions with their source, judge verdicts, supervisor
  actions), an approvals inbox with forms built from the human agent's
  outputs and tool approve / edit / reject, pause / resume / abort / steer,
  start a run, pin a run as a fixture and replay a workflow's fixtures
  (SAME/DIFF). Dev API: `POST /dev/runs/{id}/pin`, `POST /dev/runs/replay`.
- Insight **Specs** page (now the home page): spec list and template, a
  markdown editor, Analyse with a token estimate shown first, a plan review
  (summary, cost, notes, validation errors, tasks, per-file diff with partial
  apply) and the derived task board with stale/blocked badges that links to
  each task's target. `spec status --json` tasks carry their `target`.
- Reloading the project in a running server no longer fails with "Table …
  already defined" after a model-version toggle dropped a class (a model
  definition now replaces its stale table).
- Insight **workflow canvas** (2.0): the YAML stays the single source
  (comments kept); Start / agent / End nodes laid out left to right with
  positions in `.tuvl/layout/<workflow>.json`; agent cards by engine and
  determinism class (pending cards hollow) with contract, budget and
  guardrail chips and one outlet per signal (unrouted required signals in
  red); Decide (rules, model, threshold) and Loop (tools, approvals, caps)
  summaries; a task-oriented palette; a panel with Contract (types, upstream
  suggestions), Engine (decide rules table, YAML block), Budget, Governance and
  Routes tabs; Change engine that keeps the contract; drag to route; live
  validation of the unsaved draft (`POST /dev/validate/draft`) with issue badges,
  red input ports and a summary bar; a data-flow overlay. Canvas and YAML tabs
  edit the same text.
- Insight developer controls (dev mode only): breakpoints on canvas cards
  (dev runs pause before the agent; kept in `.tuvl/breakpoints.json`), step
  mode (pause before every agent and every loop tool call), view and edit the
  next agent's inputs while paused, then continue; Probe runs one agent live
  with hand-entered inputs. Dev API: `/dev/breakpoints/{wf}`,
  `/dev/runs/start {step}`, `/dev/runs/{id}/context`, `/dev/agents/probe`.
- Insight change-engine check: after swapping an agent's engine on the canvas,
  "Check vs fixtures" replays the workflow's pinned fixtures against the
  unsaved draft with that agent executing (everything else from the
  recording) and shows SAME/DIFF per fixture plus the before → after
  validate profile; "Generate stub" runs codegen when the new engine is
  `code`. Dev API: `POST /dev/change/check`; `run_test(live_agents=...)`.
- Insight judge editor (Artifacts page): model, rubric and thresholds; ask
  the judge on a subject (live, against the unsaved draft); mark its verdict
  right or wrong to save the subject under `fixtures/judge/` and append it to
  the calibration set; run calibration and see agreement vs `min_agreement`.
  Rules artifact editor with "which rule matches" for a sample context. Both
  can be created from a template. AI Models: `type: decision` models
  (provider `laya | jev | litellm`). Dev API: `/dev/judges/try`,
  `/dev/judges/calibrate`, `/dev/judges/label`, `/dev/rules/try`;
  `judges.from_document`, `judge_with`, `calibrate_with`.
- Insight browser tests (Playwright, `ui/e2e`): every page renders the
  `tuvl init --sample` project without page or console errors, plus the
  authoring exit flow — new workflow → change engine → save → breakpoint run →
  continue → pin fixture → edit rules → check SAME / DIFF. Run locally with
  `scripts/insight-smoke.sh`; CI runs it on every push (runner Postgres/Redis,
  no image pulls).
- MCP export: workflows with `trigger.mcp.enabled` are tools on
  `/mcp` (streamable HTTP, stateless). Tool input schema = `trigger.input`;
  the output is an envelope clients can validate — `{outcome: "completed",
  run_id, end, status, body}` (body typed as one of the 2xx end bodies) or
  `{outcome: "pending", run_id, state, approval?}` for a run that waits on a
  person/approval or outlives the call window; error ends are `isError`.
  `readOnlyHint` / `destructiveHint` derived from the workflow's strongest
  effect. Same Biscuit auth as REST plus scope `mcp:connect`; `tools/list`
  shows only the workflows the caller may run; a progress notification per
  started agent.
- OpenAPI from contracts: each trigger route documents its path/query
  parameters or JSON body from `trigger.input`, one response per end status
  with the body's schema (`outputs.<end>.type`, else inferred from the body
  template), and `202` with the run descriptor. `tuvl.contract.surface` is the
  shared source for OpenAPI and MCP.
- `tuvl ship` Helm chart: `split: true` deploys an `api` Deployment (the
  Service target) and a `worker` Deployment (`tuvl run --role worker`) scaled
  independently (`worker.replicaCount`, `worker.concurrency`); the default is
  one `--role all` Deployment. Pods carry `app.kubernetes.io/component`.
- Documentation for 2.0: a rewritten agentic manual and new subsystem docs —
  `runtime`, `engines`, `decide`, `loop-and-tools`, `judges`, `spec-driven`,
  `insight`, `api`, `cli` — plus updated `auth` and `observability`, README
  and contributor `AGENTS.md`. The 1.x step-kind docs are removed.
- Checkpoints keep real values (they are the resume state) and are redacted
  on export.

- Per-signal outputs: an agent's `outputs` may be keyed by its
  routed signals; data flow and type checks then use the set for the signal on
  each edge, so a consumer can require a value only some outcomes produce.

### Fixed (validate)

- The report table no longer strips `[...]` from messages (Rich read them as
  markup); 2.0 artifact references count as uses.

- `tuvl validate` silently ignored YAML files that failed to parse (their
  `kind` could not be read, so no validator owned them); they are now
  reported as errors, with a fix-it hint for the common flow-mapping mistakes.
- `POST /api/insight/spectrum/stream` — the Spectrum trace as Server-Sent
  Events (one `step` event per node, then a `final` event), with a
  `stop_after_step_id` breakpoint. Powers the Workflow Canvas test mode and the
  Spectrum page, which previously streamed over gRPC.
- `POST /dev/evaluate-trace` now reports `model_used`.

### Fixed

- `tuvl test` crashed in a plain install: `aiosqlite` (its in-memory
  database) was only a dev dependency. Found by smoke-testing the built wheels
  against the examples.
- `tuvl.lock` no longer records the litellm version for `provider: litellm`
  decision models — the model id is the pin, and a litellm patch release made
  `tuvl lock --check` fail on every fresh install. Plugin providers still pin
  their package version.
- `tuvl spec analyse` on an existing workflow merges instead of rebuilding it:
  the trigger (path, method, input), ends (status, body), metadata and other
  settings are kept, the plan only adds agents, models, new ends and policy
  keys it doesn't set, and differences are reported as notes. A workflow the
  plan doesn't change isn't rewritten, so its comments survive. Found by
  analysing the examples, where it would have changed public API paths and
  dropped response bodies.
- Spec analysis no longer writes artifacts (and tasks) planned for agents that
  are already implemented without them, and no longer turns an acceptance
  bullet that restates an example (case or punctuation aside) into a task.
- Spec analysis sees the project's vector collections (and is told they are
  not models), keeps acceptance tasks to bullets written in the spec's
  Acceptance section, drops tasks for artifacts the plan doesn't provide, and
  doesn't add an unpinned model next to a pinned one.
- `tuvl spec status`: an off-spine agent used as a loop tool is covered when
  its loop is (a test can't reach it any other way).
- `tuvl spec status`: an `end == 'x'` assertion task is covered by a Test's
  `expect.end: x`, and an agent a Test's run waits at (`expect.wait`) counts
  as covered.
- Replays and `tuvl test` runs no longer stop at Insight breakpoints or step
  mode (`TestRunner.dev_pause`).
- Insight "Change engine": switching to decide / human / loop moves the
  `default` route to the first new signal (those engines have none), drops
  `effect` for engines other than code/tool, and writes the preset's outputs
  when the agent has none.
- `POST /api/insight/spectrum` returned the final state without masking
  `secure: true` fields (per-step snapshots were masked); it is now masked.
- Clearing a user's name or phone to an empty string from Insight now
  persists (REST `PATCH /auth/admin/users/{id}` applies explicit empty
  strings; the gRPC codec dropped them).

### Version map

| Surface | Package | Version | Channel |
|---|---|---|---|
| Engine | `tuvl` | `2.0.0` | PyPI |
| Developer portal | `tuvl-insight` | `2.0.0` | PyPI (via `tuvl[standard]`) |
| TypeScript SDK | `@tuvl/client` | `2.0.0` | npm |
| Documentation | `tuvl.dev`, `tuvl.io/docs` | `2.0.0` | GitHub Pages |
| Marketing site | `tuvl.io` | `v2.0.0` | static |

---

## [1.0.1] — 2026-08-03

Patch release: correctness fixes only. No API or schema changes.

### Added

- Model fields accept generated-default literals `default: now`
  (`timestamp` / `timestamptz`) and `default: today` (`date`), mirroring the
  existing `default: uuid4`. Use them to opt a field into a server-generated
  value on create.

### Fixed

- **Nullable `uuid` / `date` / `timestamp` / `timestamptz` fields no longer
  receive a silent generated value on create.** The model loader treated *any*
  default-less field of these types as "auto-generate a value", so a nullable
  `uuid` foreign key persisted a dangling random UUID, and a default-less
  `date` / `timestamp` persisted `today` / `now()` instead of `NULL` — on every
  row created through ModelOp CRUD (raw-SQL nodes were unaffected, which is why
  the corruption stayed hidden). Generation is now limited to the cases where it
  is safe and intended: a `uuid` **primary key**, and the `created_at` /
  `updated_at` audit-column convention. Every other default-less field of these
  types now persists as `NULL` (or is required when declared `required: true`).

  **Migration.** Any field that (perhaps unintentionally) relied on the old
  implicit `today` / `now` / `uuid` now stores `NULL` — which is almost always
  what the YAML author already believed was happening, given these fields carry
  no default. To keep a generated value, opt in explicitly: `default: uuid4`,
  `default: now`, or `default: today` — or, for timestamps, rename the column to
  `created_at` / `updated_at`. `uuid` primary keys and `created_at` /
  `updated_at` are unchanged.

- CLI commands no longer emit the `Settings loaded` startup log. Constructing
  `Settings` on import logged at info level, so quick commands (e.g.
  `tuvl --version`) printed a log line alongside their output. The log moved to
  server boot — `tuvl run` / `tuvl dev` still log it — and the routine
  `Project config loaded` line dropped to debug.

## [1.0.0] — 2026-07-26

First official stable release. Establishes the SemVer baseline and folds in
scheduled triggers, an API-security hardening pass, and a full internal
quality sweep. Everything below is the state of the engine at first publish.

### Added

- **Scheduled workflow triggers.** `spec.trigger.schedule: "<5-field cron>"`
  (UTC, 1-minute granularity) runs a workflow on a schedule, alongside or
  instead of an HTTP `path`. Skip-on-miss (no catch-up); single-fire across
  workers via a Postgres advisory lock; scheduled runs execute with no
  principal. Knob `TUVL_SCHEDULER_ENABLED` (default on); single-tenant only.
  Authorable from the Insight workflow trigger panel.
- **Global CRUD kill-switch.** `spec.api.expose_model_crud: false` in
  `.tuvl/system.yaml` (`SystemConfig`) unmounts every auto-generated
  `/models/*` route — the routes are absent, not merely denied — leaving only
  workflow APIs. `TUVL_EXPOSE_MODEL_CRUD` overrides the file. Editable from the
  Insight Settings "API Access" section. Restart-required.
- **Per-model group ACLs.** `spec.access.{read,write,delete}_groups` on a
  `ModelDefinition` pins CRUD tiers to IAM groups: a caller needs the scope
  **and** group membership (`iam:admin` bypasses). Undeclared tiers cascade
  restrictively (write→read, delete→write).
- **`spec.trigger.public: true`** opts a workflow into anonymous access (see
  the default-deny change below). Workflow manifests expose the `public` flag;
  `@tuvl/client`'s `WorkflowManifest` carries it.
- `tuvl validate` / `tuvl ship` now discover config by the same recursive,
  `kind`-dispatched walk the runtime uses — YAML in any subdirectory and `.yml`
  files are validated, and `EmbeddingRegistry`/`EmbeddingConfig`,
  `CollectionRegistry`/`CollectionConfig`, `RedisConfig`, and
  `FederationProvider` are now checked. An unknown `kind:` is a validation error.
- `tuvl stream-watch --timeout` (default: no read-timeout on the event stream).

### Changed

- **BREAKING — workflow triggers deny by default in production.** Every trigger
  route (REST, the versioned `/{schema_version}/run/{name}` route, and gRPC
  `RunWorkflow`) now requires a valid bearer token by default, even when the
  workflow declares no `required_scope`/`required_group`. Anonymous access is an
  explicit opt-in via `spec.trigger.public: true`. Dev mode (`tuvl dev`) exempts
  unscoped workflows so quickstarts run tokenless. **Migration:** add
  `spec.trigger.public: true` to any workflow (e.g. user-registration endpoints)
  that must remain callable without a token; combining it with
  `required_scope`/`required_group` is a validation error.
- **Default engine port is now `8885`** (was `8000`) for `tuvl dev` and
  `tuvl run`, the Insight portal (`http://localhost:8885/insight/`), the `tuvl
  ship` container/Helm chart, and the Insight dev proxy. 8885 spells "TUVL" on a
  phone keypad (T·U·V=8, L=5) and avoids the crowded 8000/8080/8888 range.
  Override with `--port` or `TUVL_DEV_PORT`.
- **Semantic Versioning.** Releases follow SemVer (`MAJOR.MINOR.PATCH`):
  breaking changes bump the major, features the minor, fixes the patch.
- The REST and gRPC IAM surfaces are now thin adapters over one transport-neutral
  service (`core/auth/iam_service.py`) — identical behavior on both transports
  (see Security).
- Models without an explicit `spec.datasource` bind to the primary `DataSource`
  (the one with `metadata.primary: true`) rather than a hardcoded name, so a
  scaffolded project creates its tables out of the box.
- `tuvl ship` Dockerfiles float the engine to the latest release the project's
  `pyproject.toml` allows (`uv sync --upgrade-package tuvl`); other dependencies
  stay lockfile-pinned. `tuvl ship` warns when a kept `Chart.yaml` `appVersion`
  has gone stale against the build version.

### Fixed

- Diagnostic logs now go to **stderr**, keeping stdout clean for actual command
  output. Previously the log stream (including the import-time "Settings loaded"
  line) was written to stdout and interleaved with results — e.g. `tuvl --version`
  emitted a JSON log line before the version, breaking anything parsing it.
- `tuvl db generate-rls` / `check-rls` now load the target project, so tenant
  model tables appear (previously only system tables were emitted).
- A `ModelOp` database error now returns the workflow's declared error-route
  response instead of an unhandled 500, and no longer leaks the SQL statement or
  bound parameters into the error body.
- HumanInTheLoop suspension works under the `tuvl dev` session key (the dev
  principal is now a stable UUID).
- The Insight workflow editor no longer corrupts version-pinned
  `context: {models: [...]}` to `'[object Object]'` on save.
- `tuvl validate --strict` no longer false-warns on projects that legitimately
  have no custom node files.
- Per-model / rotated LLM credentials take effect immediately (resolved as
  per-call parameters instead of a process-global env mutation).
- The engine fails fast at startup when `TUVL_BISCUIT_PRIVATE_KEY` is missing in
  production, rather than booting and erroring on the first request.
- Crashed workers no longer leave phantom agent runs in the operator API
  (Redis mirror entries expire).

### Security

- Production default-deny on workflow triggers; per-model group ACLs; the CRUD
  kill-switch (above).
- REST/gRPC IAM parity closed two gRPC-only gaps: login now runs the same
  constant-time dummy-hash verify (no user enumeration by response timing), and
  `GetMe`/`RefreshToken` run the same token authorization checks (a token
  missing its `user()` fact is now rejected instead of silently accepted).
  Federation path sanitization and file I/O aligned to the hardened REST path.
- Anonymous callers get a uniform `UNAUTHENTICATED` for unknown, disabled, and
  auth-required workflows across REST and gRPC, so the workflow namespace can't
  be probed without a credential.
- No signing key, no boot (above); the dev-mode superuser shortcut hard-exits
  if enabled in a `TUVL_ENV=production` process.

### Internal / Quality

- Transport-neutral IAM service (REST+gRPC), a single shared step-execution loop
  behind `run()`/`run_streaming()`, and the 6,724-line Insight workflow editor
  split into a reviewable module tree. Validator and runtime share one config
  discovery + document normalization path. New: a protobuf wire-conformance test
  tying the hand-written TS gRPC codecs to the `.proto` definitions, an Insight
  vitest suite + CI job, and a scoped mypy ratchet. 648 engine tests + 133 UI
  tests.

### Version map

| Surface | Package | Version | Channel |
|---|---|---|---|
| Engine | `tuvl` | `1.0.0` | PyPI |
| Developer portal | `tuvl-insight` | `1.0.0` | PyPI (via `tuvl[standard]`) |
| TypeScript SDK | `@tuvl/client` | `1.0.0` | npm |
| Documentation | `tuvl.io/docs` | `1.0.0` | GitHub Pages |
| Marketing site | `tuvl.io` | `v1.0.0` | static |
