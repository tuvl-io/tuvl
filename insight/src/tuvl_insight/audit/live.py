"""Live task state while ``codegen`` works: ``.tuvl/live/tasks.json``.

The committed task list is never touched during a run. This file overlays the derived status with
what is happening right now (queued, running attempt n of N, failed, in review). It is rewritten
atomically so a reader never sees half of it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

LIVE_FILE = ".tuvl/live/tasks.json"
FAILURES_FILE = ".tuvl/live/failures.json"
STATES = ("queued", "running", "implemented", "failed", "blocked", "in_review")


def _path(project: Path) -> Path:
    return project / LIVE_FILE


def read(project: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """``{spec path: {task id: state}}``; empty when nothing is running."""
    try:
        data = json.loads(_path(project).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return tasks if isinstance(tasks, dict) else {}


def _write(project: Path, tasks: dict[str, dict[str, dict[str, Any]]]) -> None:
    path = _path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"updated": time.time(), "tasks": tasks}, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_task(project: Path, spec: str, task: str, state: str, **fields: Any) -> None:
    """Record *task* of *spec* (a spec path) as being in *state* with extra fields (attempt, gate, pr…)."""
    if state not in STATES:
        raise ValueError(f"unknown live state '{state}'")
    tasks = read(project)
    tasks.setdefault(spec, {})[task] = {"state": state, "updated": time.time(), **fields}
    _write(project, tasks)


def clear(project: Path, spec: str | None = None) -> None:
    tasks = read(project)
    if spec is None:
        tasks = {}
    else:
        tasks.pop(spec, None)
    if tasks:
        _write(project, tasks)
    else:
        with contextlib.suppress(OSError):
            _path(project).unlink()


def describe(entry: dict[str, Any]) -> str:
    """A short human line for a live entry."""
    state = entry.get("state", "")
    if state == "running":
        attempt = f" {entry['attempt']}/{entry['max_attempts']}" if "attempt" in entry else ""
        gate = f" · {entry['gate']}" if entry.get("gate") else ""
        return f"running{attempt}{gate}"
    if state == "failed":
        return f"failed: {entry.get('finding', '')}".rstrip(": ")
    if state == "in_review":
        pr = entry.get("pr") or {}
        return f"in review #{pr['number']}" if "number" in pr else "in review"
    return str(state)


def read_failures(project: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """``{spec path: {task id: report}}`` of the last failed run of each task; kept until the task succeeds."""
    try:
        data = json.loads((project / FAILURES_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_failures(project: Path, data: dict[str, dict[str, dict[str, Any]]]) -> None:
    path = project / FAILURES_FILE
    if not data:
        with contextlib.suppress(OSError):
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_failure(project: Path, spec: str, task: str, report: dict[str, Any]) -> None:
    data = read_failures(project)
    data.setdefault(spec, {})[task] = {"when": time.time(), **report}
    _write_failures(project, data)


def clear_failure(project: Path, spec: str, task: str) -> None:
    data = read_failures(project)
    if data.get(spec, {}).pop(task, None) is not None:
        if not data[spec]:
            del data[spec]
        _write_failures(project, data)
