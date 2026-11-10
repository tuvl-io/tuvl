---
name: tuvl-task-loop
description: "The core loop for implementing a tuvl project: pick the next task from spec status, implement, validate, test, repeat. Use for every implementation task."
---
# The task loop

Repeat until `tuvl list tasks --strict` exits 0:

1. **Status** — `tuvl list tasks --json` (mcp `spec_status`). Each task has `status`
   (`todo → in_progress → implemented → tested → done`, or `untracked`), `blocked`, `stale` and
   `reasons`. Never edit a status; it is derived from the project.
2. **Pick** the first task that is not `done`, not `blocked`, not `obsolete`. Everything blocked by
   the spec? Fix the spec first (`tuvl lint spec`, skill `tuvl-spec`). `stale`? Update the task list
   (`tuvl-plan`).
3. **Build** what the task targets exactly as the spec line says: the model from `# Data`; the
   workflow's trigger, ends and the step's agent — id, engine, inputs, outputs, routes, budget,
   group/expiry. Leave a step you can't finish yet as `pending`. Then `get_contract(workflow,
   agent)` (mcp) for the generated class names.
4. **Generate** — `tuvl generate code` (mcp `codegen_write`) after any contract change.
5. **Implement** with the skill for the agent's engine. A `pending` agent becomes implemented by
   choosing its engine and writing its block (only `code` needs Python).
6. **Validate** — `tuvl lint project --json`; fix every error. Unknown code? `explain_error`.
   **Verify** — `tuvl verify spec --json` (mcp `spec_verify`); fix every R error on your task — the
   build must match the spec, or the spec must change first.
7. **Test** — `tuvl run tests --json`. Add or fix tests (skill `tuvl-test`; seed with `given:`/`as:`, mock
   only model/external/human steps) until — with no STUBBED result — the task's workflow
   has a passing test that goes through the agent.
8. **Lock** — `tuvl generate lock` when models, artifacts, judges or MCP tools changed.
9. Back to 1. Reasons tell you what is missing ("pending agents: …", "no passing Test covers …",
   "tuvl.lock is out of date").

Stop and ask a person for `manual` tasks (credentials, external accounts) — never fake them.
