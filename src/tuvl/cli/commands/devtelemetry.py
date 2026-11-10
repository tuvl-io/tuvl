"""Developer telemetry: share the team's development actions through a collector (optional; ``.tuvl/dev-telemetry.yaml``)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from tuvl_insight import devtelemetry

console = Console()
_DirOpt = Annotated[Path, typer.Option("--project-dir", "-d", help="tuvl project directory")]


def set_cmd(
    server: Annotated[
        str | None,
        typer.Argument(help="https address of the team's collector (omit to keep the current one)"),
    ] = None,
    off: Annotated[bool, typer.Option("--off", help="Stop sending (the settings stay)")] = False,
    identity: Annotated[
        str | None,
        typer.Option(
            "--identity",
            help="What the events say about you: initials (default) or full (name and email)",
        ),
    ] = None,
    token_env: Annotated[
        str | None,
        typer.Option(
            "--token-env",
            help=f"Environment variable that overrides your registered token (default {devtelemetry.DEFAULT_TOKEN_ENV})",
        ),
    ] = None,
    commands: Annotated[
        bool | None,
        typer.Option("--commands/--no-commands", help="Also send one event per command run"),
    ] = None,
    forget: Annotated[
        bool,
        typer.Option(
            "--forget", help="Delete your registered token for this collector from this machine"
        ),
    ] = False,
    rotate: Annotated[
        bool,
        typer.Option(
            "--rotate-token", help="Get a new token from the collector; the old one stops working"
        ),
    ] = False,
    project_dir: _DirOpt = Path("."),
) -> None:
    """Turn developer telemetry on, off or change it. Commit the file; your token stays in your own config folder."""
    project = project_dir.resolve()
    config = devtelemetry.load(project) or devtelemetry.Config()
    if server is None and not (
        off or forget or rotate or identity or token_env or commands is not None
    ):
        console.print(
            "[red]Nothing to change.[/red] Give the collector's address, or --off, --identity, --forget."
        )
        raise typer.Exit(2)
    if rotate:
        try:
            devtelemetry.rotate(project)
        except devtelemetry.TelemetryError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1) from exc
        console.print("[green]✓ New token kept[/green]; the old one no longer works.")
    if forget:
        if not config.server:
            console.print("[red]No collector is configured, so there is no token to forget.[/red]")
            raise typer.Exit(2)
        devtelemetry.forget(config.server)
        console.print(
            f"Forgot your token for {escape(config.server)}. Register again with [bold]tuvl register developer[/bold]."
        )
    if server is not None:
        config.server = server
        config.enabled = True
    if off:
        config.enabled = False
    if identity is not None:
        config.identity = identity
    if token_env is not None:
        config.token_env = token_env
    if commands is not None:
        config.commands = commands
    if server is None and not (off or identity or token_env or commands is not None):
        return
    try:
        path = devtelemetry.save(project, config)
    except devtelemetry.TelemetryError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    state = "[green]on[/green]" if config.enabled else "[yellow]off[/yellow]"
    console.print(f"Developer telemetry is {state}  [dim]{path.relative_to(project)}[/dim]")
    if config.enabled:
        token, source = devtelemetry.token_for(config)
        console.print(
            f"  collector {escape(config.server)}; the events say [bold]{config.identity}[/bold]"
        )
        if not token:
            console.print(
                "  Next: [bold]tuvl register developer[/bold] to get your own token (ask your team for the join code)."
            )
        console.print(
            "  Commit the file so the team shares it. Check it with [bold]tuvl check dev-telemetry[/bold]."
        )


def register_cmd(
    name: Annotated[
        str | None, typer.Option("--name", help="Your name (default: your git name)")
    ] = None,
    email: Annotated[
        str | None, typer.Option("--email", help="Your email (default: your git email)")
    ] = None,
    code: Annotated[
        str | None,
        typer.Option("--code", help="The team's join code, from whoever runs the collector"),
    ] = None,
    project_dir: _DirOpt = Path("."),
) -> None:
    """Register yourself at the team's collector and keep your token (it sends and reads for you)."""
    from tuvl_insight import gitutil

    project = project_dir.resolve()
    config = devtelemetry.load(project)
    if config is None or not config.server:
        console.print(
            "[red]No collector is configured.[/red] Run [bold]tuvl set dev-telemetry <address>[/bold] first."
        )
        raise typer.Exit(2)
    name = name or typer.prompt(
        "  Your name", default=gitutil.config_get(project, "user.name") or ""
    )
    email = email or typer.prompt(
        "  Your email", default=gitutil.config_get(project, "user.email") or ""
    )
    code = code or typer.prompt("  The team's join code", hide_input=True)
    try:
        developer = devtelemetry.register(
            config.server, name=name, email=email, team_code=code, token_env=config.token_env
        )
    except devtelemetry.TelemetryError as exc:
        console.print(f"[red]Could not register:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]✓ Registered[/green] as [bold]{escape(str(developer.get('name', name)))}[/bold] "
        f"({escape(str(developer.get('email', email)))}) in team [bold]{escape(str(developer.get('team', '')))}[/bold]."
    )
    console.print(
        f"  Your token is in [dim]{devtelemetry.credentials_path()}[/dim] (only you can read it)."
    )


