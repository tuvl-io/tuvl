"""Housekeeping for agent memory: erase one owner's rows, or let rows expire.

A memory is a row of a collection whose metadata names its owner (``memory_key``). Both functions
touch exactly the rows they are asked about and report how many they removed.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


async def forget(session: AsyncSession, collection: str, key: str) -> int:
    """Delete every memory of *key* in *collection*; nothing else."""
    if not collection or not key:
        raise ValueError("forget needs a collection and a key")
    result: Any = await session.execute(
        sa.text(
            "DELETE FROM tuvl_system_vector_store "
            "WHERE collection = :collection AND metadata ? 'memory_key' "
            "AND metadata->>'memory_key' = :key"
        ),
        {"collection": collection, "key": key},
    )
    return int(result.rowcount or 0)


async def expire(
    session: AsyncSession, collection: str, older_than: timedelta, *, now: datetime | None = None
) -> int:
    """Delete the memories of *collection* written more than *older_than* ago."""
    if older_than <= timedelta(0):
        raise ValueError("older_than must be positive")
    cutoff = (now or datetime.now().astimezone()) - older_than
    result: Any = await session.execute(
        sa.text(
            "DELETE FROM tuvl_system_vector_store "
            "WHERE collection = :collection AND metadata ? 'memory_key' AND created_at < :cutoff"
        ),
        {"collection": collection, "cutoff": cutoff},
    )
    return int(result.rowcount or 0)


RECENT_SQL = (
    "SELECT content, created_at FROM tuvl_system_vector_store "
    "WHERE collection = :c AND metadata ? 'memory_key' AND metadata->>'memory_key' = :key "
    "ORDER BY created_at DESC LIMIT :n"
)


async def recall(
    session: AsyncSession,
    run_id: Any,
    *,
    collection: str,
    query: str,
    key: str,
    window: int,
    top_k: int,
) -> list[dict[str, Any]]:
    """The owner's last *window* memories and up to *top_k* older related ones, older first."""
    from tuvl.runtime.builtins import search_collection

    recent: list[dict[str, Any]] = []
    if window:
        rows = (
            await (await session.connection()).execute(
                sa.text(RECENT_SQL),
                {"c": collection, "key": key, "n": window},
            )
        ).fetchall()
        recent = [
            {"content": r.content, "at": r.created_at.isoformat(), "source": "recent"}
            for r in reversed(rows)
        ]
    seen = {r["content"] for r in recent}
    related: list[dict[str, Any]] = []
    if top_k:
        found = await search_collection(
            session,
            run_id,
            collection=collection,
            query=query,
            top_k=top_k + len(recent),
            metadata_filter={"memory_key": key},
            scope="global",
        )
        related = [
            {"content": f["content"], "score": f["score"], "source": "related"}
            for f in found
            if f["content"] not in seen
        ][:top_k]
    return related + recent


async def remember(
    session: AsyncSession,
    *,
    collection: str,
    text: str,
    key: str,
    run_id: Any,
    agent: str,
    step: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Store one memory; a step that runs again (same *step* key) stores nothing more. True if stored."""
    from tuvl.core.models.system import SystemVectorStore
    from tuvl.runtime.builtins import _embed

    seen = (
        await session.execute(
            sa.text(
                "SELECT id FROM tuvl_system_vector_store "
                "WHERE collection = :c AND metadata ? 'step' AND metadata->>'step' = :step LIMIT 1"
            ),
            {"c": collection, "step": step},
        )
    ).first()
    if seen is not None:
        return False
    session.add(
        SystemVectorStore(
            id=uuid.uuid4(),
            collection=collection,
            content=text,
            embedding=await _embed(collection, text),
            extra_metadata={
                **(extra or {}),
                "memory_key": key,
                "kind": "turn",
                "run_id": str(run_id),
                "agent": agent,
                "step": step,
            },
        )
    )
    await session.flush()
    return True


_RETENTION_EVERY = 300.0
_last_retention = float("-inf")


def retained_collections() -> dict[str, timedelta]:
    """Each memory collection a loaded workflow gives a ``retention``; the shortest one wins."""
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.contract.schema import LlmAgent, LoopAgent, duration_seconds

    found: dict[str, timedelta] = {}
    for doc in list(WORKFLOWS.values()):
        for agent in doc.spec.agents:
            m = None
            if isinstance(agent, LlmAgent):
                m = agent.llm.memory
            elif isinstance(agent, LoopAgent):
                m = agent.loop.memory
            if m is not None and m.retention:
                age = timedelta(seconds=duration_seconds(m.retention))
                found[m.collection] = min(age, found.get(m.collection, age))
    return found


async def retention_sweep(session: AsyncSession) -> int:
    """Delete memories past their collection's ``retention``. Runs at most every five minutes."""
    global _last_retention
    if session.bind.dialect.name != "postgresql":
        return 0  # the memory store is a pgvector table; the offline test database has none
    now = time.monotonic()
    if now - _last_retention < _RETENTION_EVERY:
        return 0
    _last_retention = now
    removed = 0
    for collection, age in retained_collections().items():
        removed += await expire(session, collection, age)
    return removed


def _register() -> None:
    from tuvl.runtime import queue

    queue.SWEEPS.append(retention_sweep)


_register()


async def compact(
    session: AsyncSession,
    *,
    collection: str,
    keep: int,
    min_rows: int,
    summarise: Callable[[list[str]], Awaitable[str]],
    run_id: Any = None,
    step: str = "compact",
) -> int:
    """Fold each owner's older memories into one summary row; returns how many owners were compacted.

    An owner with more than *min_rows* memories keeps their latest *keep* as they are. The rest are
    summarised by *summarise* into one row that takes the time of the newest row it replaces, and are
    deleted in the same transaction. Running it again over the same rows does nothing more.
    """
    from tuvl.core.models.system import SystemVectorStore
    from tuvl.runtime.builtins import _embed

    owners = (
        await session.execute(
            sa.text(
                "SELECT metadata->>'memory_key' AS k FROM tuvl_system_vector_store "
                "WHERE collection = :c AND metadata ? 'memory_key' "
                "GROUP BY 1 HAVING count(*) > :m ORDER BY 1"
            ),
            {"c": collection, "m": min_rows},
        )
    ).all()
    done = 0
    for (key,) in owners:
        old = (
            await session.execute(
                sa.text(
                    "SELECT id, content, created_at FROM tuvl_system_vector_store "
                    "WHERE collection = :c AND metadata ? 'memory_key' "
                    "AND metadata->>'memory_key' = :k "
                    "ORDER BY created_at DESC OFFSET :keep"
                ),
                {"c": collection, "k": key, "keep": keep},
            )
        ).all()
        if len(old) < 2:
            continue
        ordered = list(reversed(old))
        text = (await summarise([r.content for r in ordered])).strip()
        if not text:
            continue
        session.add(
            SystemVectorStore(
                id=uuid.uuid4(),
                collection=collection,
                content=text,
                embedding=await _embed(collection, text),
                created_at=ordered[-1].created_at,
                extra_metadata={
                    "memory_key": key,
                    "kind": "summary",
                    "run_id": str(run_id) if run_id else None,
                    "agent": "compaction",
                    "step": f"{step}:{key}:{ordered[-1].id}",
                    "replaces": len(ordered),
                },
            )
        )
        await session.execute(
            sa.text("DELETE FROM tuvl_system_vector_store WHERE id = ANY(:ids)"),
            {"ids": [r.id for r in ordered]},
        )
        done += 1
    await session.flush()
    return done
