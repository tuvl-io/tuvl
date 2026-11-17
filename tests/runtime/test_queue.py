"""Run queue and leases (P1.3)."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.runtime import journal, queue
from tuvl.runtime.models import SystemRun
from tuvl.runtime.queue import Lease, Worker, run_inline

pytestmark = pytest.mark.asyncio


async def enqueue(engine: AsyncEngine, n: int = 1) -> list[uuid.UUID]:
    ids = []
    async with AsyncSession(engine) as s:
        for i in range(n):
            ids.append(
                await queue.enqueue(
                    s, workflow="wf", workflow_hash="h", trigger="api", input={"i": i}
                )
            )
        await s.commit()
    return ids


async def get(engine: AsyncEngine, run_id: uuid.UUID) -> SystemRun:
    async with AsyncSession(engine) as s:
        run = await s.get(SystemRun, run_id)
        assert run is not None
        return run


async def test_enqueue_journals_and_claim_leases(engine: AsyncEngine) -> None:
    (run_id,) = await enqueue(engine)
    async with AsyncSession(engine) as s:
        assert [e.type for e in await journal.events(s, run_id)] == ["run_queued"]
        assert await queue.claim(s, "w1") == [run_id]
        await s.commit()
    run = await get(engine, run_id)
    assert run.status == "running" and run.lease_owner == "w1" and run.claims == 1
    assert run.lease_expires_at is not None and run.started_at is not None
    async with AsyncSession(engine) as s:
        assert await queue.claim(s, "w2") == []  # nothing left to claim


async def test_claim_is_oldest_first_and_limited(engine: AsyncEngine) -> None:
    ids = await enqueue(engine, 3)
    async with AsyncSession(engine) as s:
        assert await queue.claim(s, "w1", limit=2) == ids[:2]
        await s.commit()


async def test_concurrent_claims_never_overlap(pg_engine: AsyncEngine) -> None:
    ids = await enqueue(pg_engine, 30)

    async def worker(name: str) -> list[uuid.UUID]:
        got: list[uuid.UUID] = []
        while True:
            async with AsyncSession(pg_engine) as s:
                batch = await queue.claim(s, name, limit=2)
                await s.commit()
            if not batch:
                return got
            got += batch

    results = await asyncio.gather(*(worker(f"w{i}") for i in range(6)))
    claimed = [r for batch in results for r in batch]
    assert sorted(claimed) == sorted(ids)  # every run once, no run twice


async def test_expired_lease_is_reclaimed_and_old_owner_loses_it(pg_engine: AsyncEngine) -> None:
    (run_id,) = await enqueue(pg_engine)
    async with AsyncSession(pg_engine) as s:
        await queue.claim(s, "crashed", lease_seconds=0.2)
        await s.commit()
    await asyncio.sleep(0.4)
    async with AsyncSession(pg_engine) as s:
        assert await queue.claim(s, "survivor") == [run_id]
        await s.commit()
    run = await get(pg_engine, run_id)
    assert run.lease_owner == "survivor" and run.claims == 2
    async with AsyncSession(pg_engine) as s:
        assert await queue.heartbeat(s, run_id, "crashed") is False
        assert await queue.release(s, run_id, "crashed", "completed") is False
        assert await queue.heartbeat(s, run_id, "survivor") is True
        await s.commit()


async def test_live_lease_is_not_reclaimed(pg_engine: AsyncEngine) -> None:
    (run_id,) = await enqueue(pg_engine)
    async with AsyncSession(pg_engine) as s:
        await queue.claim(s, "w1", lease_seconds=30)
        await s.commit()
    async with AsyncSession(pg_engine) as s:
        assert await queue.claim(s, "w2") == []


async def test_release_and_requeue(engine: AsyncEngine) -> None:
    (run_id,) = await enqueue(engine)
    async with AsyncSession(engine) as s:
        await queue.claim(s, "w1")
        assert await queue.release(s, run_id, "w1", "waiting_human", current_agent="approval")
        await s.commit()
    run = await get(engine, run_id)
    assert run.status == "waiting_human" and run.lease_owner is None and run.finished_at is None
    async with AsyncSession(engine) as s:
        assert await queue.requeue(s, run_id) is True
        assert await queue.claim(s, "w2") == [run_id]
        assert await queue.release(
            s, run_id, "w2", "completed", output={"status": 200, "body": {"ok": True}}
        )
        await s.commit()
    run = await get(engine, run_id)
    assert (
        run.status == "completed"
        and run.finished_at is not None
        and run.output == {"status": 200, "body": {"ok": True}}
    )
    async with AsyncSession(engine) as s:
        assert await queue.requeue(s, run_id) is False  # terminal runs never requeue


async def test_workers_execute_every_run_exactly_once(pg_engine: AsyncEngine) -> None:
    ids = await enqueue(pg_engine, 12)
    executed: list[uuid.UUID] = []

    async def executor(run_id: uuid.UUID, lease: Lease) -> None:
        await asyncio.sleep(0.01)
        executed.append(run_id)
        async with AsyncSession(pg_engine) as s:
            await queue.release(s, run_id, lease.owner, "completed")
            await s.commit()

    workers = [
        Worker(pg_engine, executor, owner=f"w{i}", concurrency=3, poll_seconds=0.05)
        for i in range(3)
    ]
    loops = [asyncio.create_task(w.run_forever()) for w in workers]
    for _ in range(100):
        if len(executed) == len(ids):
            break
        await asyncio.sleep(0.05)
    for w in workers:
        await w.stop()
    await asyncio.gather(*loops)
    assert sorted(executed) == sorted(ids)
    for run_id in ids:
        assert (await get(pg_engine, run_id)).status == "completed"


async def test_crashing_executor_fails_the_run(engine: AsyncEngine) -> None:
    (run_id,) = await enqueue(engine)

    async def executor(run_id: uuid.UUID, lease: Lease) -> None:
        raise RuntimeError("boom")

    worker = Worker(engine, executor, owner="w1")
    assert await worker.claim_some() == 1
    await worker.drain()
    run = await get(engine, run_id)
    assert run.status == "failed" and run.error == {
        "error_type": "executor_crashed",
        "message": "boom",
    }
    async with AsyncSession(engine) as s:
        assert [e.type for e in await journal.events(s, run_id)] == ["run_queued", "run_failed"]


async def test_heartbeat_keeps_a_long_run_alive(pg_engine: AsyncEngine) -> None:
    (run_id,) = await enqueue(pg_engine)
    stolen: list[uuid.UUID] = []

    async def slow(run_id: uuid.UUID, lease: Lease) -> None:
        for _ in range(8):  # 1.6 s, lease is 1.0 s: only the heartbeat keeps the run ours
            await asyncio.sleep(0.2)
            async with AsyncSession(pg_engine) as s:
                stolen.extend(await queue.claim(s, "thief", lease_seconds=1.0))
                await s.commit()
        assert not lease.lost.is_set()
        async with AsyncSession(pg_engine) as s:
            await queue.release(s, run_id, lease.owner, "completed")
            await s.commit()

    worker = Worker(pg_engine, slow, owner="w1", lease_seconds=1.0, heartbeat_seconds=0.1)
    await worker.claim_some()
    await worker.drain()
    assert stolen == []
    assert (await get(pg_engine, run_id)).status == "completed"


async def test_run_inline_done_and_promoted(pg_engine: AsyncEngine) -> None:
    fast, slow = await enqueue(pg_engine, 2)

    async def executor(run_id: uuid.UUID, lease: Lease) -> None:
        await asyncio.sleep(0.5 if run_id == slow else 0)
        async with AsyncSession(pg_engine) as s:
            await queue.release(s, run_id, lease.owner, "completed")
            await s.commit()

    worker = Worker(pg_engine, executor, owner="api-1")
    assert await run_inline(worker, fast, timeout_seconds=2) == "done"
    assert await run_inline(worker, slow, timeout_seconds=0.05) == "promoted"
    assert (await get(pg_engine, slow)).status == "running"  # still going in the background
    await worker.drain()
    assert (await get(pg_engine, slow)).status == "completed"
    assert await run_inline(worker, fast, timeout_seconds=1) == "not_claimed"


async def test_wait_settled_follows_a_run_another_worker_claimed(pg_engine: AsyncEngine) -> None:
    (run_id,) = await enqueue(pg_engine)

    async def executor(run_id: uuid.UUID, lease: Lease) -> None:
        await asyncio.sleep(0.15)
        async with AsyncSession(pg_engine) as s:
            await queue.release(s, run_id, lease.owner, "completed")
            await s.commit()

    background = Worker(pg_engine, executor, owner="background")
    await background.claim_some()  # the poll loop wins the claim
    api = Worker(pg_engine, executor, owner="api-1")
    assert await run_inline(api, run_id, timeout_seconds=2) == "not_claimed"
    assert (await get(pg_engine, run_id)).status == "running"
    await queue.wait_settled(pg_engine, run_id, 2)
    assert (await get(pg_engine, run_id)).status == "completed"
    await background.drain()


async def test_wait_settled_gives_up_at_the_deadline(pg_engine: AsyncEngine) -> None:
    (run_id,) = await enqueue(pg_engine)
    async with AsyncSession(pg_engine) as s:
        await queue.claim(s, "elsewhere")
        await s.commit()
    await queue.wait_settled(pg_engine, run_id, 0.1)
    assert (await get(pg_engine, run_id)).status == "running"


# ── scheduled fires (P1.8) ──────────────────────────────────────────────────


async def test_scheduled_fire_enqueues_exactly_once(pg_engine: AsyncEngine) -> None:
    import asyncio
    from datetime import UTC, datetime

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
    set_host(RuntimeHost(pg_engine, workflows={"nightly": doc}, role="api"))
    try:
        fire_at = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)
        results = await asyncio.gather(
            *(scheduler.enqueue_scheduled("nightly", fire_at) for _ in range(5))
        )
        assert sum(results) == 1
        assert not await scheduler.enqueue_scheduled("nightly", fire_at)  # a late process
        assert await scheduler.enqueue_scheduled("nightly", fire_at.replace(day=10))  # next fire
        async with AsyncSession(pg_engine) as s:
            runs = (await s.exec(sa.select(SystemRun.trigger).order_by(SystemRun.created_at))).all()  # type: ignore[call-overload,arg-type]
        assert [r[0] for r in runs] == [
            "schedule:2026-10-09T02:00:00+00:00",
            "schedule:2026-10-10T02:00:00+00:00",
        ]
    finally:
        set_host(None)


async def test_drain_waits_then_interrupts_and_another_worker_resumes(
    pg_engine: AsyncEngine,
) -> None:
    (run_id,) = await enqueue(pg_engine, 1)
    started = asyncio.Event()

    async def stuck(run_id: uuid.UUID, lease: Lease) -> None:
        started.set()
        await asyncio.sleep(30)

    worker = Worker(pg_engine, stuck, owner="w1", lease_seconds=0.5, heartbeat_seconds=0.1)
    assert await worker.claim_some() == 1
    await started.wait()
    began = asyncio.get_running_loop().time()
    await worker.drain(0.2)
    assert asyncio.get_running_loop().time() - began < 2
    assert worker._active == {}
    assert (await get(pg_engine, run_id)).status == "running"  # not failed: it was interrupted

    resumed: list[uuid.UUID] = []

    async def finish(run_id: uuid.UUID, lease: Lease) -> None:
        resumed.append(run_id)
        async with AsyncSession(pg_engine) as s:
            await queue.release(s, run_id, lease.owner, "completed")
            await s.commit()

    other = Worker(pg_engine, finish, owner="w2", lease_seconds=0.5)
    for _ in range(40):
        if await other.claim_some():
            break
        await asyncio.sleep(0.1)
    await other.drain()
    assert resumed == [run_id] and (await get(pg_engine, run_id)).status == "completed"


async def test_drain_with_no_runs_returns_at_once(pg_engine: AsyncEngine) -> None:
    async def never(run_id: uuid.UUID, lease: Lease) -> None: ...

    await Worker(pg_engine, never, owner="w").drain(5)


# ── a worker survives what goes wrong around it ──────────────────────────────


async def test_a_failing_claim_does_not_stop_the_worker(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run_id,) = await enqueue(pg_engine)
    real, calls = queue.claim, {"n": 0}

    async def flaky(*args: Any, **kwargs: Any) -> list[uuid.UUID]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection reset")
        return await real(*args, **kwargs)

    monkeypatch.setattr(queue, "claim", flaky)
    done = asyncio.Event()

    async def executor(run_id: uuid.UUID, lease: Lease) -> None:
        async with AsyncSession(pg_engine) as s:
            await queue.release(s, run_id, lease.owner, "completed")
            await s.commit()
        done.set()

    worker = Worker(pg_engine, executor, owner="w1", poll_seconds=0.05)
    loop = asyncio.create_task(worker.run_forever())
    try:
        await asyncio.wait_for(done.wait(), 5)
    finally:
        await worker.stop()
        await asyncio.wait_for(loop, 5)
    assert calls["n"] >= 2
    assert (await get(pg_engine, run_id)).status == "completed"


async def test_a_failing_sweep_does_not_stop_the_claim(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enqueue(engine)
    ran: list[str] = []

    async def bad(session: AsyncSession) -> int:
        raise RuntimeError("sweep failed")

    async def good(session: AsyncSession) -> int:
        ran.append("good")
        return 0

    async def idle(run_id: uuid.UUID, lease: Lease) -> None: ...

    monkeypatch.setattr(queue, "SWEEPS", [bad, good])
    worker = Worker(engine, idle, owner="w1")
    assert await worker.claim_some() == 1
    assert ran == ["good"]
    await worker.drain()


async def test_a_failed_heartbeat_is_retried(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run_id,) = await enqueue(pg_engine)
    real, calls = queue.heartbeat, {"n": 0}

    async def flaky(*args: Any, **kwargs: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection reset")
        return await real(*args, **kwargs)

    monkeypatch.setattr(queue, "heartbeat", flaky)
    stolen: list[uuid.UUID] = []

    async def slow(run_id: uuid.UUID, lease: Lease) -> None:
        for _ in range(10):  # 2 s on a 1 s lease: only the retried heartbeat keeps the run ours
            await asyncio.sleep(0.2)
            async with AsyncSession(pg_engine) as s:
                stolen.extend(await queue.claim(s, "thief", lease_seconds=1.0))
                await s.commit()
        assert not lease.lost.is_set()
        async with AsyncSession(pg_engine) as s:
            await queue.release(s, run_id, lease.owner, "completed")
            await s.commit()

    worker = Worker(pg_engine, slow, owner="w1", lease_seconds=1.0, heartbeat_seconds=0.3)
    await worker.claim_some()
    await worker.drain()
    assert stolen == [] and calls["n"] >= 2
    assert (await get(pg_engine, run_id)).status == "completed"


async def test_a_lost_lease_cancels_the_running_step(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run_id,) = await enqueue(pg_engine)
    cancelled = asyncio.Event()

    async def forever(run_id: uuid.UUID, lease: Lease) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def gone(*args: Any, **kwargs: Any) -> bool:
        return False  # the update matched no row: another worker owns the run

    monkeypatch.setattr(queue, "heartbeat", gone)
    worker = Worker(pg_engine, forever, owner="w1", lease_seconds=1.0, heartbeat_seconds=0.1)
    await worker.claim_some()
    await asyncio.wait_for(cancelled.wait(), 3)
    await worker.drain(2)
    run = await get(pg_engine, run_id)
    assert run.status == "running" and run.error is None  # the new owner decides, not this worker


async def test_an_unreachable_database_for_a_whole_lease_ends_the_step(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enqueue(pg_engine)
    cancelled = asyncio.Event()

    async def forever(run_id: uuid.UUID, lease: Lease) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def down(*args: Any, **kwargs: Any) -> bool:
        raise OSError("database unreachable")

    monkeypatch.setattr(queue, "heartbeat", down)
    worker = Worker(pg_engine, forever, owner="w1", lease_seconds=0.6, heartbeat_seconds=0.1)
    await worker.claim_some()
    await asyncio.wait_for(cancelled.wait(), 3)  # after lease_seconds with no good beat
    await worker.drain(2)


async def test_the_worker_reports_its_polling(pg_engine: AsyncEngine) -> None:
    async def idle(run_id: uuid.UUID, lease: Lease) -> None: ...

    worker = Worker(pg_engine, idle, owner="w1", poll_seconds=0.05)
    assert worker.last_poll is None
    loop = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.4)
    assert worker.last_poll is not None and time.monotonic() - worker.last_poll < 1
    await worker.stop()
    await asyncio.wait_for(loop, 5)
