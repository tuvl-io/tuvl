"""``tuvl spec`` — the spec-driven loop (spec §17)."""

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
spec_app = typer.Typer(
    help="Check specs, verify the implementation against them and track their tasks.",
    no_args_is_help=True,
)


def _load(project: Path) -> None:
    use_project(str(project))
    from tuvl.core.config_loader import load_all_configs

    load_all_configs(project)


_STYLE = {
    "todo": "dim",
    "in_progress": "yellow",
    "implemented": "cyan",
    "tested": "blue",
    "done": "green",
    "untracked": "magenta",
}


@spec_app.command("status")
def status_cmd(
    spec_file: Annotated[
        Path | None, typer.Argument(help="One spec: id, name or file (default: every TaskPlan)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Exit 1 unless every task is done (none blocked or stale)"),
    ] = False,
    no_tests: Annotated[
        bool, typer.Option("--no-tests", help="Skip running tests (nothing reaches tested/done)")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable status")] = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch", "-w", help="Keep refreshing, with live codegen progress, until Ctrl-C"
        ),
    ] = False,
    interval: Annotated[float, typer.Option("--interval", help="Seconds between refreshes")] = 2.0,
) -> None:
    """Derive every task's status from the project (validate, tests, lock)."""
    if watch:
        project = project_dir.resolve()
        one = _refs(project, spec_file)
        _watch_status(project, one[0] if one else None, no_tests, interval)
        return
    from tuvl_insight.spec.tasks import (
        TaskPlanError,
        derived_plans,
        open_count,
        project_state,
        selected_plans,
    )

    project = project_dir.resolve()
    use_project(str(project))
    found = _refs(project, spec_file)
    spec_file = found[0] if found else None
    try:
        selected = selected_plans(project, spec_file)
    except TaskPlanError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    if not selected:
        console.print(
            "[yellow]No task list found — `tuvl generate tasks <spec>` derives specs/<name>.tasks.yaml from an approved spec.[/yellow]"
        )
        raise typer.Exit(1 if strict else 0)
    state = asyncio.run(project_state(project, run_tests=not no_tests))
    report, still_open = [], 0
    for plan, tasks in derived_plans(project, state, spec_file):
        still_open += open_count(tasks)
        report.append({"spec": plan.metadata.spec, "tasks": [t.to_json() for t in tasks]})
        if as_json:
            continue
        console.print(f"\n[bold]{escape(plan.metadata.spec)}[/bold]")
        for t in tasks:
            flags = "".join(
                f" [red]{f}[/red]" for f, on in (("blocked", t.blocked), ("stale", t.stale)) if on
            )
            title = f"[strike dim]{escape(t.title)}[/strike dim]" if t.obsolete else escape(t.title)
            shown = "obsolete" if t.obsolete else t.status
            console.print(f"  {t.id:<4} [{_STYLE.get(shown, 'dim')}]{shown:<11}[/]{flags} {title}")
            for why in t.reasons[:3]:
                console.print(f"        [dim]{escape(why)}[/dim]")
            if t.failure:
                console.print(
                    f"        [red]last run failed: {escape(str(t.failure.get('summary', '')))}[/red]"
                    f" [dim](tuvl show task {t.id})[/dim]"
                )
    if as_json:
        print(json.dumps({"done": still_open == 0, "plans": report}, indent=2))
    elif state.tests is not None:
        passed = sum(1 for t in state.tests if t.passed)
        console.print(
            f"\n[dim]tests {passed}/{len(state.tests)} passing · {state.lock_note if not state.lock_current else 'tuvl.lock current'}[/dim]"
        )
    if strict and still_open:
        if not as_json:
            console.print(f"[red]{still_open} task(s) not done.[/red]")
        raise typer.Exit(1)


@spec_app.command("task")
def task_cmd(
    task: Annotated[
        str, typer.Argument(help="Task id, e.g. TASK-SK-0004-01-003 (or T3 with --spec)")
    ],
    spec: Annotated[
        str | None, typer.Option("--spec", help="The spec (default: the task's own)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable record")] = False,
) -> None:
    """Show one task in full: what it builds, what it waits for and the spec section it came from."""
    from tuvl_insight.spec.tasks import task_detail, task_text

    project = project_dir.resolve()
    found = _refs(project, Path(spec)) if spec else None
    detail = task_detail(project, task, found[0] if found else None)
    if detail is None:
        console.print(f"[red]No task {escape(task)} in any task list.[/red]")
        raise typer.Exit(1)
    if as_json:
        print(json.dumps(detail, indent=2))
        return
    typer.echo(task_text(detail))


