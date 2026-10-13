# tuvl 2.0 — Agentic Manual

> **Audience:** developers and coding agents authoring tuvl projects.
> **Contract:** this manual is the ground truth for tuvl 2.0 documents. Never invent kinds, engines,
> fields or signals that are not described here. When in doubt, ask tuvl: `tuvl validate --json`,
> `tuvl spec status --json`, or the `tuvl mcp` tools.
> **Deeper references:** [runtime](runtime.md) · [engines](engines.md) · [decide](decide.md) ·
> [loop and tools](loop-and-tools.md) · [judges](judges.md) · [spec-driven workflow](spec-driven.md) ·
> [Insight](insight.md) · [API](api.md) · [CLI](cli.md) · [security](auth.md) · [observability](observability.md)

---

## 1. What tuvl is

tuvl runs **typed workflows of agents**. A workflow is a graph: each agent has a typed contract
(`inputs`, `outputs`, the signals it emits) and an **engine** that implements it — a Python
function, a database/HTTP/MCP call, a rules table, one model call, a bounded tool loop, or a person.
Signals pick the next agent or a named end. Every run is **durable** and **journaled**: it survives
restarts, can wait for people, and can be replayed as a test fixture.

Work is **spec-driven**: intent lives in `specs/<name>.md`, in a grammar `tuvl spec check` checks;
the coding agent writes the task list and builds; deterministic tooling (`spec verify`, `codegen`,
`validate`, `test`, `lock`) tracks the project to done. YAML is the single source of truth — the Insight canvas and coding agents edit the
same files.

---

## 2. Project layout

```
my-project/
├── pyproject.toml / uv.lock      # Python deps (uv)
├── tuvl.lock                     # reproducibility pins (tuvl lock)
├── .env / .env.example           # secrets — never in YAML
├── config.yaml                   # optional ProjectConfig (dirs, …)
├── specs/                        # <name>.md (intent) + <name>.tasks.yaml (TaskPlan)
├── workflows/                    # kind: Workflow
├── agents/                       # code-engine implementations, one @agent per file
│   └── _generated/               # codegen output — never hand-edited
├── models/                       # kind: ModelDefinition / EmbeddingRegistry / CollectionRegistry
├── llms/                         # kind: AgentModel (type llm | decision)
├── datasources/                  # kind: DataSource
├── federation/                   # kind: FederationProvider
├── artifacts/                    # prompt/steering/skill .md + kind: Artifact YAML
├── tests/                        # kind: Test; fixtures/ holds pinned run journals
├── fixtures/judge/               # labelled judge calibration subjects
├── .agents/                      # coding-agent rules + skills (tuvl skills update)
└── .tuvl/                        # system.yaml, layout/, dev session, judge cache (managed)
```

Folder names are conventions: discovery is recursive and dispatched by `kind:`.

### Document kinds (closed set)

| `kind` | Notes |
|---|---|
| `Workflow` | `version: tuvl/v2` (§4) |
| `ModelDefinition` | Tables and their typed variants (`Order`, `Order.create`, …) |
| `AgentModel` | `spec.type: llm` (default) or `decision` ([decide](decide.md)) |
| `DataSource`, `RedisConfig` | Connections |
| `EmbeddingRegistry` / `EmbeddingConfig`, `CollectionRegistry` / `CollectionConfig` | Vector search |
| `FederationProvider` | OAuth/OIDC login |
| `Artifact` | Versioned assets (below) |
| `Test` | `version: tuvl/v2` (§8) |
| `TaskPlan` | `version: tuvl/v2`, the task list the coding agent writes (checked by `spec check`) |
| `ProjectConfig`, `TelemetryConfig`, `SystemConfig` | Project and system settings |

Every document uses the envelope `kind` + `metadata` + `spec`. `Workflow`, `Test` and `TaskPlan`
carry `version: tuvl/v2`; a 1.x-shaped workflow fails to load.

### Artifact types (closed set)

| Type | Class | Used by |
|---|---|---|
| `prompt` | prose `.md` | `llm.system`, `llm.prompt` |
| `steering` | prose `.md` | `loop.steering` |
| `skill` | prose `.md` | `llm.skills`, `loop.skills` |
| `guardrail` | YAML | `guardrails.{input,output,tools}` |
| `hook` | YAML | agent/workflow `hooks` (observe-only) |
| `mcp` | YAML | `tool.use: mcp`, loop MCP tools |
| `judge` | YAML | output guardrails, tests, supervisor ([judges](judges.md)) |
| `rules` | YAML | `decide.rules: artifact://…` (reusable decision tables) |

