"""Where the branches of a spec's work stand: the spec, the scaffold and one branch per agent task.

The branches are stacked (each task is built on the one before it), so they merge in order. tuvl opens
the pull requests and never merges them; this report says what exists, what is merged and what to merge next.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tuvl_insight import gitflow, gitutil, taskrun
from tuvl_insight.audit import read_events
from tuvl_insight.spec import lifecycle
from tuvl_insight.spec.tasks import selected_plans

_US = "\x1f"


def _ref(project: Path, branch: str) -> str:
    """The local branch, else its remote copy, else ``""``."""
    if gitflow.branch_exists(project, branch):
        return branch
    remotes = gitutil.out(
        project, "for-each-ref", "--format=%(refname:short)", f"refs/remotes/*/{branch}"
    )
    return remotes.split()[0] if remotes else ""


def _last_commit(project: Path, ref: str) -> dict[str, str]:
    line = gitutil.out(project, "log", "-1", f"--format=%h{_US}%s{_US}%cI{_US}%an", ref)
    sha, subject, when, who = (line.split(_US) + ["", "", "", ""])[:4]
    return {"sha": sha, "subject": subject, "when": when, "author": who}


def _landed(
    project: Path, base: str, entry: dict[str, Any], spec_id: str, rel: str
) -> dict[str, str] | None:
    """Work whose branch is gone (deleted after the merge): the commit on the base branch that carries it."""
    if entry["kind"] == "spec":
        return (
            {"sha": "", "subject": rel, "when": "", "author": ""}
            if gitflow.is_tracked(project, base, rel)
            else None
        )
    start = f"{spec_id}: scaffold" if entry["kind"] == "scaffold" else f"{entry['task']}: "
    line = gitutil.out(
        project, "log", "-1", f"--format=%h{_US}%s{_US}%cI{_US}%an", f"--grep=^{start}", base
    )
    if not line:
        return None
    sha, subject, when, who = (line.split(_US) + ["", "", "", ""])[:4]
    return {"sha": sha, "subject": subject, "when": when, "author": who}


def _count(project: Path, spec: str) -> int:
    text = gitutil.out(project, "rev-list", "--count", spec)
    return int(text) if text.isdigit() else 0


def _parent(project: Path, base: str, entry: dict[str, Any], built: list[dict[str, Any]]) -> str:
    """The branch this one grew from: the nearest of the others whose commits it contains, else the base."""
    best, best_n = base, None
    for other in built:
        if other is entry or other["ref"] == entry["ref"]:
            continue
        if gitflow.is_merged(project, other["ref"], entry["ref"]):
            n = _count(project, f"{other['ref']}..{entry['ref']}")
            if best_n is None or n < best_n:
                best, best_n = other["branch"], n
    return best


def _commits(project: Path, parent_ref: str, ref: str, limit: int = 40) -> list[dict[str, str]]:
    """The commits a branch adds to the one it grew from, newest first, with their full messages."""
    raw = gitutil.out(
        project,
        "log",
        f"-{limit}",
        f"--format=%h{_US}%s{_US}%b{_US}%cI{_US}%an\x1e",
        f"{parent_ref}..{ref}",
    )
    out = []
    for record in raw.split("\x1e"):
        parts = (record.strip("\n").split(_US) + [""] * 5)[:5]
        if parts[0]:
            out.append(
                {
                    "sha": parts[0],
                    "subject": parts[1],
                    "body": parts[2].strip(),
                    "when": parts[3],
                    "author": parts[4],
                }
            )
    return out


def _worktrees(project: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    path = ""
    for line in gitutil.out(project, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line.startswith("branch refs/heads/"):
            out[line[18:]] = path
    return out


def _entries(project: Path, spec_id: str, jobs: list[taskrun.Job]) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = [
        {
            "kind": "spec",
            "branch": f"spec/{spec_id}",
            "title": "The spec (from generate spec)",
            "task": None,
        },
        {
            "kind": "scaffold",
            "branch": f"feature/{spec_id}-scaffold",
            "title": "Models, workflows and tests",
            "task": None,
        },
    ]
    agent_jobs = taskrun.order([j for j in jobs if j.type == "agent"])
    chain += [
        {"kind": "task", "branch": taskrun.task_branch(j, spec_id), "title": j.title, "task": j.id}
        for j in agent_jobs
    ]
    return chain


def report(project: Path, spec_file: Path | None = None) -> dict[str, Any]:
    """``tuvl list branches --json``: the base branch and, per spec, the stack of branches with their state."""
    if not gitflow.has_commits(project):
        return {"base": None, "current": "", "dirty": 0, "specs": []}
    base = gitflow.base_branch(project)
    prs = {
        str((e.get("refs") or {}).get("branch", "")): str(e.get("detail", ""))
        for e in read_events(project)
        if e.get("kind") == "pr.opened"
    }
    worktrees = _worktrees(project)
    specs: list[dict[str, Any]] = []
    for path, plan, _ in selected_plans(project, spec_file):
        rel = plan.metadata.spec
        source = project / rel
        spec_id = (
            lifecycle.state_of(source.read_text()).id if source.is_file() else ""
        ) or source.stem
        jobs = taskrun.load_jobs(yaml.safe_load(path.read_text()) or {})
        stack = _entries(project, spec_id, jobs)
        for entry in stack:
            branch = entry["branch"]
            ref = _ref(project, branch)
            entry["ref"] = ref
            entry["pr"] = prs.get(branch, "")
            entry["worktree"] = worktrees.get(branch, "")
            entry["state"] = "none"
            entry["ahead"] = entry["behind"] = 0
            entry["last"] = None
            entry["commits"] = []
            landed = None if ref else _landed(project, base, entry, spec_id, rel)
            if landed is not None:
                entry["state"], entry["last"], entry["deleted"] = "merged", landed, True
                entry["commits"] = [landed] if landed["sha"] else []
            if ref:
                merged = gitflow.is_merged(project, ref, base)
                entry["state"] = "merged" if merged else "open"
                entry["ahead"] = _count(project, f"{base}..{ref}")
                entry["behind"] = _count(project, f"{ref}..{base}")
                entry["last"] = _last_commit(project, ref)
        built = [e for e in stack if e["ref"]]
        for entry in built:
            entry["parent"] = _parent(project, base, entry, built)
        for index, entry in enumerate(stack):
            if not entry["ref"]:
                earlier = [e for e in stack[:index] if e["ref"] and e["state"] == "open"]
                entry["parent"] = earlier[-1]["branch"] if earlier else base
        by_branch = {e["branch"]: e for e in stack}
        for entry in built:
            parent = by_branch.get(entry["parent"])
            entry["commits"] = _commits(project, parent["ref"] if parent else base, entry["ref"])
            if not entry["commits"] and entry["state"] == "merged":
                carried = _landed(
                    project, base, entry, spec_id, rel
                )  # merged: the commit on the base carries it
                entry["commits"] = [carried] if carried and carried["sha"] else []
            first: list[str] = []
            node = parent
            while node is not None:
                if node["state"] == "open":
                    first.insert(0, node["branch"])
                node = by_branch.get(node["parent"]) if node.get("parent") else None
            entry["merge_first"] = first
        for entry in stack:
            entry.setdefault("merge_first", [])
            if entry["state"] == "open":
                entry["next"] = (
                    f"merge {entry['merge_first'][-1]} first"
                    if entry["merge_first"]
                    else (entry["pr"] or f"git merge {entry['branch']}")
                )
            else:
                entry["next"] = ""
        parallel: dict[str, list[str]] = {}
        for entry in stack:
            if entry["ref"]:
                parallel.setdefault(entry["parent"], []).append(entry["branch"])
        for entry in stack:
            siblings = parallel.get(entry.get("parent", ""), [])
            entry["parallel_with"] = (
                [b for b in siblings if b != entry["branch"]] if entry["ref"] else []
            )
        uncommitted = [
            p
            for p in (rel, str(path.relative_to(project)))
            if not gitflow.is_tracked(project, "HEAD", p)
        ]
        specs.append(
            {
                "spec": rel,
                "id": spec_id,
                "uncommitted": uncommitted,
                "stack": stack,
                "merge_next": next((e["branch"] for e in stack if e["state"] == "open"), None),
            }
        )
    dirty = len(gitutil.out(project, "status", "--porcelain").splitlines())
    return {
        "base": base,
        "current": gitflow.current_branch(project),
        "dirty": dirty,
        "specs": specs,
    }
