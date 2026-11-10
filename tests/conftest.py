"""Shared pytest fixtures and helpers for the tuvl test suite."""

from __future__ import annotations

# Set TUVL_DEV_MODE=true before any tuvl module is imported.
# In dev mode the key manager creates an ephemeral keypair instead of
# requiring TUVL_SIGNING_KEY, so the test suite runs without
# production credentials.
import importlib.util
import os

os.environ.setdefault("TUVL_DEV_MODE", "true")
# The tuvl.core.auth.access sentinel hard-fails if TUVL_DEV_MODE is set while
# TUVL_ENV=production — that combination is *never* legal in real deployments.
# Tests legitimately run with dev_mode=true and must therefore declare
# development env explicitly so the sentinel's layer-1 production refusal
# doesn't trip.
os.environ.setdefault("TUVL_ENV", "development")
# Layer-2 acknowledgement sentinel.
os.environ.setdefault("TUVL_ALLOW_DEV_AUTH", "true")

import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tuvl.core.auth.schemas import TokenUser

# ── In-memory session mock ────────────────────────────────────────────────────


def make_mock_session(
    initial_store: dict[uuid.UUID, Any] | None = None,
) -> tuple[AsyncMock, dict[uuid.UUID, Any]]:
    """Return *(mock_session, store)* where *store* is the in-memory row store.

    The mock faithfully models:
    - ``add()``    — appends to pending list
    - ``flush()``  — moves pending objects into *store* (keyed by ``.id``)
    - ``get()``    — looks up by primary key in *store*
    - ``delete()`` — removes by primary key from *store*
    - ``commit()`` / ``rollback()`` — no-ops (tracked by AsyncMock)

    The ``instance.id`` is assigned by ``default_factory=uuid.uuid4`` before
    ``flush()`` is called, so the real SQLAlchemy flush behaviour is faithfully
    reproduced.
    """
    store: dict[uuid.UUID, Any] = dict(initial_store or {})
    pending: list[Any] = []

    session = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: pending.append(obj))

    async def _flush() -> None:
        for obj in pending:
            store[obj.id] = obj
        pending.clear()

    session.flush = AsyncMock(side_effect=_flush)

    async def _get(_cls: type, pk: uuid.UUID, **_kwargs: Any) -> Any:
        # Accept (and ignore) kwargs like ``with_for_update`` so the mock
        # matches the real AsyncSession.get signature used by row-locking reads.
        return store.get(pk)

    session.get = AsyncMock(side_effect=_get)

    async def _delete(obj: Any) -> None:
        store.pop(obj.id, None)

    session.delete = AsyncMock(side_effect=_delete)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    return session, store


# ── Auth fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def admin_token_user() -> TokenUser:
    """A TokenUser with ``iam:admin`` scope — bypasses ownership checks."""
    return TokenUser(user_id="test-admin-001", scopes=["iam:admin"])


@pytest.fixture
def owner_token_user() -> TokenUser:
    """A TokenUser without elevated scopes — only owns their own instances."""
    return TokenUser(user_id="test-owner-001", scopes=[])


def make_binary(directory: Path, name: str, version: str = "1.2.3", help_text: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "{version}"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "{help_text}"; exit 0; fi\n'
        "exit 0\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def agent_bin(tmp_path: Path) -> Callable[..., Path]:
    """Put fake agent executables in a directory and return a function adding more."""
    directory = tmp_path / "bin"

    def add(name: str, **kw: str) -> Path:
        return make_binary(directory, name, **kw)

    add.dir = str(directory)  # type: ignore[attr-defined]
    add.path = str(directory) + os.pathsep + "/usr/bin" + os.pathsep + "/bin"  # type: ignore[attr-defined]
    return add


# ── tuvl-insight (the developer toolchain) is optional ──────────────────────────
# A bare install has no tuvl_insight: tests/insight/ is not collected and tests marked
# `requires_insight` are skipped, so the rest of the suite proves the engine runs without it.
INSIGHT_INSTALLED = importlib.util.find_spec("tuvl_insight") is not None
collect_ignore_glob = [] if INSIGHT_INSTALLED else ["insight/*"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if INSIGHT_INSTALLED:
        return
    skip = pytest.mark.skip(reason="needs tuvl-insight (the developer toolchain)")
    for item in items:
        if "requires_insight" in item.keywords:
            item.add_marker(skip)
