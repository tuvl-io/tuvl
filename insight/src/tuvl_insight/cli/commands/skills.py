"""``tuvl skills`` — the coding-agent skills shipped with this tuvl version."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax

console = Console()
skills_app = typer.Typer(
    help="Install and refresh coding-agent skills (.agents/skills).", no_args_is_help=True
)
_MARK = {"create": "[green]+[/green]", "update": "[yellow]~[/yellow]", "delete": "[red]-[/red]"}


@skills_app.command("update")
def update_cmd(
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    diff: Annotated[
        bool, typer.Option("--diff", help="Show what would change; write nothing")
    ] = False,
) -> None:
    """Refresh AGENTS.md and the skills to this tuvl version (removes the retired skills)."""
    from tuvl_insight import skills

    project = project_dir.resolve()
    changes = skills.plan(project)
    for change in changes:
        console.print(f"{_MARK[change.action]} {escape(str(change.path))}")
        if diff and change.diff:
            console.print(Syntax(change.diff, "diff", word_wrap=True))
    if not changes:
        console.print("[green]Skills are up to date.[/green]")
        return
    if diff:
        console.print(f"\n{len(changes)} change(s) — run without --diff to apply.")
        return
    skills.apply(project, changes)
    console.print(f"\n{len(changes)} change(s) applied ({len(skills.names())} skills).")
