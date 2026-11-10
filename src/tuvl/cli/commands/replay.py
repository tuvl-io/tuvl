"""``tuvl replay runs``: re-run pinned run journals through the local project (toolchain)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from tuvl.core.config import use_project

console = Console()


def replay_test(fixture: Path, project: Path) -> Any:
    """A Test that replays *fixture* and expects its recorded end and output."""
    from tuvl.contract.schema import TestDocument

    run = json.loads(fixture.read_text())["run"]
    expect: dict[str, Any] = {"output": run.get("output")}
    if run.get("status") in ("completed", "compensated") and run.get("end_name"):
        expect["end"] = run["end_name"]
    relative = (
        fixture.resolve().relative_to(project)
        if fixture.resolve().is_relative_to(project)
        else fixture
    )
    return TestDocument.model_validate(
        {
            "kind": "Test",
            "version": "tuvl/v2",
            "metadata": {"name": f"replay-{fixture.stem}"},
            "spec": {
                "workflow": run["workflow"],
                "input": run.get("input") or {},
                "replay": str(relative),
                "expect": expect,
            },
        }
    )


def replay_cmd(
    fixtures: Annotated[
        list[Path], typer.Argument(help="Pinned journals (tests/fixtures/…/*.journal.json)")
    ],
    project_dir: Annotated[Path, typer.Option("--project-dir", "-d")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Re-run pinned journals through the local project; fail if the end or output differ."""
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.config import settings
    from tuvl.core.config_loader import load_all_configs
    from tuvl.runtime.code import load_agents
    from tuvl.testing import run_test

    project = project_dir.resolve()
    use_project(str(project))
    load_all_configs(project)
    load_agents(settings.dir_for("agents"))

    async def run_all() -> list[Any]:
        return [
            await run_test(replay_test(f, project), dict(WORKFLOWS), base=project) for f in fixtures
        ]

    results = asyncio.run(run_all())
    if as_json:
        print(
            json.dumps(
                {
                    "passed": all(r.passed for r in results),
                    "replays": [r.to_json() for r in results],
                },
                indent=2,
            )
        )
    else:
        for fixture, r in zip(fixtures, results, strict=True):
            mark = "[green]SAME[/green]" if r.passed else "[red]DIFF[/red]"
            console.print(f"{mark} {escape(str(fixture))} [dim](END.{r.end or '?'})[/dim]")
            for failure in r.failures:
                console.print(f"     [red]•[/red] {escape(failure)}")
    raise typer.Exit(0 if all(r.passed for r in results) else 1)
