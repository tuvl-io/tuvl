"""The user and role lists make a fixed number of queries however long they are, and can be paged."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth import router as auth_router
from tuvl.core.auth.models import (
    IAMRevokedToken,
    IAMRole,
    IAMRoleScope,
    IAMSessionEnd,
    IAMUser,
    IAMUserRole,
)
from tuvl.core.auth.token import mint_token

pytestmark = pytest.mark.asyncio

TABLES = [IAMUser, IAMRole, IAMUserRole, IAMRoleScope, IAMSessionEnd, IAMRevokedToken]
N = 30


@pytest.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=[m.__table__ for m in TABLES])
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    async with AsyncSession(engine, expire_on_commit=False) as s:
        roles = [
            (
                await iam_service.create_role(
                    s, name=f"role{r}", scopes=[f"scope:{r}a", f"scope:{r}b"]
                )
            )[0]
            for r in range(3)
        ]
        for n in range(N):
            user, _ = await iam_service.create_user(s, email=f"user{n:02d}@example.com")
            for role in roles[: n % 3 + 1]:
                await iam_service.assign_role(s, user.id, role.id)
        yield s


@pytest.fixture
def statements(engine):
    seen: list[str] = []

    def record(conn, cursor, statement, *_):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_list_users_asks_the_database_a_fixed_number_of_times(session, statements) -> None:
    statements.clear()
    pairs = await iam_service.list_users(session)
    assert len(statements) <= 2
    assert [u.email for u, _ in pairs] == sorted(u.email for u, _ in pairs)
    roles_by_email = {u.email: sorted(r) for u, r in pairs}
    assert roles_by_email["user00@example.com"] == ["role0"]
    assert roles_by_email["user02@example.com"] == ["role0", "role1", "role2"]
    assert len(pairs) == N


async def test_list_users_can_be_paged(session) -> None:
    page = await iam_service.list_users(session, limit=10, offset=5)
    assert [u.email for u, _ in page] == [f"user{n:02d}@example.com" for n in range(5, 15)]
    assert len(await iam_service.list_users(session, offset=25)) == 5
    assert len(await iam_service.list_users(session)) == N  # no limit: everything, as before


async def test_list_roles_asks_the_database_a_fixed_number_of_times(session, statements) -> None:
    statements.clear()
    pairs = await iam_service.list_roles(session)
    assert len(statements) <= 2
    assert [r.name for r, _ in pairs] == ["role0", "role1", "role2"]
    assert sorted(pairs[1][1]) == ["scope:1a", "scope:1b"]
    assert len(await iam_service.list_roles(session, limit=1, offset=1)) == 1


async def test_role_summaries_ask_the_database_a_fixed_number_of_times(session, statements) -> None:
    statements.clear()
    summaries = await iam_service.list_role_summaries(session)
    assert len(statements) <= 3
    assert [(r.name, n) for r, _, n in summaries] == [("role0", N), ("role1", 20), ("role2", 10)]
    assert sorted(summaries[0][1]) == ["scope:0a", "scope:0b"]


async def test_rest_lists_accept_limit_and_offset(engine, session, monkeypatch) -> None:
    async def _session():
        s = AsyncSession(engine, expire_on_commit=False)
        try:
            yield s
        finally:
            await s.close()

    import tuvl.core.datasources.postgres as pg

    monkeypatch.setattr(pg, "get_primary_rw_session", _session)
    app = FastAPI()
    app.include_router(auth_router.iam_router)
    headers = {"Authorization": "Bearer " + mint_token("admin", [], ["iam:admin"], ttl_seconds=300)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        everyone = (await client.get("/auth/admin/users", headers=headers)).json()
        assert len(everyone) == N
        page = (await client.get("/auth/admin/users?limit=2&offset=1", headers=headers)).json()
        assert [u["email"] for u in page] == ["user01@example.com", "user02@example.com"]
        assert (await client.get("/auth/admin/users?limit=0", headers=headers)).status_code == 422
        assert (await client.get("/auth/admin/users?offset=-1", headers=headers)).status_code == 422
        roles = (await client.get("/auth/admin/roles?limit=2", headers=headers)).json()
        assert [r["name"] for r in roles] == ["role0", "role1"]
