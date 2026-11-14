"""A user sets their own password from a one-time link: issued by an admin, works once, expires, ends open sessions."""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import router as auth_router
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.models import (
    IAMPasswordToken,
    IAMRole,
    IAMRoleScope,
    IAMUser,
    IAMUserRole,
)
from tuvl.core.auth.token import mint_token

pytestmark = pytest.mark.asyncio

ADMIN = {
    "Authorization": f"Bearer {mint_token(str(uuid.uuid4()), [], ['iam:admin'], ttl_seconds=24 * 3600)}"
}
NOT_ADMIN = {
    "Authorization": f"Bearer {mint_token(str(uuid.uuid4()), [], ['runs:read'], ttl_seconds=24 * 3600)}"
}


@pytest.fixture(autouse=True)
def _clean_store():
    token_blacklist._memory.clear()
    token_blacklist._sessions.clear()
    yield
    token_blacklist._memory.clear()
    token_blacklist._sessions.clear()


@pytest.fixture
async def env(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fks(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    tables = [
        IAMUser.__table__,
        IAMRole.__table__,
        IAMRoleScope.__table__,
        IAMUserRole.__table__,
        IAMPasswordToken.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=tables)

    async def _session():
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    import tuvl.core.datasources.postgres as pg

    monkeypatch.setattr(pg, "get_primary_rw_session", _session)
    api = FastAPI()
    api.include_router(auth_router.router)
    api.include_router(auth_router.iam_router)
    yield api, engine
    await engine.dispose()


async def _call(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


async def _user(app: FastAPI, email: str, password: str | None = None, **extra) -> str:
    body = {"email": email, **({"password": password} if password else {}), **extra}
    resp = await _call(app, "POST", "/auth/admin/users", headers=ADMIN, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _invite(app: FastAPI, user_id: str, **body) -> str:
    resp = await _call(app, "POST", f"/auth/admin/users/{user_id}/invite", headers=ADMIN, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def _login(app: FastAPI, email: str, password: str):
    return await _call(app, "POST", "/auth/token", data={"username": email, "password": password})


async def test_a_user_with_no_password_sets_one_from_the_link_and_signs_in(env):
    app, _ = env
    uid = await _user(app, "vendor@acme.test")
    assert (await _login(app, "vendor@acme.test", "anything-123")).status_code == 401
    token = await _invite(app, uid)
    done = await _call(
        app, "POST", "/auth/password/set", json={"token": token, "password": "A-good-pass-1"}
    )
    assert done.status_code == 204, done.text
    assert (await _login(app, "vendor@acme.test", "A-good-pass-1")).status_code == 200


async def test_the_link_works_only_once(env):
    app, _ = env
    uid = await _user(app, "once@acme.test")
    token = await _invite(app, uid)
    body = {"token": token, "password": "First-pass-1"}
    assert (await _call(app, "POST", "/auth/password/set", json=body)).status_code == 204
    again = await _call(
        app, "POST", "/auth/password/set", json={"token": token, "password": "Second-pass-1"}
    )
    assert again.status_code == 400
    assert (await _login(app, "once@acme.test", "Second-pass-1")).status_code == 401
    assert (await _login(app, "once@acme.test", "First-pass-1")).status_code == 200


async def test_a_new_link_replaces_the_old_one(env):
    app, _ = env
    uid = await _user(app, "twice@acme.test")
    old = await _invite(app, uid)
    new = await _invite(app, uid)
    assert (
        await _call(
            app, "POST", "/auth/password/set", json={"token": old, "password": "Old-pass-123"}
        )
    ).status_code == 400
    assert (
        await _call(
            app, "POST", "/auth/password/set", json={"token": new, "password": "New-pass-123"}
        )
    ).status_code == 204


async def test_an_expired_link_is_refused(env):
    app, engine = env
    uid = await _user(app, "late@acme.test")
    token = await _invite(app, uid)
    async with AsyncSession(engine) as s:
        await s.exec(update(IAMPasswordToken).values(expires_at=datetime.datetime(2020, 1, 1)))
        await s.commit()
    resp = await _call(
        app, "POST", "/auth/password/set", json={"token": token, "password": "Late-pass-123"}
    )
    assert resp.status_code == 400


async def test_a_guessed_token_is_refused_and_nothing_changes(env):
    app, _ = env
    uid = await _user(app, "guess@acme.test", "Keep-this-1")
    await _invite(app, uid)
    resp = await _call(
        app, "POST", "/auth/password/set", json={"token": "x" * 43, "password": "Hacker-pass-1"}
    )
    assert resp.status_code == 400
    assert (await _login(app, "guess@acme.test", "Keep-this-1")).status_code == 200


async def test_the_password_rules_apply_and_the_link_survives_a_refusal(env):
    app, _ = env
    uid = await _user(app, "rules@acme.test")
    token = await _invite(app, uid)
    short = await _call(
        app, "POST", "/auth/password/set", json={"token": token, "password": "short"}
    )
    assert short.status_code == 422
    long = await _call(
        app, "POST", "/auth/password/set", json={"token": token, "password": "p" * 80}
    )
    assert long.status_code == 422
    ok = await _call(
        app, "POST", "/auth/password/set", json={"token": token, "password": "Long-enough-1"}
    )
    assert ok.status_code == 204


async def test_setting_a_password_ends_the_sessions_the_user_already_held(env):
    app, _ = env
    uid = await _user(app, "held@acme.test", "Old-password-1")
    old = (await _login(app, "held@acme.test", "Old-password-1")).json()["access_token"]
    assert (
        await _call(app, "GET", "/auth/me", headers={"Authorization": f"Bearer {old}"})
    ).status_code == 200
    token = await _invite(app, uid)
    assert (
        await _call(
            app, "POST", "/auth/password/set", json={"token": token, "password": "New-password-1"}
        )
    ).status_code == 204
    assert (
        await _call(app, "GET", "/auth/me", headers={"Authorization": f"Bearer {old}"})
    ).status_code == 401


async def test_only_an_admin_can_make_a_link_and_an_inactive_user_cannot_have_one(env):
    app, _ = env
    uid = await _user(app, "who@acme.test")
    denied = await _call(app, "POST", f"/auth/admin/users/{uid}/invite", headers=NOT_ADMIN)
    assert denied.status_code == 403
    assert (
        await _call(app, "POST", f"/auth/admin/users/{uuid.uuid4()}/invite", headers=ADMIN)
    ).status_code == 404
    off = await _user(app, "off@acme.test", is_active=False)
    assert (
        await _call(app, "POST", f"/auth/admin/users/{off}/invite", headers=ADMIN)
    ).status_code == 422
    bad = await _call(
        app, "POST", f"/auth/admin/users/{uid}/invite", headers=ADMIN, json={"ttl_hours": 0}
    )
    assert bad.status_code == 422


async def test_the_token_is_stored_only_as_a_hash(env):
    app, engine = env
    uid = await _user(app, "hash@acme.test")
    token = await _invite(app, uid)
    async with AsyncSession(engine) as s:
        from sqlmodel import select

        rows = (await s.exec(select(IAMPasswordToken))).all()
    assert len(rows) == 1 and rows[0].token_hash != token and token not in rows[0].token_hash
