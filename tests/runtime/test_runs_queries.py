"""The queue's and the run lists' queries have indexes to use, waiting for a run is driven by commits rather
than a tight poll, and housekeeping sweeps run on their own interval."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tuvl.core.datasources import postgres
from tuvl.runtime import queue, wakeup
from tuvl.runtime.queue import Worker
from tuvl.runtime.runner import Runner

NEW_INDEXES = {
    "ix_tuvl_runs_queued_created": "claim",
    "ix_tuvl_runs_principal_created": "list for a principal",
    "ix_tuvl_runs_created": "list for an administrator",
    "uq_tuvl_runs_schedule": "one run per scheduled fire",
}


async def _valid(engine: AsyncEngine, name: str) -> bool | None:
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)"),
            {"n": name},
        )
        return found.scalar_one_or_none()


async def _plan(engine: AsyncEngine, statement: str, **params: object) -> str:
    """The plan Postgres picks with sequential scans made as unattractive as it can: an index that fits the
    query is then used, one that does not leaves a sequential scan."""
    async with engine.connect() as conn:
        await conn.execute(sa.text("SET enable_seqscan = off"))
        rows = await conn.execute(sa.text("EXPLAIN " + statement), params)
        return "\n".join(r[0] for r in rows)


@pytest.fixture
async def seeded(pg_engine: AsyncEngine) -> AsyncEngine:
    async with pg_engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO tuvl_system_runs (id, workflow, workflow_hash, status, trigger, principal,"
                " input, next_seq, claims, tokens_used, created_at, updated_at) "
                "SELECT gen_random_uuid(), 'w' || (g % 5), 'h', "
                "CASE WHEN g % 400 = 0 THEN 'queued' ELSE 'completed' END, "
                "CASE WHEN g % 50 = 0 THEN 'schedule:2026-01-01T00:00:' || g ELSE 'http' END, "
                "'user' || (g % 100), '{}'::json, 1, 0, 0, "
                "now() - (g || ' seconds')::interval, now() "
                "FROM generate_series(1, 20000) g"
            )
        )
        await conn.execute(sa.text("ANALYZE tuvl_system_runs"))
    return pg_engine


@pytest.mark.parametrize("name", NEW_INDEXES)
async def test_a_fresh_database_gets_the_index(pg_engine: AsyncEngine, name: str) -> None:
    assert await _valid(pg_engine, name) is True


async def test_an_existing_table_gets_them_built_concurrently(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        for name in NEW_INDEXES:
            await conn.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    await postgres.ensure_indexes(pg_engine, postgres.RUNTIME_INDEXES)
    for name in NEW_INDEXES:
        assert await _valid(pg_engine, name) is True, name


async def test_the_claim_does_not_sort_the_backlog(seeded: AsyncEngine) -> None:
    """The statements ``queue.claim`` really sends, explained: the queued runs come off the partial index in
    order, so a long backlog costs the batch size, not the backlog."""
    sent: list[tuple[str, object]] = []

    def record(conn, cursor, statement, parameters, *_):
        if statement.lstrip().startswith("UPDATE tuvl_system_runs"):
            sent.append((statement, parameters))

    event.listen(seeded.sync_engine, "before_cursor_execute", record)
    async with AsyncSession(seeded) as s:
        await queue.claim(s, "w1", limit=8)
        await s.rollback()
    event.remove(seeded.sync_engine, "before_cursor_execute", record)
    assert sent
    plans: dict[bool, str] = {}
    async with seeded.connect() as conn:
        await conn.exec_driver_sql("SET enable_seqscan = off")
        for statement, parameters in sent:
            rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
            plans["queued" in str(parameters)] = "\n".join(r[0] for r in rows)
        await conn.rollback()
    queued = plans[True]  # the statement that takes queued runs
    assert "ix_tuvl_runs_queued_created" in queued
    assert "Sort" not in queued.replace("Sort Key", "").replace("Presorted", "")


async def test_a_principals_list_uses_its_index(seeded: AsyncEngine) -> None:
    plan = await _plan(
        seeded,
        "SELECT id FROM tuvl_system_runs WHERE principal = 'user7' ORDER BY created_at DESC LIMIT 50",
    )
    assert "ix_tuvl_runs_principal_created" in plan


async def test_an_administrators_list_uses_the_created_index(seeded: AsyncEngine) -> None:
    plan = await _plan(seeded, "SELECT id FROM tuvl_system_runs ORDER BY created_at DESC LIMIT 50")
    assert "ix_tuvl_runs_created" in plan


async def test_the_duplicate_check_of_a_scheduled_fire_uses_the_unique_index(
    seeded: AsyncEngine,
) -> None:
    plan = await _plan(
        seeded,
        "SELECT count(*) FROM tuvl_system_runs WHERE workflow = 'w0' "
        "AND trigger = 'schedule:2026-01-01T00:00:50' AND trigger LIKE 'schedule:%'",
    )
    assert "uq_tuvl_runs_schedule" in plan


async def test_a_scheduled_fire_cannot_be_enqueued_twice(pg_engine: AsyncEngine) -> None:
    def insert(trigger: str) -> sa.TextClause:
        return sa.text(
            "INSERT INTO tuvl_system_runs (id, workflow, workflow_hash, status, trigger, input,"
            " next_seq, claims, tokens_used, created_at, updated_at) VALUES "
            f"(gen_random_uuid(), 'w', 'h', 'queued', '{trigger}', '{{}}', 1, 0, 0, now(), now())"
        )

    async with pg_engine.begin() as conn:
        await conn.execute(insert("schedule:2026-01-01T00:00:00"))
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(insert("schedule:2026-01-01T00:00:00"))
    async with pg_engine.begin() as conn:  # other fires, and other kinds of run, are untouched
        await conn.execute(insert("schedule:2026-01-01T00:05:00"))
        await conn.execute(insert("http"))
        await conn.execute(insert("http"))


async def test_the_scheduler_treats_a_unique_violation_as_already_fired(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime

    from tuvl.contract.schema import WorkflowDocument
    from tuvl.core.engine import scheduler
    from tuvl.runtime.host import RuntimeHost, set_host

    doc = WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "nightly"},
            "spec": {
                "trigger": {"schedule": "0 2 * * *"},
                "agents": [
                    {
                        "id": "p",
                        "description": "d",
                        "engine": "pending",
                        "routes": {"default": "END"},
                    }
                ],
            },
        }
    )
    host = RuntimeHost(pg_engine, workflows={"nightly": doc}, role="api")

    async def collide(*_: object, **__: object) -> uuid.UUID:
        raise IntegrityError(
            "INSERT", {}, Exception("duplicate key value violates uq_tuvl_runs_schedule")
        )

    monkeypatch.setattr(host, "enqueue", collide)
    set_host(host)
    try:
        fire_at = datetime.datetime(2026, 10, 9, 2, 0, tzinfo=datetime.UTC)
        assert await scheduler.enqueue_scheduled("nightly", fire_at) is False
    finally:
        set_host(None)


# ── waiting for a run is driven by commits ────────────────────────────────────


@pytest.fixture
async def running(sqlite_engine: AsyncEngine) -> AsyncIterator[tuple[AsyncEngine, uuid.UUID]]:
    async with AsyncSession(sqlite_engine) as s:
        run_id = await queue.enqueue(
            s, workflow="w", workflow_hash="h", trigger="test", input={}, principal=None
        )
        await s.commit()
    yield sqlite_engine, run_id


async def _settle(engine: AsyncEngine, run_id: uuid.UUID, *, notify: bool) -> None:
    from tuvl.runtime.models import SystemRun

    async with AsyncSession(engine) as s:
        run = await s.get(SystemRun, run_id)
        assert run is not None
        run.status = "completed"
        if notify:
            wakeup.mark(s, run_id)
        await s.commit()


def _count_selects(engine: AsyncEngine) -> list[str]:
    seen: list[str] = []

    def record(conn, cursor, statement, *_):
        if "tuvl_system_runs" in statement and statement.lstrip().startswith("SELECT"):
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return seen


async def test_waiting_for_a_run_settled_here_ends_at_once_and_asks_little(running) -> None:
    engine, run_id = running
    seen = _count_selects(engine)

    async def settle_later() -> None:
        await asyncio.sleep(0.4)
        await _settle(engine, run_id, notify=True)

    task = asyncio.create_task(settle_later())
    started = time.monotonic()
    await queue.wait_settled(engine, run_id, timeout_seconds=5)
    took = time.monotonic() - started
    await task
    assert 0.35 <= took < 0.55  # woken by the commit, not by the next poll
    assert len(seen) <= 8  # the old 20 ms poll asked about 20 times


async def test_a_run_settled_in_another_process_is_found_by_the_fallback_poll(running) -> None:
    engine, run_id = running

    async def settle_later() -> None:
        await asyncio.sleep(0.2)
        await _settle(engine, run_id, notify=False)  # nothing in this process hears of it

    task = asyncio.create_task(settle_later())
    started = time.monotonic()
    await queue.wait_settled(engine, run_id, timeout_seconds=5)
    took = time.monotonic() - started
    await task
    assert took < 1.3


async def test_a_parent_follows_a_child_through_commits_too(running) -> None:
    engine, run_id = running
    seen = _count_selects(engine)
    runner = Runner(engine, {})

    async def settle_later() -> None:
        await asyncio.sleep(0.3)  # between two of the old 250 ms polls
        await _settle(engine, run_id, notify=True)

    task = asyncio.create_task(settle_later())
    started = time.monotonic()
    run = await runner._settled(run_id)
    took = time.monotonic() - started
    await task
    assert run.status == "completed"
    assert took < 0.42  # woken by the commit; the old poll would have found it at 0.5 s
    assert len(seen) <= 8


# ── housekeeping on its own interval ───────────────────────────────────────────


@pytest.fixture
def counting_sweep():
    calls: list[int] = []

    async def sweep(session: AsyncSession) -> int:
        calls.append(1)
        return 0

    queue.SWEEPS.append(sweep)
    yield calls
    queue.SWEEPS.remove(sweep)


async def test_sweeps_run_on_their_own_interval_not_before_every_claim(
    sqlite_engine: AsyncEngine, counting_sweep
) -> None:
    worker = Worker(sqlite_engine, Runner(sqlite_engine, {}), owner="w1")
    for _ in range(6):
        await worker.claim_some()
    assert len(counting_sweep) == 1  # the first claim sweeps; the next five are within the interval
    worker._last_sweep -= worker.sweep_seconds + 1  # the interval has passed
    await worker.claim_some()
    assert len(counting_sweep) == 2


async def test_a_zero_interval_sweeps_every_time(
    sqlite_engine: AsyncEngine, counting_sweep
) -> None:
    worker = Worker(sqlite_engine, Runner(sqlite_engine, {}), owner="w1", sweep_seconds=0)
    for _ in range(3):
        await worker.claim_some()
    assert len(counting_sweep) == 3
