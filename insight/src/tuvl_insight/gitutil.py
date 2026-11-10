"""The few git operations tuvl performs on a project, all non-fatal."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


def available() -> bool:
    return shutil.which("git") is not None


def git(
    project: Path, *args: str, timeout: int = 10, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run ``git`` in *project*; ``None`` when git is missing or does not answer."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
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


# ── the challenge tag (registering with a Nest) ───────────────────────────────

REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
CHALLENGE_TAG_RE = re.compile(r"^tuvl-nest-verify-[0-9a-f]{20}$")
_PUSH_TIMEOUT = 45


def remote_url(project: Path, remote: str = "origin") -> str:
    """The URL of *remote*, or ``""`` when there is no such remote."""
    if not REMOTE_NAME_RE.match(remote):
        return ""
    return out(project, "remote", "get-url", remote)


def normalize_remote(url: str) -> str:
    """One comparable form of a git remote or repository URL: ``host/path`` in lowercase, no credentials,
    port, ``.git`` or trailing slash, so ``git@github.com:Acme/App.git`` equals ``https://github.com/acme/app``."""
    text = url.strip()
    scp = re.match(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)(.+)$", text)
    if scp and "://" not in text:
        host, path = scp.group(1), scp.group(2)
    else:
        match = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.*)$", text, re.I)
        if not match:
            return ""
        host, path = match.group(1), match.group(2)
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    path = path[:-4] if path.endswith(".git") else path
    return f"{host.lower()}/{path.lower()}".rstrip("/")


def same_repo(remote: str, repo: str) -> bool:
    """Whether a git remote URL and the project's ``repo`` field name the same repository."""
    left, right = normalize_remote(remote), normalize_remote(repo)
    return bool(left) and left == right


def push_tag(project: Path, remote: str, tag: str) -> tuple[bool, str]:
    """Create the challenge tag on HEAD and push it. ``(True, "")`` when the remote accepted it.

    git uses the developer's own credentials (ssh agent, credential helper); it never prompts here, so
    a missing credential is a clear failure, not a hang. Only a Nest challenge tag name is accepted.
    """
    if not CHALLENGE_TAG_RE.match(tag) or not REMOTE_NAME_RE.match(remote):
        return False, "refused: not a challenge tag or remote name"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    made = git(project, "tag", tag)
    if made is None:
        return False, "git did not answer"
    if made.returncode != 0:
        return False, (
            made.stderr or "could not create the tag (does the repository have a commit?)"
        ).strip()
    done = git(
        project,
        "push",
        "--porcelain",
        "--",
        remote,
        f"refs/tags/{tag}",
        timeout=_PUSH_TIMEOUT,
        env=env,
    )
    if done is None:
        git(project, "tag", "-d", tag)
        return False, "git did not answer in time"
    rejected = any(line.startswith("!") for line in (done.stdout or "").splitlines())
    if done.returncode != 0 or rejected:
        git(project, "tag", "-d", tag)
        reason = (done.stderr or done.stdout or "the remote refused the push").strip()
        return False, reason.splitlines()[-1] if reason else "the remote refused the push"
    return True, ""


def delete_tag(project: Path, remote: str, tag: str) -> None:
    """Remove the challenge tag locally and on the remote. Best effort."""
    if not CHALLENGE_TAG_RE.match(tag) or not REMOTE_NAME_RE.match(remote):
        return
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    git(
        project,
        "push",
        "--porcelain",
        "--delete",
        "--",
        remote,
        f"refs/tags/{tag}",
        timeout=_PUSH_TIMEOUT,
        env=env,
    )
    git(project, "tag", "-d", tag)
