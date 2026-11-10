---
name: tuvl-workflow
description: "Write the workflow document around the agents: triggers (http, schedule, mcp, file uploads), ends, policy, webhooks and per-signal outputs."
---
# The workflow document

```yaml
kind: Workflow
version: tuvl/v2
metadata: { name: intake, description: Take in an invoice, required_scope: invoices:write }   # also required_group, group, tags
spec:
  models: [Invoice]                    # the only models any agent here may touch
  trigger: { ... }                     # below
  start: read_invoice                  # default: the first agent
  transaction: per_agent               # or per_run: one transaction for the whole run
  agents: [ ... ]
  outputs: { ... }                     # the ends
  policy: { ... }
  webhooks: [ ... ]
```

## Triggers (`spec.trigger`)
```yaml
trigger:
  http: { path: /api/invoices, method: POST, public: false, sync_timeout: 30s, enabled: true }
  schedule: { cron: "0 6 * * *", enabled: true, input: { region: eu } }   # UTC, 5 fields; "0 6 * * *" alone is shorthand
  mcp: { enabled: true }               # also expose the workflow as an MCP tool
  input: { order_id: uuid, note: "str?" }
```
- `input` is the typed payload. Every trigger fills it: the HTTP body or query, the MCP arguments, `schedule.input`.
- In production every way to start a run needs a verified token. `public: true` is the only anonymous route and cannot be combined with
  `required_scope` / `required_group`.
- A scheduled run starts with `schedule.input` (checked against `trigger.input`, V019): a required field with no value there can never run.
  `enabled: false` keeps the settings without mounting the route or the schedule. In `multi_tenant` mode `tenants: [..]` makes one run per tenant.
- Cron fires once per slot across all workers. A scheduled run cannot carry a file (V019): a workflow that is both uploaded to and scheduled
  needs its `file` inputs optional (`file?`), or a separate scheduled workflow.

## File uploads
```yaml
trigger:
  http:
    path: /api/invoices
    upload: { store: inbox, prefix: in/, max_bytes: 5242880, max_files: 3, accept: [application/pdf, image/*] }
  input: { invoice: file, attachments: "list[file]?", note: "str?" }
```
- The route takes `multipart/form-data`; each `file` / `list[file]` input is written to the named `kind: Storage` store under a generated key.
  Declare the store first (skill `agent-tool`); POST and PUT/PATCH only (no body on GET or DELETE).
- The run receives a **reference**, never the bytes: `{ store, key, name, size, content_type, sha256 }`. Use `{{ invoice.key }}` in expressions,
  a `storage` tool with `key: "{{ invoice.key }}"` to read it, or `await ctx.read_file(inp.invoice)` in a code agent. A child workflow or a human form
  may receive it too.
- A JSON body or query cannot name a file (422): the only way to get a reference is to upload. Limits answer 413 (size), 400 (count), 415 (type).
- V037: `upload` with no `file` input is an error, and so are an unknown or read-only store, a `file` nested deeper than `file` / `list[file]`, and a `file`
  input on a workflow exported over MCP; a `file` input on a route with no `upload`, or a public upload, only warns.
- Files of a refused request are deleted; files of a started run stay: put a lifecycle rule on the prefix.

## Ends (`spec.outputs`)
```yaml
outputs:
  default: { status: 201, body: { id: "{{ invoice.id }}" } }
  rejected: { status: 422, body: { error: "{{ reason }}" } }
  failed: { status: 500, rollback: true }          # rollback: compensate the finished agents in reverse order
```
- A route `END` means `END.default`; `END.rejected` names an end. Every end is a name under `outputs`.
- `body` templates read agent outputs; `type:` declares the response fields when they cannot be inferred. Status is 100-599.
- If the run has not finished within `sync_timeout`, the route answers `202` with a run descriptor (poll `GET /api/runs/{id}` or follow `.../events`).

## Per-signal outputs
When different signals produce different fields, key `outputs` by signal. The agent's `outputs` is then the union (fields not produced on
every signal are optional downstream):
```yaml
- id: screen
  engine: code
  routes: { clear: pay, flagged: review, error: END.failed }
  outputs:
    clear: { score: int }
    flagged: { score: int, reason: str }
```
A normal signal that is not listed produces no outputs, and reserved signals (`error`, `timeout`, `parse_error`, ...) never do. The data-flow
check uses the fields of the signal that actually reaches each agent.

## Policy (`spec.policy`)
`max_tokens_per_run` (checked against the worst-case cost), `max_bounded_agents`, `require_human_before: [write, destructive]` (an effect that needs a
person first, V020), `max_depth` (child workflow nesting, default 2), `allow_secure_to_llm` (default false), `model_only_decisions: warn|error|allow`.

## Webhooks (`spec.webhooks`)
```yaml
webhooks:
  - name: billing
    url: ${BILLING_WEBHOOK_URL}              # an ${ENV} reference, or an https:// literal
    secret: ${BILLING_WEBHOOK_SECRET}        # always an ${ENV} reference, at least 32 bytes; never the value
    events: [completed, failed]              # completed | failed | aborted | waiting
    stages: [approve_invoice]                # agent ids: notify each time one finishes
```
- Subscribe to `events` and/or `stages`; a stage must name an agent of this workflow (V038 error), and `allow_private` warns.
- A notification holds only the run id and what happened; the receiver pulls the run with its own token. Signed with HMAC-SHA256 and a timestamp; `previous_secret: ${OLD}` lets both sign while you rotate.
- https only; internal addresses are refused when declared and again when sent (`allow_private: true` opts in inside your own network).
- **Off unless `TUVL_WEBHOOKS_ENABLED=true`** in the server's environment (set it in the chart values / deployment, not in YAML).
- The supported receiver is `@tuvl/client` (`verifyWebhook`, `webhookHandler`). `GET /api/runs/{id}/webhooks` lists deliveries; `.../redeliver` retries.

## Check it
`tuvl lint project` (contracts, data flow, routes, policy, uploads V037, webhooks V038, schedule input V019), `tuvl generate code` after any contract change, and
`tuvl run tests` with a `Test` for each end. `tuvl generate ui-spec` documents the trigger (including the multipart body) for UI builders (skill `tuvl-ui`).