def show_cmd(
    project_dir: _DirOpt = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable status")] = False,
) -> None:
    """Show whether developer telemetry is on, where it goes, who you are there and what is waiting to be sent."""
    data = devtelemetry.status(project_dir.resolve())
    if as_json:
        print(json.dumps(data, indent=2))
        return
    if not data["configured"]:
        console.print(
            "Developer telemetry is [yellow]not configured[/yellow]. Turn it on with [bold]tuvl set dev-telemetry <address>[/bold]."
        )
        return
    console.print(
        f"Developer telemetry is {'[green]on[/green]' if data['enabled'] else '[yellow]off[/yellow]'}"
    )
    console.print(f"  collector  {escape(data['server']) or '-'}")
    mine = data["local"]
    shown = mine["initials"] or "-"
    if data["identity"] == "full" and mine["name"]:
        shown += f" · {mine['name']} <{mine['email']}>"
    console.print(
        f"  identity   events say: [bold]{escape(shown)}[/bold]   "
        f"(from git: {escape(mine['name'] or '-')} <{escape(mine['email'] or '-')}>; initials from tuvl init)"
    )
    console.print(
        f"             identity {data['identity']}   commands {'yes' if data['commands'] else 'no'}"
    )
    who = data["developer"]
    if data["token_source"] == "registered" and who:
        console.print(
            f"  you        [green]{escape(str(who.get('name', '')))}[/green] ({escape(str(who.get('email', '')))}), team {escape(str(who.get('team', '')))}"
        )
    elif data["token_source"] == "env":
        console.print(f"  you        token from {data['token_env']}")
    else:
        console.print(
            "  you        [yellow]not registered[/yellow]: run [bold]tuvl register developer[/bold]"
        )
    when = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["last_ok"]))
        if data["last_ok"]
        else "never"
    )
    console.print(
        f"  queued     {data['queued']} event(s) waiting   sent {data['sent']}   last delivery {when}"
    )
    if data["last_error"]:
        console.print(f"  [red]last error: {escape(data['last_error'])}[/red]")


def check_cmd(
    flush: Annotated[
        bool, typer.Option("--flush", help="Also send everything that is queued")
    ] = False,
    project_dir: _DirOpt = Path("."),
) -> None:
    """Ask the collector who your token is; exits 1 when it does not accept it."""
    project = project_dir.resolve()
    try:
        who = devtelemetry.whoami(project)
    except devtelemetry.TelemetryError as exc:
        console.print(f"[red]✗ the collector did not accept your token:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    console.print(
        f"[green]✓ the collector knows you[/green] as {escape(str(who.get('name', '')))} ({escape(str(who.get('team', '')))})"
    )
    if flush:
        result = devtelemetry.flush(project, timeout=30.0)
        console.print(f"  sent {result['sent']} queued event(s); {result['queued']} left")
        if result["error"]:
            console.print(f"  [red]{escape(result['error'])}[/red]")
            raise typer.Exit(1)


def team_cmd(
    project_dir: _DirOpt = Path("."),
    actor: Annotated[
        str | None, typer.Option("--actor", help="Only this developer (name or initials)")
    ] = None,
    entity: Annotated[
        str | None, typer.Option("--entity", help="Only events about this idea, spec or task")
    ] = None,
    kind: Annotated[
        str | None, typer.Option("--kind", help="Only this event kind, e.g. task.failed")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many events")] = 30,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable events")] = False,
) -> None:
    """The team's recent development actions, from the collector (what everyone on the project did)."""
    project = project_dir.resolve()
    try:
        events = devtelemetry.team_events(
            project, actor=actor, entity=entity, kind=kind, limit=limit
        )
    except devtelemetry.TelemetryError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    if as_json:
        print(json.dumps(events, indent=2))
        return
    for e in events:
        who = (e.get("developer") or {}).get("name") or (e.get("actor") or {}).get("initials", "")
        mark = "[green]✓[/green]" if e.get("result") == "ok" else "[red]✗[/red]"
        console.print(
            f"{mark} {str(e.get('ts', ''))[:19]} {escape(str(who)):<14} {e.get('kind', ''):<22} "
            f"{escape(', '.join(e.get('entities') or []))} [dim]{escape(str(e.get('detail', ''))[:60])}[/dim]"
        )
    if not events:
        console.print("[dim]No events yet.[/dim]")
