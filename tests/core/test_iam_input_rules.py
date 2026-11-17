"""One rule for every path that sets a password, names a role or a scope, or stores an email."""

from __future__ import annotations

import uuid

import jwt
import pytest
import sqlalchemy as sa
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth import router as auth_router
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.iam_service import ConflictError, ValidationError
from tuvl.core.auth.models import (
    IAMLoginAttempt,
    IAMPasswordToken,
    IAMRevokedToken,
    IAMRole,
    IAMRoleScope,
    IAMSessionEnd,
    IAMUser,
    IAMUserRole,
)
from tuvl.core.auth.seed import RoleSeedEntry, parse_role_seed, seed_iam
from tuvl.core.auth.token import mint_token
from tuvl.core.bootstrap import SystemRegistry

pytestmark = pytest.mark.asyncio

GOOD = "correct-horse-1"
SHORT = "1234567"
LONG = "x" * 73
MULTIBYTE = "é" * 37  # 37 characters, 74 bytes: bcrypt would cut it at 72
BAD_NAMES = ["has space", "orders:*", "a,b", "", "name\n"]
TABLES = [
    IAMUser,
    IAMRole,
    IAMUserRole,
    IAMRoleScope,
    IAMPasswordToken,
    IAMSessionEnd,
    IAMRevokedToken,
    IAMLoginAttempt,
    SystemRegistry,
]


@pytest.fixture(autouse=True)
def _clean_blacklist():
    token_blacklist.reset_cache()
    yield
    token_blacklist.reset_cache()


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
        yield s


