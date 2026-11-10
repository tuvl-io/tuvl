"""IAM service rules behind the `tuvl iam` CLI: disabled roles, the last-administrator guard,
single-scope grant/revoke, role deletion with members, and break-glass recovery."""

from __future__ import annotations

import uuid

import jwt
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.iam_service import ConflictError, NotFoundError
from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_blacklist():
    token_blacklist._memory.clear()
    yield
    token_blacklist._memory.clear()


@pytest.fixture
async def session():
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
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


def _claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


async def _admin(session, email="root@example.com"):
    """An active user holding an active role with iam:admin."""
    token = await iam_service.bootstrap(session, email=email, password="pw-1234567")
    user = await iam_service.get_user_by_login(session, email)
    role = await iam_service.get_role_by_name(session, "superadmin")
    return user, role, token


async def test_a_disabled_role_adds_no_group_and_no_scope_to_the_token(session):
    user, admin_role, _ = await _admin(session)
    role, _ = await iam_service.create_role(session, name="recruiter", scopes=["candidate:read"])
    await iam_service.assign_role(session, user.id, role.id)

    claims = _claims(await iam_service.build_token_for_user(session, user))
    assert "recruiter" in claims["groups"] and "candidate:read" in claims["scopes"]

    await iam_service.set_role_active(session, role.id, False)
    claims = _claims(await iam_service.build_token_for_user(session, user))
    assert "recruiter" not in claims["groups"] and "candidate:read" not in claims["scopes"]
    assert "iam:admin" in claims["scopes"]

    await iam_service.set_role_active(session, role.id, True)
    claims = _claims(await iam_service.build_token_for_user(session, user))
    assert "candidate:read" in claims["scopes"]


async def test_the_last_administrator_role_cannot_be_disabled(session):
    _, admin_role, _ = await _admin(session)
    role_id = admin_role.id  # a refused change rolls back and expires the objects
    with pytest.raises(ConflictError, match="no active administrator"):
        await iam_service.set_role_active(session, role_id, False)
    reloaded = await iam_service.get_role_by_id(session, role_id)
    assert reloaded.is_active is True


async def test_a_second_administrator_lets_the_first_role_go(session):
    _, admin_role, _ = await _admin(session)
    second = await iam_service.create_user(session, email="two@example.com", password="pw-1234567")
    backup, _ = await iam_service.create_role(session, name="backup", scopes=["iam:admin"])
    await iam_service.assign_role(session, second[0].id, backup.id)

    await iam_service.set_role_active(session, admin_role.id, False)
    assert (await iam_service.get_role_by_id(session, admin_role.id)).is_active is False
    # now the backup is the only one left
    with pytest.raises(ConflictError):
        await iam_service.set_role_active(session, backup.id, False)


async def test_the_last_administrator_cannot_lose_the_scope_the_role_or_the_account(session):
    user, admin_role, _ = await _admin(session)
    user_id, role_id = user.id, admin_role.id
    with pytest.raises(ConflictError):
        await iam_service.revoke_scope(session, role_id, "iam:admin")
    with pytest.raises(ConflictError):
        await iam_service.update_role_scopes(session, role_id, [])
    with pytest.raises(ConflictError):
        await iam_service.revoke_role(session, user_id, role_id)
    with pytest.raises(ConflictError):
        await iam_service.delete_role(session, role_id)
    with pytest.raises(ConflictError):
        await iam_service.update_user(session, user_id, is_active=False)
    with pytest.raises(ConflictError):
        await iam_service.delete_user(session, user_id)
    # nothing above took effect
    assert await iam_service.active_admin_count(session) == 1
    assert "iam:admin" in await iam_service.load_role_scopes(session, role_id)


async def test_a_database_without_an_administrator_is_not_locked(session):
    role, _ = await iam_service.create_role(session, name="viewer", scopes=["x:read"])
    await iam_service.set_role_active(session, role.id, False)
    await iam_service.delete_role(session, role.id)
    assert await iam_service.get_role_by_name(session, "viewer") is None


async def test_grant_scope_is_idempotent_and_revoke_scope_needs_the_scope(session):
    await _admin(session)
    role, _ = await iam_service.create_role(session, name="ops")
    await iam_service.grant_scope(session, role.id, "runs:read")
    _, scopes = await iam_service.grant_scope(session, role.id, "runs:read")
    assert scopes == ["runs:read"]
    await iam_service.revoke_scope(session, role.id, "runs:read")
    with pytest.raises(NotFoundError):
        await iam_service.revoke_scope(session, role.id, "runs:read")


async def test_delete_role_can_require_it_to_be_empty(session):
    user, _, _ = await _admin(session)
    role, _ = await iam_service.create_role(session, name="ops")
    await iam_service.assign_role(session, user.id, role.id)
    with pytest.raises(ConflictError, match="member"):
        await iam_service.delete_role(session, role.id, require_empty=True)
    await iam_service.delete_role(session, role.id)  # cascade stays the default
    assert await iam_service.get_role_by_name(session, "ops") is None


async def test_bootstrap_reuses_a_seeded_superadmin_role(session):
    seeded, _ = await iam_service.create_role(session, name="superadmin", scopes=["runs:read"])
    await iam_service.bootstrap(session, email="root@example.com", password="pw-1234567")
    roles = [r for r, _ in await iam_service.list_roles(session) if r.name == "superadmin"]
    assert len(roles) == 1 and roles[0].id == seeded.id
    scopes = await iam_service.load_role_scopes(session, seeded.id)
    assert {"runs:read", "iam:admin"} <= set(scopes)


async def test_reset_admin_recovers_a_locked_out_database(session):
    user, _, _ = await _admin(session)
    user_id = user.id
    await session.exec(IAMRole.__table__.update().values(is_active=False))
    await session.exec(IAMUser.__table__.update().values(is_active=False))
    await session.commit()
    session.expire_all()
    assert await iam_service.active_admin_count(session) == 0

    recovered, roles = await iam_service.reset_admin(
        session, email="root@example.com", password="new-pw-1234567"
    )
    assert recovered.id == user_id and recovered.is_active and "superadmin" in roles
    assert await iam_service.active_admin_count(session) == 1
    token = await iam_service.login(
        session, login_str="root@example.com", password="new-pw-1234567"
    )
    assert "iam:admin" in _claims(token)["scopes"]


async def test_reset_admin_creates_the_account_when_it_is_missing(session):
    user, roles = await iam_service.reset_admin(
        session, email=f"{uuid.uuid4().hex[:6]}@example.com", password="pw-1234567"
    )
    assert user.is_active and roles == ["superadmin"]
    assert await iam_service.active_admin_count(session) == 1
