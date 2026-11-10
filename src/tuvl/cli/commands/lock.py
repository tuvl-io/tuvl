"""``tuvl generate lock`` — write or verify ``tuvl.lock`` (spec §17.8)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from tuvl.core.config import use_project

console = Console()


def fresh_lock(project: Path, *, live_mcp: bool) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``(fresh lock document, lock file contents)`` for *project*."""
    from tuvl.contract.registry import TESTS, WORKFLOWS
    from tuvl.core.config_loader import load_all_configs
    from tuvl_insight import lock

    load_all_configs(project)
    inv = lock.inventory(dict(WORKFLOWS), TESTS.values())
    hashes = asyncio.run(lock.mcp_hashes(inv)) if live_mcp and inv.mcp else None
    current = lock.read(project)
    return lock.merge_unlisted(lock.build(inv, hashes), current), current


def lock_cmd(
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    check: Annotated[
        bool, typer.Option("--check", help="Write nothing; exit 1 if tuvl.lock is missing or stale")
    ] = False,
    mcp: Annotated[
        bool,
        typer.Option("--mcp/--no-mcp", help="List MCP servers live to pin/verify tool schemas"),
    ] = True,
) -> None:
    """Pin model ids, artifact hashes, judge/decision models and MCP schemas."""
    from tuvl_insight import lock

    project = project_dir.resolve()
    use_project(str(project))
    live = mcp
    try:
        fresh, current = fresh_lock(project, live_mcp=live)
    except Exception as exc:
        console.print(f"[red]tuvl generate lock:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    if check:
        if current is None:
            console.print(
                f"[red][V024] {lock.LOCK_FILE} is missing[/red] — run `tuvl generate lock`"
            )
            raise typer.Exit(1)
        changed = lock.differences(current, fresh, live_mcp=live)
        for line in changed:
            console.print(f"  [yellow]~[/yellow] {escape(line)}")
        if changed:
            console.print(
                f"[red][V024] {lock.LOCK_FILE} is out of date[/red] — review, then run `tuvl generate lock`"
            )
            raise typer.Exit(1)
        console.print(f"[green]{lock.LOCK_FILE} is up to date.[/green]")
        return
    unlisted = [
        f"{s}.{t}" for s, tools in fresh["mcp"].items() for t, h in tools.items() if h == "unlisted"
    ]
    (project / lock.LOCK_FILE).write_text(lock.render(fresh))
    console.print(
        f"wrote [bold]{lock.LOCK_FILE}[/bold]: {len(fresh['models'])} model(s), "
        f"{len(fresh['artifacts'])} artifact(s), {len(fresh['judges'])} judge(s), "
        f"{sum(len(t) for t in fresh['mcp'].values())} MCP tool(s)"
    )
    for item in unlisted:
        console.print(
            f"[yellow]⚠[/yellow] {escape(item)} not pinned — run `tuvl generate lock` with the MCP server reachable"
        )
