# ModelOp — Declarative CRUD Inside a Workflow

`kind: ModelOp` is tuvl's zero-code persistence step: one YAML block that creates, reads, lists, updates, or deletes a record of a registered model, scoped by the workflow's `context:` allowlist. It exists so pure CRUD never needs a custom Python node (Golden Rule 13). This document explains the internals, end to end. For the authoring reference (the YAML contract, field by field), see [`tuvl-agentic-manual.md`](tuvl-agentic-manual.md) §4.8.

---

## Table of Contents

1. [Mental Model](#1-mental-model)
2. [The Operation Set](#2-the-operation-set)
3. [Execution Path](#3-execution-path)
4. [Context Interpolation](#4-context-interpolation)
5. [The Allowlist Boundary](#5-the-allowlist-boundary)
6. [Version Pinning](#6-version-pinning)
7. [ModelOp vs the REST CRUD Routes](#7-modelop-vs-the-rest-crud-routes)
8. [As an Autonomous Agent Tool](#8-as-an-autonomous-agent-tool)
9. [Validation and Failure Modes](#9-validation-and-failure-modes)
10. [Reading the Code](#10-reading-the-code)

---

## 1. Mental Model

A `ModelDefinition` YAML file already gives a model three things: a SQLModel class in `MODEL_REGISTRY`, a table, and a set of auto-generated REST routes. `ModelOp` is the fourth surface: the same repository layer, invoked from inside a workflow step instead of from an HTTP client.

```yaml
- id: create_candidate
  kind: ModelOp
  model: Candidate            # PascalCase name from MODEL_REGISTRY
  operation: create           # create | read | list | update | delete
  payload: "{{candidate}}"    # dict or {{template}} — create/update
  record_id: "{{cand_id}}"    # PK value — read/update/delete
  filters:                    # equality filters — list
    stage: screening
  include: "posting,assessments"  # comma-separated relation names — read/list
  limit: 50                   # list cap (default 100)
  output: new_candidate       # context key (default: <step_id>_result)
  routes:
    default: next_step
    error: error_handler
```

The step resolves `{{ }}` templates from the workflow context, calls one repository method, and writes the result back into the context under `output`. No SQL, no session management, no serialization code in the workflow.

---

## 2. The Operation Set

The runner (`_run_model_op_step` in `src/tuvl/core/engine/runner.py`) supports exactly five operations. Anything else routes `error` with `_last_error` set to `unknown ModelOp operation`.

| `operation` | Repo call | Inputs used | Result written to `output` |
|---|---|---|---|
| `create` | `repo.add(payload)` | `payload` (must resolve to a dict) | the new record as a plain dict |
| `read` | `repo.get(record_id)` or `repo.get_expanded(record_id, include)` | `record_id`, optional `include` | record dict, or `None` when not found |
| `list` | `repo.list(criteria, limit)` or `repo.list_expanded(...)` | `filters`, `limit`, optional `include` | list of record dicts (empty list when nothing matches) |
| `update` | `repo.update(record_id, payload)` | `record_id`, `payload` (dict) | updated record dict, or `None` when not found |
| `delete` | `repo.remove(record_id)` | `record_id` | `{"deleted": true|false, "id": <record_id>}` |

Notes grounded in `src/tuvl/core/repositories/base.py`:

- `filters` are **equality only**, ANDed together. Filter keys that are not columns on the model are silently skipped. String filter values are coerced to the column's native type (UUID, int, bool, Decimal) before comparison.
- `include` triggers relation expansion (`get_expanded` / `list_expanded`): each included relation costs exactly one extra IN-clause query — never N+1. Included relation names come from the model's declared relations (`MODEL_RELATIONS`).
- `update` sets only the attributes present in `payload` that exist on the record (partial update). `create` passes the payload straight to the model constructor, so unknown fields raise and route `error`.
- ISO-format **string** write values are coerced to the column's native type before the constructor sees them (date, datetime, UUID, Decimal — `_coerce_write_value`). LLM-extracted JSON can therefore feed `create`/`update` payloads directly; a string that fails to parse passes through unchanged and surfaces as the DB layer's error.
- Misses are **not** errors: a `read` of a missing id yields `None`, a `list` with no matches yields `[]`, an `update` of a missing id yields `None`, a `delete` of a missing id yields `{"deleted": false, ...}` — all on the `default` signal. Route on the output value if a miss should branch.
- ORM instances are converted to plain dicts (`_orm_to_context_dict`) before entering the context, so downstream steps and `Response` mappings see JSON-shaped data.

---

## 3. Execution Path

```
HTTP request → workflow route (metadata.required_scope / required_group checked here)
    │
    ▼
_run_engine / _sse_generator (src/tuvl/core/api/manager.py)
    │  builds context["_db"] = WorkflowUoW(session, allowed_models, is_write, version_map)
    ▼
WorkflowEngine step loop → _run_model_op_step
    │  repo = uow[model_name.lower()]        ← allowlist + version pin enforced here
    ▼
BaseRepository.add/get/list/update/remove   ← flush + refresh only, no commit
    ▼
workflow completes → session.commit() + uow.commit_secondary()
```

Key properties:

- **One transaction per run.** Repository methods only `flush()`; the primary session commit happens once, in `_run_engine` (or `_sse_generator` for streaming), after the whole workflow succeeds. Any uncaught exception rolls back everything — a workflow's ModelOp writes are all-or-nothing.
- **Suspension commits.** A `HumanInTheLoop` suspension commits the session (to persist the instance row), so ModelOp writes made *before* the suspend point are durable even though the workflow has not finished.
- **Secondary datasources.** A model may declare `spec.datasource` pointing at a non-primary Postgres. `WorkflowUoW._session_for` lazily opens one session per secondary datasource (with the same tenant-isolation binding as the primary) and `commit_secondary()` / `rollback_secondary()` mirror the primary's outcome. Commits across datasources are sequential, not two-phase: a failure between the primary commit and a secondary commit can leave them divergent.
- **Repos are cached per run.** The first access to a model builds its `BaseRepository`; later steps in the same workflow reuse it (and its session).

---

## 4. Context Interpolation

`payload` and `filters` go through `WorkflowEngine._resolve_tpl_obj`, which resolves templates recursively through dicts and lists with one important rule:

- A string that is a **single** `{{ key }}` reference returns the **raw Python object** — including via dot-paths like `{{step_1.data.id}}`. This is what makes `payload: "{{candidate}}"` pass the whole candidate dict to `repo.add()` instead of its string repr. Unresolvable references return `None`.
- Any mixed string (`"score: {{score}}"`) stringifies its substitutions; absent keys render as `""`. No dot-paths in mixed strings.

```yaml
payload:
  candidate_id: "{{new_candidate.id}}"   # sole reference → raw value (UUID survives)
  summary: "Screened by {{agent_name}}"  # mixed string → stringified
  raw: "{{llm_result.parsed}}"           # sole reference → nested dict passes through
```

`record_id` uses the simpler `_tpl` helper: flat `{{key}}` substitution only (no dot-paths), absent keys become `""` — which then misses and yields `None`/`deleted: false` rather than erroring.

If `filters` resolves to a non-dict it is ignored (treated as `{}`); if `payload` resolves to a non-dict on `create`/`update`, the step routes `error`.

---

## 5. The Allowlist Boundary

The workflow's `context:` field is a security boundary, not documentation. `_extract_allowed_models` in `src/tuvl/core/api/manager.py` accepts three forms:

```yaml
context: Candidate                      # string
context: [Candidate, Job]               # list
context:                                # dict — the only form that can pin versions
  models:
    - name: Candidate
      version: v2
```

These names become `WorkflowUoW._allowed` (case-insensitive). Every repository access — from a ModelOp step or a custom Functional node using `context["_db"]` — passes through `WorkflowUoW.__getitem__`, which raises `PermissionError` for any model not in the list. The ModelOp runner wraps that into a `RuntimeError` (`cannot access model '<name>'`), which **fails the workflow and rolls back** — it does not take the `error` route. The same hard failure applies when `_db` is missing from the context entirely.

There is no per-step scope check inside the workflow. The Biscuit scope/group gate (`metadata.required_scope`, `metadata.required_group`) runs once, as a FastAPI dependency on the workflow's trigger route (`_build_route_deps`). Past that gate, what a run may touch is defined solely by the `context:` allowlist — declare it minimally.

---

## 6. Version Pinning

Multiple `ModelDefinition` files may declare the same model name with different `metadata.schema_version` values. The loader (`src/tuvl/core/models/loader.py`) records all of them in `MODEL_VERSION_REGISTRY`, but only **enabled** definitions produce a class in `MODEL_REGISTRY` — and each generated class is stamped with `__schema_version__`. A version's effective `enabled` value is the YAML flag (`enabled: true` is the default) unless an admin toggle (`PATCH /admin/models/{name}/{version}/toggle`) overrides it — the DB flag wins and is applied at the next boot (`apply_db_enabled_overrides` in `src/tuvl/core/models/model_version.py`).

Two enforcement points protect against version mix-ups:

1. **Load time — cross-version collision.** Two *enabled* versions of the same model sharing a physical table but declaring different field shapes raise `RuntimeError` at registration ("Cross-version model collision"). Resolve by separate `spec.tablename`s, disabling one version, or aligning the fields.
2. **Run time — the pin check.** When a workflow pins a version (`context.models: [{name: Candidate, version: v2}]`), `WorkflowUoW.__getitem__` compares the pin against the live class's `__schema_version__` on first access. A mismatch — including the case where the pinned version's definition is disabled — raises `RuntimeError` and refuses to execute against the wrong schema.

The pin map is also injected into the execution context as `_context_model_versions` so custom Functional nodes can honor it. Versions are toggled at runtime via `POST /admin/models/{name}/{version}/toggle` (state persists in the `tuvl_system_model_versions` table); a toggle takes effect for `MODEL_REGISTRY` on reload.

Unpinned workflows simply get whatever version is currently enabled in `MODEL_REGISTRY` — pin when that ambiguity matters.

---

## 7. ModelOp vs the REST CRUD Routes

Every registered model also gets five auto-generated routes under `/models/{model}/` (`src/tuvl/core/api/crud_router.py`). Both surfaces call the same `BaseRepository`; the differences are around it:

| | `kind: ModelOp` (in a workflow) | REST `/models/{model}/` |
|---|---|---|
| Auth | workflow's `metadata.required_scope` / `required_group`, checked once at the trigger route | per-route Biscuit scopes: `<model>:read` / `:write` / `:delete` (overridable via `spec.access.*_scope` in the model YAML) |
| Access boundary | `context:` allowlist (`PermissionError`) | scope check only — any model, given the scope |
| Transaction | one commit for the whole workflow run; rollback on failure undoes all steps | commit per request, immediately after the operation |
| Record id | any PK value from context (templated string) | `uuid.UUID` path parameter |
| Filters | `filters:` map in YAML | `?filter[field]=value` query params |
| Pagination | `limit` only | `limit` + `offset` |
| Validation | payload passed raw to the model constructor | Pydantic create/read/update schemas from `SCHEMA_REGISTRY` |
| Version pinning | `context.models[].version` | none — always the enabled version |
| Miss behavior | `None` / `deleted: false` on `default` signal | HTTP 404 |

Use REST when an external client owns the call; use ModelOp when persistence is one step of declared business logic — validation, agents, routing, and response shaping around it.

---

## 8. As an Autonomous Agent Tool

A ModelOp step can be declared as a tool for a `kind: Agent` step in `mode: autonomous` (`agent.tools[].ref` naming the step id). See [`autonomous-agent.md`](autonomous-agent.md) for the loop itself; the ModelOp-specific mechanics:

- **Parameter schema.** When the tool entry omits `parameters`, `_derive_parameters` in `src/tuvl/core/engine/agent_tools.py` derives one. For ModelOp it is a single free-form object property named `payload` ("Record fields for the operation."). Author the step's `payload: "{{payload}}"` so the LLM-provided object flows in via the sole-reference rule. Declare explicit `parameters` for a precise field contract.
- **Dispatch.** `_dispatch_tool` runs the referenced step against a shallow copy of the context with the LLM's arguments merged in. The copy shares the same `_db` handle, so the allowlist and the run's single transaction still apply — an agent cannot reach a model the workflow did not declare.
- **Results and errors.** The tool result fed back to the model is the public-context delta the step produced (its `output` key). An `error` signal returns `{"error": <_last_error>}` to the model — the loop continues, letting the agent react to, say, a unique-constraint violation.
- **Context writes.** The delta is merged back into the real workflow context only when the tool entry sets `writes_context: true`.
- Agent-invoked writes commit or roll back with the surrounding workflow run, exactly like directly-executed ModelOp steps.

---

## 9. Validation and Failure Modes

`tuvl validate` (`src/tuvl/cli/commands/validate.py`) checks ModelOp steps statically: a `model:` value that matches no known `ModelDefinition` produces a warning, and the generic route check flags `routes:` targets that name no step. The engine reads `model` from the step root — there is no nested `model_op:` block.

At runtime, failures split into two classes:

| Condition | Behavior |
|---|---|
| success (including read/list/update/delete misses) | signal `default`, result under `output` |
| unknown `operation` | signal `error`, `_last_error` set |
| `payload` not a dict (create/update) | signal `error`, `_last_error` set |
| repository/DB exception (constraint violation, bad column type, ...) | signal `error`, `_last_error` set |
| `_db` missing from context | `RuntimeError` — workflow fails, transaction rolls back |
| model not in `context:` allowlist | `RuntimeError` (wrapping `PermissionError`) — workflow fails, rolls back |
| version pin mismatch / pinned version disabled | `RuntimeError` from `WorkflowUoW` — workflow fails, rolls back |

`error`-signal failures are routable (`routes: {error: handle_it}`) and leave `_last_error` in the context for the handler; an unmapped `error` route ends the run on that signal. The `RuntimeError` class is deliberate: those are authoring mistakes, not runtime conditions, and they surface as HTTP 500 with a rolled-back transaction.

Every ModelOp execution is traced and logged like any other step — see [`observability.md`](observability.md); failures log `Model-op step failed` with `workflow`, `step`, and `error` fields.

---

## 10. Reading the Code

| Module | What lives there |
|---|---|
| `src/tuvl/core/engine/runner.py` | `_run_model_op_step` (the five operations, signals), `_resolve_tpl_obj` / `_tpl` (interpolation), `_orm_to_context_dict`, `_dispatch_tool` (agent-tool path) |
| `src/tuvl/core/repositories/uow.py` | `WorkflowUoW` — allowlist gate, version-pin check, per-datasource session routing, `commit_secondary` / `rollback_secondary` |
| `src/tuvl/core/repositories/base.py` | `BaseRepository` — `add` / `get` / `list` / `update` / `remove`, relation expansion, filter coercion |
| `src/tuvl/core/api/manager.py` | UoW construction from `context:` (`_extract_allowed_models`, `_extract_model_version_map`), commit/rollback in `_run_engine` and `_sse_generator`, `_build_route_deps` (workflow-level auth) |
| `src/tuvl/core/models/loader.py` | `MODEL_REGISTRY`, `__schema_version__` stamping, cross-version collision detection |
| `src/tuvl/core/models/model_version.py` | `tuvl_system_model_versions` persistence, `enabled` toggling |
| `src/tuvl/core/api/crud_router.py` | the parallel REST CRUD surface |
| `src/tuvl/core/engine/agent_tools.py` | `_derive_parameters` — the `payload` object schema for ModelOp tools |
| `src/tuvl/cli/commands/validate.py` | static checks for ModelOp steps |
| `tests/core/test_model_op_step.py` | execution-path coverage: all five operations, allowlist, templates, error routes |
