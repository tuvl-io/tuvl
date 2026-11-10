"""The audit ledger: an append-only, hash-chained record of what was done to a project.

One JSON object per line in ``.tuvl/audit/<YYYY-MM>.jsonl``. Every event carries ``prev``, the
sha256 of the previous line (across month files), so an edited, removed or reordered line is found
by :func:`verify`. The ledger is local to a machine and never committed; it holds ids, hashes and
short details — never file contents, prompts or secrets (those live in the trace).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AUDIT_DIR = ".tuvl/audit"

#: Every kind of event. Later phases add their emitters, not new kinds.
KINDS: frozenset[str] = frozenset(
    {
        "init.completed",
        "agent.selected",
        "consent.granted",
        "idea.imported",
        "id.renumbered",
        "spec.created",
        "spec.generated",
        "spec.approved",
        "spec.rejected",
        "spec.reopened",
        "tasks.derived",
        "codegen.started",
        "codegen.deterministic",
        "task.started",
        "task.attempt",
        "task.implemented",
        "task.failed",
        "scope.violation",
        "gate.result",
        "branch.created",
        "commit.created",
        "pr.opened",
        "pr.merged",
        "review.requested",
        "review.finding",
        "review.completed",
        "ship.packaged",
        "ui.spec.generated",
    }
)
RESULTS = ("ok", "refused", "failed")
_SECRET_KEY = re.compile(r"secret|token|password|passwd|api[_-]?key|authorization|credential", re.I)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LOCK_WAIT = 5.0

#: replaceable in tests
clock: Callable[[], float] = time.time


class AuditError(ValueError):
    pass


def _encode(n: int) -> str:
    out = ""
    for _ in range(26):
        out = _CROCKFORD[n & 31] + out
        n >>= 5
    return out


def _decode(ulid: str) -> int:
    n = 0
    for ch in ulid:
        n = (n << 5) | _CROCKFORD.index(ch)
    return n


def _ulid(at: float, after: str = "") -> str:
    """A time-ordered id; never below *after*, so events in one millisecond (or after a clock step back) still sort."""
    n = (int(at * 1000) << 80) | secrets.randbits(80)
    if after:
        with contextlib.suppress(ValueError):
            n = max(n, _decode(after) + 1)
    return _encode(n)


def _line(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def audit_dir(project: Path) -> Path:
    return project / AUDIT_DIR


def files(project: Path) -> list[Path]:
    root = audit_dir(project)
    return sorted(root.glob("*.jsonl")) if root.is_dir() else []


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if _SECRET_KEY.search(str(k)) else _scrub(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value][:50]
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:297] + "..."
    return value


@contextlib.contextmanager
def _locked(project: Path) -> Iterator[None]:
    root = audit_dir(project)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".lock"
    deadline = time.monotonic() + _LOCK_WAIT
    fd = -1
    while fd < 0:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:  # a lock left by a crashed process
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink()
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise AuditError("the audit ledger is locked") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            lock.unlink()


def _last_line(project: Path) -> str:
    for path in reversed(files(project)):
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        if lines:
            return lines[-1]
    return ""


def actor(project: Path) -> dict[str, str]:
    from tuvl_insight import gitutil
    from tuvl_insight.coding_agents import read_settings

    settings = read_settings(project)
    name, email = (
        gitutil.config_get(project, "user.name"),
        gitutil.config_get(project, "user.email"),
    )
    return {
        "initials": settings.initials if settings else "",
        "git": f"{name} <{email}>" if name and email else name,
    }


def append(
    project: Path,
    kind: str,
    *,
    command: str,
    entities: list[str] | None = None,
    result: str = "ok",
    detail: str = "",
    refs: dict[str, Any] | None = None,
    agent: dict[str, str] | None = None,
    who: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Add one event. Raises :class:`AuditError`; use :func:`emit` where failing must not matter."""
    if kind not in KINDS:
        raise AuditError(f"unknown audit event kind '{kind}'")
    if result not in RESULTS:
        raise AuditError(f"unknown result '{result}'")
    body: dict[str, Any] = {
        "actor": who if who is not None else actor(project),
        "command": command,
        "kind": kind,
        "entities": list(entities or []),
        "result": result,
        "detail": str(_scrub(detail)),
        "refs": _scrub(refs or {}),
    }
    if agent:
        body["agent"] = agent
    with _locked(project):  # the id and time are taken inside the lock so file order is time order
        at = clock()
        stamp = _dt.datetime.fromtimestamp(at, _dt.UTC)
        prev = _last_line(project)
        try:
            last_id = str(json.loads(prev).get("id", "")) if prev else ""
        except json.JSONDecodeError:
            last_id = ""
        event = {
            "id": _ulid(at, last_id),
            "ts": stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z",
            **body,
        }
        event["prev"] = _digest(prev) if prev else ""
        path = audit_dir(project) / f"{stamp:%Y-%m}.jsonl"
        with path.open("a") as fh:
            fh.write(_line(event) + "\n")
    from tuvl_insight import devtelemetry

    devtelemetry.enqueue(project, event)
    return event


