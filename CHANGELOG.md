# Changelog

All notable changes to **tuvl** are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [Semantic Versioning](https://semver.org) as of `1.0.0`.
Releases are cut by hand: `make bump VERSION=x.y.z`, update this file, tag `vX.Y.Z`.
Changes that have not shipped yet are listed under an `[Unreleased]` heading at the top; when there are none, the newest
release below is the current one.

---

## [Unreleased]

### Fixed

- **Google Cloud Storage with an HMAC key can write.** The S3 backend sent Google's create-only header (`x-goog-if-generation-match`) next to the
  Amazon-style headers HMAC requests were signed with, and Google refuses a request that mixes them, so every first write failed with
  `400 ExcessHeaderValues`. HMAC requests to Google are now signed with Google's own scheme (`GOOG4-HMAC-SHA256`), carry `x-goog-*` headers only,
  and presigned URLs use `X-Goog-*` query names. A second write of the same key is refused with `Conflict` as on every other provider.
  Checked against a real bucket: create-only write, metadata, get, list, copy, presigned GET and PUT.

### Changed

- Storage request names and signing labels are now in one place, `tuvl.storage.dialects` (Amazon, Google, Azure). `S3Backend` takes a `dialect`
  (default Amazon), so S3-compatible services keep working unchanged and a service with different names is one entry.

---

## [2.1.0-beta.3] — 2026-11-12

**`tuvl deploy` deploys to a tuvl cloud.** `tuvl` and `tuvl-insight` release together as `2.1.0b3`.
This is a beta. Install it by pinning both packages, **without** `--prerelease allow`:

```bash
uv tool install "tuvl==2.1.0b3" --with "tuvl-insight==2.1.0b3"      # the CLI and Insight
pip install "tuvl==2.1.0b3" "tuvl-insight==2.1.0b3"                  # or into an environment
pip install "tuvl==2.1.0b3"                                          # the production engine only
```

Upgrading from 2.1.0b2: nothing in your YAML changes. In a project created by `tuvl init`, change the `tuvl-insight` pin in the `dev` group to
`>=2.1.0b3` and run `uv lock --upgrade`.

### Added

- **`tuvl deploy` deploys to a tuvl cloud.** Until now the command only announced that hosting was coming. It packages the project with
  `tuvl ship --format archive` (or takes `--archive`), uploads it with a **client token**, follows the upload, build and deploy, and prints the address.
  The token comes from `TUVL_CLIENT_TOKEN`, standard input (`--token-stdin`) or a saved login (`tuvl deploy --login`, `--logout`); it is never an
  argument, never printed, and sent only over https (plain http only to `localhost`). The cloud is `https://cloud.tuvl.io` unless `--url` or
  `TUVL_CLOUD_URL` says otherwise. A project name nobody owns is claimed by deploying it; one owned by another customer is refused as "not available"
  without saying whether it exists; a wallet that cannot pay the plan (`--plan hobby|standard`) stops the deployment before anything is built.
- **`tuvl deploy --admin-email EMAIL` creates an administrator in a new app.** The password is made on your machine, sealed so only the cloud can read it,
  and shown once at the end (`--no-admin` skips it).
- **`tuvl deploy --check-dns [--hostname NAME] [--project P]`** deploys nothing. It says whether a name is available, already this project's, taken
  (used by another project, not saying which), outside the cloud's base domain or invalid, and for a usable name whether DNS resolves it to the cloud
  (`ok`, `missing`, `wrong`). A taken or unusable name exits 1; a record that does not resolve yet is only a warning.
- **`tuvl deploy --set-hostname NAME`** serves the deployed app under a new name without packaging or uploading anything: the app restarts on its
  running release behind the new name, the old name stops working, the database is untouched, and it costs no credits. It is refused before anything
  restarts when the name is taken, already this project's or outside the base domain, or when the project is not yours, not running, or stopped for an
  unpaid plan. A deploy to a name another project already serves is refused before the upload.

### Changed

- The `tuvl` help screen no longer lists `deploy` as "coming soon", and the `tuvl-ship` agent skill, the CLI reference and `docs/cli.md` describe
  the working command.
- Needs a tuvl cloud with the client gateway (cloud.tuvl.io, the `tuvl-app-host` platform). A cloud's operator makes client tokens in its console.
  Custom domains of your own (`app.acme.com`) are not supported yet.

---

## [2.1.0-beta.2] — 2026-11-11

**Role management over HTTP, open registration in the Nest, and a safer beta install.** `tuvl` and `tuvl-insight` release together as `2.1.0b2`.
This is a beta. Install it by pinning both packages, **without** `--prerelease allow`:

```bash
uv tool install "tuvl==2.1.0b2" --with "tuvl-insight==2.1.0b2"      # the CLI and Insight
pip install "tuvl==2.1.0b2" "tuvl-insight==2.1.0b2"                  # or into an environment
pip install "tuvl==2.1.0b2"                                          # the production engine only
```

`--prerelease allow` lets *every* dependency go to a pre-release. With 2.1.0b1 it resolved `httpx 1.0.dev6` (no `httpx.Limits`), `protobuf 7.37.0rc1`
and `wrapt 2.6.0.dev1`, and the engine failed to start (`AttributeError: module 'httpx' has no attribute 'Limits'`). Pinning the two tuvl packages
asks for pre-releases of those two only.

Upgrading from 2.1.0b1: nothing in your YAML changes. If you run a project created by `tuvl init` on b1, delete the line `prerelease = "allow"`
from its `[tool.uv]` table, add `"tuvl-insight>=2.1.0b2"` to the `dev` dependency group, and run `uv lock --upgrade`. Registering a project in the
Nest needs this release (the old device-login flow is gone).

### Fixed

- **The engine failed to start when a pre-release `httpx` was installed.** `httpx` is now capped below 1.0 (`httpx>=0.27.0,<1`).
- **`tuvl init` no longer writes `prerelease = "allow"`.** A new project pinned to a pre-release tuvl now carries `tuvl>=<version>` and, in the `dev`
  group, `tuvl[standard]>=<version>` plus `tuvl-insight>=<version>`: uv then allows pre-releases of those two packages only, and everything else
  resolves to its stable release.
- **Insight: the collapsed menu could not be expanded on a short window.** The icon rail kept its expand button below the icons, so in any window shorter than the rail (about 900 px, a typical laptop browser) the button was off screen, and the pages at the bottom (IAM, Federation, API Docs, Settings) were unreachable. The button now sits under the logo, the icons scroll, and the app height follows the visible viewport (`100dvh`), so a browser toolbar no longer covers the bottom of the page.

### Changed

- **Listing a project in the Nest is open and checked by a git tag, not by GitHub sign-in.** `tuvl register project` (and Insight → Settings → Project →
  Register) needs no account. It registers, then, after a prompt, pushes a one-time tag to the project's git remote with your own credentials and removes it
  again. The Nest answers with a label: **Verified** (it saw the tag on a public repository), **Push confirmed** (this tool pushed, the Nest could not see it:
  a private repository) or **Listed** (self-declared). The first registration returns an entry secret, kept in `~/.config/tuvl/nest.json` (`TUVL_NEST_ENTRY_SECRET`
  for CI), that authorises later updates. `--no-verify` lists without the push, `--remote` and `--keep-tag` adjust it. The device-login dev routes
  (`/dev/project/register/poll`) are gone. Developer toolchain only: nothing of it is in a production install. Needs the matching Nest (tuvl-io/hub).
- Insight → Settings → API Access lists the `roles` group.

### Added

- **Role management over HTTP: the `roles` API group.** List roles, find users, add or remove a member, disable or enable a role, from your own
  screen. It is a separate group from `iam`, **not mounted in production** until `roles` is in `spec.api.expose` / `TUVL_API_EXPOSE`, and needs the
  new scope `roles:manage` (`iam:admin` also passes). Creating roles, changing what a role grants, creating users and federation stay
  development-time (`RoleSeed`, `tuvl iam`) and have no route. A caller without `iam:admin` can only change roles within their own scopes, and never a role
  carrying `iam:admin` or `roles:manage`. See [auth](docs/auth.md#role-management-from-your-own-screen-roles).

---

## [2.1.0-beta.1] — 2026-11-10

**Production IAM, an environment manifest, and a thinner engine.** `tuvl` and `tuvl-insight` release together as `2.1.0b1`. This is a
beta: install it with `uv tool install --prerelease allow "tuvl[standard]"` (or pin `tuvl[standard]==2.1.0b1`).

Upgrading from 2.0.x: nothing in your YAML or in the CLI changes. Check the three items under *Breaking* below if you run a production
server that calls the REST IAM admin API, import tuvl's Python modules, or install `tuvl[codegen]`, `[test]`, `[tui]`, `[review]` or `[pdf]`.

### Added

- **`tuvl iam`**: manage users, roles and scopes from inside the deployment (`kubectl exec -it deploy/<app> -- tuvl iam roles list`).
  The administrator signs in, and the command works on the database directly. Roles: list, create, delete, enable, disable, grant,
  revoke, add-user, remove-user. Users: list, create, enable, disable. Also `scopes list`, `check` (is every scope the project
  enforces granted by an active role?), `reset-admin` (break-glass) and `export` (developer toolchain: live roles to a `RoleSeed`).
  See [Production IAM](docs/auth.md#production-iam).
- **`kind: RoleSeed`** and **`TUVL_BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` / `_PASSWORD_FILE`**: roles and the first administrator are created on
  the first boot of an empty database, once in its life (an `iam_seeded` marker in `tuvl_system_registry`), under an advisory lock.
  A database that already holds a user or a role is never touched.
- **Roles can be disabled.** A disabled role keeps its members and scopes but grants no scopes and adds no group to a token. Existing
  databases get the `is_active` column on boot.
- **The last administrator is protected.** A change that would leave no active administrator is refused, for the REST API as well as the CLI.
- **`kind: EnvConfig`**: a project declares its environment variables with a description, `secret`, `required` and `default`. `tuvl lint project`
  checks it (V045-V047), `tuvl ship` writes the Secret list, the chart `env:` values and `.env.example` from it, a production server refuses to
  start without a required variable, and `tuvl check system` reports one that is missing. See [environment](docs/environment.md).
- **Insight: Settings → Environment** lists the engine's, the project's and the referenced variables, saves values to `.env` (a secret's is
  write-only), and declares undeclared ones.

### Changed

- **The developer tooling moves into the `tuvl-insight` package.** `tuvl` is now the engine and the production CLI
  (160 modules instead of 264): it parses the YAML, talks to the database and serves the API. Everything that exists to
  *develop* a project is in `tuvl-insight`: the lint engine (`tuvl lint project`), spec and task tooling, code
  generation, `tuvl init` / `dev` / `ship`, the terminal UI, the offline test runner, the file-based dev API and Insight.
  It is still one `tuvl` command: on a bare install the toolchain commands answer with how to install them, and
  `tuvl[standard]` installs everything as before. See [Core and Insight](docs/core-and-insight.md).
- `tuvl` and `tuvl-insight` are released in lockstep and pin each other with `==`.
- `tuvl replay runs` is a toolchain command (it runs `tuvl.testing`); `tuvl pin run` stays in the production CLI.
- The engine no longer loads the lint engine: `import tuvl.runtime.runner` used to pull in all of `tuvl.validate`.

### Breaking

- **The REST IAM admin API is off in production by default.** `/auth/bootstrap` and `/auth/admin/*` move to a new `iam` API group that a
  production server does not mount by default. Login, refresh, logout, `me` and OAuth stay in `auth`. Use `tuvl iam`, or add `iam` to
  `spec.api.expose` / `TUVL_API_EXPOSE`. The dev server still mounts every group.
- Python imports of tooling modules moved from `tuvl.<name>` to `tuvl_insight.<name>`: `spec`, `specgen`, `taskrun`,
  `codegen`, `coding_agents`, `skills`, `review`, `audit`, `uispec`, `tui`, `devmcp`, `devtelemetry`, `testing`,
  `gitflow`, `gitutil`, `shipgate`, `shiparchive`, `validate`, `lock` (generation), `cli.commands.<toolchain command>`.
  These were never documented API. `tuvl.contract.facts` and `tuvl.contract.lockfile` hold what the engine needs from them.
- The extras `tuvl[codegen]`, `tuvl[test]`, `tuvl[tui]`, `tuvl[review]` and `tuvl[pdf]` are gone: their packages are
  dependencies of `tuvl-insight`. Use `tuvl[standard]`.

---

## [2.0.3] — 2026-11-05

A security patch release of `tuvl` and `tuvl-insight`: patched dependencies for every open Dependabot advisory. Dependency updates only, no changes to tuvl's own code.

### Security

- **Python dependencies move to patched releases.** The lockfile was behind advisories published for `PyJWT`, `starlette`,
  `python-multipart`, `mcp`, `cryptography`, `pypdf`, `anyio`, `certifi`, `idna`, `opentelemetry-api` and `pydantic-settings`.
  `uv.lock` now resolves `PyJWT` 2.15.1, `starlette` 1.10.0, `python-multipart` 0.0.33, `mcp` 1.30.0, `cryptography` 50.0.2,
  `pypdf` 6.21.4, `anyio` 4.14.2, `certifi` 2026.10.23, `idna` 3.20, `opentelemetry-api` 1.45.2 (with the matching
  OpenTelemetry packages) and `pydantic-settings` 2.16.2. tuvl signs and verifies its tokens with `PyJWT` using local EdDSA keys only;
  it does not use `PyJWKClient`, so most `PyJWT` advisories did not reach it, but the denial-of-service ones could. The multipart
  and form-parsing advisories (`starlette`, `python-multipart`) reach the upload endpoints, and the `mcp` ones reach `/mcp` when the export is on.
- **The minimum versions of the packages tuvl uses on a request path are raised.** `PyJWT[crypto]>=2.15.0`, `starlette>=1.3.1`,
  `python-multipart>=0.0.31`, `opentelemetry-api>=1.45.1`, `pydantic-settings>=2.14.2`, `mcp>=1.30.0` (extra `mcp`) and
  `pypdf>=6.21.4` (extra `pdf`), so an install that resolves to the floor no longer picks a vulnerable release.
- **The Insight UI lockfile is refreshed.** `vite` 8.3.6, `react-router-dom` 7.18.5, `js-yaml` 4.3.2 and `vitest` 4.1.12 (their
  ranges in `ui/package.json` are raised), and the lockfile entries for `postcss`, `nanoid`, `browserslist`, `source-map-js`, `brace-expansion`,
  `zod`, `@babel/core` and others. 27 of the alerts were in development tooling only.
- **`minimatch` is forced to 11.x in the UI lockfile.** `typescript-eslint` still depends on `minimatch` 10, which has a denial-of-service
  advisory fixed in 11.0.1; `ui/package.json` carries a `pnpm.overrides` entry (`minimatch@10` to `^11.0.1`) until `typescript-eslint` moves to 11.
  It is a lint-time dependency; typecheck, lint, tests and the UI build pass with it, and no Dependabot alert is left open.

---

## [2.0.2] — 2026-11-04

A patch release of `tuvl` and `tuvl-insight`: the CLI starts on Windows, the spec checker accepts more of what the engine builds, and Gemini loop agents keep their tool-call signatures.

### Added

- **A `note:` line under a spec step.** `- note: <text>` is free text no check reads; use it to say why a step is built
  the way it is. When a step line does not parse, the S001 message now lists the lines a step takes.

### Fixed

- **`tuvl` starts on Windows.** `devtelemetry` imported `fcntl`, which does not exist on Windows, so every command
  failed at import with `No module named 'fcntl'`. The outbox locks use `msvcrt.locking` there (#32).
- **Commands started from Insight work on Windows.** They ran through `sh -c` and were stopped with `os.killpg`, and
  the check that a command was still running used `os.kill(pid, 0)`, which ends the process on Windows. They now run
  through Python, are stopped with `taskkill /T`, and are checked through the process table.
- CI installs the wheel on Windows, starts the CLI, and runs the command-job tests there.
- **Gemini loop tool calls preserve thought signatures.** OpenAI-compatible parsing and streaming now keep Gemini's opaque `extra_content` on tool calls, and loop agents re-emit it on the next request instead of failing with a missing `thought_signature`.
- **The spec checker knows the signals of storage and http tool steps.** `tool storage get` could not route
  `not_found` and `tool http` could not route `http_404` (S004, S012), so a spec-first project had to build such a step
  as a `code` agent; they now follow `tuvl lint project`. `tuvl verify spec` no longer reports R002 for a storage step.
- **An optional trigger input in a spec's `Input:` line is kept.** ``Input: `x`? (str)`` was dropped without a finding,
  so the scaffold left the field out of the trigger and `verify spec` reported R001. It is read as `str?`, and an
  `Input:` field that does not parse is an S001 finding.
- **A project that sets `llm.memory.retention` passes `tuvl run tests`.** The retention sweep used a PostgreSQL-only
  query and crashed on the offline SQLite database; it now does nothing off PostgreSQL.
- **The `agent-code` skill lists the effects the schema accepts.** It listed `effect: none`, which the schema rejects;
  the effects are `read`, `write` and `destructive`, and the default is `write`.

## [2.0.1] — 2026-11-02

A patch release of `tuvl` and `tuvl-insight`: two small fixes found when CI first ran on freshly booted GitHub-hosted machines, the PyPI project descriptions, and the release workflow.

### Fixed

- **A token's `iat` is never ahead of the clock.** `mint_token` rounded `iat` to the millisecond with `round()`, which can round up by half a millisecond, and `jwt.decode` rejects an `iat` in the future. A token verified within that window of being minted failed with `ImmatureSignatureError`. `iat` is now floored to the millisecond (same precision).
- **The first memory-retention and draft sweeps no longer wait for the machine to have been up.** Both throttles compared `time.monotonic()` (seconds since boot) with a last-run value of `0.0`, so on a machine up for less than the interval (5 minutes, and 1 minute) the first sweep was skipped. They now start open.
- **`tuvl-insight` has a project description on PyPI.** Its `pyproject.toml` had no `readme`, so the project page said the author had not provided one.

### Changed

- **PyPI descriptions follow the current positioning.** `tuvl` and `tuvl-insight` open with "Intelligent workflows, deterministic execution" and say what the package does; `tuvl-insight` had no project description and now ships a README. Both explain PyPI's "repository or configuration differs from a previous release" notice and how to verify a release.
- **Releases publish from the public repository only, with provenance checked.** The publish jobs check out the provenance script they run, `uv publish` skips files PyPI already has, and a manual run can set `skip_tuvl` to publish only `tuvl-insight` (this is how `tuvl-insight` 2.0.0 was published: the `v2.0.0` run uploaded `tuvl`, then stopped on the missing script).
- Dependabot pull requests target `develop`, like every other change.

---

## [2.0.0] — 2026-11-01

The first stable 2.0 release. Everything since `2.0.0-beta.10` is below; the beta sections that follow record how 2.0 was built.

### Added

- **Browser apps on another origin can call the API.** `spec.api.cors_origins` in `.tuvl/system.yaml` (or `TUVL_CORS_ORIGINS`, which wins) lists the origins allowed to call it. Nothing is allowed by default, including in dev, so existing servers behave as before. An origin is `scheme://host[:port]` with no trailing slash: `tuvl lint project` rejects anything else and warns on `*`, and the server leaves a malformed entry out. The middleware is the outermost one, so a preflight is answered before the IP allowlist or auth, and a `401` still carries the headers; it exposes `X-Tuvl-Run-Id`, which `@tuvl/client` reads, and sends no credentials mode (tokens are bearer headers). `tuvl generate ui-spec` says in its backend-surface section whether a browser may call from another origin.
- **Five skills for building the UI with `@tuvl/client`.** `tuvl-ui` is the starting point (read `ui/specs/`, install the client and generate the types, let the browser reach the backend (an allowed origin or one shared origin), one screen per workflow, every status handled); `ui-run` starts and follows a run (`execute`, `RunHandle`, events, live drafts, errors), `ui-approve` answers human forms and tool-call approvals, `ui-auth` covers sign-in, token storage and refresh, and `ui-data` covers `crud()`, file uploads and server-side webhooks. `tuvl init` and `tuvl update skills` install them with the others, and every code block in them is typechecked against the published client.
- **An idea can come from a link.** `tuvl import idea --url <link>` (repeatable, and mixed with files) saves a Markdown snapshot of a web page,
  a Jira issue, a Confluence page or a GitHub issue or pull request as an ordinary idea file; `IDEA.yaml` records the link, its kind and when it
  was fetched, and Insight's upload dialog takes links one per line. Jira and Confluence read `TUVL_ATLASSIAN_EMAIL` + `TUVL_ATLASSIAN_TOKEN`
  (Cloud) or `TUVL_ATLASSIAN_PAT` (Server / Data Center), GitHub reads `TUVL_GITHUB_TOKEN`; credentials go only to Atlassian Cloud, the hosts in
  `TUVL_ATLASSIAN_HOSTS` and `api.github.com`, and never into the idea. Requests use the webhook address guard: internal addresses and plain
  `http` are refused unless `--allow-private`, the connection is pinned to the address that was checked, and redirects are checked at every hop.
  The spec prompt marks a snapshot with its source and tells the agent that an idea is material to specify, not instructions (the same rule is
  in the `tuvl-spec` skill), because a page or ticket can be edited by anyone.
- **`tuvl init` asks for the coding agent with a menu.** Up and Down (or `j` and `k`, or a number) move and Enter chooses, instead of typing
  an id. Each agent says whether tuvl can draft and build with it. Without a terminal (a pipe, CI, a coding agent) `init` asks in plain text
  as before.

### Changed

- **Antigravity CLI can draft specs and build tasks, not only review.** tuvl runs it with `--mode accept-edits`, which approves file edits inside
  the working folder and denies a shell command or an edit outside it (checked on `agy` 1.4.10); a review runs without the mode. When agy
  denies a tool it still exits `0`, so tuvl now reports the denied tool.

### Fixed

- **Settings → Project shows the project record again.** `GET /dev/project` was answered by the project summary, so the project
  id, author and email never reached the Settings page. The summary now lives at `GET /dev/project/summary`; `/dev/project` is
  the record (GET and PUT). A test fails if two routes share a method and path.
- **Memory lookups use their index.** `recall`, `forget` and `compact` now carry the `metadata ? 'memory_key'` predicate that
  the partial index `ix_svs_memory_key` requires, so Postgres no longer scans the whole vector store.
- The `SystemRunCheckpoint` docstring said checkpoints are redacted; they are stored unredacted (resume state) and redacted on
  export. Removed the unused `DeltaStream._wake`.
- **`tuvl run tests` no longer flakes on `map`, `parallel` and batched loop tools.** The offline database shared one
  transaction between every session, so one session's rollback could undo another's journal sequence number and the run
  crashed on `UNIQUE constraint failed: tuvl_system_run_events.run_id, seq` (reported as `run ended running: None`). The
  offline database now runs in autocommit. Offline tests therefore keep an agent's writes where Postgres would roll them
  back (an error outcome, a retried attempt, a `per_run` rollback).

### Documentation

- **Contributing and security, for a public repository.** `CONTRIBUTING.md` says when to open an issue first, how a pull request
  lands (the source is published as tagged release snapshots) and asks for a `Signed-off-by` line under the new `DCO` file;
  the PR template has the sign-off box; `SECURITY.md` offers GitHub private vulnerability reporting; the code of conduct
  names `developer@tuvl.io` instead of a personal address; the README no longer says development is private; the
  Discussions link is gone (the feature is off).
- **A complete option reference, generated from the CLI.** `docs/cli-reference.md` lists every command and every option with its value, default and
  description (62 commands), written by `python scripts/gen_cli_reference.py` and checked by a test, so a new option cannot be missing from the docs.
  `docs/cli.md` gains the rows and options it was missing (`verify lock`, `verify ui-spec`, `import idea --commit`, `check storage --json`,
  `list objects --limit`), and `tuvl init --help` and `tuvl dev --help` now say what they do.
- `CONTRIBUTING.md`, `AGENTS.md`, `README.md` and `SECURITY.md` match 2.0: the real source layout, all nine engines (`map`
  and `parallel` included), validation codes V001–V043, Python 3.13–3.14, and 2.x as the only supported line. A test keeps
  the engine lists in README and AGENTS in step with the schema.
- The source maps in `AGENTS.md` and `CONTRIBUTING.md` show the split `api/dev/` and `validate/workflow/` packages and the new runtime modules, and both
  describe `scripts/bench_steps.py`. `api.md` says how soon the event stream sees a commit, `runtime.md` how the system indexes are built at boot, and
  `storage.md` where a local store keeps `sha256`. The `tuvl-test` and `agent-tool` skills say that the offline test database autocommits and that MCP
  sessions are reused.

### Removed

- `make publish`: releases are published by CI (`.github/workflows/publish.yml`, with PEP 740 attestations), never from a laptop.

### Performance

- **`tool` agents with `use: mcp` reuse their MCP session.** A worker keeps one session open per tenant and server instead of
  starting a stdio server and running the MCP handshake on every call. A change to the server's artifact or to an environment
  variable it reads opens a new session, a dead server is replaced on the next call, and an unused session closes after
  `TUVL_MCP_IDLE_SECONDS` (default 300; `0` restores a session per call). Sessions close at shutdown.
- **Run event streams wake on commit instead of polling every 250 ms.** A journal event, a run leaving `running`, or a draft piece
  committed in the same process wakes `GET /api/runs/{id}/events` at once; a 1 s poll covers writes from other worker processes.
  An idle stream now costs about one query batch a second instead of four.
- **New indexes:** `tuvl_system_run_deltas (created_at)` for the draft sweep, and a partial index for agent memory's per-step lookup
  (`remember`), whose query now carries the matching `metadata ? 'step'` predicate.
- **Indexes on large system tables are built concurrently at boot.** The new indexes and the vector store's existing ones are created
  with `CREATE INDEX CONCURRENTLY IF NOT EXISTS` outside any transaction, so upgrading a deployment with a large table does not block
  writes. One process builds them (an advisory lock; others skip), and an index left invalid by an interrupted build is rebuilt. The
  booting process waits for the build to finish.
- **Uploads hash each file once.** The upload handler reuses the digest `store.put()` computes, and the local store keeps `sha256` in
  `.meta/<key>.json`, so `head` and `list` no longer re-read every file. Files written by older versions are hashed as before.
- The judge verdict cache and the dev file listing (`GET /dev/files`) no longer do file I/O on the event loop.
- **Less work per agent step.** The runner hashes the workflow and looks up a child run's depth once per run load instead of on every
  step; parsed TEL expressions, templates and type strings are cached; the helper models the `llm`, `loop` and `tool` engines build
  (structured-response, tool-argument and finish models, `where` type adapters) are built once per agent. A reloaded project gets new
  objects, so nothing built against the previous registries is reused.
- **Model calls reuse connections.** `tuvl.llm` keeps one pooled HTTP client per event loop (timeouts are per request) instead of
  opening a client, and its DNS, TCP and TLS handshakes, for every completion, stream and embedding.
- **Webhook deliveries reuse connections, without weakening the address check.** Clients are pooled per scheme, host name, checked
  address and port, so a connection is only reused for the name its certificate was verified for and the address checked on that
  attempt; every delivery still resolves and checks the name first. A short response body with a declared length is now drained and
  dropped so its connection can be reused.
- `scripts/bench_steps.py` measures runs/s, steps/s, step latency and SQL statements per step against Postgres with a stubbed model.
- **Fewer database round-trips per agent step** (11.8 → 8.8 SQL statements per step on the benchmark, one transaction fewer).
  Completing a step takes the lease fence, the run-row update and both journal sequence numbers in one `UPDATE … RETURNING`.
  Outside dev mode, the operator-control check (pause, abort) is read in the same statement and transaction that journals
  `agent_started`, instead of in a separate transaction before it; dev mode keeps the separate check, because breakpoints are
  checked between the two. `journal.allocate_seqs` allocates several sequence numbers at once; `append_event` and
  `write_checkpoint` accept a number allocated that way. With webhooks enabled, the journal no longer loads the run row to find its
  workflow when the runner already knows it, or when no loaded workflow declares webhooks.
- Under `transaction: per_run`, a worker that has lost its lease no longer journals `agent_completed` for the step it was running.


### Changed

- Internal refactor, no behaviour change: `tuvl.api.dev` and `tuvl.validate.workflow` are packages split by area (same router,
  routes, OpenAPI document, public names and diagnostics); Insight shares one `YamlPanel` and `RunWithAgent` reuses `JobOutput`.
- Insight's `react-hooks` lint warnings are fixed and ESLint warnings now fail CI (`--max-warnings 0`, no longer advisory).


## [2.0.0-beta.10] — 2026-10-26

**Answers that stream, steps that run side by side, agents that remember.** Everything below is checked by the validator, journaled,
and covered by tests; none of it changes a workflow that does not use it.

- **An `llm` agent can show its answer while it is written.** `llm: { stream_tokens: true, stream_field: draft }` sends the text of one
  `str` output to the run's event stream (`token_delta`) as the model produces it; the agent's result, validation, token count and
  retries are unchanged, and `token_reset` tells a client to drop the draft when the answer is rejected or the agent runs again.
  `stream_field` can be left out when there is one `str` output. It is refused with `guardrails.output` (V039), because text a guardrail
  could still refuse must not be shown.
  - **The event stream carries drafts only when asked.** `GET /api/runs/{id}/events?drafts=true` adds `token_delta` / `token_reset`
    frames (no `id`, not journal events, not replayed or exported); without it the stream is exactly the journal, so existing clients are
    unchanged. A client that connects mid-agent first gets the draft so far (`replay: true`). A worker writes drafts in batches of about
    100 ms to a new table, `tuvl_system_run_deltas`, created on first start; a sweep removes a finished run's drafts after a minute.
  - **The model client can stream.** `tuvl.llm.astream` takes the same arguments as `acompletion` and yields `TextDelta` events, then
    one `StreamEnd` with the same `Completion` a non-streaming call returns. It covers the OpenAI-compatible providers (OpenAI, Gemini,
    Ollama, Groq, Mistral, vLLM and any gateway) and Anthropic. `timeout` is the longest silence between chunks.
  - **Insight and `@tuvl/client`.** The run view shows the live draft under the running step and drops it when the step completes.
    `RunHandle.onToken(listener, { onReset })` (and `tokens()`, `stream()`, `runs.tokens`, `runs.stream`) in `@tuvl/client` ask for drafts;
    `events()` never receives them.

- **New engine `parallel`: different read-only agents at the same time.** One step runs 2-16 branches (db reads, http GET, storage reads,
  `llm`, `code` with `effect: read`) together and joins their outputs, so the step takes as long as its slowest branch. `on_error: fail`
  stops the rest at the first failure; `collect` lets all finish and emits `partial`. Each branch has its own database session and
  idempotency key, the step's `budget.timeout` cancels every branch, and a worker killed mid-fan-out is resumed on another worker.
  - The journal gets `parallel_started`, `parallel_branch` and `parallel_finished`, and a branch's own events carry `branch`. The
    worst-case tokens are the sum of the branches. Offline tests mock each branch by id (or the whole step); `tuvl generate code` writes
    stubs for `code` branches.
  - New checks V040 (branches only read), V041 (inputs and outputs line up), V042 (not under `transaction: per_run`).
  - Insight has a **Run lookups in parallel** preset, a card, and branch badges in the run timeline; the `agent-parallel` skill covers it.

- **A loop runs the read-only tool calls of one model turn at the same time.** When a turn asks for several MCP tools marked
  `readOnlyHint` that need no approval, they run together; events and the transcript keep the order the model asked in, and
  `max_tool_calls` / `max_calls` count as before. Writes, approvals and agent, workflow and collection tools still run one at a time.

- **Agents can remember across runs.**
  - `llm: { memory: { collection, key, window, recall_top_k, max_tokens, write, retention } }` (and the same block on `loop`) puts the
    owner's latest memories and related older ones in the prompt, fenced as data, and stores the `write` fields after a successful run,
    once per step. The owner is the signed-in principal, the tenant, or an input (refused on a public trigger, V043); the model never
    chooses it, and every read and write is filtered by it in the query. `write` can never include secure fields (V043), unknown
    collections are V014, and the recalled tokens count towards `policy.max_tokens_per_run`. Journal events `memory_recalled` and
    `memory_written`.
  - The built-ins `tuvl.memory_remember` and `tuvl.memory_recall` do the same from a `code` step; `tuvl.memory_compact` (run it from a
    scheduled workflow) folds each owner's older memories into one model-written summary and deletes the originals in one transaction.
  - `retention: 90d` makes the runtime delete older memories (checked every five minutes). `tuvl prune memory <collection> --key <owner>`
    erases one owner's memories and `--older-than 30d` erases old ones; both report how many rows went and touch nothing else.
  - `tuvl_system_vector_store` rows now carry `created_at` (added to an existing table at startup, indexed) and there is an index for
    reading one owner's rows by time.
  - Insight has a **Memory** page (collections, each owner's memories with the agent and run that wrote them, **Forget** for one memory or
    one owner) and a **Remember across runs** section in the agent panel; the dev API has `GET /dev/memory`, `GET /dev/memory/{collection}`
    and `DELETE /dev/memory/{collection}?key=` / `.../{id}`.

- **The coding-agent skills cover what b8 and b9 added.** A new skill, `tuvl-workflow`, covers the workflow document around the agents:
  triggers (http, schedule with `input` and `enabled`, mcp), **file uploads** (`upload`, `file` inputs, the reference a run receives,
  V037), ends and `rollback`, `policy`, **webhooks** (`spec.webhooks`, V038, off unless `TUVL_WEBHOOKS_ENABLED=true`) and per-signal
  outputs. `agent-loop` describes the `collection:` tool and `loop.memory`, `agent-llm` streaming and memory, `agent-code` the `file`
  reference (`ctx.read_file`) and the settings of `tuvl.data_search` and `tuvl.data_ingest`, `agent-tool` reading an uploaded file, and
  `tuvl-ship` the webhook switch and the review step of the gate. `tuvl update skills` adds the new skills to existing projects.

## [2.0.0-beta.9] — 2026-10-24

- **Insight's agent panel picks the AI model, and shows the agent as YAML.** An `llm` or `loop` agent's tab (Prompt / Loop) now has an
  **AI model** dropdown of the models configured on the AI Models page (or a `provider/model` id of your own), with the system prompt
  and prompt as fields; a `decide` agent's decision model is the same dropdown, limited to decision models. A new **YAML** tab holds the
  whole agent as it is in the workflow file, editable; its id cannot change there, because routes point at it.
  The same panel also uses the artifacts stored in the project: the system prompt, prompt and steering can each be written there or
  taken from a stored artifact (pinned to a version), an `llm` or `loop` agent lists its skills, and a loop lists the MCP servers it may
  call with their allowed tools and the judge that supervises it.


- **Upload an idea and generate a spec from Insight.** The Ideas page has **Upload idea** (files, a folder or text; the result is
  what `tuvl import idea` makes, numbered from your initials, with a title, an optional "replaces" and an optional commit) and, on
  each idea, **Generate a spec with my agent**, which runs `tuvl generate spec --idea` with your coding agent, shows its output
  and asks for consent the first time. The draft is written into the project uncommitted, or onto a `spec/<id>` branch with nothing
  pushed. The dev API has `POST /dev/ideas` and `POST /dev/ideas/{id}/spec`, and `GET /dev/ideas` now also says who you are
  (`me`: your initials, your agent, whether you consented).


- **Collections in workflows.** A loop agent can search a collection as a tool: `- collection: kb_docs` (with optional `top_k`, `filter`
  and `scope`) is offered to the model as `search_kb_docs(query)`, a read that needs no approval; `tuvl lint project` checks the
  collection exists, and the same check now covers the `collection` of `tuvl.data_search` and `tuvl.data_ingest`. In Insight those two
  steps pick their collection from the configured ones (with its embedding, and a warning when pgvector is missing), a loop lists the
  collections it can search, and **Retrieve and answer** adds a search step routed into an answering step that cites its sources.


- **Releases carry PyPI provenance again, and fail when they do not.** 2.0.0b4 to b8 were published without it: the signed attestations
  sat beside the files but `uv publish` was given a list that left them out. It now uploads them, and the release job checks PyPI
  for every file afterwards. Provenance cannot be added to a release later, so b4 to b8 stay without it.

## [2.0.0-beta.8] — 2026-10-22

- **The terminal UI draws the dotted feather.** The banner and the side panel show the logo's dot lattice, every other dot, so the
  dots read as points like the logo in Insight. The browser-tab icon of Insight is dotted too.


- **The public catalogue is Nest, at `nest.tuvl.io`.** `tuvl project register` and Insight's Register dialog default
  to it; `--nest URL` or `TUVL_NEST_URL` point at a self-hosted one, and the login is cached in
  `~/.config/tuvl/nest.json`.

- **A synchronous request no longer answers `202` for a run that is about to finish.** When the background worker claimed a
  freshly queued run before the HTTP request could, the request returned the run still `running`, so a caller that
  expected the end's status and body (a registration that returns a token) got a `202` descriptor instead. The request now
  waits for that run, up to `sync_timeout`.

- **Developer telemetry (optional).** Share what developers do on a project through a collector the team runs, and see what
  the others did. Every audit event (ideas, specs, approvals, tasks, agent attempts, branches, commits, pull requests,
  gates, ships) and one `cli.command` event per command (no arguments) is queued locally and sent in the background;
  a collector that is down only delays delivery and a command never waits on it. Off by default; turned on at `tuvl init`
  (`--dev-telemetry URL` or the prompt), with `tuvl set dev-telemetry URL` or in Insight → Settings → Developer telemetry;
  the setting is `.tuvl/dev-telemetry.yaml` (commit it). Each developer registers once at the collector with the team's join
  code (`tuvl register developer`) and gets a token of their own, kept in their own config folder, never in the project; the
  token sends and reads. Insight shows the team: the Audit page's **Team** view, **Team activity** in a task, and who built
  each branch; `tuvl list team-activity` does the same in a terminal. `tuvl show dev-telemetry` and `check dev-telemetry`
  show and test it. The default identity is initials only. It is development only: `tuvl ship` also refuses the config,
  `.tuvl/audit/`, `.tuvl/trace/`, `.tuvl/live/`, `.tuvl/worktrees/` and `.tuvl/agent.yaml` if a `.dockerignore` lets them into
  the image (V027). The collector we provide is the separate Tuvl Flightlog (Apache 2.0; a Python service for SQLite or PostgreSQL, and a tuvl project for PostgreSQL); the contract is in
  `docs/dev-telemetry.md`, so any server can stand in. A company's own certificate authority is set with
  `TUVL_DEV_TELEMETRY_CA`, and `tuvl set dev-telemetry --rotate-token` replaces a token.

- **Specs are approved, and tasks are worked, in Insight and the terminal UI.** Insight's Specs page has
  **Approve**, **Send back…**, **Reopen** and **Derive tasks** (the same commands as the terminal, with the
  same audit entries); the terminal UI has the same actions. A task opens in a floating panel that drags,
  resizes, pops out into its own window and reads as Markdown: what it builds, why it is blocked or not done,
  what it waits for and the spec section it came from. `tuvl show task <id>` prints the same in the terminal,
  and the terminal UI shows it too. **Graph** draws a spec's tasks as a dependency tree, top to bottom in
  execution order (tasks that do not depend on each other share a step), coloured by done, running, failed,
  blocked, in progress and ready.

- **Branches have a dashboard.** `tuvl list branches` and Insight's **Branches** tab show the spec's work as a tree of
  what grew from what (spec, scaffold, one branch per agent task; branches built apart sit side by side and are
  marked parallel; a branch lists its commits and messages): merged (even when the branch was deleted after the
  merge), open (commits ahead and behind, last commit, pull request) or not built, which one to merge next and the
  command for it. tuvl still never merges. A second scaffold run on an unmerged scaffold branch says so instead
  of "Nothing to scaffold".

- **A blocked task says why, and cannot be run.** The reason names what it waits for and the state of each
  (`blocked: waiting on TASK-… Order model (todo)`, each dependency once). `Run with my agent` is disabled for a blocked task and
  the server refuses it; only agent tasks are run with the agent (models and workflows come from the scaffold:
  Insight offers `Generate the scaffold`, which runs `generate code --no-agent`). `list tasks --json` gains
  `depends_on`, `waiting_on`, `live` and `failure`.

- **A failed task run keeps a report of every attempt.** For each attempt: the check it failed (the coding
  agent did not finish, it changed files outside the task, the project did not validate or the step was not
  implemented, the review found blocking problems), the problems, the agent's exit code and the end of its
  output. It is shown in Insight (**Why it failed**), by `generate code` (`full report: tuvl show task …`), by
  `show task`, `list tasks` and the terminal UI, and kept until the task succeeds. A command started from
  Insight that fails ends with its exit code and the errors it logged instead of nothing.

- **`generate spec --here` writes the spec into your working tree** (no branch, no pull request), and
  `generate code` carries a spec and task list that were never committed on the scaffold branch. Spec
  commands accept the id (`SPEC-SR-0001-04`) as well as the path. The terminal UI selects with the mouse
  and copies, moves between actions with the arrow keys, and ends each command with a highlighted token line.

- **The tagline is "Intelligent Workflows. Deterministic Execution."** in the CLI help and banner, the terminal
  UI, Insight and the documentation; the banner and terminal UI show the Malayalam name തൂവൽ (feather).


- **Commands are actions: `tuvl <verb> <thing>`.** The pipeline reads in order: `init`, `import idea`,
  `generate spec --idea`, `approve spec`, `generate tasks --spec`, `generate code --spec|--task`, `verify spec`,
  `review code --spec|--task`, `run tests`, `ship`. Each verb makes one promise: `generate` writes files, `lint`
  runs static rules (zero tokens), `verify` asks whether one thing is still in step with another (exit 1 on
  drift), `check` asks whether something works right now, `list`/`show` only read. `validate` is
  `lint project`, `doctor` is `check system`, `codegen` is `generate code`, `lock` is `generate lock` and
  `lock --check` is `verify lock`, `test` is `run tests`, `ui spec` is `generate ui-spec`, `spec …`, `runs …`,
  `audit …`, `agent …`, `project …`, `keys`, `db`, `storage`, `skills`, `mcp` moved under their verbs (the
  table is in `docs/cli.md`). `review code --spec` / `--task` reviews only the code agents those tasks created.
  There are no aliases for the earlier names; `--json` shapes are unchanged. Files tuvl generated mention
  the new names, so run `tuvl generate code` and `tuvl generate ui-spec` once after upgrading.

- **Agent commands report their token use, and the terminal UI is easier to use.** Every command that ran a coding agent ends with a `tokens · … in · … out · $…` line (Claude Code reports tokens; other agents are named as not reporting). In `tuvl dev`'s terminal UI the project actions are listed under every selection, `c` copies the output, and each command ends with its token line (or "none used"). `tuvl init` now creates `idea/` and `specs/`, and `create spec` says `Created specs/<id>.md` on one line.

- **The production server shuts down cleanly and is easier to run behind a proxy.** `tuvl serve app` now replaces its own process with uvicorn instead of running it as a child, so a container's
  SIGTERM reaches uvicorn: open requests finish (`--graceful-timeout`), running runs get `--drain-timeout`
  seconds, then the rest are interrupted and resumed by another worker from their checkpoint. New
  `--forwarded-allow-ips` (so `--allow-host` and logs see the real client behind an ingress),
  `--limit-max-requests`; the `Server` header is off. The Helm chart gains a termination grace period, a
  `preStop` pause, `forwardedAllowIps`, `limitMaxRequests` and a hardened `securityContext` (non-root, no
  privilege escalation, capabilities dropped, seccomp; `readOnlyRootFilesystem` opt-in); the image's start
  command is `tuvl serve app`.


- **Webhooks.** A workflow declares `spec.webhooks` (`name`, `url`, `secret`, `events`, `stages`) and the engine
  sends a small signed HTTPS notification when a run completes, fails, aborts, waits for a person or finishes a
  named agent. The notification carries only a run id and what happened; the receiver pulls the run with its
  own token. Targets come from the workflow and the environment, never from a caller. Security: HMAC-SHA256
  signature with a timestamp, secrets only as `${ENV}` references (rotation with `previous_secret`), `https`
  only, internal and numeric-trick addresses refused when declared and again after resolving (every address, the
  connection pinned to the checked one), no redirects, no proxies, no response read, a fixed request, a circuit
  breaker. Delivery is a transactional outbox, at-least-once with backoff; off unless
  `TUVL_WEBHOOKS_ENABLED=true`. `GET /api/runs/{id}/webhooks` and `.../redeliver`; V038 in `tuvl validate`.
  `@tuvl/client` is the supported receiver (`verifyWebhook`, `webhookHandler`).


- **A production server exposes only what it needs, and the API documentation lists only your workflows.** `spec.api.expose` (or `TUVL_API_EXPOSE`) chooses which system route groups a production server mounts: `auth` by default; `admin` (`/admin/*`), `artifacts` and `system` (`/api/_system/*`) are mounted only when you ask. `runs` (starting, following and approving runs) is always mounted, in dev and production, and cannot be switched off. `spec.api.docs` (or `TUVL_API_DOCS`) is `off`, `workflows` or `all`: `/docs`, `/redoc` and `/openapi.json` are off in production by default and, where they are on, list your own workflow and model routes without the system ones. The dev server mounts every group as before and its documentation hides the system routes; `spec.api.dev_expose_all: false` (`TUVL_DEV_EXPOSE_ALL`, or Insight's Settings → API Access) makes it mount only what production would. Insight's API Access page also edits the production groups and the documentation mode. `tuvl generate ui-spec` now writes a Backend surface section (the groups and documentation mode of a production server), says which of a workflow's two start routes suits which caller, and tells UI builders never to call `/api/_system`, `/admin` or `/api/artifacts` unless listed. **Breaking for production deployments** that call `/admin/*`, `/api/artifacts` or `/api/_system/*`, or read `/docs`: add them to `spec.api.expose` or `spec.api.docs`.

- **`TUVL_BISCUIT_PRIVATE_KEY` is no longer read.** The 1.x name for the signing key was accepted with a
  warning; only `TUVL_SIGNING_KEY` sets the key now (same format: 64 hex characters). Without it the
  server refuses to start in production and uses an ephemeral key in development. `tuvl keys generate
  --write --force` no longer looks for the old line.


- **mypy is clean across all of `src/` (0 errors in 218 files) and CI gates on it.** Fixing it found two
  bugs: `GET /models/{model}/{id}?include=…` failed for any model with relations (the record was passed
  where a list was expected), and the `pii_mask` guardrail raised on JSON content whose top level is a
  list instead of masking it.

- **File uploads.** New `file` type and `http.upload` on a trigger: the route accepts
  `multipart/form-data`, stores each `file` / `list[file]` input in a named store (S3, Azure, GCS or local)
  under a generated key, and the run receives a reference (`store`, `key`, `name`, `size`,
  `content_type`, `sha256`), never the bytes. The caller is authenticated before the body is read, size and
  count limits apply while it streams, a content-type allow-list is available, and a JSON body cannot name
  a file (so a reference cannot be forged). `POST /api/workflows/{name}/runs` takes the same multipart body.
  `tuvl validate` adds V037; OpenAPI describes the multipart body; `tuvl codegen` types it; a code agent reads
  it with `ctx.read_file`; `tuvl ui spec` documents it; Insight's Run form and trigger panel handle it.
  `@tuvl/client` sends a multipart body when an input is a `File` or `Blob`.


- **Code-quality pass.** Removed dead code (13 unused functions and constants, the `core/errors.py`
  module, the 1.x skill files under `.agents/`, a 2 MB logo shrunk to 56 KB); `tuvl` now declares
  `cryptography`, which it imports directly; the mypy gate covers about 150 files (was about 85).

- **Security: a failed create-user insert no longer returns the database driver's message**, which can
  include the failing row (the password hash). It is logged; the response says only that a database
  error happened.

- **CRUD list filters are checked.** `filter[field]=…` on an unknown field used to be ignored and return
  every row; it now answers `422`, as does a filter on a `secure` field.
- A corrupt `.dev-session` file no longer crashes `tuvl dev`; model versions sync in one query at startup.
- The package classifier says Beta until 2.0.0 is released.


- **Schedule and API triggers are switchable, and scheduled runs carry input.** `trigger.schedule`
  accepts `{ cron, enabled, input }` (the cron string still works) and `trigger.http` accepts
  `enabled: false` to keep its settings without mounting the route. Scheduled runs used to start with an
  empty input, so a workflow with required fields could never run on a schedule; `schedule.input` fixes
  that and `tuvl validate` checks it against `trigger.input` (V019). Insight's Start panel gets **API** and
  **Schedule** checkboxes, a cron check with the next fire times, an input field per trigger input, and the
  scheduler's status. New dev endpoints `GET /dev/schedule/preview` and `GET /dev/schedules`; the admin
  workflow list reports `api_enabled` and `schedule_enabled`.

## [2.0.0-beta.7] — 2026-10-20

- **`tuvl ui spec` writes the UI integration guidelines.** `ui/specs/` (scaffolded by `tuvl init`) tells the
  developers and coding agents who build the frontend how to use the backend, and recommends `@tuvl/client`:
  an index, a file per workflow whose tasks are done (endpoint, auth, request, every end's status and body,
  the run lifecycle, approval forms, real examples, snippets, a checklist), a file for models with CRUD
  routes, and `ui-contract.json`. Zero tokens; `codegen --spec` adds a workflow's file with the task that
  completes it; `--check` detects drift; notes below the marker are kept. New audit event `ui.spec.generated`.

- **More coding agents, and a page on authoring with them.** Codex CLI and Gemini CLI can now draft
  specs and build tasks like Claude Code (their sandbox / `auto_edit` mode confines writes to the
  folder); Cursor, Antigravity, Copilot CLI and Amp can review code through `tuvl review --agent` but are
  never given write access, because their headless modes cannot limit it to one folder. `tuvl agent
  list` shows which is which, and `tuvl agent test` checks yours. New `docs/authoring.md` describes the
  driven path end to end (what an agent may do, what tuvl enforces, what is committed).

- **`tuvl dev` starts a terminal UI on a terminal.** A tree of ideas, specs and tasks (with live
  `codegen` progress), the actions for what you select, and three logs (command, engine, audit). It is
  a launcher: every action runs the `tuvl` command you would type, so the gates and the audit trail are
  the same; actions that spend your agent's tokens or are hard to undo ask first. `--no-tui`, or no
  terminal, keeps the old behaviour. New extra `tuvl[tui]` (Textual), in `tuvl[standard]`.

- **Insight shows the authoring trail, read-only.** A new **Ideas** page (each idea's files with a text
  preview, the specs drawn from it and their approval), an **Audit** page (the local ledger with filters,
  live polling, event JSON and a chain-integrity badge), and on **Specs** an approval badge per tile and
  a pipeline strip idea → spec → tasks. Behind them: `GET /dev/ideas`, `/dev/ideas/{id}`,
  `/dev/specs/lifecycle` and `/dev/audit` (dev mode only).

- **`tuvl ship` runs the deterministic gate and can write a runnable archive.** Before packaging it
  checks that every spec under approval is approved and unchanged, `spec check` and `spec verify`
  have no errors, every task is done, generated files are current, the agents' Python has no C-rule
  errors, `uv.lock` is current and the git tree is clean (`--allow-incomplete`, `--allow-dirty`). The
  image is labelled with a hash of its runtime files and the git commit. `--format archive|both`
  writes `dist/<name>-<version>.tar.gz` (or `--archive-format zip`) plus a `.sha256`: the runtime files
  with `run.sh`, `RUN.md`, `.env.example` and `BUILD-INFO.json`, byte-for-byte reproducible, to run
  with `uv` on a host without Docker. `--no-build` is now `--no-image` (the old name still works).

- **`tuvl review`: the Python of code agents, checked.** Free rules first (C001–C012: shape, processes,
  direct network, environment, file writes, non-determinism, module state, hard-coded URLs, secrets,
  unrouted signals, writes in a read agent) plus `ruff`; errors fail, warnings with `--strict`.
  `--agent` adds a second opinion from a coding agent that cannot change anything and must answer in
  JSON; it is advisory unless `--blocking`, and `--post` puts it on the pull request. `codegen --spec`
  reviews each task's Python before committing (`--review static|agent|none`). New skill `tuvl-review`.
  `tuvl init --ci github|gitlab` writes the deterministic gate as CI.

- **`tuvl codegen --spec`: build a spec's models and workflows from its tasks.** Deterministic part
  first (no tokens): the missing models from `# Data`, a workflow skeleton of `pending` steps from
  `# Flows`, then the generated files, on a `feature/<spec>-scaffold` branch. Then your coding agent
  implements one pending step per task, each on its own `feature/TASK-…` branch stacked on the
  previous one, limited to the workflow, `agents/*.py` and `artifacts/`; a task is accepted only when
  `validate` and `spec verify` agree, and failures are fed back. Pull requests are opened as drafts
  and never merged. `tuvl codegen` without `--spec` is unchanged.
- **Fixed:** `spec verify` no longer reports a route the spec *waives* (`- conflict: waived — …`) as
  one the spec doesn't have, although `validate` requires it. `tuvl validate` no longer calls
  `idea/INDEX.yaml` and `idea/*/IDEA.yaml` unknown kinds. `tuvl spec gen` titles its commit with the
  spec's title.

- **`tuvl spec gen`: a coding agent drafts a spec from an idea.** tuvl numbers the spec, works on a
  `spec/<id>` branch in a worktree, lets the agent change only that spec file (anything else is
  reverted and fed back), re-imposes the lifecycle fields it must not set, runs `spec check` and
  retries with the findings, then commits and, with a remote and `gh`/`glab`, opens a draft pull
  request. It never approves and never merges. Claude Code runs restricted at the source (no
  code-running tools, files confined to the folder); a `generic` agent (`tuvl agent use generic
  --command '…'`) is checked afterwards only. Idea text is sent to the agent after a one-time consent.
  `tuvl agent test` now runs the agent once on a trivial task; `tuvl spec import --commit` commits
  the idea. A missing task list is now answered with `tuvl spec tasks`, not a request to the agent.

- **Audit ledger.** Authoring actions (init, agent choice, idea import, renumber, spec create /
  approve / reject / reopen, task derivation) are recorded, refusals included, in a local,
  hash-chained `.tuvl/audit/` ledger; `tuvl audit log|show|verify|export|prune` read it, and `tuvl
  doctor` verifies it. Prompts and agent output go to `.tuvl/trace/`, referenced by hash. `tuvl spec
  status --watch` refreshes the derived task status live and shows `codegen` progress from
  `.tuvl/live/tasks.json`.

- **Spec approval and derived task lists.** `tuvl spec approve` records who approved a spec and a hash of
  its body; editing the body afterwards makes the approval stale (new check S017) and the spec a draft
  again. `reject` and `reopen` send it back; `tuvl spec list` shows the state. `tuvl spec tasks` derives
  the task list from an approved spec with no model: one task per new model, step and example, plus a
  manual task for each external-system step; re-running keeps ids and marks removed items `obsolete`.
  `tuvl spec new --idea` numbers and links a spec; checks S018–S021 cover the idea links.

- **Ideas.** `tuvl spec import <file-or-folder>` copies an idea into `idea/IDEA-<INITIALS>-NNNN/`
  with a record of every file's hash and the text of Word files and PDFs (`tuvl[pdf]`) extracted
  beside them; `tuvl spec ideas`, `tuvl spec idea <id>` and `tuvl spec renumber <id>` list, show and
  renumber them. Ids are hierarchical: `SPEC-SK-0004-01` and `TASK-SK-0004-01-003` inherit their
  idea's number, and a task list may use these ids (the `T1` form still works). `tuvl doctor` has an
  `ideas` group.

- **`tuvl init` asks who you are and which coding agent to use.** It detects the coding agents on
  your machine, asks your initials and which agent tuvl may run for you (or none), and writes both
  to the git-ignored `.tuvl/agent.yaml`. It also runs `git init` on branch `develop` when git is
  installed (`--no-git`, `--branch`, `--commit`). New options: `--agent`, `--no-agent`,
  `--list-agents`, `--initials`. The new `tuvl agent list|use|test` shows and changes the choice,
  and `tuvl doctor` has an `agent` group.

- **`tuvl doctor` checks the whole project.** Named groups (`project`, `env`, `config`, `models`,
  `storage`, and with the toolchain `git` and `skills`), `--only` / `--skip`, `--strict`, `--quiet`,
  and `--fix` for the safe repairs (`git init -b develop`, ignoring `.env`). `--json` keeps `healthy`,
  `models` and `storage` and adds `groups`, `summary`, `checks` and `fixed`.

- **A bare `pip install tuvl` is the production CLI.** It offers `run`, `runs`, `db`, `keys`, `doctor`
  and `storage`. The developer commands (`init`, `dev`, `validate`, `ship`, `deploy`, `test`,
  `codegen`, `lock`, `mcp`, `spec`, `skills`, `project`) appear when the toolchain extras are installed
  (`tuvl[standard]`); without them each answers with the install hint and exits 2.
- **`tuvl init` template:** `requires-python` is `>=3.13,<3.15` (it was `<3.14`), and `prerelease =
  "allow"` is written while the pinned tuvl is a prerelease, so a new project resolves on Python 3.14
  and on a beta.

## [2.0.0-beta.6] — 2026-10-19

- **Project registration: organization.** The project record gains `organization` and an optional
  `organization_url` (`tuvl project set organization …`, and Insight → Settings → Project). The
  author and email are now always sent when you register, so the hub can reach the developer; the hub
  keeps them private. The `--public-email` flag and its Insight checkbox are removed. The catalogue
  lists the name, version, organization (linked), type, license, description and the repository link.

## [2.0.0-beta.5] — 2026-10-18

- **`tuvl deploy` announces managed hosting.** The command exists so the name is reserved and
  discoverable: it explains that deploying to a hosted, managed tuvl engine is coming, points to
  `tuvl ship` for today, deploys nothing and exits 1 so a script cannot mistake it for a deployment.
- **License: Apache License 2.0.** Starting with `2.0.0b5`, tuvl and tuvl-insight are licensed
  under the Apache License, Version 2.0, which includes an express patent grant. You can use,
  modify, distribute and sell software built on tuvl, and contributions are accepted under the same
  license. A `NOTICE` file travels with every distribution, changed files in a redistribution
  should say so, and the name and logos are not licensed as a brand (§6). Earlier releases keep the
  terms they were published with. The wheels now declare `License-Expression: Apache-2.0` and
  include `LICENSE`, `NOTICE` and, for tuvl-insight, `THIRD_PARTY_LICENSES.txt` (the notices of the
  JavaScript packages compiled into the UI; `make ui-notices` regenerates it) and the SIL Open Font
  License text for the bundled Geist fonts. `tuvl init` no longer writes a license into a new
  project's `pyproject.toml`: that choice is yours. `SECURITY.md` lists 2.x as supported.
- **Blob storage.** Workflows can read and write files through named stores declared in
  `storage/*.yaml` (`kind: Storage`): S3 and every S3-compatible service (MinIO, R2, Wasabi, Spaces,
  B2), Azure Blob Storage, Google Cloud Storage and local disk. `tool: { use: storage }` adds
  `put`, `get`, `head`, `list`, `copy`, `delete` and `presign`; code agents get `ctx.storage(name)`.
  The client is plain httpx with built-in request signing (SigV4, Shared Key, SAS, OAuth), so the
  install grows by no packages. Credentials resolve from keys, the environment, web identity,
  container and instance roles, and Azure and GCP identities. `tuvl storage check|ls`, `tuvl doctor`
  and an Insight Storage page; validator rules V030–V034 and a new reserved signal `too_large`.
  See `docs/storage.md`.
- **OTLP over HTTP by default.** Span and metric export now uses OTLP/HTTP (`http/protobuf`, port
  4318) instead of gRPC, which removes `grpcio` from the install — about 35 MB. **Breaking for
  telemetry users:** the default endpoint is `http://localhost:4318`, and a collector reached on
  `:4317` needs `TUVL_OTLP_PROTOCOL=grpc` plus `pip install 'tuvl[otel-grpc]'`. tuvl logs a warning
  at startup when an HTTP exporter points at `:4317`. `OTEL_EXPORTER_OTLP_PROTOCOL` is honoured,
  and `.tuvl/telemetry.yaml` gains `otlp_protocol` (also a field in Insight → Settings → Telemetry).
- **litellm removed.** tuvl calls models through its own client (`tuvl.llm`, httpx only), so the
  install drops litellm and the packages only it needed — about 175 MB of site-packages (including
  the `google-cloud-aiplatform` stack of the `[google]` extra) and a 2.7 s first-call import. Model
  ids stay `provider/model`. Built in: OpenAI, Anthropic (native Messages API, structured output
  through a forced tool), Gemini, Ollama, Groq, Mistral, DeepSeek, xAI, OpenRouter, Together,
  Fireworks, Perplexity, LM Studio and vLLM, plus Azure OpenAI (v1 endpoint), Bedrock
  (`bedrock-runtime` with a Bedrock API key), Vertex AI (OpenAI endpoint with an access token), Cohere
  and Voyage (embeddings) through the vendors' OpenAI-compatible APIs. Any other OpenAI-compatible
  endpoint works with `api_base`, including a LiteLLM proxy (`litellm_proxy/<alias>`); SageMaker,
  watsonx, Hugging Face and Replicate need a gateway. Embedding entries accept `api_base` and `api_key`
  (an `${ENV_VAR}` reference), also editable in Insight. Model calls emit `gen_ai.*` spans on their own, without a
  callback. See `docs/models.md`.
- **Changed:** decision-model `provider: litellm` is now `provider: llm` (`litellm` is still read
  as an alias). `tuvl init` writes `OLLAMA_BASE_URL` (was `LITELLM_OLLAMA_BASE_URL`) and, for a custom
  endpoint, `LLM_API_BASE` (was `LITELLM_PROXY_BASE_URL`); the unused `litellm_proxy_base_url` setting
  is gone.
- **`tuvl ship` installs only what a project uses.** `redis`, `pgvector`, `mcp`, `libcst` and
  `aiosqlite` are no longer base dependencies of `tuvl`; they are the extras `tuvl[redis]`,
  `tuvl[vector]`, `tuvl[mcp]`, `tuvl[otel-grpc]`, `tuvl[codegen]` and `tuvl[test]`, and `tuvl[standard]` includes all
  of them, so a development install is unchanged. `tuvl ship` reads the workflows (an `mcp` tool or
  export, a redis datasource, collections or `tuvl.data_*`) and the imports of `agents/*.py`, writes
  `tuvl[<only those>]==<locked version>` into the Dockerfile and records it as the image label
  `io.tuvl.extras`. It refuses an import that `pyproject.toml` does not declare, lists declared
  packages nothing imports, and stops when a kept `deploy/Dockerfile` lacks an extra the project now
  needs. `--extra NAME` adds one the scan cannot see. A feature used without its extra fails with
  `pip install 'tuvl[name]'`. A bare `pip install tuvl` is about 13 MB smaller. A missing pgvector is
  now logged at info, not warning.
- **Fan-out: the `map` engine (TD-52).** `engine: map` runs a child workflow once per item of a
  list, `concurrency` items at a time, and returns the results in input order. Each item is a child
  run with its own journal; a parent that is reclaimed after a crash follows the children it already
  started, so no item runs twice. `on_error: fail` stops at the first failed item; `collect` keeps
  going and emits `partial`. A mapped workflow must finish on its own (no human steps), recursion is
  cut by `policy.max_depth`, and `tuvl validate` prices the step at the child's worst case times
  `max_items` (V035, V036). Specs can describe it (`map <workflow>`), and Insight draws, adds and
  traces it. This is a map over one workflow, not a fork/join of different branches.
- **Multi-tenant runs (TD-40).** In `multi_tenant` mode a run now belongs to a tenant: the run,
  journal, checkpoint and approval tables actually get their `tenant_id` column (the injection
  never fired before, so every run was `default_tenant`; existing databases are upgraded at boot),
  starting a run requires a tenant, workers execute it under that tenant with the row-level-security
  variable set, and callers reach only their own tenant's runs (`iam:admin` and `runs:control`
  included; another tenant's run is a `404`). Scheduled workflows no longer refuse multi-tenant
  mode: `trigger.tenants: [acme, globex]` starts one run per tenant per slot.
- **Workflows that name a disabled model are refused (TD-56)** with the reason (`409` over REST)
  instead of failing at their first `db` call; Insight shows why on the workflow.
- **Insight Models page keeps `secure` (TD-58).** Editing a model used to drop `secure: true`; the
  flag now has a column and survives a save.
- **Releases (TD-34):** `make bump VERSION=x.y.z` and `make check-version` replace the unused
  release-please setup. **Tests (TD-59, TD-60):** a steadier heartbeat test and a 180 s per-test
  timeout. **Cleanup (TD-57):** the unused 1.x `/dev/agents/{name}` file endpoints are gone.

## [2.0.0-beta.4] — 2026-10-16

- **Project identity.** `tuvl init` writes `.tuvl/project.yaml` with an immutable, self-checking id
  (`prj_<uuidv7>-<check>`; commands refuse an id that fails the check), author,
  email, type (`poc`/`dev`/`production`) and version. `tuvl project info|set|init|register` and an
  Insight **Settings → Project** page read and edit it. Insight can also register the project with
  the catalogue (payload preview, GitHub device login) and flags a listing that is out of date.
  The description is Markdown (2000 characters): `tuvl project set description @about.md` or `-`
  for stdin, rendered by `project info`, Insight's preview and the hub. `tuvl project init --new-id`
  re-mints the id of a copied project. The public catalogue at hub.tuvl.io is not live yet; `--hub`
  or `TUVL_HUB_URL` point registration at another hub. See `docs/project.md`.
- **GitHub device login.** `POST /auth/oauth/{provider}/device/start|poll` lets a CLI sign in with
  GitHub without a browser callback; `allowed_users` restricts GitHub logins by handle.
- **Changed:** the browser OAuth redirect now carries the token in the URL fragment
  (`…#token=…`), not the query string.

## [2.0.0-beta.3] — 2026-10-14

- **Insight: workflow groups and a tile picker.** `metadata.group` is accepted again, so workflows
  group the way they did in 1.0. Clicking the workflow name (or ⌘K) opens a panel of tiles, grouped
  and searchable, below the top bar; picking one closes it. The Start panel has a Group field and
  new workflows can be created into a group.
- **Insight: workflow access is visible.** The START node and a top-bar chip show who can run the
  workflow (IAM role, scope, public or any signed-in user); the chip opens the access editor.
- **Insight: black, white and cyan theme**, in dark and light, in step with tuvl.io and tuvl.dev.
  The canvas follows the theme.
- **Insight: node icons and hover details.** One icon per engine kind (pending is an hourglass), the
  breakpoint dot shows on hover, and a read-only popover shows a node's details and validation
  messages.

## [2.0.0-beta.2] — 2026-10-14

- **Laya from Docker Hub, English-only by default.** `tuvl init --decision-model laya` and
  `tuvl ship` use the published `tuvl/laya-serve` image. `init` scaffolds `model: english` with the
  English-only `-en` image (about 0.8 GB smaller, about 2 GiB of memory); `ship` picks the `-en`
  image for `model: english` and the full image for `auto` / `multilingual`, with matching memory
  requests in the Helm chart.

## [2.0.0] — 2026-10-10

tuvl 2.0 is a redesign. Workflows are graphs of typed **agents**, every run is
**durable and journaled**, and projects are built **spec-first**. There is no
1.x compatibility layer: 1.x workflows (`steps:` with `kind:` step types) fail to
load with an error pointing at the 2.0 format.

### Highlights

- **Typed agents and seven engines.** Each agent declares `inputs`, `outputs`
  and the signals it emits; its engine is `code`, `tool` (db / http / mcp),
  `decide` (rules, then an optional decision model), `llm`, `loop` (bounded
  tool use), `human` or `pending`. Named ends replace `Response` steps. A typed
  type language and a statically checked expression language (TEL).
- **`tuvl validate`** checks contracts, data flow, types and policy statically
  (V001–V026) and prints each workflow's determinism profile and worst-case
  token cost.
- **Durable runtime.** Postgres-backed runs, journal and checkpoints; a work
  queue with leases (any worker continues any run); waits for people and tool
  approvals; compensation; operator pause/resume/abort/steer; replay;
  `tuvl run --role api | worker`.
- **Decisions and loops.** `decide` never falls back silently; a decision
  model reports its confidence (a missing one is an error, never 1.0) and can
  be a hosted model behind an OpenAI-compatible endpoint (`api_base` +
  `${ENV}` `api_key` on `type: decision`). **Laya and Jev** are built in over
  System One (`POST /v1/systemone`): `tuvl init --decision-model laya`
  scaffolds a self-hosted Laya (the `tuvl/laya-serve` image on Docker Hub, amd64 and arm64) and `tuvl ship` adds it
  to the Helm chart (`--with-laya/--without-laya`); `loop` tools are
  agents, child workflows or allow-listed MCP tools, with per-tool approval,
  budgets and a supervisor. **Judges** are calibrated, cached artifacts.
- **Spec-driven tooling.** `tuvl spec new | check | verify | status | diff`, a
  task list written by the coding agent with derived statuses, deterministic `tuvl codegen`, offline `tuvl test` with mocks and
  replayed fixtures, `tuvl lock`, the `tuvl mcp` dev server and 13 skills for
  coding agents.
- **Insight 2.0.** Specs as a row of tiles with the task board below and
  each spec editable in its own window, a canvas over the same YAML,
  runs with breakpoints, step mode and approvals, change-engine checks against
  pinned fixtures, judge calibration and a rules editor.
- **Surfaces.** REST + SSE (gRPC removed), OpenAPI generated from contracts,
  MCP export of workflows, `@tuvl/client` 2.0 with typed client generation.
- **Access tokens are Ed25519-signed JWTs** (`alg: EdDSA`, pinned), replacing
  Biscuit. Same claims (`sub`, `groups`, `scopes`, `tenant`, `iat`, `exp`), the
  same 32-byte signing key — now `TUVL_SIGNING_KEY`; `TUVL_BISCUIT_PRIVATE_KEY`
  is still read — and the public key at `GET /auth/jwks.json` for gateways.
  Tokens issued before the upgrade stop working: everyone signs in once.
  `tuvl.core.auth.biscuit_auth` is now `tuvl.core.auth.access`;
  `mint_biscuit_token` is `mint_token`.
- **Python 3.13 and 3.14.** Dropping the native `biscuit-python` dependency
  (no 3.14 wheels on any platform) lets tuvl run on 3.14; CI tests both.
- **Prompt-injection fencing.** `llm`/`loop` inputs and loop tool results are
  fenced as untrusted `<data>`; IAM changes (deactivation, role removal) end
  existing sessions at once.

- **`tuvl spec check`** (CLI, MCP `spec_check`, `GET /dev/specs/check`): a spec's `# Data`, `# Flows`
  and examples are now a small Markdown grammar, checked with zero tokens against rules S001–S016 —
  unrouted engine signals, unreachable ends, unsafe model steps, data-flow gaps, secure fields reaching
  a model, name collisions, missing access, bad rules and loop tools. It is the first step of the CI
  gate. `tuvl spec new` and the `--sample` spec use the grammar.
- **Fixes from a live demo.** `tuvl.data_search` failed on a `filter` rendered from a uuid
  input (`community_id: "{{ community_id }}"`) — filters are JSON-encoded like ingest metadata now.
  A code agent's input model was cached by workflow and agent id only, so an edited contract kept
  validating against the old one until restart; the contract is part of the key now.
- **Code agents are edited in Insight, with AI assist** (as in 1.0, rebuilt for 2.0). The Engine tab
  of a `code` agent is a Python editor (full-screen mode, ⌘S): saves are syntax-checked, normalized by
  codegen so the contract's signature survives, and hot-reloaded. **✦ Assist** uses a project LLM
  AgentModel with the agent's contract, its models' fields and the `agent-code` skill; it returns the
  whole file into the editor and never saves. `GET|PUT /dev/agents/{run}/source`,
  `POST /dev/agents/{run}/assist`, `GET /dev/agents/assist/models` (dev mode only).
- **Dogfooding fixes (a project built end to end with the skills).** Spec grammar: compensators
  (`compensate `x`` + `T` step) and `- tests: waived — why`; `spec check` resolves an `(existing)`
  model to the project's schema and flags reserved signals routed from `code` steps (as the validator
  does); `spec verify` compares defaults by value (`false` = `False`) and checks `compensate`; task
  status honours test waivers and counts compensators a rollback test ran. Tests record
  `compensated`; a mock that only injects `signal: error` is fault injection, not T001. The db tool
  stores a uuid/number/date rendered into a string column as text. `policy.model_only_decisions:
  allow` accepts a model-only decision feeding writes on purpose (V013), visibly in the YAML.
- **Insight referee views.** The Specs page shows `spec check` findings inline and in a Findings
  tab, and a Traceability tab (`spec verify`): step → agent → tests, example test results with their
  failure reasons; steps open their canvas node and the canvas links back. Run/Step are disabled
  while a model the workflow uses isn't configured. A test that ends somewhere unexpected now names
  the agent and error that ended it. Concurrent test runs in the dev server are serialized (a run
  swapped decision providers and a status reload cleared registries under another run).
- **The coding agent writes the task list; tuvl checks it.** `tuvl spec analyse` / `--apply` are
  removed (CLI, MCP `spec_analyse`/`spec_apply`, `/dev/specs/analyse|apply|analysis`, Insight's
  Analyse button and plan review), with the analysis model: `tuvl init` writes no `llms/analysis.yaml`
  or `config.yaml`, and `ProjectConfig.spec.analysis_model` is gone. `tuvl spec check` checks the
  task list (P001–P005: format, targets, coverage, no estimates or typed statuses, staleness) and its
  JSON lists the spec's section anchors and hashes. `TaskPlan` tasks lose `effort` and gain
  `example` targets. Task status now also follows `spec check` (blocking findings block every task)
  and `spec verify` (a target that contradicts the spec is `in_progress`). The `tuvl-plan` skill is
  rewritten for the agent-written list; `tuvl-task-loop` builds from the spec and loops on
  `spec verify`. The `--sample` task list covers every model, step and example.
- **`tuvl spec verify`** (CLI, MCP `spec_verify`, `GET /dev/specs/verify`): compares each spec flow
  with its workflow, each Data model with its ModelDefinition and each example with its honest test
  result (R001–R010), and prints the traceability matrix spec step → agent → tests. Contradictions
  fail; unfinished work fails with `--strict`, which joins the CI gate.
- **Honest tests.** A test that only passes through unmocked `pending` placeholders is reported
  **STUBBED**, never counts towards task status, and fails `tuvl test --strict`. Tests seed rows with
  `given:` and set the caller with `as:`, so `code`, `tool db` and rule-based `decide` agents run for
  real; mocking them is warning T001. Estimates are no longer shown (`spec status`, Insight).
- **Model safeguards.** `tuvl validate` reports which models the workflows need and their
  environment variables; an unknown model reference is an error (V028) and undeclared credentials a
  warning (V029, an error for `tuvl ship`). A production server refuses to start while a model's
  variable is missing; `/health` reports model readiness; new `tuvl doctor [--live]`; `tuvl ship`
  lists the exact secrets the deployment needs.
- **Ship only runtime files.** `tuvl ship` writes `.dockerignore` as an allowlist, so specs,
  task lists, tests, agent rules and skills, compose files and any other file stay out of the image
  (V027 refuses development files; regenerate an older `.dockerignore` with `--force`). Ship now
  refuses `pending` agents (V010), and a production server never loads `Test`/`TaskPlan` documents.

### Upgrading from 1.x

Rewrite workflows as 2.0 agent graphs (`version: tuvl/v2`): `Functional` →
`code` agents (`@agent`, typed `In`/`Out` from `tuvl codegen`), `ModelOp` /
`APICall` / `MCP` → `tool` agents, `Router` → `decide`, `Agent` → `llm` or
`loop`, `HumanInTheLoop` → `human`, `Response` → named ends. `kind: test` files
become `kind: Test` documents. Model CRUD routes are off unless a model opts in
with `spec.api.crud`. The 2.0 examples (`tuvl-io/examples`) show each pattern.

### Removed

- **gRPC / gRPC-Web transport.** The `/grpc` mount (sonora) and its three
  services — `ExecutionService`, `DevService`, `IamService` — are gone, along
  with the `grpcio`, `sonora` and `grpcio-tools` dependencies, `make proto`,
  `make apply-patches` and the vendored sonora patch. REST + SSE is the only
  application transport. (OTLP export to a collector is separate: it uses
  HTTP by default, see above.)
- The 1.x Insight AI code chat (`POST /dev/ai/chat`) and the UI's unused 1.x
  workflow, Lens and Spectrum types. Code agents are written in an editor or by
  a coding agent using `tuvl mcp`.
- Insight's hand-rolled protobuf codecs and the `@protobuf-ts/*` dependencies;
  the UI calls the existing REST endpoints directly.

### Added

- `tuvl.contract` — the 2.0 type language (`str`, `int`, `float`, `bool`,
  `date`, `datetime`, `uuid`, `json`, `enum[...]`, `list[T]`, `Model[.variant]`,
  `T?`, plus the `{type, description, default}` long form): parser with
  column-precise errors, canonical formatter, and `build_model()` compiling a
  field map to a strict Pydantic model.
- `tuvl.contract.tel` — TEL, the 2.0 expression language for decide rules,
  approval guards, test assertions and `{{ }}` templates: a strict parser,
  a static type checker against declared inputs (unknown fields, impossible
  enum values and type mismatches are compile-time errors), and a sandboxed
  evaluator that only walks mappings and lists.
- `tuvl.contract.schema` — Pydantic models for the 2.0 documents: `Workflow`
  (agents as a discriminated union over the seven engines, each engine's
  parameters under its own key, named outputs, policy, supervisor), `Test`
  and `TaskPlan`, plus the `judge`/`rules` artifact types and
  `AgentModel` `type: decision`. Unknown keys are rejected everywhere;
  embedded types, TEL conditions and cron expressions are syntax-checked.
- `tuvl schema export [--kind Workflow|Test|TaskPlan|all]` (hidden) prints the
  JSON Schema Insight's forms are rendered from.
- Artifact loading validates `judge` and `rules` artifacts; `AgentModel`
  rejects an unknown `spec.type` and validates decision providers.
- The config loader routes `version: tuvl/v2` documents (`Workflow`, `Test`,
  `TaskPlan`) to `tuvl.contract.registry`, validated against the 2.0 schema.
  Errors are reported in the author's terms (`spec.agents[triage].decide.rules[0].when`)
  with hints for YAML pitfalls (booleans from bare `on`/`off`, commas and `?`
  inside `{ }`). 1.x workflows still load into the 1.x engine until it is
  removed; the rejection path (with a migration pointer) is in place.
- `tuvl validate` shape-checks 2.0 documents and no longer warns "no
  workflows" for a 2.0-only project.

- `tuvl.validate` — the 2.0 workflow validator (spec Appendix B rules
  V002–V012, V014, V016–V019, V022): route targets, signal coverage per
  engine, must-available data flow with a witness path, producer/consumer
  type compatibility, TEL and template type checks against declared inputs,
  artifact and model references, unguarded cycles, and the determinism /
  worst-case-token profile. Models are typed from `ModelDefinition` YAML, so
  validation does not boot the runtime.
- `tuvl validate` reports 2.0 findings with `file:line` and rule codes, prints
  each workflow's profile, and gains `--json` for CI and coding agents.

- `tuvl.runtime.models` — durable run storage for the 2.0 runner:
  `tuvl_system_runs` (status, lease, sequence counter, control flags),
  `tuvl_system_run_events` (append-only journal), `tuvl_system_run_checkpoints`
  and `tuvl_system_approvals`, created on the primary datasource at boot and
  tenant-scoped in multi-tenant mode.
- `tuvl.runtime.journal` — the run journal: events and checkpoints share one
  per-run sequence allocated atomically (`UPDATE … RETURNING`), values are
  stored as JSON with `secure: true` fields redacted at any depth, oversized
  event payloads become size markers, and whole runs export/import as
  `tuvl.journal/v1` documents (the fixture and replay format).
- `tuvl.runtime.queue` — the run queue: `enqueue`, `claim` (oldest first,
  `FOR UPDATE SKIP LOCKED`; also re-claims runs whose lease expired, which is
  crash recovery), lease `heartbeat`, owner-guarded `release`, `requeue` for
  resumes; a `Worker` loop with bounded concurrency and heartbeats; and
  `run_inline` for the synchronous HTTP path (answer 202 once `sync_timeout`
  passes while the run continues). Lease times use the database clock.
- `tuvl.runtime.runner` — the 2.0 runner: walks the graph one
  agent at a time; checks inputs and the outputs declared for the emitted
  signal (`contract_violation`), applies `budget.timeout` and `budget.retry`,
  routes, renders named ends, honours pause/abort requests, and checkpoints
  every step. An agent's database writes, its checkpoint and a lease-ownership
  check commit in one transaction. Rollback ends, unhandled errors and aborts
  run compensators newest-first. Engines plug in via
  `tuvl.runtime.engines`; the `pending` engine returns typed stubs.
  `transaction: per_run` is refused for now.
- The `code` engine: `from tuvl import agent, Ctx`; `@agent("name")`
  registers an async function, `agents/*.py` load via
  `tuvl.runtime.code.load_agents` (project root on `sys.path` so
  `agents._generated` imports resolve). The function gets its inputs as one
  typed object (its annotated class, or a model built from the contract) and
  returns outputs, `(outputs, signal)` or a bare signal. `ctx.db`
  (`get`/`list`/`create`/`update`/`delete`) is limited to `spec.models`,
  refuses writes under `effect: read`, and writes inside the agent's
  transaction; `ctx.http`, `ctx.artifact(ref)`, `ctx.log`,
  `ctx.idempotency_key`.
- The `tool` engine. `db`: create/read/update/delete/list/upsert
  with rendered `where`/`values` (validated and coerced into the model),
  `not_found`/`conflict` signals, writes inside the agent's transaction; the
  result lands in the agent's single output (V007 checks the count
  and type). `http`: `${VAR}` env expansion, `expect.status`, `http_<status>`
  signals when routed, 429 → `rate_limit` and 5xx → `transient` (retryable),
  `map` from `response.*` paths, `idempotency: auto`. `mcp`: stdio/SSE servers
  from `type: mcp` artifacts; object results spread across declared outputs.
- The `llm` engine: `system`/`prompt`/`skills` (inline or
  artifacts; `{{ }}` in the prompt renders against the view), the inputs as a
  typed `## Input` block, and structured output from the agent's `outputs`
  (`response_format` json_schema, also stated in the system message). With
  more than one normal route the schema gains `outcome: enum[...]`, which
  picks the signal. Invalid JSON, a schema mismatch, an unrouted outcome or a
  missing per-signal field is `parse_error`; `finish_reason: length` is
  `budget_exceeded`; provider errors classify as `timeout`/`rate_limit`/
  `transient`/`llm_error` for `budget.retry`. Non-judge guardrails run on
  input and output; judge guardrails are refused (`not_supported`) until the
  judge runtime lands. Each call journals an `llm_turn`.
- The `human` engine: opens an approval request (rendered
  title/instruction/assignee, `show`n inputs, a form derived from `outputs`
  and decorated by `human.form`) in the agent's transaction and suspends the
  run as `waiting_human`. `tuvl.runtime.human.decide` validates the form
  against `outputs`, enforces the authorisation rules (required group, no
  self-approval, owner or `iam:admin` otherwise) and re-queues the run;
  `route_on` maps bool fields to `true`/`false` and enums to their values.
  Workers sweep expired requests, whose runs take the `expired` route.
- Step idempotency keys count finished steps (suspensions excluded), so they
  are stable across retries, crash re-execution and suspend/resume.
- **Breaking:** models expose no `/models/*` CRUD routes by default. Enable operations one by one with `spec.api.crud: [list, read, create,
  update, delete]`; each route requires the model's IAM permission for its
  tier (`read`, `write`, `delete`; `spec.access` overrides and group ACLs
  apply). The admin scope listing shows only exposed tiers.
  `spec.api.expose_model_crud` in `system.yaml` stays as a deployment-wide
  master off. `spec.schema` is removed — schemas are always generated, since
  typed contracts need them; `validate` warns where it is still set.
- The 2.0 REST surface: each workflow's `trigger.http`
  route is mounted from YAML (body or query plus path params form the input,
  validated against `trigger.input`; default-deny, `public: true`,
  `required_scope`/`required_group`). Runs execute inline up to
  `sync_timeout` and answer with the end's body and status, `202` with
  `status_url`/`events_url` (and the approval descriptor when waiting), or
  `500`/`409` with the run error. `POST /api/workflows/{name}/runs`,
  `GET /api/runs/{id}` (`runs:read`, own runs; `iam:admin` any),
  `GET /api/runs/{id}/events` (SSE: replay from `?from_seq=` or
  `Last-Event-ID`, then live), `POST /api/runs/{id}/{pause|resume|abort}`
  (`runs:control`; aborting a waiting run cancels its requests),
  `GET /api/approvals`, `POST /api/approvals/{id}` and
  `POST /api/runs/{id}/human/{agent_id}` (`approvals:decide` plus the
  agent's group rules). `steer` answers 501 until the supervisor lands.
- `tuvl run --role all|api|worker --concurrency N`; the app starts the 2.0
  runtime at boot (loads `agents/`, mounts triggers, starts the worker).
- The scheduler enqueues 2.0 runs instead of executing them (`trigger:
  schedule:<fire time>`); the check-and-enqueue runs under the advisory lock,
  so each fire is enqueued once across processes.
- Built-in code agents `tuvl.data_search` (hybrid RRF over pgvector and
  full-text) and `tuvl.data_ingest`, replacing the 1.x `DataSearch` /
  `DataIngest` nodes; `code.with` gives any code agent static, templated
  parameters (`ctx.params`). The `tuvl.` agent prefix is reserved.
- Validation: V025 checks built-in parameters; V015 warns on write or
  destructive steps without `compensate` when later steps can still fail,
  and on a declared tool effect weaker than the derived one.
- **Breaking — the 1.x engine is removed.** Workflows without
  `version: tuvl/v2` fail to load (and `validate`) with a migration hint; 1.x
  `kind: test` files are rejected. Removed: the step runner, step kinds and
  streaming, `nodes/` and `@node` (use `agents/` and `@agent`), the 1.x HITL
  router and instance table, the versioned `/{api_version}/run/{name}` route
  and workflow-version toggle/fork, `/api/agents/runs` (folded into
  `/api/runs`), Insight Lens/Spectrum (rebuilt on the journal in Insight 2.0),
  and `tuvl test` (Test 2.0 arrives with the spec tooling). The scheduler,
  system manifest, admin listing, boot-time artifact check and dev API read
  the 2.0 registry; `tuvl stream-watch` starts a run and follows its SSE
  events. `tuvl init --sample` writes a 2.0 ticket-triage project
  (`llm`, `code`, `human`, `tool` agents) that passes `validate --strict`;
  the scaffold's AGENTS.md describes 2.0 and the 1.x step skills are gone.
  `validate` checks `agents/` and that every `code` agent's `run` exists
  (V025).
- Runtime observability: one OTel span per run segment
  (`tuvl.run`) and per agent invocation (`tuvl.agent <id>`) with
  `tuvl.workflow`, `tuvl.agent`, `tuvl.engine`, `tuvl.determinism`,
  `tuvl.signal`, `tuvl.tokens`, `tuvl.attempt` and `tuvl.error_type` (error
  status); agent and workflow `hooks` fire `before_step`, `after_step` and
  `on_error`, observe-only.
- `llm` structured output lists every field as required (optional ones as
  nullable) and drops schema defaults: providers otherwise omit defaulted
  properties even when the value is in the input (seen with Gemini).
- `tool: http` bodies and query params, MCP args and ingest metadata are sent
  as JSON-safe values (uuid, date, decimal from typed inputs no longer fail
  serialisation).
- `transaction: per_run`: one data transaction for the whole
  run — each step in a savepoint (a failed or retried step drops only its own
  writes), no checkpoints (a crash reruns from the start), and the run's
  writes, completion and `run_completed` commit together; failures and
  rollback ends discard everything. Pause requests are ignored under
  `per_run`. V021 rejects `per_run` with human agents, approval-gated loop
  tools or a schedule trigger.
- Resilience suite (`tests/resilience`): a worker process SIGKILLed mid-run
  resumes on another worker from its last checkpoint with exactly-once
  writes and a stable `Idempotency-Key`; a human decision made after the
  requesting worker died resumes on another worker; a failing run
  compensates end to end over HTTP.
- The `decide` engine: ordered TEL rules (inline or a `rules`
  artifact; first match wins, the model is not called), then an optional
  decision model — confident → its value, below `min_confidence` →
  `low_confidence`, unavailable → `error / decision_model_unavailable` with no
  fallback; `shadow: true` journals the model beside the rules. Every
  invocation journals a `decision` record (`source: rule#n | model | rules`).
  Decision providers: built-in `litellm` (enum-constrained output, confidence
  from logprobs, retried without them when a model rejects the parameter);
  others register through the `tuvl.decision_providers` entry point. `/health`
  reports each decision model's last status. V013 (model-only decisions
  feeding writes, per `policy.model_only_decisions`) and V026 (provider
  installed, model pinned).
- The `loop` engine: a bounded tool-using agent. Tools are
  `agent:` (an off-spine agent; schema from its contract), `workflow:` (a
  child run with its own journal and gates, `parent_run_id`, tokens counted
  toward the loop, nesting capped by `policy.max_depth`) and `mcp:` (the
  server's `tools/list` filtered by `allow`). Arguments are validated before
  execution; invalid ones come back as an observation. The model ends by
  calling `finish` with the declared outputs (and `outcome`), with one repair
  attempt. Budgets `max_iterations`, `max_tokens`, `max_tool_calls` and per-tool
  `max_calls`; every turn and tool call is journaled; input, output and tool
  guardrails apply. In-loop approval: a matching call suspends the run as
  `waiting_approval` with the loop state in the request; approve, edit
  (re-validated) or reject (an observation) via `POST /api/approvals/{id}`
  resumes it on any worker. V014 rejects human and loop agents as loop tools.
- Loop supervision and live control: `loop.supervisor` (or
  `spec.supervisor`) rules — `tool_repeated`, `iteration_reached`,
  `budget_fraction`, `effect_count` — run every `every_n_iterations`; each
  fires once and acts by `abort` (exits through `aborted`), `steer` (injects
  a message) or `pause` (the loop state is saved; `POST /api/runs/{id}/resume`
  continues it on any worker). Operator requests reach a running loop
  mid-flight through the run's control flag: `pause`, `abort` and
  `steer` (`{message}`). Each action journals a `supervisor` event. Loop tool
  calls are bounded by `TUVL_AGENT_TOOL_TIMEOUT_S`. The 1.x orchestrator,
  `agent_core` and `agent_tools` are removed.
- Validation: V020 enforces `policy` statically — worst-case tokens against
  `max_tokens_per_run` (unbounded cycles included), `max_bounded_agents`,
  `require_human_before` (every path to a guarded effect passes a human; loop
  tools with that effect need `approval`), and `allow_secure_to_llm: false`
  (model-typed inputs carrying `secure: true` fields never reach llm, loop or
  model-backed decide agents). Schema errors are reported as V001.
- Judges: `type: judge` artifacts give typed verdicts
  `{verdict, score, reasons}` (a pass below `min_score` is a fail), cached in
  `.tuvl/judge-cache/` by `sha256(input ‖ judge content hash)` so re-runs are
  free and deterministic; `tuvl.runtime.judges.calibrate` measures agreement
  with the artifact's labelled examples. As guardrails they fail closed —
  `fail` → `guardrail_violation`, `uncertain` / unavailable → `error`
  (`judge_uncertain` / `judge_unavailable`) — and their tokens count toward
  the agent. A loop supervisor's `judge` reviews the trajectory every
  `every_n_iterations` (`on_violation`, `on_judge_error`). The 1.x guardrail
  `llm_judge` check is removed (validate points to judge artifacts).
- Loop fixes from the live support-triage demo: a failed `finish` ends as
  `error` (`parse_error`); when an approver edits a
  call's arguments the observation tells the model what ran (`requested` vs
  `executed`); each model turn has its own timeout with one retry, so a
  stalled provider no longer consumes the loop's whole budget.
- `tuvl test` (Test 2.0): runs `kind: Test` documents through the
  real runner in a throwaway in-memory database, offline by default. Mocks are
  checked against the agent's contract (a stale mock fails); `replay:` serves
  results from a pinned journal; unmocked `llm`, `loop`, `tool` and
  model-backed `decide` agents point to mocks, replay or `--live`, and `human`
  agents must be mocked. `expect` is evaluated structure → `output` (partial
  match) → TEL `assertions` → `judge` (cached; `--allow-uncertain`). Judges
  are calibrated before first use — untrusted ones are reported, `--strict`
  fails (V023). `--json`, `--only`. `tuvl runs pin <run_id>` writes a run's
  journal to `tests/fixtures/` via `GET /api/runs/{id}/export`. TEL
  `contains()` also tests an object's keys.
- Test `expect.wait: human | approval` (an unmocked human agent suspends the
  run) and `expect.path_includes`.
- Spec parsing (`tuvl.spec`): front matter, stable `#`/`##`
  anchors with content hashes (a section covers its sub-sections; whitespace
  edits don't move a hash), and deterministic extraction of `tuvl-example`
  blocks into `Test` documents with line-numbered errors.
- `tuvl codegen [<workflow>] [--check]` (deterministic): renders
  `agents/_generated/schemas.py` (typed inputs/outputs for every agent, trigger
  inputs, end names, referenced model variants); creates or **libcst-merges**
  `code` agent stubs — decorator, annotations, `async` and docstring follow the
  contract while the body and parameter names are preserved; prompt
  placeholders for missing `artifact://` prompts; `Test` files from spec
  `tuvl-example` blocks (stale generated tests removed, hand-written ones never
  touched); a `decide` rules skeleton for empty blocks. `--check` is the CI
  drift gate. New dependency: `libcst`.
- `tuvl lock [--check] [--no-mcp]`: `tuvl.lock` pins the tuvl
  version, AgentModel → model id, decision models (provider + package
  version), every `artifact://` reference (workflows and tests) → resolved
  version + sha256, judge models, and MCP tool input-schema hashes (listed
  live). `tuvl ship` fails on a missing or stale lock (V024) and its Dockerfile
  installs the locked tuvl version instead of floating with
  `--upgrade-package`. In production a loop logs `mcp.schema_drift` once per
  tool whose live schema differs from the lock (V014 W).
- `tuvl spec analyse <spec> [--model] [--apply [--only]] [--json]` (spec
  §17.3): the analysis model answers a structured `Plan` (never YAML); tuvl
  renders models, workflows (`pending`/`decide`/`human` agents, required
  `error` routes, reachable ends, policies), prose artifacts and the
  `<spec>.tasks.yaml` TaskPlan; validates with the plan applied and repairs at
  most twice on the errors the plan introduced; reports diff, notes, tasks,
  tokens and cost; applies the saved plan only on `--apply`. Re-analysis keeps
  implemented agents, existing models and task ids, and flags dropped tasks
  `obsolete` (new `Task.obsolete`). `ProjectConfig.spec.analysis_model`.
  Evaluation set of five reference specs (`tests/eval`, live with
  `TUVL_EVAL_LIVE=1`, nightly workflow).
- `tuvl spec status [<spec>] [--strict] [--no-tests] [--json]`:
  every TaskPlan task's status derived from the project — validation issues
  attributed to their workflow/agent/model, tests run offline, the lock
  checked — with `blocked`/`stale` overlays, obsolete tasks set aside and the
  remaining LLM-estimated hours. `tuvl spec diff` lists stale tasks and
  sections no task references. Analysis plans now target models and artifacts,
  keep task ids by title when a target moves, and drop judge/guardrail
  artifacts the model tries to write as prose.
- `tuvl runs list|get|tail|pin|replay` (`--url`/`--token`, `TUVL_URL` /
  `TUVL_TOKEN`) with a new `GET /api/runs` (newest first, `workflow`/`status`
  filters, the caller's own runs unless admin). `tail` follows the SSE stream
  and can start a run (`--workflow --payload`); `replay` re-runs pinned
  journals offline and fails when the end or output differ. **Removed:**
  `tuvl stream-watch` (use `tuvl runs tail`).
- `tuvl spec new <name>` writes a spec template.
- The `--json` outputs of `validate`, `test`, `runs replay`, `spec status`,
  `spec diff` and `spec analyse` are documented models
  (`tuvl schema export --kind cli`), snapshot-tested. `spec diff --json` is now
  an object: `{"specs": [...]}`.
- `tuvl init --preset gemini|openai|anthropic|ollama` writes current model ids
  for agents (`llms/default.yaml`) and analysis (`llms/analysis.yaml`,
  `config.yaml` `spec.analysis_model`); `-y/--yes` runs without prompts.
  `--sample` is now a 2.0 ticket-triage project with a spec, TaskPlan,
  generated tests and `tuvl.lock` that passes `validate --strict`,
  `codegen --check`, `lock --check`, `test` and `spec status --strict` as
  created (regression test runs the gate in subprocesses).
- `tuvl test`: `tool: db` agents run offline against the test database.
- `tuvl validate` no longer warns about a `config.yaml` without `dirs`.
- Commands given `-d <project>` from another directory now use that project's
  `.env` and directories (`use_project`); loading a project twice in one
  process no longer fails on already-defined model tables.
- `tuvl mcp [-d] [--allow-write]`: a stdio MCP server for coding
  agents. Read tools: `validate`, `spec_status`, `spec_diff`, `list_agents`,
  `get_contract` (with the generated class names), `explain_error`, `get_run`,
  `get_run_events`, `spec_analyse` (dry run), `codegen` (dry run), `test`.
  Write tools with `--allow-write`: `spec_apply`, `codegen_write`,
  `run_workflow`, `pin_fixture`. Refuses `TUVL_ENV=production`.
  `tuvl.validate.codes` holds the V-code table.
- Coding-agent skills for 2.0: `AGENTS.md` and 13 skills
  (`tuvl-spec`, `tuvl-plan`, `tuvl-task-loop`, `agent-code|tool|llm|loop|decide|human`,
  `tuvl-data`, `tuvl-test`, `tuvl-harden`, `tuvl-ship`) shipped with tuvl and
  installed by `tuvl init`; `tuvl skills update [--diff]` refreshes them, mirrors
  them to `.claude/skills/`, makes `CLAUDE.md` import `AGENTS.md`, and removes
  the nine 1.x skills.
- From the first P3 acceptance run (a coding agent built the refund spec to a
  green CI gate): `tool: db` `where` values take the column's type (a uuid
  string matches a uuid column; a bad value is a clear `tool_args_invalid`);
  analysis makes tasks with nothing to measure `manual` instead of leaving
  them `untracked` forever; skills document judge artifacts, calibration
  fixtures, guardrail checks and mocking db reads.
- From the KYC acceptance run: judge tasks the analysis model plans itself are
  dropped (tuvl derives one per acceptance bullet); a duplicate had become an
  unclosable manual task.
- Insight dev API v2 (`/dev/*`, dev mode): project summary, project files as
  text (YAML comments survive), validate / test / codegen / spec status, diff,
  new, analyse, apply in the CLI's `--json` shapes, schema export, canvas
  layout sidecar (`.tuvl/layout/<workflow>.json`). **Removed:** the `/dev/nodes`
  aliases and the YAML-parse-only `/dev/validate`.
- Insight: Spectrum, Lens and the Agents dashboard are gone, and so is the 1.x
  workflow canvas (it wrote 1.x YAML). Workflows is a YAML editor with inline
  validation and the workflow profile until the 2.0 canvas lands; the
  validation panel shows V-codes, lines and warnings.
- Insight **Runs** page: live run list with workflow/status filters, the
  journal timeline per agent (loop turns, tool calls with effects and
  arguments, decisions with their source, judge verdicts, supervisor
  actions), an approvals inbox with forms built from the human agent's
  outputs and tool approve / edit / reject, pause / resume / abort / steer,
  start a run, pin a run as a fixture and replay a workflow's fixtures
  (SAME/DIFF). Dev API: `POST /dev/runs/{id}/pin`, `POST /dev/runs/replay`.
- Insight **Specs** page (now the home page): spec list and template, a
  markdown editor, Analyse with a token estimate shown first, a plan review
  (summary, cost, notes, validation errors, tasks, per-file diff with partial
  apply) and the derived task board with stale/blocked badges that links to
  each task's target. `spec status --json` tasks carry their `target`.
- Reloading the project in a running server no longer fails with "Table …
  already defined" after a model-version toggle dropped a class (a model
  definition now replaces its stale table).
- Insight **workflow canvas** (2.0): the YAML stays the single source
  (comments kept); Start / agent / End nodes laid out left to right with
  positions in `.tuvl/layout/<workflow>.json`; agent cards by engine and
  determinism class (pending cards hollow) with contract, budget and
  guardrail chips and one outlet per signal (unrouted required signals in
  red); Decide (rules, model, threshold) and Loop (tools, approvals, caps)
  summaries; a task-oriented palette; a panel with Contract (types, upstream
  suggestions), Engine (decide rules table, YAML block), Budget, Governance and
  Routes tabs; Change engine that keeps the contract; drag to route; live
  validation of the unsaved draft (`POST /dev/validate/draft`) with issue badges,
  red input ports and a summary bar; a data-flow overlay. Canvas and YAML tabs
  edit the same text.
- Insight developer controls (dev mode only): breakpoints on canvas cards
  (dev runs pause before the agent; kept in `.tuvl/breakpoints.json`), step
  mode (pause before every agent and every loop tool call), view and edit the
  next agent's inputs while paused, then continue; Probe runs one agent live
  with hand-entered inputs. Dev API: `/dev/breakpoints/{wf}`,
  `/dev/runs/start {step}`, `/dev/runs/{id}/context`, `/dev/agents/probe`.
- Insight change-engine check: after swapping an agent's engine on the canvas,
  "Check vs fixtures" replays the workflow's pinned fixtures against the
  unsaved draft with that agent executing (everything else from the
  recording) and shows SAME/DIFF per fixture plus the before → after
  validate profile; "Generate stub" runs codegen when the new engine is
  `code`. Dev API: `POST /dev/change/check`; `run_test(live_agents=...)`.
- Insight judge editor (Artifacts page): model, rubric and thresholds; ask
  the judge on a subject (live, against the unsaved draft); mark its verdict
  right or wrong to save the subject under `fixtures/judge/` and append it to
  the calibration set; run calibration and see agreement vs `min_agreement`.
  Rules artifact editor with "which rule matches" for a sample context. Both
  can be created from a template. AI Models: `type: decision` models
  (provider `laya | jev | litellm`). Dev API: `/dev/judges/try`,
  `/dev/judges/calibrate`, `/dev/judges/label`, `/dev/rules/try`;
  `judges.from_document`, `judge_with`, `calibrate_with`.
- Insight browser tests (Playwright, `ui/e2e`): every page renders the
  `tuvl init --sample` project without page or console errors, plus the
  authoring exit flow — new workflow → change engine → save → breakpoint run →
  continue → pin fixture → edit rules → check SAME / DIFF. Run locally with
  `scripts/insight-smoke.sh`; CI runs it on every push (runner Postgres/Redis,
  no image pulls).
- MCP export: workflows with `trigger.mcp.enabled` are tools on
  `/mcp` (streamable HTTP, stateless). Tool input schema = `trigger.input`;
  the output is an envelope clients can validate — `{outcome: "completed",
  run_id, end, status, body}` (body typed as one of the 2xx end bodies) or
  `{outcome: "pending", run_id, state, approval?}` for a run that waits on a
  person/approval or outlives the call window; error ends are `isError`.
  `readOnlyHint` / `destructiveHint` derived from the workflow's strongest
  effect. Same Biscuit auth as REST plus scope `mcp:connect`; `tools/list`
  shows only the workflows the caller may run; a progress notification per
  started agent.
- OpenAPI from contracts: each trigger route documents its path/query
  parameters or JSON body from `trigger.input`, one response per end status
  with the body's schema (`outputs.<end>.type`, else inferred from the body
  template), and `202` with the run descriptor. `tuvl.contract.surface` is the
  shared source for OpenAPI and MCP.
- `tuvl ship` Helm chart: `split: true` deploys an `api` Deployment (the
  Service target) and a `worker` Deployment (`tuvl run --role worker`) scaled
  independently (`worker.replicaCount`, `worker.concurrency`); the default is
  one `--role all` Deployment. Pods carry `app.kubernetes.io/component`.
- Documentation for 2.0: a rewritten agentic manual and new subsystem docs —
  `runtime`, `engines`, `decide`, `loop-and-tools`, `judges`, `spec-driven`,
  `insight`, `api`, `cli` — plus updated `auth` and `observability`, README
  and contributor `AGENTS.md`. The 1.x step-kind docs are removed.
- Checkpoints keep real values (they are the resume state) and are redacted
  on export.

- Per-signal outputs: an agent's `outputs` may be keyed by its
  routed signals; data flow and type checks then use the set for the signal on
  each edge, so a consumer can require a value only some outcomes produce.

### Fixed (validate)

- The report table no longer strips `[...]` from messages (Rich read them as
  markup); 2.0 artifact references count as uses.

- `tuvl validate` silently ignored YAML files that failed to parse (their
  `kind` could not be read, so no validator owned them); they are now
  reported as errors, with a fix-it hint for the common flow-mapping mistakes.
- `POST /api/insight/spectrum/stream` — the Spectrum trace as Server-Sent
  Events (one `step` event per node, then a `final` event), with a
  `stop_after_step_id` breakpoint. Powers the Workflow Canvas test mode and the
  Spectrum page, which previously streamed over gRPC.
- `POST /dev/evaluate-trace` now reports `model_used`.

### Fixed

- `tuvl test` crashed in a plain install: `aiosqlite` (its in-memory
  database) was only a dev dependency. Found by smoke-testing the built wheels
  against the examples.
- `tuvl.lock` no longer records the litellm version for `provider: litellm`
  decision models — the model id is the pin, and a litellm patch release made
  `tuvl lock --check` fail on every fresh install. Plugin providers still pin
  their package version.
- `tuvl spec analyse` on an existing workflow merges instead of rebuilding it:
  the trigger (path, method, input), ends (status, body), metadata and other
  settings are kept, the plan only adds agents, models, new ends and policy
  keys it doesn't set, and differences are reported as notes. A workflow the
  plan doesn't change isn't rewritten, so its comments survive. Found by
  analysing the examples, where it would have changed public API paths and
  dropped response bodies.
- Spec analysis no longer writes artifacts (and tasks) planned for agents that
  are already implemented without them, and no longer turns an acceptance
  bullet that restates an example (case or punctuation aside) into a task.
- Spec analysis sees the project's vector collections (and is told they are
  not models), keeps acceptance tasks to bullets written in the spec's
  Acceptance section, drops tasks for artifacts the plan doesn't provide, and
  doesn't add an unpinned model next to a pinned one.
- `tuvl spec status`: an off-spine agent used as a loop tool is covered when
  its loop is (a test can't reach it any other way).
- `tuvl spec status`: an `end == 'x'` assertion task is covered by a Test's
  `expect.end: x`, and an agent a Test's run waits at (`expect.wait`) counts
  as covered.
- Replays and `tuvl test` runs no longer stop at Insight breakpoints or step
  mode (`TestRunner.dev_pause`).
- Insight "Change engine": switching to decide / human / loop moves the
  `default` route to the first new signal (those engines have none), drops
  `effect` for engines other than code/tool, and writes the preset's outputs
  when the agent has none.
- `POST /api/insight/spectrum` returned the final state without masking
  `secure: true` fields (per-step snapshots were masked); it is now masked.
- Clearing a user's name or phone to an empty string from Insight now
  persists (REST `PATCH /auth/admin/users/{id}` applies explicit empty
  strings; the gRPC codec dropped them).

### Version map

| Surface | Package | Version | Channel |
|---|---|---|---|
| Engine | `tuvl` | `2.0.0` | PyPI |
| Developer portal | `tuvl-insight` | `2.0.0` | PyPI (via `tuvl[standard]`) |
| TypeScript SDK | `@tuvl/client` | `2.0.0` | npm |
| Documentation | `tuvl.dev`, `tuvl.io/docs` | `2.0.0` | GitHub Pages |
| Marketing site | `tuvl.io` | `v2.0.0` | static |

---

## [1.0.1] — 2026-08-03

Patch release: correctness fixes only. No API or schema changes.

### Added

- Model fields accept generated-default literals `default: now`
  (`timestamp` / `timestamptz`) and `default: today` (`date`), mirroring the
  existing `default: uuid4`. Use them to opt a field into a server-generated
  value on create.

### Fixed

- **Nullable `uuid` / `date` / `timestamp` / `timestamptz` fields no longer
  receive a silent generated value on create.** The model loader treated *any*
  default-less field of these types as "auto-generate a value", so a nullable
  `uuid` foreign key persisted a dangling random UUID, and a default-less
  `date` / `timestamp` persisted `today` / `now()` instead of `NULL` — on every
  row created through ModelOp CRUD (raw-SQL nodes were unaffected, which is why
  the corruption stayed hidden). Generation is now limited to the cases where it
  is safe and intended: a `uuid` **primary key**, and the `created_at` /
  `updated_at` audit-column convention. Every other default-less field of these
  types now persists as `NULL` (or is required when declared `required: true`).

  **Migration.** Any field that (perhaps unintentionally) relied on the old
  implicit `today` / `now` / `uuid` now stores `NULL` — which is almost always
  what the YAML author already believed was happening, given these fields carry
  no default. To keep a generated value, opt in explicitly: `default: uuid4`,
  `default: now`, or `default: today` — or, for timestamps, rename the column to
  `created_at` / `updated_at`. `uuid` primary keys and `created_at` /
  `updated_at` are unchanged.

- CLI commands no longer emit the `Settings loaded` startup log. Constructing
  `Settings` on import logged at info level, so quick commands (e.g.
  `tuvl --version`) printed a log line alongside their output. The log moved to
  server boot — `tuvl run` / `tuvl dev` still log it — and the routine
  `Project config loaded` line dropped to debug.

## [1.0.0] — 2026-07-26

First official stable release. Establishes the SemVer baseline and folds in
scheduled triggers, an API-security hardening pass, and a full internal
quality sweep. Everything below is the state of the engine at first publish.

### Added

- **Scheduled workflow triggers.** `spec.trigger.schedule: "<5-field cron>"`
  (UTC, 1-minute granularity) runs a workflow on a schedule, alongside or
  instead of an HTTP `path`. Skip-on-miss (no catch-up); single-fire across
  workers via a Postgres advisory lock; scheduled runs execute with no
  principal. Knob `TUVL_SCHEDULER_ENABLED` (default on); single-tenant only.
  Authorable from the Insight workflow trigger panel.
- **Global CRUD kill-switch.** `spec.api.expose_model_crud: false` in
  `.tuvl/system.yaml` (`SystemConfig`) unmounts every auto-generated
  `/models/*` route — the routes are absent, not merely denied — leaving only
  workflow APIs. `TUVL_EXPOSE_MODEL_CRUD` overrides the file. Editable from the
  Insight Settings "API Access" section. Restart-required.
- **Per-model group ACLs.** `spec.access.{read,write,delete}_groups` on a
  `ModelDefinition` pins CRUD tiers to IAM groups: a caller needs the scope
  **and** group membership (`iam:admin` bypasses). Undeclared tiers cascade
  restrictively (write→read, delete→write).
- **`spec.trigger.public: true`** opts a workflow into anonymous access (see
  the default-deny change below). Workflow manifests expose the `public` flag;
  `@tuvl/client`'s `WorkflowManifest` carries it.
- `tuvl validate` / `tuvl ship` now discover config by the same recursive,
  `kind`-dispatched walk the runtime uses — YAML in any subdirectory and `.yml`
  files are validated, and `EmbeddingRegistry`/`EmbeddingConfig`,
  `CollectionRegistry`/`CollectionConfig`, `RedisConfig`, and
  `FederationProvider` are now checked. An unknown `kind:` is a validation error.
- `tuvl stream-watch --timeout` (default: no read-timeout on the event stream).

### Changed

- **BREAKING — workflow triggers deny by default in production.** Every trigger
  route (REST, the versioned `/{schema_version}/run/{name}` route, and gRPC
  `RunWorkflow`) now requires a valid bearer token by default, even when the
  workflow declares no `required_scope`/`required_group`. Anonymous access is an
  explicit opt-in via `spec.trigger.public: true`. Dev mode (`tuvl dev`) exempts
  unscoped workflows so quickstarts run tokenless. **Migration:** add
  `spec.trigger.public: true` to any workflow (e.g. user-registration endpoints)
  that must remain callable without a token; combining it with
  `required_scope`/`required_group` is a validation error.
- **Default engine port is now `8885`** (was `8000`) for `tuvl dev` and
  `tuvl run`, the Insight portal (`http://localhost:8885/insight/`), the `tuvl
  ship` container/Helm chart, and the Insight dev proxy. 8885 spells "TUVL" on a
  phone keypad (T·U·V=8, L=5) and avoids the crowded 8000/8080/8888 range.
  Override with `--port` or `TUVL_DEV_PORT`.
- **Semantic Versioning.** Releases follow SemVer (`MAJOR.MINOR.PATCH`):
  breaking changes bump the major, features the minor, fixes the patch.
- The REST and gRPC IAM surfaces are now thin adapters over one transport-neutral
  service (`core/auth/iam_service.py`) — identical behavior on both transports
  (see Security).
- Models without an explicit `spec.datasource` bind to the primary `DataSource`
  (the one with `metadata.primary: true`) rather than a hardcoded name, so a
  scaffolded project creates its tables out of the box.
- `tuvl ship` Dockerfiles float the engine to the latest release the project's
  `pyproject.toml` allows (`uv sync --upgrade-package tuvl`); other dependencies
  stay lockfile-pinned. `tuvl ship` warns when a kept `Chart.yaml` `appVersion`
  has gone stale against the build version.

### Fixed

- Diagnostic logs now go to **stderr**, keeping stdout clean for actual command
  output. Previously the log stream (including the import-time "Settings loaded"
  line) was written to stdout and interleaved with results — e.g. `tuvl --version`
  emitted a JSON log line before the version, breaking anything parsing it.
- `tuvl db generate-rls` / `check-rls` now load the target project, so tenant
  model tables appear (previously only system tables were emitted).
- A `ModelOp` database error now returns the workflow's declared error-route
  response instead of an unhandled 500, and no longer leaks the SQL statement or
  bound parameters into the error body.
- HumanInTheLoop suspension works under the `tuvl dev` session key (the dev
  principal is now a stable UUID).
- The Insight workflow editor no longer corrupts version-pinned
  `context: {models: [...]}` to `'[object Object]'` on save.
- `tuvl validate --strict` no longer false-warns on projects that legitimately
  have no custom node files.
- Per-model / rotated LLM credentials take effect immediately (resolved as
  per-call parameters instead of a process-global env mutation).
- The engine fails fast at startup when `TUVL_BISCUIT_PRIVATE_KEY` is missing in
  production, rather than booting and erroring on the first request.
- Crashed workers no longer leave phantom agent runs in the operator API
  (Redis mirror entries expire).

### Security

- Production default-deny on workflow triggers; per-model group ACLs; the CRUD
  kill-switch (above).
- REST/gRPC IAM parity closed two gRPC-only gaps: login now runs the same
  constant-time dummy-hash verify (no user enumeration by response timing), and
  `GetMe`/`RefreshToken` run the same token authorization checks (a token
  missing its `user()` fact is now rejected instead of silently accepted).
  Federation path sanitization and file I/O aligned to the hardened REST path.
- Anonymous callers get a uniform `UNAUTHENTICATED` for unknown, disabled, and
  auth-required workflows across REST and gRPC, so the workflow namespace can't
  be probed without a credential.
- No signing key, no boot (above); the dev-mode superuser shortcut hard-exits
  if enabled in a `TUVL_ENV=production` process.

### Internal / Quality

- Transport-neutral IAM service (REST+gRPC), a single shared step-execution loop
  behind `run()`/`run_streaming()`, and the 6,724-line Insight workflow editor
  split into a reviewable module tree. Validator and runtime share one config
  discovery + document normalization path. New: a protobuf wire-conformance test
  tying the hand-written TS gRPC codecs to the `.proto` definitions, an Insight
  vitest suite + CI job, and a scoped mypy ratchet. 648 engine tests + 133 UI
  tests.

### Version map

| Surface | Package | Version | Channel |
|---|---|---|---|
| Engine | `tuvl` | `1.0.0` | PyPI |
| Developer portal | `tuvl-insight` | `1.0.0` | PyPI (via `tuvl[standard]`) |
| TypeScript SDK | `@tuvl/client` | `1.0.0` | npm |
| Documentation | `tuvl.io/docs` | `1.0.0` | GitHub Pages |
| Marketing site | `tuvl.io` | `v1.0.0` | static |
