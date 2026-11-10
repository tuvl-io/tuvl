"""``tuvl run tests`` — run the project's ``kind: Test`` documents (spec §18).

Offline by default: model and tool agents must be mocked or replayed.
``--live`` lets them call out. Judges used by the tests are calibrated
first; an untrusted judge is reported, and ``--strict`` fails on it (V023).
"""

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


async def _run(
    project: Path, *, live: bool, allow_uncertain: bool, only: str | None, strict: bool
) -> tuple[list[Any], list[str]]:
    from tuvl.contract.registry import TESTS, WORKFLOWS
    from tuvl.core.config import settings
    from tuvl.core.config_loader import load_all_configs
    from tuvl.runtime import judges
    from tuvl.runtime.code import load_agents
    from tuvl_insight.testing import run_test

    load_all_configs(project)
    load_agents(settings.dir_for("agents"))
    tests = [TESTS[n] for n in sorted(TESTS) if only is None or only in n]
    warnings: list[str] = []
    used = sorted({j.judge for t in tests for j in t.spec.expect.judge})
    for ref in used:
        try:
            calibration = await judges.calibrate(ref, project)
        except judges.JudgeUnavailableError as exc:
            warnings.append(f"[V023] {ref}: calibration unavailable — {exc}")
            continue
        if calibration.results and not calibration.trusted:
            warnings.append(
                f"[V023] {ref}: calibration agreement {calibration.agreement:.0%} is below "
                f"min_agreement — its verdicts are untrusted"
            )
    results = [
        await run_test(t, dict(WORKFLOWS), base=project, live=live, allow_uncertain=allow_uncertain)
        for t in tests
    ]
    if strict and warnings:
        for r in results:
            r.passed = False
    return results, warnings


def test_cmd(
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    live: Annotated[
        bool, typer.Option("--live", help="Let unmocked model and tool agents call out")
    ] = False,
    allow_uncertain: Annotated[
        bool, typer.Option("--allow-uncertain", help="An 'uncertain' judge verdict passes")
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict", help="Also fail on stubbed results and untrusted judge calibration (CI)"
        ),
    ] = False,
    only: Annotated[
        str | None, typer.Option("--only", "-k", help="Run tests whose name contains this")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable report")] = False,
) -> None:
    """Run the project's tests (offline unless --live)."""
    project = project_dir.resolve()
    use_project(str(project))
    results, warnings = asyncio.run(
        _run(project, live=live, allow_uncertain=allow_uncertain, only=only, strict=strict)
    )
    failed = [r for r in results if r.status == "failed"]
    stubbed = [r for r in results if r.status == "stubbed"]
    ok = not failed and not (strict and stubbed)
    if as_json:
        report = {
            "passed": ok,
            "tests": [r.to_json() for r in results],
            "warnings": warnings,
        }
        print(json.dumps(report, indent=2))
    else:
        marks = {
            "passed": "[green]PASS   [/green]",
            "stubbed": "[yellow]STUBBED[/yellow]",
            "failed": "[red]FAIL   [/red]",
        }
        for r in results:
            console.print(
                f"{marks[r.status]} {escape(r.name)} [dim]({escape(r.workflow)} → "
                f"END.{r.end or '?'}, {r.tokens} tok)[/dim]"
            )
            if r.stubbed:
                console.print(
                    f"     [yellow]•[/yellow] through {len(r.stubbed)} pending agent(s): "
                    f"{escape(', '.join(r.stubbed))} — not counted as passing"
                )
            for failure in r.failures:
                console.print(f"     [red]•[/red] {escape(failure)}")
            for warning in r.warnings:
                console.print(f"     [yellow]![/yellow] {escape(warning)}")
        for w in warnings:
            console.print(f"[yellow]⚠[/yellow] {escape(w)}")
        if not results:
            console.print("[dim]No tests found (kind: Test, version: tuvl/v2).[/dim]")
        else:
            passed = len(results) - len(failed) - len(stubbed)
            console.print(
                f"\n{passed} passed · {len(stubbed)} stubbed (not counted) · {len(failed)} failed"
            )
    raise typer.Exit(0 if ok else 1)
