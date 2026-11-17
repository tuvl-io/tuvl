"""Failed logins counted by several workers at once: no guess is lost.

Skipped when ``TUVL_TEST_PG_URL`` points at nothing reachable.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.runtime.conftest import pg_engine  # noqa: F401 - shared fixture
from tuvl.core.auth import iam_service
from tuvl.core.auth.iam_service import AuthenticationError
from tuvl.core.auth.models import (
    IAMLoginAttempt,
    IAMRevokedToken,
    IAMRole,
    IAMRoleScope,
    IAMSessionEnd,
    IAMUser,
    IAMUserRole,
)

pytestmark = pytest.mark.asyncio

TABLES = [
    IAMUser.__table__,
    IAMRole.__table__,
    IAMUserRole.__table__,
    IAMRoleScope.__table__,
    IAMSessionEnd.__table__,
    IAMRevokedToken.__table__,
    IAMLoginAttempt.__table__,
]


@pytest.fixture
async def iam_tables(pg_engine: AsyncEngine):  # noqa: F811
    iam_service.reset_login_throttle()
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=TABLES))
    yield pg_engine
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))
    iam_service.reset_login_throttle()


async def test_guesses_made_at_once_are_all_counted(iam_tables: AsyncEngine) -> None:
    async with AsyncSession(iam_tables, expire_on_commit=False) as session:
        await iam_service.create_user(session, email="ann@example.com", password="correct-horse-1")

    async def guess() -> None:
        async with AsyncSession(iam_tables, expire_on_commit=False) as session:
            with pytest.raises(AuthenticationError):
                await iam_service.login(
                    session, login_str="ann@example.com", password="wrong-password-1"
                )

    await asyncio.gather(*(guess() for _ in range(5)))
    async with AsyncSession(iam_tables) as session:
        row = (await session.exec(select(IAMLoginAttempt))).one()
    assert row.failures == 5


async def test_guesses_at_an_unknown_login_made_at_once_make_one_row(
    iam_tables: AsyncEngine,
) -> None:
    async def guess() -> None:
        async with AsyncSession(iam_tables, expire_on_commit=False) as session:
            with pytest.raises(AuthenticationError):
                await iam_service.login(
                    session, login_str="ghost@example.com", password="wrong-password-1"
                )

    await asyncio.gather(*(guess() for _ in range(5)))
    async with AsyncSession(iam_tables) as session:
        rows = (await session.exec(select(IAMLoginAttempt))).all()
    assert [r.failures for r in rows] == [5]
