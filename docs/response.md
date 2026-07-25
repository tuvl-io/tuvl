# Response — Shaping the Workflow's Exit Contract

`kind: Response` is the step that decides what a workflow's caller sees: it projects the final context into the HTTP body. Without it, the caller gets a generic dump of public context. This document covers the internals — how the payload is built, how it reaches the HTTP layer, which status codes apply, and where masking does (and does not) happen. For the authoring reference, see `tuvl-agentic-manual.md` §4.9.

---

## Table of Contents

1. [Mental Model](#1-mental-model)
2. [The Step's Two Modes](#2-the-steps-two-modes)
3. [How the Payload Reaches the Caller](#3-how-the-payload-reaches-the-caller)
4. [Status Codes](#4-status-codes)
5. [Streaming and gRPC Differences](#5-streaming-and-grpc-differences)
6. [Masking Behavior](#6-masking-behavior)
7. [Terminating Semantics](#7-terminating-semantics)
8. [Failure Modes](#8-failure-modes)
9. [Reading the Code](#9-reading-the-code)

---

## 1. Mental Model

A workflow accumulates state in its context dict; most of it is intermediate. The Response step writes a *projection* of that state to the reserved key `context["_response"]`, and the HTTP layer (`tuvl.core.api.manager`) uses that as the response root. The resolution order at the end of a run:

1. `_response` present → it becomes the body (a `kind: Response` step ran).
2. Otherwise, the workflow's top-level `output:` key names a context value → that value becomes the body.
3. Otherwise → all public context keys (everything not prefixed `_`) are returned.

Fallback 3 is a debugging convenience, not a contract — it leaks every public key the workflow touched. Production workflows should always end through a Response step (or `output:`).

---

## 2. The Step's Two Modes

`_run_response_step` (`tuvl.core.engine.runner`) accepts exactly one of two shaping modes:

```yaml
# Source mode — expose one existing context key as-is
- id: respond
  kind: Response
  source: candidate

# Mapping mode — project fields, flattening nested paths
- id: respond
  kind: Response
  mapping:
    id: candidate.id
    full_name: candidate.name
    score: evaluation.total_score
```

| Mode | Behavior |
|---|---|
| `source: <key>` | `context["_response"] = context[<key>]` — the raw value, any JSON-serializable type. A missing key yields `null`, not an error. |
| `mapping: {alias: dot.path}` | Builds a fresh dict; each value is resolved with dot-path lookup into nested dicts. |
| neither | Logs `response.config.missing` and falls back to the full public context (same as having no Response step). |

Both modes read; neither mutates business keys. The step emits `default` on success. Any exception during shaping is caught: the error string lands in `_last_error` and the step emits `error`, routable via `routes:`.

---

## 3. How the Payload Reaches the Caller

```
WorkflowEngine.run … final step
        │  context["_response"] = <shaped>
        ▼
manager._run_engine
        │  priority: _response → output: key → public context
        ▼
JSONResponse envelope
{
  "success": true,
  "status_code": 200,
  "data": <the shaped payload>,
  "error": null
}
```

Every REST workflow response is wrapped in this `{success, status_code, data, error}` envelope — the Response step shapes `data`, never the envelope itself. The envelope is serialized through FastAPI's `jsonable_encoder`, so non-JSON-native values survive the trip: `date`/`datetime` → ISO strings, `Decimal` → float, `UUID` → string. SSE frames serialize the same values via `default=str`.

When the workflow declares `trigger.response_schema`, the shaped payload is validated against it (pydantic `TypeAdapter`) after shaping; a mismatch is a 500 (`Workflow Output Validation Failed`), so the schema is a contract on the Response step's output.

---

## 4. Status Codes

| Condition | Status | `data` |
|---|---|---|
| Run completed, no `_last_error` | 200 | shaped payload |
| Run completed but `_last_error` is set (a step failed and was routed to a normal end) | 400 | shaped payload, plus `error.details` = the error string |
| `HumanInTheLoop` suspension | 202 | the `hitl_request` payload (no envelope) — see `human-in-the-loop.md` |
| Typed trigger body failed `input_schema` validation | 422 | FastAPI validation detail |
| Output failed `response_schema` validation | 500 | `null` |
| Engine `RuntimeError` (unmapped signal, version-pin mismatch, …) / unexpected exception | 500 | `null`, sanitized detail via `core/errors.safe_detail` |

Note the 400 case: reaching a Response step does not guarantee 200. If any earlier step recorded `_last_error` and the workflow still ran to completion, the envelope reports failure with the shaped payload attached. Clear error state deliberately (a Functional step can pop `_last_error`) if a recovered error should read as success.

---

## 5. Streaming and gRPC Differences

The three transports do not deliver the shaped payload identically:

| Transport | Terminal payload |
|---|---|
| REST (sync) | Envelope with `data` = shaped payload (priority rules above) |
| SSE | `done` event `{success, data, error}` — same priority rules; `data` is the shaped payload |
| gRPC (`RunWorkflow`) | Terminal `StepEvent(event_type="done")` carrying the **masked public snapshot** — `_response` is a `_`-prefixed key and is stripped from snapshots, so the shaped payload is *not* delivered on this frame |

A gRPC/SDK consumer therefore sees the public context, not the Response projection. Shape gRPC-consumed results by writing them to a public context key (the `output_key` of a prior step) rather than relying on the Response projection alone.

---

## 6. Masking Behavior

Two different mechanisms apply on different paths — be precise about which one protects what:

- **Snapshot masking** (`core/engine/masking.py`, `public_safe_snapshot`) strips `_`-keys and masks `secure: true` model fields. It is applied to every streamed step-event snapshot and the gRPC terminal frame.
- **The sync/SSE response body is not masked.** A Response step's payload is returned exactly as shaped — `source: candidate` returns the candidate record including its `secure: true` fields. The step is trusted output selection: expose only what the caller should see. Use `mapping:` to project non-sensitive fields instead of `source:` when the source model carries secure fields.

The fallback full-context dump (no Response step, no `output:`) strips `_`-keys but does **not** apply secure-field masking either — one more reason the fallback is not for production.

---

## 7. Terminating Semantics

A Response step is usually the workflow's last step, but termination is positional, not kind-based:

- The engine ends the run when the current step has no matching route and is the last element of `steps:` (implicit fall-through), or when a route points to `END`.
- Golden Rule 7: when a Response step is not literally last in `steps:` (e.g. it sits on one Router branch), give it an explicit route to `END`.
- Multiple Response steps are legal — one per branch. Each overwrites `_response`; the last one to run wins.

`tuvl validate` has no Response-specific checks beyond the generic ones (known `kind:`, unique ids, mapped signals) — a typo'd `source:` key is only observable at runtime as a `null` body.

---

## 8. Failure Modes

| Failure | Result |
|---|---|
| `source:` key absent from context | `data: null`, 200 — not an error |
| `mapping:` path missing a segment | that alias is `null` in the body |
| Exception while shaping (non-serializable value, …) | signal `error`, `_last_error` set; unrouted `error` at the last step → run ends, envelope is the 400 business-error shape |
| Shaped payload fails `response_schema` | 500, `data: null` |
| No Response step and no `output:` | full public context returned (with a `response.config.missing` warning when the step exists but is empty) |

---

## 9. Reading the Code

| Module | What it holds |
|---|---|
| `tuvl/core/engine/runner.py` | `_run_response_step` — both modes, `_resolve_tpl_obj` dot-path resolution |
| `tuvl/core/api/manager.py` | `_run_engine` (sync envelope, priority rules, status codes), `_sse_generator` (SSE `done` event) |
| `tuvl/core/grpc/servicer.py` | gRPC terminal `done` frame (masked snapshot) |
| `tuvl/core/engine/masking.py` | `public_safe_snapshot` — what streamed frames may carry |
| `tuvl/core/errors.py` | `safe_detail` — sanitized 500 messages |
| `tests/core/test_workflow_route_plumbing.py` | route/termination coverage |
| `tests/core/test_masking.py` | snapshot masking pins |
