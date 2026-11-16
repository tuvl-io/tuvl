"""``tuvl prune runs`` — remove finished runs, and what belongs to them, older than an age."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

console = Console()


def _runs(n: int) -> str:
    return f"{n} run{'' if n == 1 else 's'}"


def prune_runs_cmd(
    older_than: Annotated[
        str,
        typer.Option(
            "--older-than", help="Remove runs that ended longer ago than this (30d, 12h)."
        ),
    ],
    status: Annotated[
        list[str] | None,
        typer.Option(
            "--status",
            "-s",
            help="Only runs that ended this way (repeatable): completed, failed, aborted, compensated.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Say what would be removed and remove nothing.")
    ] = False,
    with_uploads: Annotated[
        bool,
        typer.Option(
            "--with-uploads", help="Also delete the files the runs' triggers stored in a store."
        ),
    ] = False,
    batch: Annotated[
        int, typer.Option("--batch", min=1, max=5000, help="Runs removed per commit.")
    ] = 500,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="Project directory.", file_okay=False)
    ] = Path("."),
) -> None:
    """Remove runs that ended before --older-than, with their journal, checkpoints, approvals, webhook
    deliveries and drafts. A run that is queued, running, waiting or paused is never removed.
    Uploaded files stay unless --with-uploads. Try --dry-run first."""
    from tuvl.contract.schema import duration_seconds
    from tuvl.runtime.models import TERMINAL_STATUSES

    try:
        age = timedelta(seconds=duration_seconds(older_than))
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    chosen = set(status or ())
    unfinished = sorted(chosen - TERMINAL_STATUSES)
    if unfinished:
        console.print(
            f"[red]A run that is {escape(', '.join(unfinished))} can still change; only ended runs "
            f"({', '.join(sorted(TERMINAL_STATUSES))}) can be removed.[/red]"
        )
        raise typer.Exit(2)

    from tuvl.cli.commands.db import _ensure_project_loaded

    _ensure_project_loaded(project_dir)
    from sqlalchemy.ext.asyncio import AsyncSession

    from tuvl.core.datasources import postgres
    from tuvl.runtime import retention

    async def delete_object(store: str, key: str) -> None:
        from tuvl.storage import get_store

        await get_store(store).delete(key)

    async def run() -> retention.Pruned:
        engine = postgres._engines_rw[postgres._PRIMARY_DS]
        try:
            async with AsyncSession(engine) as session:
                return await retention.prune(
                    session,
                    older_than=age,
                    statuses=chosen or None,
                    batch=batch,
                    dry_run=dry_run,
                    delete_object=delete_object if with_uploads else None,
                )
        finally:
            await engine.dispose()

    result = asyncio.run(run())
    detail = ", ".join(f"{n} {t.removeprefix('tuvl_system_')}" for t, n in result.rows.items() if n)
    if dry_run:
        console.print(f"Would remove {_runs(result.runs)} that ended more than {older_than} ago.")
        if detail:
            console.print(f"  with {detail}")
        return
    console.print(f"Removed {_runs(result.runs)} that ended more than {older_than} ago.")
    if detail:
        console.print(f"  with {detail}")
    if with_uploads:
        console.print(f"  and {result.uploads} uploaded file(s)")
    if result.skipped:
        console.print(
            f"[yellow]Kept {_runs(result.skipped)} whose uploaded files could not be deleted "
            "right now; run it again later.[/yellow]"
        )
