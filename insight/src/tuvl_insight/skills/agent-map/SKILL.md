---
name: agent-map
description: "Implement a map agent: run a child workflow once per item of a list, with concurrency, a size cap and fail or collect error handling."
---
# Map agents (no Python)

```yaml
- id: score_each
  engine: map
  inputs:  { candidates: "list[json]", job_id: uuid }
  outputs: { scores: "list[json]" }       # exactly one list: the results, in input order
  map:
    over: candidates                      # an input that is a list
    workflow: score_candidate             # the child workflow, run once per item
    as: candidate                         # the child's trigger input that receives the item (default: item)
    with: { job_id: "{{ job_id }}" }      # other child inputs, templates over this agent's inputs
    concurrency: 4                        # 1-32, default 4
    max_items: 100                        # 1-1000, default 100; a longer list fails before any item starts
    on_error: collect                     # fail (default) | collect
  effect: write                           # never weaker than the child's strongest effect (V015)
  routes: { default: rank, partial: review, error: END.failed }
```
- Each item is a child run with its own journal and cost. The result of an item is the `body` of the
  end its child finished on (status below 400); a failed item is `null` under `collect`.
- `fail` stops at the first failed item and emits `error`. `collect` runs every item and emits `partial`
  instead of `default` when at least one failed and the workflow routes `partial`; `partial` is only
  valid with `collect` (V004).
- The child must finish on its own: no `human` or `pending` agent, no loop tool that needs approval (V036).
  It exists, is not this workflow, has the `as` input and every `with` key, and gets every required
  input (V035). Item and result types are checked (V007).
- `tuvl lint project` prices it at the child's worst case times `max_items`. Bound `max_items` as tightly as the
  data allows.
- It is one workflow over many items, not a fork/join of different branches. Do not use it to run two
  different things in parallel (that is the `parallel` engine, skill `agent-parallel`).
- Tests: the child's agents are mocked in the test of the parent workflow (`mocks:` by agent id), not in
  a separate test.
