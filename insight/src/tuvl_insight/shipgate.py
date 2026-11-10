"""What must be true before ``tuvl ship`` packages anything: the deterministic gate, in one place.

Each check returns plain messages; ``ship`` decides what to print. Nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tuvl_insight import gitutil
from tuvl_insight.spec import lifecycle

_SHIP_OUTPUT = ("deploy/", "dist/", ".dockerignore")


@dataclass
class GateResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def spec_gate(project: Path, *, allow_incomplete: bool) -> GateResult:
    """Specs approved and unchanged, nothing unbuilt, the spec checks and verify clean."""
    out = GateResult()
    specs = sorted((project / "specs").glob("*.md")) if (project / "specs").is_dir() else []
    if not specs:
        out.notes.append("no specs: the spec gate does not apply")
        return out
    managed = 0
    for path in specs:
        state = lifecycle.state_of(path.read_text())
        front, _ = lifecycle.split_front(path.read_text())
        name = state.id or path.name
        if "status" not in front:
            out.warnings.append(
                f"{name} is not under approval (no `status:`): `tuvl approve spec` it"
            )
            continue
        managed += 1
        if state.stale:
            out.errors.append(
                f"{name} was approved, but its body changed since: `tuvl approve spec` it again"
            )
        elif state.status != "approved":
            out.errors.append(f"{name} is {state.status}: it must be approved before it ships")
    from tuvl.core.config import use_project
    from tuvl_insight.spec.check import check_project
    from tuvl_insight.spec.tasks import (
        TaskPlanError,
        derived_plans,
        open_count,
        project_state,
        selected_plans,
    )
    from tuvl_insight.spec.verify import verify_project

    use_project(str(project))
    for result in check_project(project):
        out.errors += [f"{result.path}: {f.code} line {f.line}: {f.message}" for f in result.errors]
    for verified in asyncio.run(verify_project(project, run_tests=False)):
        out.errors += [
            f"{verified.path}: {f.code} line {f.line}: {f.message}" for f in verified.errors
        ]
    try:
        has_plans = bool(selected_plans(project, None))
    except TaskPlanError as exc:
        out.errors.append(str(exc))
        has_plans = False
    if has_plans:
        project_status = asyncio.run(project_state(project, run_tests=True))
        for plan, tasks in derived_plans(project, project_status, None):
            left = open_count(tasks)
            if left and not allow_incomplete:
                names = [
                    t.id
                    for t in tasks
                    if not t.obsolete and (t.status != "done" or t.blocked or t.stale)
                ]
                out.errors.append(
                    f"{plan.metadata.spec}: {left} task(s) not done ({', '.join(names[:6])}"
                    f"{'…' if len(names) > 6 else ''}); `tuvl list tasks`, or --allow-incomplete"
                )
    out.notes.append(f"{managed} spec(s) under approval")
    return out


def codegen_current(project: Path) -> list[str]:
    """Generated files that are out of date (empty when ``tuvl generate code --check`` would pass)."""
    from tuvl_insight import codegen

    result = codegen.run(project, write=False)
    return [str(c["path"]) for c in result["would_change"]]


def git_state(project: Path, *, allow_dirty: bool) -> tuple[str, list[str]]:
    """``(commit, errors)``: the commit being shipped; an uncommitted change is an error unless allowed."""
    if not gitutil.available() or not gitutil.inside_work_tree(project):
        return "", []
    try:
        commit = gitutil.out(project, "rev-parse", "--short", "HEAD")
        status = gitutil.out(project, "status", "--porcelain", "-uall").splitlines()
        # what `tuvl ship` itself writes is not a change to the project
        dirty = [ln for ln in status if ln.strip() and not ln[3:].startswith(_SHIP_OUTPUT)]
    except (AttributeError, TypeError):  # a git that does not answer is no git
        return "", []
    if dirty and not allow_dirty:
        shown = ", ".join(ln[3:] for ln in dirty[:4]) + ("…" if len(dirty) > 4 else "")
        return commit, [
            f"{len(dirty)} uncommitted change(s) ({shown}): commit them, or --allow-dirty"
        ]
    return commit, []


def runtime_hash(project: Path, files: list[str]) -> str:
    """One sha256 over the runtime files' names and contents, in a fixed order."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        path = project / rel
        if not path.is_file():
            continue
        digest.update(rel.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def uv_lock_current(project: Path) -> str | None:
    """Why ``uv.lock`` cannot be trusted (missing or stale); ``None`` when it is current or uv is absent."""
    import shutil

    if not (project / "uv.lock").is_file():
        return "uv.lock is missing: run `uv lock`"
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        done = subprocess.run(
            [uv, "lock", "--check"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return None if getattr(done, "returncode", 0) == 0 else "uv.lock is out of date: run `uv lock`"
