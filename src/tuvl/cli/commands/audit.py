"""``tuvl audit`` — the local record of what was done to this project."""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import re
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from tuvl_insight.audit import ledger, trace

console = Console()
audit_app = typer.Typer(
    help="The local audit ledger: what was done to this project, by whom, in order.",
    no_args_is_help=True,
)
ProjectOpt = Annotated[Path, typer.Option("--project-dir", "-d", help="tuvl project directory")]
_RELATIVE = re.compile(r"^(\d+)([smhdw])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_MARK = {"ok": "[green]✓[/green]", "refused": "[yellow]![/yellow]", "failed": "[red]✗[/red]"}


def _since(value: str | None) -> str | None:
    """``2d`` / ``12h`` / an ISO date or time, as an ISO prefix comparable with event timestamps."""
    if value is None:
        return None
    m = _RELATIVE.match(value.strip())
    if m:
        at = _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2)])
        return at.strftime("%Y-%m-%dT%H:%M:%S")
    if not re.match(r"^\d{4}-\d{2}-\d{2}", value):
        raise typer.BadParameter(
            "use 2d / 12h / 30m, or a date like 2026-10-19", param_hint="--since"
        )
    return value


def _print(event: dict[str, Any]) -> None:
    who = (event.get("actor") or {}).get("initials") or (event.get("actor") or {}).get("git") or "?"
    entities = " ".join(event.get("entities", []))
    detail = f" — {escape(str(event['detail']))}" if event.get("detail") else ""
    console.print(
        f"{_MARK.get(event.get('result', ''), ' ')} [dim]{str(event.get('ts', ''))[:19]}[/dim] "
        f"{escape(str(who)):<5} [bold]{event.get('kind', '')}[/bold] {escape(entities)}{detail}"
    )


@audit_app.command("log")
def log_cmd(
    project_dir: ProjectOpt = Path("."),
    entity: Annotated[
        str | None,
        typer.Option(
            "--entity", help="An idea, spec or task id; an idea covers its specs and tasks"
        ),
    ] = None,
    actor: Annotated[str | None, typer.Option("--actor", help="Initials or name")] = None,
    kind: Annotated[
        str | None, typer.Option("--kind", help="Event kind or prefix, e.g. spec.")
    ] = None,
    since: Annotated[str | None, typer.Option("--since", help="2d, 12h, 30m or a date")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Newest N events (0 = all)")] = 50,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing new events")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable events")] = False,
) -> None:
    """Show recorded events, oldest first."""
    project = project_dir.resolve()
    chosen = ledger.select(
        ledger.read_events(project),
        entity=entity,
        actor_match=actor,
        kind=kind,
        since=_since(since),
    )
    if limit:
        chosen = chosen[-limit:]
    if as_json:
        typer.echo(json.dumps({"events": chosen}, indent=2))
        return
    if not chosen and not follow:
        console.print("[dim]No events recorded yet.[/dim]")
    for event in chosen:
        _print(event)
    if follow:
        try:
            for event in ledger.follow(project):
                if ledger.select([event], entity=entity, actor_match=actor, kind=kind):
                    _print(event)
        except KeyboardInterrupt:
            raise typer.Exit() from None


@audit_app.command("show")
def show_cmd(
    ref: Annotated[
        str, typer.Argument(help="An event id (or its first characters) or an entity id")
    ],
    project_dir: ProjectOpt = Path("."),
) -> None:
    """Show one event in full, or every event about an idea, spec or task."""
    events = ledger.read_events(project_dir.resolve())
    exact = [e for e in events if str(e.get("id", "")).startswith(ref.upper()) and len(ref) >= 6]
    chosen = exact or ledger.select(events, entity=ref)
    if not chosen:
        console.print(f"[red]Nothing recorded for '{escape(ref)}'.[/red]")
        raise typer.Exit(1)
    for event in chosen:
        typer.echo(json.dumps(event, indent=2))


@audit_app.command("verify")
def verify_cmd(project_dir: ProjectOpt = Path(".")) -> None:
    """Check the ledger's hash chain, schema and order. Exit 1 if it was altered."""
    result = ledger.verify(project_dir.resolve())
    if result.ok:
        console.print(f"[green]✓[/green] {result.events} event(s), chain intact")
        return
    console.print(
        f"[red]✗ {escape(result.problem)}[/red] at {result.where} (after {result.events} good event(s))"
    )
    raise typer.Exit(1)


@audit_app.command("export")
def export_cmd(
    project_dir: ProjectOpt = Path("."),
    fmt: Annotated[str, typer.Option("--format", help="json or csv")] = "json",
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write here instead of stdout")
    ] = None,
    entity: Annotated[str | None, typer.Option("--entity")] = None,
) -> None:
    """Export the ledger (optionally one entity's history) as JSON or CSV."""
    if fmt not in ("json", "csv"):
        raise typer.BadParameter("json or csv", param_hint="--format")
    events = ledger.select(ledger.read_events(project_dir.resolve()), entity=entity)
    if fmt == "json":
        text = json.dumps({"events": events}, indent=2)
    else:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "ts", "actor", "command", "kind", "entities", "result", "detail"])
        for e in events:
            who = (e.get("actor") or {}).get("git") or (e.get("actor") or {}).get("initials", "")
            writer.writerow(
                [
                    e.get("id"),
                    e.get("ts"),
                    who,
                    e.get("command"),
                    e.get("kind"),
                    " ".join(e.get("entities", [])),
                    e.get("result"),
                    e.get("detail", ""),
                ]
            )
        text = buf.getvalue()
    if output:
        output.write_text(text)
        console.print(f"wrote {len(events)} event(s) to {escape(str(output))}")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


@audit_app.command("prune")
def prune_cmd(
    project_dir: ProjectOpt = Path("."),
    older_than: Annotated[
        float, typer.Option("--older-than", help="Days; the trace only, never the ledger")
    ] = 30,
) -> None:
    """Delete trace files (prompts, agent output) older than N days. The ledger is kept."""
    removed = trace.prune(project_dir.resolve(), older_than)
    console.print(f"removed {removed} trace file(s)")
