# tuvl project — rules for coding agents

This is a **tuvl 2.0** project: typed YAML workflows of agents, run by a durable, journaled runtime.
Work spec-first and let tuvl tell you the state of the project.

## Two ways you may be started
- **You were pointed at the project** (open path): follow the rules and skills below, and end green.
- **tuvl started you** (driven path: `tuvl generate spec`, `tuvl generate code --spec`): you have one narrow job on a
  branch, in a folder tuvl made. Do exactly that job and nothing else: tuvl reverts any file outside it,
  puts back the spec's `id`/`ideas`/`status` fields, never lets you approve, and checks the result with
  `lint spec` / `validate` / `verify spec`. If it says what is wrong, fix exactly that.

## Building a UI on this backend
Start with skill `tuvl-ui`. Read `ui/specs/README.md` first, then the file of the workflow or model you are building a screen for. Use
`@tuvl/client` (not hand-written `fetch`) for runs, events, approvals, CRUD and sign-in, call only endpoints that
are documented there, and handle every status listed. Regenerate with `tuvl generate ui-spec` after the backend changes;
write your own notes only below the notes marker. A browser app on another origin works only if the backend lists its origin in
`spec.api.cors_origins` (or you share one origin through a proxy); `ui/specs/README.md` says which applies.

## Non-negotiable
1. **Never infer project state — ask tuvl.** Use `tuvl lint spec|verify|status --json`,
   `tuvl lint project --json`, `tuvl run tests --json` or the `tuvl serve mcp` tools (`spec_check`, `spec_verify`,
   `spec_status`, `validate`, `get_contract`, …).
2. **Specs drive the work.** Intent lives in `specs/<name>.md`, in the grammar `tuvl lint spec`
   checks. You write the task list (`specs/<name>.tasks.yaml`, skill `tuvl-plan`) and build what it
   names; `tuvl verify spec` says whether the build matches the spec. Never set a task's status — it
   is derived.
3. **Never hand-edit generated files**: `agents/_generated/`, tests whose `metadata.generated_from`
   is set, `tuvl.lock`. Regenerate with `tuvl generate code` / `tuvl generate lock`.
4. **Only `code` agents need Python**, and only the function body. The decorator, signature and
   docstring belong to `tuvl generate code`.
5. **Never invent fields, engines or kinds.** Engines: `pending code tool decide llm loop human map`.
   Every document is `kind:` + `version:` + `metadata:` + `spec:`; workflows are `version: tuvl/v2`.
6. **Every change ends green**: `tuvl lint spec && tuvl lint project --strict && tuvl generate code --check && tuvl verify lock
   && tuvl run tests --strict && tuvl verify spec --strict && tuvl list tasks --strict && tuvl review code --strict` (the CI gate).
7. PII fields are `secure: true` and never reach a model (V020). Secrets live in `.env`, never in YAML.

## Skills (`.agents/skills/`)
| Skill | Use it when |
|---|---|
| `tuvl-spec` | writing or changing `specs/<name>.md` |
| `tuvl-plan` | writing and maintaining the task list from a checked spec |
| `tuvl-task-loop` | **always** — the core loop for implementing tasks |
| `agent-code` / `agent-tool` / `agent-llm` / `agent-loop` / `agent-decide` / `agent-human` / `agent-map` / `agent-parallel` | implementing an agent with that engine |
| `tuvl-workflow` | the workflow around the agents: triggers (http, schedule, mcp), file uploads, ends, policy, webhooks, per-signal outputs |
| `tuvl-data` | models, fields, access, CRUD |
| `tuvl-test` | tests, mocks, fixtures, judges |
| `tuvl-review` | reviewing the Python of code agents (the C-rules, then judgement) |
| `tuvl-harden` | changing a working workflow safely |
| `tuvl-ship` | lock, CI gate, shipping (`tuvl ship` installs only the tuvl extras the project uses) |
| `tuvl-ui` | **always for a UI task** — building the user interface with `@tuvl/client`: specs, setup, same-origin proxy, one screen per workflow |
| `ui-run` / `ui-approve` / `ui-auth` / `ui-data` | UI screens that start and follow runs, answer people and approvals, sign in, and show model data, uploads and webhooks |
