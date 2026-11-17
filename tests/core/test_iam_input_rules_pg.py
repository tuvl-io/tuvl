"""The IAM rules that need a real PostgreSQL: the bootstrap lock, a role assigned twice at once, and the
unique index on lower(email).

Skipped when ``TUVL_TEST_PG_URL`` points at nothing reachable.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.runtime.conftest import pg_engine  # noqa: F401 - shared fixture
from tuvl.core.auth import iam_service
from tuvl.core.auth.iam_service import ConflictError
from tuvl.core.auth.models import (
    IAMRevokedToken,
    IAMRole,
    IAMRoleScope,
    IAMSessionEnd,
    IAMUser,
    IAMUserRole,
)
from tuvl.core.datasources import postgres

pytestmark = pytest.mark.asyncio

TABLES = [
    IAMUser.__table__,
    IAMRole.__table__,
    IAMUserRole.__table__,
    IAMRoleScope.__table__,
    IAMSessionEnd.__table__,
    IAMRevokedToken.__table__,
]
USERS = IAMUser.__table__.name
INDEX = "uq_tuvl_iam_users_email_lower"


@pytest.fixture
async def iam_tables(pg_engine: AsyncEngine):  # noqa: F811
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=TABLES))
    yield pg_engine
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=TABLES))


async def test_two_first_administrators_at_once_make_one(iam_tables: AsyncEngine) -> None:
    async def boot(n: int) -> str:
        async with AsyncSession(iam_tables, expire_on_commit=False) as session:
            try:
                await iam_service.bootstrap(
                    session, email=f"root{n}@example.com", password="correct-horse-1"
                )
            except ConflictError:
                return "refused"
            return "created"

    outcomes = await asyncio.gather(*(boot(n) for n in range(6)))
    assert sorted(outcomes) == ["created"] + ["refused"] * 5
    async with AsyncSession(iam_tables) as session:
        assert len((await session.exec(select(IAMUser))).all()) == 1
        assert await iam_service.active_admin_count(session) == 1


async def test_assigning_one_role_many_times_at_once_is_idempotent(iam_tables: AsyncEngine) -> None:
    async with AsyncSession(iam_tables, expire_on_commit=False) as session:
        user, _ = await iam_service.create_user(
            session, email="ann@example.com", password="correct-horse-1"
        )
        role, _ = await iam_service.create_role(session, name="ops")

    async def assign() -> None:
        async with AsyncSession(iam_tables, expire_on_commit=False) as session:
            await iam_service.assign_role(session, user.id, role.id)

    await asyncio.gather(*(assign() for _ in range(8)))
    async with AsyncSession(iam_tables) as session:
        assert len((await session.exec(select(IAMUserRole))).all()) == 1


async def test_the_index_makes_two_spellings_of_an_email_one_user(iam_tables: AsyncEngine) -> None:
    await postgres.ensure_iam_indexes(iam_tables)
    async with AsyncSession(iam_tables, expire_on_commit=False) as session:
        await iam_service.create_user(session, email="ann@example.com", password="correct-horse-1")
        session.add(IAMUser(email="x@example.com", is_active=True))
        await session.commit()
    async with iam_tables.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(f"UPDATE {USERS} SET email = 'ANN@example.com' WHERE email = 'x@example.com'")
            )


async def test_building_the_index_twice_is_harmless(iam_tables: AsyncEngine) -> None:
    await postgres.ensure_iam_indexes(iam_tables)
    await postgres.ensure_iam_indexes(iam_tables)
    async with iam_tables.connect() as conn:
        found = (
            await conn.execute(
                text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)"),
                {"n": INDEX},
            )
        ).scalar_one()
    assert found is True


async def test_spellings_stored_before_the_index_warn_and_do_not_stop_the_boot(
    iam_tables: AsyncEngine,
) -> None:
    async with iam_tables.begin() as conn:
        for email in ("Ann@Example.com", "ann@example.com", "bob@example.com"):
            await conn.execute(
                text(
                    f"INSERT INTO {USERS} (id, email, is_active, created_at) "
                    f"VALUES (gen_random_uuid(), :e, true, now())"
                ),
                {"e": email},
            )
    with structlog.testing.capture_logs() as logs:
        await postgres.ensure_iam_indexes(iam_tables)

    warned = [e for e in logs if e["event"] == "iam.email.duplicates"]
    assert len(warned) == 1 and warned[0]["groups"] == 1
    async with iam_tables.connect() as conn:
        built = (await conn.execute(text("SELECT to_regclass(:n)"), {"n": INDEX})).scalar_one()
    assert built is None
    async with AsyncSession(iam_tables) as session:  # lookups still answer
        assert await iam_service.get_user_by_login(session, "bob@example.com") is not None
