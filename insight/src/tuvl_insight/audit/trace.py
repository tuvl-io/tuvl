"""The trace: verbose, local detail behind the ledger — full prompts, agent output, diffs.

Content is stored once by its sha256 under ``.tuvl/trace/blobs/``; the ledger refers to it by hash,
so it proves what was sent without containing it. Never committed.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

TRACE_DIR = ".tuvl/trace"


def blob_dir(project: Path) -> Path:
    return project / TRACE_DIR / "blobs"


def store(project: Path, data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    digest = hashlib.sha256(raw).hexdigest()
    path = blob_dir(project) / digest
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)
    return digest


def load(project: Path, digest: str) -> bytes | None:
    path = blob_dir(project) / digest
    return path.read_bytes() if path.is_file() and len(digest) == 64 else None


def prune(project: Path, older_than_days: float, *, now: float | None = None) -> int:
    """Delete trace files not touched for *older_than_days*; returns how many were removed."""
    cutoff = (now if now is not None else time.time()) - older_than_days * 86400
    removed = 0
    root = project / TRACE_DIR
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed
