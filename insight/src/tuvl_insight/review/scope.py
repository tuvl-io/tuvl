"""The code a spec (or some of its tasks) is responsible for, so a review can stay on it.

A task whose target names an agent of a workflow points at that agent's Python when the agent is a
``code`` agent: ``agents/<code.run>.py`` (one ``@agent`` per file, file name = run name). Tasks for
models, steps that are not code, tests and examples have no file of their own and add nothing.
"""

from __future__ import annotations

from pathlib import Path

from tuvl_insight.review.static import agents_in_workflows
from tuvl_insight.spec.lifecycle import LifecycleError, resolve_spec
from tuvl_insight.spec.tasks import TaskPlanError, plans
from tuvl_insight.taskrun import Job, TaskRunError, load_jobs, resolve_tokens


class ScopeError(ValueError):
    """The spec or task cannot be resolved."""


def jobs_of(project: Path, spec: str | None, tokens: list[str]) -> list[Job]:
    try:
        spec_file = resolve_spec(project, spec) if spec else None
        found = plans(project)
    except (LifecycleError, TaskPlanError) as exc:
        raise ScopeError(str(exc)) from exc
    jobs: list[Job] = []
    for _, plan in found:
        if spec_file is not None and (project / plan.metadata.spec).resolve() != spec_file:
            continue
        jobs += load_jobs(plan.model_dump(mode="json"))
    if spec is not None and not jobs:
        raise ScopeError(f"'{spec}' has no task list yet; run `tuvl generate tasks --spec {spec}`")
    if tokens:
        try:
            return resolve_tokens(jobs, tokens)
        except TaskRunError as exc:
            raise ScopeError(str(exc)) from exc
    return jobs


def files_for(project: Path, spec: str | None, tokens: list[str]) -> list[Path]:
    """The Python files of the code agents the chosen tasks target (the whole spec when none chosen)."""
    runs = {(a.workflow, a.agent): a.run for a in agents_in_workflows(project).values()}
    files: list[Path] = []
    for job in jobs_of(project, spec, tokens):
        run = runs.get((job.target.get("workflow", ""), job.target.get("agent", "")))
        path = project / "agents" / f"{run}.py" if run else None
        if path is not None and path.is_file() and path not in files:
            files.append(path)
    return files
