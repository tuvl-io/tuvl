"""The few git operations tuvl performs on a project, all non-fatal."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def available() -> bool:
    return shutil.which("git") is not None


def git(project: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run ``git`` in *project*; ``None`` when git is missing or does not answer."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def out(project: Path, *args: str) -> str:
    done = git(project, *args)
    if done is None or getattr(done, "returncode", 1) != 0:
        return ""
    return str(getattr(done, "stdout", "") or "").strip()


def inside_work_tree(project: Path) -> bool:
    return out(project, "rev-parse", "--is-inside-work-tree") == "true"


def config_get(project: Path, key: str) -> str:
    return out(project, "config", "--get", key)


def init_repo(project: Path, branch: str = "develop") -> bool:
    """``git init`` on *branch*; git older than 2.28 has no ``-b``, so it points HEAD instead."""
    done = git(project, "init", "-b", branch)
    if done is not None and done.returncode == 0:
        return True
    done = git(project, "init")
    if done is None or done.returncode != 0:
        return False
    done = git(project, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    return done is not None and done.returncode == 0


def commit_all(project: Path, message: str) -> bool:
    if git(project, "add", "-A") is None:
        return False
    done = git(project, "commit", "-q", "-m", message)
    return done is not None and done.returncode == 0
