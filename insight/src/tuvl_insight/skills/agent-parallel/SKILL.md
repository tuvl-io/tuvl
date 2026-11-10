---
name: agent-parallel
description: "Implement a parallel agent: several different read-only lookups (db read, http GET, storage read, llm, read-only code) at the same time, joined into one step."
---
# Parallel agents (no Python unless a branch is `code`)

```yaml
- id: gather
  engine: parallel
  inputs:  { customer_id: uuid, name: str }
  outputs: { profile: json, hit: bool }       # exactly the branches' outputs together, no name twice
  parallel:
    on_error: fail                            # fail (default) | collect
    concurrency: 3                            # optional, 1-16; default: all branches at once
    branches:                                 # 2-16
      - id: crm
        engine: tool
        inputs:  { customer_id: uuid }        # taken from the step's inputs
        outputs: { profile: json }
        tool: { use: db, model: Customer, op: read, where: { id: "{{ customer_id }}" } }
      - id: sanctions
        engine: tool
        inputs:  { name: str }
        outputs: { hit: bool }
        tool: { use: http, method: GET, url: "https://screening.example/check?q={{ name }}", map: { hit: response.body.hit } }
  budget: { timeout: 30s }                    # for the whole step; it cancels every branch
  routes: { default: decide, error: END.failed }   # + partial: review with on_error: collect
```
- Use it when the lookups are independent and the next step needs all of them: the step takes as long as the slowest
  branch. If they depend on each other, use separate steps. For the same workflow over a list, use `map`.
- Branches only read (V040): `tool` db `read`/`list`, http `GET`/`HEAD`, storage `get`/`head`/`list`/`presign` GET, `llm`,
  and `code` with `effect: read`. No writes, no MCP tools, no `loop`, `human`, `decide`, `map` or nested `parallel`.
  Put writes in a step of their own after the join.
- A branch has no `routes` of its own and ends on `default`; any failure is a branch failure. `fail` stops the others at the
  first failure and emits `error` (`parallel_branch_failed`). `collect` runs all of them, leaves a failed branch's outputs
  `null` (declare them optional: `profile: "json?"`) and emits `partial` when it is routed. `partial` needs `collect` (V004).
- Branch inputs must be inputs of the step with a compatible type, and the step's outputs must be exactly the branches'
  outputs (V041). `transaction: per_run` is refused (V042): each branch has its own database session.
- `tuvl lint project` prices the step at the sum of the branches' `max_tokens`. Give an `llm` branch its own `budget`.
- Tests: mock every branch by id (`mocks: { crm: { outputs: { profile: {...} } }, sanctions: { outputs: { hit: false } } }`);
  or mock the step itself (`mocks: { gather: { outputs: {...} } }`). An unmocked branch fails the offline test and is named.
- A `code` branch needs its Python function like any code agent (`tuvl generate code` writes the stub).
