---
name: ui-approve
description: "Build the screens where a person answers a run that waits: human forms and tool-call approvals with @tuvl/client (approvals.list, decide)."
---
# People in the loop

A run waits when a `human` agent asks for a form (`waiting_human`) or a tool call needs approval (`waiting_approval`). The UI shows what is
asked and sends the answer; the backend decides who may answer.

```ts
const open = await client.approvals.list();                          // only what this token may decide
const approval = open.find((a) => a.run_id === runId);
```
`Approval`: `approval_id`, `run_id`, `agent_id`, `kind` (`human` | `tool` | `pause`), `title`, `instruction`, `show`, `expires_at`,
`required_group`, and either `form` (human) or `tool` + `args` + `reason` (tool call).

## Human form
Render `approval.form` as inputs: `{ [field]: { type, required, label?, widget?, help? } }`. Show `title`, `instruction` and the `show` data the
person needs to decide. Send values keyed by field name:
```ts
await client.approvals.decide(approval.approval_id, { values: { approved: true, note } });
```
The spec file of the workflow (`ui/specs/<name>.md`, "Waiting for a person") lists each step's form fields, widgets and deciding group: build
the form from it, and let the live `form` object confirm it.

## Tool call
Show the tool name, `args` and `reason`; offer the choices the agent allows:
```ts
await client.approvals.decide(approval.approval_id, { decision: "approve" });
await client.approvals.decide(approval.approval_id, { decision: "edit", edited_args: { amount: 10 } });
await client.approvals.decide(approval.approval_id, { decision: "reject" });
```

## After deciding
Keep following the run (`handle.events()` or `handle.wait()`, skill `ui-run`); the run continues by itself. Do not start it again.
```ts
const handle = client.run(approval.run_id);
const output = await handle.wait();
```

## Who may decide
- The token needs scope `approvals:decide` and the agent's `required_group`. When a group is required, the person who started the run is not allowed to
  decide it (separation of duties): a `403` is expected, not a bug. Show a no-access state.
- A decision the server refuses (already decided, expired, not yours to decide) rejects `TuvlApiError` with the server's status and message: show it and refresh the list.
- `approvals.list()` is per token: build an inbox screen from it and refresh it on `approval_requested` events or when the screen gets focus.
- `kind: "pause"` is not a form: a loop agent suspended itself and gives a `reason`. Show it; `handle.resume()` (scope `runs:control`) continues the run.
