# Human-in-the-Loop — Suspend & Resume Internals

`kind: HumanInTheLoop` pauses a running workflow, persists its state to Postgres, and hands control to a human. A later `POST /api/workflows/resume` call merges the human's answer into the saved context and continues execution — in a different process, hours or days later, with no worker held open in between.

This document covers the internals: what is persisted, how the engine suspends, and the exact semantics of resume. For the YAML authoring contract, see [tuvl-agentic-manual.md §4.10](tuvl-agentic-manual.md).

---

## Table of Contents

1. [Overview](#1-overview)
2. [The Suspend Path](#2-the-suspend-path)
3. [The Instance Row](#3-the-instance-row)
4. [The Resume Path](#4-the-resume-path)
5. [Resume Authorization](#5-resume-authorization)
6. [Versioning & Definition Drift](#6-versioning--definition-drift)
7. [Interaction with Autonomous Agents](#7-interaction-with-autonomous-agents)
8. [Operational Notes](#8-operational-notes)
9. [Failure Modes](#9-failure-modes)
10. [Reading the Code](#10-reading-the-code)

---

## 1. Overview

A `HumanInTheLoop` step is a declared pause point. When the engine reaches it, the run does **not** block a worker or poll — the workflow's public context is snapshotted into a system table (`tuvl_system_workflow_instances`, Postgres, primary datasource), the HTTP caller receives a `202 Accepted` with a form-rendering payload, and the process is free. Resume is a separate, authenticated HTTP request that rehydrates the engine and continues at the next step.

Use it for approval gates (refunds, access grants, publishing), review of agent output before an irreversible action, or any decision the model should not make alone. A minimal step:

```yaml
- id: approve_refund
  kind: HumanInTheLoop
  ui:
    title: "Approve refund"
    instruction: "Refund {{ amount }} to {{ customer_name }}?"
    display_context: [amount, customer_name, order_summary]
  human_feedback:
    - { name: approved, type: boolean, required: true }
    - { name: notes,    type: string }
  output_key: approval
  auth:
    required_group: finance_approvers
```

`HumanInTheLoop` requires a database session in `context["_session"]`, so it only works on the DB-backed execution paths (REST, SSE, gRPC). The CLI test runner (`tuvl/cli/testing/runner.py`) skips unstubbed HITL steps with a `default` signal so the rest of a workflow can still be exercised; stub the step to inject a simulated answer.

---

## 2. The Suspend Path

```
POST <trigger.path>                        (the workflow's own endpoint)
    │
    ▼
WorkflowEngine.run() ── step loop ── reaches kind: HumanInTheLoop
    │
    ▼
_suspend_for_hitl()                       (engine/runner.py)
    │  builds context_data from ui.display_context (allowlist)
    │  interpolates {{var}} placeholders in ui.instruction
    ▼
_persist_hitl_instance()
    │  INSERT tuvl_system_workflow_instances (flush — id assigned, not committed)
    ▼
raise SuspendWorkflowError(instance_id, hitl_request, paused_step_id)
    │
    ▼
caught in _run_engine / _sse_generator     (api/manager.py)
    │  session.commit()  ← the row becomes durable HERE
    ▼
REST: HTTP 202, body = hitl_request
SSE:  event: suspended, data = hitl_request
gRPC: StepEvent(event_type="suspended", snapshot_json = hitl_request)
```

Key points:

- Suspension is exception-driven. `_dispatch_step` checks for `HumanInTheLoop` before any other kind and raises `SuspendWorkflowError` — the step never returns a route signal, and the engine loop unwinds immediately.
- The insert is only flushed inside the engine; the **caller** (`_run_engine` or `_sse_generator` in `tuvl/core/api/manager.py`) commits it when catching the exception. This means work done by prior steps in the same transaction commits together with the suspension.
- The `hitl_request` payload is built once, attached to the exception, and forwarded verbatim on all three transports.

The 202 body / `suspended` frame:

```json
{
  "instance_id": "8c9a…",
  "paused_step_id": "approve_refund",
  "output_key": "approval",
  "ui": {
    "title": "Approve refund",
    "instruction": "Refund 95.00 to Ada Lovelace?",
    "display_context": ["amount", "customer_name", "order_summary"]
  },
  "human_feedback": [
    { "name": "approved", "type": "boolean", "required": true },
    { "name": "notes",    "type": "string" }
  ],
  "context_data": { "amount": 95.0, "customer_name": "Ada Lovelace", "order_summary": "…" },
  "auth": { "required_group": "finance_approvers" }
}
```

- `context_data` contains **only** the keys listed in `ui.display_context` (ORM objects are converted to plain dicts). Public context keys not on the allowlist never leave the server in this payload.
- `ui.instruction` supports `{{var}}` interpolation against the context; unresolved placeholders are left intact.
- `auth` is echoed only when the step declares it. `auth.assignee_user` is a UI routing hint for inboxes/notifications — the server does not enforce it.
- `instance_id` is the only value the client must keep. Everything else exists so a frontend can render the form without another round-trip.

---

## 3. The Instance Row

`SystemWorkflowInstance` (`tuvl/core/hitl/models.py`) maps to `tuvl_system_workflow_instances`, created automatically at startup on the **primary** datasource (`tuvl/core/datasources/postgres.py`, `checkfirst=True` — no migration needed). Storage is Postgres, not Redis: suspensions survive restarts and have no eviction.

| Column | Type | Contents |
|---|---|---|
| `id` | UUID PK | Returned to the caller as `instance_id`. |
| `workflow_name` | text | Logical `metadata.name`; used to look up the config in `WORKFLOW_REGISTRY` on resume. |
| `paused_step_id` | text | The HITL step's `id`; used to compute the resume start index. |
| `call_stack` | JSON | Ordered workflow-name list; currently always `[workflow_name]`, reserved for nested workflows. |
| `context_snapshot` | JSONB | All public context keys, serialized. |
| `ui_interaction` | JSON | The `human_feedback` schema, echoed verbatim. |
| `output_key` | text | Where the human answer is merged on resume (defaults to `hitl_<step_id>`). |
| `user_id` | UUID, nullable | The triggering user; `NULL` for unauthenticated triggers. Drives the owner-only resume rule. |
| `created_at` | timestamptz | Suspension time; for audits and manual sweeps. |

Serialization (`serialize_context`):

- Every `_`-prefixed key is stripped — `_session`, `_db`, `_user_id`, `_last_error`, `_response`, `_route` never reach the database.
- The remainder is round-tripped through JSON with UUID and datetime values converted to strings.

Masking note: the snapshot stores **all** public keys **unmasked** — the full state is required to resume, so `secure: true` field masking (see [observability.md](observability.md)) is not applied here. The database is the trust boundary. What leaves the server is a different story: the wire payload is limited to the `display_context` allowlist, and trace-span snapshots are masked separately. Keep this in mind when granting read access to the system table.

---

## 4. The Resume Path

```
POST /api/workflows/resume
Content-Type: application/json

{ "instance_id": "8c9a…", "human_input": { "approved": true, "notes": "ok" } }
```

Handled by `tuvl/core/hitl/router.py` in this exact order:

1. **Row-locked load.** `session.get(SystemWorkflowInstance, id, with_for_update=True)` takes a `SELECT … FOR UPDATE` lock. Two concurrent resumes for the same instance serialize: the second blocks until the first commits its delete, then sees the row gone and gets `404`. Double execution is impossible.
2. **Config + step resolution.** The workflow config comes from `WORKFLOW_REGISTRY[workflow_name]`; the paused step dict is located inside it (its `auth.required_group` decides authorization, so this happens before the auth check). Unregistered workflow → `400`.
3. **Authorization.** See [§5](#5-resume-authorization). Failure → `403`.
4. **Context reconstruction.** `context_snapshot` is copied, then the private references are re-injected: a fresh `_session`, a rebuilt `_db` (`WorkflowUoW` from the same context extractors the fresh-run handler uses, so all three `context:` forms — string, list, and `{models: [{name, version}]}` — yield the correct allowlist **and version-pin map** on resume), `_user_id` set to the **resumer's** id, and `_instance_id` restored from the row id — the row reuses the run's per-run UUID, so `scope: instance` RAG rows ingested before suspension stay reachable. Finally `human_input` is merged at the instance's `output_key`:

   ```python
   resumed_context[instance.output_key or f"hitl_{paused_step_id}"] = body.human_input
   ```

   The answer is a plain dict under one key — downstream steps read `{{ approval.approved }}`, `{{ approval.notes }}`.
5. **Start index.** `start_index = index(paused_step_id) + 1`. Execution continues at the step **after** the HITL step in document order. The HITL step's `routes:` block is **not** consulted on resume — a HITL step cannot branch. Branch on the answer with a `Router` step placed immediately after it. If the paused step id no longer exists in the current definition → `400`.
6. **One-shot delete.** The row is deleted and **committed in its own transaction before the engine runs**. If the engine crashes mid-resume, the instance is already gone — resume is at-most-once, never at-least-once. A crashed resume cannot be retried; the workflow must be triggered again from the start. This is a deliberate trade: replay of a consumed `instance_id` (with a possibly different `human_input`) is a worse failure than a lost run.
7. **Dispatch.** With `Accept: text/event-stream` the response streams SSE frames; otherwise it is the standard JSON envelope. The resumed run behaves exactly like a fresh one from `start_index` onward — including suspending **again** if it reaches another `HumanInTheLoop` step, which produces a fresh `202` with a brand-new `instance_id` (each suspension is an independent row).

Resume response shapes match every other workflow endpoint:

| Outcome | Status | Body |
|---|---|---|
| Ran to completion | `200` | `{"success": true, "status_code": 200, "data": …, "error": null}` |
| Hit another HITL step | `202` | a new `hitl_request` payload |
| Business error (`_last_error`) | `400` | `{"success": false, …, "error": {…}}` |
| Engine error | `500` | `{"success": false, …}` |

---

## 5. Resume Authorization

The resume endpoint requires an authenticated caller (`get_current_user`). Who may resume depends on whether the paused step declares `auth.required_group`:

| Step `auth` | Instance `user_id` | Who may resume |
|---|---|---|
| `required_group: G` | any | members of group `G`, or `iam:admin`. The triggering user gets **no** implicit right — the requester cannot approve their own request unless they independently hold `G` or admin. |
| none | set | the triggering user (owner), or `iam:admin`. |
| none | `NULL` (unauthenticated trigger) | `iam:admin` only. |

`iam:admin` (a scope) bypasses either rule. `auth.assignee_user` never grants or restricts anything server-side — it is a hint for UIs to route the task. All of the above is enforced in `tuvl/core/hitl/router.py` and pinned by `tests/core/test_hitl_required_group.py`.

Because the group requirement is read from the **current** workflow definition (not stored on the row), redeploying the workflow with a different `auth` block changes who may resume already-pending instances.

---

## 6. Versioning & Definition Drift

The instance stores only the logical `workflow_name` — not a schema version or a copy of the definition. Resume always runs against the **currently registered** config: `WORKFLOW_REGISTRY` is the flat map where the last-loaded enabled `schema_version` per name wins.

Consequences of changing a workflow while instances are pending:

- Steps added/removed **before** the paused step: safe — the step index is recomputed by id on every resume, not stored.
- The paused step's `id` renamed or removed: resume returns `400` ("paused step no longer exists").
- The workflow unregistered entirely: resume returns `400`; the orphaned row stays in the table (see [§8](#8-operational-notes)).
- Steps after the paused step changed: the resumed run simply executes the new definition.

---

## 7. Interaction with Autonomous Agents

A `HumanInTheLoop` step is a natural escalation target for an autonomous-mode `Agent`'s closed outcome set. The agent's `outcome.enum` values are routed like any other signal, and a route may point at a HITL step id:

```yaml
- id: triage_agent
  kind: Agent
  mode: autonomous
  agent:
    outcome:
      enum: [resolved, escalate, needs_human]
      write: agent_result
  routes:
    resolved:    format_reply
    escalate:    notify_manager
    needs_human: hitl_review        # ← suspends here; agent_result is in the snapshot

- id: hitl_review
  kind: HumanInTheLoop
  ui:
    title: "Agent needs a human"
    display_context: [agent_result]
  human_feedback:
    - { name: decision, type: string, required: true }
  output_key: human_decision
```

When the agent exits `needs_human`, the engine jumps to `hitl_review` and suspends. `agent_result` is part of the persisted snapshot, so the reviewer sees the agent's reasoning (via `display_context`) and the resumed run has both `agent_result` and `human_decision` in context.

---

## 8. Operational Notes

- **No TTL.** Nothing expires or garbage-collects suspended instances — rows live until resumed (which deletes them) or manually removed. `created_at` exists so operators can build their own sweep or SLA alerting.
- **No list endpoint.** The engine exposes no HTTP API to enumerate pending instances; inspect the table directly:

  ```sql
  SELECT id, workflow_name, paused_step_id, user_id, created_at
  FROM tuvl_system_workflow_instances
  ORDER BY created_at;
  ```

- **Storage is Postgres**, on the primary datasource, auto-created at startup. Suspensions survive restarts and redeploys.
- **`output_key` is effectively required.** `tuvl validate` warns when a HITL step omits it; the engine then falls back to `hitl_<step_id>`, which downstream steps must reference literally.
- **SSE callers** must handle three terminal frame types: `done`, `error`, and `suspended` (`tuvl/core/engine/streaming.py`). A `suspended` frame ends the stream; the client re-connects via the resume endpoint.
- **tuvl insight (Lens)** probes report a HITL suspension as `status: "suspended"` with the `hitl_request` as output rather than treating it as a failure; Spectrum traces skip HITL steps with a `default` signal.
- **Structured logs** to watch: `"Workflow suspended"` (with `workflow`, `step`, `instance_id`) on suspend, and `"Resuming workflow …"` on resume.

---

## 9. Failure Modes

| Scenario | Status | Detail |
|---|---|---|
| Unknown `instance_id` | `404` | Never existed, or already consumed. |
| Double resume (sequential) | `404` | The row was deleted before the first resume's engine run — the second call finds nothing, even if the first is still executing. Pinned by `tests/core/test_hitl_engine.py`. |
| Double resume (concurrent) | `404` for the loser | The `FOR UPDATE` lock serializes the two requests. |
| Caller lacks `required_group` (incl. the requester self-approving) | `403` | Detail names the missing group. |
| Caller is neither owner nor admin (no `auth` block) | `403` | |
| Workflow no longer registered | `400` | Row remains in the table. |
| `paused_step_id` missing from current definition | `400` | Definition drifted; row remains. |
| Resumed run hits another HITL step | `202` | New instance, new `instance_id`. |
| Engine crash after the delete commit | run lost | At-most-once by design; re-trigger the workflow. |

---

## 10. Reading the Code

| Module | What lives there |
|---|---|
| `src/tuvl/core/engine/runner.py` | `SuspendWorkflowError`, `_persist_hitl_instance`, `WorkflowEngine._suspend_for_hitl`, HITL dispatch in `_dispatch_step`, `start_index` support in `run` / `run_streaming`. |
| `src/tuvl/core/hitl/models.py` | `SystemWorkflowInstance` table model, `serialize_context`. |
| `src/tuvl/core/hitl/router.py` | `POST /api/workflows/resume`: row lock, authorization, context rehydration, one-shot delete, dispatch. |
| `src/tuvl/core/api/manager.py` | `_run_engine` / `_sse_generator` catch `SuspendWorkflowError`, commit the row, emit `202` / `suspended`; `WORKFLOW_REGISTRY`. |
| `src/tuvl/core/engine/streaming.py` | `suspended_event_to_sse` and the other SSE frame serializers. |
| `src/tuvl/core/grpc/servicer.py` | gRPC `suspended` step-event. |
| `src/tuvl/core/datasources/postgres.py` | Startup creation of `tuvl_system_workflow_instances` on the primary datasource. |
| `tests/core/test_hitl_engine.py` | Invariants: display-context masking, replay → 404, private-key stripping/re-injection, resume index, chained HITL. |
| `tests/core/test_hitl_required_group.py` | The full authorization matrix. |
