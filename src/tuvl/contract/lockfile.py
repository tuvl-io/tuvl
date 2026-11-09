"""Reading `tuvl.lock` and the runtime's drift warning.

The engine reads the lock to warn when a live MCP tool's schema differs from what was pinned. Writing the lock
(`tuvl generate lock`, `tuvl ship`) is tooling and lives in `tuvl.lock`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)

LOCK_FILE = "tuvl.lock"


class LockError(ValueError):
    """A reference the lock cannot resolve."""


def schema_hash(schema: Any) -> str:
    canonical = json.dumps(schema or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def read(project: Path) -> dict[str, Any] | None:
    path = project / LOCK_FILE
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else None


# ── runtime drift check ──────────────────────────────────────────────────────

_WARNED: set[tuple[str, str]] = set()


def check_live_mcp(project: Path, server: str, tool: str, schema: Any) -> None:
    """Warn once per tool when a live MCP schema differs from the lock (V014)."""
    if (server, tool) in _WARNED:
        return
    _WARNED.add((server, tool))
    pinned = ((read(project) or {}).get("mcp") or {}).get(server, {}).get(tool)
    if pinned and pinned != "unlisted" and pinned != schema_hash(schema):
        logger.warning(
            "mcp.schema_drift",
            server=server,
            tool=tool,
            hint="the server's tool schema differs from tuvl.lock — re-run `tuvl generate lock` after review",
        )
