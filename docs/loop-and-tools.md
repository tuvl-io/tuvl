# The `loop` engine and tools

A `loop` agent gives a model a set of **tools** and lets it work toward an outcome within hard
budgets. Tools are other agents, other workflows, or allow-listed MCP tools; their schemas are
derived from contracts, never hand-written.

```yaml
- id: resolve_ticket
  description: Resolve a support ticket using the available tools
  engine: loop
  inputs:  { ticket: Ticket }
  outputs: { resolution: str }
  budget:  { max_iterations: 8, max_tokens: 40000, max_tool_calls: 12, timeout: 2m }   # all required
  loop:
    model: default
    steering: artifact://support-steering@4     # text or a steering artifact
    skills:   [artifact://refund-policy@2]
    tools:
      - agent: lookup_order                     # off-spine agent in this workflow
      - workflow: issue_refund                  # another workflow as a child run
        approval: { when: "args.amount > 200", required_group: support_manager }
      - mcp: artifact://zendesk@3
        allow: [add_comment, set_status]        # required allow-list
        max_calls: 3
    supervisor: { rules: [...], judge: artifact://resolution-judge@1 }
  guardrails: { tools: [artifact://no-secrets-in-observations@1] }
  routes: { resolved: reply, escalate: human_queue, error: ops_alert,
            max_iterations: human_queue, budget_exceeded: human_queue,
            timeout: human_queue, aborted: ops_alert }
```

## Tool sources

| Source | Schema | Execution |
|---|---|---|
| `agent: <id>` | The agent's `inputs` → parameters, `description` → tool description | The runner executes that agent with the model's arguments; its own `routes` are ignored |
| `workflow: <name>` | The workflow's `trigger.input` and description | A **child run** with its own journal, gates and budget; its tokens count toward the parent's `max_tokens`; nesting capped by `policy.max_depth` (default 2) |
| `mcp: artifact://x` | The server's `tools/list`, filtered by `allow` | Through the artifact's connection; each allowed tool's schema hash is pinned in `tuvl.lock` |

The model's arguments are validated against the tool's schema before execution; invalid arguments
return to the model as an error observation (`tool_args_invalid`) and count as an iteration. Tool
results stay inside the loop — only the declared `outputs` reach the run context.

## Per-tool governance

| Field | Meaning |
|---|---|
| `approval` | `required`, or `{ when: <TEL over args>, required_group? }` — the loop pauses at that call |
| `max_calls` | Per-tool cap, on top of `budget.max_tool_calls` |

Each tool's **effect** is derived (the agent's, the child workflow's strongest, or the MCP hints) and
shown to the model and in Insight. Write/destructive MCP calls are at-least-once; V015 asks for an
approval or `compensate`.

### In-loop approval

1. The model requests a call that matches `approval`.
2. The runner journals the full loop state and the pending call, and the run becomes
   `waiting_approval`.
3. An approver (`approvals:decide`, plus the tool's `required_group`) sees the exact arguments and
   **approves**, **edits** them (re-validated) or **rejects** with a message:
   `POST /api/approvals/{id}` `{ decision: approve | edit | reject, edited_args?, message? }`.
4. The run is re-queued; approve/edit executes the call, reject returns the rejection to the model.

## Outcome

The final turn must return the `outputs` schema plus `outcome: enum[<normal route keys>]`; the
outcome is the signal. A bad parse gets one repair attempt, then `error` (`parse_error`). Budgets end
the loop with `max_iterations`, `budget_exceeded` or `timeout`; the supervisor or an operator abort
ends it with `aborted`.

## Supervisor

`spec.supervisor` applies to every loop agent unless the agent sets `loop.supervisor`.

```yaml
supervisor:
  rules:
    - { when: tool_repeated, count: 3 }               # same call, same args, N times (optional tool:)
    - { when: budget_fraction, gt: 0.8 }              # a budget more than 80 % used
    - { when: iteration_reached, gte: 6 }
    - { when: effect_count, effect: write, count: 3 } # more than N write calls
  judge: artifact://resolution-judge@1                 # evaluates the trajectory
  every_n_iterations: 1
  on_violation: pause                                  # abort | pause | steer
  on_judge_error: ignore                               # ignore | pause | abort
  steer_message: Stop repeating lookups; decide with what you have.
```

The supervisor reads the journal and writes control flags on the run record, so it works across
workers. A `pause` waits for an operator (`POST /api/runs/{id}/resume`); `steer` injects a message
(operators can also steer with `POST /api/runs/{id}/steer`).

## Guardrails

`guardrails.tools` lists `guardrail`/`judge` artifacts applied to tool observations before the model
sees them; `guardrails.output` checks the final result (`guardrail_violation`).
