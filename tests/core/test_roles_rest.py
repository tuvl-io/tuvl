"""Role membership over HTTP: the ``roles`` API group (tuvl.core.auth.roles_router).

Runtime needs only: list roles, find users, add and remove a member, disable or re-enable a role.
A manager may not hand out access they do not hold, and nothing here creates roles, changes scopes
or creates users.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole
from tuvl.core.auth.roles_router import roles_router
from tuvl.core.auth.token import mint_token

pytestmark = pytest.mark.asyncio


def _token(*scopes: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + mint_token(str(uuid.uuid4()), [], list(scopes), ttl_seconds=300)
    }


MANAGER = ("roles:manage", "reports:read", "reports:write")


@pytest.fixture(autouse=True)
def _clean_blacklist():
    token_blacklist._memory.clear()
    yield
    token_blacklist._memory.clear()


@pytest.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            SQLModel.metadata.create_all,
            tables=[
                IAMUser.__table__,
                IAMRole.__table__,
                IAMUserRole.__table__,
                IAMRoleScope.__table__,
            ],
        )

    async def _session():
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    import tuvl.core.datasources.postgres as pg

    monkeypatch.setattr(pg, "get_primary_rw_session", _session)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(roles_router)
    return app


async def _call(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


async def _role(engine, name: str, scopes: list[str]) -> uuid.UUID:
    async with AsyncSession(engine, expire_on_commit=False) as s:
        role, _ = await iam_service.create_role(s, name=name, scopes=scopes)
        return role.id


async def _user(engine, email: str, **kw) -> uuid.UUID:
    async with AsyncSession(engine, expire_on_commit=False) as s:
        user, _ = await iam_service.create_user(s, email=email, password="pw-123456", **kw)
        return user.id


async def _assign(engine, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await iam_service.assign_role(s, user_id, role_id)


# ── access ──────────────────────────────────────────────────────────────────


async def test_the_group_needs_roles_manage(app, db):
    assert (await _call(app, "GET", "/auth/roles")).status_code == 401
    assert (
        await _call(app, "GET", "/auth/roles", headers=_token("reports:read"))
    ).status_code == 403
    assert (
        await _call(app, "GET", "/auth/roles", headers=_token("roles:manage"))
    ).status_code == 200
    assert (await _call(app, "GET", "/auth/roles", headers=_token("iam:admin"))).status_code == 200


async def test_the_group_exposes_no_development_time_operations(app):
    ops = {(m, r.path) for r in roles_router.routes for m in r.methods}
    assert ops == {
        ("GET", "/auth/roles"),
        ("GET", "/auth/roles/candidates"),
        ("GET", "/auth/roles/{role_id}/members"),
        ("PUT", "/auth/roles/{role_id}/members/{user_id}"),
        ("DELETE", "/auth/roles/{role_id}/members/{user_id}"),
        ("PATCH", "/auth/roles/{role_id}"),
    }


# ── listing ─────────────────────────────────────────────────────────────────


async def test_roles_are_listed_with_scopes_state_and_member_count(app, db):
    reviewers = await _role(db, "reviewers", ["reports:read"])
    await _role(db, "writers", ["reports:write", "reports:read"])
    await _assign(db, await _user(db, "a@x.io"), reviewers)
    body = (await _call(app, "GET", "/auth/roles", headers=_token(*MANAGER))).json()
    assert [r["name"] for r in body] == ["reviewers", "writers"]
    assert body[0]["member_count"] == 1 and body[0]["is_active"] is True
    assert body[1]["scopes"] == ["reports:read", "reports:write"] and body[1]["member_count"] == 0


async def test_members_and_candidates_page_by_email(app, db):
    role = await _role(db, "reviewers", ["reports:read"])
    ids = [await _user(db, f"u{i}@x.io", first_name=f"User{i}") for i in range(5)]
    for uid in ids[:3]:
        await _assign(db, uid, role)
    h = _token(*MANAGER)

    first = (await _call(app, "GET", f"/auth/roles/{role}/members?limit=2", headers=h)).json()
    assert [p["email"] for p in first["items"]] == ["u0@x.io", "u1@x.io"] and first["next_cursor"]
    last = (
        await _call(
            app,
            "GET",
            f"/auth/roles/{role}/members?limit=2&cursor={first['next_cursor']}",
            headers=h,
        )
    ).json()
    assert [p["email"] for p in last["items"]] == ["u2@x.io"] and last["next_cursor"] is None

    hit = (await _call(app, "GET", "/auth/roles/candidates?q=USER4", headers=h)).json()
    assert [p["email"] for p in hit["items"]] == ["u4@x.io"]
    assert set(hit["items"][0]) == {"id", "email", "first_name", "last_name", "is_active"}
    assert len((await _call(app, "GET", "/auth/roles/candidates", headers=h)).json()["items"]) == 5
    assert (
        await _call(app, "GET", f"/auth/roles/{uuid.uuid4()}/members", headers=h)
    ).status_code == 404


# ── membership ──────────────────────────────────────────────────────────────


async def test_add_is_idempotent_and_remove_ends_the_users_sessions(app, db):
    role, user = await _role(db, "reviewers", ["reports:read"]), await _user(db, "a@x.io")
    h = _token(*MANAGER)
    url = f"/auth/roles/{role}/members/{user}"
    assert (await _call(app, "PUT", url, headers=h)).status_code == 204
    assert (await _call(app, "PUT", url, headers=h)).status_code == 204
    assert (await _call(app, "GET", "/auth/roles", headers=h)).json()[0]["member_count"] == 1
    assert await token_blacklist.sessions_ended_at(str(user)) is None
    assert (await _call(app, "DELETE", url, headers=h)).status_code == 204
    assert await token_blacklist.sessions_ended_at(str(user)) is not None
    assert (await _call(app, "DELETE", url, headers=h)).status_code == 404
    assert (
        await _call(app, "PUT", f"/auth/roles/{role}/members/{uuid.uuid4()}", headers=h)
    ).status_code == 404
    assert (
        await _call(app, "PUT", f"/auth/roles/{uuid.uuid4()}/members/{user}", headers=h)
    ).status_code == 404


async def test_a_manager_cannot_hand_out_access_they_do_not_hold(app, db):
    beyond = await _role(db, "approvers", ["reports:read", "payments:approve"])
    admins = await _role(db, "admins", ["iam:admin"])
    managers = await _role(db, "managers", ["roles:manage"])
    user = await _user(db, "a@x.io")
    h = _token(*MANAGER)
    for role in (beyond, admins, managers):
        assert (
            await _call(app, "PUT", f"/auth/roles/{role}/members/{user}", headers=h)
        ).status_code == 403
        assert (
            await _call(app, "PATCH", f"/auth/roles/{role}", json={"is_active": False}, headers=h)
        ).status_code == 403
    detail = (await _call(app, "PUT", f"/auth/roles/{beyond}/members/{user}", headers=h)).json()[
        "detail"
    ]
    assert "payments:approve" in detail
    assert (await _call(app, "GET", "/auth/roles", headers=h)).json()[0]["member_count"] == 0
    # an administrator may change any of them
    assert (
        await _call(app, "PUT", f"/auth/roles/{beyond}/members/{user}", headers=_token("iam:admin"))
    ).status_code == 204


async def test_the_last_administrator_cannot_be_removed_or_disabled(app, db):
    admins = await _role(db, "admins", ["iam:admin"])
    admin = await _user(db, "root@x.io")
    await _assign(db, admin, admins)
    h = _token("iam:admin")
    assert (
        await _call(app, "DELETE", f"/auth/roles/{admins}/members/{admin}", headers=h)
    ).status_code == 409
    assert (
        await _call(app, "PATCH", f"/auth/roles/{admins}", json={"is_active": False}, headers=h)
    ).status_code == 409
    assert (await _call(app, "GET", "/auth/roles", headers=h)).json()[0]["member_count"] == 1


# ── disable / enable ────────────────────────────────────────────────────────


async def test_a_role_can_be_disabled_and_enabled_and_keeps_its_members(app, db):
    role, user = await _role(db, "reviewers", ["reports:read"]), await _user(db, "a@x.io")
    await _assign(db, user, role)
    h = _token(*MANAGER)
    off = await _call(app, "PATCH", f"/auth/roles/{role}", json={"is_active": False}, headers=h)
    assert off.status_code == 200
    assert off.json()["is_active"] is False and off.json()["member_count"] == 1
    assert off.json()["scopes"] == ["reports:read"]
    async with AsyncSession(db) as s:
        assert [
            r.name
            for r in await iam_service.load_user_roles(
                s, await s.get(IAMUser, user), active_only=True
            )
        ] == []
    assert await token_blacklist.sessions_ended_at(str(user)) is not None
    on = await _call(app, "PATCH", f"/auth/roles/{role}", json={"is_active": True}, headers=h)
    assert on.json()["is_active"] is True
    assert (await _call(app, "PATCH", f"/auth/roles/{role}", json={}, headers=h)).status_code == 422
    assert (
        await _call(
            app, "PATCH", f"/auth/roles/{uuid.uuid4()}", json={"is_active": False}, headers=h
        )
    ).status_code == 404
