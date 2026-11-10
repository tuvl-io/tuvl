"""Commands started from Insight, run as detached CLI processes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from tuvl.core.dev.project import project_dir

router = APIRouter()


def _cli_argv(args: list[str]) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from tuvl.cli import main; main()",
        *args,
        "-d",
        str(project_dir()),
    ]


# Runs the command with its output in a file and its exit code beside it. Python, not `sh`, so it works on Windows.
_RUNNER = (
    "import subprocess, sys; out = sys.argv[1]; "
    "code = subprocess.call(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=open(out, 'wb'), "
    "stderr=subprocess.STDOUT); open(out + '.exit', 'w').write(str(code))"
)


def _detach() -> dict[str, Any]:
    """Start the command in a session of its own, so it outlives a reload of the dev server."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _cli_env() -> dict[str, str]:
    return {**os.environ, "NO_COLOR": "1", "COLUMNS": "100"}


class _Job:
    """A `tuvl` command started from Insight: its lines so far and how it ended.

    The command writes its output to ``.tuvl/live/jobs/<id>.out`` (and its exit code to ``<id>.out.exit``) in a
    session of its own, so a hot reload of the dev server neither kills it nor loses its output: the job is
    read back from those files.
    """

    def __init__(self, label: str, job_id: str | None = None) -> None:
        self.id = job_id or uuid.uuid4().hex[:12]
        self.label = label
        self.lines: list[str] = []
        self.logs: list[str] = []  # structured log lines: kept apart, shown when the command fails
        self.exit_code: int | None = None
        self.stopped = False
        self.pid: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None

    @property
    def out(self) -> Path:
        return _jobs_dir() / f"{self.id}.out"

    @property
    def status(self) -> str:
        if self.exit_code is None:
            return "running"
        return "stopped" if self.stopped else ("ok" if self.exit_code == 0 else "failed")


_JOBS: dict[str, _Job] = {}


def _jobs_dir() -> Path:
    path = project_dir() / ".tuvl" / "live" / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _alive_windows(pid: int) -> bool:
    """``os.kill(pid, 0)`` would end the process on Windows, so ask the process table."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def _terminate(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)


def _log_text(raw: str) -> str:
    """One structured log line as readable text: ``error · logger · event (key=value …)``."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return raw[:240]
    if not isinstance(doc, dict):
        return raw[:240]
    skip = {"event", "level", "logger", "timestamp"}
    extra = " ".join(f"{k}={v}" for k, v in doc.items() if k not in skip)
    head = f"{doc.get('level', 'log')} · {doc.get('logger', '')} · {doc.get('event', '')}"
    return f"{head} ({extra})"[:300] if extra else head


_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s")


def _is_problem(line: str) -> bool:
    if line.startswith("{"):
        return '"level": "error"' in line or '"level": "warning"' in line
    found = _LOG_LINE.match(line)
    return bool(found and found.group(1) in ("WARNING", "ERROR", "CRITICAL"))


def _ingest(job: _Job, line: str) -> None:
    """What the command prints is shown; its own log lines (JSON or timestamped) are kept for a failure report."""
    if line.startswith("{") or _LOG_LINE.match(line):
        job.logs.append(line)
    else:
        job.lines.append(line)


def _summarise(job: _Job) -> None:
    """What to say when the command ended: its exit code and the errors it logged."""
    if job.stopped:
        return
    if job.exit_code != 0:
        job.lines.append(f"✗ exit code {job.exit_code}" if job.exit_code is not None else "✗ lost")
        problems = [ln for ln in job.logs if _is_problem(ln)]
        shown = problems[-8:] or job.logs[-6:]
        if shown:
            job.lines.append("The command's own log:" if not problems else "Problems it logged:")
            job.lines.extend(f"  {_log_text(ln)}" for ln in shown)
        elif len(job.lines) <= 2:
            job.lines.append(
                "It printed nothing else; run the same command in a terminal for more."
            )
    elif len(job.lines) <= 1:
        job.lines.append("Finished; the command printed nothing.")


def _exit_code(job: _Job) -> int | None:
    try:
        return int(Path(f"{job.out}.exit").read_text().strip())
    except (OSError, ValueError):
        return None


async def _pump(job: _Job) -> None:
    """Follow the command's output file until it has ended, then record how it ended."""
    pos, rest = 0, ""
    while True:
        ended = job.proc.returncode is not None if job.proc else not _alive(job.pid)
        ended = ended or _exit_code(job) is not None
        try:
            with job.out.open("rb") as fh:
                fh.seek(pos)
                data = fh.read()
        except OSError:
            data = b""
        pos += len(data)
        chunk = rest + data.decode(errors="replace")
        *whole, rest = chunk.split("\n")
        for line in whole:
            _ingest(job, line)
        if ended:
            if rest:
                _ingest(job, rest)
            break
        await asyncio.sleep(0.15)
    code = _exit_code(job)
    job.exit_code = code if code is not None else (job.proc.returncode if job.proc else -1)
    if job.proc is not None:
        await job.proc.wait()
    _summarise(job)


def _meta(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.json"


def _recover(job_id: str) -> _Job | None:
    """A job the dev server no longer remembers (it restarted), read back from its files."""
    try:
        meta = json.loads(_meta(job_id).read_text())
    except (OSError, ValueError):
        return None
    job = _Job(str(meta.get("label", "command")), job_id)
    job.pid = meta.get("pid")
    job.lines.append(str(meta.get("command", "")))
    job.reader = asyncio.create_task(_pump(job))
    _JOBS[job.id] = job
    return job


def _running_elsewhere() -> bool:
    """A command started before the dev server restarted and still running."""
    for meta_file in _jobs_dir().glob("*.json"):
        job_id = meta_file.stem
        if job_id in _JOBS:
            continue
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        if _alive(meta.get("pid")) and not Path(f"{meta_file.with_suffix('.out')}.exit").exists():
            return True
    return False


async def _start_job(label: str, args: list[str]) -> _Job:
    if any(j.status == "running" for j in _JOBS.values()) or _running_elsewhere():
        raise HTTPException(status_code=409, detail="Another command is still running.")
    job = _Job(label)
    command = f"$ tuvl {' '.join(args)}"
    job.lines.append(command)
    job.proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _RUNNER,
        str(job.out),
        *_cli_argv(args),
        stdin=asyncio.subprocess.DEVNULL,
        env=_cli_env(),
        **_detach(),
    )
    job.pid = job.proc.pid
    _meta(job.id).write_text(
        json.dumps({"label": label, "command": command, "pid": job.pid, "started": time.time()})
    )
    _JOBS[job.id] = job
    job.reader = asyncio.create_task(_pump(job))
    return job


@router.get("/jobs/{job_id}", summary="A command started from Insight: its output so far")
async def job_state(job_id: str, after: int = Query(0, ge=0)) -> dict[str, Any]:
    job = _JOBS.get(job_id) or _recover(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such command")
    return {
        "label": job.label,
        "status": job.status,
        "exit_code": job.exit_code,
        "lines": job.lines[after:],
        "next": len(job.lines),
    }


@router.post("/jobs/{job_id}/stop", summary="Stop a command started from Insight")
async def stop_job(job_id: str) -> dict[str, str]:
    job = _JOBS.get(job_id) or _recover(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such command")
    if job.status == "running" and job.pid:
        job.stopped = True
        _terminate(job.pid)
        job.lines.append("stopped")
    return {"status": job.status}
