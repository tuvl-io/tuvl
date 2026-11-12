# CLI reference (every command and option)

This page is generated from the command line itself (`python scripts/gen_cli_reference.py`), so it lists every
command and every option that exists. [CLI reference](cli.md) says what each command is for.

- `-d/--project-dir` defaults to the current directory on every command that works on a project.
- `-u/--url` (`TUVL_URL`) and `-t/--token` (`TUVL_TOKEN`) are how the commands that talk to a running server find it.
- Options written `--x/--no-x` are on/off switches; the one shown in **Default** is on unless you switch it.
- Commands marked *toolchain* need the developer toolchain (`uv tool install "tuvl[standard]"`).


## Commands without a verb


### `tuvl init` *(toolchain)*

Scaffold a new tuvl project: its folders, a database connection, AI model presets and the coding-agent rules and skills; --sample adds a working example.

Arguments: `[name]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY |  | Alias for the NAME argument — path to scaffold the project into. |
| `--sample` |  |  | A 2.0 sample: ticket triage with a spec, task plan, tests and lock that pass the CI gate. |
| `--multi-tenant` |  |  | Enable multi-tenancy. Requires a PostgreSQL datasource to be configured at init time. The mode is locked into the database on first boot and cannot be changed without re-initialising. |
| `--no-ai-skills` |  |  | Skip scaffolding the .agents/ directory containing AI agent rules and skills. |
| `--preset` | TEXT |  | LLM preset: gemini, openai, anthropic or ollama (current model ids; the key is read from the environment, else left blank in .env). |
| `--yes`, `-y` |  |  | Accept every default without prompting (CI, coding agents). |
| `--decision-model` | TEXT |  | Decision model for `decide` agents: laya (a self-hosted laya-serve container, added to `tuvl ship`) or none. |
| `--agent` | TEXT |  | Coding agent tuvl may run for you (see --list-agents), or `none`. Asked when omitted. |
| `--no-agent` |  |  | Same as --agent none. |
| `--list-agents` |  |  | Show the coding agents found on this machine and exit. |
| `--initials` | TEXT |  | Your initials (2-4 letters/digits); they stamp the ids of ideas and specs you create. |
| `--git/--no-git` |  | on | Run `git init` on the project when git is installed. |
| `--branch` | TEXT | `develop` | The initial git branch. |
| `--commit` |  |  | Make an initial commit. |
| `--ci` | TEXT | `none` | Write the deterministic gate as CI: github (.github/workflows/tuvl.yml), gitlab (.gitlab-ci.yml) or none. |
| `--dev-telemetry` | TEXT |  | https address of your team's telemetry collector (optional; tuvl set dev-telemetry changes it later). |


### `tuvl dev` *(toolchain)*

Run the engine in development mode: hot reload, the Insight editor at /insight and the terminal UI.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--port`, `-p` | INTEGER | `8885` | Port to bind the engine on. |
| `--project-dir`, `-d` | DIRECTORY |  | Path to the tuvl project directory (contains models/, workflows/, etc.). Defaults to current directory. |
| `--no-browser` |  |  | Skip opening the UI in a browser. |
| `--allow-host`, `-H` | TEXT (repeatable) |  | Allow remote connections from this IP or CIDR (e.g. --allow-host 192.168.1.0/24). Can be repeated. Localhost is always permitted. |
| `--show-key` |  |  | Print the generated dev API key to the terminal. Off by default so the key is not captured by shell history, recording tools, or shared screens. The key is always persisted to <project-dir>/.tuvl/.dev-session (mode 0600). |
| `--auto-login` |  |  | Automatically bypass the security screen in Tuvl Insight. |
| `--no-tui` |  |  | Run the engine in this terminal as before, without the terminal UI that starts on a terminal. |


### `tuvl ship` *(toolchain)*

