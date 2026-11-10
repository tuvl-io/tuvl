---
name: tuvl-plan
description: "Write and maintain a spec's task list (specs/<name>.tasks.yaml) from the checked spec; tuvl checks it and derives every status."
---
# The task list

**Run `tuvl generate tasks <spec>` first.** For an approved spec it derives the list with no model: one
task per new model, step and example, plus a manual task for each step that calls an external system.
It keeps the ids of tasks already there, refreshes their section hashes and marks tasks whose step or
example left the spec `obsolete`. You only add what it cannot derive (a `manual` task for a policy, a
contract or credentials, with its `status`), and you never retype ids or hashes.

tuvl checks the list (`tuvl lint spec`, P001–P005) and derives each task's status
(`tuvl list tasks`). Work only from a spec with **no blocking `lint spec` findings**.

```yaml
kind: TaskPlan
version: tuvl/v2
metadata: { name: refund-request, spec: specs/refund-request.md, spec_hash: <spec hash> }
spec:
  tasks:
    - id: T1
      title: Refund model
      type: model
      spec_ref: { anchor: data, hash: <section hash> }
      target: { model: Refund }
    - id: T2
      title: Load the order (tool db read)
      type: agent
      spec_ref: { anchor: flow-refund-request, hash: <section hash> }
      target: { workflow: refund_request, agent: load_order }
      depends_on: [T1]
    - id: T8
      title: "Example: a small refund is paid automatically"
      type: test
      spec_ref: { anchor: acceptance, hash: <section hash> }
      target: { workflow: refund_request, example: a small refund is paid automatically }
      depends_on: [T7]
    - id: T9
      title: Payments API credentials
      type: manual
      target: { workflow: refund_request, agent: pay_refund }
      status: todo
```

## What to write
- One task per **new** model in `# Data` (`target: {model}`), per step of every `## Flow:`
  including `T` steps (`target: {workflow, agent}`), and per `tuvl-example` (`type: test`,
  `target: {workflow, example: <example name>}`).
- For each `tool http` / `tool mcp` step, also a `manual` task for its credentials/access (same
  target). Only `manual` tasks set `status` (`todo | in_progress | done`).
- `spec_ref`: the section the task comes from — `data`, `flow-<flow-name-with-dashes>`, `acceptance` —
  with that section's hash. Take anchors and hashes from `tuvl lint spec --json` (MCP `spec_check`):
  `sections[].anchor/hash`, and the spec's `hash` for `metadata.spec_hash`. Never invent them.
- `depends_on`: models before the steps that use them, steps in flow order, examples after the steps
  they run through.
- **No estimates** (`effort`) and no typed statuses — P004 rejects them.

## Keeping it current
- After a spec change, `tuvl lint spec` reports P005 (a task's section changed) and P003 (a new
  step or example has no task). Add tasks for new steps; mark removed ones `obsolete: true` — never
  delete or renumber tasks. Refresh the `spec_ref` hashes you've reviewed.
- Then follow skill `tuvl-task-loop`: `tuvl list tasks --json` (MCP `spec_status`) gives the next
  unblocked task, derived from `lint spec`, `verify spec` and honest tests.
