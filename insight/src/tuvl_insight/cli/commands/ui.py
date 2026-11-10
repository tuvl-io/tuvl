"""``tuvl generate ui-spec`` — guidelines for building a UI on the implemented backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from tuvl_insight import audit
from tuvl_insight.uispec import DEFAULT_BASE, SPECS_DIR, check, collect, generate

console = Console()
ui_app = typer.Typer(
    help="Guidelines for the people (and agents) who build the UI.", no_args_is_help=True
)

ProjectOpt = Annotated[Path, typer.Option("--project-dir", "-d", help="tuvl project directory")]


@ui_app.command("spec")
def spec_cmd(
    project_dir: ProjectOpt = Path("."),
    workflow: Annotated[
        list[str] | None, typer.Option("--workflow", "-w", help="Only this workflow (repeatable)")
    ] = None,
    include_partial: Annotated[
        bool, typer.Option("--all", help="Also write workflows whose tasks are not all done")
    ] = False,
    no_tests: Annotated[
        bool,
        typer.Option("--no-tests", help="Do not run the tests: implemented tasks count as done"),
    ] = False,
    base_url: Annotated[
        str, typer.Option("--base-url", help="The backend URL shown in the snippets")
    ] = DEFAULT_BASE,
    check_only: Annotated[
        bool,
        typer.Option(
            "--check", help="Write nothing; exit 1 if ui/specs/ is not what would be written"
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable result")] = False,
) -> None:
    """Write ui/specs/: how to build the UI on this backend (zero tokens).

    One file per workflow whose tasks are done, one for the models with CRUD routes, an index with the
    `@tuvl/client` setup, and `ui-contract.json`. Text below the notes marker in a file is kept.
    """
    project = project_dir.resolve()
    if check_only:
        outcome = check(project, run_tests=not no_tests, base_url=base_url)
        if as_json:
            typer.echo(json.dumps({"ok": outcome.clean, **vars(outcome)}, indent=2))
        else:
            for n in outcome.missing:
                console.print(f"  [red]✗[/red] {SPECS_DIR / n} is missing")
            for n in outcome.stale:
                console.print(f"  [red]✗[/red] {SPECS_DIR / n} is out of date")
            for n in outcome.extra:
                console.print(
                    f"  [yellow]![/yellow] {SPECS_DIR / n} documents something that is not ready or no longer exists"
                )
            if outcome.clean:
                console.print(
                    f"[green]✓[/green] {SPECS_DIR} is current ({len(outcome.unchanged)} file(s))"
                )
            else:
                console.print("Run `tuvl generate ui-spec` to regenerate.")
        raise typer.Exit(0 if outcome.clean else 1)
    facts = collect(project, run_tests=not no_tests)
    outcome = generate(
        project,
        only=workflow,
        include_partial=include_partial,
        base_url=base_url,
        facts=facts,
    )
    audit.emit(
        project,
        "ui.spec.generated",
        command="generate ui-spec",
        entities=[f"ui/specs/{n}" for n in outcome.written],
        detail=f"{len(outcome.written)} written, {len(outcome.unchanged)} unchanged",
    )
    if as_json:
        typer.echo(
            json.dumps({"written": outcome.written, "unchanged": outcome.unchanged}, indent=2)
        )
        return
    for n in outcome.written:
        console.print(f"  [green]✓[/green] wrote {escape(str(SPECS_DIR / n))}")
    waiting = [w["name"] for w in facts["workflows"] if not w["ready"]]
    if waiting and not include_partial:
        console.print(
            f"  [dim]not ready yet (no file): {', '.join(waiting)}; `--all` writes them anyway[/dim]"
        )
    console.print(
        f"{len(outcome.written)} written, {len(outcome.unchanged)} unchanged in {SPECS_DIR}"
    )
