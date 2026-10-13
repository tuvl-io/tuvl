# API: REST, SSE, MCP and the client

REST + Server-Sent Events are the application transports. MCP export is an additional, opt-in
surface per workflow. (gRPC was removed in 2.0.)

## REST

| Endpoint | Purpose |
|---|---|
| `<trigger.http.path>` | The workflow's own route: the end's `status`/`body`, or `202` + run descriptor |
| `POST /api/workflows/{name}/runs` `{ input }` | Start any workflow by name (`?wait=false` returns `202` immediately) |
| `GET /api/runs` | Recent runs (`?workflow=`, `?status=`, `?limit=`) |
| `GET /api/runs/{id}` | Status, current agent, end, output, error, tokens, pending approval |
| `GET /api/runs/{id}/events` | **SSE**: journal replay from `?from_seq=`, then live; `?follow=false` stops after the replay |
| `GET /api/runs/{id}/export` | The run's journal as a replayable fixture |
| `POST /api/runs/{id}/pause\|resume\|abort\|steer` | Operator control (`runs:control`); steer takes `{ message }` |
| `GET /api/approvals` | Open approvals the caller may decide |
| `POST /api/approvals/{id}` | Decide: `{ values }` for a human form, `{ decision: approve\|edit\|reject, edited_args?, message? }` for a tool call |
| `POST /api/runs/{id}/human/{agent_id}` | Submit a human agent's form (alias) |
| `/models/{model}/…` | CRUD for models that opt in with `spec.api.crud: [list, read, create, update, delete]` |
| `/auth/*`, `/admin/*` | Login, IAM, federation; engine admin |
| `/api/artifacts` | Artifact list/upload |
| `/health` | Liveness and decision-model readiness |
| `/openapi.json`, `/docs` | OpenAPI generated from the contracts |

A run descriptor (`202` body, `GET /api/runs/{id}`):

```json
{ "run_id": "…", "workflow": "refund_flow", "status": "waiting_human", "trigger": "http",
  "current_agent": "approval", "end": null, "output": null, "error": null, "tokens_used": 412,
  "status_url": "/api/runs/…", "events_url": "/api/runs/…/events",
  "approval": { "approval_id": "…", "kind": "human", "title": "Refund 123", "form": { … }, "decide_url": "/api/approvals/…" } }
```

### SSE events

```
id: 7
event: decision
data: {"run_id":"…","seq":7,"ts":"…","type":"decision","agent_id":"triage","engine":"decide","tokens":null,"latency_ms":3,"payload":{"decision":"auto_refund","source":"rule#3"}}
```

Event types are listed in [runtime](runtime.md#journal-events). Reconnect with `Last-Event-ID` (or
`?from_seq=`) to resume without gaps.

### OpenAPI from contracts

Each trigger operation is generated from the workflow:

- path and query parameters, or the JSON body, from `trigger.input`;
- one response per end status, typed by `outputs.<end>.type` or inferred from the body template
  (`"{{ ticket.id }}"` takes the declared type of `ticket`'s `id`);
- `202` with the run descriptor, and `x-tuvl-workflow: <name>` for client generators.

## MCP export

```yaml
spec:
  trigger:
    mcp: { enabled: true }        # also needs metadata.description (V019)
```

- Workflows with `trigger.mcp.enabled` are tools on **`/mcp`** (streamable HTTP, stateless).
  Tool name = workflow name, description = `metadata.description`, input schema = `trigger.input`.
- Annotations are derived: `readOnlyHint` when every effect is read, `destructiveHint` when any is
  destructive.
- Auth is the REST auth: a Biscuit bearer token with **`mcp:connect`**, and each tool keeps its
  workflow's scope/group gates. `tools/list` shows only what the caller may run.
- A call runs the workflow and sends a progress notification per started agent. The result's
  `structuredContent` is an envelope MCP clients can validate:
  - `{ outcome: "completed", run_id, end, status, body }` — `body` is one of the 2xx end bodies;
  - `{ outcome: "pending", run_id, state, current_agent, approval? }` — the run waits on a person or
    outlived the call window (120 s); follow it over REST.
  An end with status ≥ 400, a failure or an abort is an `isError` result.

Export never replaces the REST route. For coding agents *building* a project, see `tuvl mcp`
([spec-driven workflow](spec-driven.md#coding-agents)) — a different, dev-only server.

## `@tuvl/client` (TypeScript)

```bash
npm install @tuvl/client
npx tuvl-client generate --url http://localhost:8885 --out src/tuvl.generated.ts
```

```ts
import { RunHandle, TuvlClient } from "@tuvl/client";
import type { Workflows } from "./tuvl.generated";

const client = new TuvlClient<Workflows>({ baseUrl: "http://localhost:8885", token });
const result = await client.execute("refund_flow", { order_id, reason: "damaged", message });
if (result instanceof RunHandle) {
  for await (const event of result.events()) console.log(event.type, event.agent_id);
  const output = await result.wait();
}
```

`execute` resolves the end body or a `RunHandle` (202); error ends reject with `TuvlRunError`,
refusals with `TuvlApiError`. Also: `start`, `runs.{list,get,events,control,export}`,
`approvals.{list,decide}`, `crud(model)`, and `TuvlAuth` for `/auth/*`.
