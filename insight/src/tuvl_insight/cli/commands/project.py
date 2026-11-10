"""``tuvl project`` — the project's identity (``.tuvl/project.yaml``)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from tuvl.core.project_info import (
    EDITABLE_FIELDS,
    ProjectInfoError,
    create_project,
    default_identity,
    id_is_valid,
    load_project,
    record_registration,
    regenerate_id,
    registration_payload,
    registration_state,
    update_project,
)

console = Console()

project_app = typer.Typer(
    name="project",
    help="Show and edit the project's identity: id, author, type, version.",
    no_args_is_help=True,
)

_DirOpt = Annotated[
    Path,
    typer.Option("--project-dir", "-d", help="Project directory.", file_okay=False),
]


def _require(project_dir: Path) -> dict:
    record = load_project(project_dir)
    if record is None or not record.get("id"):
        console.print("[red]No .tuvl/project.yaml here.[/red] Run `tuvl upgrade project`.")
        raise typer.Exit(1)
    if not id_is_valid(record["id"]):
        console.print(
            f"[red]The project id {record['id']!r} is malformed or was edited by hand.[/red] "
            "Restore it from version control, or run `tuvl upgrade project --new-id`."
        )
        raise typer.Exit(1)
    return record


@project_app.command("info")
def info_cmd(
    project_dir: _DirOpt = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Print the project record."""
    record = _require(project_dir)
    if as_json:
        typer.echo(json.dumps(record, indent=2, default=str))
        return
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    for key in ("id", "name", "author", "email", "type", "version", "license", "repo"):
        table.add_row(key, str(record.get(key, "") or "—"))
    reg = record.get("registry")
    state = {
        "unregistered": "not registered",
        "current": "up to date",
        "outdated": "changed since last registration — run `tuvl register project`",
        "id_changed": "id differs from the registered one — run `tuvl register project`",
    }.get(registration_state(record), "")
    table.add_row(
        "registry", f"{reg.get('url')} · {reg.get('registered_at')} · {state}" if reg else state
    )
    console.print(table)
    if record.get("description"):
        from rich.markdown import Markdown

        console.print()
        console.print(Markdown(str(record["description"])))


@project_app.command("set")
def set_cmd(
    field: Annotated[str, typer.Argument(help=f"One of: {', '.join(EDITABLE_FIELDS)}")],
    value: Annotated[
        str,
        typer.Argument(help="New value. For long text: @FILE reads a file, - reads stdin."),
    ],
    project_dir: _DirOpt = Path("."),
) -> None:
    """Update one field. The description is Markdown."""
    _require(project_dir)
    if value == "-":
        value = sys.stdin.read()
    elif value.startswith("@"):
        try:
            value = Path(value[1:]).read_text()
        except OSError as exc:
            console.print(f"[red]Cannot read {value[1:]}: {exc}[/red]")
            raise typer.Exit(2) from exc
    try:
        update_project(project_dir, {field: value})
    except ProjectInfoError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(f"[green]✓[/green] {field} updated")


@project_app.command("init")
def init_cmd(
    project_dir: _DirOpt = Path("."),
    name: Annotated[
        str | None, typer.Option("--name", help="Defaults to the directory name.")
    ] = None,
    new_id: Annotated[
        bool,
        typer.Option(
            "--new-id", help="Replace the id (copied project) and clear its registration."
        ),
    ] = False,
) -> None:
    """Create project.yaml for a project that predates it. Never changes an existing id."""
    existing = load_project(project_dir)
    if new_id:
        if existing is None:
            console.print("[red]No .tuvl/project.yaml here.[/red]")
            raise typer.Exit(1)
        if existing.get("registry") and not typer.confirm(
            "This project is registered; a new id starts a separate listing. Continue?"
        ):
            raise typer.Exit(1)
        console.print(
            f"[green]✓[/green] Project id [bold]{regenerate_id(project_dir)['id']}[/bold]"
        )
        return
    if existing is not None and existing.get("id"):
        console.print(f"Already initialised: [bold]{existing['id']}[/bold]")
        return
    who = default_identity(project_dir)
    record = create_project(
        project_dir,
        name=name or Path(project_dir).resolve().name,
        author=who["author"],
        email=who["email"],
    )
    console.print(f"[green]✓[/green] Project id [bold]{record['id']}[/bold]")


_REGISTER_PATH = "/api/register-project"


@project_app.command("register")
def register_cmd(
    project_dir: _DirOpt = Path("."),
    nest: Annotated[
        str | None, typer.Option("--nest", help="Registry URL (TUVL_NEST_URL).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """List this project in the public catalogue (shows the payload first)."""
    import httpx

    from tuvl_insight.cli.nest import NestError, ensure_token, forget_token, nest_url

    record = _require(project_dir)
    import importlib.metadata

    try:
        engine_version = importlib.metadata.version("tuvl")
    except importlib.metadata.PackageNotFoundError:
        engine_version = ""
    payload = registration_payload(record, tuvl_version=engine_version)

    url = nest_url(nest)
    console.print(f"Registering with [bold]{url}[/bold]:")
    console.print_json(json.dumps(payload))
    if not yes and not typer.confirm("Send this?", default=False):
        raise typer.Exit(1)

    try:
        token = ensure_token(url)
        for attempt in (0, 1):
            res = httpx.post(
                f"{url}{_REGISTER_PATH}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if res.status_code == 401 and attempt == 0:
                forget_token(url)
                token = ensure_token(url, relogin=True)
                continue
            break
    except (NestError, httpx.HTTPError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if res.status_code >= 400:
        detail = res.json().get("detail", res.text) if res.content else res.status_code
        console.print(f"[red]The Nest rejected the registration: {detail}[/red]")
        raise typer.Exit(1)

    record_registration(
        project_dir,
        url,
        when=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    console.print(f"[green]✓[/green] Registered {record['id']}")