@spec_app.command("branches")
def branches_cmd(
    spec_file: Annotated[
        Path | None, typer.Argument(help="One spec: id, name or file (default: every task list)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable report")] = False,
) -> None:
    """Where the branches of each spec's work stand: spec, scaffold and one per task, and what to merge next."""
    from tuvl_insight.spec.branches import report

    project = project_dir.resolve()
    found = _refs(project, spec_file) if spec_file else None
    data = report(project, found[0] if found else None)
    if as_json:
        print(json.dumps(data, indent=2))
        return
    if data["base"] is None:
        console.print("[yellow]The project has no commits yet.[/yellow]")
        return
    console.print(f"base [bold]{data['base']}[/bold] · on [bold]{data['current']}[/bold]")
    if data["dirty"]:
        console.print(f"[yellow]{data['dirty']} uncommitted change(s) in the working tree[/yellow]")
    marks = {
        "merged": "[green]✓ merged[/green]",
        "open": "[yellow]● open[/yellow]",
        "none": "[dim]○ not built[/dim]",
    }
    for entry in data["specs"]:
        console.print(f"\n[bold]{escape(entry['spec'])}[/bold]")
        for path in entry["uncommitted"]:
            console.print(f"  [yellow]! {escape(path)} is not committed anywhere yet[/yellow]")
        stack = entry["stack"]
        landed = [n for n in stack if n.get("deleted")]
        waiting = [n for n in stack if n["state"] == "none"]
        children: dict[str, list[dict]] = {}
        for node in (n for n in stack if n["ref"]):
            children.setdefault(node["parent"], []).append(node)

        def show(node: dict, prefix: str, last: bool, children: dict[str, list[dict]]) -> None:
            extra = ""
            if node["state"] == "open":
                extra = f" {node['ahead']} ahead" + (
                    f", {node['behind']} behind" if node["behind"] else ""
                )
            if node["parallel_with"]:
                extra += " [magenta]∥ parallel[/magenta]"
            console.print(
                f"{prefix}{'└─ ' if last else '├─ '}{marks[node['state']]} {escape(node['branch'])}{extra}"
            )
            pad = prefix + ("   " if last else "│  ")
            for commit in node["commits"][:6]:
                console.print(f"{pad}[dim]{commit['sha']} {escape(commit['subject'])}[/dim]")
            if node["pr"]:
                console.print(f"{pad}[dim]{escape(node['pr'])}[/dim]")
            if node["next"]:
                console.print(f"{pad}[cyan]next: {escape(node['next'])}[/cyan]")
            kids = children.get(node["branch"], [])
            for i, kid in enumerate(kids):
                show(kid, pad, i == len(kids) - 1, children)

        console.print(f"  [bold]{data['base']}[/bold]")
        for node in landed:
            console.print(
                f"  ├─ [green]✓ landed[/green] {escape(node['branch'])} "
                "[dim](branch deleted after the merge)[/dim]"
            )
        roots = children.get(data["base"], [])
        for i, node in enumerate(roots):
            show(node, "  ", i == len(roots) - 1 and not waiting, children)
        for i, node in enumerate(waiting):
            end = "└─" if i == len(waiting) - 1 else "├─"
            console.print(
                f"  {end} {marks['none']} {escape(node['branch'])} [dim]{escape(node['title'])}[/dim]"
            )
        if entry["merge_next"]:
            console.print(
                f"  [bold]Merge next:[/bold] {escape(entry['merge_next'])} (tuvl never merges)"
            )


@spec_app.command("diff")
def diff_cmd(
    spec_file: Annotated[
        Path | None, typer.Argument(help="One spec: id, name or file (default: every TaskPlan)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable diff")] = False,
) -> None:
    """Show how each spec moved since its plan: stale tasks, uncovered sections."""
    from tuvl_insight.spec.tasks import spec_diff

    project = project_dir.resolve()
    results = []
    from tuvl_insight.spec.tasks import selected_plans

    found = _refs(project, spec_file)
    for _, plan, spec in selected_plans(project, found[0] if found else None):
        if spec is None:
            console.print(f"[red]{escape(plan.metadata.spec)} is missing or unreadable.[/red]")
            raise typer.Exit(1)
        results.append(spec_diff(plan, spec))
    if as_json:
        print(json.dumps({"specs": [r.to_json() for r in results]}, indent=2))
        return
    for r in results:
        head = (
            "[yellow]changed since the task list[/yellow]"
            if r.changed
            else "[green]unchanged[/green]"
        )
        console.print(f"[bold]{escape(r.spec)}[/bold]: {head}")
        for task, why in r.stale:
            console.print(f"  [yellow]stale[/yellow] {task}: {escape(why)}")
        if r.uncovered:
            console.print(f"  [dim]sections no task references: {', '.join(r.uncovered)}[/dim]")
        if r.stale:
            console.print(
                "  update the task list (skill tuvl-plan): new tasks for new steps, obsolete for removed ones"
            )


from tuvl_insight.spec.template import SPEC_TEMPLATE  # noqa: E402 — also imported by the dev API


