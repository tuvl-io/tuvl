# API: REST, SSE, MCP and the client

REST + Server-Sent Events are the application transports. MCP export is an additional, opt-in
surface per workflow. (gRPC was removed in 2.0.)

## REST

| Endpoint | Purpose |
|---|---|
| `<trigger.http.path>` | The workflow's own route: the end's `status`/`body`, or `202` + run descriptor |
| `POST /api/workflows/{name}/runs` `{ input }` | Start any workflow by name (`?wait=false` returns `202` immediately). For a workflow with `http.upload`, send `multipart/form-data` instead: one part per input |
| `GET /api/runs` | Recent runs (`?workflow=`, `?status=`, `?limit=`) |
| `GET /api/runs/{id}` | Status, current agent, end, output, error, tokens, pending approval |
| `GET /api/runs/{id}/events` | **SSE**: journal replay from `?from_seq=`, then live; `?follow=false` stops after the replay |
| `GET /api/runs/{id}/export` | The run's journal as a replayable fixture |
| | A workflow that names a disabled or undefined model is not started: `409 {"error": "workflow_unavailable", "reasons": [...]}` (the same for MCP calls and child runs). With `multi_tenant` on, a run belongs to the caller's tenant and other tenants' runs answer `404` |
| `POST /api/runs/{id}/pause\|resume\|abort\|steer` | Operator control (`runs:control`); steer takes `{ message }` |
| `GET /api/approvals` | Open approvals the caller may decide |
| `POST /api/approvals/{id}` | Decide: `{ values }` for a human form, `{ decision: approve\|edit\|reject, edited_args?, message? }` for a tool call |
| `POST /api/runs/{id}/human/{agent_id}` | Submit a human agent's form (alias) |
| `/models/{model}/…` | CRUD for models that opt in with `spec.api.crud: [list, read, create, update, delete]` |
| `/auth/*`, `/admin/*` | Login; IAM administration and federation admin (the `iam` group) and engine admin (`admin`), both off in production unless you expose them |
| `/api/artifacts` | Artifact list/upload (off in production unless you expose it) |
| `/health` | Liveness and decision-model readiness |
| `/openapi.json`, `/docs`, `/redoc` | OpenAPI generated from the contracts; off in production unless you turn it on |

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

A new event reaches the stream as soon as it commits when the run is written by the same process that serves the stream. Writes from
another worker process are picked up by a poll once a second, so allow up to a second there. An idle stream costs about one query batch
a second.

With `?drafts=true`, while an agent is writing its answer the stream also carries two events that are **not journaled** and have no `id` (without it the stream is exactly the journal, so existing clients are unaffected):

```
event: token_delta
data: {"run_id":"…","agent_id":"draft_email","delta":"Hello","replay":false}

event: token_reset
data: {"run_id":"…","agent_id":"draft_email"}
```

`token_delta` adds `delta` to that agent's draft. `token_reset` means the agent is starting its answer again (a retry, or another worker after a
crash): discard the draft. A client that connects while an agent is running first receives the draft so far as one `token_delta` with
`replay: true`; it does not get drafts of agents that already finished, and an ended run replays only its journal. Drafts are not kept, exported or
replayed, and the final result is in the journal and the run output as always, so a client that missed deltas loses nothing. They need the
same `runs:read` access as the rest of the stream.

### OpenAPI from contracts

Each trigger operation is generated from the workflow:

