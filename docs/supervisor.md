# Agent Supervisor & Orchestrator — Internals

The agent orchestrator is the runtime substrate that makes live autonomous-mode
`Agent` runs observable and controllable. On top of it sits `spec.supervisor` — a
declarative, per-workflow circuit breaker that watches every autonomous agent
run in that workflow and can **pause**, **steer**, or **abort** it while it runs.

This document covers the internals: how a run becomes addressable, how the
supervisor watches it, and how a directive lands back inside the loop. For the
authoring contract (the YAML reference), see `tuvl-agentic-manual.md` §4.14.

The orchestrator is tagged **experimental**.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [The Run Registry](#3-the-run-registry)
4. [Trigger Path 1 — Deterministic Rules](#4-trigger-path-1--deterministic-rules)
5. [Trigger Path 2 — The LLM Judge](#5-trigger-path-2--the-llm-judge)
6. [The Three Actions](#6-the-three-actions)
7. [Fail-Open by Default, Fail-Closed by Opt-In](#7-fail-open-by-default-fail-closed-by-opt-in)
8. [Operator API](#8-operator-api)
9. [Multi-Worker: the Redis Mirror](#9-multi-worker-the-redis-mirror)
10. [Static Validation](#10-static-validation)
11. [Configuration Reference](#11-configuration-reference)
12. [Metrics](#12-metrics)
13. [Reading the Code](#13-reading-the-code)

---

## 1. Overview

An `Agent` step in `mode: autonomous` runs a bounded ReAct tool-loop: the model
picks among author-declared tools, observes results, and re-decides until it
emits one of a closed set of outcomes. The loop is already bounded by
`max_iterations` and `token_budget` — but those are static caps. The supervisor
adds a **live, zero-trust layer**: an out-of-band watcher that evaluates the run
*as it happens* and intervenes before a cap is hit.

Key properties:

- **A sibling of `steps:`, not a step.** `spec.supervisor` is one optional block
  per workflow. It never appears in `routes:` and consumes no iteration of the
  loop it watches.
- **Out-of-band.** For each autonomous agent run, the engine spawns a separate
  asyncio task (`Supervisor.watch`) that subscribes to the run's live progress
  fan-out. The agent loop never calls the supervisor; it only publishes events
  and consults a control channel.
- **Cooperative.** Directives land at safe boundaries — the top of a loop turn,
  or between tool calls for abort — never mid-LLM-call, never mid-tool. There is
  no torn state.
- **Additive and inert.** With no `supervisor:` block declared, the run handle
  is registered but passive; the control channel is never touched and the run
  behaves exactly as before.

---

## 2. Architecture

```
Autonomous Agent loop (agent_core)
   │ progress events                        ▲ directives, consulted
   │ (iteration / tool_call / outcome)      │ at the turn boundary
   ▼                                        │
RunHandle ──────────────────────────► AgentControl
   ├── record() → trace ring buffer         ▲ abort / pause / resume / steer
   │             (capped at 200 frames)     │
   ├── publish() → per-subscriber queues    │
   │             (capped at 256, drop-on-full, never block the loop)
   │      │
   │      ├────► Supervisor.watch task ─────┘   rules every turn,
   │      │                                     LLM judge every N iterations
   │      └────► streaming sink (SSE/gRPC progress frames)
   │
   └── snapshot() ──► Redis mirror  (hash tuvl:agent:runs)
                      control pub/sub (channel tuvl:agent:control)
                            ▲
                            │ cross-worker directives
        Operator API  /api/agents/*  (this worker, or via Redis)
```

The wiring lives in `tuvl.core.engine.runner` (`_run_autonomous_agent_step`).
For every autonomous-mode `Agent` execution the engine:

1. Mints a `RunHandle` (`run_id`, workflow, step id, tenant/user from the
   request's ContextVars, `token_budget`, and the author-declared tool names)
   and registers it in the per-process `REGISTRY`.
2. Mirrors the handle's snapshot to Redis, when available.
3. Spawns `Supervisor(spec.supervisor).watch(handle)` as a concurrent task —
   only when the workflow declares a supervisor.
4. Wraps the runner's progress sink so every event updates the handle
   (`iteration`, `tokens_used`), is appended to the retained trace, and is
   fanned out to all subscribers before reaching the streaming sink.
5. On completion (any exit), cancels the supervisor task, unregisters the
   handle, and removes the Redis mirror entry.

The agent loop itself (`agent_core.AgentStepRunner._run_autonomous`) checks
`AgentControl` at the top of every iteration: wait if paused, exit if aborted,
inject any queued steering messages — then proceeds with the LLM call.

---

## 3. The Run Registry

`tuvl.core.engine.orchestrator.registry` holds three pieces:

**`RunHandle`** — the live record of one in-flight run: identity
(`run_id`, `workflow`, `step_id`, `tenant_id`, `user_id`), live counters
(`iteration`, `tokens_used`, `token_budget`), the declared tool whitelist, a
capped trace (last 200 progress frames), and the control channel. `snapshot()`
returns the JSON-safe view served by the operator API.

**`AgentControl`** — the cooperative control channel. Single reader (the loop),
one or more writers (supervisor, operator, Redis subscriber). Pause is an
`asyncio.Event` the loop parks on; abort is a directive flag (`request_abort`
also sets the resume event so a paused loop is never left stuck); steer is a
queue of messages drained at the next turn.

**`RunRegistry` / `REGISTRY`** — a per-process dict of `run_id → RunHandle`,
one instance per uvicorn worker. `list()` filters by tenant.

Fan-out is deliberately lossy-but-safe: `publish()` uses `put_nowait` against
bounded per-subscriber queues (256 events). A slow or broken subscriber has its
events dropped — it can never stall or crash the agent loop.

---

## 4. Trigger Path 1 — Deterministic Rules

`rules:` are evaluated by the supervisor on **every** progress event — no LLM
call, no cost. Each rule is `{when: <kind>, ...params, then?: <action>}`; when
`then` is omitted, the block-level `on_violation` (default `pause`) applies.
Each rule fires **at most once** per run.

| `when` | Params | Fires when |
|---|---|---|
| `tool_repeated` | `tool?` (name), `count` (default 3) | a tool has been called `count` times — the named tool if `tool` is set, otherwise any single tool |
| `budget_fraction` | `gt` (default 0.8) | `tokens_used / token_budget` exceeds `gt`; can never fire without a declared `token_budget` |
| `iteration_reached` | `gte` (default 1) | the loop has reached iteration `gte` |

Tool counts are accumulated from `tool_call` progress events; budget and
iteration checks read the handle's live counters. The supervisor ignores its own
`supervisor`-type events, so an intervention can never re-trigger a rule.

---

## 5. Trigger Path 2 — The LLM Judge

Declaring `model` plus `criteria` — inline policy text, or an `artifact://`
reference to a `steering` artifact — enables the LLM path. It is **cost-gated**:
the judge is invoked only on `iteration` events, only when the iteration is a
multiple of `every_n_iterations` (default 1), and never twice for the same
iteration.

Mechanics (`Supervisor._judge`):

- `model` resolves exactly like an agent step's: a bare name (no `/`) is looked
  up as `llms/<name>.yaml` and its litellm model string is used; a string
  containing `/` passes through to litellm as-is.
- The verdict runs as a **subtask** with at most one in flight — a slow or hung
  judge never stalls the deterministic rules, and a still-running verdict simply
  skips the next cadence slot.
- Each call is bounded by `TUVL_AGENT_JUDGE_TIMEOUT_S` (default 30s); without
  it, litellm's own default (600s) would apply.
- The judge is the **shared judge core** (`tuvl.core.insight.judge`) also used
  by `tuvl test` and the dev `EvaluateTrace` route. It returns a structured
  `Scorecard {passed: bool, reason: str}` enforced via `response_format`.
- The supervisor uses `SUPERVISOR_SYSTEM_PROMPT`, which is deliberately
  conservative: *set `passed=false` only when the policy is being violated and
  the agent should be stopped or corrected now; prefer letting a healthy agent
  continue.*
- A `passed=false` verdict triggers the `on_violation` action with the verdict's
  `reason` — which also becomes the steer text when the action is `steer`.

The judge sees **loop metadata only**, never context values:
`{iteration, tokens_used, token_budget, tool_calls, undeclared_tool_calls}`.
Tool names in `tool_calls` are whitelisted against the author-declared tool set;
any call to an undeclared name is surfaced as an aggregate count only, so a
model-chosen string can never smuggle text into the judge prompt.

When `criteria` is an `artifact://` ref, it must resolve to a `steering`
artifact (site compatibility is enforced — the old `criteria` artifact type
folded into `steering`). The ref is resolved per judge pass through the artifact
registry, so a dev-mode edit to the `.md` artifact applies live; an unresolvable
ref logs `supervisor.criteria.unresolved` and disables that judge pass rather
than failing the run. The old `criteria_file` key was **removed** — `tuvl
validate` rejects it with a pointed error.

---

## 6. The Three Actions

All triggers — rules, judge, operator API, Redis directives — funnel into the
same `AgentControl`, and land cooperatively inside the loop:

**`pause`** clears the resume event. At the top of its next turn the loop parks
on `wait_if_paused()`. A paused run pins its request-scoped DB session and open
transaction, so the dwell is capped: after `TUVL_AGENT_PAUSE_MAX_S` (default
300s) without a resume, the pause **escalates to an abort** and the run exits
`aborted` with `_last_error` set to the deadline reason. Resume comes only from
the operator API (locally or via Redis) — the supervisor itself has no resume
action.

**`steer`** queues a message. At the next turn boundary the loop drains the
queue and injects each message as a `role: system` message, wrapped in a fixed
template that frames it as supervisor guidance:

```
[Supervisor steering] Adjust your approach per the guidance below. It does
not change your tools, your outcome contract, or the task itself.
<steering>...</steering>
```

Deterministic `steer` rules use the block's `steer_message` (a generic refocus
prompt by default); a judge-triggered steer uses the verdict's reason. The
template exists because steer text is free-form operator/judge input — it may
guide the agent, but must not pose as a new operating contract.

**`abort`** sets the directive flag. The loop exits at the next turn boundary
— and, within a multi-tool turn, **between tool calls**, so a turn that
dispatches several tools cannot outlive the directive. The run ends with the
reserved exit signal **`aborted`**, sets `_last_error` /
`_last_error_type: aborted`, and increments the `tuvl.agent.aborts` counter.

Because any run can be aborted (supervisor or operator), an autonomous `Agent`
step's `routes:` should map `aborted:`. `tuvl validate` warns when it is
unmapped; at runtime an unrouted `aborted` ends the workflow cleanly (with a
warning) rather than raising — an out-of-band stop is not an authoring bug.

Every intervention is observable: it increments
`tuvl.agent.supervisor_actions`, logs a structured `supervisor.action` event,
and is published to all subscribers as a `type: supervisor` frame carrying
`{action, source, reason, iteration}`. `source` is the rule's `when` kind,
`llm` for a judge verdict, or `judge_error` for the fail-closed path.

---

## 7. Fail-Open by Default, Fail-Closed by Opt-In

The supervision layer never becomes the reason a healthy run dies:

- No `supervisor:` block → no watcher task, no cost.
- No `token_budget` → `budget_fraction` can never fire.
- No `model` or no `criteria` (or an unresolvable `criteria` artifact ref) →
  the judge path is disabled.
- A judge error or timeout logs `supervisor.judge_failed`, increments
  `tuvl.agent.judge_failures`, and by default the run **continues**
  (`on_judge_error: ignore`).
- A broken progress subscriber only loses its own events; a Redis outage makes
  every mirror function a silent no-op.

The one opt-in inversion: `on_judge_error: pause | abort` makes LLM supervision
**fail-closed** — when the judge is unavailable, the run is paused (or aborted)
with the reason `judge unavailable (<ExcType>)` instead of proceeding
unsupervised. Use it when the judge *is* the safety control, e.g. an agent with
irreversible side-effect tools.

---

## 8. Operator API

`tuvl.core.api.orchestrator_router` mounts the human escape hatch under
`/api/agents`. The supervisor is the primary, declarative interface; this is the
out-of-band surface for an operator to inspect and break/steer a runaway.

| Method & path | Scope | Behaviour |
|---|---|---|
| `GET /api/agents/runs` | `agent:observe` | List in-flight runs — local registry plus other workers' snapshots via Redis |
| `GET /api/agents/runs/{id}` | `agent:observe` | One run's snapshot (local worker only) |
| `GET /api/agents/runs/{id}/trace` | `agent:observe` | The retained progress frames — iteration / tool_call / outcome / supervisor (local worker only) |
| `POST /api/agents/runs/{id}/abort` | `agent:control` | Request a cooperative abort |
| `POST /api/agents/runs/{id}/pause` | `agent:control` | Pause at the next turn boundary |
| `POST /api/agents/runs/{id}/resume` | `agent:control` | Clear a pause |
| `POST /api/agents/runs/{id}/steer` | `agent:control` | Inject a steering message |

Authorization is Biscuit-scoped: `agent:observe` to read, `agent:control` to
mutate; `iam:admin` bypasses both. The router binds the principal context so
every endpoint is **tenant-scoped**: a caller sees and controls only runs in its
own tenant, and a foreign-tenant or unknown `run_id` returns **404** — never a
403 that would leak existence.

The steer body is `{"message": "<non-empty string>"}`; a missing or empty
`message` fails validation (422), and a whitespace-only one is rejected with
400.

---

## 9. Multi-Worker: the Redis Mirror

The registry is per-process. Under `tuvl run --workers N`, an operator request
lands on one worker and would otherwise see only that worker's runs.
`tuvl.core.engine.orchestrator.redis_mirror` closes the gap when Redis is
configured — and is a **complete no-op without Redis** (single-process
`tuvl dev` and the test suite are unaffected):

- **Visibility.** Each worker mirrors run snapshots into a shared hash
  (`tuvl:agent:runs`) on register/unregister. `GET /runs` unions local handles
  with other workers' snapshots. Snapshot-detail and trace endpoints resolve
  local runs only.
- **Control.** A control action for a run not on this worker is published to a
  pub/sub channel (`tuvl:agent:control`); every worker runs a subscriber task
  (started in the app lifespan; it exits immediately when Redis is unavailable)
  that applies directives to its own local handles.
- **Idempotency.** Directives are tagged with a per-process `WORKER_ID`; a
  worker skips its own published directives (already applied locally).
- **Tenant re-check.** Authorization happens on the publishing worker, but the
  receiving worker re-checks the directive's tenant id against its local
  handle — a payload from any other Redis publisher cannot control a tenanted
  run.
- **Resilience.** The subscriber polls `get_message` under the shared client's
  socket timeout (a blocking `listen()` would die on a quiet channel) and
  re-subscribes with exponential backoff (1s → 30s cap) on any connection
  error, closing the stale pubsub each cycle so Redis restarts don't leak
  connections. Mirror write failures are logged and swallowed — they never
  affect the run.

---

## 10. Static Validation

`tuvl validate` checks the `supervisor:` block before anything runs
(`tuvl.cli.commands.validate`):

- `on_violation` and every rule's `then` must be one of `abort | pause | steer`;
  `on_judge_error` one of `ignore | pause | abort`.
- Every rule's `when` must be one of
  `tool_repeated | budget_fraction | iteration_reached`.
- `watches` entries must be `spine` or `agents`. Only `agents` (the default) has
  runtime effect today; `spine` validates but is currently inert.
- `every_n_iterations` must be an integer ≥ 1.
- `criteria_file` is rejected with a pointed error (removed — use `criteria`
  with inline text or an `artifact://` ref to a steering artifact).
- A `criteria` artifact ref must resolve to a known artifact of type `steering`
  (error otherwise); a floating (unpinned) ref is a warning.
- A bare `model` name (no `/`) is warned about when `llms/<name>.yaml` is not
  found.
- Separately, every autonomous `Agent` step with an unmapped `aborted` route
  gets a warning, since any run can be aborted.

---

## 11. Configuration Reference

### YAML keys (`spec.supervisor`)

| Key | Default | Meaning |
|---|---|---|
| `watches` | `["agents"]` | What to watch; only `agents` is active today |
| `rules` | `[]` | Deterministic rules, `{when, ...params, then?}` (see §4) |
| `on_violation` | `pause` | Action when a rule has no `then`, or the judge verdict fails |
| `model` | — | Judge model; an AgentModel name (`llms/<name>.yaml`) or a litellm string |
| `criteria` | — | Judge policy — inline text or `artifact://<steering artifact>` (resolved per judge pass) |
| `every_n_iterations` | `1` | Judge cadence (clamped to ≥ 1) |
| `on_judge_error` | `ignore` | `ignore` \| `pause` \| `abort` when the judge errors or times out |
| `steer_message` | generic refocus text | Steer text for deterministic `steer` rules |

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TUVL_AGENT_PAUSE_MAX_S` | `300.0` | Max pause dwell before escalating to abort (a paused run pins its DB session) |
| `TUVL_AGENT_JUDGE_TIMEOUT_S` | `30.0` | Timeout for one supervisor judge verdict |
| `TUVL_AGENT_TOOL_TIMEOUT_S` | `300.0` | Ceiling for a single agent tool call — a hung tool would otherwise make the run unkillable, since control lands only between calls |

---

## 12. Metrics

The orchestrator emits six OTel counters on the `tuvl.agent` meter —
`tuvl.agent.iterations`, `tuvl.agent.tool_calls`, `tuvl.agent.aborts`,
`tuvl.agent.budget_exceeded`, `tuvl.agent.supervisor_actions`, and
`tuvl.agent.judge_failures`. See `observability.md` §6 for the full table
(trigger conditions and attributes) and alerting guidance, and §2/§4 for the
structured log events and span hierarchy an agent run produces.

---

## 13. Reading the Code

| Module | Owns |
|---|---|
| `tuvl.core.engine.orchestrator.registry` | `RunHandle`, `AgentControl`, `RunRegistry`/`REGISTRY`, trace/queue caps |
| `tuvl.core.engine.orchestrator.supervisor` | `Supervisor.watch`, rule matching, the judge subtask, `_apply_action` |
| `tuvl.core.engine.orchestrator.redis_mirror` | Cross-worker snapshots, control pub/sub, subscriber restart loop |
| `tuvl.core.engine.orchestrator.metrics` | The six agent counters |
| `tuvl.core.engine.agent_core` | The unified agent runtime — the autonomous loop and its control checkpoint (pause/abort/steer landing sites) |
| `tuvl.core.engine.runner` | Handle registration, supervisor task lifecycle, progress fan-out wiring, `aborted` routing |
| `tuvl.core.api.orchestrator_router` | The operator API and its scope/tenant enforcement |
| `tuvl.core.insight.judge` | Shared judge core: `Scorecard`, `judge_async`, `SUPERVISOR_SYSTEM_PROMPT` |
| `tuvl.cli.commands.validate` | Static validation of the `supervisor:` block |
