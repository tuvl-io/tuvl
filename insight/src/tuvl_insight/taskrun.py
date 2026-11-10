"""``tuvl generate code --spec``: turn an approved spec's tasks into models and workflows.

Phase 1 is deterministic and free: the missing models, a workflow skeleton whose steps are all
``pending``, then the generated schemas, stubs, prompt placeholders and tests. It is one branch,
``feature/<spec>-scaffold``. Phase 2 hands each agent task to the coding agent on its own branch
``feature/<TASK-id>``, stacked on the one before: the agent replaces one pending step, tuvl
regenerates what is generated, and the task passes only when ``validate`` and ``verify spec`` accept
it. tuvl opens the pull requests and never merges them.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tuvl_insight import audit, gitflow
from tuvl_insight.audit import live, trace
from tuvl_insight.codegen import scaffold
from tuvl_insight.coding_agents import runner
from tuvl_insight.spec import ids, lifecycle
from tuvl_insight.spec.derive import _sequence


class TaskRunError(RuntimeError):
    pass


@dataclass
class ReviewPlan:
    """How a task's code is reviewed before it is committed."""

    mode: str = "static"  # none | static | agent
    blocking: str = (
        "off"  # off | high | medium | low: agent findings at least this severe fail the task
    )
    adapter: Any = None
    binary: str = ""
    label: str = ""
    limits: runner.Limits = field(default_factory=runner.Limits)


@dataclass
class Job:
    id: str
    title: str
    type: str
    target: dict[str, str]
    anchor: str = ""
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    job: Job
    ok: bool
    branch: str = ""
    commit: str = ""
    pr_url: str = ""
    attempts: int = 0
    cost_usd: float = 0.0
    problems: list[str] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    note: str = ""
    review: str = ""


@dataclass
class Report:
    scaffold_branch: str = ""
    scaffold_pr: str = ""
    scaffold_waiting: str = ""  # a scaffold branch built earlier that is not merged yet
    scaffolded: list[str] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.ok for o in self.outcomes)


# ── choosing tasks ───────────────────────────────────────────────────────────


def load_jobs(plan: dict[str, Any]) -> list[Job]:
    return [
        Job(
            str(t["id"]),
            str(t.get("title", "")),
            str(t.get("type", "")),
            dict(t.get("target") or {}),
            str((t.get("spec_ref") or {}).get("anchor", "")),
            [str(d) for d in t.get("depends_on") or []],
        )
        for t in (plan.get("spec") or {}).get("tasks") or []
        if not t.get("obsolete")
    ]


def resolve_tokens(jobs: list[Job], tokens: list[str]) -> list[Job]:
    """``3``, ``003``, ``T3`` or a full task id, to the tasks they name."""
    chosen: list[Job] = []
    for raw in tokens:
        for token in (t.strip() for t in raw.split(",") if t.strip()):
            hit = [j for j in jobs if j.id == token]
            if not hit and re.fullmatch(r"[Tt]?\d+", token):
                number = int(token.lstrip("Tt"))
                hit = [j for j in jobs if _sequence(j.id) == number]
            if not hit:
                raise TaskRunError(f"no task '{token}' in this spec's task list")
            for job in hit:
                if job not in chosen:
                    chosen.append(job)
    return chosen


def order(jobs: list[Job]) -> list[Job]:
    """Dependencies first; ties keep the task list's order."""
    ids_in = {j.id for j in jobs}
    done: set[str] = set()
    out: list[Job] = []
    pending = list(jobs)
    while pending:
        ready = [j for j in pending if all(d in done or d not in ids_in for d in j.depends_on)]
        if not ready:
            raise TaskRunError("the selected tasks depend on each other in a circle")
        for j in ready[:1]:
            out.append(j)
            done.add(j.id)
            pending.remove(j)
    return out


# ── the prompt ───────────────────────────────────────────────────────────────


def _skill(engine: str) -> str:
    name = f"agent-{engine}"
    try:
        return (importlib.resources.files("tuvl_insight.skills") / name / "SKILL.md").read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        return ""


