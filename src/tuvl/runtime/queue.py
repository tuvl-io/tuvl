"""Run queue and leases (spec §14.3–§14.4).

A run is a row; a worker *claims* it with ``FOR UPDATE SKIP LOCKED`` and holds
a time-limited lease that a heartbeat keeps renewing. The same claim query
also picks up ``running`` rows whose lease expired — that is crash recovery:
whichever worker claims next continues from the last checkpoint. Writes that
end or pause a run are conditional on still owning the lease, so a worker that
lost its lease can never clobber the run another worker now owns.

Lease times use the database clock on Postgres, so clock skew between workers
cannot make a live lease look expired.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tuvl.runtime import journal, wakeup
from tuvl.runtime.models import TERMINAL_STATUSES, SystemRun

logger = structlog.get_logger(__name__)

LEASE_SECONDS = 30.0
HEARTBEAT_SECONDS = 10.0
POLL_SECONDS = 1.0
SWEEP_SECONDS = 15.0

_RUNS = SystemRun.__table__  # type: ignore[attr-defined]
#: Statuses a worker holds a lease in; ``compensating`` is a run undoing its completed agents.
_LEASED = ("running", "compensating")
#: Statuses of a run that has not settled: it is waiting for a worker or being executed.
_LIVE = ("queued", "running", "compensating")
#: Statuses a resume or retry may move back to ``queued``.
_RESUMABLE = frozenset({"waiting_human", "waiting_approval", "paused"})


# ── Multi-tenant mode ────────────────────────────────────────────────────────
# A run belongs to the tenant bound when it was enqueued (the ``tenant_id`` column's default
# reads ``current_tenant_id``). The worker rebinds that tenant while it executes the run, so the
# run's database work, logs and child runs all carry it.


def _multi_tenant() -> bool:
    import tuvl.core.bootstrap as bootstrap

    return bootstrap.LOCKED_DEPLOYMENT_MODE == "multi_tenant"


def require_tenant() -> None:
    """In multi-tenant mode a run needs a tenant: refuse to create an unowned one."""
    if not _multi_tenant():
        return
    from tuvl.core.config import settings
    from tuvl.core.context import current_tenant_id
    from tuvl.core.datasources.postgres import MissingTenantContextError

    if not current_tenant_id.get("") and getattr(settings, "tuvl_require_tenant_context", True):
        raise MissingTenantContextError(
            "Multi-tenant mode: a run needs a tenant. Start it from a request that carries a "
            "tenant claim, or from code inside tuvl.tenancy.tenant_scope(); a scheduled workflow "
            "lists its tenants in trigger.tenants."
        )


async def run_tenant(session: AsyncSession, run_id: uuid.UUID) -> str | None:
    """The tenant that owns *run_id*; ``None`` outside multi-tenant mode."""
    if not _multi_tenant():
        return None
    row = await session.execute(
        sa.text("SELECT tenant_id FROM tuvl_system_runs WHERE id = :id"), {"id": run_id}
    )
    return row.scalar_one_or_none()


def bind_engine_tenant(engine: AsyncEngine) -> None:
    """Make every transaction on *engine* carry ``app.current_tenant_id`` for row-level security.

    The runtime opens plain sessions, which the HTTP-side ``after_begin`` hook never sees. A
    transaction with no tenant bound (claims, heartbeats, sweeps) sets nothing.
    """
    from sqlalchemy import event

    from tuvl.core.context import current_tenant_id
    from tuvl.core.datasources.postgres import _TENANT_ID_RE

    sync_engine = engine.sync_engine
    if not _multi_tenant() or sync_engine.dialect.name != "postgresql":
        return
    if getattr(sync_engine, "_tuvl_tenant_bound", False):
        return

    @event.listens_for(sync_engine, "begin")
    def _set_tenant(conn: Any) -> None:
        tid = current_tenant_id.get("")
        if tid and _TENANT_ID_RE.fullmatch(tid):
            conn.exec_driver_sql(f"SET LOCAL app.current_tenant_id = '{tid}'")

    sync_engine._tuvl_tenant_bound = True  # type: ignore[attr-defined]


def worker_id() -> str:
    """Unique per process: ``host:pid:random``."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _is_pg(session: AsyncSession) -> bool:
    return session.bind.dialect.name == "postgresql"


def _now(session: AsyncSession) -> Any:
    return sa.func.now() if _is_pg(session) else datetime.now(UTC)


