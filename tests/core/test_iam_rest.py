"""REST IAM surface (tuvl.core.auth.router over tuvl.core.auth.iam_service).

Covers login timing equalization, generic login-failure wording, logout and
refresh revocation, federation path-traversal sanitization and admin CRUD,
bootstrap conflict, user/role CRUD + assignment, and admin/auth rejection —
the REST adapter is the only transport, and the Insight UI talks to it
directly.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth import router as auth_router
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY
from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole
from tuvl.core.auth.schemas import TokenUser
from tuvl.core.auth.token import mint_token

pytestmark = pytest.mark.asyncio

ADMIN_SCOPE = "iam:admin"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admin_token() -> str:
    return mint_token(str(uuid.uuid4()), [], [ADMIN_SCOPE], ttl_seconds=300)


@pytest.fixture(autouse=True)
def _clean_blacklist():
    token_blacklist._memory.clear()
    yield
    token_blacklist._memory.clear()


async def _make_iam_engine():
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
    return engine


def _patch_iam_engine(monkeypatch, engine) -> None:
    """Point ``get_primary_rw_session()`` (used by ``router._get_iam_session``)
    at *engine*."""

    async def _fake_get_primary_rw_session():
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    import tuvl.core.datasources.postgres as pg

    monkeypatch.setattr(pg, "get_primary_rw_session", _fake_get_primary_rw_session)


@pytest.fixture
async def iam_db(monkeypatch):
    engine = await _make_iam_engine()
    _patch_iam_engine(monkeypatch, engine)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def rest_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(auth_router.iam_router)
    return app


async def _rest(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


# ── Login ───────────────────────────────────────────────────────────────────


async def test_login_timing_equalizer_invoked_for_nonexistent_user(iam_db, rest_app, monkeypatch):
    calls: list[str] = []
    orig = iam_service.averify_password

    async def _spy(password, hashed):
        calls.append(hashed)
        return await orig(password, hashed)

    monkeypatch.setattr(iam_service, "averify_password", _spy)

    resp = await _rest(
        rest_app, "POST", "/auth/token", data={"username": "ghost@example.com", "password": "x"}
    )
    assert resp.status_code == 401
    assert calls, "login must run a bcrypt comparison even for a missing user"
    assert calls[-1] == iam_service.DUMMY_PW_HASH


async def test_login_uses_real_hash_for_existing_user_wrong_password(iam_db, rest_app, monkeypatch):
    async with AsyncSession(iam_db, expire_on_commit=False) as session:
        user, _ = await iam_service.create_user(
            session, email="realhash@example.com", password="correct-horse", is_active=True
        )
        real_hash = user.hashed_password

    calls: list[str] = []
    orig = iam_service.averify_password

    async def _spy(password, hashed):
        calls.append(hashed)
        return await orig(password, hashed)

    monkeypatch.setattr(iam_service, "averify_password", _spy)

    resp = await _rest(
        rest_app,
        "POST",
        "/auth/token",
        data={"username": "realhash@example.com", "password": "wrong"},
    )
    assert resp.status_code == 401
    assert calls[-1] == real_hash


async def test_login_failure_generic_message(iam_db, rest_app):
    resp = await _rest(
        rest_app, "POST", "/auth/token", data={"username": "nobody@example.com", "password": "x"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password."
    assert resp.headers.get("www-authenticate") == "Bearer"


# ── Logout / refresh / me ───────────────────────────────────────────────────


async def test_logout_revokes_token(iam_db, rest_app):
    token = mint_token(str(uuid.uuid4()), [], ["whatever"], ttl_seconds=120)

    resp = await _rest(rest_app, "POST", "/auth/logout", headers=_auth(token))
    assert resp.status_code == 204

    resp = await _rest(rest_app, "GET", "/auth/me", headers=_auth(token))
    assert resp.status_code == 401


async def test_refresh_happy_path_revokes_old_and_rejects_inactive_user(iam_db, rest_app):
    async with AsyncSession(iam_db, expire_on_commit=False) as session:
        user, _ = await iam_service.create_user(
            session, email="refresh@example.com", password="pw123456", is_active=True
        )
        old_token = await iam_service.build_token_for_user(session, user)

    resp = await _rest(rest_app, "POST", "/auth/refresh", headers=_auth(old_token))
    assert resp.status_code == 200, resp.text
    new_token = resp.json()["access_token"]
    assert new_token and new_token != old_token

    resp = await _rest(rest_app, "GET", "/auth/me", headers=_auth(old_token))
    assert resp.status_code == 401

    async with AsyncSession(iam_db, expire_on_commit=False) as session:
        await iam_service.update_user(session, user.id, is_active=False)

    # deactivation ends the user's sessions, so the token itself is refused …
    resp = await _rest(rest_app, "POST", "/auth/refresh", headers=_auth(new_token))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Session ended — sign in again."
    # … and refresh still checks the account for a token minted afterwards
    late = mint_token(str(user.id), [], [], ttl_seconds=300)
    resp = await _rest(rest_app, "POST", "/auth/refresh", headers=_auth(late))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "User not found or inactive."


async def test_me_returns_identity_groups_and_scopes(iam_db, rest_app):
    async with AsyncSession(iam_db, expire_on_commit=False) as session:
        user, _ = await iam_service.create_user(
            session, email="me@example.com", password="pw123456", is_active=True
        )
        role, _ = await iam_service.create_role(session, name="viewer", scopes=["thing:read"])
        await iam_service.assign_role(session, user.id, role.id)
        token = await iam_service.build_token_for_user(session, user)

    resp = await _rest(rest_app, "GET", "/auth/me", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(user.id)
    assert body["groups"] == ["viewer"]
    assert body["scopes"] == ["thing:read"]


async def test_me_rejects_missing_token(rest_app):
    resp = await _rest(rest_app, "GET", "/auth/me")
    assert resp.status_code == 401


# ── Federation admin ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_federation_dir(tmp_path, monkeypatch):
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))
    snapshot = dict(FEDERATION_REGISTRY)
    FEDERATION_REGISTRY.clear()
    try:
        yield tmp_path
    finally:
        FEDERATION_REGISTRY.clear()
        FEDERATION_REGISTRY.update(snapshot)


async def test_federation_path_traversal_rejected(isolated_federation_dir):
    admin_user = TokenUser(user_id="admin-1", scopes=[ADMIN_SCOPE])

    with pytest.raises(HTTPException) as exc:
        await auth_router.get_federation_provider("../../etc", admin_user)
    assert exc.value.status_code == 400
    assert "Invalid provider name" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        await auth_router.save_federation_provider(
            "../../etc", {"kind": "FederationProvider"}, admin_user
        )
    assert exc.value.status_code == 400


async def test_federation_admin_crud_round_trip(isolated_federation_dir):
    admin_user = TokenUser(user_id="admin-1", scopes=[ADMIN_SCOPE])
    body = {
        "kind": "FederationProvider",
        "metadata": {"name": "acme"},
        "spec": {"provider": "google", "client_id": "cid", "client_secret": "shh"},
    }

    await auth_router.save_federation_provider("acme", body, admin_user)
    assert await auth_router.list_federation_providers(admin_user) == ["acme"]

    fetched = await auth_router.get_federation_provider("acme", admin_user)
    assert fetched["spec"]["client_secret"] == "***"

    await auth_router.delete_federation_provider("acme", admin_user)
    with pytest.raises(HTTPException) as exc:
        await auth_router.get_federation_provider("acme", admin_user)
    assert exc.value.status_code == 404


# ── Bootstrap ───────────────────────────────────────────────────────────────


async def test_bootstrap_happy_path_then_conflict(iam_db, rest_app):
    resp = await _rest(
        rest_app,
        "POST",
        "/auth/bootstrap",
        json={"email": "root@example.com", "password": "s3cret-pw"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]

    resp = await _rest(
        rest_app,
        "POST",
        "/auth/bootstrap",
        json={"email": "root2@example.com", "password": "s3cret-pw"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Bootstrap is only available when no users exist."


# ── Users / roles admin ─────────────────────────────────────────────────────


async def test_user_crud_happy_path_and_not_found(iam_db, rest_app):
    admin = _auth(_admin_token())

    resp = await _rest(
        rest_app,
        "POST",
        "/auth/admin/users",
        headers=admin,
        json={"email": "crud@example.com", "password": "pw123456", "is_active": True},
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["id"]

    resp = await _rest(
        rest_app,
        "POST",
        "/auth/admin/users",
        headers=admin,
        json={"email": "crud@example.com", "password": "pw123456"},
    )
    assert resp.status_code == 409

    resp = await _rest(rest_app, "GET", "/auth/admin/users", headers=admin)
    assert any(u["email"] == "crud@example.com" for u in resp.json())

    # An explicitly-empty string IS applied (clears the field); omitted fields
    # are left alone.
    resp = await _rest(
        rest_app,
        "PATCH",
        f"/auth/admin/users/{user_id}",
        headers=admin,
        json={"first_name": "", "is_active": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["first_name"] == ""
    assert resp.json()["is_active"] is False

    async with AsyncSession(iam_db, expire_on_commit=False) as session:
        db_user = await iam_service.get_user_by_id(session, uuid.UUID(user_id))
        assert db_user.first_name == ""
        assert db_user.hashed_password is not None

    resp = await _rest(rest_app, "DELETE", f"/auth/admin/users/{user_id}", headers=admin)
    assert resp.status_code == 204
    resp = await _rest(rest_app, "DELETE", f"/auth/admin/users/{user_id}", headers=admin)
    assert resp.status_code == 404
    resp = await _rest(rest_app, "PATCH", f"/auth/admin/users/{user_id}", headers=admin, json={})
    assert resp.status_code == 404


async def test_role_crud_and_assignment_happy_path_and_not_found(iam_db, rest_app):
    admin = _auth(_admin_token())

    resp = await _rest(
        rest_app,
        "POST",
        "/auth/admin/users",
        headers=admin,
        json={"email": "roleuser@example.com", "password": "pw123456"},
    )
    user_id = resp.json()["id"]

    resp = await _rest(
        rest_app,
        "POST",
        "/auth/admin/roles",
        headers=admin,
        json={"name": "hr_manager", "description": "HR", "scopes": ["req:write"]},
    )
    assert resp.status_code == 201, resp.text
    role = resp.json()
    assert role["scopes"] == ["req:write"]

    resp = await _rest(
        rest_app, "POST", "/auth/admin/roles", headers=admin, json={"name": "hr_manager"}
    )
    assert resp.status_code == 409

    resp = await _rest(
        rest_app,
        "PATCH",
        f"/auth/admin/roles/{role['id']}/scopes",
        headers=admin,
        json={"scopes": ["req:read", "cand:read"]},
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["scopes"]) == {"req:read", "cand:read"}

    resp = await _rest(rest_app, "GET", "/auth/admin/roles", headers=admin)
    role_out = next(r for r in resp.json() if r["name"] == "hr_manager")
    assert set(role_out["scopes"]) == {"req:read", "cand:read"}

    assign = f"/auth/admin/users/{user_id}/roles/{role['id']}"
    assert (await _rest(rest_app, "POST", assign, headers=admin)).status_code == 204
    assert (await _rest(rest_app, "POST", assign, headers=admin)).status_code == 204  # idempotent

    resp = await _rest(rest_app, "GET", "/auth/admin/users", headers=admin)
    u_out = next(u for u in resp.json() if u["id"] == user_id)
    assert u_out["roles"] == ["hr_manager"]

    assert (await _rest(rest_app, "DELETE", assign, headers=admin)).status_code == 204
    assert (await _rest(rest_app, "DELETE", assign, headers=admin)).status_code == 404

    resp = await _rest(
        rest_app, "POST", f"/auth/admin/users/{uuid.uuid4()}/roles/{role['id']}", headers=admin
    )
    assert resp.status_code == 404

    role_path = f"/auth/admin/roles/{role['id']}"
    assert (await _rest(rest_app, "DELETE", role_path, headers=admin)).status_code == 204
    assert (await _rest(rest_app, "DELETE", role_path, headers=admin)).status_code == 404


async def test_admin_endpoints_reject_non_admin_token(iam_db, rest_app):
    non_admin = mint_token(str(uuid.uuid4()), [], ["thing:read"], ttl_seconds=60)
    resp = await _rest(rest_app, "GET", "/auth/admin/users", headers=_auth(non_admin))
    assert resp.status_code == 403


# ── End to end over HTTP ────────────────────────────────────────────────────


async def test_live_boot_rest_bootstrap_login_authed_logout_rejected(monkeypatch):
    """REST: /auth/bootstrap → /auth/token → authed /auth/me → /auth/logout →
    token now rejected — driven over real ASGI/HTTP, not a direct call."""
    engine = await _make_iam_engine()
    _patch_iam_engine(monkeypatch, engine)
    try:
        app = FastAPI()
        app.include_router(auth_router.router)
        app.include_router(auth_router.iam_router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/auth/bootstrap",
                json={"email": "live-rest@example.com", "password": "s3cret-pw-1"},
            )
            assert resp.status_code == 200, resp.text
            boot_token = resp.json()["access_token"]

            resp = await client.post(
                "/auth/token",
                data={"username": "live-rest@example.com", "password": "s3cret-pw-1"},
            )
            assert resp.status_code == 200, resp.text
            token = resp.json()["access_token"]

            resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["scopes"] == ["iam:admin"]

            resp = await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 204

            resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401

            # The bootstrap token is independent of the login token — still valid.
            resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {boot_token}"})
            assert resp.status_code == 200
    finally:
        await engine.dispose()
