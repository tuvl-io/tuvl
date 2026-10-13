# CLI reference

Every read command supports `--json` with a stable, documented schema (coding agents parse it;
`/dev/schema/cli` serves the schemas in dev mode). `-d/--project-dir` defaults to the current
directory everywhere.

## Project

| Command | Options | |
|---|---|---|
| `tuvl init [NAME]` | `--sample`, `--preset gemini\|openai\|anthropic\|ollama`, `--decision-model laya\|none`, `-y/--yes`, `--multi-tenant`, `--no-ai-skills` | Scaffold a project. A preset writes `llms/default.yaml` with a current model id, reading the key from the environment. `--sample` is a ticket-triage project (spec, TaskPlan, tests, lock) that passes the CI gate as created. `--decision-model laya` adds `llms/laya.yaml`, `LAYA_API_*` in `.env` and a `compose.yaml` that runs laya-serve locally (asked interactively, default no). `-y` accepts every default |
| `tuvl validate` | `--strict`, `--json` | Contracts, data flow, types, policy, artifacts, lock; prints each workflow's determinism profile and worst-case tokens. `--strict` fails on warnings. Ends with **needs models**: every model the workflows use, its provider and the environment variables it reads (or "no LLM required"); V028 = unknown model reference, V029 = undeclared credentials (an error at ship time) |
| `tuvl codegen [WORKFLOW]` | `--check` | Schemas, code-agent stubs (body-preserving merge), prompt placeholders, spec tests. `--check` writes nothing and exits 1 on drift |
| `tuvl lock` | `--check`, `--mcp/--no-mcp` | Pin model ids, artifact hashes, judge/decision models and MCP tool schemas in `tuvl.lock` |
| `tuvl doctor` | `--live`, `--json` | Are the project's models configured? Lists each model, the variables it needs and which are missing (exit 1 if any). `--live` makes one minimal call per model (a few tokens; Laya/Jev: their `/health`). A production server runs the same check at startup and refuses to start on a missing variable; `/health` reports each model's readiness |
| `tuvl test` | `--live`, `--allow-uncertain`, `--strict`, `-k/--only TEXT`, `--json` | Run `Test` documents offline (`--live` lets unmocked model/tool agents call out). Results are PASS, **STUBBED** (expectations met only through unmocked `pending` placeholders — not counted) or FAIL; `--strict` (CI) also fails on stubbed results and untrusted judge calibration |
| `tuvl skills update` | `--diff` | Refresh `AGENTS.md` and `.agents/skills/` to this tuvl version (removes 1.x skills; mirrors to `.claude/skills/`) |

## Specs

| Command | Options | |
|---|---|---|
| `tuvl spec verify [SPEC_FILE…]` | `--strict`, `--no-tests`, `--json` | Does the implementation match the spec? Zero tokens; rules R001–R010 and a traceability matrix (step → agent → tests). Exits 1 on a mismatch (`--strict`: also on unfinished work) |
| `tuvl spec new NAME` | `--owner`, `--workflow` | Write `specs/<name>.md` from the template |
| `tuvl spec check [SPEC_FILE…]` | `--strict`, `--json` | Is the spec complete and consistent? Zero tokens; rules S001–S016 with line numbers, plus P001–P005 on its task list ([spec-driven](spec-driven.md#2-check-it)). Exits 1 on a blocking finding (`--strict`: on warnings too) |
| `tuvl spec status [SPEC_FILE]` | `--strict`, `--no-tests`, `--json` | Derived task statuses. `--strict` exits 1 unless every live task is done and none is blocked or stale |
| `tuvl spec diff [SPEC_FILE]` | `--json` | Stale tasks and spec sections no task covers |

## Servers

| Command | Options | |
|---|---|---|
| `tuvl dev` | `-p/--port` (8885), `--auto-login`, `--show-key`, `--no-browser`, `-H/--allow-host` | Dev server with Insight and the dev API, reloading on YAML changes. The session key is stored in `.tuvl/.dev-session` (0600); `--show-key` prints it |
| `tuvl run` | `-p/--port`, `--host`, `-w/--workers`, `--role all\|api\|worker`, `-c/--concurrency` (8), `-H/--allow-host` | Production server. `api` serves HTTP and runs synchronous requests inline; `worker` executes the queue |
| `tuvl mcp` | `--allow-write` | Dev tools for coding agents over MCP (stdio); `--allow-write` adds `codegen_write`, `run_workflow`, `pin_fixture`. Refuses production |

## Runs

These talk to a running server: `-u/--url` (`TUVL_URL`, default `http://localhost:8885`) and
`-t/--token` (`TUVL_TOKEN`, needs `runs:read`).

| Command | Options | |
|---|---|---|
| `tuvl runs list` | `-w/--workflow`, `-s/--status`, `-n/--limit`, `--json` | Recent runs, newest first |
| `tuvl runs get RUN_ID` | | One run as JSON |
| `tuvl runs tail [RUN_ID]` | `-w/--workflow`, `-p/--payload JSON` | Follow a run's events until it ends (or start one with `--workflow`); exits 1 if it fails |
| `tuvl runs pin RUN_ID` | `-n/--name` | Export the journal to `tests/fixtures/<workflow>/<name>.journal.json` |
| `tuvl runs replay FIXTURES…` | `--json` | Re-run pinned journals offline; fail if the end or output differ |

## Shipping and operations

| Command | Options | |
|---|---|---|
| `tuvl ship` | `-t/--tag`, `--no-build`, `--push`, `--force`, `--strict`, `--with-laya/--without-laya` | Gate (validate, no `pending` agents — V010 is an error at ship time, current lock), then `.dockerignore` as an **allowlist** (only models, workflows, agents, artifacts, datasources, llms, federation, config, lock and `.tuvl/system.yaml` / `telemetry.yaml` enter the image; specs, task lists, tests, agent rules and skills, compose files and anything else stay out — V027 refuses development files), `deploy/Dockerfile` (pins the locked tuvl version) and a Helm chart (`split: true` for api + worker Deployments; a Laya Deployment + Service, with `LAYA_API_BASE` set for the engine, when a model uses `provider: laya` or with `--with-laya` — `laya.enabled` in values.yaml) and `docker build` |
| `tuvl keys generate` | `-w/--write`, `-f/--force` | A persistent Ed25519 key for Biscuit tokens (`TUVL_BISCUIT_PRIVATE_KEY`) |
| `tuvl db generate-rls` | `-o/--out` | Idempotent row-level-security SQL for tenant-scoped tables |
| `tuvl db check-rls` | | Exit 1 if a tenant-scoped table lacks its RLS policy |

## The CI gate

```bash
tuvl spec check && tuvl validate --strict && tuvl codegen --check && tuvl lock --check && tuvl test --strict && tuvl spec verify --strict && tuvl spec status --strict
```