Reference with `artifact://name@N`; pin `@N` for production (an unpinned ref is a V017 warning).
Content is capped at 512 KB.

---

## 3. Types, expressions and templates

### Type language

Used in `inputs`, `outputs`, trigger input, end types, human forms and tests.

| Form | Meaning |
|---|---|
| `str`, `int`, `float`, `bool`, `date`, `datetime`, `uuid`, `json` | Scalars |
| `enum[a, b, c]` | Closed string set |
| `list[T]` | List of `T` |
| `Order` | A `ModelDefinition`'s read shape (model names start uppercase) |
| `Order.create` / `.update` / `.read` / `.expanded` | A specific model variant |
| `T?` | Optional (absent or null; defaults to `null`) |
| `{ type: T, description: str, default: any }` | Long form |

YAML pitfalls: inside a flow mapping, quote anything with a comma or `?` —
`outputs: { verdict: "enum[strong, weak]", note: "str?" }`. Avoid bare `on`/`off`/`yes`/`no` as keys
or values; route keys `true:`/`false:` are accepted and normalised to strings.

### TEL (tuvl expression language)

Used in `decide.rules[].when`, tool `approval.when`, test `assertions` and `{{ }}` templates.

- Paths: `order.total`, `items[0].sku`, `args.amount` — **null-safe** (a missing key yields `null`).
- Literals: strings, numbers, booleans, `null`, lists.
- Operators: `== != > >= < <=`, `in`, `not in`, `and or not`, `+ - * /`, parentheses.
  Comparisons don't chain; `and`/`or`/`not` need booleans (no truthiness); ordering against `null` is `false`.
- Functions (closed set): `len`, `lower`, `upper`, `contains`, `starts_with`, `ends_with`,
  `is_empty`, `now`, `days_between`.
- Statically type-checked by `tuvl validate` against the agent's declared inputs (V008): every root
  name must be an input, fields are checked against models, enum literals must be enum values.

### Templates

A string containing `{{ expr }}` is a template. A value that is **exactly** one `{{ expr }}` keeps the
expression's type (a number stays a number); otherwise values are interpolated as text. Every path a
template reads must be a declared input of that agent (V008).

---

## 4. The workflow document

```yaml
kind: Workflow
version: tuvl/v2
metadata:
  name: refund_flow                      # unique; snake_case
  description: Handle a refund request end to end.   # required for MCP export
  required_scope: refunds:write          # optional token scope gate
  required_group: support                # optional IAM group gate
spec:
  models: [Order, Refund]                # allow-list for tool:db and ctx.db
  trigger:
    http:     { path: /api/refunds, method: POST, public: false, sync_timeout: 30s }
    schedule: "0 * * * *"                # optional cron (UTC)
    mcp:      { enabled: true }          # optional MCP export (api.md)
    input:    { order_id: uuid, reason: str, message: str }
  start: load_order                      # entry agent; defaults to the first agent
  transaction: per_agent                 # per_agent (default) | per_run
  agents: [ ... ]                        # §5
  outputs:                               # named ends
    default:  { status: 200, body: { refund_id: "{{ refund.id }}" } }
    rejected: { status: 200, body: { status: rejected } }
    failed:   { status: 500, rollback: true, body: { error: refund failed } }
  policy:   { max_tokens_per_run: 5000 } # §7
  supervisor: { ... }                    # default supervisor for loop agents
  hooks: [artifact://audit-log@1]
```

### Graph rules

- **No implicit edges.** Every signal an agent can emit must be mapped in `routes` (V004).
- Route targets: an agent id, `END` (= `END.default`) or `END.<name>` for a declared output.
- Agents used only as loop tools or compensators are **off-spine** and can't be route targets (V003).
- Cycles are allowed, but each must pass through a bounded-budget or `human` agent (V005 warning).

### Named ends

| Field | Meaning |
|---|---|
| `status` | HTTP status (default 200) |
| `body` | Response mapping: templates or literals |
| `type` | Optional declared body type (field map); otherwise inferred from `body` for OpenAPI/MCP |
| `rollback` | Reaching this end compensates every completed agent with a `compensate` |

Every template in a `body` must reference keys produced on **every** path to that end (V022).

### Triggers

- `http`: default-deny — a valid bearer token is required unless `public: true` (which can't be
  combined with `required_scope`/`required_group`, V019). The response is the end's `status`/`body`,
  or `202` with the run descriptor when the run outlives `sync_timeout` or waits on a person.
