---
name: agent-loop
description: "Implement a loop agent: a model that investigates with tools, under budgets, approvals and a supervisor."
---
# loop agents

```yaml
- id: investigate
  engine: loop
  inputs: { customer_id: str }
  outputs: { resolution: str }
  budget: { max_iterations: 6, max_tokens: 20000, max_tool_calls: 4, timeout: 3m }
  loop:
    model: default
    steering: artifact://investigator-steering@2   # persistent instructions
    skills: [artifact://red-flags@1]
    tools:
      - agent: lookup_account                      # another agent in this workflow (off-spine)
      - workflow: issue_credit                     # a child run
        approval: { when: "args.amount > 50", required_group: team_leads }
        max_calls: 1
      - collection: kb_docs                        # search a collection (read-only; skill tuvl-data)
        top_k: 3
      - mcp: artifact://fetch-web@1
        allow: [fetch]                             # explicit allow-list, always
    supervisor:
      rules: [{ when: tool_repeated, count: 3, then: abort }]
      judge: artifact://supervisor-judge@1         # optional, calibrated (skill tuvl-test)
      on_judge_error: abort                        # fail closed
  routes: { resolved: persist, needs_human: review, max_iterations: review, budget_exceeded: review,
            timeout: review, aborted: review, error: END.failed }
```
- The model finishes by choosing an outcome (a route signal) with the outputs; required routes:
  `max_iterations`, `budget_exceeded`, `timeout`, `aborted` (+ `guardrail_violation`).
- Tool arguments are validated before execution; writes behind `approval:` pause the run for a
  person (approve / edit / reject). Human and loop agents cannot be tools (V014).
- A `collection:` tool is offered to the model as `search_<collection>(query)` (or the `name:` you give it) and returns
  `{ passages: [{ content, metadata, score }] }`, the same hybrid search as `tuvl.data_search`. `top_k` (default 5), `filter`
  (metadata the passages must contain) and `scope: global|run` are fixed in the YAML, not chosen by the model. It is a read: no `approval`
  needed. The collection must exist (V014), declared in a `CollectionRegistry` / `CollectionConfig` with its embedding.
- Keep tools small and typed; give each a precise `description` — it is what the model reads.
- To remember across runs, add `loop.memory: { collection: support_memory, key: principal, window: 5, recall_top_k: 3, max_tokens: 1000, write: [ticket, resolution], retention: 90d }`:
  the owner's earlier turns open the conversation as data, and the `write` fields are stored only when the loop finishes with a result (not on a failure,
  a wait for approval or a budget stop). Same owner rules as `llm.memory`: `principal`, `tenant` or an input (refused on a public trigger, V043).