def _later(session: AsyncSession, seconds: float) -> Any:
    if _is_pg(session):
        return sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, 0, seconds)
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def _execute(session: AsyncSession, statement: Any) -> Any:
    return await AsyncSession.execute(session, statement)


# ── Run lifecycle ────────────────────────────────────────────────────────────


async def enqueue(
    session: AsyncSession,
    *,
    workflow: str,
    workflow_hash: str,
    trigger: str,
    input: dict[str, Any],
    principal: str | None = None,
    parent_run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Create a ``queued`` run and journal ``run_queued``. The caller commits."""
    require_tenant()
    run = SystemRun(
        workflow=workflow,
        workflow_hash=workflow_hash,
        trigger=trigger,
        input=journal.to_json(input),
        principal=principal,
        parent_run_id=parent_run_id,
    )
    session.add(run)
    await session.flush()
    await journal.append_event(session, run.id, "run_queued", payload={"trigger": trigger})
    return run.id


async def claim(
    session: AsyncSession,
    owner: str,
    *,
    run_id: uuid.UUID | None = None,
    limit: int = 1,
    lease_seconds: float = LEASE_SECONDS,
) -> list[uuid.UUID]:
    """Lease up to *limit* claimable runs (queued, or running with an expired
    lease) to *owner*, oldest first; or exactly *run_id* if it is claimable.
    The caller commits.

    A crashed worker's runs (expired leases, few and found by ``(status, lease_expires_at)``) are taken
    first, then queued ones, oldest first off the partial index on queued runs: the two are asked
    separately because one query over both would have to sort the whole backlog.
    """
    expired = sa.and_(_RUNS.c.status.in_(_LEASED), _RUNS.c.lease_expires_at < _now(session))
    queued = _RUNS.c.status == "queued"
    if run_id is not None:
        return await _lease(
            session,
            owner,
            sa.or_(queued, expired),
            run_id=run_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )
    taken = await _lease(session, owner, expired, limit=limit, lease_seconds=lease_seconds)
    if len(taken) < limit:
        taken += await _lease(
            session, owner, queued, limit=limit - len(taken), lease_seconds=lease_seconds
        )
    return taken


async def _lease(
    session: AsyncSession,
    owner: str,
    where: Any,
    *,
    limit: int,
    lease_seconds: float,
    run_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    pick = sa.select(_RUNS.c.id).where(where)
    if run_id is not None:
        pick = pick.where(_RUNS.c.id == run_id)
    pick = pick.order_by(_RUNS.c.created_at).limit(limit).with_for_update(skip_locked=True)
    result = await _execute(
        session,
        sa.update(_RUNS)
        .where(_RUNS.c.id.in_(pick))
        .values(
            # a run found half way through compensation goes on compensating, it does not start over
            status=sa.case((_RUNS.c.status == "compensating", "compensating"), else_="running"),
            lease_owner=owner,
            lease_expires_at=_later(session, lease_seconds),
            claims=_RUNS.c.claims + 1,
            started_at=sa.func.coalesce(_RUNS.c.started_at, _now(session)),
            updated_at=_now(session),
        )
        .returning(_RUNS.c.id, _RUNS.c.created_at),
    )
    # RETURNING order is unspecified; keep the batch oldest-first.
    return [row[0] for row in sorted(result.all(), key=lambda r: r[1])]


async def heartbeat(
    session: AsyncSession, run_id: uuid.UUID, owner: str, *, lease_seconds: float = LEASE_SECONDS
) -> bool:
    """Extend the lease. False means it was lost (expired and reclaimed)."""
    result = await _execute(
        session,
        sa.update(_RUNS)
        .where(_RUNS.c.id == run_id, _RUNS.c.lease_owner == owner, _RUNS.c.status.in_(_LEASED))
        .values(lease_expires_at=_later(session, lease_seconds), updated_at=_now(session)),
    )
    return bool(result.rowcount)


async def release(
    session: AsyncSession, run_id: uuid.UUID, owner: str, status: str, **fields: Any
) -> bool:
    """Leave the ``running`` state (finished, failed, waiting, paused …) and
    drop the lease — only if *owner* still holds it. The caller commits."""
    values: dict[str, Any] = {
        "status": status,
        "lease_owner": None,
        "lease_expires_at": None,
        "updated_at": _now(session),
        **{k: journal.to_json(v) if isinstance(v, dict) else v for k, v in fields.items()},
    }
    if status in TERMINAL_STATUSES:
        values["finished_at"] = _now(session)
    result = await _execute(
        session,
        sa.update(_RUNS)
        .where(_RUNS.c.id == run_id, _RUNS.c.lease_owner == owner, _RUNS.c.status.in_(_LEASED))
        .values(**values),
    )
    wakeup.mark(session, run_id)
    return bool(result.rowcount)


async def requeue(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Move a waiting or paused run back to the queue (resume). The caller commits."""
    result = await _execute(
        session,
        sa.update(_RUNS)
        .where(_RUNS.c.id == run_id, _RUNS.c.status.in_(_RESUMABLE))
        .values(status="queued", updated_at=_now(session)),
    )
    return bool(result.rowcount)


# ── Worker ───────────────────────────────────────────────────────────────────

#: Runs one claimed run to its next stopping point (end, pause, wait). It must
#: release the run itself; the worker only fails runs whose executor raised.
Executor = Callable[[uuid.UUID, "Lease"], Awaitable[None]]

#: Housekeeping run before each claim (e.g. expiring approvals, which
#: re-queues their runs). Each commits nothing; the worker commits.
SWEEPS: list[Callable[[AsyncSession], Awaitable[int]]] = []


@dataclass
class Lease:
    """Handed to the executor: lets it check it still owns the run."""

    run_id: uuid.UUID
    owner: str
    #: set by whoever learns the lease is gone: the heartbeat, or the runner when a fenced write matches no row
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    #: set by the heartbeat alone; the executor is then cancelled, because the run is no longer this worker's
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Worker:
    engine: AsyncEngine
    executor: Executor
    owner: str = field(default_factory=worker_id)
    concurrency: int = 4
    lease_seconds: float = LEASE_SECONDS
    heartbeat_seconds: float = HEARTBEAT_SECONDS
    poll_seconds: float = POLL_SECONDS
    #: housekeeping sweeps (approval expiry, retention) run at most this often, not before every claim
    sweep_seconds: float = SWEEP_SECONDS
    #: ``time.monotonic()`` when the poll loop last finished an attempt, whether or not it worked
    last_poll: float | None = None
    _last_sweep: float | None = None
    _active: dict[uuid.UUID, asyncio.Task[None]] = field(default_factory=dict)
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)

    async def run_forever(self) -> None:
        logger.info("runtime.worker.started", worker=self.owner, concurrency=self.concurrency)
        failures = 0
        while not self._stopping.is_set():
            try:
                claimed = await self.claim_some()
                failures = 0
            except Exception:
                # a dropped connection or a failover must not end the poll loop for good
                failures += 1
                claimed = 0
                logger.exception("runtime.worker.poll_failed", worker=self.owner, failures=failures)
            self.last_poll = time.monotonic()
            if not claimed:
                delay = (
                    min(self.poll_seconds * 2**failures, 30.0) if failures else self.poll_seconds
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), delay)
        await self.drain()

    async def stop(self) -> None:
        self._stopping.set()

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for the running runs. After *timeout* seconds the ones still going are interrupted:
        their leases expire and another worker resumes them from the last checkpoint."""
        tasks = list(self._active.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("runtime.worker.drain_timeout", interrupted=len(pending))
            await asyncio.gather(*pending, return_exceptions=True)

    async def claim_some(self) -> int:
        free = self.concurrency - len(self._active)
        if free <= 0:
            return 0
        async with AsyncSession(self.engine, expire_on_commit=False) as s:
            now = time.monotonic()
            if self._last_sweep is None or now - self._last_sweep >= self.sweep_seconds:
                self._last_sweep = now
                for sweep in SWEEPS:
                    try:
                        await sweep(s)
                        await s.commit()
                    except Exception:  # one failing sweep must not stop the others, or the claim
                        logger.exception(
                            "runtime.sweep.failed", sweep=getattr(sweep, "__name__", sweep)
                        )
                        await s.rollback()
            ids = await claim(s, self.owner, limit=free, lease_seconds=self.lease_seconds)
            await s.commit()
        for run_id in ids:
            self.start(run_id)
        return len(ids)

    def start(self, run_id: uuid.UUID) -> asyncio.Task[None]:
        """Execute an already-claimed run under heartbeat."""
        task = asyncio.create_task(self.execute(run_id))
        self._active[run_id] = task

        def forget(_task: asyncio.Task[None]) -> None:
            self._active.pop(run_id, None)

        task.add_done_callback(forget)
        return task

    async def execute(self, run_id: uuid.UUID) -> None:
        lease = Lease(run_id, self.owner)
        beat = asyncio.create_task(self.keep_alive(lease))
        try:
            await self._until_revoked(run_id, lease)
        except Exception as exc:
            logger.exception("runtime.run.crashed", run_id=str(run_id))
            async with AsyncSession(self.engine) as s:
                if await release(
                    s,
                    run_id,
                    self.owner,
                    "failed",
                    error={"error_type": "executor_crashed", "message": str(exc)},
                ):
                    await journal.append_event(s, run_id, "run_failed", payload={"error": str(exc)})
                await s.commit()
        finally:
            beat.cancel()

    async def _until_revoked(self, run_id: uuid.UUID, lease: Lease) -> None:
        """Run the executor, and cancel it the moment the heartbeat finds the run is no longer ours:
        a step that carries on would repeat its outside effects next to the worker that owns the run now."""
        work = asyncio.create_task(self._run_as_tenant(run_id, lease))
        revoked = asyncio.create_task(lease.revoked.wait())
        try:
            await asyncio.wait({work, revoked}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                work.result()
                return
            logger.warning("runtime.lease.cancelled", run_id=str(run_id), worker=self.owner)
        finally:
            revoked.cancel()
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def _run_as_tenant(self, run_id: uuid.UUID, lease: Lease) -> None:
        """Execute under the tenant that owns the run (multi-tenant mode); a plain call otherwise."""
        async with AsyncSession(self.engine) as s:
            tenant = await run_tenant(s, run_id)
        if tenant is None:
            await self.executor(run_id, lease)
            return
        from tuvl.tenancy import tenant_scope

        with tenant_scope(tenant):
            await self.executor(run_id, lease)

    async def keep_alive(self, lease: Lease) -> None:
        interval = self.heartbeat_seconds
        last_good = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            attempt = time.monotonic()
            try:
                async with AsyncSession(self.engine) as s:
                    alive = await heartbeat(
                        s, lease.run_id, lease.owner, lease_seconds=self.lease_seconds
                    )
                    await s.commit()
            except Exception:
                logger.warning("runtime.heartbeat.failed", run_id=str(lease.run_id), exc_info=True)
                if time.monotonic() - last_good >= self.lease_seconds:
                    # a whole lease without a good beat: the database has already given the run away
                    logger.error(
                        "runtime.lease.expired", run_id=str(lease.run_id), worker=lease.owner
                    )
                    self._revoke(lease)
                    return
                interval = max(
                    self.heartbeat_seconds / 3, 0.05
                )  # try again sooner than the lease runs out
                continue
            if not alive:
                logger.warning("runtime.lease.lost", run_id=str(lease.run_id), worker=lease.owner)
                self._revoke(lease)
                return
            last_good, interval = attempt, self.heartbeat_seconds

    @staticmethod
    def _revoke(lease: Lease) -> None:
        lease.lost.set()
        lease.revoked.set()


async def run_inline(
    worker: Worker, run_id: uuid.UUID, *, timeout_seconds: float
) -> Literal["done", "promoted", "not_claimed"]:
    """The synchronous HTTP path (spec §14.3): claim *run_id* in this worker
    and run it now. If it outlives *timeout_seconds* the caller answers 202 —
    the run keeps going in the background under the same lease."""
    async with AsyncSession(worker.engine) as s:
        claimed = await claim(s, worker.owner, run_id=run_id, lease_seconds=worker.lease_seconds)
        await s.commit()
    if not claimed:
        return "not_claimed"
    task = worker.start(run_id)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
    except TimeoutError:
        return "promoted"
    return "done"


async def wait_settled(engine: AsyncEngine, run_id: uuid.UUID, timeout_seconds: float) -> None:
    """Wait until *run_id* is no longer queued or running, or the time is up.

    For a run the background worker claimed before the HTTP request could: the request still owes
    its caller the finished answer, not a 202 for a run that takes milliseconds."""

    async def settled() -> bool | None:
        async with AsyncSession(engine) as s:
            run = await s.get(SystemRun, run_id)
        return True if run is None or run.status not in _LIVE else None

    await wakeup.wait_until(run_id, settled, timeout=timeout_seconds)
