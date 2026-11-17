"""The tables RLS covers are real, and the role the engine connects as can be checked for bypassing it."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.runtime.conftest import PG_URL
from tuvl.core.datasources import rls

ROLES = {"tuvl_rls_plain": "NOBYPASSRLS", "tuvl_rls_bypass": "BYPASSRLS"}


def test_every_hand_listed_system_table_is_a_real_tenant_table() -> None:
    import tuvl.core.artifacts.models  # noqa: F401 — defines its tables
    import tuvl.core.models.system  # noqa: F401
    import tuvl.runtime.models  # noqa: F401
    from tuvl.core.models.base import tenant_tables

    real = {table.name for table in tenant_tables()}
    assert set(rls._SYSTEM_TENANT_TABLES) <= real, set(rls._SYSTEM_TENANT_TABLES) - real


@pytest.fixture
async def pg() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(PG_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            for name, attribute in ROLES.items():
                await conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
                await conn.execute(text(f"CREATE ROLE {name} NOLOGIN {attribute}"))
    except Exception as exc:  # no database, or a role that may not create roles
        await engine.dispose()
        pytest.skip(f"cannot prepare roles on Postgres at TUVL_TEST_PG_URL ({type(exc).__name__})")
    yield engine
    async with engine.begin() as conn:
        for name in ROLES:
            await conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
    await engine.dispose()


async def _reason(engine: AsyncEngine, role: str | None = None) -> str | None:
    async with engine.begin() as conn:
        if role:
            await conn.execute(text(f"SET LOCAL ROLE {role}"))
        return await rls.rls_bypass_reason(conn)


async def test_a_role_that_is_neither_superuser_nor_bypassrls_is_enforced(pg: AsyncEngine) -> None:
    assert await _reason(pg, "tuvl_rls_plain") is None


async def test_a_bypassrls_role_ignores_the_policies(pg: AsyncEngine) -> None:
    assert await _reason(pg, "tuvl_rls_bypass") == "it has BYPASSRLS"


async def test_the_connecting_role_is_judged_by_its_own_attributes(pg: AsyncEngine) -> None:
    async with pg.begin() as conn:
        superuser = (
            await conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
        ).scalar_one()
    assert await _reason(pg) == ("it is a superuser" if superuser else None)


async def test_the_boot_check_says_nothing_outside_multi_tenant_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tuvl.core.bootstrap.LOCKED_DEPLOYMENT_MODE", "single_tenant")
    assert await rls.warn_if_role_bypasses_rls() is None


async def test_the_boot_check_reports_a_role_that_bypasses_in_multi_tenant_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tuvl.core.bootstrap.LOCKED_DEPLOYMENT_MODE", "multi_tenant")

    async def superuser() -> str:
        return "it is a superuser"

    monkeypatch.setattr(rls, "primary_role_bypass_reason", superuser)
    assert await rls.warn_if_role_bypasses_rls() == "it is a superuser"
