"""``tuvl storage`` — check and browse the stores declared in ``kind: Storage`` files."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

console = Console()

storage_app = typer.Typer(
    name="storage",
    help="Check and browse blob stores (S3, Azure Blob, GCS, local).",
    no_args_is_help=True,
)

_DirOpt = Annotated[
    Path, typer.Option("--project-dir", "-d", help="Project directory.", file_okay=False)
]


def _load(project_dir: Path) -> dict[str, Any]:
    """The project's ``kind: Storage`` documents; workflows are not loaded, so a broken one cannot hide a store."""
    import yaml

    from tuvl.core.config import use_project
    from tuvl.core.config_loader import walk_yaml_files
    from tuvl.storage import STORE_REGISTRY, ConfigError, register_store_from_dict

    project = project_dir.resolve()
    use_project(str(project))
    STORE_REGISTRY.clear()
    for path in walk_yaml_files(project):
        try:
            documents = [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]
        except (OSError, yaml.YAMLError):
            continue
        for data in documents:
            if data.get("kind") != "Storage":
                continue
            try:
                register_store_from_dict(data, path.name)
            except ConfigError as exc:
                console.print(f"[red]{path.name}:[/red] {escape(str(exc))}")
                raise typer.Exit(1) from exc
    return dict(STORE_REGISTRY)


def _missing_env(spec: Any) -> list[str]:
    from tuvl.storage import required_env

    return [var for var in required_env(spec) if var not in os.environ]


async def probe(name: str) -> list[tuple[str, str | None]]:
    from tuvl.storage import get_store
    from tuvl.storage.check import check_store

    store = get_store(name)
    try:
        return await check_store(store)
    finally:
        await store.aclose()


@storage_app.command("check")
def check_cmd(
    name: Annotated[str | None, typer.Argument(help="Store name (default: every store).")] = None,
    project_dir: _DirOpt = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Write, read and delete a probe object in each store (read-only stores: list only)."""
    stores = _load(project_dir)
    if name is not None and name not in stores:
        console.print(
            f"[red]No Storage named '{name}'.[/red] Declared: {', '.join(stores) or 'none'}"
        )
        raise typer.Exit(1)
    chosen = {name: stores[name]} if name else stores
    report: dict[str, Any] = {}
    for store_name, spec in chosen.items():
        missing = _missing_env(spec)
        if missing:
            report[store_name] = {"ok": False, "provider": spec.provider, "missing": missing}
            continue
        try:
            steps = asyncio.run(probe(store_name))
        except Exception as exc:
            steps = [("open", f"{type(exc).__name__}: {exc}")]
        report[store_name] = {
            "ok": all(err is None for _, err in steps),
            "provider": spec.provider,
            "steps": [{"step": s, "error": e} for s, e in steps],
        }
    healthy = all(info["ok"] for info in report.values())
    if as_json:
        typer.echo(json.dumps({"healthy": healthy, "stores": report}, indent=2))
    else:
        if not report:
            console.print("No Storage declared.")
        for store_name, info in report.items():
            mark = "[green]✓[/green]" if info["ok"] else "[red]✗[/red]"
            detail = ""
            if info.get("missing"):
                detail = "[red]missing " + ", ".join(info["missing"]) + "[/red]"
            else:
                detail = " ".join(
                    f"{s['step']}{'' if s['error'] is None else ' [red]' + escape(s['error']) + '[/red]'}"
                    for s in info["steps"]
                )
            console.print(f"{mark} [bold]{store_name}[/bold] ({info['provider']}) — {detail}")
    if not healthy:
        raise typer.Exit(1)


@storage_app.command("ls")
def ls_cmd(
    name: Annotated[str, typer.Argument(help="Store name.")],
    prefix: Annotated[str, typer.Argument(help="Key prefix.")] = "",
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=1000)] = 100,
    project_dir: _DirOpt = Path("."),
) -> None:
    """List objects in a store."""
    from tuvl.storage import StorageError, get_store

    stores = _load(project_dir)
    if name not in stores:
        console.print(f"[red]No Storage named '{name}'.[/red]")
        raise typer.Exit(1)

    async def run() -> None:
        store = get_store(name)
        try:
            listing = await store.list(prefix, limit)
        finally:
            await store.aclose()
        for blob in listing.items:
            console.print(f"{blob.size:>12}  {blob.modified or '-':<20}  {escape(blob.key)}")
        if listing.cursor:
            console.print(f"[dim]… more (limit {limit})[/dim]")

    try:
        asyncio.run(run())
    except StorageError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
