---
name: agent-tool
description: "Implement a tool agent: db (model operations), http (external API), storage (blob put/get/list/presign) or mcp (one MCP tool), with effects, idempotency and compensation."
---
# Tool agents (no Python)

```yaml
- id: persist
  engine: tool
  inputs: { subject: str, body: str }
  outputs: { ticket: Ticket }          # db results land in the single output
  tool:
    use: db
    model: Ticket
    op: create                         # read | list | create | update | upsert | delete
    values: { subject: "{{ subject }}", body: "{{ body }}" }
  compensate: discard_ticket           # off-spine agent that undoes it on a rollback end
  routes: { default: END, conflict: END.failed, error: END.failed }
```
- `where` values are converted to the column's type (a uuid string matches a `uuid` column); type
  the inputs that feed them to match the model (`order_id: uuid`).
- `read`/`delete` require a `not_found` route; `create`/`update`/`upsert` a `conflict` route.
- http: `tool: { use: http, method: POST, url: "${ENV:default}", body: {...}, map: { out: response.body.x }, idempotency: auto }`.
  POST/PUT/PATCH/DELETE are writes unless you declare `effect: read` on the agent. Route `http_4xx`
  signals as needed; outages go to `error` — route them somewhere safe.
- storage: `tool: { use: storage, store: invoices, op: put, key: "{{ id }}.pdf", body: "{{ pdf_b64 }}", encoding: base64 }`
  on a store declared in `storage/*.yaml` (`kind: Storage`: `s3`, `azure`, `gcs` or `local`; secrets as
  `${ENV}`, never literal). `op`: put, get, head, list, copy, delete, presign. The result is the single
  output (typed `json`); `get`/`head`/`copy` emit `not_found`, `put`/`copy` emit `conflict` unless
  `if_exists: overwrite`, so route them. `delete` is destructive; a `readonly` store refuses writes (V031);
  keys must be relative with no `..` (V032). Object bytes are never journaled. A missing store is V030.
  To read an uploaded file (a `file` input, skill `tuvl-workflow`), name the upload's store and pass its key:
  `tool: { use: storage, store: inbox, op: get, key: "{{ invoice.key }}", as: base64 }` (`as`: text | json | base64).
- mcp: `tool: { use: mcp, server: artifact://fetch-web@1, name: fetch, args: { url: "{{ url }}" } }`;
  the server is a `type: mcp` artifact. Pin it in `tuvl generate lock`. A worker keeps one session per tenant and server, so a
  stdio server is started once, not per call: do not keep per-call state in the server, and expect an artifact or environment change
  to open a new session (idle ones close after `TUVL_MCP_IDLE_SECONDS`, default 300).
- Templates use TEL over the agent's inputs only. Run `tuvl lint project` — it type-checks every flow.
- Tests: `db` tools run offline against the test database; `http`/`mcp`/`storage` must be mocked.