- `schedule`: cron in UTC; each slot fires once across all workers.
- `mcp.enabled`: lists the workflow as a tool on `/mcp` ([API](api.md)).
- Every workflow can also be started with `POST /api/workflows/{name}/runs`.

---

## 5. The agent contract

```yaml
- id: score_candidate                    # [a-z][a-z0-9_]*, unique, stable
  description: Score a CV against similar profiles   # required; also the tool description
  engine: llm                            # pending | code | tool | decide | llm | loop | human
  inputs:  { cv: str, similar: list[Candidate] }
  outputs: { score: int, verdict: "enum[strong, weak]" }
  budget:
    timeout: 30s
    max_tokens: 4000                     # llm / loop / decide with an LLM model
    max_iterations: 8                    # loop
    max_tool_calls: 12                   # loop
    retry: { attempts: 2, errors: [timeout, rate_limit, parse_error, transient], backoff: 1.5 }
  llm: { ... }                           # engine parameters live under the engine's name
  guardrails: { input: [...], output: [...] }   # llm, loop
  hooks: [artifact://trace-hook@1]
  compensate: undo_score                 # off-spine agent run on rollback
  effect: write                          # code (and tool overrides): read | write | destructive
  routes: { strong: persist, weak: reject, error: END.failed }
```

| Field | Rule |
|---|---|
| `id`, `description`, `engine`, `routes` | Required |
| `inputs` / `outputs` | Typed (§3); may be `{}` |
| engine key | Required for every engine except `pending`; must match `engine` |
| `budget` | Required fields depend on the engine (§7, V011) |

### Per-signal outputs

When outcomes produce different data, key `outputs` by routed signal:

```yaml
outputs:
  valid:      { invoice: Invoice.create, verification: json }
  incomplete: { verification: json }
```

A field on several signals must have the same type. Data flow (V006/V007) uses the set for the signal
on each edge. `decide` agents can't use this form.

### Context isolation

1. An agent sees **only its declared inputs** (plus `ctx` for code agents). Undeclared keys are invisible.
2. Its result is validated against `outputs`; a missing, extra or mistyped key is `error`
   (`contract_violation`).
3. Only declared outputs merge into the run context. Two agents on one path writing the same key is a
   V009 warning.

### Signals

An agent emits exactly one signal per invocation:

- **Normal:** `default` for single-outcome agents; the enum values used for routing (`llm`,
  `decide`, `human.route_on`, loop outcome); explicit strings from `code` agents.
- **Reserved** (must be mapped where the engine can emit them):

| Engine | Reserved signals |
|---|---|
| all except `pending` | `error` (unmapped → V004 warning; the run fails) |
| `tool` | db `read`/`delete`: `not_found` · db `create`/`update`/`upsert`: `conflict` · http: `http_<status>` (only if mapped) |
| `llm` | `parse_error`, `timeout`, `budget_exceeded`, `guardrail_violation` (with guardrails) |
| `loop` | `max_iterations`, `budget_exceeded`, `timeout`, `aborted`, `guardrail_violation` (with guardrails) |
| `decide` | `low_confidence` (with a model) |
| `human` | `expired` (with `expires`) |

`error` carries an `error_type` in the journal: `contract_violation`, `no_rule_matched`,
`decision_model_unavailable`, `judge_unavailable`, `judge_uncertain`, `tool_args_invalid`,
`http_error`, `db_error`, `mcp_error`, `code_exception`, `compensation_failed`, `policy_violation`,
`lease_lost`, `approval_expired`.

---

## 6. Engines

| Engine | Implementation | Determinism | LLM cost |
|---|---|---|---|
| `pending` | none — a typed contract; runs as a typed stub in dev and tests | — | 0 |
| `code` | an `@agent` Python function | deterministic | 0 |
| `tool` | `db` / `http` / `mcp` operation | deterministic | 0 |
| `decide` | ordered rules, then an optional decision model | deterministic (rules only) / bounded | 0 → low |
| `llm` | one structured model call | bounded | $ |
| `loop` | a bounded tool-using model loop | bounded | $$ |
| `human` | a form for a person | external | 0 |

Only `code` agents need Python. Everything else is configured in YAML. Details and full examples:
[engines](engines.md), [decide](decide.md), [loop and tools](loop-and-tools.md).

