# Runtime and journal

tuvl 2.0 runs every workflow on a **durable runner**: one deterministic loop that walks the graph,
invokes engines and records everything. There is no master process — any worker can continue any
run, because run state lives in Postgres, not in worker memory.

## Storage

| Table (`tuvl_system_` prefix) | Contents |
|---|---|
| `runs` | id, workflow name + content hash, status, trigger (`http`, `api`, `schedule`, `mcp`, `child`, `dev`), principal, tenant, parent run, current agent, lease owner + expiry, tokens used, output, error |
| `run_events` | The **journal**: append-only `(run_id, seq)` events with type, agent, payload, tokens and latency |
| `run_checkpoints` | The context after each completed agent — the resume state |
| `approvals` | Open human forms and tool-call approvals: run, agent, kind, payload, required group, expiry |

Statuses: `queued`, `running`, `waiting_human`, `waiting_approval`, `paused`, `completed`, `failed`,
`aborted`, `compensated` (`compensating` while compensators run).

## Lifecycle

1. **Trigger** — HTTP route, `POST /api/workflows/{name}/runs`, schedule, MCP or a parent loop. The
   principal is verified, the input validated against `trigger.input`, and a run is queued.
2. **Sync path** (HTTP): the receiving process claims the run and executes it inline. If it ends
   within `trigger.http.sync_timeout` (default 30 s) the response is the end's `status`/`body`;
   otherwise `202` with `{ run_id, status_url, events_url, … }`, and the run keeps going.
3. **Async path:** workers claim queued runs with `SELECT … FOR UPDATE SKIP LOCKED` and hold a
   heartbeat-renewed **lease**.
4. **Per agent:** `agent_started` → engine → output validation → `agent_completed` + signal →
   checkpoint → route. The step's database writes commit in the same transaction as its checkpoint.
5. **Pause** (human form, tool approval, breakpoint, supervisor or operator): checkpoint, set the
   status, release the lease. A decision or resume re-queues the run; any worker continues from the
   last checkpoint.
6. **End:** `run_completed` with the end name and response; compensation if the end has
   `rollback: true`.

### Roles

`tuvl run --role all | api | worker` (Helm: `split: true`):

- `all` serves HTTP and executes the queue (default);
- `api` serves HTTP and only executes synchronous requests inline;
- `worker` executes the queue (and serves `/health`).

`--concurrency` bounds runs per process.

## Crash recovery

An expired lease returns a run to `queued`; the next claimer resumes from the last checkpoint and
re-executes the in-flight agent. Per-agent semantics are **at-least-once**, made safe by:

- `tool: db` on the primary datasource — writes commit with the checkpoint (exactly-once);
- `tool: http` — `idempotency: auto` sends a stable `Idempotency-Key`;
- `code` — `ctx.idempotency_key`, and `ctx.db` writes commit with the checkpoint;
- MCP and other external calls — at-least-once; give write/destructive ones a `compensate` (V015).

A human agent's idempotency key counts finished steps, so it is stable across suspend and resume.

## Transactions

| `spec.transaction` | Semantics | Constraints |
|---|---|---|
| `per_agent` (default) | Each agent's writes commit with its checkpoint. Cross-agent atomicity comes from compensation | — |
| `per_run` | One transaction for the whole run, each step in a savepoint; committed with the run's end, rolled back on failure. No checkpoints — a crash reruns the run | No `human`, approvals or async triggers; sync only (V021) |

## Compensation

Reaching an end with `rollback: true`, an unhandled `error`, or an abort runs the `compensate` agent
of every **completed** agent that declares one, in **reverse completion order**. Each compensator
receives the original agent's inputs and outputs. A failing compensator makes the run `failed`
(`compensation_failed`); the rest still run. Success ends as `compensated`.

## Journal events

Every event: `{ run_id, seq, ts, type, agent_id, engine, tokens, latency_ms, payload }`.

`run_queued` · `run_started` · `agent_started` · `agent_completed` · `signal` · `llm_turn` ·
`tool_call` · `tool_result` · `decision` · `judge_verdict` · `approval_requested` ·
`approval_decided` · `paused` · `resumed` · `checkpoint` · `compensation_started` ·
`compensation_completed` · `run_completed` · `run_failed` · `run_aborted`

Read them with `GET /api/runs/{id}/events` (SSE: replay, then live), `tuvl runs tail`, the Insight
Runs page, or `RunHandle.events()` in `@tuvl/client`. Checkpoints keep real values (they are the
resume state); secure fields are redacted on export.

## Replay and fixtures

The journal is the recording. **Replay** re-executes a run's graph but serves `llm`, `loop`, decide
model, `tool` and judge results from recorded events. `tuvl runs pin <run_id>` (or 📌 in Insight)
exports a run to `tests/fixtures/<workflow>/<name>.journal.json`; a `Test` with `replay:` then runs
with zero tokens, and `tuvl runs replay <fixture>` fails if the end or output differ. Insight's
**Change engine** check replays fixtures with only the changed agent live.

## Operator control

`POST /api/runs/{id}/pause | resume | abort | steer` (scope `runs:control`). Control is read by the
runner between agents and loop iterations, under the run's lock, so it works across workers. In dev
mode, Insight breakpoints and step mode pause runs through the same mechanism.

## Scheduler

`trigger.schedule` (cron, UTC) enqueues one run per slot across all workers, guarded by a Postgres
advisory lock.
