"""Removing finished runs, and everything that belongs to them: `tuvl prune runs` and the optional sweep."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tuvl.core.config import settings
from tuvl.runtime import queue
from tuvl.runtime.models import (
    TERMINAL_STATUSES,
    SystemApproval,
    SystemRun,
    SystemRunCheckpoint,
    SystemRunDelta,
    SystemRunEvent,
    SystemWebhookDelivery,
)
from tuvl.storage.errors import NotFound, Unavailable

NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.UTC)
DEPENDENTS = (
    "tuvl_system_run_events",
    "tuvl_system_run_checkpoints",
    "tuvl_system_approvals",
    "tuvl_system_webhook_deliveries",
    "tuvl_system_run_deltas",
)


async def make_run(
    engine: AsyncEngine,
    *,
    status: str,
    age_days: float,
    input: dict[str, Any] | None = None,
) -> uuid.UUID:
    """A run `age_days` old that ended (or not) then, with one row in every table that belongs to it."""
    when = NOW - dt.timedelta(days=age_days)
    run = SystemRun(
        workflow="w",
        workflow_hash="h",
        status=status,
        trigger="http",
        input=input or {},
        created_at=when,
        updated_at=when,
        finished_at=when if status in TERMINAL_STATUSES else None,
    )
    async with AsyncSession(engine, expire_on_commit=False) as s:
        s.add(run)
        await s.flush()
        s.add_all(
            [
                SystemRunEvent(run_id=run.id, seq=1, type="run_queued", created_at=when),
                SystemRunCheckpoint(
                    run_id=run.id,
                    seq=1,
                    agent_id="a",
                    signal="default",
                    next="END",
                    created_at=when,
                ),
                SystemApproval(
                    run_id=run.id, agent_id="a", kind="human", status="decided", created_at=when
                ),
                SystemWebhookDelivery(
                    run_id=run.id,
                    workflow="w",
                    webhook="hook",
                    type="run_completed",
                    seq=1,
                    status="delivered",
                    created_at=when,
                ),
                SystemRunDelta(run_id=run.id, agent_id="a", text="x", created_at=when),
            ]
        )
        await s.commit()
    return run.id


async def present(engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, int]:
    out: dict[str, int] = {}
    async with engine.connect() as conn:
        out["tuvl_system_runs"] = (
            await conn.execute(
                sa.text("SELECT count(*) FROM tuvl_system_runs WHERE id = :i"), {"i": run_id}
            )
        ).scalar_one()
        for table in DEPENDENTS:
            out[table] = (
                await conn.execute(
                    sa.text(f"SELECT count(*) FROM {table} WHERE run_id = :i"), {"i": run_id}
                )
            ).scalar_one()
    return out


@pytest.fixture
def retention():
    from tuvl.runtime import retention as module

    return module


async def test_an_old_finished_run_goes_with_everything_that_belongs_to_it(
    pg_engine: AsyncEngine, retention
) -> None:
    old = await make_run(pg_engine, status="completed", age_days=40)
    recent = await make_run(pg_engine, status="completed", age_days=3)
    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(s, older_than=dt.timedelta(days=30), now=NOW)
    assert result.runs == 1
    assert all(n == 0 for n in (await present(pg_engine, old)).values())
    assert all(n == 1 for n in (await present(pg_engine, recent)).values())
    assert result.rows["tuvl_system_run_events"] == 1


@pytest.mark.parametrize(
    "status", ["queued", "running", "waiting_human", "waiting_approval", "paused", "compensating"]
)
async def test_a_run_that_has_not_ended_is_never_removed(
    pg_engine: AsyncEngine, retention, status: str
) -> None:
    run = await make_run(pg_engine, status=status, age_days=400)
    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(s, older_than=dt.timedelta(days=30), now=NOW)
    assert result.runs == 0
    assert (await present(pg_engine, run))["tuvl_system_runs"] == 1


async def test_a_status_filter_removes_only_those_runs(pg_engine: AsyncEngine, retention) -> None:
    done = await make_run(pg_engine, status="completed", age_days=40)
    failed = await make_run(pg_engine, status="failed", age_days=40)
    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(
            s, older_than=dt.timedelta(days=30), statuses={"failed"}, now=NOW
        )
    assert result.runs == 1
    assert (await present(pg_engine, failed))["tuvl_system_runs"] == 0
    assert (await present(pg_engine, done))["tuvl_system_runs"] == 1


async def test_a_status_that_can_still_change_is_refused(pg_engine: AsyncEngine, retention) -> None:
    async with AsyncSession(pg_engine) as s:
        with pytest.raises(ValueError, match="running"):
            await retention.prune(
                s, older_than=dt.timedelta(days=1), statuses={"completed", "running"}, now=NOW
            )


async def test_a_dry_run_counts_and_removes_nothing(pg_engine: AsyncEngine, retention) -> None:
    old = await make_run(pg_engine, status="completed", age_days=40)
    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(s, older_than=dt.timedelta(days=30), dry_run=True, now=NOW)
    assert result.runs == 1 and result.dry_run is True
    assert all(n == 1 for n in (await present(pg_engine, old)).values())


async def test_many_runs_go_in_batches_with_a_commit_each(
    pg_engine: AsyncEngine, retention
) -> None:
    ids = [await make_run(pg_engine, status="completed", age_days=40 + i) for i in range(25)]
    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(s, older_than=dt.timedelta(days=30), batch=10, now=NOW)
    assert result.runs == 25 and result.batches == 3
    for run_id in ids:
        assert (await present(pg_engine, run_id))["tuvl_system_runs"] == 0


FILE_A = {
    "store": "docs",
    "key": "in/a.pdf",
    "name": "a.pdf",
    "size": 3,
    "content_type": "x",
    "sha256": "0",
}
FILE_B = {
    "store": "scans",
    "key": "in/b.png",
    "name": "b.png",
    "size": 3,
    "content_type": "x",
    "sha256": "1",
}


def test_file_references_are_found_wherever_the_input_holds_them(retention) -> None:
    payload = {"title": "t", "attachment": FILE_A, "more": [{"nested": FILE_B}], "n": 3}
    assert sorted(retention.file_refs(payload)) == [("docs", "in/a.pdf"), ("scans", "in/b.png")]
    assert retention.file_refs({"almost": {"store": "x", "key": "y"}}) == []  # not a file reference


async def test_the_uploads_of_a_pruned_run_are_deleted_when_asked(
    pg_engine: AsyncEngine, retention
) -> None:
    run = await make_run(
        pg_engine, status="completed", age_days=40, input={"a": FILE_A, "list": [FILE_B]}
    )
    deleted: list[tuple[str, str]] = []

    async def delete(store: str, key: str) -> None:
        deleted.append((store, key))

    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(
            s, older_than=dt.timedelta(days=30), delete_object=delete, now=NOW
        )
    assert sorted(deleted) == [("docs", "in/a.pdf"), ("scans", "in/b.png")]
    assert result.uploads == 2 and (await present(pg_engine, run))["tuvl_system_runs"] == 0


async def test_an_object_that_is_already_gone_does_not_keep_its_run(
    pg_engine: AsyncEngine, retention
) -> None:
    run = await make_run(pg_engine, status="completed", age_days=40, input={"a": FILE_A})

    async def delete(store: str, key: str) -> None:
        raise NotFound(key)

    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(
            s, older_than=dt.timedelta(days=30), delete_object=delete, now=NOW
        )
    assert result.runs == 1 and (await present(pg_engine, run))["tuvl_system_runs"] == 0


async def test_a_run_whose_upload_cannot_be_deleted_now_is_kept_for_the_next_time(
    pg_engine: AsyncEngine, retention
) -> None:
    kept = await make_run(pg_engine, status="completed", age_days=40, input={"a": FILE_A})
    fine = await make_run(pg_engine, status="completed", age_days=41)

    async def delete(store: str, key: str) -> None:
        raise Unavailable("the store is down")

    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(
            s, older_than=dt.timedelta(days=30), delete_object=delete, now=NOW
        )
    assert result.runs == 1 and result.skipped == 1
    assert (await present(pg_engine, kept))[
        "tuvl_system_runs"
    ] == 1  # its object may still be there
    assert (await present(pg_engine, fine))["tuvl_system_runs"] == 0


async def test_without_the_uploads_flag_objects_are_left_alone(
    pg_engine: AsyncEngine, retention
) -> None:
    await make_run(pg_engine, status="completed", age_days=40, input={"a": FILE_A})
    async with AsyncSession(pg_engine) as s:
        result = await retention.prune(s, older_than=dt.timedelta(days=30), now=NOW)
    assert result.runs == 1 and result.uploads == 0


# ── the sweep ─────────────────────────────────────────────────────────────────


async def test_the_sweep_is_off_by_default(pg_engine: AsyncEngine, retention, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tuvl_run_retention_days", 0)
    old = await make_run(pg_engine, status="completed", age_days=400)
    async with AsyncSession(pg_engine) as s:
        assert await retention.retention_sweep(s) == 0
        await s.commit()
    assert (await present(pg_engine, old))["tuvl_system_runs"] == 1


async def test_the_sweep_removes_a_batch_of_old_runs_each_time(
    pg_engine: AsyncEngine, retention, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "tuvl_run_retention_days", 30)
    monkeypatch.setattr(retention, "SWEEP_BATCH", 4)
    monkeypatch.setattr(retention, "_now", lambda: NOW)
    ids = [await make_run(pg_engine, status="completed", age_days=40 + i) for i in range(6)]
    fresh = await make_run(pg_engine, status="completed", age_days=2)
    async with AsyncSession(pg_engine) as s:
        assert await retention.retention_sweep(s) == 4
        await s.commit()
    async with AsyncSession(pg_engine) as s:
        assert await retention.retention_sweep(s) == 2
        await s.commit()
    for run_id in ids:
        assert (await present(pg_engine, run_id))["tuvl_system_runs"] == 0
    assert (await present(pg_engine, fresh))["tuvl_system_runs"] == 1


def test_the_sweep_is_registered_with_the_worker(retention) -> None:
    assert retention.retention_sweep in queue.SWEEPS
