# Functional Nodes — Custom Python Steps

`kind: Functional` is the escape hatch of tuvl's closed step-kind set: arbitrary async Python executed inside a declared workflow contract. The YAML declares *where* the node runs, what it may touch, and where each of its signals routes; the Python decides *what* happens. Everything else — spans, structured logs, context masking, data-access permissions, error routing — is supplied by the engine around the call.

This document covers the node contract, registration and discovery, the exact execution path in the runner, data access rules, the built-in system nodes, and how a Functional step doubles as a tool for an autonomous-mode `Agent` step. For the YAML authoring rules themselves, see the [agentic manual](tuvl-agentic-manual.md) §4.3 and Golden Rules 13/14 in §5 — they are the authoring authority and are not duplicated here.

---

## Table of Contents

1. [Mental Model](#1-mental-model)
2. [The Node Contract](#2-the-node-contract)
3. [Registration & Discovery](#3-registration--discovery)
4. [The Execution Path](#4-the-execution-path)
5. [Context & Data Access](#5-context--data-access)
6. [Built-in System Nodes](#6-built-in-system-nodes)
7. [Functional Steps as Agent Tools](#7-functional-steps-as-agent-tools)
8. [Validation & Failure Modes](#8-validation--failure-modes)
9. [Scaffolds & the Dev UI Editor](#9-scaffolds--the-dev-ui-editor)
10. [Reading the Code](#10-reading-the-code)

---

## 1. Mental Model

A Functional node is a plain async Python function registered under a name. The workflow step references that name via `runner:`; at execution time the engine looks the function up in a global registry, hands it the live context dict, and interprets what it returns as a routing signal.

```yaml
- id: validate
  kind: Functional
  runner: my_validator        # must be registered via @node("my_validator")
  routes:
    valid:   process
    invalid: reject
    error:   handle_failure
```

Custom nodes exist for orchestration logic that no other step kind expresses — scoring, transformation, custom I/O. For pure CRUD, prefer `kind: ModelOp` (Golden Rule 13); for outbound HTTP, `kind: APICall`; for LLM calls, `kind: Agent`. A Functional node should be the last resort, not the first.

---

## 2. The Node Contract

A node is an `async` function that takes the workflow context dict and returns an updated context, a signal, or both:

```python
# nodes/my_validator.py
"""Reject submissions without an email address."""
from typing import Any

from tuvl.core.nodes.base import node


@node("my_validator")
async def my_validator(ctx: dict[str, Any]) -> tuple[dict[str, Any], str]:
    email = str(ctx.get("email") or "").strip()
    if "@" not in email:
        ctx["rejection_reason"] = "invalid email"
        return ctx, "invalid"
    ctx["email"] = email.lower()
    return ctx, "valid"
```

### Inputs

The single argument is the mutable workflow context: the trigger input plus every key earlier steps produced. Engine-owned keys are `_`-prefixed (see [§5](#5-context--data-access)). One of them is set specifically for this call: `ctx["_step"]` holds the full YAML dict of the current step, so a node can read its own configuration (`collection:`, `top_k:`, or any custom key you add to the step). It is injected immediately before the call and removed afterwards.

### Return values

The runner (`_run_functional_step` in `src/tuvl/core/engine/runner.py`) accepts four shapes:

| Return value | Effect on context | Signal |
|---|---|---|
| `dict` | Returned dict **replaces** the context | `default` |
| `str` | Context object kept as passed (in-place mutations persist) | the string |
| `(dict, str)` tuple | First element replaces the context | second element |
| anything else (incl. `None`) | Context object kept as passed | `default` |

Notes:

- Replacing the context means exactly that — return `{**ctx, "score": 1.0}` or the mutated `ctx` itself; returning a partial dict drops every other key.
- Any signal other than `default` **must** be mapped in the step's `routes:` — an unmapped non-default signal raises `RuntimeError` and aborts the run (Golden Rule 6). An unmapped `default` falls through to the next step in list order.
- `_step` is stripped from whatever comes back, so a node cannot leak or overwrite it.

### Error handling

Any exception raised by the node is caught by the runner: it logs a warning, writes `ctx["_last_error"] = str(exc)`, and emits the signal `error`. If the step maps `error:` in `routes:`, the workflow branches there; if not, the run stops cleanly with a warning (`Workflow node error — no error route, stopping`). Raising is therefore the idiomatic way to signal failure — do not invent an ad-hoc failure signal.

One failure is *not* soft: a `runner:` name that is not in the registry raises `RuntimeError` before the node is called. That is an authoring bug, not a runtime condition, and it aborts the run regardless of an `error:` route.

---

## 3. Registration & Discovery

### The `@node` decorator

`src/tuvl/core/nodes/base.py` is the entire registry mechanism:

```python
NODE_REGISTRY: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}

def node(name: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        if name in NODE_REGISTRY:
            raise ValueError(f"Node '{name}' is already registered. ...")
        NODE_REGISTRY[name] = func
        return func
    return decorator
```

Registration happens as a side effect of importing the file. Duplicate names raise `ValueError` at import time — which fails startup, since discovery re-raises import errors.

### Discovery at startup

During app startup (`lifespan` in `src/tuvl/core/app.py`), `load_all_nodes()` from `src/tuvl/core/nodes/loader.py` imports every node file:

- The directory is `settings.dir_for("nodes")` — `nodes/` by default, remappable via a `ProjectConfig` `config.yaml`.
- Files matching `*.py` are imported in sorted order; files starting with `_` are skipped (shared helpers can live in `nodes/_utils.py` without being treated as nodes).
- Each file is imported under the module namespace `_tuvl_nodes.<filename-stem>`.
- A file that fails to import is logged (`Failed to load node module`) and the exception is **re-raised** — a broken node file prevents the server from starting. Catch problems earlier with `tuvl validate`.
- A missing `nodes/` directory is not an error; the loader logs and skips.

After user nodes load, `register_rag_nodes()` (`src/tuvl/core/nodes/rag.py`) adds the built-in `DataIngest` / `DataSearch` entries — but only if the names are still free, so a user node can deliberately shadow a built-in by claiming its name first.

### One `@node()` per file — and why

Each node lives in its own file, named after its decorator argument: `@node("score_resume")` lives in `nodes/score_resume.py`. This is Golden Rule 14 and it is load-bearing, not stylistic: the dev UI code editor resolves a step's source file as `nodes/{runner}.py`. If the file for a runner name does not exist under that exact name — because two nodes share a file, or the filename drifted from the decorator argument — the editor falls back to displaying generated scaffold code instead of the real implementation, and saving from the editor writes a *new* `{runner}.py`, silently forking the implementation. The loader itself only cares about decorator names, but `tuvl validate` warns on both violations (filename mismatch, multiple `@node()` decorators in one file). Keep one `@node()` per file, filename equal to the decorator argument, without exception.

---

## 4. The Execution Path

```
step reached (kind: Functional, or kind omitted)
    │
    ▼
_dispatch_step ──► _run_functional_step
    │
    ├─ NODE_REGISTRY.get(step["runner"])
    │       └─ None → RuntimeError (lists registered nodes) — run aborts
    │
    ├─ ctx["_step"] = step
    ├─ result = await node_func(ctx)
    │       ├─ dict          → context = result,        signal = "default"
    │       ├─ str           → context unchanged,       signal = result
    │       ├─ (dict, str)   → context = result[0],     signal = result[1]
    │       ├─ other/None    → context unchanged,       signal = "default"
    │       └─ exception     → ctx["_last_error"] set,  signal = "error"
    └─ ctx.pop("_step")
    │
    ▼
_advance(routes, signal)
    ├─ signal mapped in routes:  → jump to target step ("END" stops the run)
    ├─ "default", unmapped       → next step in list order
    ├─ "error",   unmapped       → stop run (warning logged)
    └─ other,     unmapped       → RuntimeError
```

Two details worth knowing:

- **Missing or unknown `kind:` falls through to the Functional handler.** `_dispatch_step` checks the recognised kinds explicitly and treats everything else as Functional — a step with no `kind:` is Functional by default, and a typo like `kind: Functionnal` reaches the Functional runner at runtime (then fails on the missing runner registration). `tuvl validate` catches the typo statically: any kind outside the closed set in `src/tuvl/core/engine/step_kinds.py` is a validation error.
- **Observability is wrapped around the call, not inside it.** Every step executes inside a `node.Functional` OTel span carrying `tuvl.node.id`, `tuvl.node.kind`, the emitted signal, duration, and a masked context snapshot (`_`-prefixed keys stripped, `secure: true` fields masked). Use structlog inside nodes rather than `print`; see [observability.md](observability.md).

---

## 5. Context & Data Access

### Reserved keys

Engine-owned context keys are `_`-prefixed. A node may *read* them but must never overwrite them (Golden Rule 10). The full table lives in the [agentic manual](tuvl-agentic-manual.md) §4.11; the ones a node actually uses:

| Key | What a node does with it |
|---|---|
| `_step` | Read its own YAML step config |
| `_db` | `WorkflowUoW` — permission-gated repository access |
| `_session` | Primary `AsyncSession`, for raw SQL when a repository is not enough |
| `_last_error` | Read a previous step's failure message (on an `error:` branch) |
| `_context_model_versions` | Read the workflow's model version pins |
| `_instance_id` | Per-run instance UUID minted by the engine; the RAG nodes use it for instance-scoped collections (`scope: instance`) |
| `_user_id` | Authenticated principal, when the trigger was authenticated |

`_`-prefixed keys are stripped from span snapshots, SSE frames, and HTTP responses — anything a node stores under a `_` name is invisible downstream, which is exactly why nodes must not use that namespace for outputs.

### The models allowlist and `WorkflowUoW`

`ctx["_db"]` is a `WorkflowUoW` (`src/tuvl/core/repositories/uow.py`), a unit of work scoped to this execution. Indexing it by model name returns a repository:

```python
repo = ctx["_db"]["candidate"]          # case-insensitive model name
record = await repo.get(ctx["candidate_id"])
await repo.update(record.id, {"stage": "screening"})
```

Access is gated by the workflow's `spec.context` declaration: indexing a model **not** listed there raises `PermissionError` naming the allowed set (Golden Rule 5). Two further guards apply on first access: if the workflow pinned a model version (`context.models[].version`) that does not match what `MODEL_REGISTRY` loaded, the UoW raises `RuntimeError` rather than executing against a mismatched schema; and models bound to a non-primary datasource are transparently routed to their own session with the same tenant isolation as the primary.

Do not commit. Transaction boundaries are owned by the engine (`_run_engine` in `src/tuvl/core/api/manager.py`): one commit on success, rollback on failure, for the whole run. `await session.flush()` is fine when a node needs generated IDs mid-run.

---

## 6. Built-in System Nodes

Two system nodes are registered as Functional runners at startup and require no Python authoring (manual §4.2 has the full YAML contracts):

| Runner | Purpose | Key step fields |
|---|---|---|
| `DataIngest` | Embed a document and persist it to the system vector store | `collection`, `document`, `scope`, `metadata` |
| `DataSearch` | Hybrid retrieval over the vector store; writes `list[{content, metadata, score}]` into the context | `collection`, `query`, `top_k`, `metadata_filter`, `output_key` |

Both live in `src/tuvl/core/nodes/rag.py`, read their configuration from `ctx["_step"]` with `{{key}}` template resolution against the context, and require the pgvector Postgres extension — they raise with an install pointer when it is absent.

Internals, briefly: `DataIngest` resolves the collection's embedding model from the collection registry, calls `litellm.aembedding`, and inserts a `SystemVectorStore` row (`tuvl_system_vector_store`) with the text, vector, optional JSONB metadata, and — for `scope: instance` — the workflow instance id. `DataSearch` embeds the query the same way, then runs a single Reciprocal Rank Fusion SQL query (`score = 1/(60 + vector_rank) + 1/(60 + text_rank)`) fusing L2 vector distance against `ts_rank` full-text ranking in one CTE, with the `metadata_filter` applied as a JSONB containment predicate (`@>`) inside both ranked CTEs. `scope: global` matches only globally-stored rows; `scope: instance` additionally matches rows scoped to the current run.

Because registration checks `if name not in NODE_REGISTRY`, a user file `nodes/DataIngest.py` would take precedence over the built-in.

---

## 7. Functional Steps as Agent Tools

A Functional step can serve as a tool for a `kind: Agent` step in `mode: autonomous`: `agent.tools[].ref` names the step id, and the agent's model may then call it during its loop. The dispatch path (`_dispatch_tool` in `src/tuvl/core/engine/runner.py`) runs the node against a shallow copy of the live context with the model-generated arguments merged in; the public-context delta the node produced is returned to the model as the tool result, and merges back into the real context only when the tool entry sets `writes_context: true`. The step's own `routes:` are ignored in tool mode. Every tool needs a `description` — sourced from the referenced step's `description:` field, with the tool entry's own `description:` as a fallback — or `tuvl validate` errors, since the description is what tells the model when to call it. In the dev UI canvas, you attach a step as a tool by dragging from the agent's `tools` handle onto the step's bottom tool handle (a step already wired into the flow must be disconnected first). Full loop semantics: [autonomous-agent.md](autonomous-agent.md).

---

## 8. Validation & Failure Modes

`tuvl validate` checks Functional plumbing statically (`src/tuvl/cli/commands/validate.py`):

- **Per node file** (`_validate_nodes`): AST syntax check; collection of every `@node("name")` decorator argument; then a real import of the file — import-time failures (missing packages, broken code, duplicate node names) are errors. Golden Rule 14 violations — a filename that does not match the decorator argument, or multiple `@node()` decorators in one file — are warnings.
- **Per workflow step**: `kind:` must be in the closed set; a Functional step (or a step with no `kind:`) must declare `runner:`, and the runner must be among the collected node names; every route target must be a real step id or `END`.
- Not checked: the node's return shape — that surfaces at runtime.

Runtime failure summary:

| Failure | Behaviour |
|---|---|
| Node file fails to import at startup | Logged, re-raised — server does not start |
| `runner:` not in `NODE_REGISTRY` | `RuntimeError`, run aborts (not routable) |
| Node raises an exception | `_last_error` set, signal `error` — routable |
| Non-default signal unmapped in `routes:` | `RuntimeError`, run aborts |
| `error` signal unmapped | Run stops cleanly with a warning |
| Node touches an undeclared model via `_db` | `PermissionError` from `WorkflowUoW` — surfaces like any node exception |

---

## 9. Scaffolds & the Dev UI Editor

`tuvl init` creates the `nodes/` directory and (with samples) two reference implementations, `nodes/score_resume.py` and `nodes/mark_rejected.py` — each a single `@node()` file demonstrating the contract (templates in `src/tuvl/cli/commands/init_templates.py`).

In dev mode, the workflow canvas embeds a code editor per Functional step. It is a thin client over two endpoints in `src/tuvl/core/dev/router.py`: `GET /dev/nodes/{name}` returns the file content and `PUT /dev/nodes/{name}` writes it (filenames are traversal-guarded and must be bare `*.py` names). The editor derives the filename from the step's `runner` field — `{runner}.py` — and when no such file exists it shows a generated scaffold (decorator, async signature, docstring) as the starting point; saving writes the real file. The in-editor AI assistant (`src/tuvl/core/dev/ai.py`, exposed via `DevService.AiChat`) generates full-file replacements under the same contract rules — one decorator per file, async signature, reserved keys untouched — so AI-generated and hand-written code stay interchangeable through the same file. This filename derivation is the reason behind the one-node-per-file rule in [§3](#3-registration--discovery).

---

## 10. Reading the Code

| Module | What lives there |
|---|---|
| `src/tuvl/core/nodes/base.py` | `NODE_REGISTRY` and the `@node` decorator |
| `src/tuvl/core/nodes/loader.py` | `load_all_nodes` — startup import of `nodes/*.py` |
| `src/tuvl/core/nodes/rag.py` | `DataIngest` / `DataSearch` implementations and registration |
| `src/tuvl/core/engine/runner.py` | `_dispatch_step`, `_run_functional_step`, `_advance`, `_dispatch_tool` |
| `src/tuvl/core/engine/step_kinds.py` | The closed set of step kinds |
| `src/tuvl/core/repositories/uow.py` | `WorkflowUoW` — allowlist gating, version pins, datasource routing |
| `src/tuvl/core/api/manager.py` | Context assembly (`_session`, `_db`), commit/rollback ownership |
| `src/tuvl/cli/commands/validate.py` | `_validate_nodes` + Functional step checks |
| `src/tuvl/core/dev/router.py`, `src/tuvl/core/dev/ai.py` | Dev UI node file endpoints and the in-editor assistant |
| `src/tuvl/cli/commands/init_templates.py` | The `tuvl init` sample nodes |