Package the project for production — validate, containerize, and emit a Helm chart.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY | `.` | Path to the tuvl project directory (contains config.yaml, models/, workflows/, etc.). |
| `--tag`, `-t` | TEXT |  | Image reference to build. Defaults to <project>:<version>. |
| `--no-build`, `--no-image` |  |  | Write the Dockerfile and Helm chart but skip the docker build. |
| `--format` | TEXT | `image` | image (a docker image and a Helm chart), archive (the runtime files as a project you run with uv) or both. |
| `--archive-format` | TEXT | `tar.gz` | tar.gz or zip. |
| `--out` | PATH | `dist` | Where the archive is written. |
| `--allow-incomplete` |  |  | Ship although some spec tasks are not done. |
| `--allow-dirty` |  |  | Ship although the working tree has uncommitted changes. |
| `--push` |  |  | Push the image after a successful build. |
| `--force` |  |  | Overwrite existing generated files (deploy/, .dockerignore). |
| `--strict` |  |  | Treat validation warnings as errors. |
| `--extra` | TEXT (repeatable) |  | Also install this tuvl extra (mcp, redis, vector) — for a feature the project turns on in a way `tuvl ship` cannot see. Repeatable. |
| `--with-laya/--without-laya` |  |  | Add a Laya decision server (laya-serve) to the Helm chart. Default: on when an AgentModel uses provider: laya. |


### `tuvl deploy` *(toolchain)*

