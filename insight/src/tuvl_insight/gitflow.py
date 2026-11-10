"""Branches, worktrees, commits and pull requests for the work tuvl hands to a coding agent.

Every function is a thin, explicit git/forge call. tuvl opens pull requests; it never merges them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tuvl_insight import gitutil


class GitFlowError(RuntimeError):
    pass


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    done = gitutil.git(cwd, *args)
    if done is None:
        raise GitFlowError("git is not available")
    if check and done.returncode != 0:
        raise GitFlowError((done.stderr or done.stdout or f"git {args[0]} failed").strip())
    return done


def has_commits(project: Path) -> bool:
    return bool(gitutil.out(project, "rev-parse", "--verify", "-q", "HEAD"))


def current_branch(project: Path) -> str:
    return gitutil.out(project, "symbolic-ref", "--short", "-q", "HEAD")


def branch_exists(project: Path, branch: str) -> bool:
    return bool(gitutil.out(project, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"))


def base_branch(project: Path, requested: str | None = None) -> str:
    """The branch work starts from: the one asked for, else ``develop``, else the current branch."""
    if requested:
        if not branch_exists(project, requested):
            raise GitFlowError(f"branch '{requested}' does not exist")
        return requested
    if branch_exists(project, "develop"):
        return "develop"
    return current_branch(project)


def is_tracked(project: Path, ref: str, path: str) -> bool:
    return bool(gitutil.out(project, "ls-tree", "-r", "--name-only", ref, "--", path))


def spec_branch_ids(project: Path) -> set[str]:
    """Spec ids that already have a ``spec/<id>`` branch, local or remote: drafts not merged yet."""
    names = gitutil.out(
        project, "for-each-ref", "--format=%(refname:short)", "refs/heads/spec", "refs/remotes"
    ).split()
    found: set[str] = set()
    for name in names:
        tail = name.split("spec/", 1)[1] if "spec/" in name else ""
        if tail.startswith("SPEC-"):
            found.add(tail)
    return found


def add_worktree(project: Path, path: Path, branch: str, base: str) -> None:
    if branch_exists(project, branch):
        raise GitFlowError(f"branch '{branch}' already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(project, "worktree", "add", "-q", "-b", branch, str(path), base)


def add_worktree_on(project: Path, path: Path, branch: str) -> None:
    """A worktree on an existing *branch*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(project, "worktree", "add", "-q", str(path), branch)


def is_merged(project: Path, branch: str, into: str) -> bool:
    done = gitutil.git(project, "merge-base", "--is-ancestor", branch, into)
    return done is not None and done.returncode == 0


def remove_worktree(project: Path, path: Path) -> None:
    gitutil.git(project, "worktree", "remove", "--force", str(path))
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    gitutil.git(project, "worktree", "prune")


def delete_branch(project: Path, branch: str) -> None:
    gitutil.git(project, "branch", "-D", branch)


def changed_paths(workdir: Path) -> list[str]:
    """Every path that differs from HEAD in *workdir*: modified, added, deleted or untracked."""
    done = _run(workdir, "status", "--porcelain", "-z", "-uall")
    paths: list[str] = []
    entries = done.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":  # the next entry is the old name
            paths.append(entries[i])
            i += 1
        paths.append(path)
    return sorted(set(paths))


def revert_paths(workdir: Path, paths: list[str]) -> None:
    """Put *paths* back to HEAD: restore tracked files, delete untracked ones."""
    for path in paths:
        tracked = bool(gitutil.out(workdir, "ls-files", "--", path))
        if tracked:
            _run(workdir, "checkout", "HEAD", "--", path)
        else:
            target = workdir / path
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()


def commit(workdir: Path, message: str, paths: list[str]) -> str:
    """Commit exactly *paths*; returns the new commit's short hash."""
    _run(workdir, "add", "--", *paths)
    _run(workdir, "commit", "-q", "-m", message, "--", *paths)
    return gitutil.out(workdir, "rev-parse", "--short", "HEAD")


def has_remote(project: Path, name: str = "origin") -> bool:
    return name in gitutil.out(project, "remote").split()


def push(workdir: Path, branch: str, remote: str = "origin") -> None:
    _run(workdir, "push", "-q", "-u", remote, branch)


@dataclass(frozen=True)
class Forge:
    name: str  # "github" | "gitlab"
    cli: str  # "gh" | "glab"


def forge(project: Path, remote: str = "origin") -> Forge | None:
    """The forge behind *remote* when its command-line tool is installed."""
    url = gitutil.out(project, "remote", "get-url", remote).lower()
    if "github" in url and shutil.which("gh"):
        return Forge("github", "gh")
    if "gitlab" in url and shutil.which("glab"):
        return Forge("gitlab", "glab")
    return None


def open_pr(
    workdir: Path,
    forge_: Forge,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    draft: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    """Open a pull/merge request and return its URL. Never merges."""
    if forge_.cli == "gh":
        argv = [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            argv.append("--draft")
    else:
        argv = [
            "glab",
            "mr",
            "create",
            "--target-branch",
            base,
            "--source-branch",
            branch,
            "--title",
            title,
            "--description",
            body,
            "--yes",
        ]
        if draft:
            argv.append("--draft")
    try:
        done = subprocess.run(
            argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, **(env or {})},
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFlowError(f"{forge_.cli} failed: {exc}") from exc
    if done.returncode != 0:
        raise GitFlowError((done.stderr or done.stdout or f"{forge_.cli} failed").strip()[:400])
    urls = [ln for ln in done.stdout.split() if ln.startswith("http")]
    return urls[-1] if urls else done.stdout.strip()


def comment_pr(
    workdir: Path, forge_: Forge, body: str, *, env: dict[str, str] | None = None
) -> str:
    """Comment on the pull/merge request of the branch checked out in *workdir*."""
    argv = (
        ["gh", "pr", "comment", "--body", body]
        if forge_.cli == "gh"
        else ["glab", "mr", "note", "--message", body]
    )
    try:
        done = subprocess.run(
            argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, **(env or {})},
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFlowError(f"{forge_.cli} failed: {exc}") from exc
    if done.returncode != 0:
        raise GitFlowError((done.stderr or done.stdout or f"{forge_.cli} failed").strip()[:400])
    urls = [ln for ln in done.stdout.split() if ln.startswith("http")]
    return urls[-1] if urls else ""