def _step_text(flow_text: str, step_id: str) -> str:
    lines = flow_text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if re.match(rf"^\d+\.\s+`{re.escape(step_id)}`", ln)), None
    )
    if start is None:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^\d+\.\s+`", lines[i])), len(lines)
    )
    return "\n".join(lines[start:end]).rstrip()


def build_prompt(
    *,
    job: Job,
    spec_id: str,
    engine: str,
    step_text: str,
    flow_text: str,
    data_text: str,
    workflow_path: str,
    workflow_text: str,
    feedback: list[str] | None = None,
) -> str:
    agent = job.target.get("agent", "")
    parts = [
        f"You are implementing one task of the tuvl spec {spec_id}: **{job.id} — {job.title}**.",
        f"The workflow `{job.target.get('workflow')}` is in `{workflow_path}`, where step `{agent}` is still "
        "`engine: pending`. Replace that one agent with the real implementation.",
        "Rules:\n"
        f"- Change only `{workflow_path}`, files under `agents/` (but never `agents/_generated/`) and files under "
        "`artifacts/`. Change no other file and no other agent.\n"
        "- Keep the agent's `id`, `inputs`, `outputs`, `routes` and `budget` exactly as they are; they come from the "
        "spec. Choose the engine the spec names and fill in its block. Do not add fields or engines that do not exist.\n"
        "- For a `code` step also write `agents/<run>.py` with the function body; tuvl regenerates the decorator, "
        "signature and docstring around it.\n"
        "- You cannot run anything. tuvl will run `tuvl generate code`, `tuvl lint project` and `tuvl verify spec` and tell you "
        "what is still wrong.\n"
        "- Never put secrets in a file.",
        f"# The step in the spec\n\n{step_text}",
        f"# The whole flow\n\n{flow_text}",
        f"# Data\n\n{data_text}",
        f"# `{workflow_path}` now\n\n```yaml\n{workflow_text}\n```",
    ]
    skill = _skill(engine)
    if skill:
        parts.append(f"# How to write a `{engine}` agent\n\n{skill}")
    if feedback:
        parts.append(
            "# Your previous attempt is not accepted yet\nFix exactly this, change nothing else:\n"
            + "\n".join(f"- {line}" for line in feedback)
        )
    return "\n\n".join(parts)


def allowed(path: str) -> bool:
    """What the agent may touch for a step: the workflow, agent code and prompt artifacts."""
    if path.startswith(("agents/_generated/", "specs/", "idea/", "tests/", ".tuvl/", "models/")):
        return False
    return path.startswith(("workflows/", "artifacts/")) or (
        path.startswith("agents/") and path.endswith(".py")
    )


# ── the gate ─────────────────────────────────────────────────────────────────


def gate(worktree: Path, spec_file: Path, job: Job) -> list[str]:
    """Why the task is not implemented yet; empty when ``validate`` and ``verify spec`` accept it."""
    from tuvl.core.config import use_project
    from tuvl_insight.spec.tasks import derived_plans, project_state

    use_project(str(worktree))
    state = asyncio.run(project_state(worktree, run_tests=False))
    for _plan, tasks in derived_plans(worktree, state, spec_file):
        for t in tasks:
            if t.id == job.id:
                if t.status in ("implemented", "tested", "done") and not t.stale:
                    return []
                return t.reasons or [f"the step is still {t.status}"]
    return [f"{job.id} is not in the task list"]


def statuses(worktree: Path, spec_file: Path) -> dict[str, str]:
    from tuvl.core.config import use_project
    from tuvl_insight.spec.tasks import derived_plans, project_state

    use_project(str(worktree))
    state = asyncio.run(project_state(worktree, run_tests=False))
    return {
        t.id: t.status for _p, tasks in derived_plans(worktree, state, spec_file) for t in tasks
    }


def regenerate(worktree: Path) -> list[str]:
    """The deterministic pass: schemas, code stubs, prompt placeholders, tests."""
    from tuvl_insight import codegen

    result = codegen.run(worktree, write=True)
    return [c["path"] for c in result["written"]]


# ── the run ──────────────────────────────────────────────────────────────────


def task_branch(job: Job, spec_id: str) -> str:
    return f"feature/{job.id}" if ids.TASK_ID.match(job.id) else f"feature/{spec_id}-{job.id}"


def run_tasks(
    project: Path,
    *,
    spec_ref: str,
    tokens: list[str],
    all_tasks: bool,
    agent_id: str,
    command: tuple[str, ...] = (),
    agent_label: str = "",
    base: str | None = None,
    max_attempts: int = 3,
    limits: runner.Limits | None = None,
    use_agent: bool = True,
    open_prs: bool = True,
    draft: bool = True,
    stop_on_fail: bool = True,
    keep_worktree: bool = False,
    review: ReviewPlan | None = None,
    run: Callable[..., runner.RunResult] = runner.run_agent,
    say: Callable[[str], None] = lambda _m: None,
) -> Report:
    from tuvl_insight.spec.grammar import parse_structure
    from tuvl_insight.spec.parse import parse_text
    from tuvl_insight.spec.tasks import plan_for

    report = Report()
    spec_file = lifecycle.resolve_spec(project, spec_ref)
    text = spec_file.read_text()
    state = lifecycle.state_of(text)
    if state.effective != "approved":
        why = "approved, but its body changed since" if state.stale else state.status
        raise TaskRunError(f"{spec_ref} is not approved ({why}); run `tuvl approve spec` first")
    rel = str(spec_file.relative_to(project))
    spec = parse_text(text, path=rel, default_name=spec_file.stem)
    structure = parse_structure(text)
    plan_path = plan_for(project, spec)
    if plan_path is None:
        raise TaskRunError(f"{spec_ref} has no task list; run `tuvl generate tasks {spec_ref}`")
    plan = yaml.safe_load(plan_path.read_text()) or {}
    plan_rel = str(plan_path.relative_to(project))
    jobs = load_jobs(plan)
    spec_id = state.id or spec.name

    if not gitflow.has_commits(project):
        raise TaskRunError("the project has no commits yet")
    base_ref = gitflow.base_branch(project, base)
    seed: list[str] = []
    for needed in (rel, plan_rel):
        if gitflow.is_tracked(project, base_ref, needed):
            continue
        if not gitflow.is_tracked(project, "HEAD", needed):
            seed.append(needed)  # written in place (generate spec --here), never committed
        else:
            raise TaskRunError(
                f"{needed} is not on {base_ref}: merge the spec's pull request first, or start from its branch with --base"
            )
    selected = resolve_tokens(jobs, tokens) if tokens else [j for j in jobs if j.type == "agent"]
    if use_agent and not tokens and not all_tasks:
        raise TaskRunError("name the tasks (--task 3, --task 1,3) or use --all")
    flow_by_name = {f.name: f for f in structure.flows.values()}
    audit.emit(
        project,
        "codegen.started",
        command="generate code",
        entities=[spec_id],
        detail=f"{len(selected)} task(s)",
    )

    # ── phase 1: deterministic ──
    scaffold_branch = f"feature/{spec_id}-scaffold"
    wt0 = project / ".tuvl" / "worktrees" / f"{spec_id}-scaffold"
    if gitflow.branch_exists(project, scaffold_branch) and gitflow.is_merged(
        project, scaffold_branch, base_ref
    ):
        gitflow.delete_branch(
            project, scaffold_branch
        )  # landed on the base; only the label is left
    reuse = gitflow.branch_exists(project, scaffold_branch)
    if reuse:
        gitflow.add_worktree_on(project, wt0, scaffold_branch)
        report.scaffold_waiting = scaffold_branch
        say(f"scaffold: using {scaffold_branch}")
    else:
        gitflow.add_worktree(project, wt0, scaffold_branch, base_ref)
    keep0 = reuse
    try:
        if not reuse:
            for needed in seed:
                (wt0 / needed).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(project / needed, wt0 / needed)
            if seed:
                say(f"the spec and its task list ride on {scaffold_branch}: {', '.join(seed)}")
            created = scaffold.write_scaffold(wt0, spec, structure)
            written = regenerate(wt0) if created or _needs_regen(wt0) else []
            changed = gitflow.changed_paths(wt0)
            if changed:
                say(f"scaffold: {len(changed)} file(s)")
                gitflow.commit(
                    wt0,
                    f"{spec_id}: scaffold models, workflows and generated files\n\nDeterministic; no agent involved.",
                    changed,
                )
                report.scaffold_branch, report.scaffolded = scaffold_branch, changed
                audit.emit(
                    project,
                    "codegen.deterministic",
                    command="generate code",
                    entities=[spec_id],
                    detail=f"{len(changed)} file(s)",
                    refs={"created": created, "generated": written},
                )
                audit.emit(
                    project,
                    "branch.created",
                    command="generate code",
                    entities=[spec_id],
                    detail=scaffold_branch,
                )
                if open_prs:
                    report.scaffold_pr = _publish(
                        project,
                        wt0,
                        scaffold_branch,
                        base_ref,
                        f"{spec_id}: scaffold",
                        report,
                        draft,
                        spec_id,
                    )
            keep0 = bool(changed)
        current = statuses(wt0, wt0 / rel)
    finally:
        gitflow.remove_worktree(project, wt0)
        if not keep0:
            gitflow.delete_branch(project, scaffold_branch)
    stack_base = scaffold_branch if keep0 else base_ref

    if not use_agent:
        return report

    # ── phase 2: one task at a time ──
    todo: list[Job] = []
    for job in order(selected):
        if job.type != "agent":
            report.skipped.append((job.id, f"a {job.type} task: not for the agent"))
        elif current.get(job.id) in ("implemented", "tested", "done"):
            report.skipped.append((job.id, f"already {current[job.id]}"))
        elif "agent" not in job.target or job.target.get("workflow") not in flow_by_name:
            report.skipped.append((job.id, "its step is not in the spec"))
        else:
            todo.append(job)
    live.clear(project, rel)
    for job in todo:
        live.set_task(project, rel, job.id, "queued")
    failed: set[str] = set()
    adapter = runner.adapter_for(agent_id, command, write=True)
    binary = runner.binary_for(agent_id, command)
    for job in todo:
        existing = task_branch(job, spec_id)
        if gitflow.branch_exists(project, existing):
            if gitflow.is_merged(project, existing, base_ref):
                gitflow.delete_branch(project, existing)
            else:
                report.skipped.append((job.id, f"{existing} is already built and not merged yet"))
                stack_base = existing
                continue
        if any(d in failed for d in job.depends_on):
            report.skipped.append((job.id, "a task it depends on failed"))
            live.set_task(project, rel, job.id, "blocked")
            continue
        outcome = _one_task(
            project,
            job,
            spec_id=spec_id,
            spec=spec,
            structure=structure,
            rel=rel,
            stack_base=stack_base,
            adapter=adapter,
            binary=binary,
            max_attempts=max_attempts,
            limits=limits or runner.Limits(),
            agent_label=agent_label or agent_id,
            open_prs=open_prs,
            draft=draft,
            keep_worktree=keep_worktree,
            review=review or ReviewPlan(),
            run=run,
            say=say,
        )
        report.outcomes.append(outcome)
        if outcome.ok:
            stack_base = outcome.branch
        else:
            failed.add(job.id)
            if stop_on_fail:
                for rest in todo[todo.index(job) + 1 :]:
                    report.skipped.append((rest.id, "stopped after a failed task"))
                    live.set_task(project, rel, rest.id, "blocked")
                break
    return report


def _with_ui_specs(worktree: Path, changed: list[str]) -> list[str]:
    """Write the UI spec of every workflow this task completed, so it lands in the task's commit."""
    from tuvl_insight import uispec

    try:
        uispec.generate(worktree, run_tests=False)
    except Exception:
        return changed
    return gitflow.changed_paths(worktree)


def _needs_regen(worktree: Path) -> bool:
    return (worktree / "workflows").is_dir()


def _publish(
    project: Path,
    wt: Path,
    branch: str,
    base: str,
    title: str,
    report: Report,
    draft: bool,
    spec_id: str,
    body: str = "",
) -> str:
    if not gitflow.has_remote(project):
        report.warnings.append(f"{branch}: no remote configured, the branch is local")
        return ""
    try:
        gitflow.push(wt, branch)
    except gitflow.GitFlowError as exc:
        report.warnings.append(f"{branch}: push failed: {exc}")
        return ""
    forge = gitflow.forge(project)
    if forge is None:
        report.warnings.append(
            f"{branch}: pushed; no gh/glab for this remote, open the request yourself"
        )
        return ""
    try:
        url = gitflow.open_pr(
            wt,
            forge,
            branch=branch,
            base=base,
            title=title,
            draft=draft,
            body=body or f"Part of `{spec_id}`. Review and merge in order; tuvl never merges.\n",
        )
    except gitflow.GitFlowError as exc:
        report.warnings.append(f"{branch}: the pull request could not be opened: {exc}")
        return ""
    audit.emit(
        project,
        "pr.opened",
        command="generate code",
        entities=[spec_id],
        detail=url,
        refs={"branch": branch},
    )
    return url


def _one_task(
    project: Path,
    job: Job,
    *,
    spec_id: str,
    spec: Any,
    structure: Any,
    rel: str,
    stack_base: str,
    adapter: runner.Adapter,
    binary: str,
    max_attempts: int,
    limits: runner.Limits,
    agent_label: str,
    open_prs: bool,
    draft: bool,
    keep_worktree: bool,
    review: ReviewPlan,
    run: Callable[..., runner.RunResult],
    say: Callable[[str], None],
) -> Outcome:
    workflow, agent = job.target["workflow"], job.target["agent"]
    branch = task_branch(job, spec_id)
    outcome = Outcome(job, False, branch=branch)
    wt = project / ".tuvl" / "worktrees" / job.id
    gitflow.add_worktree(project, wt, branch, stack_base)
    flow = next(f for f in structure.flows.values() if f.name == workflow)
    step = flow.step(agent)
    section = spec.section(job.anchor)
    flow_text = section.text if section is not None else ""
    data_section = spec.section("data")
    wf_rel = f"workflows/{workflow}.yaml"
    audit.emit(
        project, "task.started", command="generate code", entities=[job.id], detail=job.title
    )
    feedback: list[str] | None = None
    history: list[dict[str, Any]] = []

    def failed_attempt(number: int, gate: str, problems: list[str], output: str = "") -> None:
        history.append(
            {
                "attempt": number,
                "gate": gate,
                "problems": [p[:600] for p in problems[:20]],
                **({"output": output[-2500:]} if output.strip() else {}),
            }
        )

    try:
        for number in range(1, max_attempts + 1):
            outcome.attempts = number
            live.set_task(
                project,
                rel,
                job.id,
                "running",
                attempt=number,
                max_attempts=max_attempts,
                gate="agent",
            )
            say(f"{job.id} attempt {number}/{max_attempts}: the agent is working")
            wf_path = wt / wf_rel
            prompt = build_prompt(
                job=job,
                spec_id=spec_id,
                engine=step.engine if step else "",
                step_text=_step_text(flow_text, agent),
                flow_text=flow_text,
                data_text=data_section.text if data_section is not None else "",
                workflow_path=wf_rel,
                workflow_text=wf_path.read_text() if wf_path.is_file() else "",
                feedback=feedback,
            )
            prompt_sha = trace.store(project, prompt)
            result = run(adapter, binary, prompt, workdir=wt, limits=limits)
            outcome.cost_usd += result.cost_usd or 0.0
            audit.emit(
                project,
                "task.attempt",
                command="generate code",
                entities=[job.id],
                result="ok" if result.ok else "failed",
                detail=f"attempt {number}",
                refs={
                    "prompt_sha256": prompt_sha,
                    "output_sha256": trace.store(project, result.stdout or result.error),
                    "cost_usd": result.cost_usd,
                    "agent": agent_label,
                },
            )
            if not result.ok:
                feedback = None
                outcome.problems = [f"the agent failed: {result.error or 'no reason given'}"]
                failed_attempt(
                    number,
                    "agent",
                    [
                        *outcome.problems,
                        f"exit code {result.returncode} after {result.seconds:.0f}s",
                        *[f"not allowed: {d}" for d in result.denials[:5]],
                    ],
                    result.stderr or result.stdout or result.text,
                )
                continue
            stray = [p for p in gitflow.changed_paths(wt) if not allowed(p)]
            if stray:
                gitflow.revert_paths(wt, stray)
                outcome.problems = [
                    f"you changed {p}, which you may not touch for this task" for p in stray
                ]
                feedback = list(outcome.problems)
                failed_attempt(number, "scope", outcome.problems)
                audit.emit(
                    project,
                    "scope.violation",
                    command="generate code",
                    entities=[job.id],
                    detail=", ".join(stray),
                )
                continue
            live.set_task(
                project,
                rel,
                job.id,
                "running",
                attempt=number,
                max_attempts=max_attempts,
                gate="validate",
            )
            try:
                regenerate(wt)
                why = gate(wt, wt / rel, job)
            except Exception as exc:
                why = [f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}"]
            audit.emit(
                project,
                "gate.result",
                command="generate code",
                entities=[job.id],
                result="ok" if not why else "failed",
                detail="; ".join(why)[:200],
                refs={"findings": why[:10]},
            )
            if why:
                outcome.problems = why
                feedback = why
                failed_attempt(number, "validate", why)
                continue
            changed = gitflow.changed_paths(wt)
            if review.mode != "none":
                live.set_task(
                    project,
                    rel,
                    job.id,
                    "running",
                    attempt=number,
                    max_attempts=max_attempts,
                    gate="review",
                )
                found, outcome.review = _review(
                    project, wt, job, changed, review, spec_id=spec_id, flow_text=flow_text, run=run
                )
                outcome.cost_usd += found["cost"]
                if found["blocking"]:
                    outcome.problems = found["blocking"]
                    feedback = found["blocking"]
                    failed_attempt(number, "review", found["blocking"])
                    continue
            changed = _with_ui_specs(wt, changed)
            outcome.commit = gitflow.commit(
                wt, f"{job.id}: {job.title}\n\nImplements step `{agent}` of `{workflow}`.", changed
            )
            outcome.ok, outcome.problems = True, []
            audit.emit(
                project,
                "task.implemented",
                command="generate code",
                entities=[job.id],
                detail=job.title,
            )
            audit.emit(
                project, "branch.created", command="generate code", entities=[job.id], detail=branch
            )
            audit.emit(
                project,
                "commit.created",
                command="generate code",
                entities=[job.id],
                detail=outcome.commit,
                refs={"branch": branch},
            )
            live.set_task(project, rel, job.id, "implemented")
            live.clear_failure(project, rel, job.id)
            if open_prs:
                rep = Report()
                outcome.pr_url = _publish(
                    project,
                    wt,
                    branch,
                    stack_base,
                    f"{job.id}: {job.title}",
                    rep,
                    draft,
                    spec_id,
                    body=(
                        f"Implements `{agent}` of workflow `{workflow}` ({spec_id}). "
                        f"Stacked on `{stack_base}`; merge in order.\n"
                        + (f"\n{outcome.review}" if outcome.review else "")
                    ),
                )
                outcome.note = "; ".join(rep.warnings)
                if outcome.pr_url:
                    num = re.search(r"/(\d+)$", outcome.pr_url)
                    live.set_task(
                        project,
                        rel,
                        job.id,
                        "in_review",
                        pr={
                            "url": outcome.pr_url,
                            **({"number": int(num.group(1))} if num else {}),
                        },
                    )
            return outcome
        audit.emit(
            project,
            "task.failed",
            command="generate code",
            entities=[job.id],
            result="failed",
            detail=(outcome.problems[0] if outcome.problems else "")[:200],
            refs={"attempts": [{k: v for k, v in h.items() if k != "output"} for h in history]},
        )
        summary = (outcome.problems[0] if outcome.problems else "")[:200]
        live.set_task(project, rel, job.id, "failed", finding=summary[:120])
        outcome.failure = {
            "summary": summary,
            "agent": agent_label,
            "max_attempts": max_attempts,
            "cost_usd": round(outcome.cost_usd, 4),
            "attempts": history,
        }
        live.set_failure(project, rel, job.id, outcome.failure)
        return outcome
    finally:
        if not keep_worktree or not outcome.ok:
            gitflow.remove_worktree(project, wt)
        if not outcome.ok:
            gitflow.delete_branch(project, branch)


def _review(
    project: Path,
    wt: Path,
    job: Job,
    changed: list[str],
    plan: ReviewPlan,
    *,
    spec_id: str,
    flow_text: str,
    run: Callable[..., runner.RunResult],
) -> tuple[dict[str, Any], str]:
    """Tier 1 on the changed Python, then (``agent`` mode) the agent's second opinion.

    Returns ``{"blocking": [...], "cost": usd}`` and the markdown for the pull request.
    """
    from tuvl_insight import gitutil
    from tuvl_insight.review import agent as agent_review
    from tuvl_insight.review import review_project

    py = [
        wt / p
        for p in changed
        if p.endswith(".py") and p.startswith("agents/") and "_generated" not in p
    ]
    if not py:  # a step without Python (a tool call, a decide rule) has nothing for a code review
        return {"blocking": [], "cost": 0.0}, ""
    static = review_project(wt, py)
    blocking = [f.text() for f in static.errors]
    result: dict[str, Any] = {"blocking": blocking, "cost": 0.0}
    if blocking or plan.mode != "agent":
        audit.emit(
            project,
            "review.completed",
            command="generate code",
            entities=[job.id],
            result="failed" if blocking else "ok",
            detail=f"static: {len(blocking)} error(s)",
            refs={"files": [str(p.relative_to(wt)) for p in py]},
        )
        return result, ""
    gitutil.git(wt, "add", "--", *changed)
    diff = gitutil.out(wt, "diff", "--cached")
    files = {str(p.relative_to(wt)): p.read_text() for p in py if p.is_file()}
    prompt = agent_review.build_prompt(
        subject=f"{job.id} — {job.title} ({spec_id})",
        spec_text=flow_text,
        diff=diff,
        files=files,
        tier1=static.findings,
    )
    audit.emit(
        project,
        "review.requested",
        command="generate code",
        entities=[job.id],
        detail=plan.label,
        refs={"prompt_sha256": trace.store(project, prompt)},
    )
    outcome = agent_review.review(
        wt, adapter=plan.adapter, binary=plan.binary, prompt=prompt, limits=plan.limits, run=run
    )
    result["cost"] = outcome.cost_usd or 0.0
    for f in outcome.findings[:20]:
        audit.emit(
            project,
            "review.finding",
            command="generate code",
            entities=[job.id],
            detail=f.text()[:200],
        )
    audit.emit(
        project,
        "review.completed",
        command="generate code",
        entities=[job.id],
        result="ok" if outcome.ok else "failed",
        detail=outcome.verdict or outcome.error[:120],
        refs={"findings": len(outcome.findings), "cost_usd": outcome.cost_usd},
    )
    result["blocking"] = [f.text() for f in outcome.at_least(plan.blocking)] if outcome.ok else []
    return result, agent_review.markdown(outcome, reviewer=plan.label)
