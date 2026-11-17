"""Starting several replicas on one database: the schema is made once, no ALTER runs on an up-to-date
database, a datasource without a read replica holds one pool, and a database that cannot be reached
stops the process it started in rather than the process that started it."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel

from tests.runtime.conftest import PG_URL, pg_engine  # noqa: F401 - shared fixture
from tuvl.core.auth.models import IAMRole, IAMUser
from tuvl.core.config import Settings, settings
from tuvl.core.datasources import loader, postgres

pytestmark = pytest.mark.asyncio

TABLES = [IAMUser.__table__, IAMRole.__table__]


@pytest.fixture
async def empty(pg_engine: AsyncEngine):  # noqa: F811
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))
    yield pg_engine
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))


async def test_replicas_starting_together_create_the_schema_once(empty: AsyncEngine) -> None:
    engines = [create_async_engine(PG_URL) for _ in range(6)]
    try:
        await asyncio.gather(*(postgres.ensure_schema(e, TABLES) for e in engines))
    finally:
        for e in engines:
            await e.dispose()
    async with empty.connect() as conn:
        found = await conn.run_sync(
            lambda c: (
                sa.inspect(c).has_table(IAMUser.__table__.name)
                and sa.inspect(c).has_table(IAMRole.__table__.name)
            )
        )
    assert found


async def test_a_process_waits_for_the_schema_lock_without_holding_a_snapshot(
    empty: AsyncEngine,
) -> None:
    holder = await empty.connect()
    try:
        await holder.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": postgres._SCHEMA_LOCK})
        with pytest.raises(postgres.PostgresStartupError, match="schema"):
            await postgres.ensure_schema(empty, TABLES, timeout=0.6)
        await holder.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": postgres._SCHEMA_LOCK})
    finally:
        await holder.close()
    await postgres.ensure_schema(empty, TABLES, timeout=5)  # the lock is free: it goes through


async def test_an_up_to_date_database_is_not_altered(empty: AsyncEngine) -> None:
    await postgres.ensure_schema(empty, TABLES)
    seen: list[str] = []

    def record(conn, cursor, statement, *_):
        seen.append(statement)

    event.listen(empty.sync_engine, "before_cursor_execute", record)
    try:
        async with empty.begin() as conn:
            await postgres.ensure_iam_columns(conn)
        await postgres.ensure_schema(empty, TABLES, extra=postgres.ensure_iam_columns)
    finally:
        event.remove(empty.sync_engine, "before_cursor_execute", record)
    assert not [s for s in seen if "ALTER TABLE" in s.upper()]


async def test_a_column_a_table_lacks_is_added(empty: AsyncEngine) -> None:
    await postgres.ensure_schema(empty, TABLES)
    async with empty.begin() as conn:
        await conn.execute(sa.text(f'ALTER TABLE "{IAMRole.__table__.name}" DROP COLUMN is_active'))
    async with empty.begin() as conn:
        await postgres.ensure_iam_columns(conn)
    async with empty.connect() as conn:
        columns = await conn.run_sync(
            lambda c: {col["name"] for col in sa.inspect(c).get_columns(IAMRole.__table__.name)}
        )
    assert "is_active" in columns


# ── one pool unless there is a read replica ───────────────────────────────────


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    def declare(**connection: Any) -> None:
        entry = {
            "spec": {
                "type": "postgres",
                "connection": {
                    "host": "db.internal",
                    "port": 5432,
                    "database": "d",
                    "username": "u",
                    "password": "p",
                    **connection,
                },
            }
        }
        monkeypatch.setattr(postgres, "DATASOURCE_REGISTRY", {"main": entry})
        monkeypatch.setattr(loader, "PRIMARY_DS", "main")

    for store in (postgres._engines_rw, postgres._engines_ro):
        store.clear()
    for store in (postgres._sessions_rw, postgres._sessions_ro):
        store.clear()
    yield declare
    for store in (postgres._engines_rw, postgres._engines_ro):
        store.clear()
    for store in (postgres._sessions_rw, postgres._sessions_ro):
        store.clear()


async def test_without_a_read_replica_there_is_one_pool(registry) -> None:
    registry()
    postgres._build_engines()
    assert postgres._engines_ro["main"] is postgres._engines_rw["main"]
    assert postgres._sessions_ro["main"] is postgres._sessions_rw["main"]
    await postgres.close_db()


async def test_with_a_read_replica_there_are_two(registry) -> None:
    registry(ro_host="replica.internal")
    postgres._build_engines()
    assert postgres._engines_ro["main"] is not postgres._engines_rw["main"]
    assert "replica.internal" in str(postgres._engines_ro["main"].url)
    await postgres.close_db()


def test_the_pool_settings_nothing_read_are_gone() -> None:
    for name in ("db_echo", "db_pool_min", "db_pool_max"):
        assert name not in Settings.model_fields


# ── a database that cannot be reached ─────────────────────────────────────────


async def test_in_development_the_reloader_is_stopped_and_the_process_exits(monkeypatch) -> None:
    from tuvl.core import app as app_module

    killed: list[tuple[int, int]] = []
    exited: list[int] = []
    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    monkeypatch.setattr(app_module.os, "getppid", lambda: 4242)
    monkeypatch.setattr(app_module.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(app_module.os, "_exit", lambda code: exited.append(code))
    await app_module._database_unreachable(postgres.PostgresStartupError("down"))
    assert killed == [(4242, signal.SIGTERM)] and exited == [1]


async def test_in_production_the_startup_fails_without_touching_the_parent(monkeypatch) -> None:
    from tuvl.core import app as app_module

    killed: list[int] = []
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    monkeypatch.setattr(app_module.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(app_module.os, "_exit", lambda code: killed.append(-code))
    assert os.getppid() != 0
    with pytest.raises(postgres.PostgresStartupError, match="down"):
        await app_module._database_unreachable(postgres.PostgresStartupError("down"))
    assert killed == []
