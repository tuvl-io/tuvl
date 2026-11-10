"""``tuvl review code`` — review the Python of a project's code agents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from tuvl_insight import gitutil
from tuvl_insight.review import Report, review_project
from tuvl_insight.review.rules import RULES

console = Console()
_MARK = {"error": "[red]✗[/red]", "warning": "[yellow]![/yellow]", "info": "[dim]·[/dim]"}


def changed_python(project: Path, base: str) -> list[Path]:
    """Python files that differ from *base* (committed, staged or not)."""
    names = gitutil.out(project, "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD").split()
    names += gitutil.out(project, "diff", "--name-only", "--diff-filter=d", "HEAD").split()
    names += gitutil.out(project, "ls-files", "--others", "--exclude-standard").split()
    return sorted({project / n for n in names if n.endswith(".py")})


def print_report(report: Report, *, quiet: bool = False) -> None:
    for f in sorted(report.findings, key=lambda f: (f.file, f.line, f.code)):
        console.print(
            f"  {_MARK[f.severity]} {f.code} {escape(f.file)}:{f.line}  {escape(f.message)}"
        )
    for note in report.notes:
        console.print(f"  [dim]{escape(note)}[/dim]")
    if not quiet:
        console.print(
            f"\n{len(report.files)} file(s) · {len(report.errors)} error(s) · {len(report.warnings)} warning(s)"
        )


def review_cmd(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Python files (default: every code agent's)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    base: Annotated[
        str | None, typer.Option("--base", help="Only files changed since this branch or commit")
    ] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Warnings fail too")] = False,
    no_ruff: Annotated[bool, typer.Option("--no-ruff", help="Skip the ruff pass")] = False,
    agent: Annotated[
        bool,
        typer.Option(
            "--agent", help="Also ask a coding agent for a second opinion (spends tokens)"
        ),
    ] = False,
    reviewer: Annotated[
        str | None, typer.Option("--reviewer", help="The reviewing agent (implies --agent)")
    ] = None,
    spec: Annotated[
        str | None,
        typer.Option(
            "--spec",
            help="Only the code of this spec's tasks (id, name or file); with --agent, also the "
            "spec the code is judged against",
        ),
    ] = None,
    task: Annotated[
        list[str] | None,
        typer.Option(
            "--task",
            help="Only the code of these tasks: 3, T3, 1,3 or a full id (repeatable)",
        ),
    ] = None,
    blocking: Annotated[
        str,
        typer.Option(
            "--blocking", help="Agent findings at least this severe fail: off, high, medium, low"
        ),
    ] = "off",
    post: Annotated[
        bool,
        typer.Option(
            "--post", help="With --agent: comment the review on this branch's pull request"
        ),
    ] = False,
    consent: Annotated[
        bool, typer.Option("--consent", help="Agree that the code is sent to the reviewing agent")
    ] = False,
    budget: Annotated[
        float, typer.Option("--budget", help="Most USD the review may spend (Claude Code)")
    ] = 1.0,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds for the review")] = 600,
    explain: Annotated[
        str | None, typer.Option("--explain", help="Explain a rule code (C001…) and exit")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable report")] = False,
) -> None:
    """Check the Python of code agents against tuvl's rules (C001…) and ruff. Zero tokens.

    `--spec` or `--task` reviews only the code agents those tasks created.
    """
    if explain:
        code = explain.upper()
        if code not in RULES:
            console.print(
                f"[red]Unknown rule '{escape(explain)}'.[/red] Rules: {', '.join(sorted(RULES))}"
            )
            raise typer.Exit(2)
        severity, text = RULES[code]
        console.print(f"[bold]{code}[/bold] ({severity}): {escape(text)}")
        return
    project = project_dir.resolve()
    if paths:
        files = [p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve() for p in paths]
    elif base:
        files = changed_python(project, base)
    elif spec or task:
        from tuvl_insight.review.scope import ScopeError, files_for

        try:
            files = files_for(project, spec, task or [])
        except ScopeError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(2) from exc
        if not files:
            console.print(
                "[yellow]No code agent belongs to those tasks[/yellow] (models, steps that are "
                "not code, tests and examples have no Python of their own)."
            )
            raise typer.Exit(0)
    else:
        files = None
    if blocking not in ("off", "high", "medium", "low"):
        console.print("[red]--blocking is off, high, medium or low.[/red]")
        raise typer.Exit(2)
    report = review_project(project, files, with_ruff=not no_ruff)
    failed = bool(report.errors) or (strict and bool(report.warnings))
    opinion = (
        _agent_review(
            project,
            report,
            files,
            base,
            spec,
            reviewer,
            blocking,
            post,
            consent,
            budget,
            timeout,
        )
        if (agent or reviewer)
        else None
    )
    if opinion is not None and opinion.failed:
        failed = True
    if as_json:
        payload = report.to_json()
        if opinion is not None:
            payload["agent_review"] = opinion.to_json()
        typer.echo(json.dumps(payload, indent=2))
    else:
        if not report.files:
            console.print("[dim]No Python to review (agents/*.py).[/dim]")
        print_report(report)
        if opinion is not None:
            opinion.print()
    if failed:
        raise typer.Exit(1)


class _Opinion:
    def __init__(self, label: str, result: object, blocked: list[str]) -> None:
        self.label, self.result, self.blocked = label, result, blocked

    @property
    def failed(self) -> bool:
        return bool(self.blocked) or not getattr(self.result, "ok", False)

    def to_json(self) -> dict[str, object]:
        r = self.result
        return {
            "reviewer": self.label,
            "ok": getattr(r, "ok", False),
            "verdict": getattr(r, "verdict", ""),
            "error": getattr(r, "error", ""),
            "cost_usd": getattr(r, "cost_usd", None),
            "findings": [f.to_json() for f in getattr(r, "findings", [])],
        }

    def print(self) -> None:
        r = self.result
        cost = getattr(r, "cost_usd", None)
        spent = f", ${cost:.3f}" if cost is not None else ""
        console.print(f"\n[bold]Agent review[/bold] ({escape(self.label)}{spent})")
        if not getattr(r, "ok", False):
            console.print(f"  [red]{escape(getattr(r, 'error', ''))}[/red]")
            return
        findings = getattr(r, "findings", [])
        if not findings:
            console.print("  no findings")
        for f in findings:
            console.print(
                f"  [yellow]{f.severity:<6}[/yellow] {escape(f.file)}:{f.line}  {escape(f.summary)}"
            )
            if f.suggestion:
                console.print(f"         [dim]{escape(f.suggestion)}[/dim]")
        for note in getattr(r, "notes", []):
            console.print(f"  [dim]{escape(note)}[/dim]")


def _agent_review(
    project: Path,
    report: Report,
    files: list[Path] | None,
    base: str | None,
    spec: str | None,
    reviewer: str | None,
    blocking: str,
    post: bool,
    consent: bool,
    budget: float,
    timeout: float,
) -> _Opinion:
    from tuvl.cli import agent_run
    from tuvl_insight import audit, gitflow
    from tuvl_insight.audit import trace
    from tuvl_insight.coding_agents import read_settings, runner
    from tuvl_insight.review import agent as agent_review
    from tuvl_insight.review.static import python_files

    settings = read_settings(project)
    author = settings.agent if settings else "none"
    who = agent_run.choose_reviewer(project, reviewer, author)
    agent_run.ensure_consent(
        project, who, flag=consent, what="the code under review", command="review code"
    )
    paths = files if files is not None else python_files(project)
    sources = {
        (str(p.relative_to(project)) if p.is_relative_to(project) else str(p)): p.read_text()
        for p in (q if q.is_absolute() else project / q for q in paths)
        if p.suffix == ".py" and p.is_file()
    }
    if not sources:
        console.print("[dim]No Python to send to a reviewer.[/dim]")
        raise typer.Exit(0)
    diff = gitutil.out(project, "diff", f"{base}...HEAD", "--", *sources) if base else ""
    spec_text = ""
    if spec:
        from tuvl_insight.spec.lifecycle import LifecycleError, resolve_spec

        try:
            spec_text = resolve_spec(project, spec).read_text()
        except LifecycleError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
    prompt = agent_review.build_prompt(
        subject=f"{len(sources)} file(s) of {project.name}"
        + (f", changed since {base}" if base else ""),
        spec_text=spec_text,
        diff=diff,
        files=sources,
        tier1=report.findings,
    )
    audit.emit(
        project,
        "review.requested",
        command="review code",
        detail=who.label,
        refs={"prompt_sha256": trace.store(project, prompt)},
    )
    adapter = runner.adapter_for(who.id, who.command)
    binary = runner.binary_for(who.id, who.command)
    with console.status(f"{escape(who.label)} is reviewing…"):
        result = agent_review.review(
            project,
            adapter=adapter,
            binary=binary,
            prompt=prompt,
            limits=runner.Limits(
                timeout=timeout, budget_usd=budget if who.id == "claude" else None
            ),
        )
    for f in result.findings[:20]:
        audit.emit(project, "review.finding", command="review code", detail=f.text()[:200])
    audit.emit(
        project,
        "review.completed",
        command="review code",
        result="ok" if result.ok else "failed",
        detail=result.verdict or result.error[:120],
        refs={"findings": len(result.findings), "cost_usd": result.cost_usd},
    )
    if post:
        forge = gitflow.forge(project)
        if forge is None:
            console.print("[yellow]--post: no gh/glab for this remote; nothing posted.[/yellow]")
        else:
            try:
                url = gitflow.comment_pr(
                    project, forge, agent_review.markdown(result, reviewer=who.label)
                )
                console.print(f"posted {escape(url)}".rstrip())
            except gitflow.GitFlowError as exc:
                console.print(f"[yellow]--post failed: {escape(str(exc))}[/yellow]")
    blocked = [f.text() for f in result.at_least(blocking)] if result.ok else []
    return _Opinion(who.label, result, blocked)