```yaml
# code — agents/issue_refund.py holds @agent("issue_refund")
- id: issue_refund
  description: Calculate the refund amount and record it
  engine: code
  effect: write
  inputs:  { order: Order, reason: str }
  outputs: { refund: Refund }
  code: { run: issue_refund }
  routes: { default: reply, error: END.failed }

# tool — effect is derived
- id: load_order
  description: Load the order
  engine: tool
  inputs:  { order_id: uuid }
  outputs: { order: Order }
  tool: { use: db, model: Order, op: read, where: { id: "{{ order_id }}" } }
  routes: { default: triage, not_found: END.missing, error: END.failed }

# decide — rules first; a model only when no rule matches
- id: triage
  description: Decide how a refund is handled
  engine: decide
  inputs:  { order: Order, reason: str }
  outputs: { route: "enum[auto_refund, manager_review, reject]" }
  decide:
    rules:
      - { when: "order.total > 500", then: manager_review }
      - { when: "reason == 'fraud_flag'", then: reject }
      - { when: "true", then: auto_refund }
  routes: { auto_refund: issue_refund, manager_review: approval, reject: END.rejected, error: END.failed }

# llm — one structured call; inputs become the prompt's input block
- id: reply
  description: Write the customer confirmation
  engine: llm
  inputs:  { order: Order, refund: Refund }
  outputs: { text: str }
  budget:  { timeout: 20s, max_tokens: 1500 }
  llm: { model: default, system: artifact://support-voice@3, prompt: Confirm the refund briefly. }
  routes: { default: END, parse_error: END.failed, timeout: END.failed, budget_exceeded: END.failed, error: END.failed }

# human — the outputs are the form
- id: approval
  description: A manager approves a large refund
  engine: human
  inputs:  { order: Order }
  outputs: { approved: bool, note: "str?" }
  human: { title: "Refund {{ order.id }}", show: [order], required_group: support_manager, expires: 48h, route_on: approved }
  routes: { "true": issue_refund, "false": END.rejected, expired: END.failed, error: END.failed }
```

---

## 7. Determinism, budgets and policy

- A workflow's **profile** counts its agents per class ("11 agents · 8 deterministic · 2 bounded · 1
  external"); `tuvl validate` prints it with the **worst-case tokens per run** (the maximum over paths
  of summed `max_tokens`, including guardrail judges and child workflows).
- Required budgets (V011): `llm` → `max_tokens`, `timeout`; `loop` → `max_iterations`, `max_tokens`,
  `max_tool_calls`, `timeout`; `decide` with an LLM-backed model → `max_tokens`, `timeout`.

```yaml
policy:
  max_tokens_per_run: 5000          # V020 if the worst case exceeds it
  max_bounded_agents: 2
  require_human_before: [destructive]   # every path to a destructive effect passes a human or approval
  model_only_decisions: warn        # warn | error | allow (V013; allow = accepted on purpose, reviewed)
  max_depth: 2                      # workflow-as-tool nesting
  allow_secure_to_llm: false        # secure model fields never reach a model (V020)
```

---

## 8. Testing

```yaml
kind: Test
version: tuvl/v2
metadata: { name: small-damaged-refund-is-automatic }
spec:
  workflow: refund_flow
  given:                                   # rows that exist before the run (in-memory database)
    Order: [{ id: "00000000-0000-0000-0000-000000000040", total: 40, status: delivered }]
  as: { user: u-customer }                 # the caller: the run's principal (ctx.principal)
  input: { order_id: "00000000-0000-0000-0000-000000000040", reason: damaged, message: "Arrived broken" }
  mocks:                                   # model, external and human steps only — typed against the contract
    classify: { outputs: { category: damaged } }
  replay: tests/fixtures/refund_flow/small-refund.journal.json   # serve recorded model/tool results
  expect:
    end: default                           # or wait: human | approval
    path: [load_order, triage, issue_refund, reply]   # or path_includes: [...]
    decisions: { triage: { source: "rule#3" } }
    output: { status: 200, body: { amount: 40 } }   # partial match
    assertions: ["output.body.amount == 40"]
    judge:
      - { judge: artifact://refund-reply-quality@2, target: "{{ text }}" }
```

- `tuvl test` is **offline**: `tool: db` and `code` agents run for real against an in-memory database
  seeded by `given:`, with the `as:` caller as the principal; an unmocked `human` agent stops the run
  (assert with `expect.wait`); unmocked model/HTTP/MCP agents fail with a hint to mock or replay them
  (`--live` lets them call out).
- **Honest results:** a test whose path goes through an unmocked `pending` agent (a typed placeholder)
  is **stubbed**, not passed — it is reported, doesn't count towards task status, and fails
  `tuvl test --strict` (the CI gate).