@pytest.fixture
def rest(engine, monkeypatch):
    async def _session():
        s = AsyncSession(engine, expire_on_commit=False)
        try:
            yield s
        finally:
            await s.close()

    import tuvl.core.datasources.postgres as pg

    monkeypatch.setattr(pg, "get_primary_rw_session", _session)
    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(auth_router.iam_router)

    async def call(method: str, path: str, **kwargs):
        headers = {
            "Authorization": "Bearer "
            + mint_token(str(uuid.uuid4()), [], ["iam:admin"], ttl_seconds=300)
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return call


def _claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


# ── passwords ──────────────────────────────────────────────────────────────────


async def test_the_password_rule_is_one_function() -> None:
    iam_service.validate_password("12345678")
    iam_service.validate_password("x" * 72)
    with pytest.raises(ValidationError, match="at least 8 characters"):
        iam_service.validate_password(SHORT)
    for too_long in (LONG, MULTIBYTE):
        with pytest.raises(ValidationError, match="at most 72 bytes"):
            iam_service.validate_password(too_long)


async def _create(session, password):
    await iam_service.create_user(session, email="new@example.com", password=password)


async def _update(session, password):
    user, _ = await iam_service.create_user(session, email="old@example.com", password=GOOD)
    await iam_service.update_user(session, user.id, password=password)


async def _bootstrap(session, password):
    await iam_service.bootstrap(session, email="root@example.com", password=password)


async def _reset_admin(session, password):
    await iam_service.reset_admin(session, email="root@example.com", password=password)


async def _link(session, password):
    user, _ = await iam_service.create_user(session, email="old@example.com", password=GOOD)
    raw, _ = await iam_service.issue_password_token(session, user.id)
    await iam_service.set_password_with_token(session, raw, password)


PASSWORD_PATHS = {
    "create": _create,
    "update": _update,
    "bootstrap": _bootstrap,
    "reset-admin": _reset_admin,
    "link": _link,
}


@pytest.mark.parametrize("path", PASSWORD_PATHS)
@pytest.mark.parametrize("bad", [SHORT, LONG, MULTIBYTE], ids=["short", "long", "multibyte"])
async def test_every_path_that_sets_a_password_applies_the_rule(session, path, bad) -> None:
    with pytest.raises(ValidationError):
        await PASSWORD_PATHS[path](session, bad)
    if path in ("create", "bootstrap", "reset-admin"):
        assert (await session.exec(select(IAMUser))).first() is None


async def test_a_user_without_a_password_is_still_allowed(session) -> None:
    user, _ = await iam_service.create_user(session, email="invited@example.com")
    assert user.hashed_password is None


async def test_rest_answers_422_for_a_bad_password(rest) -> None:
    created = await rest(
        "POST", "/auth/admin/users", json={"email": "a@example.com", "password": "x"}
    )
    assert created.status_code == 422 and "at least 8" in created.json()["detail"]
    ok = await rest("POST", "/auth/admin/users", json={"email": "b@example.com", "password": GOOD})
    assert ok.status_code == 201
    patched = await rest("PATCH", f"/auth/admin/users/{ok.json()['id']}", json={"password": "x"})
    assert patched.status_code == 422
    boot = await rest("POST", "/auth/bootstrap", json={"email": "r@example.com", "password": "x"})
    assert boot.status_code == 422


async def test_a_short_bootstrap_password_fails_the_seed_and_writes_nothing(session) -> None:
    roles = [RoleSeedEntry("viewer", None, ("runs:read",))]
    with pytest.raises(ValueError, match="TUVL_BOOTSTRAP_ADMIN_PASSWORD"):
        await seed_iam(session, roles=roles, admin_email="a@example.com", admin_password="short")
    await session.rollback()
    assert (await session.exec(select(IAMRole))).first() is None
    assert (await session.exec(select(IAMUser))).first() is None


# ── role and scope names ───────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", BAD_NAMES)
async def test_a_role_name_is_checked_where_it_is_written(session, bad) -> None:
    with pytest.raises(ValidationError):
        await iam_service.create_role(session, name=bad)
    assert (await session.exec(select(IAMRole))).first() is None


@pytest.mark.parametrize("bad", BAD_NAMES)
async def test_a_scope_is_checked_on_every_path(session, bad) -> None:
    role, _ = await iam_service.create_role(session, name="ops")
    with pytest.raises(ValidationError):
        await iam_service.create_role(session, name="other", scopes=["fine:read", bad])
    with pytest.raises(ValidationError):
        await iam_service.update_role_scopes(session, role.id, ["fine:read", bad])
    with pytest.raises(ValidationError):
        await iam_service.grant_scope(session, role.id, bad)
    with pytest.raises(ValidationError):
        await iam_service.bootstrap(session, email="r@example.com", password=GOOD, admin_scope=bad)
    assert await iam_service.load_role_scopes(session, role.id) == []
    assert await iam_service.get_role_by_name(session, "other") is None
    assert await iam_service.get_user_by_login(session, "r@example.com") is None


async def test_rest_answers_422_for_a_bad_role_or_scope(rest) -> None:
    bad_role = await rest("POST", "/auth/admin/roles", json={"name": "has space"})
    assert bad_role.status_code == 422
    bad_scope = await rest(
        "POST", "/auth/admin/roles", json={"name": "ops", "scopes": ["orders:*"]}
    )
    assert bad_scope.status_code == 422
    made = await rest("POST", "/auth/admin/roles", json={"name": "ops"})
    assert made.status_code == 201
    replaced = await rest(
        "PATCH", f"/auth/admin/roles/{made.json()['id']}/scopes", json={"scopes": ["orders:*"]}
    )
    assert replaced.status_code == 422


async def test_a_role_seed_with_a_scope_a_token_cannot_carry_is_refused() -> None:
    document = {"spec": {"roles": [{"name": "ops", "scopes": ["orders:*"]}]}}
    with pytest.raises(ValueError, match="scopes"):
        parse_role_seed(document)
    assert parse_role_seed({"spec": {"roles": [{"name": "ops", "scopes": ["orders:read"]}]}})


async def test_a_bad_stored_scope_does_not_stop_the_login(session) -> None:
    user, _ = await iam_service.create_user(session, email="u@example.com", password=GOOD)
    good, _ = await iam_service.create_role(session, name="good", scopes=["orders:read"])
    odd = IAMRole(name="bad name")
    session.add(odd)
    await session.flush()
    session.add(IAMRoleScope(role_id=odd.id, scope_name="orders:*"))
    session.add(IAMRoleScope(role_id=good.id, scope_name="has space"))
    session.add(IAMUserRole(user_id=user.id, role_id=good.id))
    session.add(IAMUserRole(user_id=user.id, role_id=odd.id))
    await session.commit()

    with structlog.testing.capture_logs() as logs:
        token = await iam_service.login(session, login_str="u@example.com", password=GOOD)

    claims = _claims(token)
    assert claims["groups"] == ["good"] and claims["scopes"] == ["orders:read"]
    skipped = [e for e in logs if e["event"] == "auth.token.invalid_entry_skipped"]
    assert {(e["kind"], e["name"]) for e in skipped} == {
        ("group", "bad name"),
        ("scope", "orders:*"),
        ("scope", "has space"),
    }
    assert all(e["log_level"] == "error" for e in skipped)


async def test_a_bad_scope_already_stored_can_still_be_revoked(session) -> None:
    role, _ = await iam_service.create_role(session, name="ops")
    session.add(IAMRoleScope(role_id=role.id, scope_name="orders:*"))
    await session.commit()
    _, scopes = await iam_service.revoke_scope(session, role.id, "orders:*")
    assert scopes == []


# ── email ──────────────────────────────────────────────────────────────────────


async def test_an_email_is_stored_trimmed_and_lower_case(session) -> None:
    user, _ = await iam_service.create_user(session, email="  Ann@Example.COM ", password=GOOD)
    assert user.email == "ann@example.com"


async def test_lookup_and_login_ignore_the_case_of_the_email(session) -> None:
    await iam_service.create_user(session, email="Ann@Example.com", password=GOOD)
    for spelling in ("ann@example.com", "ANN@EXAMPLE.COM", " Ann@example.com "):
        assert await iam_service.get_user_by_login(session, spelling) is not None, spelling
    token = await iam_service.login(session, login_str="ANN@example.com", password=GOOD)
    assert token


async def test_two_spellings_of_one_email_are_one_user(session) -> None:
    await iam_service.create_user(session, email="ann@example.com", password=GOOD)
    with pytest.raises(ConflictError):
        await iam_service.create_user(session, email="Ann@Example.com", password=GOOD)


async def test_a_row_stored_before_the_rule_is_still_found(session) -> None:
    user, _ = await iam_service.create_user(session, email="legacy@example.com", password=GOOD)
    await session.exec(
        sa.update(IAMUser.__table__)
        .where(IAMUser.__table__.c.id == user.id)
        .values(email="Legacy@Example.com")
    )
    await session.commit()
    found = await iam_service.get_user_by_login(session, "legacy@example.com")
    assert found is not None and found.id == user.id


async def test_bootstrap_and_reset_admin_use_the_same_spelling(session) -> None:
    await iam_service.bootstrap(session, email="Root@Example.com", password=GOOD)
    user, _ = await iam_service.reset_admin(
        session, email="ROOT@example.com", password="other-pass-1"
    )
    assert user.email == "root@example.com"
    assert len((await session.exec(select(IAMUser))).all()) == 1


# ── what is logged ─────────────────────────────────────────────────────────────


async def test_a_database_error_is_logged_without_the_row(session, monkeypatch) -> None:
    secret = "$2b$12$thehashofthepassword"
    orig = Exception(
        'null value in column "email" violates not-null constraint\n'
        f"DETAIL:  Failing row contains (1, {secret})."
    )
    failure = IntegrityError("INSERT INTO users ...", {"hashed_password": secret}, orig)

    async def boom() -> None:
        raise failure

    monkeypatch.setattr(session, "commit", boom)
    with structlog.testing.capture_logs() as logs, pytest.raises(iam_service.PersistenceError):
        await iam_service.create_user(session, email="a@example.com", password=GOOD)

    errors = [e for e in logs if e["event"] == "iam.create_user.db_error"]
    assert len(errors) == 1
    assert secret not in repr(logs) and "DETAIL" not in repr(logs)
    assert not errors[0].get("exc_info")  # a traceback prints the statement's parameters
