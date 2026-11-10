"""The action-first command tree: ``tuvl <verb> <thing>``.

Each verb makes one kind of promise:

* ``generate`` writes files (spends tokens only where a coding agent is involved)
* ``lint`` runs static rules over what you wrote — offline, zero tokens
* ``verify`` asks whether one thing is still in step with another, and exits 1 on drift
* ``check`` asks whether something is reachable and working right now
* ``review`` is a judgement of code
* ``approve`` / ``reject`` / ``reopen`` are human decisions, recorded in the audit ledger
* ``list`` / ``show`` only read

"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from tuvl.extras import toolchain_installed

ProjectOpt = Annotated[Path, typer.Option("--project-dir", "-d", help="tuvl project directory")]

#: (verb, panel, help) in the order the help screen shows them.
VERBS: tuple[tuple[str, str, str], ...] = (
    ("create", "Setup", "Create a blank file from a template."),
    ("import", "Setup", "Bring existing material into the project."),
    ("use", "Setup", "Choose what tuvl works with."),
    ("set", "Setup", "Change a project setting."),
    ("upgrade", "Setup", "Bring an older project up to date."),
    ("register", "Setup", "Publish the project to the public catalogue."),
    ("update", "Setup", "Refresh files tuvl installed into the project."),
    (
        "generate",
        "Generate",
        "Write files: specs, tasks, code, UI guidelines, the lock, keys, SQL.",
    ),
    ("approve", "Decide", "Approve something (recorded in the audit ledger)."),
    ("reject", "Decide", "Send something back with a reason."),
    ("reopen", "Decide", "Withdraw an approval so it can be edited."),
    ("renumber", "Decide", "Give an idea a new number and rewrite what derives from it."),
    ("lint", "Quality", "Static rules over what you wrote. Offline, zero tokens."),
    ("verify", "Quality", "Is one thing still in step with another? Exits 1 on drift."),
    ("check", "Quality", "Is it reachable and working right now? Touches the live environment."),
    ("review", "Quality", "A judgement of code (rules and ruff; --agent adds a second opinion)."),
    ("run", "Run", "Run a workflow or the project's tests."),
    ("serve", "Run", "Start a server: the production app or the MCP development tools."),
    ("follow", "Run", "Follow something live until it ends."),
    ("pin", "Run", "Keep a run as a test fixture."),
    ("replay", "Run", "Re-run pinned runs against the project."),
    ("list", "Inspect", "List ideas, specs, tasks, runs, agents or stored objects."),
    (
        "show",
        "Inspect",
        "Show one idea, task, run, event or the project, the audit log or the drift.",
    ),
    ("export", "Admin", "Write something out as JSON or CSV."),
    ("prune", "Admin", "Delete old files and old agent memories."),
)
_INSTALL = escape("uv tool install 'tuvl[standard]'")


def _hint(name: str) -> Callable[[], None]:
    def hint() -> None:
        Console(stderr=True).print(
            f"[red]`tuvl {name}` is part of the developer toolchain, which is not installed.[/red]\n"
            f"Install it with: [bold]{_INSTALL}[/bold]"
        )
        raise typer.Exit(2)

    return hint


class _Group:
    def __init__(self, verb: str, help_text: str) -> None:
        self.verb = verb
        self.toolchain = toolchain_installed()
        self.app = typer.Typer(name=verb, help=help_text, no_args_is_help=True)

    def add(
        self,
        thing: str,
        fn: Callable[..., Any],
        *,
        toolchain: bool = True,
        hidden: bool = False,
        help_text: str | None = None,
    ) -> None:
        if toolchain and not self.toolchain:
            self.app.command(
                thing,
                hidden=True,
                context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
            )(_hint(f"{self.verb} {thing}"))
            return
        self.app.command(thing, hidden=hidden, help=help_text)(fn)


# ── wrappers: a new name over an old command with some options fixed ──────────────────────────


def verify_lock(
    project_dir: ProjectOpt = Path("."),
    mcp: Annotated[
        bool, typer.Option("--mcp/--no-mcp", help="List MCP servers live to verify tool schemas")
    ] = True,
) -> None:
    """Is tuvl.lock up to date? Exits 1 if it is missing or stale."""
    from tuvl_insight.cli.commands.lock import lock_cmd

    lock_cmd(project_dir=project_dir, check=True, mcp=mcp)


def verify_ui_spec(
    project_dir: ProjectOpt = Path("."),
    no_tests: Annotated[
        bool, typer.Option("--no-tests", help="Do not run the tests: implemented counts as done")
    ] = False,
    base_url: Annotated[
        str, typer.Option("--base-url", help="The backend URL in the snippets")
    ] = "",
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable result")] = False,
) -> None:
    """Is ui/specs/ what `tuvl generate ui-spec` would write now? Exits 1 if not."""
    from tuvl_insight.cli.commands.ui import DEFAULT_BASE, spec_cmd

    spec_cmd(
        project_dir=project_dir,
        workflow=None,
        include_partial=False,
        no_tests=no_tests,
        base_url=base_url or DEFAULT_BASE,
        check_only=True,
        as_json=as_json,
    )


def run_workflow(
    name: Annotated[str, typer.Argument(help="The workflow to start on a running server")],
    payload: Annotated[
        str, typer.Option("--payload", "-p", help="The workflow's input, as JSON")
    ] = "{}",
    url: Annotated[
        str, typer.Option("--url", "-u", envvar="TUVL_URL", help="tuvl server base URL")
    ] = "http://localhost:8885",
    token: Annotated[
        str | None, typer.Option("--token", envvar="TUVL_TOKEN", help="Access token")
    ] = None,
) -> None:
    """Start a run of a workflow on a running server and follow it. Exits 1 if it fails."""
    from tuvl.cli.commands.runs import tail_cmd

    tail_cmd(run_id=None, workflow=name, payload=payload, url=url, token=token)


def follow_run(
    run_id: Annotated[str, typer.Argument(help="The run to follow")],
    url: Annotated[
        str, typer.Option("--url", "-u", envvar="TUVL_URL", help="tuvl server base URL")
    ] = "http://localhost:8885",
    token: Annotated[
        str | None, typer.Option("--token", envvar="TUVL_TOKEN", help="Access token")
    ] = None,
) -> None:
    """Follow a run's events until it ends. Exits 1 if it fails."""
    from tuvl.cli.commands.runs import tail_cmd

    tail_cmd(run_id=run_id, workflow=None, payload="{}", url=url, token=token)


