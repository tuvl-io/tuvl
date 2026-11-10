"""IAM changes take effect at once: deleting users and roles that are in use, and
ending the sessions of anyone whose access just shrank (found by the 2.0 IAM audit)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import router as auth_router
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole
from tuvl.core.auth.token import mint_token

pytestmark = pytest.mark.asyncio

# minted at import: must outlive a full run of the suite, which takes ~10 minutes on a loaded runner
_TTL = 24 * 3600

ADMIN = {
    "Authorization": f"Bearer {mint_token(str(uuid.uuid4()), [], ['iam:admin'], ttl_seconds=_TTL)}"
}

# the user↔role link table as databases created before the cascade have it
_LEGACY_LINKS = """
CREATE TABLE tuvl_system_iam_user_roles (
    user_id CHAR(32) NOT NULL REFERENCES tuvl_system_iam_users (id),
    role_id CHAR(32) NOT NULL REFERENCES tuvl_system_iam_roles (id),
    PRIMARY KEY (user_id, role_id)
)"""


@pytest.fixture(autouse=True)
def _clean_store():
    token_blacklist._memory.clear()
    token_blacklist._sessions.clear()
    yield
    token_blacklist._memory.clear()
    token_blacklist._sessions.clear()


async def _engine(*, legacy_links: bool):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fks(dbapi_conn, _record):  # SQLite enforces foreign keys only when asked
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    tables = [IAMUser.__table__, IAMRole.__table__, IAMRoleScope.__table__]
    if not legacy_links:
        tables.append(IAMUserRole.__table__)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=tables)
        if legacy_links:
            await conn.execute(text(_LEGACY_LINKS))
    return engine


def _use(monkeypatch, engine) -> None:
    async def _session():
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    import tuvl.core.datasources.postgres as pg

    monkeypatch.setattr(pg, "get_primary_rw_session", _session)


@pytest.fixture
async def app(monkeypatch, request):
    engine = await _engine(legacy_links=getattr(request, "param", False))
    _use(monkeypatch, engine)
    api = FastAPI()
    api.include_router(auth_router.router)
    api.include_router(auth_router.iam_router)
    yield api
    await engine.dispose()


async def _call(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


async def _user(app: FastAPI, email: str, password: str = "pw-123456") -> str:
    resp = await _call(
        app, "POST", "/auth/admin/users", headers=ADMIN, json={"email": email, "password": password}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _role(app: FastAPI, name: str, scopes: list[str]) -> str:
    resp = await _call(
        app, "POST", "/auth/admin/roles", headers=ADMIN, json={"name": name, "scopes": scopes}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _assign(app: FastAPI, user_id: str, role_id: str) -> None:
    assert (
        await _call(app, "POST", f"/auth/admin/users/{user_id}/roles/{role_id}", headers=ADMIN)
    ).status_code == 204


async def _login(app: FastAPI, email: str, password: str = "pw-123456"):
    await asyncio.sleep(0.005)  # tokens and session ends are ms-stamped
    resp = await _call(app, "POST", "/auth/token", data={"username": email, "password": password})
    return resp.json().get("access_token") if resp.status_code == 200 else None


async def _me(app: FastAPI, token: str) -> int:
    return (
        await _call(app, "GET", "/auth/me", headers={"Authorization": f"Bearer {token}"})
    ).status_code


@pytest.mark.parametrize(
    "app", [True, False], indirect=True, ids=["legacy-links", "cascading-links"]
)
async def test_users_and_roles_in_use_can_be_deleted(app):
    alice = await _user(app, "alice@example.com")
    bob = await _user(app, "bob@example.com")
    reviewers = await _role(app, "reviewers", ["approvals:decide"])
    approvers = await _role(app, "approvers", [])
    for u in (alice, bob):
        await _assign(app, u, reviewers)
    await _assign(app, alice, approvers)

    assert (
        await _call(app, "DELETE", f"/auth/admin/users/{alice}", headers=ADMIN)
    ).status_code == 204
    assert (
        await _call(app, "DELETE", f"/auth/admin/roles/{reviewers}", headers=ADMIN)
    ).status_code == 204
    users = (await _call(app, "GET", "/auth/admin/users", headers=ADMIN)).json()
    assert [(u["email"], u["roles"]) for u in users] == [("bob@example.com", [])]
    assert [
        r["name"] for r in (await _call(app, "GET", "/auth/admin/roles", headers=ADMIN)).json()
    ] == ["approvers"]


async def test_narrowing_access_ends_existing_sessions(app):
    user = await _user(app, "rita@example.com")
    role = await _role(app, "reviewers", ["approvals:decide", "runs:read"])
    await _assign(app, user, role)

    token = await _login(app, "rita@example.com")
    assert await _me(app, token) == 200

    # adding scopes ends nothing (they arrive at the next sign-in)
    await asyncio.sleep(0.005)
    resp = await _call(
        app,
        "PATCH",
        f"/auth/admin/roles/{role}/scopes",
        headers=ADMIN,
        json={"scopes": ["approvals:decide", "runs:read", "runs:control"]},
    )
    assert resp.status_code == 200 and await _me(app, token) == 200

    # removing one does
    await _call(
        app,
        "PATCH",
        f"/auth/admin/roles/{role}/scopes",
        headers=ADMIN,
        json={"scopes": ["approvals:decide"]},
    )
    assert await _me(app, token) == 401
    token = await _login(app, "rita@example.com")
    assert await _me(app, token) == 200

    # so does taking the role away …
    await _call(app, "DELETE", f"/auth/admin/users/{user}/roles/{role}", headers=ADMIN)
    assert await _me(app, token) == 401
    token = await _login(app, "rita@example.com")
    me = (await _call(app, "GET", "/auth/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert me["groups"] == []

    # … a password change …
    await _call(
        app, "PATCH", f"/auth/admin/users/{user}", headers=ADMIN, json={"password": "pw-new-654321"}
    )
    assert await _me(app, token) == 401
    assert await _login(app, "rita@example.com") is None
    token = await _login(app, "rita@example.com", "pw-new-654321")
    assert await _me(app, token) == 200

    # … and deactivation, which also refuses new sign-ins and refreshes
    await _call(app, "PATCH", f"/auth/admin/users/{user}", headers=ADMIN, json={"is_active": False})
    assert await _me(app, token) == 401
    assert await _login(app, "rita@example.com", "pw-new-654321") is None


async def test_deleting_a_role_or_user_ends_their_sessions(app):
    sam = await _user(app, "sam@example.com")
    tina = await _user(app, "tina@example.com")
    role = await _role(app, "echo_runner", ["echo:run"])
    await _assign(app, sam, role)
    sam_token = await _login(app, "sam@example.com")
    tina_token = await _login(app, "tina@example.com")

    await asyncio.sleep(0.005)
    await _call(app, "DELETE", f"/auth/admin/roles/{role}", headers=ADMIN)
    assert await _me(app, sam_token) == 401
    assert await _me(app, tina_token) == 200  # not a member: untouched

    await _call(app, "DELETE", f"/auth/admin/users/{tina}", headers=ADMIN)
    assert await _me(app, tina_token) == 401


async def test_tokens_issued_after_a_session_end_are_accepted(app):
    user = await _user(app, "old@example.com")
    before = mint_token(user, [], [], ttl_seconds=300)
    await asyncio.sleep(0.005)
    await token_blacklist.end_sessions(user, 300)
    await asyncio.sleep(0.005)
    after = mint_token(user, [], [], ttl_seconds=300)
    assert await _me(app, before) == 401
    assert await _me(app, after) == 200


async def test_a_token_without_an_issue_time_is_refused(app):
    import jwt

    from tuvl.core.auth.keys import get_key_manager

    user = await _user(app, "legacy@example.com")
    no_iat = jwt.encode(
        {"iss": "tuvl", "sub": user, "groups": [], "scopes": []},
        get_key_manager().private_key,
        algorithm="EdDSA",
    )
    assert await _me(app, no_iat) == 401  # iat is required: a session end can always be applied
