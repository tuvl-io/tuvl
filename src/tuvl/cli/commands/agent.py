"""``tuvl agent`` — the coding agents on this machine and the one tuvl may run for you."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from tuvl_insight.coding_agents import (
    AGENTS,
    VERIFIED_ON,
    Settings,
    detect,
    get,
    read_settings,
    runner,
    write_settings,
)

console = Console()
agent_app = typer.Typer(
    help="List the coding agents on this machine and choose the one tuvl may run.",
    no_args_is_help=True,
)

ProjectOpt = Annotated[Path, typer.Option("--project-dir", "-d", help="tuvl project directory")]


def agents_table(selected: str = "") -> Table:
    found = {d.spec.id: d for d in detect()}
    table = Table(box=None, padding=(0, 2), show_edge=False)
    for col in ("", "agent", "version", "tuvl can run it", "path"):
        table.add_column(col, no_wrap=col != "path")
    for spec in AGENTS:
        d = found.get(spec.id)
        mark = "[green]●[/green]" if spec.id == selected else ""
        if d is None:
            table.add_row(mark, f"[dim]{spec.name}[/dim]", "[dim]not found[/dim]", "", "")
            continue
        cap = runner.capability(spec.id)
        runs = {"write": "[green]yes[/green]", "review": "[cyan]review only[/cyan]"}.get(
            cap, "[yellow]detected, not yet[/yellow]"
        )
        table.add_row(mark, f"[bold]{spec.name}[/bold]", escape(d.version), runs, d.path)
    return table


@agent_app.command("list")
def list_cmd(
    project_dir: ProjectOpt = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable list")] = False,
) -> None:
    """Show every known coding agent, whether it is installed, and which one is selected."""
    settings = read_settings(project_dir.resolve())
    selected = settings.agent if settings else "none"
    if as_json:
        found = {d.spec.id: d for d in detect()}
        rows = [
            {
                "id": s.id,
                "name": s.name,
                "installed": s.id in found,
                "version": found[s.id].version if s.id in found else "",
                "path": found[s.id].path if s.id in found else "",
                "verified": s.verified,
                "capability": runner.capability(s.id),
                "headless": s.headless,
                "selected": s.id == selected,
            }
            for s in AGENTS
        ]
        typer.echo(
            json.dumps({"selected": selected, "verified_on": VERIFIED_ON, "agents": rows}, indent=2)
        )
        return
    console.print(agents_table(selected))
    console.print(
        f"\nSelected: [bold]{selected}[/bold]   [dim](headless flags checked {VERIFIED_ON})[/dim]"
    )


@agent_app.command("use")
def use_cmd(
    agent: Annotated[str, typer.Argument(help="Agent id (see `tuvl list agents`) or `none`")],
    project_dir: ProjectOpt = Path("."),
    force: Annotated[
        bool, typer.Option("--force", help="Select an agent that was not found on this machine")
    ] = False,
    command: Annotated[
        str | None,
        typer.Option(
            "--command",
            help="For `generic`: the command to run. It reads the prompt on stdin and edits the "
            "working directory, e.g. --command 'mytool --yes'.",
        ),
    ] = None,
) -> None:
    """Choose the coding agent tuvl may run for you, or `none`. Stored in `.tuvl/agent.yaml`."""
    project = project_dir.resolve()
    choice = agent.strip().lower()
    argv: tuple[str, ...] = ()
    if choice == "generic":
        import shlex

        argv = tuple(shlex.split(command or ""))
        if not argv:
            console.print("[red]generic needs --command 'the command to run'.[/red]")
            raise typer.Exit(2)
        console.print(
            "[yellow]generic cannot limit what the command touches[/yellow]: tuvl only checks afterwards "
            "which files changed."
        )
    elif choice != "none":
        spec = get(choice)
        if spec is None:
            console.print(
                f"[red]Unknown agent '{escape(agent)}'.[/red] Choose: "
                + ", ".join(a.id for a in AGENTS)
                + ", none."
            )
            raise typer.Exit(2)
        installed = {d.spec.id for d in detect()}
        if choice not in installed and not force:
            console.print(
                f"[red]{spec.name} was not found on PATH.[/red] Install it, or use --force."
            )
            raise typer.Exit(1)
        cap = runner.capability(choice)
        if cap == "":
            console.print(
                f"[yellow]{spec.name} is detected but tuvl cannot run it yet[/yellow] "
                "(its headless flags are not checked). Selecting it records the choice only."
            )
        elif cap == "review":
            console.print(
                f"[yellow]{spec.name} can review code through tuvl but cannot draft specs or build tasks yet[/yellow] "
                "(its headless mode cannot limit writes to one folder). Choose a writer for those."
            )
    current = read_settings(project) or Settings()
    write_settings(project, Settings(current.initials, choice, current.consent, argv))
    from tuvl_insight import audit

    audit.emit(
        project, "agent.selected", command="use agent", detail=choice, refs={"agent": choice}
    )
    console.print(f"Coding agent: [bold]{choice}[/bold]")


@agent_app.command("test")
def test_cmd(
    project_dir: ProjectOpt = Path("."),
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask before spending tokens")
    ] = False,
) -> None:
    """Check the selected agent starts, then run it once on a trivial task in a scratch folder."""
    import tempfile

    settings = read_settings(project_dir.resolve())
    if settings is None or settings.agent == "none":
        console.print("No coding agent selected. Choose one with `tuvl use agent <agent>`.")
        raise typer.Exit(1)
    try:
        adapter = runner.adapter_for(settings.agent, settings.command)
        binary = runner.binary_for(settings.agent, settings.command)
    except runner.AgentError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]✓[/green] {settings.agent} at {escape(binary)}")
    if not adapter.can_write:
        _test_review_only(adapter, binary, yes)
        return
    if not yes and not typer.confirm(
        "Run it once on a trivial task in a scratch folder? (uses a few tokens of your own account)",
        default=True,
    ):
        return
    prompt = (
        "Create a file named hello.txt in the current directory containing exactly the word ok. "
        "Do not create or change any other file. Reply with one short sentence."
    )
    with tempfile.TemporaryDirectory(prefix="tuvl-agent-test-") as scratch:
        work = Path(scratch)
        result = runner.run_agent(
            adapter,
            binary,
            prompt,
            workdir=work,
            limits=runner.Limits(timeout=120, budget_usd=0.25),
        )
        files = sorted(p.name for p in work.iterdir())
        wrote = (work / "hello.txt").read_text().strip() if (work / "hello.txt").is_file() else ""
    if not result.ok:
        console.print(f"[red]✗ the agent failed:[/red] {escape(result.error)}")
        raise typer.Exit(1)
    cost = f", ${result.cost_usd:.3f}" if result.cost_usd is not None else ""
    if wrote != "ok" or files != ["hello.txt"]:
        console.print(
            f"[red]✗ it answered but did not do the task[/red] (files: {files}, hello.txt: {wrote!r})"
        )
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/green] it edits files in its working directory ({result.seconds:.1f}s{cost})"
    )


def _test_review_only(adapter: runner.Adapter, binary: str, yes: bool) -> None:
    """An agent tuvl only uses to review: ask it one question and check it answers."""
    if not yes and not typer.confirm(
        "Ask it one question in a scratch folder? (uses a few tokens of your own account)",
        default=True,
    ):
        return
    import tempfile

    with tempfile.TemporaryDirectory(prefix="tuvl-agent-test-") as scratch:
        result = runner.run_agent(
            adapter,
            binary,
            "Reply with the single word: ok",
            workdir=Path(scratch),
            limits=runner.Limits(timeout=120, read_only=True),
        )
        left = sorted(p.name for p in Path(scratch).iterdir())
    if not result.ok:
        console.print(f"[red]✗ the agent failed:[/red] {escape(result.error)}")
        raise typer.Exit(1)
    if "ok" not in result.text.lower() or left:
        console.print(
            f"[red]✗ unexpected answer or it changed files[/red] ({escape(result.text[:80])!r}, {left})"
        )
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/green] it answers ({result.seconds:.1f}s); tuvl uses it to review, not to write"
    )
