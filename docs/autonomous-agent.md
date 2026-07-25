# Agent (`mode: autonomous`) — Internals of the Bounded Tool-Loop

`kind: Agent` with `mode: autonomous` is tuvl's bounded ReAct step: an LLM that chooses among author-declared tools, observes results, and re-decides — inside a loop the workflow contract caps on every axis. This document explains how the mode works internally, end to end. For the authoring reference (the YAML contract, field by field), see `tuvl-agentic-manual.md` §4.4 and §4.13.

Both agent modes share **one runtime**: `src/tuvl/core/engine/agent_core.py` (`AgentStepRunner`) — one runner class, two execution strategies, selected by the required step-level `mode:` discriminator. Message assembly, call-parameter resolution (`resolve_call_params`), retry, guardrail gating, and the `outcome` routing contract (`OutcomeContract`) are shared; this document covers the autonomous strategy.

---

## Table of Contents

1. [Mental Model](#1-mental-model)
2. [The Turn Lifecycle](#2-the-turn-lifecycle)
3. [How Tools Work](#3-how-tools-work)
4. [Steering and Skills](#4-steering-and-skills)
5. [Exits and Routing](#5-exits-and-routing)
6. [Guardrail Gates](#6-guardrail-gates)
7. [Hook Events](#7-hook-events)
8. [Live Progress Streaming](#8-live-progress-streaming)
9. [Supervision Hooks](#9-supervision-hooks)
10. [Validation](#10-validation)
11. [Observability](#11-observability)
12. [Reading the Code](#12-reading-the-code)

---

## 1. Mental Model

An autonomous-mode `Agent` step is autonomous only *inside* a closed contract. The model decides *which tool to call, with what arguments, and when to stop* — nothing else. Everything around that decision loop is declared in YAML and enforced by the engine:

| Axis | Bound | Enforced by |
|---|---|---|
| Actions | `agent.tools[]` — a closed set of refs to other steps in the same workflow (REQUIRED in this mode) | `build_tool_specs` + the dispatch callback |
| Exits | `agent.outcome.enum` — a closed set, plus five reserved abnormal exits | `OutcomeContract.finalize_autonomous` + `routes:` validation |
| Iterations | `agent.max_iterations` (default 8) | the loop itself |
| Tokens | `agent.token_budget` (optional, cumulative) | checked at the top of every turn |
| Wall clock per tool | `TUVL_AGENT_TOOL_TIMEOUT_S` (default 300s) | `asyncio.wait_for` around each dispatch |
| Content | `agent.guardrails` — `input` / `output` / `tools` gates (§6) | `run_guardrails` inside the shared core |
| Context writes | only `outcome.write`; tools need `writes_context: true` to write | `_dispatch_tool` merge policy |

Compare `mode: completion` (a single retried call, no tools): the difference is the loop, and the loop is why every bound above exists. The step never mutates shared context mid-run except through explicitly opted-in tools; its one guaranteed write is the final `outcome.write` key (default `<step_id>_result`).

Note: the persistent instruction field is `steering` — inline text or an `artifact://` reference. `system` / `prompt` are completion-mode fields and are rejected by `tuvl validate` on an autonomous step; `mode:` itself is required, with no default.

---

## 2. The Turn Lifecycle

The loop lives in `src/tuvl/core/engine/agent_core.py` (`AgentStepRunner._run_autonomous`). Each pass through the `for iteration in range(1, max_iterations + 1)` loop is one *turn*:

```
                ┌─────────────────────────────────────────────┐
                │ control checkpoint (pause / abort / steer)  │  ← cooperative,
                ├─────────────────────────────────────────────┤    turn boundary only
                │ token budget check (tokens_used >= budget?) │──── budget_exceeded ──▶ exit
                ├─────────────────────────────────────────────┤
                │ LLM call (transport-retried per agent.retry)│──── LLM exception ───▶ error exit
                ├─────────────────────────────────────────────┤
        ┌──────▶│ tool_calls in the response?                 │
        │       └──────────────┬───────────────┬──────────────┘
        │             yes      │               │ no
        │                      ▼               ▼
        │       ┌────────────────────────┐   ┌────────────────────────────┐
        │       │ dispatch each tool     │   │ output guardrail gate      │
        │       │ SEQUENTIALLY, in order │   │ finalize_autonomous:       │
        │       │ (tools gate on each    │   │ parse {"outcome","result"} │
        │       │  observation; abort    │   │ write outcome.write        │
        │       │  checked between calls;│   │ return outcome signal      │
        │       │  timeout fatal)        │   └────────────────────────────┘
        │       └───────────┬────────────┘
        │                   │
        └── next iteration ◀┘        loop exhausted ──▶ max_iterations exit
```

Details that matter:

- **Message assembly** (`_assemble_autonomous_messages`): the system prompt is built from the steering block (inline text or a resolved `steering` artifact, always injected), the skills block (each entry inline or a resolved `skill` artifact, injected as "apply when relevant"), a fixed autonomous-agent instruction, and — when `outcome.enum` is declared — a strict final-answer format: `{"outcome": <enum value>, "result": <payload>}`, no markdown fences. The first user message is the *public* context (keys not starting with `_`) serialized as a JSON block, or `"Begin."` when the context is empty; `context_injection` keys are appended as a delimited untrusted-content message.
- **Input gate**: before anything is sent to the model, the assembled user text passes the `input` guardrail gate (§6).
- **Transport retry** (`_acompletion_with_transport_retry`): each LLM turn is retried on transport failures per `agent.retry` — only `timeout` / `error` in `retry.on` trigger a retried call (with exponential backoff); parse-level issues are handled by the loop itself. Exhausted attempts raise the last exception, which becomes the `error` exit.
- **Token accounting**: `usage.total_tokens` from each response is accumulated into `tokens_used`. The budget check runs at the *top* of every turn, so it also bounds the final no-tool answer turn, not only tool-calling turns.
- **Tool calls are sequential, not concurrent.** All tools in a turn share the request's single SQLAlchemy `AsyncSession` (through a shallow context copy); concurrent statements on one `AsyncSession` raise `IllegalStateChangeError` and can poison the request-final commit. Sequential dispatch also keeps each `role: "tool"` message aligned with its `tool_call_id`, and lets an abort directive land between calls so a multi-tool turn cannot outlive it.
- **Tool failure is not loop failure.** A tool that raises is captured as `{"error": ...}` with signal `error` and fed back to the model, which may retry or route around it. Two fatal cases: `ToolTimeoutError` (the cancelled coroutine may have interrupted a statement on the shared session, so the run ends on the `error` exit rather than continuing on a possibly-poisoned connection) and a `GuardrailViolationError` from the `tools` gate (routed as `guardrail_violation`).
- **Tool results are truncated** to 8000 characters (`_MAX_TOOL_RESULT_CHARS`), then passed through the `tools` guardrail gate, before being appended as the tool message.
- **Termination**: a response with no tool calls means the agent is done — the content passes the `output` guardrail gate, then `OutcomeContract.finalize_autonomous` strips code fences, parses the JSON, writes the `result` payload (or the whole parsed object, or the raw text) to `outcome.write` (default `<stepId>_result`), and returns the outcome signal. Exhausting the loop returns `max_iterations`.

---

## 3. How Tools Work

A tool is another declared step in the same workflow. There is no separate tool registry: `agent.tools[].ref` names a step `id`, and the engine reuses the existing step runners to execute it.

### 3.1 Schema derivation

`src/tuvl/core/engine/agent_tools.py` (`build_tool_specs`) converts each tool entry into an OpenAI/Anthropic function-calling spec, forwarded by LiteLLM as `tools=`:

- **name** — the `ref` itself.
- **description** — sourced from the referenced step's top-level `description:` when set, otherwise the tool entry's `description:`, otherwise the generic fallback `"Invoke the '<ref>' component."`. Validation errors when neither is set (§10).
- **parameters** — the tool entry's `parameters` (a JSON Schema object) when declared. When omitted, `_derive_parameters` builds a best-effort schema offline from what the component already declares: `MCP` → the keys of its `mcp.arguments` map (as strings); `APICall` → the `{{ var }}` placeholders found in its `http` block; `ModelOp` → a free-form `payload` object. Anything else falls back to a permissive empty object. Declare explicit `parameters` for a precise contract; derivation is the convenience path.

### 3.2 Dispatch

`_run_autonomous_agent_step` in `src/tuvl/core/engine/runner.py` resolves every `ref` against the workflow's step index up front (an unknown ref raises before the loop starts), then wires a dispatch callback into the runner. When the model calls a tool, `_dispatch_tool`:

1. Makes a **shallow copy** of the live context and merges the LLM-generated arguments into it, so the component's `{{ }}` templates resolve from them. The copy shares the request's `_session` / `_db` handles — ModelOp IAM scope checks, masking, and OTel spans all apply exactly as on the spine.
2. Dispatches by the component's `kind` through the matching runner: `APICall`, `MCP`, `ModelOp`, `Router`, or `Functional` (the default). Any other kind returns an error result.
3. Ignores the tool step's own `routes:` — the returned signal goes back to the agent (and onto the `tool_call` span/progress event), it never routes the workflow.
4. Computes the **delta**: public context keys the tool changed. The delta is what the model sees as the tool result (falling back to the component's declared output key when there is no delta). Only when the tool entry set `writes_context: true` (default `false`) is the delta merged back into the real workflow context.

So the context policy is: the agent reads the full public context once (turn 1), tool results flow back to the *model* by default, and shared context changes only through `writes_context: true` tools and the final `outcome.write` write.

### 3.3 Timeout

Every dispatch runs under `asyncio.wait_for` with `TUVL_AGENT_TOOL_TIMEOUT_S` (default 300s, `settings.tuvl_agent_tool_timeout_s`). Without it a hung tool would make the run unkillable, because control directives land only between calls. A timeout is fatal for the run (§2).

---

## 4. Steering and Skills

Two instruction channels, one injection point (`_assemble_autonomous_messages`):

| Channel | YAML key | Injection | Framing in the system prompt |
|---|---|---|---|
| Steering | `agent.steering` | always | "persistent operating context — always follow it" |
| Skills | `agent.skills[]` | always present, but framed as conditional | "apply the following skills/instructions when relevant" |

Steering is the contract; skills are capabilities the model applies when the task calls for them.

Both fields take **inline text or `artifact://` references** — resolved through the artifact registry (`resolve_prose`) with site type-compatibility enforced: `steering` accepts `steering` and `prompt` artifacts; each `skills[]` entry accepts `skill` artifacts. The old per-agent directory scheme (`agent.steering_files`, `agents/<workflow>__<stepId>/{steering,skills}/`, the `.agents/skills/` library) is **gone** — artifacts are the single mechanism, shared and versioned project-wide (see `tuvl-agentic-manual.md` §2.11). An unresolvable ref fails startup in production and produces a precise runtime error in dev; in dev mode, edits to a file-source `.md` artifact apply on the next run without a restart (mtime refresh).

Supervisor steer messages (§9) arrive through a different channel — appended system messages at the turn boundary — and are explicitly templated as guidance that "does not change your tools, your outcome contract, or the task itself."

---

## 5. Exits and Routing

Every run ends on exactly one signal, drawn from a closed set:

| Signal | Source | Meaning |
|---|---|---|
| one of `outcome.enum` | `finalize_autonomous` | normal completion; the model chose this exit |
| `default` | `finalize_autonomous` | normal completion when no `outcome.enum` is declared |
| `max_iterations` | loop exhaustion | the cap was reached without a final answer |
| `budget_exceeded` | top-of-turn check | cumulative `total_tokens` reached `token_budget` |
| `error` | several paths | LLM call failed, a tool timed out, or the model returned an outcome not in the enum |
| `aborted` | control channel | a supervisor rule/judge or the operator API aborted the run, or a pause outlived `TUVL_AGENT_PAUSE_MAX_S` |
| `guardrail_violation` | any guardrail gate | a declared check failed on input, a tool observation, or the final output (§6) |

`max_iterations`, `budget_exceeded`, `error`, `aborted`, and `guardrail_violation` are the reserved exits (`RESERVED_EXITS` in `src/tuvl/core/engine/agent_core.py`); they are emitted by the engine, so do not reuse them as `outcome.enum` values. Every abnormal exit also writes `_last_error` and `_last_error_type` into context so the fallback step can report what happened.

Routing follows the engine's normal `_advance` rules, which for this step mean:

- Every `outcome.enum` value **must** be mapped in `routes:` — `tuvl validate` errors otherwise, and an unmapped non-default signal raises `RuntimeError` at runtime.
- An unmapped `error` or `aborted` stops the workflow cleanly with a warning (an abort is an out-of-band stop, not an authoring bug). Map them anyway when you want a routed fallback.
- An unmapped `max_iterations`, `budget_exceeded`, or `guardrail_violation` **raises** at runtime; validation warns when they are missing.
- A model answer whose `outcome` is not in the enum routes through `error`, never through an invented signal — the exit set stays closed even against a misbehaving model.

```yaml
routes:
  resolved:            format_reply
  escalate:            notify_manager
  max_iterations:      fallback_summary
  budget_exceeded:     fallback_summary
  error:               alert_ops
  aborted:             alert_ops
  guardrail_violation: alert_ops
```

---

## 6. Guardrail Gates

Guardrails (`type: guardrail` artifacts — see `tuvl-agentic-manual.md` §4.15) attach to the step as `agent.guardrails: {input: […], output: […], tools: […]}`. The gates run **inside the shared agent core** (`AgentStepRunner._gate` → `run_guardrails`), not in the engine's step dispatch — so every execution surface (engine, streaming, Spectrum, test runner) is correct by construction:

- **`input`** — on the assembled user text, before anything is sent to the model.
- **`tools`** — on every serialized tool observation (after the 8000-char truncation), before it reaches the model. Autonomous-mode only.
- **`output`** — on the model's final no-tool content, before `finalize_autonomous` merges anything into context.

A failing check raises `GuardrailViolationError` inside the core and exits the run on the reserved `guardrail_violation` signal, with `_last_error` naming the artifact and check. `pii_mask` is the exception — a transform that masks declared `secure` fields and never fails; the (possibly transformed) content continues through the gate. `llm_judge` is the only check that costs an LLM call and is fail-open on judge outage unless `on_judge_error: violation`.

---

## 7. Hook Events

Observe-only hooks (`type: hook` artifacts — see `tuvl-agentic-manual.md` §4.16) fire around the loop when attached per step (`hooks: […]`) or workflow-wide (`spec.hooks:`):

- `before_step` / `after_step` / `on_error` fire in the engine's single per-kind dispatch (`WorkflowEngine._run_kind`), like every other kind.
- `before_tool` / `after_tool` fire inside `_invoke_tool`, around each tool dispatch, with `{tool}` / `{tool, signal}` detail.

Hooks never mutate context and never affect flow control; a failing hook is logged and swallowed. `action: metric` increments the `tuvl.hook.events` counter; `action: notify` runs a declared `APICall` step on a private payload copy (`WorkflowEngine._notify_hook`).

---

## 8. Live Progress Streaming

On the streaming execution path, `_stream_autonomous_agent` in `src/tuvl/core/engine/runner.py` runs the step as a background task and bridges its progress events through an `asyncio.Queue`. The runner emits three event types via its progress sink:

| Event | Payload |
|---|---|
| `iteration` | `iteration`, `tool_calls` (count requested this turn), `tokens_used` |
| `tool_call` | `iteration`, `tool` (name), `signal` |
| `outcome` | `iteration`, `signal` |

Each one is wrapped in a `StepEvent` with `signal="running"` and the payload under `snapshot.agent_progress`, then serialized to the wire by `src/tuvl/core/engine/streaming.py` (SSE, gRPC, and the CLI stream watcher share the same serializers). The snapshot carries **loop metadata only** — no context values, no tool arguments, no results — so it adds no PII surface beyond the masked final frame.

Two hardening properties: a broken sink never breaks the loop (emit failures are swallowed), and a client disconnect (`GeneratorExit` on the generator) cancels the background agent task instead of leaving it running against a tearing-down session and burning tokens.

On non-streaming paths no sink is wired and the loop runs identically without emitting. Everything else about step execution — including hooks — funnels through the one per-kind dispatch (`WorkflowEngine._run_kind`) shared by engine run/streaming, Spectrum, and the test runner; there are no per-surface dispatch forks to keep in sync.

---

## 9. Supervision Hooks

Every autonomous-mode `Agent` run registers a `RunHandle` in the orchestrator registry (mirrored to Redis when available, so other workers can see and address it), and — when the workflow declares `spec.supervisor` — spawns a `Supervisor` watcher task for the run's lifetime. With no supervisor and no operator, the control channel is never touched and costs nothing.

Control is **cooperative**, applied through an `AgentControl` checkpoint at the top of every turn — never mid-LLM-call, never mid-tool:

- **pause** — the loop parks in `wait_if_paused`. A paused run pins its request-scoped DB session/transaction, so the dwell is capped by `TUVL_AGENT_PAUSE_MAX_S` (default 300s) and escalates to an abort at the deadline.
- **steer** — queued messages are drained and appended as templated system messages before the next LLM call. Steer text is free-form operator/judge input; the template frames it as guidance, not a new operating contract.
- **abort** — the run returns on the `aborted` exit. The abort directive is also checked between tool calls within a turn, so a multi-tool turn cannot outlive it.

The supervisor itself — deterministic rules, the LLM judge, cadence, fail-open behavior, and the `/api/agents/*` operator API — is documented in `docs/supervisor.md`.

---

## 10. Validation

`tuvl validate` (`src/tuvl/cli/commands/validate.py`) checks the step's contract statically:

| Check | Severity |
|---|---|
| `mode` missing or not `completion` / `autonomous` | error |
| `agent.model` missing | error |
| `agent.model` names an `llms/<name>.yaml` that does not exist | warning |
| removed contracts present (`agent.output`, `agent.steering_files`, `outcome.output_key`, `outcome.signal_from`) | error (pointed message naming the replacement) |
| completion-only fields (`system` / `prompt`) on an autonomous step | error |
| `agent.steering` missing | warning |
| no `tools` declared (`mode: autonomous` requires them) | error |
| tool entry missing `ref`, `ref` not a declared step, or `ref` is the agent itself | error |
| tool has no description (neither on the referenced step nor on the tool entry) | error |
| an `outcome.enum` value unmapped in `routes:` | error |
| `steering` / `skills` / `guardrails` artifact ref unknown, wrong type for the site, or pinned to a missing version | error |
| a floating (unpinned) artifact ref | warning (`tuvl ship --strict` fails on it) |
| guardrails gate not `input` / `output` / `tools`, or a non-artifact entry | error |
| `tools` guardrail gate on a completion step | error |
| guardrails attached but `guardrail_violation` unmapped in `routes:` | warning (raises at runtime if hit) |
| `max_iterations` / `budget_exceeded` unmapped in `routes:` | warning (raises at runtime if hit) |
| `aborted` unmapped in `routes:` | warning (an abort stops the workflow without a routed exit) |

---

## 11. Observability

The loop is instrumented at both granularities; `docs/observability.md` is the full reference. The step's own span is `node.Agent` — autonomous agents appear under the same span name as completion agents, distinguished by their child spans.

- **Spans** — `agent.iteration` (attributes `tuvl.agent.step_id`, `tuvl.agent.iteration`, `tuvl.agent.tokens_used`, `tuvl.agent.tool_calls`) wraps each turn including its tool dispatches; `agent.tool_call` (`tuvl.agent.tool`, `tuvl.agent.tool_signal`) nests inside it, and the dispatched step's own spans (LiteLLM `gen_ai.*`, HTTP, DB) nest inside that.
- **Counters** — meter `tuvl.agent`: `tuvl.agent.iterations`, `tuvl.agent.tool_calls`, `tuvl.agent.aborts`, `tuvl.agent.budget_exceeded` (plus the supervisor counters) — unchanged names. Hooks add `tuvl.hook.events`. See `docs/observability.md` §6 for attributes and export behavior.
- **Structured logs** — `agent.turn`, `agent.tool_call`, and the exit events (`agent.max_iterations`, `agent.budget_exceeded`, `agent.llm_error`, `agent.tool_timeout`, `agent.aborted`, `agent.invalid_outcome`, `agent.guardrail_violation`), all carrying `step_id` and iteration fields, correlated to spans via `trace_id`/`span_id`.

---

## 12. Reading the Code

| Module | What lives there |
|---|---|
| `src/tuvl/core/engine/agent_core.py` | `AgentStepRunner` — both strategies: message assembly, budget check, control checkpoint, transport retry, tool invocation, guardrail gates, `OutcomeContract`, `RESERVED_EXITS` |
| `src/tuvl/core/engine/agent_tools.py` | `build_tool_specs` / `_derive_parameters` — declared tools → provider function-calling schemas |
| `src/tuvl/core/engine/guardrails.py` | `run_guardrails`, the closed check set, `GuardrailViolationError` |
| `src/tuvl/core/engine/hooks.py` | `emit_hooks`, the closed event/action sets, `tuvl.hook.events` |
| `src/tuvl/core/engine/runner.py` | `_run_kind` (the single per-kind dispatch), `_run_agent_step` (mode gate), `_run_autonomous_agent_step` (setup, run registration, supervisor spawn), `_dispatch_tool` (kind dispatch, context-delta policy), `_stream_autonomous_agent` (progress bridging), `_notify_hook`, `_advance` (signal routing) |
| `src/tuvl/core/artifacts/` | the artifact registry, loaders, and `artifact://` resolution used by `steering` / `skills` / `guardrails` |
| `src/tuvl/core/engine/streaming.py` | `StepEvent` wire serialization shared by SSE, gRPC, and the CLI |
| `src/tuvl/core/engine/orchestrator/` | `RunHandle` / registry / `AgentControl`, Redis mirror, `Supervisor`, agent metrics |
| `src/tuvl/cli/commands/validate.py` | the static checks in §10 |
| `docs/tuvl-agentic-manual.md` §4.4, §4.13–4.16 | the authoring contract this document sits beneath |