- path and query parameters, or the JSON body, from `trigger.input`;
- one response per end status, typed by `outputs.<end>.type` or inferred from the body template
  (`"{{ ticket.id }}"` takes the declared type of `ticket`'s `id`);
- `202` with the run descriptor, and `x-tuvl-workflow: <name>` for client generators.

### Which routes a server exposes

Fewer routes are fewer ways in, so a production server mounts only what it needs, and the API
documentation lists only your own workflows. Both are set in `.tuvl/system.yaml` and can be
overridden by the environment (the environment wins):

```yaml
kind: SystemConfig
spec:
  api:
    expose: [auth]         # system route groups to mount in production (runs is always mounted)
    docs: workflows        # off | workflows | all
    dev_expose_all: true   # false: the dev server mounts only what production would
```

| Group | Routes | Dev server | Production default |
|---|---|---|---|
| `auth` | `/auth/*`: token, refresh, logout, JWKS, `me`, OAuth login | mounted | mounted |
| `iam` | `/auth/bootstrap`, `/auth/admin/*`: users, roles, federation providers | mounted | **not mounted** (production IAM is `tuvl iam`) |
| `runs` | `/api/runs/*`, `/api/approvals/*`, `POST /api/workflows/{name}/runs` | mounted | **always mounted**; it cannot be switched off |
| `admin` | `/admin/*`: engine administration | mounted | **not mounted** |
| `artifacts` | `/api/artifacts/*` | mounted | **not mounted** |
| `system` | `/api/_system/*`: introspection | mounted | **not mounted** |

Your workflow trigger routes, and any `/models/*` CRUD you opted into, are never affected.
`TUVL_API_EXPOSE=auth,runs,iam,admin` overrides `spec.api.expose`. Add `iam`, `admin`, `artifacts` or `system`
to a production server only when something there calls them: the TypeScript client and the CLI use
`auth` and `runs` (and your workflow routes) only, and `system` is read by Insight alone. `runs` is
not a choice: every client starts runs through it and every run is followed through it.

The dev server mounts every group, because Insight needs them. Set `spec.api.dev_expose_all: false`
(`TUVL_DEV_EXPOSE_ALL=false`, or the switch in Insight's Settings → API Access) and it mounts what a
production server would, with the same documentation default, so you can see that surface; the parts
of Insight that need the other groups then stop working until you turn it back on.

`spec.api.docs` (`TUVL_API_DOCS`) decides what `/docs`, `/redoc` and `/openapi.json` show:
`workflows` lists only your workflows and models (no `/auth`, `/admin`, `/api/_system`, `/health`,
`/dev`); `all` adds the system routes; `off` serves none of the three paths. The dev server defaults
to `workflows`, production to `off`. `tuvl lint project` checks both settings.

## Calling from a browser

A browser lets a page call only its own origin unless the server says otherwise. By default tuvl sends no CORS headers, so a UI served from another origin (a Vite dev server, a CDN, another domain) is blocked.
Either serve the UI and the backend from one origin, with a reverse proxy for `/api`, `/auth` and `/models`, or list the UI's origin:

```yaml
kind: SystemConfig
spec:
  api:
    cors_origins: [https://app.example.com, http://localhost:5173]
```

`TUVL_CORS_ORIGINS=https://app.example.com,http://localhost:5173` overrides it (the environment wins; set to an empty string it turns the setting off).

- An origin is `scheme://host[:port]` exactly as the browser sends it: no path, no trailing slash. `tuvl lint project` rejects anything else; the server leaves a bad entry out, so a mistake narrows access.
- Nothing is allowed by default, in dev too: a web page you open could otherwise call your dev server. Add `http://localhost:5173` yourself if you want to skip the proxy.
- `*` allows every website; the API still needs a token, but `tuvl lint project` warns and the server logs a warning. Prefer a list.
- Allowed: methods `GET POST PUT PATCH DELETE`, request headers `Authorization`, `Content-Type`, `Accept`, `Last-Event-ID`. Credentials mode is off: tokens travel in the `Authorization` header, never in cookies. The response header `X-Tuvl-Run-Id` is exposed, because `@tuvl/client` reads it.
- It is not a login. CORS only decides which pages the browser lets read a response; scopes and groups still decide who may do what.
- The setting is read at boot: restart after changing it. The IP allowlist (`TUVL_ALLOWED_HOSTS`) is a separate gate on the client's address.

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
- Auth is the REST auth: a bearer token (JWT) with **`mcp:connect`**, and each tool keeps its
  workflow's scope/group gates. `tools/list` shows only what the caller may run.
- A call runs the workflow and sends a progress notification per started agent. The result's
  `structuredContent` is an envelope MCP clients can validate:
  - `{ outcome: "completed", run_id, end, status, body }` — `body` is one of the 2xx end bodies;
  - `{ outcome: "pending", run_id, state, current_agent, approval? }` — the run waits on a person or
    outlived the call window (120 s); follow it over REST.
  An end with status ≥ 400, a failure or an abort is an `isError` result.

Export never replaces the REST route. For coding agents *building* a project, see `tuvl serve mcp`
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

In a browser on another origin the backend must allow that origin first ([Calling from a browser](#calling-from-a-browser)); a blocked call rejects with the browser's own `TypeError` ("Failed to fetch"), not a `TuvlApiError`.
