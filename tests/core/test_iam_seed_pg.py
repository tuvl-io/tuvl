"""The IAM seed and the roles migration on a real PostgreSQL (advisory lock, ALTER TABLE).

Skipped when ``TUVL_TEST_PG_URL`` points at nothing reachable.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.runtime.conftest import pg_engine  # noqa: F401 - shared fixture
from tuvl.core.auth import iam_service
from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole
from tuvl.core.auth.seed import RoleSeedEntry, seed_iam
from tuvl.core.bootstrap import SystemRegistry
from tuvl.core.datasources.postgres import ensure_iam_columns

pytestmark = pytest.mark.asyncio

TABLES = [
    IAMUser.__table__,
    IAMRole.__table__,
    IAMUserRole.__table__,
    IAMRoleScope.__table__,
    SystemRegistry.__table__,
]
ROLES = [RoleSeedEntry("recruiter", None, ("candidate:read",)), RoleSeedEntry("viewer")]


@pytest.fixture
async def iam_tables(pg_engine: AsyncEngine):  # noqa: F811
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=TABLES))
    yield pg_engine
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))


async def test_replicas_booting_together_seed_exactly_once(iam_tables: AsyncEngine) -> None:
    async def boot() -> str:
        async with AsyncSession(iam_tables, expire_on_commit=False) as session:
            result = await seed_iam(
                session, roles=ROLES, admin_email="root@example.com", admin_password="pw-1234567"
            )
            await session.commit()
            return result.status

    statuses = await asyncio.gather(*(boot() for _ in range(6)))
    assert sorted(statuses) == ["already_seeded"] * 5 + ["seeded"]

    async with AsyncSession(iam_tables) as session:
        assert len((await session.exec(select(IAMRole))).all()) == 3  # two seeded + superadmin
        assert len((await session.exec(select(IAMUser))).all()) == 1
        assert await iam_service.active_admin_count(session) == 1


async def test_a_database_made_before_roles_could_be_disabled_gets_the_column(
    pg_engine: AsyncEngine,  # noqa: F811
) -> None:
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))
        await conn.execute(
            text(
                "CREATE TABLE tuvl_system_iam_roles "
                "(id uuid PRIMARY KEY, name varchar NOT NULL UNIQUE, description varchar)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO tuvl_system_iam_roles VALUES "
                "('00000000-0000-0000-0000-000000000001', 'legacy', 'from 2.0.3')"
            )
        )
    try:
        for _ in range(2):  # idempotent
            async with pg_engine.begin() as conn:
                await ensure_iam_columns(conn)
        async with pg_engine.begin() as conn:
            rows = (
                await conn.execute(text("SELECT name, is_active FROM tuvl_system_iam_roles"))
            ).all()
        assert [tuple(r) for r in rows] == [("legacy", True)]
    finally:
        async with pg_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS tuvl_system_iam_roles"))
