# Spec-driven development

Intent lives in a spec. **The coding agent builds; tuvl referees.** Every tuvl step below is
deterministic and spends no tokens — only the agents you build (llm, loop, decision models) call
models at run time.

```
spec (.md) ──spec check──▶ checked spec ──coding agent (skills)──▶ task list + workflows, models, code, tests
   ▲                                                                        │
   │ spec diff / P005: stale tasks                                          ▼
   │                                              codegen: schemas, stubs, prompt placeholders, tests
   │                                                                        │
   └── derived task status ◀── spec verify · validate · test (honest) ◀─────┘
                                                                            │
                                                                     lock · ship
```

## 1. Write the spec

A spec is Markdown. Prose sections (`# Intent`, `# Policies`, anything else) are for people and the
coding agent. Three sections are **structured** and checked: `# Data`, `# Flows` and the
`tuvl-example` blocks under `# Acceptance`. `tuvl spec new refund-request` writes a template that
passes `tuvl spec check`.

~~~markdown
---
name: refund-request
owner: payments-team
---
# Intent
Customers request refunds. Small ones are paid automatically; large ones need a finance lead.

# Data
- **Order** (existing) — `id` (uuid), `customer_email` (string; secure), `total` (numeric)
- **Refund** (new) — `order_id` (uuid → Order; required), `amount` (numeric; required),
  `status` (enum: requested | approved | rejected | paid; required; default requested)

# Flows

## Flow: refund_request
Trigger: POST /refunds · signed-in
Input: `order_id` (uuid), `amount` (float), `reason` (str)
Ends: default 201 · unknown_order 404 · rejected 200 · failed 500

1. `load_order` — tool db read Order · in `order_id` · out `order` (Order)
   - not_found → END.unknown_order
2. `check_amount` — decide rules · in `amount` · out `lane` (enum[auto, review])
   - `amount <= 100` → auto
   - otherwise → review
   - auto → `record_refund`
   - review → `finance_review`
3. `finance_review` — human group finance_lead · expires 24h · in `order`, `amount` · out `approved` (bool)
   - true → `record_refund`
   - false, expired → END.rejected
4. `record_refund` — tool db create Refund · in `order_id`, `amount`, `reason` · out `refund` (Refund)
   - conflict → END.failed

# Acceptance
```tuvl-example
name: a small refund is recorded
given: { Order: [{ id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e10", total: 120 }] }
as: { user: u-customer }
input: { order_id: "6f1c0a52-2a8e-4d0b-9a77-0b9d7c2f1e10", amount: 40, reason: "Arrived broken." }
expect: { end: default }
```
~~~

### Grammar

| Element | Form |
|---|---|
| Model | `- **Name** (new\|existing) — fields`; a bullet may wrap onto indented lines |
| Field | `` `name` (type; flags) `` — types `string text integer numeric boolean uuid date timestamptz jsonb`, `enum: a \| b`, `uuid → Model`; flags `required unique secure`, `default <v>` |
| Flow | `## Flow: <workflow_name>` under `# Flows` |
| Trigger | `Trigger: POST /path · signed-in` — or `group <g>` / `public`; `schedule <cron>`; `tool` (only called as a loop's workflow tool) |
| Input / Ends | `` Input: `f` (type), … `` · `Ends: default 201 · <name> <status> · failed 500` (`default` and `failed` are required) |
| Step | ``N. `id` — <engine …> · in `a`, `b`? · out `x` (type) · budget 800 tok, 20s, retry 2 · effect write · expires 24h · guardrails `g@1` `` — a trailing `` `b`? `` marks an optional input; long steps continue on an indented `· …` line |
| Engines | `code` · `tool db read\|list\|create\|update\|upsert\|delete <Model>` · `tool http <METHOD> <name>` · `tool mcp <server>` · `llm <model>` · `loop <model>` (with `` tools: `t1`, workflow `w` (approval <group> when `expr`; max 1 call) ``) · `decide rules` · `decide model <ref> ≥ 0.8` · `decide rules + model <ref> ≥ 0.7` · `human group <g>` |
| Route | `` - signal[, signal] → `step` `` or `→ END` / `→ END.<end>`; `(out `f` (type))` names outputs a reserved signal carries |
| Rule | `` - `TEL condition` → value ``, last `- otherwise → value` |
| Waiver | `- <signal>: waived — <reason>` under a step; `- tests: waived — <reason>` for a step no offline test can run (e.g. it needs pgvector) — reported by `spec verify`, counted by task status; `   - <end>: no example — <reason>` (indented) under `Ends:` |
| Off-spine | `T1.` steps — a loop's tools (`tool of `loop``), compensators (a step's `` · compensate `undo_x` `` part: runs on a rollback end to undo it) and alternate paths, which route `default` explicitly |

Implicit: `default` goes to the next numbered step (or `END`), and `error` to `END.failed`.

## 2. Check it

`tuvl spec check [SPEC] [--strict] [--json]` (MCP `spec_check`, `GET /dev/specs/check`) — zero
tokens, deterministic. It reports every finding with its line; blocking findings fail.

