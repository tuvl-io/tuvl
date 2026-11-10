"""``tuvl generate code`` — deterministic scaffolding from the project (spec §17.6)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from tuvl.core.config import use_project

console = Console()
_MARK = {"create": "[green]+[/green]", "update": "[yellow]~[/yellow]", "delete": "[red]-[/red]"}


def codegen_cmd(
    workflow: Annotated[
        str | None, typer.Argument(help="Limit stubs, prompts and tests to one workflow")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    check: Annotated[
        bool, typer.Option("--check", help="Write nothing; exit 1 if anything would change")
    ] = False,
    spec: Annotated[
        str | None,
        typer.Option(
            "--spec",
            help="An approved spec (id, name or file): build its models and workflows from its tasks",
        ),
    ] = None,
    task: Annotated[
        list[str] | None,
        typer.Option("--task", help="Task(s) for the agent: 3, T3, 1,3 or a full id (repeatable)"),
    ] = None,
    all_tasks: Annotated[
        bool, typer.Option("--all", help="With --spec: every agent task not yet implemented")
    ] = False,
    no_agent: Annotated[
        bool, typer.Option("--no-agent", help="With --spec: only the deterministic part, no agent")
    ] = False,
    agent: Annotated[
        str | None, typer.Option("--agent", help="With --spec: the coding agent (default: yours)")
    ] = None,
    max_attempts: Annotated[int, typer.Option("--max-attempts", help="Tries per task")] = 3,
    budget: Annotated[
        float, typer.Option("--budget", help="Most USD one attempt may spend (Claude Code)")
    ] = 1.0,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds per attempt")] = 900,
    model: Annotated[
        str | None, typer.Option("--model", help="Model, if the agent takes one")
    ] = None,
    base: Annotated[
        str | None, typer.Option("--base", help="Branch to start from (default develop)")
    ] = None,
    no_pr: Annotated[
        bool, typer.Option("--no-pr", help="Commit on the branches; push nothing")
    ] = False,
    ready: Annotated[
        bool, typer.Option("--ready", help="Open pull requests ready for review, not as drafts")
    ] = False,
    keep_going: Annotated[
        bool,
        typer.Option("--continue", help="After a failed task go on with those independent of it"),
    ] = False,
    keep_worktree: Annotated[
        bool, typer.Option("--keep-worktree", help="Leave the work folders in .tuvl/worktrees")
    ] = False,
    consent: Annotated[
        bool,
        typer.Option("--consent", help="Agree that spec and project files are sent to your agent"),
    ] = False,
    review: Annotated[
        str,
        typer.Option(
            "--review",
            help="With --spec, review each task's Python before committing: none, static (the "
            "C-rules and ruff, free) or agent (static plus a second opinion from a coding agent)",
        ),
    ] = "static",
    review_blocking: Annotated[
        str,
        typer.Option(
            "--review-blocking",
            help="Agent findings at least this severe fail the task: off, high, medium or low",
        ),
    ] = "off",
    reviewer: Annotated[
        str | None,
        typer.Option(
            "--reviewer", help="The reviewing agent (default: another one if you have it)"
        ),
    ] = None,
) -> None:
    """Generate schemas, stubs, prompts and tests (zero tokens); with --spec, build a spec's models and workflows."""
    if spec is not None:
        if check or workflow is not None:
            console.print("[red]--spec cannot be combined with --check or a workflow name.[/red]")
            raise typer.Exit(2)
        _spec_mode(
            project_dir.resolve(),
            spec,
            task or [],
            all_tasks,
            no_agent,
            agent,
            max_attempts,
            budget,
            timeout,
            model,
            base,
            no_pr,
            ready,
            keep_going,
            keep_worktree,
            consent,
            review,
            review_blocking,
            reviewer,
        )
        return
    if task or all_tasks or no_agent:
        console.print("[red]--task, --all and --no-agent belong with --spec.[/red]")
        raise typer.Exit(2)
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY
    from tuvl.core.config_loader import load_all_configs
    from tuvl.core.models.schemas import SCHEMA_REGISTRY
    from tuvl_insight import codegen
    from tuvl_insight.spec import SpecError, load_specs

    project = project_dir.resolve()
    use_project(str(project))
    skeletons = codegen.decide_skeletons(project)
    if not check:
        codegen.apply(project, skeletons.changes)
    try:
        load_all_configs(project)
        specs = load_specs(project)
        if workflow is not None and workflow not in WORKFLOWS:
            console.print(f"[red]No workflow named {escape(workflow)!r}.[/red]")
            raise typer.Exit(1)
        result = codegen.plan(
            project,
            dict(WORKFLOWS),
            SCHEMA_REGISTRY,
            specs,
            artifacts=set(ARTIFACT_REGISTRY),
            only=workflow,
        )
    except (codegen.CodegenError, SpecError) as exc:
        console.print(f"[red]codegen:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    changes = [*skeletons.changes, *result.changes]
    for change in changes:
        console.print(f"{_MARK[change.action]} {escape(str(change.path))}")
    for warning in [*skeletons.warnings, *result.warnings]:
        console.print(f"[yellow]⚠[/yellow] {escape(warning)}")
    if check:
        if changes:
            console.print(
                f"\n[red]{len(changes)} generated file(s) out of date[/red] — run `tuvl generate code`"
            )
            raise typer.Exit(1)
        console.print("[green]Generated files are up to date.[/green]")
        return
    codegen.apply(project, result.changes)
    console.print(f"\n{len(changes)} file(s) written." if changes else "Nothing to generate.")


def _spec_mode(
    project: Path,
    spec: str,
    tasks: list[str],
    all_tasks: bool,
    no_agent: bool,
    agent: str | None,
    max_attempts: int,
    budget: float,
    timeout: float,
    model: str | None,
    base: str | None,
    no_pr: bool,
    ready: bool,
    keep_going: bool,
    keep_worktree: bool,
    consent: bool,
    review: str,
    review_blocking: str,
    reviewer: str | None,
) -> None:
    from tuvl_insight import taskrun
    from tuvl_insight.cli import agent_run
    from tuvl_insight.coding_agents import runner
    from tuvl_insight.gitflow import GitFlowError

    agent_run.need_git(project, "codegen --spec")
    if not no_agent and not tasks and not all_tasks:
        console.print(
            "[red]Name the tasks[/red] (--task 3, --task 1,3) or use --all; --no-agent does only the scaffold."
        )
        raise typer.Exit(2)
    chosen = None
    if not no_agent:
        chosen = agent_run.choose_agent(project, agent)
        agent_run.ensure_consent(
            project,
            chosen,
            flag=consent,
            what="the spec and the project's files",
            command="generate code",
        )
    limits = runner.Limits(
        timeout=timeout,
        budget_usd=budget if chosen and chosen.id == "claude" else None,
        model=model,
    )
    if review not in ("none", "static", "agent") or review_blocking not in (
        "off",
        "high",
        "medium",
        "low",
    ):
        console.print(
            "[red]--review is none, static or agent; --review-blocking is off, high, medium or low.[/red]"
        )
        raise typer.Exit(2)
    plan = taskrun.ReviewPlan(mode=review, blocking=review_blocking)
    if review == "agent" and chosen is not None:
        who = agent_run.choose_reviewer(project, reviewer, chosen.id)
        plan = taskrun.ReviewPlan(
            "agent",
            review_blocking,
            runner.adapter_for(who.id, who.command),
            runner.binary_for(who.id, who.command),
            who.label,
            runner.Limits(
                timeout=timeout, budget_usd=budget if who.id == "claude" else None, model=model
            ),
        )
    try:
        with console.status("building…") as status:
            report = taskrun.run_tasks(
                project,
                spec_ref=spec,
                tokens=tasks,
                all_tasks=all_tasks,
                agent_id=chosen.id if chosen else "none",
                command=chosen.command if chosen else (),
                agent_label=chosen.label if chosen else "",
                base=base,
                max_attempts=max_attempts,
                limits=limits,
                use_agent=not no_agent,
                open_prs=not no_pr,
                draft=not ready,
                stop_on_fail=not keep_going,
                keep_worktree=keep_worktree,
                review=plan,
                say=lambda m: status.update(escape(m)),
            )
    except (taskrun.TaskRunError, GitFlowError, runner.AgentError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    if report.scaffolded:
        console.print(
            f"[green]✓[/green] scaffold on [bold]{report.scaffold_branch}[/bold]: {len(report.scaffolded)} file(s)"
        )
        for path in report.scaffolded[:12]:
            console.print(f"    [dim]{escape(path)}[/dim]")
        if report.scaffold_pr:
            console.print(f"    {escape(report.scaffold_pr)}")
    elif report.scaffold_waiting:
        console.print(
            f"[yellow]The scaffold is already built on [bold]{report.scaffold_waiting}[/bold] "
            "and is not merged yet.[/yellow]\n"
            f"    Review it and merge it into the base branch (git merge {report.scaffold_waiting}); "
            "the model and workflow tasks turn done once it is merged."
        )
    elif not report.outcomes:
        console.print("Nothing to scaffold: the models and workflows already exist.")
    for o in report.outcomes:
        mark = "[green]✓[/green]" if o.ok else "[red]✗[/red]"
        console.print(
            f"{mark} {o.job.id} {escape(o.job.title)}  [dim]{o.attempts} attempt(s), ${o.cost_usd:.3f}[/dim]"
        )
        if o.ok:
            console.print(f"    {o.branch} {o.commit} {escape(o.pr_url)}".rstrip())
        if not o.ok and o.failure:
            for a in o.failure["attempts"]:
                console.print(
                    f"    [red]attempt {a['attempt']}: failed the {a['gate']} check[/red]"
                )
                for p in a["problems"][:4]:
                    console.print(f"      [red]{escape(p)}[/red]")
            console.print(f"    [dim]full report: tuvl show task {o.job.id}[/dim]")
        else:
            for p in o.problems[:4]:
                console.print(f"    [red]{escape(p)}[/red]")
        if o.review:
            console.print(f"    [dim]{escape(o.review.strip().splitlines()[0])}[/dim]")
    for job_id, why in report.skipped:
        console.print(f"[dim]– {escape(job_id)}: {escape(why)}[/dim]")
    for w in report.warnings:
        console.print(f"[yellow]{escape(w)}[/yellow]")
    if not report.ok:
        raise typer.Exit(1)
    console.print(
        "Next: review the pull requests in order, then `tuvl list tasks` and `tuvl run tests`."
    )