@spec_app.command("check")
def check_cmd(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Specs: id, name or file (default: every spec)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    strict: Annotated[bool, typer.Option("--strict", help="Warnings fail too")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable report")] = False,
) -> None:
    """Is the spec complete and consistent? Deterministic, zero tokens (S001–S021)."""
    from tuvl_insight.spec.check import check_project
    from tuvl_insight.spec.parse import SpecError

    project = project_dir.resolve()
    _load(project)
    try:
        results = check_project(project, _refs(project, paths) if paths else None)
    except SpecError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    ok = all(not r.errors and not (strict and r.warnings) for r in results)
    if as_json:
        print(json.dumps({"ok": ok, "specs": [r.to_json() for r in results]}, indent=2))
        raise typer.Exit(0 if ok else 1)
    if not results:
        console.print("[dim]No specs found (specs/*.md).[/dim]")
    marks = {"error": "[red]✗[/red]", "warning": "[yellow]![/yellow]", "waived": "[dim]·[/dim]"}
    for r in results:
        console.print(f"\n[bold]{escape(r.path)}[/bold]")
        for f in sorted(
            r.findings, key=lambda f: ({"error": 0, "warning": 1}.get(f.severity, 2), f.line)
        ):
            where = f"{f.file}:{f.line}" if f.file else f":{f.line}"
            console.print(f"  {marks[f.severity]} {f.code:<6} {where:<5} {escape(f.message)}")
        unchecked = ", ".join(f"#{s}" for s in r.unchecked) or "none"
        console.print(
            f"  [dim]{len(r.errors)} blocking · {len(r.warnings)} warning(s) · "
            f"unchecked: {escape(unchecked)} (prose)[/dim]"
        )
    raise typer.Exit(0 if ok else 1)


@spec_app.command("verify")
def verify_cmd(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Specs: id, name or file (default: every spec)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    strict: Annotated[
        bool, typer.Option("--strict", help="Unfinished work (pending, untested) fails too")
    ] = False,
    no_tests: Annotated[
        bool, typer.Option("--no-tests", help="Skip running tests (R009/R010 not checked)")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable report")] = False,
) -> None:
    """Does the implementation match the spec, step by step? Zero tokens (R001–R010)."""
    from tuvl.core.config import settings
    from tuvl.runtime.code import load_agents
    from tuvl_insight.spec.parse import SpecError
    from tuvl_insight.spec.verify import verify_project

    project = project_dir.resolve()
    _load(project)
    load_agents(settings.dir_for("agents"))
    try:
        results = asyncio.run(
            verify_project(
                project, _refs(project, paths) if paths else None, run_tests=not no_tests
            )
        )
    except SpecError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    ok = all(not r.errors and not (strict and r.warnings) for r in results)
    if as_json:
        print(json.dumps({"ok": ok, "specs": [r.to_json() for r in results]}, indent=2))
        raise typer.Exit(0 if ok else 1)
    if not results:
        console.print("[dim]No specs found (specs/*.md).[/dim]")
    impl_mark = {
        "ok": "[green]✓[/green]",
        "pending": "[yellow]… pending[/yellow]",
        "mismatch": "[red]✗ mismatch[/red]",
        "missing": "[red]✗ missing[/red]",
    }
    marks = {"error": "[red]✗[/red]", "warning": "[yellow]![/yellow]", "waived": "[dim]·[/dim]"}
    for r in results:
        console.print(f"\n[bold]{escape(r.path)}[/bold]")
        for flow in r.flows:
            console.print(
                f"  [bold]{escape(flow.flow)}[/bold]"
                + ("" if flow.workflow else " [yellow](no workflow)[/yellow]")
            )
            for st in flow.steps:
                if st.tests.passed:
                    tests = f"{len(st.tests.passed)} passed"
                elif st.tests.mocked:
                    tests = f"mocked in {len(st.tests.mocked)}"
                else:
                    tests = "[dim]—[/dim]" if r.tests_run else "[dim]not run[/dim]"
                console.print(
                    f"    {escape(st.step):<22} [dim]{escape(st.engine[:28]):<28} :{st.line:<4}[/dim] "
                    f"{impl_mark[st.impl]:<12} {tests}"
                )
        for f in sorted(r.findings, key=lambda f: (f.severity != "error", f.line)):
            console.print(f"  {marks[f.severity]} {f.code:<5} :{f.line:<4} {escape(f.message)}")
        s = r.summary()
        console.print(
            f"  [dim]{len(r.errors)} mismatch(es) · steps implemented {s['implemented']}/{s['steps']} · "
            f"exercised by real tests {s['exercised']}/{s['steps']}[/dim]"
        )
    raise typer.Exit(0 if ok else 1)


@spec_app.command("new")
def new_cmd(
    name: Annotated[str, typer.Argument(help="Spec name, e.g. refund-automation")],
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    owner: Annotated[str, typer.Option("--owner", help="Owning team")] = "",
    workflow: Annotated[
        str | None,
        typer.Option("--workflow", help="Workflow the examples run (default: from name)"),
    ] = None,
    idea: Annotated[
        list[str] | None,
        typer.Option("--idea", help="The idea(s) it comes from; numbers the spec SPEC-XX-NNNN-NN"),
    ] = None,
) -> None:
    """Write specs/<name>.md from the template (numbered and linked to its idea with --idea)."""
    from tuvl_insight.spec import ids, lifecycle
    from tuvl_insight.spec.parse import slug

    project = project_dir.resolve()
    stem = slug(name)
    flow = workflow or stem.replace("-", "_")
    text = SPEC_TEMPLATE.format(name=stem, owner=owner or "unassigned", workflow=flow)
    if idea:
        known = ids.idea_ids(project)
        missing = [i for i in idea if i not in known]
        if missing:
            console.print(f"[red]Not an idea in this project: {', '.join(missing)}.[/red]")
            raise typer.Exit(1)
        spec_id = ids.next_spec_id(project, idea[0])
        text = lifecycle.with_front(
            text, {"id": spec_id, "ideas": list(idea), "status": "draft", "rev": 1}
        )
        target = project / "specs" / f"{spec_id}.md"
    else:
        target = project / "specs" / f"{stem}.md"
    if target.exists():
        console.print(f"[red]{escape(str(target))} already exists.[/red]")
        raise typer.Exit(1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if idea:
        lifecycle.sync_idea_links(project)
        from tuvl_insight import audit

        audit.emit(
            project, "spec.created", command="create spec", entities=[spec_id, *idea], detail=stem
        )
    rel = escape(str(target.relative_to(project)))
    console.print(
        f"[green]✓[/green] Created [bold]{rel}[/bold]\nNext: fill it in, then `tuvl lint spec {rel}`",
        soft_wrap=True,
    )


# ── ideas ────────────────────────────────────────────────────────────────────


def _initials(project: Path, given: str | None) -> str:
    from tuvl_insight.coding_agents import Settings, read_settings, valid_initials, write_settings

    settings = read_settings(project)
    if given is not None:
        given = given.strip().upper()
        if not valid_initials(given):
            console.print(
                "[red]--initials must be 2-4 letters or digits, starting with a letter.[/red]"
            )
            raise typer.Exit(2)
        if settings is None or not settings.initials:
            write_settings(project, Settings(given, settings.agent if settings else "none"))
        return given
    if settings is None or not settings.initials:
        console.print(
            "[red]No developer initials set.[/red] Ideas are numbered with them: "
            "pass [bold]--initials XX[/bold] once (it is saved in .tuvl/agent.yaml)."
        )
        raise typer.Exit(1)
    return settings.initials


def _commit_idea(project: Path, idea: str, title: str) -> str:
    """Commit one idea's folder and the index; ``""`` (with a note) when git cannot."""
    from tuvl_insight import gitflow, gitutil

    if not gitutil.inside_work_tree(project):
        console.print("[yellow]--commit: this is not a git repository, nothing committed.[/yellow]")
        return ""
    try:
        return gitflow.commit(project, f"{idea}: {title}", [f"idea/{idea}", "idea/INDEX.yaml"])
    except gitflow.GitFlowError as exc:
        console.print(f"[yellow]--commit failed: {escape(str(exc))}[/yellow]")
        return ""


@spec_app.command("import")
def import_cmd(
    paths: Annotated[
        list[Path] | None, typer.Argument(help="A file or folder holding the idea (repeatable)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    url: Annotated[
        list[str] | None,
        typer.Option(
            "--url",
            help="A web page, Jira issue, Confluence page or GitHub issue to snapshot (repeatable)",
        ),
    ] = None,
    allow_private: Annotated[
        bool,
        typer.Option(
            "--allow-private", help="Let --url reach internal addresses (your own Jira or wiki)"
        ),
    ] = False,
    title: Annotated[
        str | None, typer.Option("--title", help="Idea title (default: first heading)")
    ] = None,
    supersedes: Annotated[
        str | None, typer.Option("--supersedes", help="The idea this one replaces (IDEA-XX-NNNN)")
    ] = None,
    initials: Annotated[
        str | None, typer.Option("--initials", help="Your initials (saved on first use)")
    ] = None,
    include: Annotated[list[str] | None, typer.Option("--include", help="Only these globs")] = None,
    exclude: Annotated[list[str] | None, typer.Option("--exclude", help="Skip these globs")] = None,
    max_size: Annotated[int, typer.Option("--max-size", help="Largest file, in MB")] = 10,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be imported")] = False,
    commit: Annotated[
        bool,
        typer.Option(
            "--commit", help="Commit the new idea (needed before `generate spec` can use it)"
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable result")] = False,
) -> None:
    """Copy an idea (files, a folder, or a snapshot of a URL) into idea/IDEA-<initials>-NNNN/ as a numbered, immutable record."""
    import tempfile

    from tuvl_insight.spec.fetch import FetchError, fetch_into, safe_url
    from tuvl_insight.spec.ideas import IdeaError, plan_import

    urls = [u.strip() for u in url or [] if u.strip()]
    if not (paths or urls):
        console.print("[red]Name a file or folder, or pass --url.[/red]")
        raise typer.Exit(2)
    limit = max_size * 1024 * 1024
    with tempfile.TemporaryDirectory(prefix="tuvl-url-") as snapshots:
        try:
            plan = (
                plan_import(paths, max_file=limit, include=include, exclude=exclude)
                if paths
                else None
            )
            if urls and not dry_run:
                fetched = fetch_into(
                    urls, Path(snapshots), allow_private=allow_private, max_bytes=limit
                )
                pages = plan_import([Path(snapshots)], max_file=limit)
                origins = {f.name: f.origin for f in fetched}
                for item in pages.files:
                    item.origin = origins.get(item.relative)
                if plan is None:
                    plan = pages
                else:
                    taken = {f.relative for f in plan.files}
                    plan.files += [f for f in pages.files if f.relative not in taken]
                    plan.skipped += pages.skipped
        except (FetchError, IdeaError) as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
        _import_idea(
            project_dir.resolve(), plan, [safe_url(u) for u in urls], title, supersedes, initials,
            dry_run=dry_run, commit=commit, as_json=as_json,
        )  # fmt: skip


def _import_idea(
    project: Path,
    plan: Any,
    urls: list[str],
    title: str | None,
    supersedes: str | None,
    initials: str | None,
    *,
    dry_run: bool,
    commit: bool,
    as_json: bool,
) -> None:
    from tuvl_insight import gitutil
    from tuvl_insight.spec import ids
    from tuvl_insight.spec.ideas import IdeaError, ImportPlan, import_idea

    who = _initials(project, initials)
    plan = plan or ImportPlan()
    try:
        if dry_run:
            result = {
                "id": ids.next_idea_id(project, who),
                "files": [f.relative for f in plan.files],
                "urls": urls,
                "skipped": [{"path": p, "reason": r} for p, r in plan.skipped],
                "bytes": plan.total,
            }
            if as_json:
                typer.echo(json.dumps(result, indent=2))
            else:
                console.print(f"Would import [bold]{result['id']}[/bold] ({plan.total:,} bytes):")
                for f in plan.files:
                    console.print(f"  {escape(f.relative)}")
                for u in urls:
                    console.print(f"  {escape(u)} [dim](fetched on import)[/dim]")
                for p, r in plan.skipped:
                    console.print(f"  [dim]skip {escape(p)} — {r}[/dim]")
            return
        name, email = (
            gitutil.config_get(project, "user.name"),
            gitutil.config_get(project, "user.email"),
        )
        by = f"{name} <{email}>".strip() if name or email else who
        done = import_idea(
            project, plan, initials=who, title=title, supersedes=supersedes, imported_by=by
        )
        from tuvl_insight import audit

        audit.emit(
            project,
            "idea.imported",
            command="import idea",
            entities=[done.id] + ([supersedes] if supersedes else []),
            detail=done.title,
            refs={"files": len(done.files), "skipped": len(plan.skipped), "urls": len(urls)},
        )
    except IdeaError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    committed = _commit_idea(project, done.id, done.title) if commit else ""
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "id": done.id,
                    "title": done.title,
                    "folder": str(done.folder.relative_to(project)),
                    "files": done.files,
                    "skipped": [{"path": p, "reason": r} for p, r in plan.skipped],
                    "warnings": done.warnings,
                },
                indent=2,
            )
        )
        return
    console.print(f"Imported [bold]{done.id}[/bold] — {escape(done.title)}")
    if committed:
        console.print(f"  committed {committed}")
    console.print(
        f"  {len(done.files)} file(s) in {escape(str(done.folder.relative_to(project)))}/"
    )
    for p, r in plan.skipped:
        console.print(f"  [dim]skipped {escape(p)} — {r}[/dim]")
    for w in done.warnings:
        console.print(f"  [yellow]{escape(w)}[/yellow]")
    console.print(
        f"Next: [bold]tuvl generate spec --idea {done.id}[/bold] (a coding agent drafts the spec), or "
        f"[bold]tuvl create spec <name> --idea {done.id}[/bold] to write it yourself"
    )


@spec_app.command("ideas")
def ideas_cmd(
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable list")] = False,
) -> None:
    """List the project's ideas and the specs generated from each."""
    import json

    from tuvl_insight.spec import ids

    rows = ids.read_index(project_dir.resolve())
    if as_json:
        typer.echo(json.dumps({"ideas": rows}, indent=2, default=str))
        return
    if not rows:
        console.print("No ideas yet. Add one with [bold]tuvl import idea <file-or-folder>[/bold].")
        return
    for row in rows:
        sup = f" [dim](supersedes {row['supersedes']})[/dim]" if row.get("supersedes") else ""
        specs = ", ".join(row.get("specs") or []) or "no spec yet"
        console.print(f"[bold]{row['id']}[/bold] {escape(str(row.get('title', '')))}{sup}")
        console.print(f"    [dim]{row.get('imported_at', '')} · {specs}[/dim]")


@spec_app.command("idea")
def idea_cmd(
    idea: Annotated[str, typer.Argument(help="Idea id, e.g. IDEA-SK-0004")],
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable record")] = False,
) -> None:
    """Show one idea: its files, their hashes and the specs generated from it."""
    import json

    from tuvl_insight.spec.ideas import IdeaError, read_idea

    try:
        record = read_idea(project_dir.resolve(), idea)
    except IdeaError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    if as_json:
        typer.echo(json.dumps(record, indent=2, default=str))
        return
    console.print(f"[bold]{record['id']}[/bold] {escape(str(record.get('title', '')))}")
    console.print(
        f"  imported {record.get('imported_at', '')} by {escape(str(record.get('imported_by', '')))}"
    )
    if record.get("supersedes"):
        console.print(f"  supersedes {record['supersedes']}")
    for f in record.get("files", []):
        extra = f"  → {f['extracted']}" if f.get("extracted") else ""
        console.print(f"  {f['path']}  [dim]{f['sha256'][:12]}{extra}[/dim]")
        if f.get("warning"):
            console.print(f"    [yellow]{escape(f['warning'])}[/yellow]")
    console.print("  specs: " + (", ".join(record.get("specs") or []) or "none yet"))


@spec_app.command("renumber")
def renumber_cmd(
    idea: Annotated[str, typer.Argument(help="The idea to renumber, e.g. IDEA-SK-0004")],
    to: Annotated[
        str | None, typer.Option("--to", help="New idea id (default: the next free one)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
) -> None:
    """Give an idea a new number (after a duplicate) and rewrite every spec and task id derived from it."""
    from tuvl_insight.spec.renumber import RenumberError, renumber_idea

    try:
        done = renumber_idea(project_dir.resolve(), idea, to)
    except RenumberError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    from tuvl_insight import audit

    audit.emit(
        project_dir.resolve(),
        "id.renumbered",
        command="renumber idea",
        entities=[done.old, done.new],
        refs={"from": done.old, "to": done.new, "files": len(done.changed)},
    )
    console.print(f"{idea} → [bold]{done.new}[/bold]")
    for path in done.changed:
        console.print(f"  [dim]{escape(path)}[/dim]")


# ── lifecycle ────────────────────────────────────────────────────────────────


def _who(project: Path) -> str:
    from tuvl_insight import gitutil
    from tuvl_insight.coding_agents import read_settings

    name, email = (
        gitutil.config_get(project, "user.name"),
        gitutil.config_get(project, "user.email"),
    )
    if name:
        return f"{name} <{email}>" if email else name
    settings = read_settings(project)
    if settings and settings.initials:
        return settings.initials
    console.print(
        "[red]Cannot tell who you are.[/red] Set git user.name, or your initials with "
        "`tuvl import idea --initials XX`."
    )
    raise typer.Exit(1)


def _record(
    project: Path,
    kind: str,
    file: Path,
    command: str,
    *,
    result: str = "ok",
    detail: str = "",
    **refs: object,
) -> None:
    from tuvl_insight import audit
    from tuvl_insight.spec import lifecycle

    state = lifecycle.state_of(file.read_text())
    shown = str(file.relative_to(project)) if file.is_relative_to(project) else str(file)
    audit.emit(
        project,
        kind,
        command=command,
        entities=[state.id or shown],
        result=result,
        detail=detail,
        refs=dict(refs),
    )


def _resolve(project: Path, ref: str) -> Path:
    from tuvl_insight.spec.lifecycle import LifecycleError, resolve_spec

    try:
        return resolve_spec(project, ref)
    except LifecycleError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None


def _refs(project: Path, refs: list[Path] | Path | None) -> list[Path] | None:
    """Spec files from whatever was typed: a path, a spec id or a name."""
    if refs is None:
        return None
    return [_resolve(project, str(r)) for r in ([refs] if isinstance(refs, Path) else refs)]


@spec_app.command("approve")
def approve_cmd(
    spec: Annotated[str, typer.Argument(help="Spec id (SPEC-SK-0004-01), file or name")],
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    force: Annotated[
        bool, typer.Option("--force", help="Approve although `lint spec` reports errors")
    ] = False,
) -> None:
    """Approve a spec: record who, when and a hash of the body. Any later edit makes it a draft again."""
    from tuvl_insight.spec import lifecycle
    from tuvl_insight.spec.check import check_project

    project = project_dir.resolve()
    file = _resolve(project, spec)
    text = file.read_text()
    try:
        lifecycle.approve(text, by="check")  # refuses already-approved / superseded before any work
    except lifecycle.LifecycleError as exc:
        _record(project, "spec.approved", file, "approve spec", result="refused", detail=str(exc))
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    if not force:
        _load(project)
        # S017 is the stale approval this command is about to renew
        errors = [e for r in check_project(project, [file]) for e in r.errors if e.code != "S017"]
        if errors:
            _record(
                project,
                "spec.approved",
                file,
                "approve spec",
                result="refused",
                detail=f"{len(errors)} blocking finding(s)",
                codes=sorted({e.code for e in errors}),
            )
            console.print(
                f"[red]{len(errors)} blocking finding(s):[/red] run `tuvl lint spec {escape(spec)}`."
            )
            for e in errors[:5]:
                console.print(f"  {e.code} line {e.line}: {escape(e.message)}")
            raise typer.Exit(1)
    who = _who(project)
    file.write_text(lifecycle.approve(text, by=who))
    lifecycle.refresh_plan_hash(project, file)
    lifecycle.sync_idea_links(project)
    state = lifecycle.state_of(file.read_text())
    _record(
        project,
        "spec.approved",
        file,
        "approve spec",
        detail=who,
        approved_hash=state.approved_hash,
        forced=force,
    )
    console.print(
        f"Approved [bold]{escape(state.id or file.stem)}[/bold] as {escape(who)} (body {state.approved_hash})"
    )
    console.print(f"Next: [bold]tuvl generate tasks {escape(state.id or file.stem)}[/bold]")


@spec_app.command("reject")
def reject_cmd(
    spec: Annotated[str, typer.Argument(help="Spec id, file or name")],
    reason: Annotated[str, typer.Option("--reason", help="Why it goes back to draft")],
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
) -> None:
    """Send a spec back to draft with a reason (clears any approval)."""
    from tuvl_insight.spec import lifecycle

    project = project_dir.resolve()
    file = _resolve(project, spec)
    try:
        file.write_text(lifecycle.reject(file.read_text(), by=_who(project), reason=reason))
    except lifecycle.LifecycleError as exc:
        _record(project, "spec.rejected", file, "reject spec", result="refused", detail=str(exc))
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    lifecycle.refresh_plan_hash(project, file)
    _record(project, "spec.rejected", file, "reject spec", detail=reason.strip())
    console.print(f"[bold]{escape(spec)}[/bold] is a draft again: {escape(reason.strip())}")


@spec_app.command("reopen")
def reopen_cmd(
    spec: Annotated[str, typer.Argument(help="Spec id, file or name")],
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
) -> None:
    """Withdraw an approval so the spec can be edited."""
    from tuvl_insight.spec import lifecycle

    project = project_dir.resolve()
    file = _resolve(project, spec)
    try:
        file.write_text(lifecycle.reopen(file.read_text()))
    except lifecycle.LifecycleError as exc:
        _record(project, "spec.reopened", file, "reopen spec", result="refused", detail=str(exc))
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    lifecycle.refresh_plan_hash(project, file)
    _record(project, "spec.reopened", file, "reopen spec")
    console.print(f"[bold]{escape(spec)}[/bold] is a draft again")


@spec_app.command("tasks")
def tasks_cmd(
    spec: Annotated[str, typer.Argument(help="Spec id, file or name")],
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the task list; needs no approval, writes nothing"),
    ] = False,
) -> None:
    """Derive the task list from an approved spec: one task per new model, step and example. Zero tokens."""
    import yaml

    from tuvl_insight.spec import derive, lifecycle
    from tuvl_insight.spec.grammar import parse_structure
    from tuvl_insight.spec.parse import SpecError, parse_text
    from tuvl_insight.spec.tasks import plan_for

    project = project_dir.resolve()
    file = _resolve(project, spec)
    text = file.read_text()
    state = lifecycle.state_of(text)
    if not dry_run and state.effective != "approved":
        why = (
            "it was approved, but its body has changed since"
            if state.stale
            else f"it is {state.status}"
        )
        _record(project, "tasks.derived", file, "spec tasks", result="refused", detail=why)
        console.print(
            f"[red]Tasks come from an approved spec, and {escape(spec)} is not: {why}.[/red] "
            f"Run `tuvl approve spec {escape(spec)}` (or --dry-run to preview)."
        )
        raise typer.Exit(1)
    shown = str(file.relative_to(project)) if file.is_relative_to(project) else str(file)
    try:
        parsed = parse_text(text, path=shown, default_name=file.stem)
    except SpecError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    structure = parse_structure(text)
    if not (structure.has_data or structure.has_flows):
        console.print(
            "[red]The spec has no `# Data` or `# Flows`: nothing to derive tasks from.[/red]"
        )
        raise typer.Exit(1)
    target = plan_for(project, parsed) or file.with_suffix(".tasks.yaml")
    before = yaml.safe_load(target.read_text()) if target.is_file() else None
    document = derive.merge(parsed, structure, before)
    dumped = derive.dump(document)
    if dry_run:
        typer.echo(dumped)
        return
    target.write_text(dumped)
    old_ids = {t["id"] for t in (before or {}).get("spec", {}).get("tasks", [])}
    tasks = document["spec"]["tasks"]
    added = [t for t in tasks if t["id"] not in old_ids]
    obsolete = [t for t in tasks if t.get("obsolete")]
    _record(
        project,
        "tasks.derived",
        file,
        "spec tasks",
        tasks=len(tasks),
        new=len(added),
        obsolete=len(obsolete),
    )
    console.print(
        f"Wrote [bold]{escape(str(target.relative_to(project)))}[/bold]: {len(tasks)} task(s), "
        f"{len(added)} new, {len(obsolete)} obsolete"
    )
    console.print(f"Next: [bold]tuvl lint spec {escape(shown)}[/bold]")


@spec_app.command("list")
def list_cmd(
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable list")] = False,
) -> None:
    """List the specs with their id, status (approved / draft / stale) and approver."""
    from tuvl_insight.spec import lifecycle

    project = project_dir.resolve()
    rows: list[dict[str, Any]] = []
    for file in sorted((project / "specs").glob("*.md")):
        state = lifecycle.state_of(file.read_text())
        rows.append(
            {
                "file": str(file.relative_to(project)),
                "id": state.id,
                "status": state.status,
                "stale": state.stale,
                "approved_by": state.approved_by,
                "approved_at": state.approved_at,
                "ideas": state.ideas,
            }
        )
    if as_json:
        typer.echo(json.dumps({"specs": rows}, indent=2))
        return
    if not rows:
        console.print(
            "No specs yet. Write one with `tuvl create spec` or `tuvl create spec --idea IDEA-…`."
        )
    for r in rows:
        status = "[yellow]approved, changed since[/yellow]" if r["stale"] else r["status"]
        who = (
            f" by {escape(r['approved_by'])}"
            if r["status"] == "approved" and not r["stale"]
            else ""
        )
        console.print(
            f"[bold]{escape(r['id'] or r['file'])}[/bold]  {status}{who}  [dim]{', '.join(r['ideas'])}[/dim]"
        )


# ── watch ────────────────────────────────────────────────────────────────────

_WATCHED = ("models", "workflows", "agents", "specs", "tests", "artifacts", "llms", "datasources")


def _fingerprint(project: Path) -> tuple[tuple[str, int], ...]:
    seen: list[tuple[str, int]] = []
    for name in _WATCHED:
        root = project / name
        if root.is_dir():
            seen.extend(
                (str(p), p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()
            )
    lock = project / "tuvl.lock"
    if lock.is_file():
        seen.append((str(lock), lock.stat().st_mtime_ns))
    return tuple(seen)


def _status_view(project: Path, spec_file: Path | None, no_tests: bool):  # noqa: ANN202
    """``(plans, state)`` — derived statuses, recomputed only when a project file changed."""
    from tuvl_insight.spec.tasks import derived_plans, project_state

    use_project(str(project))
    state = asyncio.run(project_state(project, run_tests=not no_tests))
    return derived_plans(project, state, spec_file), state


def _pause(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _watch_status(project: Path, spec_file: Path | None, no_tests: bool, interval: float) -> None:
    from rich.live import Live
    from rich.text import Text

    from tuvl_insight.audit import live as live_state
    from tuvl_insight.spec.tasks import open_count

    seen: tuple | None = None
    view: tuple | None = None
    error = ""
    try:
        with Live(console=console, refresh_per_second=4, screen=False) as screen:
            while True:
                now = _fingerprint(project)
                if now != seen:
                    seen = now
                    try:
                        view, error = _status_view(project, spec_file, no_tests), ""
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"
                overlay = live_state.read(project)
                lines: list[str] = []
                if error:
                    lines.append(f"[red]{escape(error)}[/red]")
                if view is None:
                    lines.append("[dim]waiting for the project to load…[/dim]")
                else:
                    plans, state = view
                    for plan, tasks in plans:
                        running = overlay.get(plan.metadata.spec, {})
                        lines.append(
                            f"\n[bold]{escape(plan.metadata.spec)}[/bold]  [dim]{open_count(tasks)} open[/dim]"
                        )
                        for t in tasks:
                            shown = "obsolete" if t.obsolete else t.status
                            now_doing = running.get(t.id)
                            extra = (
                                f"  [bold cyan]{escape(live_state.describe(now_doing))}[/bold cyan]"
                                if now_doing
                                else ""
                            )
                            title = escape(t.title)
                            lines.append(
                                f"  {t.id:<22} [{_STYLE.get(shown, 'dim')}]{shown:<11}[/] {title}{extra}"
                            )
                    if state.tests is not None:
                        passed = sum(1 for t in state.tests if t.passed)
                        lines.append(f"\n[dim]tests {passed}/{len(state.tests)} passing[/dim]")
                lines.append(f"[dim]watching {escape(str(project))} — Ctrl-C to stop[/dim]")
                screen.update(Text.from_markup("\n".join(lines)))
                _pause(interval)
    except KeyboardInterrupt:
        return


# ── gen ──────────────────────────────────────────────────────────────────────


@spec_app.command("gen")
def gen_cmd(
    idea: Annotated[
        list[str] | None, typer.Option("--idea", help="Idea(s) to write the spec from (repeatable)")
    ] = None,
    from_path: Annotated[
        list[Path] | None,
        typer.Option("--from", help="Import this file/folder as a new idea (and commit it) first"),
    ] = None,
    title: Annotated[
        str | None, typer.Option("--title", help="Spec title (default: the idea's)")
    ] = None,
    agent: Annotated[
        str | None, typer.Option("--agent", help="Coding agent for this run (default: yours)")
    ] = None,
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    max_attempts: Annotated[int, typer.Option("--max-attempts", help="Tries before giving up")] = 3,
    budget: Annotated[
        float, typer.Option("--budget", help="Most USD one attempt may spend (Claude Code)")
    ] = 1.0,
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds per attempt")] = 600,
    model: Annotated[
        str | None, typer.Option("--model", help="Model, if the agent takes one")
    ] = None,
    base: Annotated[
        str | None, typer.Option("--base", help="Branch to start from (default develop)")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan and what would be sent; run nothing")
    ] = False,
    show_prompt: Annotated[
        bool, typer.Option("--show-prompt", help="Print the exact prompt")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask before committing the draft")
    ] = False,
    consent: Annotated[
        bool, typer.Option("--consent", help="Agree that idea files are sent to your coding agent")
    ] = False,
    no_pr: Annotated[
        bool, typer.Option("--no-pr", help="Commit on the branch; push nothing")
    ] = False,
    ready: Annotated[
        bool, typer.Option("--ready", help="Open the pull request ready for review, not as a draft")
    ] = False,
    keep_worktree: Annotated[
        bool, typer.Option("--keep-worktree", help="Leave the work folder in .tuvl/worktrees")
    ] = False,
    here: Annotated[
        bool,
        typer.Option(
            "--here",
            help="Write the draft into this folder, uncommitted: no branch, no pull request",
        ),
    ] = False,
) -> None:
    """A coding agent writes a draft spec from an idea, on a spec/<id> branch with a pull request."""
    from tuvl.cli import agent_run
    from tuvl_insight import audit, gitflow, specgen
    from tuvl_insight.coding_agents import runner
    from tuvl_insight.spec import ids

    project = project_dir.resolve()
    chosen = agent_run.choose_agent(project, agent)
    agent_id, command, label = chosen.id, chosen.command, chosen.label
    agent_run.need_git(project, "spec gen")

    ideas = list(idea or [])
    if from_path:
        if dry_run:
            console.print(
                "[red]--dry-run cannot be combined with --from: import the idea first.[/red]"
            )
            raise typer.Exit(2)
        from tuvl_insight.spec.ideas import IdeaError, import_idea, plan_import

        try:
            who = _initials(project, None)
            planned = plan_import(from_path)
            imported = import_idea(
                project, planned, initials=who, title=title, supersedes=None, imported_by=who
            )
        except IdeaError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
        _commit_idea(project, imported.id, imported.title)
        audit.emit(
            project,
            "idea.imported",
            command="generate spec",
            entities=[imported.id],
            detail=imported.title,
        )
        console.print(f"Imported and committed [bold]{imported.id}[/bold]")
        ideas.append(imported.id)
    if not ideas:
        console.print("[red]Name the idea:[/red] --idea IDEA-XX-NNNN, or --from <file-or-folder>.")
        raise typer.Exit(2)
    unknown = [i for i in ideas if i not in ids.idea_ids(project)]
    if unknown:
        console.print(f"[red]Not an idea in this project: {', '.join(unknown)}.[/red]")
        raise typer.Exit(1)

    spec_id = specgen.allocate_spec_id(project, ideas[0])
    idea_text, idea_warnings = specgen.gather_ideas(project, ideas)
    prompt = specgen.build_prompt(
        spec_id=spec_id, spec_path=f"specs/{spec_id}.md", ideas=ideas, idea_text=idea_text
    )
    if dry_run or show_prompt:
        console.print(f"[bold]{spec_id}[/bold] from {', '.join(ideas)} with {escape(label)}")
        console.print(f"  branch spec/{spec_id}; the agent may change only specs/{spec_id}.md")
        console.print(
            f"  sends {len(idea_text):,} characters of idea text ({len(prompt):,} in the prompt)"
        )
        for w in idea_warnings:
            console.print(f"  [yellow]{escape(w)}[/yellow]")
        if show_prompt:
            typer.echo("\n" + prompt)
        if dry_run:
            return

    agent_run.ensure_consent(
        project, chosen, flag=consent, what="the idea files", command="generate spec"
    )

    limits = runner.Limits(
        timeout=timeout, budget_usd=budget if agent_id == "claude" else None, model=model
    )
    base_ref = gitflow.base_branch(project, base)
    try:
        with console.status(f"{escape(label)} is writing {spec_id}…") as status:
            done = specgen.generate(
                project,
                ideas=ideas,
                title=title,
                agent_id=agent_id,
                command=command,
                base=base,
                max_attempts=max_attempts,
                limits=limits,
                on_attempt=lambda n, what: status.update(f"attempt {n}/{max_attempts}: {what}…"),
                agent_label=label,
            )
    except (specgen.SpecGenError, gitflow.GitFlowError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    cost = sum(a.cost_usd or 0 for a in done.attempts)
    refs = {
        "agent": label,
        "attempts": len(done.attempts),
        "cost_usd": round(cost, 4),
        "prompt_sha256": done.attempts[-1].prompt_sha if done.attempts else "",
        "output_sha256": done.attempts[-1].output_sha if done.attempts else "",
    }
    if not done.ok:
        audit.emit(
            project,
            "spec.generated",
            command="generate spec",
            entities=[done.spec_id, *ideas],
            result="failed",
            detail=(
                done.attempts[-1].problems[0]
                if done.attempts and done.attempts[-1].problems
                else ""
            )[:200],
            refs=refs,
        )
        console.print(f"[red]No valid draft after {len(done.attempts)} attempt(s).[/red]")
        for a in done.attempts:
            for p in a.problems[:5]:
                console.print(f"  attempt {a.number}: {escape(p)}")
        for w in done.warnings:
            console.print(f"  [yellow]{escape(w)}[/yellow]")
        raise typer.Exit(1)

    text = (done.worktree / done.spec_path).read_text()
    console.print(
        f"[green]✓[/green] {done.spec_id} drafted in {len(done.attempts)} attempt(s), {len(text.splitlines())} lines, ${cost:.3f}"
    )
    for w in [*idea_warnings, *done.warnings]:
        console.print(f"  [yellow]{escape(w)}[/yellow]")
    from tuvl_insight.spec import lifecycle as _lifecycle

    heading = str(_lifecycle.split_front(text)[0].get("title") or done.spec_id)
    final_title = title or heading
    if not (yes or here) and not typer.confirm(
        f"Commit this draft to branch {done.branch}?", default=True
    ):
        from tuvl_insight import gitflow as _g

        _g.remove_worktree(project, done.worktree)
        _g.delete_branch(project, done.branch)
        console.print("Discarded.")
        raise typer.Exit(1)
    if here:
        target = project / done.spec_path
        if target.exists():
            console.print(f"[red]{escape(done.spec_path)} already exists; nothing written.[/red]")
            raise typer.Exit(1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        gitflow.remove_worktree(project, done.worktree)
        gitflow.delete_branch(project, done.branch)
        audit.emit(
            project,
            "spec.generated",
            command="generate spec",
            entities=[done.spec_id, *ideas],
            refs=refs,
        )
        console.print(f"Wrote [bold]{done.spec_path}[/bold] in this folder (not committed).")
        console.print(
            f"Next: review it, then [bold]tuvl approve spec {done.spec_id}[/bold] and "
            f"[bold]tuvl generate tasks {done.spec_id}[/bold]."
        )
        return
    shown = len(done.warnings)
    finished = specgen.finish(
        project,
        done,
        ideas=ideas,
        title=final_title,
        base=base_ref,
        open_pr=not no_pr,
        draft=not ready,
        keep_worktree=keep_worktree,
    )
    audit.emit(
        project,
        "spec.generated",
        command="generate spec",
        entities=[done.spec_id, *ideas],
        refs=refs,
    )
    audit.emit(
        project,
        "branch.created",
        command="generate spec",
        entities=[done.spec_id],
        detail=done.branch,
    )
    audit.emit(
        project,
        "commit.created",
        command="generate spec",
        entities=[done.spec_id],
        detail=finished.commit,
        refs={"branch": done.branch},
    )
    if finished.pr_url:
        audit.emit(
            project,
            "pr.opened",
            command="generate spec",
            entities=[done.spec_id],
            detail=finished.pr_url,
        )
    console.print(f"Committed {finished.commit} on [bold]{done.branch}[/bold]")
    if finished.pr_url:
        console.print(f"Pull request: {escape(finished.pr_url)}")
    for w in finished.warnings[shown:]:
        console.print(f"  [yellow]{escape(w)}[/yellow]")
    console.print(
        f"Next: [bold]git switch {done.branch}[/bold], review and edit {done.spec_path}, then "
        f"[bold]tuvl approve spec {done.spec_id}[/bold] and [bold]tuvl generate tasks {done.spec_id}[/bold]."
    )