- **Mock policy:** mock what is non-deterministic, external or human (`llm`, `loop`, `decide` with a
  model, `tool http`/`mcp`, `human`). Mocking business logic (`code`, `tool db`, `decide` rules) is
  warning **T001** — seed `given:` rows and set `as:` instead.
- `expect` order: structure (`end`/`wait`, `path`, `decisions`) → `output` → `assertions` → `judge`.
- Pin any run as a fixture (`tuvl runs pin <run_id>` or Insight 📌); `replay:` then serves its
  recorded engine results with zero tokens.
- Judges are cached and calibrated ([judges](judges.md)).

---

## 9. The development loop

```bash
tuvl spec new refund-automation            # write intent: specs/refund-automation.md
tuvl spec check                            # the grammar: fix every blocking finding
#   the coding agent writes specs/refund-automation.tasks.yaml (skill tuvl-plan)
tuvl spec status --json                    # derived task statuses — pick the next unblocked task
#   build it (YAML engine blocks; Python bodies only for code agents)
tuvl codegen                               # schemas, code-agent stubs, prompt placeholders, tests
tuvl spec verify                           # does the build match the spec, step by step?
tuvl spec check && tuvl validate --strict && tuvl codegen --check && tuvl lock --check && tuvl test --strict && tuvl spec verify --strict && tuvl spec status --strict
```

That last line is the **CI gate**. Task statuses are **derived** (`todo` → `in_progress` →
`implemented` → `tested` → `done`, with `blocked` and `stale` overlays) — never set them by hand.
See [spec-driven workflow](spec-driven.md).

---

## 10. Validation rules

Severity: **E** error, **W** warning (error under `--strict`). `tuvl validate --json` reports
`file:line`, code and message.

| Code | Sev | Rule |
|---|---|---|
| V001 | E | Unknown kind, engine, field or artifact type; missing `version: tuvl/v2` |
| V002 | E | Duplicate or malformed agent id |
| V003 | E | Route target is not an agent, `END` or a declared `END.<name>`; off-spine agent as a target |
| V004 | E | Emittable signal unmapped, or a route for a signal never emitted (W for unmapped `error`) |
| V005 | W | Unreachable agent; cycle without a bounded/human guard |
| V006 | E | An input isn't produced on every path to the agent |
| V007 | E | Producer output type incompatible with consumer input type |
| V008 | E | Template/TEL references an undeclared input or fails to type-check |
| V009 | W | Two agents on one path write the same key |
| V010 | W / E (ship) | `pending` agent present |
| V011 | E | Missing required budget fields |
| V012 | E | `decide`: enum ≠ normal route keys; rule `then` not in enum; model without `low_confidence` route |
| V013 | W | Model-only decision feeds a write/destructive effect without a human/approval/compensate |
| V014 | E | Loop tool source unresolved; MCP tool without `allow`; nesting too deep (W: MCP schema differs from lock) |
| V015 | W | Write/destructive tool without `compensate` when the run continues; declared effect weaker than derived |
| V016 | E | `human`: form ≠ outputs; bad `route_on`; `expires` without `expired` route |
| V017 | E | Artifact ref unresolved or wrong type (W: unpinned) |
| V018 | E | Model used by `tool: db`/`ctx.db` not in `spec.models`; pinned model version disabled |
| V019 | E | `public: true` with scope/group; invalid cron; MCP export without description |
| V020 | E | Policy violation (worst-case tokens, bounded cap, human-before-destructive, secure field to a model) |
| V021 | E | `transaction: per_run` with human/approval/async |
| V022 | E | End body references a key not produced on every path; undeclared end |
| V023 | W | Judge calibration below `min_agreement` (test time) |
| V024 | E (ship) | `tuvl.lock` missing or stale |
| V025 | E | `code` agent `run` not registered, or its signature doesn't match the generated schema |
| V026 | E | Decision provider not installed, or unpinned hosted decision model (W) |

`tuvl mcp`'s `explain_error` tool explains any code.

---

## 11. Rules for coding agents

1. **Never infer project state — ask tuvl** (`--json` output or `tuvl mcp` tools).
2. **Specs drive the work**; task status is derived, never set.
3. **Never hand-edit generated files**: `agents/_generated/`, tests with `metadata.generated_from`,
   `tuvl.lock`. Regenerate them.
4. **Only `code` agents need Python**, and only the function body; `tuvl codegen` owns the
   decorator, signature and docstring.
5. **Never invent fields, engines or kinds.**
6. **Every change ends green** on the CI gate (§9).
7. **PII fields are `secure: true`** and never reach a model; secrets live in `.env`.