def emit(project: Path | str, kind: str, **kwargs: Any) -> None:
    """:func:`append` that never raises: an audit failure must not stop the command it describes."""
    with contextlib.suppress(Exception):
        append(Path(project), kind, **kwargs)


# ── reading ──────────────────────────────────────────────────────────────────


def read_events(project: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in files(project):
        for text in path.read_text().splitlines():
            if not text.strip():
                continue
            try:
                out.append(json.loads(text))
            except json.JSONDecodeError:
                out.append({"id": "", "kind": "corrupt", "detail": text[:80], "entities": []})
    return out


def related(candidate: str, entity: str) -> bool:
    """Whether an event about *candidate* belongs to the history of *entity* (an idea covers its specs and tasks)."""
    from tuvl_insight.spec import ids

    if candidate == entity:
        return True
    try:
        if ids.IDEA_ID.match(entity):
            return ids.idea_of(candidate) == entity
        if ids.SPEC_ID.match(entity):
            return ids.TASK_ID.match(candidate) is not None and ids.spec_of(candidate) == entity
    except ids.IdError:
        return False
    return False


def select(
    events: list[dict[str, Any]],
    *,
    entity: str | None = None,
    actor_match: str | None = None,
    kind: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    out = events
    if entity:
        out = [e for e in out if any(related(x, entity) for x in e.get("entities", []))]
    if actor_match:
        needle = actor_match.lower()
        out = [e for e in out if needle in json.dumps(e.get("actor", {})).lower()]
    if kind:
        out = [e for e in out if str(e.get("kind", "")).startswith(kind)]
    if since:
        out = [e for e in out if str(e.get("ts", "")) >= since]
    return out


@dataclass
class Verification:
    ok: bool
    events: int
    problem: str = ""
    where: str = ""


_REQUIRED = ("id", "ts", "actor", "command", "kind", "entities", "result", "prev")


def verify(project: Path) -> Verification:
    """Check the chain, the schema and the order of the whole ledger."""
    prev_digest = ""
    count = 0
    last_id = ""
    for path in files(project):
        for number, text in enumerate(path.read_text().splitlines(), 1):
            if not text.strip():
                continue
            where = f"{path.name}:{number}"
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                return Verification(False, count, "not valid JSON", where)
            missing = [k for k in _REQUIRED if k not in event]
            if missing or event.get("kind") not in KINDS:
                return Verification(
                    False,
                    count,
                    f"malformed event ({', '.join(missing) or event.get('kind')})",
                    where,
                )
            if event["prev"] != prev_digest:
                return Verification(
                    False, count, "the chain is broken: a line was edited, removed or moved", where
                )
            if last_id and event["id"] < last_id:
                return Verification(False, count, "events are out of order", where)
            prev_digest, last_id = _digest(text), event["id"]
            count += 1
    return Verification(True, count)


def follow(
    project: Path, *, poll: float = 0.5, stop: Callable[[], bool] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield events as they are appended (new events only). Ends when *stop* returns true."""
    seen = sum(1 for _ in read_events(project))
    while not (stop and stop()):
        events = read_events(project)
        yield from events[seen:]
        seen = len(events)
        time.sleep(poll)