| Rule | | Finding |
|---|---|---|
| S001 | ✗ | A line in `# Data` / `# Flows` doesn't parse — never skipped silently |
| S002 | ✗ | A route targets an end not in `Ends:`, an unknown step or a loop tool |
| S003 | ✗ | A listed end is unreachable from the trigger |
| S004 | ✗ | An engine-implied signal is neither routed nor waived (db read/delete → `not_found`, create/update/upsert → `conflict`, llm → `parse_error timeout budget_exceeded`, loop → `max_iterations budget_exceeded timeout aborted`, `decide model` → `low_confidence`, `human … expires` → `expired`, guardrails → `guardrail_violation`, each enum value of a decide/human output) — or a routed signal the engine never emits |
| S005 | ✗ | A model step lacks its safeguards: `decide model` without a ref, threshold or time budget; `decide` with neither rules nor a model, or rules without `otherwise` and no model; `llm`/`loop` without a model or a token + time budget |
| S006 | ✗ | A step reads a field not produced on every path to it (mark it optional with `?` if that's intended) |
| S007 | ✗ | A model or field isn't declared in `# Data`, an `(existing)` model or field isn't in `models/`, an unknown column type, or an enum default outside its values |
| S008 | ✗ | A `secure` field (or a model holding one) is an input of a model-backed step |
| S009 | ✗ | A step output reuses a name the trigger input or an earlier step on its path already holds |
| S010 | ✗ | An HTTP trigger declares no access (`signed-in`, `public` or `group <g>`) |
| S011 | ! | An end has no `tuvl-example` and no `no example` waiver |
| S012 | ✗ | An example names an unknown flow, step, end or model, or a mock sets outputs the step doesn't declare for that signal |
| S013 | ! | A `human` step has no group or no expiry |
| S014 | ! | A free-text acceptance bullet — listed, not checked |
| S015 | ✗ | A `decide` rule doesn't compile or type-check against the step's inputs, reads a non-input, or yields a value outside the output enum |
| S016 | ✗ | A loop tool isn't a `T` step of the flow (or a `## Flow:`/project workflow), an approval condition doesn't compile, or an off-spine step is never used |
| T001 | ! | An example mocks deterministic logic (`code`, `tool db`, `decide` rules) — seed `given:` and `as:` instead |

Prose sections are listed as *unchecked*. A waived signal or end is reported (·) so a reviewer sees it.

## 3. Verify the implementation

`tuvl spec verify [SPEC] [--strict] [--no-tests] [--json]` (MCP `spec_verify`,
`GET /dev/specs/verify`) — zero tokens. It compares each `## Flow:` with its workflow, each `# Data`
model with its `ModelDefinition`, and runs the tests (offline) to see which steps they exercise.
**Errors** are contradictions; **warnings** are unfinished work (a missing workflow, a `pending`
agent, an untested step), which `--strict` — the CI gate — also fails.

| Rule | Checks |
|---|---|
| R001 | The flow has a workflow of that name; trigger method, path, schedule, access (`public` ↔ `trigger.http.public`, `group <g>` ↔ `metadata.required_scope`) and input names and types match |
| R002 | Each step is an agent with the same id and engine family (`tool db read Order` ↔ `use: db, op: read, model: Order`; `decide rules + model` ↔ both); `pending` is reported as not implemented; agents the spec doesn't name are reported |
| R003 | Inputs and outputs match the step's contract |
| R004 | Every spec route (including the implicit `default` and waivers) is in the workflow with the same target; a workflow route to an end the spec doesn't have is reported |
| R005 | Ends and their HTTP statuses match the workflow's `outputs` |
| R006 | Data fields exist in the `ModelDefinition` with a compatible type, the same enum values and the same `required`/`unique`/`secure`/`default` |
| R007 | Model steps use the spec's model, threshold and budget (tokens, time, iterations, tool calls); decide rules match in order; loop tools, approvals and `max_calls` match |
| R008 | Human steps use the spec's group and expiry |
| R009 | Each `tuvl-example` has its generated test, and it **passed** (stubbed doesn't count) |
| R010 | Each step is run by a passing test; deterministic steps must run for real (mocked-only is reported), model, external and human steps count through their mocks |

```text
specs/triage-ticket.md
  triage_ticket
    classify               llm default                  :23   ✓ mocked in 3
    prioritise             code                         :25   ✓ 3 passed
    review                 human group support_leads    :28   ✓ mocked in 1
    store                  tool db create Ticket        :32   ✓ 1 passed
  0 mismatch(es) · steps implemented 4/4 · exercised by real tests 2/4
```

## 4. The task list

The **coding agent writes the task list** (skill `tuvl-plan`) from the checked spec — tuvl doesn't
plan with a model. tuvl checks it as part of `tuvl spec check` and derives each task's status.

```yaml
kind: TaskPlan
version: tuvl/v2
metadata: { name: refund-request, spec: specs/refund-request.md, spec_hash: 9f2c… }
spec:
  tasks:
    - id: T3
      title: Route refunds by amount (decide rules)
      type: agent                 # model | workflow | agent | artifact | integration | test | policy | manual
      spec_ref: { anchor: flow-refund-request, hash: 41ad… }
      target: { workflow: refund_request, agent: check_amount }
      depends_on: [T2]
    - id: T7
      title: "Example: a small refund is paid automatically"
      type: test
      spec_ref: { anchor: acceptance, hash: 7be0… }
      target: { workflow: refund_request, example: a small refund is paid automatically }
    - id: T9
      title: Payments API credentials
      type: manual                # only manual tasks set a status
      target: { workflow: refund_request, agent: pay_refund }
      status: todo
```

One task per new model, per step, per example, and a `manual` task per external integration
(`tool http`/`mcp`); dependencies follow the flow. No estimates. After a spec change the agent adds
tasks for new steps and marks removed ones `obsolete: true` (never deletes them).

| Rule | | Finding |
|---|---|---|
| P001 | ✗ | The file isn't a valid `TaskPlan`, or a non-manual task has no target |
| P002 | ✗ | A task targets something the spec doesn't declare (model, flow, step or example) |
| P003 | ! | Coverage: a new model, step, example or external integration has no task (or there is no task list yet) |
| P004 | ✗ | A task carries an estimate (`effort`) or a hand-set status (only `manual` tasks may) |
| P005 | ! | A task's spec section changed since it was written — the agent should update it |

### Derived status

| Status | Rule |
|---|---|
| `todo` | Target agent is `pending`, the target file is absent, or an example has no generated test |
| `in_progress` | Target exists but validate reports errors on it, or it contradicts the spec (`spec verify`) |
| `implemented` | Target validates cleanly and matches the spec |
| `tested` | A passing `Test` covers it (stubbed results don't count), and its acceptance examples pass |
| `done` | Nothing blocks `ship`: lock current, no warnings on the target |
| `blocked` (overlay) | The spec has blocking `spec check` findings, or a `depends_on` task isn't `done` |
| `stale` (overlay) | The spec section changed since the task was written |

Only `type: manual` tasks take a manual status. Models and artifacts skip `tested`. A task tuvl can't
measure is `untracked`.

```bash
tuvl spec status --json     # derived statuses (runs the tests offline; --no-tests caps at implemented)
tuvl spec status --strict   # exit 1 unless every live task is done and none is blocked or stale
tuvl spec diff              # stale tasks and spec sections no task covers
```

## 5. Codegen

`tuvl codegen [<workflow>] [--check]` — deterministic, no model:

| Output | Rule |
|---|---|
| `agents/_generated/schemas.py` | `<Id>In`/`<Id>Out` models per agent (per-signal variants, `<Id>Signal`), `<Workflow>Input`, `<Workflow>End`. Never hand-edited |
| `agents/<run>.py` | A stub per `code` agent when missing; otherwise a **libcst merge** that regenerates decorator, signature and docstring and keeps parameter names and the **body** |
| `artifacts/<name>.md` | A prompt placeholder for a missing `artifact://` referenced by an `llm`/`loop` agent |
| `decide` agents | A block with neither rules nor model gets one never-matching rule per enum value |
| `tests/<workflow>/<example>.yaml` | `Test` documents from the spec's `tuvl-example` blocks; hand-written tests are never touched |

`--check` exits 1 if anything would change (the CI drift gate). **tuvl never writes function bodies.**

## 6. Lock

`tuvl lock` writes `tuvl.lock`: the tuvl version, each AgentModel's model id, each `artifact://`
ref's version and sha256, judge and decision-model versions, and each allowed MCP tool's input-schema
hash (listed live; `--no-mcp` compares by name only). `tuvl lock --check` verifies it; `tuvl ship`
requires it (V024) and pins the locked tuvl version in the image.

## 7. The CI gate

```bash
tuvl spec check && tuvl validate --strict && tuvl codegen --check && tuvl lock --check && tuvl test --strict && tuvl spec verify --strict && tuvl spec status --strict
```

`tuvl init <name> --sample` creates a project that passes this gate as created.

## Coding agents

- `tuvl mcp [-d <project>] [--allow-write]` serves the same operations over MCP (stdio): `validate`,
  `spec_check`, `spec_verify`, `spec_status`, `spec_diff`, `get_contract`, `list_agents`,
  `explain_error`, `get_run`, `get_run_events`, `codegen`, `test`; with `--allow-write` also
  `codegen_write`, `run_workflow`, `pin_fixture`. It refuses `TUVL_ENV=production`.
- `.agents/skills/` (refreshed by `tuvl skills update [--diff]`, mirrored to `.claude/skills/`):
  `tuvl-spec`, `tuvl-plan`, `tuvl-task-loop`, `agent-code`, `agent-tool`, `agent-llm`, `agent-loop`,
  `agent-decide`, `agent-human`, `tuvl-data`, `tuvl-test`, `tuvl-harden`, `tuvl-ship`, plus a root
  `AGENTS.md` with the non-negotiable rules.
- Every read command has `--json` with a documented schema (`/dev/schema/cli` in dev mode).
