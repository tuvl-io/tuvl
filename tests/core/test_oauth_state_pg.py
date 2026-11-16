"""A sign-in state can be used once, even when several workers try it at the same moment.

Skipped when ``TUVL_TEST_PG_URL`` points at nothing reachable.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.runtime.conftest import pg_engine  # noqa: F401 - shared fixture
from tuvl.core.auth.models import IAMOAuthState
from tuvl.core.auth.state_store import oauth_state_store

pytestmark = pytest.mark.asyncio

TABLES = [IAMOAuthState.__table__]


@pytest.fixture
async def tables(pg_engine: AsyncEngine):  # noqa: F811
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=TABLES))
    yield pg_engine
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))


async def test_a_state_is_used_by_exactly_one_of_many_workers(tables: AsyncEngine) -> None:
    async with AsyncSession(tables, expire_on_commit=False) as s:
        state = await oauth_state_store.new_state("google", {"verifier": "v"}, session=s)

    async def take() -> dict | None:
        async with AsyncSession(tables, expire_on_commit=False) as s:
            return await oauth_state_store.consume_state(state, "google", session=s)

    taken = await asyncio.gather(*(take() for _ in range(8)))
    assert [t for t in taken if t is not None] == [{"verifier": "v"}]


async def test_a_device_handle_is_dropped_once(tables: AsyncEngine) -> None:
    async with AsyncSession(tables, expire_on_commit=False) as s:
        handle = await oauth_state_store.new_device(
            "github", {"device_code": "d", "interval": 5}, ttl=900, session=s
        )

    async def look() -> dict | None:
        async with AsyncSession(tables, expire_on_commit=False) as s:
            return await oauth_state_store.get_device(handle, "github", session=s)

    assert all(
        r == {"device_code": "d", "interval": 5} for r in await asyncio.gather(look(), look())
    )
