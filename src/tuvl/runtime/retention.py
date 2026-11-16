"""Removing finished runs, and everything that belongs to them.

A run leaves rows in six tables (the run, its journal, its checkpoints, its approvals, its webhook
deliveries and its drafts) and, when its trigger took uploads, objects in a store. Nothing removed them, so
they grew without limit. :func:`prune` deletes the runs that ended before a cutoff, in batches that each
commit; ``tuvl prune runs`` runs it on demand and :func:`retention_sweep` runs a batch of it from the
worker when ``TUVL_RUN_RETENTION_DAYS`` is set.

Only a run that has ended is ever removed (``completed``, ``failed``, ``aborted``, ``compensated``): one
that is queued, running, waiting or paused can still change. Uploaded objects are deleted only when asked
(they may be the only copy of a customer's document); a run whose object cannot be deleted right now is
kept for the next time rather than orphaning it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from tuvl.core.config import settings
from tuvl.runtime import queue
from tuvl.runtime.models import TERMINAL_STATUSES, SystemRun

logger = structlog.get_logger(__name__)

#: Tables whose rows belong to a run (``run_id``), removed with it.
RUN_TABLES = (
    "tuvl_system_run_events",
    "tuvl_system_run_checkpoints",
    "tuvl_system_approvals",
    "tuvl_system_webhook_deliveries",
    "tuvl_system_run_deltas",
)
_FILE_KEYS = frozenset({"store", "key", "name", "size", "content_type", "sha256"})
#: Runs the worker's sweep removes each time it comes round.
SWEEP_BATCH = 500
_RUNS = SystemRun.__table__  # type: ignore[attr-defined]

DeleteObject = Callable[[str, str], Awaitable[None]]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Pruned:
    runs: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    uploads: int = 0
    #: runs kept because an object they hold could not be deleted
    skipped: int = 0
    batches: int = 0
    dry_run: bool = False


def file_refs(value: Any) -> list[tuple[str, str]]:
    """The ``(store, key)`` of every ``file`` reference inside *value* (a run's input)."""
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        if (
            value.keys() >= _FILE_KEYS
            and isinstance(value["store"], str)
            and isinstance(value["key"], str)
        ):
            found.append((value["store"], value["key"]))
        else:
            for item in value.values():
                found.extend(file_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(file_refs(item))
    return found


def _ended_before(cutoff: dt.datetime, statuses: frozenset[str]) -> Any:
    ended = sa.func.coalesce(_RUNS.c.finished_at, _RUNS.c.updated_at)
    return sa.and_(_RUNS.c.status.in_(sorted(statuses)), ended < cutoff)


async def _delete_batch(session: AsyncSession, ids: list[Any], result: Pruned) -> None:
    for table in RUN_TABLES:
        gone = await queue._execute(
            session,
            sa.text(f"DELETE FROM {table} WHERE run_id IN :ids").bindparams(  # noqa: S608 - RUN_TABLES is a fixed tuple of our own table names
                sa.bindparam("ids", value=ids, expanding=True)
            ),
        )
        result.rows[table] = result.rows.get(table, 0) + (gone.rowcount or 0)
    await queue._execute(session, sa.delete(_RUNS).where(_RUNS.c.id.in_(ids)))
    result.runs += len(ids)


async def prune(
    session: AsyncSession,
    *,
    older_than: dt.timedelta,
    statuses: set[str] | frozenset[str] | None = None,
    batch: int = 500,
    dry_run: bool = False,
    delete_object: DeleteObject | None = None,
    now: dt.datetime | None = None,
) -> Pruned:
    """Remove the runs that ended more than *older_than* ago (of *statuses*, default every ended one).

    Each batch of *batch* runs is deleted and committed on its own, so a long prune never holds one
    huge transaction. With *delete_object* the objects named by ``file`` references in a run's input are
    deleted first; one that is already gone is fine, any other failure keeps that run. With *dry_run*
    nothing is changed and the result says what would be.
    """
    chosen = frozenset(statuses) if statuses else frozenset(TERMINAL_STATUSES)
    unfinished = sorted(chosen - TERMINAL_STATUSES)
    if unfinished:
        raise ValueError(
            f"a run that is {', '.join(unfinished)} can still change; only ended runs "
            f"({', '.join(sorted(TERMINAL_STATUSES))}) can be removed"
        )
    cutoff = (now or _now()) - older_than
    where = _ended_before(cutoff, chosen)
    result = Pruned(dry_run=dry_run)

    if dry_run:
        ids = (await queue._execute(session, sa.select(_RUNS.c.id).where(where))).scalars().all()
        result.runs = len(ids)
        if ids:
            for table in RUN_TABLES:
                counted = await queue._execute(
                    session,
                    sa.text(f"SELECT count(*) FROM {table} WHERE run_id IN :ids").bindparams(  # noqa: S608 - same fixed tuple
                        sa.bindparam("ids", value=list(ids), expanding=True)
                    ),
                )
                result.rows[table] = counted.scalar_one()
        return result

    skipped: set[Any] = set()
    while True:
        query = sa.select(_RUNS.c.id, _RUNS.c.input).where(where)
        if skipped:
            query = query.where(_RUNS.c.id.not_in(skipped))
        rows = (
            await queue._execute(
                session,
                query.order_by(
                    sa.func.coalesce(_RUNS.c.finished_at, _RUNS.c.updated_at), _RUNS.c.id
                ).limit(batch),
            )
        ).all()
        if not rows:
            break
        removable = []
        for run_id, payload in rows:
            if delete_object is None:
                removable.append(run_id)
                continue
            try:
                for store, key in file_refs(payload):
                    try:
                        await delete_object(store, key)
                    except Exception as exc:
                        if type(exc).__name__ != "NotFound":
                            raise
                    result.uploads += 1
            except Exception:
                logger.warning("retention.upload_not_deleted", run_id=str(run_id), exc_info=True)
                skipped.add(run_id)
                result.skipped += 1
            else:
                removable.append(run_id)
        if removable:
            await _delete_batch(session, removable, result)
        await session.commit()
        result.batches += 1
    return result


async def retention_sweep(session: AsyncSession) -> int:
    """One batch of the removal ``TUVL_RUN_RETENTION_DAYS`` asks for (the caller commits); 0 when it is off."""
    days = settings.tuvl_run_retention_days
    if days <= 0:
        return 0
    cutoff = _now() - dt.timedelta(days=days)
    rows = (
        (
            await queue._execute(
                session,
                sa.select(_RUNS.c.id)
                .where(_ended_before(cutoff, frozenset(TERMINAL_STATUSES)))
                .order_by(sa.func.coalesce(_RUNS.c.finished_at, _RUNS.c.updated_at), _RUNS.c.id)
                .limit(SWEEP_BATCH),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    result = Pruned()
    await _delete_batch(session, list(rows), result)
    logger.info("retention.swept", runs=result.runs, older_than_days=days)
    return result.runs


queue.SWEEPS.append(retention_sweep)