# ── registration ──────────────────────────────────────────────────────────────────────────────


def register(app: typer.Typer) -> None:
    from tuvl.cli.commands import db, doctor, keys, memory, run, runs, storage

    toolchain = toolchain_installed()
    tc: Any = None
    if toolchain:
        import types

        from tuvl_insight.cli.commands import (
            agent,
            audit,
            codegen,
            devtelemetry,
            lock,
            mcp,
            project,
            replay,
            review,
            schema,
            skills,
            spec,
            test,
            ui,
            validate,
        )

        tc = types.SimpleNamespace(
            agent=agent,
            audit=audit,
            codegen=codegen,
            devtelemetry=devtelemetry,
            lock=lock,
            mcp=mcp,
            project=project,
            replay=replay,
            review=review,
            schema=schema,
            skills=skills,
            spec=spec,
            test=test,
            ui=ui,
            validate=validate,
        )

    def fn(module: str, name: str) -> Callable[..., Any]:
        """A toolchain command function, or a stand-in when the toolchain is absent."""
        return getattr(getattr(tc, module), name) if toolchain else _hint(f"{module} {name}")

    groups = {verb: _Group(verb, help_text) for verb, _, help_text in VERBS}

    g = groups
    g["create"].add("spec", fn("spec", "new_cmd"))
    g["import"].add("idea", fn("spec", "import_cmd"))
    g["use"].add("agent", fn("agent", "use_cmd"))
    g["set"].add("project", fn("project", "set_cmd"))
    g["set"].add("dev-telemetry", fn("devtelemetry", "set_cmd"))
    g["upgrade"].add("project", fn("project", "init_cmd"))
    g["register"].add("project", fn("project", "register_cmd"))
    g["register"].add("developer", fn("devtelemetry", "register_cmd"))
    g["update"].add("skills", fn("skills", "update_cmd"))

    g["generate"].add("spec", fn("spec", "gen_cmd"))
    g["generate"].add("tasks", fn("spec", "tasks_cmd"))
    g["generate"].add("code", fn("codegen", "codegen_cmd"))
    g["generate"].add("ui-spec", fn("ui", "spec_cmd"))
    g["generate"].add("lock", fn("lock", "lock_cmd"))
    g["generate"].add("key", keys.generate_cmd, toolchain=False)
    g["generate"].add("rls", db.generate_rls, toolchain=False)

    g["approve"].add("spec", fn("spec", "approve_cmd"))
    g["reject"].add("spec", fn("spec", "reject_cmd"))
    g["reopen"].add("spec", fn("spec", "reopen_cmd"))
    g["renumber"].add("idea", fn("spec", "renumber_cmd"))

    g["lint"].add("project", fn("validate", "validate_cmd"))
    g["lint"].add("spec", fn("spec", "check_cmd"))
    g["verify"].add("spec", fn("spec", "verify_cmd"))
    g["verify"].add("lock", verify_lock)
    g["verify"].add("ui-spec", verify_ui_spec)
    g["verify"].add("rls", db.check_rls, toolchain=False)
    g["verify"].add("audit", fn("audit", "verify_cmd"))
    g["check"].add("system", doctor.doctor_cmd, toolchain=False)
    g["check"].add("agent", fn("agent", "test_cmd"))
    g["check"].add("dev-telemetry", fn("devtelemetry", "check_cmd"))
    g["check"].add("storage", storage.check_cmd, toolchain=False)
    g["review"].add("code", fn("review", "review_cmd"))

    g["run"].add("workflow", run_workflow, toolchain=False)
    g["run"].add("tests", fn("test", "test_cmd"))
    g["serve"].add("app", run.serve_cmd, toolchain=False)
    g["serve"].add("mcp", fn("mcp", "mcp_cmd"))
    g["follow"].add("run", follow_run, toolchain=False)
    g["pin"].add("run", runs.pin_cmd, toolchain=False)
    g["replay"].add("runs", fn("replay", "replay_cmd"))

    g["list"].add("ideas", fn("spec", "ideas_cmd"))
    g["list"].add("specs", fn("spec", "list_cmd"))
    g["list"].add("tasks", fn("spec", "status_cmd"))
    g["list"].add("branches", fn("spec", "branches_cmd"))
    g["list"].add("runs", runs.list_cmd, toolchain=False)
    g["list"].add("agents", fn("agent", "list_cmd"))
    g["list"].add("objects", storage.ls_cmd, toolchain=False)
    g["show"].add("idea", fn("spec", "idea_cmd"))
    g["show"].add("task", fn("spec", "task_cmd"))
    g["show"].add("run", runs.get_cmd, toolchain=False)
    g["show"].add("project", fn("project", "info_cmd"))
    g["show"].add("audit", fn("audit", "log_cmd"))
    g["show"].add("dev-telemetry", fn("devtelemetry", "show_cmd"))
    g["list"].add("team-activity", fn("devtelemetry", "team_cmd"))
    g["show"].add("event", fn("audit", "show_cmd"))
    g["show"].add("drift", fn("spec", "diff_cmd"))
    g["export"].add("audit", fn("audit", "export_cmd"))
    g["export"].add("schema", fn("schema", "export_cmd"), hidden=True)
    g["prune"].add("traces", fn("audit", "prune_cmd"))
    g["prune"].add("memory", memory.prune_memory_cmd, toolchain=False)

    for verb, panel, _ in VERBS:
        app.add_typer(groups[verb].app, name=verb, rich_help_panel=panel)

    from tuvl.cli.commands.iam import iam_app

    app.add_typer(iam_app, name="iam", rich_help_panel="Admin")