Deploy this project to a tuvl cloud with a client token (cloud.tuvl.io by default).

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY | `.` | Path to the tuvl project. |
| `--archive` | FILE |  | Deploy this archive (made by `tuvl ship --format archive`) instead of packaging the project. |
| `--url` | TEXT |  | The cloud to deploy to (default: $TUVL_CLOUD_URL, then https://cloud.tuvl.io). |
| `--plan` | TEXT | `hobby` | The hosting plan for a new project (hobby or standard); its price is taken from your wallet. |
| `--hostname` | TEXT |  | The name to serve a new deployment on (default: <project>.<the cloud's base domain>). |
| `--set-hostname` | TEXT |  | Change the name the deployed app is served on, and nothing else: nothing is packaged or uploaded. The app restarts behind the new name; the old name stops working. |
| `--check-dns` |  |  | Check that a name is available and points at the cloud (the name of --hostname or --set-hostname, else the project's default name). Nothing is deployed. |
| `--project` | TEXT |  | The project to check or rename (default: the name in pyproject.toml). |
| `--admin-email` | TEXT |  | Create an administrator with this email in the new app; its password is made here, shown once, and kept nowhere. (`TUVL_ADMIN_EMAIL`) |
| `--no-admin` |  |  | Do not create an administrator in the app. |
| `--login` |  |  | Save a client token for this cloud (asked for with a hidden prompt) and exit. |
| `--logout` |  |  | Forget the saved token for this cloud and exit. |
| `--token-stdin` |  |  | Read the client token from standard input (for CI). It is never taken from an argument. |
| `--no-follow` |  |  | Start the deployment and return; it continues on the cloud. |
| `--allow-dirty` |  |  | Pass to `tuvl ship`: package although the working tree has uncommitted changes. |
| `--allow-incomplete` |  |  | Pass to `tuvl ship`: package although some spec tasks are not done. |
| `--strict` |  |  | Pass to `tuvl ship`: treat validation warnings as errors. |


## `tuvl create`


### `tuvl create spec`

Write specs/<name>.md from the template (numbered and linked to its idea with --idea).

Arguments: `<name>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--owner` | TEXT |  | Owning team |
| `--workflow` | TEXT |  | Workflow the examples run (default: from name) |
| `--idea` | TEXT (repeatable) |  | The idea(s) it comes from; numbers the spec SPEC-XX-NNNN-NN |


## `tuvl import`


### `tuvl import idea`

Copy an idea (files, a folder, or a snapshot of a URL) into idea/IDEA-<initials>-NNNN/ as a numbered, immutable record.

Arguments: `[paths]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--url` | TEXT (repeatable) |  | A web page, Jira issue, Confluence page or GitHub issue to snapshot (repeatable) |
| `--allow-private` |  |  | Let --url reach internal addresses (your own Jira or wiki) |
| `--title` | TEXT |  | Idea title (default: first heading) |
| `--supersedes` | TEXT |  | The idea this one replaces (IDEA-XX-NNNN) |
| `--initials` | TEXT |  | Your initials (saved on first use) |
| `--include` | TEXT (repeatable) |  | Only these globs |
| `--exclude` | TEXT (repeatable) |  | Skip these globs |
| `--max-size` | INTEGER | `10` | Largest file, in MB |
| `--dry-run` |  |  | Show what would be imported |
| `--commit` |  |  | Commit the new idea (needed before `generate spec` can use it) |
| `--json` |  |  | Machine-readable result |


## `tuvl use`


### `tuvl use agent`

Choose the coding agent tuvl may run for you, or `none`. Stored in `.tuvl/agent.yaml`.

Arguments: `<agent>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--force` |  |  | Select an agent that was not found on this machine |
| `--command` | TEXT |  | For `generic`: the command to run. It reads the prompt on stdin and edits the working directory, e.g. --command 'mytool --yes'. |


## `tuvl set`


### `tuvl set dev-telemetry`

Turn developer telemetry on, off or change it. Commit the file; your token stays in your own config folder.

Arguments: `[server]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--off` |  |  | Stop sending (the settings stay) |
| `--identity` | TEXT |  | What the events say about you: initials (default) or full (name and email) |
| `--token-env` | TEXT |  | Environment variable that overrides your registered token (default TUVL_DEV_TELEMETRY_TOKEN) |
| `--commands/--no-commands` |  |  | Also send one event per command run |
| `--forget` |  |  | Delete your registered token for this collector from this machine |
| `--rotate-token` |  |  | Get a new token from the collector; the old one stops working |
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |


### `tuvl set project`

Update one field. The description is Markdown.

Arguments: `<field>` `<value>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY | `.` | Project directory. |


## `tuvl upgrade`


### `tuvl upgrade project`

Create project.yaml for a project that predates it. Never changes an existing id.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY | `.` | Project directory. |
| `--name` | TEXT |  | Defaults to the directory name. |
| `--new-id` |  |  | Replace the id (copied project) and clear its registration. |


## `tuvl register`


### `tuvl register developer`

Register yourself at the team's collector and keep your token (it sends and reads for you).

| Option | Value | Default | What it does |
|---|---|---|---|
| `--name` | TEXT |  | Your name (default: your git name) |
| `--email` | TEXT |  | Your email (default: your git email) |
| `--code` | TEXT |  | The team's join code, from whoever runs the collector |
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |


### `tuvl register project`

List this project in the public catalogue. Optional, open, no sign-in.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY | `.` | Project directory. |
| `--nest` | TEXT |  | Registry URL (TUVL_NEST_URL). |
| `--remote` | TEXT | `origin` | The git remote that holds the project's repository. |
| `--no-verify` |  |  | List the project without pushing the one-time tag. |
| `--keep-tag` |  |  | Leave the one-time tag on the remote. |
| `--yes`, `-y` |  |  | Skip the confirmations. |


## `tuvl update`


### `tuvl update skills`

Refresh AGENTS.md and the skills to this tuvl version (removes the retired skills).

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--diff` |  |  | Show what would change; write nothing |


## `tuvl generate`


### `tuvl generate code`

Generate schemas, stubs, prompts and tests (zero tokens); with --spec, build a spec's models and workflows.

Arguments: `[workflow]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--check` |  |  | Write nothing; exit 1 if anything would change |
| `--spec` | TEXT |  | An approved spec (id, name or file): build its models and workflows from its tasks |
| `--task` | TEXT (repeatable) |  | Task(s) for the agent: 3, T3, 1,3 or a full id (repeatable) |
| `--all` |  |  | With --spec: every agent task not yet implemented |
| `--no-agent` |  |  | With --spec: only the deterministic part, no agent |
| `--agent` | TEXT |  | With --spec: the coding agent (default: yours) |
| `--max-attempts` | INTEGER | `3` | Tries per task |
| `--budget` | FLOAT | `1.0` | Most USD one attempt may spend (Claude Code) |
| `--timeout` | FLOAT | `900` | Seconds per attempt |
| `--model` | TEXT |  | Model, if the agent takes one |
| `--base` | TEXT |  | Branch to start from (default develop) |
| `--no-pr` |  |  | Commit on the branches; push nothing |
| `--ready` |  |  | Open pull requests ready for review, not as drafts |
| `--continue` |  |  | After a failed task go on with those independent of it |
| `--keep-worktree` |  |  | Leave the work folders in .tuvl/worktrees |
| `--consent` |  |  | Agree that spec and project files are sent to your agent |
| `--review` | TEXT | `static` | With --spec, review each task's Python before committing: none, static (the C-rules and ruff, free) or agent (static plus a second opinion from a coding agent) |
| `--review-blocking` | TEXT | `off` | Agent findings at least this severe fail the task: off, high, medium or low |
| `--reviewer` | TEXT |  | The reviewing agent (default: another one if you have it) |


### `tuvl generate key`

Generate a persistent Ed25519 private key for signing access tokens.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--write`, `-w` |  |  | Write TUVL_SIGNING_KEY into the project .env instead of only printing it. |
| `--force`, `-f` |  |  | Overwrite an existing TUVL_SIGNING_KEY in .env (use with --write). |
| `--project-dir`, `-d` | DIRECTORY | `.` | Project directory whose .env to update (with --write). |


### `tuvl generate lock`

Pin model ids, artifact hashes, judge/decision models and MCP schemas.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--check` |  |  | Write nothing; exit 1 if tuvl.lock is missing or stale |
| `--mcp/--no-mcp` |  | on | List MCP servers live to pin/verify tool schemas |


### `tuvl generate rls`

Print idempotent RLS-enable + tenant-isolation policy SQL for every tenant-scoped table.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY |  | Path to the tuvl project directory. Defaults to current directory. |
| `--out`, `-o` | PATH |  | Write the SQL to this file instead of stdout. |


### `tuvl generate spec`

A coding agent writes a draft spec from an idea, on a spec/<id> branch with a pull request.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--idea` | TEXT (repeatable) |  | Idea(s) to write the spec from (repeatable) |
| `--from` | PATH (repeatable) |  | Import this file/folder as a new idea (and commit it) first |
| `--title` | TEXT |  | Spec title (default: the idea's) |
| `--agent` | TEXT |  | Coding agent for this run (default: yours) |
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--max-attempts` | INTEGER | `3` | Tries before giving up |
| `--budget` | FLOAT | `1.0` | Most USD one attempt may spend (Claude Code) |
| `--timeout` | FLOAT | `600` | Seconds per attempt |
| `--model` | TEXT |  | Model, if the agent takes one |
| `--base` | TEXT |  | Branch to start from (default develop) |
| `--dry-run` |  |  | Show the plan and what would be sent; run nothing |
| `--show-prompt` |  |  | Print the exact prompt |
| `--yes`, `-y` |  |  | Do not ask before committing the draft |
| `--consent` |  |  | Agree that idea files are sent to your coding agent |
| `--no-pr` |  |  | Commit on the branch; push nothing |
| `--ready` |  |  | Open the pull request ready for review, not as a draft |
| `--keep-worktree` |  |  | Leave the work folder in .tuvl/worktrees |
| `--here` |  |  | Write the draft into this folder, uncommitted: no branch, no pull request |


### `tuvl generate tasks`

Derive the task list from an approved spec: one task per new model, step and example. Zero tokens.

Arguments: `<spec>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--dry-run` |  |  | Print the task list; needs no approval, writes nothing |


### `tuvl generate ui-spec`

Write ui/specs/: how to build the UI on this backend (zero tokens).

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--workflow`, `-w` | TEXT (repeatable) |  | Only this workflow (repeatable) |
| `--all` |  |  | Also write workflows whose tasks are not all done |
| `--no-tests` |  |  | Do not run the tests: implemented tasks count as done |
| `--base-url` | TEXT | `http://localhost:8885` | The backend URL shown in the snippets |
| `--check` |  |  | Write nothing; exit 1 if ui/specs/ is not what would be written |
| `--json` |  |  | Machine-readable result |


## `tuvl approve`


### `tuvl approve spec`

Approve a spec: record who, when and a hash of the body. Any later edit makes it a draft again.

Arguments: `<spec>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--force` |  |  | Approve although `lint spec` reports errors |


## `tuvl reject`


### `tuvl reject spec`

Send a spec back to draft with a reason (clears any approval).

Arguments: `<spec>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--reason` | TEXT |  | Why it goes back to draft |
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |


## `tuvl reopen`


### `tuvl reopen spec`

Withdraw an approval so the spec can be edited.

Arguments: `<spec>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |


## `tuvl renumber`


### `tuvl renumber idea`

Give an idea a new number (after a duplicate) and rewrite every spec and task id derived from it.

Arguments: `<idea>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--to` | TEXT |  | New idea id (default: the next free one) |
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |


## `tuvl lint`


### `tuvl lint project`

Validate a tuvl project — YAMLs, code agents, workflow contracts, and more.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY | `.` | Path to the tuvl project directory (contains config.yaml, models/, workflows/, etc.). |
| `--strict` |  |  | Treat warnings as errors (non-zero exit if any warnings). |
| `--json` |  |  | Print a machine-readable report (for CI and coding agents). |


### `tuvl lint spec`

Is the spec complete and consistent? Deterministic, zero tokens (S001–S021).

Arguments: `[paths]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--strict` |  |  | Warnings fail too |
| `--json` |  |  | Machine-readable report |


## `tuvl verify`


### `tuvl verify audit`

Check the ledger's hash chain, schema and order. Exit 1 if it was altered.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |


### `tuvl verify lock`

Is tuvl.lock up to date? Exits 1 if it is missing or stale.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--mcp/--no-mcp` |  | on | List MCP servers live to verify tool schemas |


### `tuvl verify rls`

Verify every tenant-scoped table has the ``tuvl_tenant_isolation`` RLS policy installed.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY |  | Path to the tuvl project directory. Defaults to current directory. |


### `tuvl verify spec`

Does the implementation match the spec, step by step? Zero tokens (R001–R010).

Arguments: `[paths]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--strict` |  |  | Unfinished work (pending, untested) fails too |
| `--no-tests` |  |  | Skip running tests (R009/R010 not checked) |
| `--json` |  |  | Machine-readable report |


### `tuvl verify ui-spec`

Is ui/specs/ what `tuvl generate ui-spec` would write now? Exits 1 if not.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--no-tests` |  |  | Do not run the tests: implemented counts as done |
| `--base-url` | TEXT |  | The backend URL in the snippets |
| `--json` |  |  | Machine-readable result |


## `tuvl check`


### `tuvl check agent`

Check the selected agent starts, then run it once on a trivial task in a scratch folder.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--yes`, `-y` |  |  | Do not ask before spending tokens |


### `tuvl check dev-telemetry`

Ask the collector who your token is; exits 1 when it does not accept it.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--flush` |  |  | Also send everything that is queued |
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |


### `tuvl check storage`

Write, read and delete a probe object in each store (read-only stores: list only).

Arguments: `[name]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY | `.` | Project directory. |
| `--json` |  |  | Machine-readable output. |


### `tuvl check system`

Check the project: configuration, models, storage, git and coding-agent files.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--live` |  |  | Also make one minimal call per model (spends a few tokens) |
| `--json` |  |  | Machine-readable report |
| `--only` | TEXT |  | Run only these check groups (comma-separated) |
| `--skip` | TEXT |  | Skip these check groups (comma-separated) |
| `--strict` |  |  | Exit 2 when there are warnings (and no failures) |
| `--fix` |  |  | Apply the safe fixes some checks offer, then re-check |
| `--quiet`, `-q` |  |  | Show only warnings and failures |
| `--verbose` |  |  | Show the detail of passing checks too |


## `tuvl review`


### `tuvl review code`

Check the Python of code agents against tuvl's rules (C001…) and ruff. Zero tokens.

Arguments: `[paths]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--base` | TEXT |  | Only files changed since this branch or commit |
| `--strict` |  |  | Warnings fail too |
| `--no-ruff` |  |  | Skip the ruff pass |
| `--agent` |  |  | Also ask a coding agent for a second opinion (spends tokens) |
| `--reviewer` | TEXT |  | The reviewing agent (implies --agent) |
| `--spec` | TEXT |  | Only the code of this spec's tasks (id, name or file); with --agent, also the spec the code is judged against |
| `--task` | TEXT (repeatable) |  | Only the code of these tasks: 3, T3, 1,3 or a full id (repeatable) |
| `--blocking` | TEXT | `off` | Agent findings at least this severe fail: off, high, medium, low |
| `--post` |  |  | With --agent: comment the review on this branch's pull request |
| `--consent` |  |  | Agree that the code is sent to the reviewing agent |
| `--budget` | FLOAT | `1.0` | Most USD the review may spend (Claude Code) |
| `--timeout` | FLOAT | `600` | Seconds for the review |
| `--explain` | TEXT |  | Explain a rule code (C001…) and exit |
| `--json` |  |  | Machine-readable report |


## `tuvl run`


### `tuvl run tests`

Run the project's tests (offline unless --live).

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--live` |  |  | Let unmocked model and tool agents call out |
| `--allow-uncertain` |  |  | An 'uncertain' judge verdict passes |
| `--strict` |  |  | Also fail on stubbed results and untrusted judge calibration (CI) |
| `--only`, `-k` | TEXT |  | Run tests whose name contains this |
| `--json` |  |  | Machine-readable report |


### `tuvl run workflow`

Start a run of a workflow on a running server and follow it. Exits 1 if it fails.

Arguments: `<name>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--payload`, `-p` | TEXT | `{}` | The workflow's input, as JSON |
| `--url`, `-u` | TEXT | `http://localhost:8885` | tuvl server base URL (`TUVL_URL`) |
| `--token` | TEXT |  | Access token (`TUVL_TOKEN`) |


## `tuvl serve`


### `tuvl serve app`

Start the engine in production mode (uvicorn).

| Option | Value | Default | What it does |
|---|---|---|---|
| `--port`, `-p` | INTEGER | `8885` | Port to bind the engine on. |
| `--host` | TEXT | `0.0.0.0` | Host interface to bind. |
| `--workers`, `-w` | INTEGER | `1` | Number of uvicorn workers. |
| `--project-dir`, `-d` | DIRECTORY |  | Path to the tuvl project directory (contains models/, workflows/, etc.). Defaults to current directory. |
| `--allow-host`, `-H` | TEXT (repeatable) |  | Restrict access to this IP or CIDR (e.g. --allow-host 10.0.0.0/8). Can be repeated. When set, all other IPs (except localhost) are rejected. |
| `--role` | TEXT | `all` | all: serve HTTP and execute queued runs; api: serve HTTP, executing only synchronous requests inline; worker: execute queued runs (HTTP still serves health). |
| `--concurrency`, `-c` | INTEGER RANGE | `8` | Runs each process executes at once. |
| `--graceful-timeout` | INTEGER RANGE | `20` | Seconds to let open requests finish after SIGTERM, before the runtime drains. |
| `--drain-timeout` | FLOAT RANGE | `20.0` | Seconds the worker waits for running runs at shutdown. A run still going is interrupted; its lease expires and another worker resumes it. |
| `--forwarded-allow-ips` | TEXT |  | Proxies (IPs or CIDRs, comma-separated) whose X-Forwarded-For is trusted, so --allow-host sees the real client. Behind an ingress set the ingress network; '*' trusts every sender and lets a client spoof its address. Default: 127.0.0.1. |
| `--limit-max-requests` | INTEGER RANGE |  | Restart each worker after this many requests (bounds slow memory growth). |


### `tuvl serve mcp`

Serve development tools to a coding agent over MCP (stdio).

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--allow-write` |  |  | Also offer codegen_write, run_workflow, pin_fixture |


## `tuvl follow`


### `tuvl follow run`

Follow a run's events until it ends. Exits 1 if it fails.

Arguments: `<run_id>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--url`, `-u` | TEXT | `http://localhost:8885` | tuvl server base URL (`TUVL_URL`) |
| `--token` | TEXT |  | Access token (`TUVL_TOKEN`) |


## `tuvl pin`


### `tuvl pin run`

Export a run's journal to tests/fixtures/<workflow>/<name>.journal.json for `replay:`.

Arguments: `<run_id>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--name`, `-n` | TEXT |  | Fixture name (default: the run id) |
| `--project-dir`, `-d` | PATH | `.` |  |
| `--url`, `-u` | TEXT | `http://localhost:8885` | tuvl server base URL (`TUVL_URL`) |
| `--token`, `-t` | TEXT |  | Bearer token (a run you started, or iam:admin) (`TUVL_TOKEN`) |


## `tuvl replay`


### `tuvl replay runs`

Re-run pinned journals through the local project; fail if the end or output differ.

Arguments: `<fixtures>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` |  |
| `--json` |  |  |  |


## `tuvl list`


### `tuvl list agents`

Show every known coding agent, whether it is installed, and which one is selected.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--json` |  |  | Machine-readable list |


### `tuvl list branches`

Where the branches of each spec's work stand: spec, scaffold and one per task, and what to merge next.

Arguments: `[spec_file]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--json` |  |  | Machine-readable report |


### `tuvl list ideas`

List the project's ideas and the specs generated from each.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--json` |  |  | Machine-readable list |


### `tuvl list objects`

List objects in a store.

Arguments: `<name>` `[prefix]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--limit`, `-n` | INTEGER RANGE | `100` |  |
| `--project-dir`, `-d` | DIRECTORY | `.` | Project directory. |


### `tuvl list runs`

Recent runs, newest first.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--workflow`, `-w` | TEXT |  | Only this workflow |
| `--status`, `-s` | TEXT |  | Only this status |
| `--limit`, `-n` | INTEGER | `20` |  |
| `--json` |  |  |  |
| `--url`, `-u` | TEXT | `http://localhost:8885` | tuvl server base URL (`TUVL_URL`) |
| `--token`, `-t` | TEXT |  | Bearer token (a run you started, or iam:admin) (`TUVL_TOKEN`) |


### `tuvl list specs`

List the specs with their id, status (approved / draft / stale) and approver.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--json` |  |  | Machine-readable list |


### `tuvl list tasks`

Derive every task's status from the project (validate, tests, lock).

Arguments: `[spec_file]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--strict` |  |  | Exit 1 unless every task is done (none blocked or stale) |
| `--no-tests` |  |  | Skip running tests (nothing reaches tested/done) |
| `--json` |  |  | Machine-readable status |
| `--watch`, `-w` |  |  | Keep refreshing, with live codegen progress, until Ctrl-C |
| `--interval` | FLOAT | `2.0` | Seconds between refreshes |


### `tuvl list team-activity`

The team's recent development actions, from the collector (what everyone on the project did).

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--actor` | TEXT |  | Only this developer (name or initials) |
| `--entity` | TEXT |  | Only events about this idea, spec or task |
| `--kind` | TEXT |  | Only this event kind, e.g. task.failed |
| `--limit`, `-n` | INTEGER | `30` | How many events |
| `--json` |  |  | Machine-readable events |


## `tuvl show`


### `tuvl show audit`

Show recorded events, oldest first.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--entity` | TEXT |  | An idea, spec or task id; an idea covers its specs and tasks |
| `--actor` | TEXT |  | Initials or name |
| `--kind` | TEXT |  | Event kind or prefix, e.g. spec. |
| `--since` | TEXT |  | 2d, 12h, 30m or a date |
| `--limit`, `-n` | INTEGER | `50` | Newest N events (0 = all) |
| `--follow`, `-f` |  |  | Keep printing new events |
| `--json` |  |  | Machine-readable events |


### `tuvl show dev-telemetry`

Show whether developer telemetry is on, where it goes, who you are there and what is waiting to be sent.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--json` |  |  | Machine-readable status |


### `tuvl show drift`

Show how each spec moved since its plan: stale tasks, uncovered sections.

Arguments: `[spec_file]`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--json` |  |  | Machine-readable diff |


### `tuvl show event`

Show one event in full, or every event about an idea, spec or task.

Arguments: `<ref>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |


### `tuvl show idea`

Show one idea: its files, their hashes and the specs generated from it.

Arguments: `<idea>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--json` |  |  | Machine-readable record |


### `tuvl show project`

Print the project record.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | DIRECTORY | `.` | Project directory. |
| `--json` |  |  | Machine-readable output. |


### `tuvl show run`

One run: status, end, output, cost (JSON).

Arguments: `<run_id>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--url`, `-u` | TEXT | `http://localhost:8885` | tuvl server base URL (`TUVL_URL`) |
| `--token`, `-t` | TEXT |  | Bearer token (a run you started, or iam:admin) (`TUVL_TOKEN`) |


### `tuvl show task`

Show one task in full: what it builds, what it waits for and the spec section it came from.

Arguments: `<task>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--spec` | TEXT |  | The spec (default: the task's own) |
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--json` |  |  | Machine-readable record |


## `tuvl export`


### `tuvl export audit`

Export the ledger (optionally one entity's history) as JSON or CSV.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--format` | TEXT | `json` | json or csv |
| `--output`, `-o` | PATH |  | Write here instead of stdout |
| `--entity` | TEXT |  |  |


## `tuvl prune`


### `tuvl prune memory`

Erase memories: all of one owner's (--key), or those older than an age (--older-than). Not both.

Arguments: `<collection>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--key`, `-k` | TEXT |  | Erase every memory of this owner. |
| `--older-than` | TEXT |  | Erase memories older than this (30d, 12h). |
| `--project-dir`, `-d` | DIRECTORY | `.` | Project directory. |


### `tuvl prune traces`

Delete trace files (prompts, agent output) older than N days. The ledger is kept.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--project-dir`, `-d` | PATH | `.` | tuvl project directory |
| `--older-than` | FLOAT | `30` | Days; the trace only, never the ledger |


## `tuvl iam`


### `tuvl iam check`

Is every scope this project enforces granted by an active role? Exits 1 if not.


### `tuvl iam export`

Write the live roles as a RoleSeed document for the next fresh database.

| Option | Value | Default | What it does |
|---|---|---|---|
| `--out`, `-o` | PATH |  | Write to this file instead of stdout. |


### `tuvl iam reset-admin`

Break-glass: make EMAIL an active administrator with a new password. Asks for no login.

Arguments: `<email>`


### `tuvl iam roles add-user`

Give a user a role.

Arguments: `<name>` `<email>`


### `tuvl iam roles create`

Create a role.

Arguments: `<name>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--description` | TEXT |  | What the role is for. |
| `--scope`, `-s` | TEXT (repeatable) |  | A scope to grant (repeatable). |


### `tuvl iam roles delete`

Delete a role (refused while it has members unless --force).

Arguments: `<name>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--force` |  |  | Delete even when users still hold the role. |
| `--yes`, `-y` |  |  | Do not ask to confirm. |


### `tuvl iam roles disable`

Disable a role: it keeps its members and scopes but grants nothing until enabled.

Arguments: `<name>`


### `tuvl iam roles enable`

Enable a role again.

Arguments: `<name>`


### `tuvl iam roles grant`

Grant a scope to a role.

Arguments: `<name>` `<scope>`


### `tuvl iam roles list`

List roles with their scopes and member counts.


### `tuvl iam roles remove-user`

Take a role away from a user (they are signed out).

Arguments: `<name>` `<email>`


### `tuvl iam roles revoke`

Take a scope away from a role (its holders are signed out).

Arguments: `<name>` `<scope>`


### `tuvl iam scopes list`

List the scopes this project enforces and what each guards.


### `tuvl iam users create`

Create a user. The password is prompted (or TUVL_IAM_NEW_PASSWORD).

Arguments: `<email>`

| Option | Value | Default | What it does |
|---|---|---|---|
| `--first-name` | TEXT |  |  |
| `--last-name` | TEXT |  |  |
| `--phone` | TEXT |  |  |
| `--role`, `-r` | TEXT (repeatable) |  | A role to give (repeatable). |


### `tuvl iam users disable`

Stop a user signing in (their sessions end).

Arguments: `<email>`


### `tuvl iam users enable`

Let a disabled user sign in again.

Arguments: `<email>`


### `tuvl iam users list`

List users with their roles.
