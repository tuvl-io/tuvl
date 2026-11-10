# Core and Insight

tuvl is two packages that release together and give you one command, `tuvl`.

| | `tuvl` (the engine) | `tuvl-insight` (the tooling) |
|---|---|---|
| What it is | Parses the project's YAML, connects to the database, runs workflows, serves the API | Everything that exists to develop a project |
| Install | `uv tool install tuvl` | `uv tool install "tuvl[standard]"` (adds this and the production extras) |
| Commands | `serve app`, `run workflow`, `list runs`, `show run`, `follow run`, `pin run`, `list objects`, `check system`, `check storage`, `generate key`, `generate rls`, `verify rls`, `prune memory` | `init`, `dev`, `ship`, `deploy`, `lint project`, `generate code/spec/tasks/lock/ui-spec`, `run tests`, `replay runs`, `review code`, `serve mcp`, the spec and idea commands, ... |
| HTTP | The API, auth, the live-state `/dev` routes (schedules, memory, breakpoints, judges, storage), the artifact routes | The file-based `/dev` API (files, specs, jobs, validation, canvas, probes), the code-assist routes and the Insight UI |
| Where it runs | Production and development | Development only |

On a bare install every tooling command still exists and answers with how to get it
(`` `tuvl lint project` is part of the developer toolchain, which is not installed. ``, exit 2). That is the only difference
you can see; `tuvl --help` lists the same verbs.

## Who may import whom

- `tuvl` never imports `tuvl_insight`, except at four seams: the verb tree and the one-word commands in `tuvl.cli`,
  `tuvl.core.dev.extensions` (mounts the tooling's routes in dev mode), `tuvl.doctor` (registers the tooling's check
  groups) and `tuvl.main` (mounts the Insight UI). `tuvl.extras.toolchain_installed()` is "`tuvl_insight` is importable"
  (`TUVL_TOOLCHAIN=0/1` overrides it).
- `tuvl_insight` may import only the engine modules listed, with a reason each, in `tests/test_boundary.py`, and no
  underscore names. What both sides need lives in `tuvl.contract` (the document model, `facts.py`, `lockfile.py`).
- A test enforces both. `python scripts/split_report.py` prints the sizes and every import that crosses the line.

## Tests

`pytest tests` passes on an install without `tuvl-insight`: `tests/insight/` is then not collected and tests marked
`requires_insight` are skipped. CI runs the suite twice, with and without it, and checks the contents of both wheels
(`tests/test_wheel_contents.py`).

## Python imports

Module paths changed in 2.1: `tuvl.validate`, `tuvl.spec`, `tuvl.codegen`, `tuvl.lock` and the other tooling packages are
`tuvl_insight.<name>`. They were never public API; see the changelog for the list.
