"""Transport-neutral IAM service layer.

This module holds the CORE logic for every IAM operation — bootstrap, login,
token refresh/logout, user/role CRUD, and federation-provider administration —
extracted from the hardened REST implementation (``tuvl.core.auth.router``).

``tuvl.core.auth.router`` is a THIN adapter over these functions: it parses
request input, calls into this module, and maps the result (or a raised
:class:`IamError`) to an HTTP status. No FastAPI types appear here —
functions take a plain DB session plus plain Python inputs and return plain
results, or raise one of the typed domain errors below.

Security-relevant behavior that lives HERE:

- ``login`` always performs one bcrypt comparison, even for a missing or
  passwordless account (``DUMMY_PW_HASH``), so response timing cannot be used
  to enumerate which emails/phone numbers exist.
- ``safe_federation_path`` is the single, regex-anchored path-traversal guard
  used by the federation-provider admin endpoints.
- Federation-provider file I/O (list/get/save/delete + registry reload) is
  offloaded to the threadpool so the event loop never blocks on disk access.
- ``revoke_token`` is the single blacklist-revocation call used by both
  logout and refresh.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

import yaml as _yaml
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import distinct, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.crypto import aget_password_hash, averify_password, get_password_hash
from tuvl.core.auth.federation_loader import load_all_federation_providers
from tuvl.core.auth.models import (
    IAMPasswordToken,
    IAMRole,
    IAMRoleScope,
    IAMUser,
    IAMUserRole,
)
from tuvl.core.auth.token import mint_token
from tuvl.core.config import settings
from tuvl.core.logger import logger

# ── Domain error taxonomy ─────────────────────────────────────────────────────
#
# The REST adapter maps these to HTTP status codes:
#
#   Domain error           | HTTP
#   -----------------------+-------------------------------------------
#   NotFoundError          | 404
#   ConflictError          | 409
#   AuthenticationError    | 401 (+ headers per call-site, see router.py)
#   ForbiddenError         | 403
#   ValidationError        | 400
#   ParseError             | 422
#   PersistenceError       | 500 (``.detail``)


class IamError(Exception):
    """Base class for all domain errors raised by the IAM service layer."""


class NotFoundError(IamError):
    """The requested user, role, assignment, or federation provider does not exist."""


class ConflictError(IamError):
    """A uniqueness or state conflict — duplicate email/role name, or a
    bootstrap attempt after the first admin account already exists."""


class AuthenticationError(IamError):
    """Credentials or the caller's identity could not be authenticated."""


class ForbiddenError(IamError):
    """The caller is authenticated but may not change this role (HTTP 403)."""


class ValidationError(IamError):
    """Malformed caller input, e.g. an unsafe federation provider name."""


class ParseError(IamError):
    """A stored resource (federation provider YAML) could not be parsed."""


class PersistenceError(IamError):
    """An unexpected database error occurred.

    ``detail`` is safe to return to a caller: the driver's own message can carry
    the failing row (including the password hash), so it is logged, never surfaced.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<UNSET>"


#: Sentinel distinguishing "field not provided" from "field explicitly set to
#: None/empty" in :func:`update_user`.
UNSET: Any = _Unset()

# A throwaway bcrypt hash used to equalise login timing when the account is
# missing or passwordless (prevents user-enumeration via response time).
# Computed once at import time, exactly as it was in the original REST router.
DUMMY_PW_HASH = get_password_hash("tuvl-login-timing-equaliser")


# ── Shared queries ─────────────────────────────────────────────────────────────


async def get_user_by_login(session: AsyncSession, login_str: str) -> IAMUser | None:
    result = await session.exec(
        select(IAMUser).where(or_(IAMUser.email == login_str, IAMUser.phone_number == login_str))
    )
    return result.first()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> IAMUser | None:
    result = await session.exec(select(IAMUser).where(IAMUser.id == user_id))
    return result.first()


async def get_role_by_id(session: AsyncSession, role_id: uuid.UUID) -> IAMRole | None:
    result = await session.exec(select(IAMRole).where(IAMRole.id == role_id))
    return result.first()


async def get_role_by_name(session: AsyncSession, name: str) -> IAMRole | None:
    result = await session.exec(select(IAMRole).where(IAMRole.name == name))
    return result.first()


async def load_user_roles(
    session: AsyncSession, user: IAMUser, *, active_only: bool = False
) -> list[IAMRole]:
    stmt = (
        select(IAMRole)
        .join(IAMUserRole, col(IAMUserRole.role_id) == IAMRole.id)
        .where(IAMUserRole.user_id == user.id)
    )
    if active_only:
        stmt = stmt.where(col(IAMRole.is_active).is_(True))
    result = await session.exec(stmt)
    return list(result.all())


async def load_role_scopes_for_roles(session: AsyncSession, role_ids: list[uuid.UUID]) -> list[str]:
    """Deduplicated scope names across *role_ids* — used when minting a token."""
    if not role_ids:
        return []
    result = await session.exec(
        select(IAMRoleScope.scope_name).where(col(IAMRoleScope.role_id).in_(role_ids)).distinct()
    )
    return list(result.all())


async def load_role_scopes(session: AsyncSession, role_id: uuid.UUID) -> list[str]:
    """Scope names for a single role — used for display (list/get), no dedup."""
    result = await session.exec(
        select(IAMRoleScope.scope_name).where(IAMRoleScope.role_id == role_id)
    )
    return list(result.all())


async def build_token_for_user(session: AsyncSession, user: IAMUser) -> str:
    """Assemble roles + scopes then mint an access token for *user*; a disabled role adds neither."""
    roles = await load_user_roles(session, user, active_only=True)
    role_names = [r.name for r in roles]
    scopes = await load_role_scopes_for_roles(session, [r.id for r in roles])
    ttl = settings.tuvl_token_ttl_seconds or None
    return mint_token(str(user.id), role_names, scopes, ttl_seconds=ttl)


async def revoke_token(raw_token: str) -> None:
    """Blacklist *raw_token* using the standard TTL-or-fallback rule.

    Shared by logout and refresh, on both transports.
    """
    ttl = settings.tuvl_token_ttl_seconds or 86400
    await token_blacklist.revoke(raw_token, ttl_seconds=ttl)


# ── Keeping an administrator ───────────────────────────────────────────────────

ADMIN_SCOPE = "iam:admin"
#: Lets a caller change who holds a role (the ``roles`` API group); it grants no other administration.
ROLE_MANAGE_SCOPE = "roles:manage"
#: Roles carrying either of these change hands only through an administrator (or ``tuvl iam``).
_PROTECTED_SCOPES = frozenset({ADMIN_SCOPE, ROLE_MANAGE_SCOPE})
SUPERADMIN_ROLE = "superadmin"


async def active_admin_count(session: AsyncSession) -> int:
    """Active users who hold an active role that grants ``iam:admin``."""
    result = await session.exec(
        select(func.count(distinct(col(IAMUser.id))))
        .join(IAMUserRole, col(IAMUserRole.user_id) == IAMUser.id)
        .join(IAMRole, col(IAMRole.id) == IAMUserRole.role_id)
        .join(IAMRoleScope, col(IAMRoleScope.role_id) == IAMRole.id)
        .where(
            col(IAMUser.is_active).is_(True),
            col(IAMRole.is_active).is_(True),
            IAMRoleScope.scope_name == ADMIN_SCOPE,
        )
    )
    return int(result.one())


async def _keep_an_admin(session: AsyncSession, before: int) -> None:
    """Undo the flushed change when it removed the last active administrator.

    Only enforced when there was one: a database that never had an administrator is not locked.
    """
    if before and not await active_admin_count(session):
        await session.rollback()
        raise ConflictError(
            f"That change would leave no active administrator "
            f"(a user holding an active role with {ADMIN_SCOPE})."
        )


# ── Token lifecycle ────────────────────────────────────────────────────────────


async def _ensure_superadmin_role(session: AsyncSession, scopes: list[str]) -> IAMRole:
    """The ``superadmin`` role, active and holding *scopes* (created when absent, e.g. not seeded)."""
    role = await get_role_by_name(session, SUPERADMIN_ROLE)
    if role is None:
        role = IAMRole(name=SUPERADMIN_ROLE, description="Full platform administrator")
        session.add(role)
        await session.flush()  # populate role.id
    role.is_active = True
    held = set(await load_role_scopes(session, role.id))
    for scope_name in scopes:
        if scope_name not in held:
            session.add(IAMRoleScope(role_id=role.id, scope_name=scope_name))
    await session.flush()
    return role


async def bootstrap(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    admin_scope: str = "iam:admin",
    phone_number: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> str:
    """Create the first superadmin user. Raises :class:`ConflictError` if any
    user already exists."""
    count_result = await session.exec(select(IAMUser))
    if count_result.first() is not None:
        raise ConflictError("Bootstrap is only available when no users exist.")

    initial_scopes = list({admin_scope, ADMIN_SCOPE})
    role = await _ensure_superadmin_role(session, initial_scopes)

    hashed_password = await aget_password_hash(password)
    user = IAMUser(
        email=email,
        phone_number=phone_number,
        first_name=first_name,
        last_name=last_name,
        hashed_password=hashed_password,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    session.add(IAMUserRole(user_id=user.id, role_id=role.id))
    await session.commit()

    logger.info("auth.iam.bootstrap", admin_email=email)
    return await build_token_for_user(session, user)


async def _verified_user(session: AsyncSession, *, login_str: str, password: str) -> IAMUser:
    """The active user these credentials belong to, or :class:`AuthenticationError`.

    The message never distinguishes "no such user" from "wrong password" (user-enumeration guard).
    """
    user = await get_user_by_login(session, login_str)

    # Constant-time-ish: always run one bcrypt verification, even for a missing
    # or passwordless account, so response timing can't be used to enumerate
    # which emails/phones exist.
    candidate_hash = user.hashed_password if (user and user.hashed_password) else DUMMY_PW_HASH
    password_ok = await averify_password(password, candidate_hash)

    if user is None or not user.is_active or user.hashed_password is None or not password_ok:
        raise AuthenticationError("Incorrect email or password.")
    return user


async def authenticate_admin(session: AsyncSession, *, login_str: str, password: str) -> IAMUser:
    """Verify credentials and require ``iam:admin`` through an active role (used by ``tuvl iam``)."""
    user = await _verified_user(session, login_str=login_str, password=password)
    roles = await load_user_roles(session, user, active_only=True)
    scopes = await load_role_scopes_for_roles(session, [r.id for r in roles])
    if ADMIN_SCOPE not in scopes:
        raise AuthenticationError(f"{user.email} does not hold {ADMIN_SCOPE}.")
    return user


async def login(session: AsyncSession, *, login_str: str, password: str) -> str:
    """Verify credentials and mint a token. Raises :class:`AuthenticationError`
    on any failure — the message never distinguishes "no such user" from
    "wrong password" (user-enumeration guard)."""
    user = await _verified_user(session, login_str=login_str, password=password)
    token = await build_token_for_user(session, user)
    logger.info("auth.login.password", email=user.email)
    return token


async def refresh(session: AsyncSession, *, user_id: str, raw_token: str) -> str:
    """Revoke *raw_token* and mint a fresh one for the user it belonged to.

    Raises :class:`AuthenticationError` if the user is gone or disabled.
    """
    user = await get_user_by_id(session, uuid.UUID(user_id))
    if user is None or not user.is_active:
        raise AuthenticationError("User not found or inactive.")

    await revoke_token(raw_token)

    new_token = await build_token_for_user(session, user)
    logger.info("auth.token.refreshed", email=user.email)
    return new_token


# ── Users ──────────────────────────────────────────────────────────────────────


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str | None = None,
    phone_number: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    is_active: bool = True,
) -> tuple[IAMUser, list[str]]:
    existing = await get_user_by_login(session, email)
    if existing is not None:
        raise ConflictError(f"User '{email}' already exists.")

    hashed = await aget_password_hash(password) if password else None
    user = IAMUser(
        email=email,
        phone_number=phone_number,
        first_name=first_name,
        last_name=last_name,
        hashed_password=hashed,
        is_active=is_active,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Unique-constraint violation — another request may have raced us, or the
        # pre-check SELECT ran in a stale snapshot.  Surface as a conflict either way.
        if "unique" in str(exc.orig).lower() or "duplicate" in str(exc.orig).lower():
            raise ConflictError(f"User '{email}' already exists (database constraint).") from None
        logger.error("iam.create_user.db_error", error=str(exc.orig), exc_info=True)
        raise PersistenceError("Database error while creating user.") from None
    await session.refresh(user)

    return user, []


async def list_users(session: AsyncSession) -> list[tuple[IAMUser, list[str]]]:
    result = await session.exec(select(IAMUser))
    out: list[tuple[IAMUser, list[str]]] = []
    for u in result.all():
        roles = await load_user_roles(session, u)
        out.append((u, [r.name for r in roles]))
    return out


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> tuple[IAMUser, list[str]]:
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    roles = await load_user_roles(session, user)
    return user, [r.name for r in roles]


async def end_sessions(user_ids: list[uuid.UUID]) -> None:
    """Refuse the tokens these users already hold: their access just shrank."""
    from tuvl.core.auth.blacklist import token_blacklist

    ttl = settings.tuvl_token_ttl_seconds or None
    for user_id in user_ids:
        await token_blacklist.end_sessions(str(user_id), ttl)


async def _role_members(session: AsyncSession, role_id: uuid.UUID) -> list[uuid.UUID]:
    links = await session.exec(select(IAMUserRole).where(IAMUserRole.role_id == role_id))
    return [link.user_id for link in links.all()]


async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    before = await active_admin_count(session)
    # role links first: databases created before the link FKs cascaded refuse the delete
    links = await session.exec(select(IAMUserRole).where(IAMUserRole.user_id == user_id))
    for link in links.all():
        await session.delete(link)
    await session.flush()
    await session.delete(user)
    await session.flush()
    await _keep_an_admin(session, before)
    await session.commit()
    await end_sessions([user_id])


async def update_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    password: Any = UNSET,
    is_active: Any = UNSET,
    first_name: Any = UNSET,
    last_name: Any = UNSET,
    phone_number: Any = UNSET,
) -> tuple[IAMUser, list[str]]:
    """Update only the fields whose caller-supplied value is not :data:`UNSET`
    and not ``None`` — matching the original REST ``is not None`` semantics
    exactly (including: an explicitly-empty string IS applied).
    """
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found.")

    before = await active_admin_count(session) if is_active is False else 0
    ends_sessions = False
    if password is not UNSET and password is not None:
        user.hashed_password = await aget_password_hash(password)
        ends_sessions = True
    if is_active is not UNSET and is_active is not None:
        ends_sessions = ends_sessions or (user.is_active and not is_active)
        user.is_active = is_active
    if first_name is not UNSET and first_name is not None:
        user.first_name = first_name
    if last_name is not UNSET and last_name is not None:
        user.last_name = last_name
    if phone_number is not UNSET and phone_number is not None:
        user.phone_number = phone_number

    await session.flush()
    await _keep_an_admin(session, before)
    await session.commit()
    await session.refresh(user)
    if ends_sessions:
        await end_sessions([user_id])
    roles = await load_user_roles(session, user)
    return user, [r.name for r in roles]


# ── Set-password links ─────────────────────────────────────────────────────────

PASSWORD_TOKEN_DEFAULT_HOURS = 72
PASSWORD_TOKEN_MAX_HOURS = 24 * 30
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72  # bcrypt ignores everything after 72 bytes


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


async def issue_password_token(
    session: AsyncSession, user_id: uuid.UUID, *, ttl_hours: int = PASSWORD_TOKEN_DEFAULT_HOURS
) -> tuple[str, datetime.datetime]:
    """A one-time token that lets the user set their own password; earlier unused ones stop working.

    Only its hash is stored, so the token cannot be read back: hand it to the user now.
    """
    if not 1 <= ttl_hours <= PASSWORD_TOKEN_MAX_HOURS:
        raise ValidationError(f"The link must last between 1 and {PASSWORD_TOKEN_MAX_HOURS} hours.")
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    if not user.is_active:
        raise ValidationError("An inactive user cannot be given a link.")
    old = await session.exec(
        select(IAMPasswordToken).where(
            IAMPasswordToken.user_id == user_id, col(IAMPasswordToken.used_at).is_(None)
        )
    )
    for row in old.all():
        await session.delete(row)
    raw = secrets.token_urlsafe(32)
    expires = _now() + datetime.timedelta(hours=ttl_hours)
    session.add(IAMPasswordToken(user_id=user_id, token_hash=_digest(raw), expires_at=expires))
    await session.commit()
    logger.info("auth.password_token.issued", user_id=str(user_id), ttl_hours=ttl_hours)
    return raw, expires


async def set_password_with_token(session: AsyncSession, raw_token: str, password: str) -> None:
    """Set the password of the user a token was issued for; the token works once."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValidationError(f"The password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password.encode()) > PASSWORD_MAX_BYTES:
        raise ValidationError(f"The password must be at most {PASSWORD_MAX_BYTES} bytes.")
    invalid = AuthenticationError("This link is not valid or has expired.")
    found = await session.exec(
        select(IAMPasswordToken).where(IAMPasswordToken.token_hash == _digest(raw_token))
    )
    row = found.first()
    if row is None or row.used_at is not None or row.expires_at < _now():
        raise invalid
    user = await get_user_by_id(session, row.user_id)
    if user is None or not user.is_active:
        raise invalid
    # claim it first: of two requests with the same token only one changes a row
    claimed = await session.exec(
        update(IAMPasswordToken)
        .where(col(IAMPasswordToken.id) == row.id, col(IAMPasswordToken.used_at).is_(None))
        .values(used_at=_now())
    )
    if claimed.rowcount != 1:
        raise invalid
    user.hashed_password = await aget_password_hash(password)
    await session.commit()
    await end_sessions([user.id])
    logger.info("auth.password_token.used", user_id=str(user.id))


# ── Roles ──────────────────────────────────────────────────────────────────────


async def create_role(
    session: AsyncSession,
    *,
    name: str,
    description: str | None = None,
    scopes: list[str] | None = None,
) -> tuple[IAMRole, list[str]]:
    scopes = scopes or []
    result = await session.exec(select(IAMRole).where(IAMRole.name == name))
    if result.first() is not None:
        raise ConflictError(f"Role '{name}' already exists.")

    role = IAMRole(name=name, description=description)
    session.add(role)
    await session.flush()

    for scope_name in scopes:
        session.add(IAMRoleScope(role_id=role.id, scope_name=scope_name))

    await session.commit()
    await session.refresh(role)
    return role, scopes


async def list_roles(session: AsyncSession) -> list[tuple[IAMRole, list[str]]]:
    result = await session.exec(select(IAMRole))
    out: list[tuple[IAMRole, list[str]]] = []
    for role in result.all():
        scopes = await load_role_scopes(session, role.id)
        out.append((role, scopes))
    return out


async def delete_role(
    session: AsyncSession, role_id: uuid.UUID, *, require_empty: bool = False
) -> None:
    """Delete a role with its scopes and memberships.

    With *require_empty* a role that still has members is refused instead of cascaded.
    """
    role = await get_role_by_id(session, role_id)
    if role is None:
        raise NotFoundError("Role not found.")
    members = await _role_members(session, role_id)
    if require_empty and members:
        raise ConflictError(
            f"Role '{role.name}' still has {len(members)} member(s); remove them or force the delete."
        )
    before = await active_admin_count(session)
    links = await session.exec(select(IAMUserRole).where(IAMUserRole.role_id == role_id))
    for link in links.all():
        await session.delete(link)
    scope_rows = await session.exec(select(IAMRoleScope).where(IAMRoleScope.role_id == role_id))
    for scope_row in scope_rows.all():
        await session.delete(scope_row)
    await session.flush()
    await session.delete(role)
    await session.flush()
    await _keep_an_admin(session, before)
    await session.commit()
    await end_sessions(members)


async def update_role_scopes(
    session: AsyncSession, role_id: uuid.UUID, scopes: list[str]
) -> tuple[IAMRole, list[str]]:
    """Atomically replace the full scope set for a role (delete-all then
    re-insert). An empty list removes all scopes."""
    role = await get_role_by_id(session, role_id)
    if role is None:
        raise NotFoundError("Role not found.")

    before = await active_admin_count(session)
    existing = await session.exec(select(IAMRoleScope).where(IAMRoleScope.role_id == role_id))
    rows = existing.all()
    removed = {row.scope_name for row in rows} - set(scopes)
    for scope_row in rows:
        await session.delete(scope_row)

    for scope_name in scopes:
        session.add(IAMRoleScope(role_id=role_id, scope_name=scope_name))

    members = await _role_members(session, role_id) if removed else []
    await session.flush()
    await _keep_an_admin(session, before)
    await session.commit()
    await session.refresh(role)
    await end_sessions(members)  # only a narrowed role ends sessions; added scopes wait for sign-in
    return role, scopes


async def grant_scope(
    session: AsyncSession, role_id: uuid.UUID, scope: str
) -> tuple[IAMRole, list[str]]:
    """Add one scope to a role (idempotent). Holders get it at their next sign-in."""
    role = await get_role_by_id(session, role_id)
    if role is None:
        raise NotFoundError("Role not found.")
    held = await load_role_scopes(session, role_id)
    if scope not in held:
        session.add(IAMRoleScope(role_id=role_id, scope_name=scope))
        await session.commit()
        held = [*held, scope]
    return role, held


async def revoke_scope(
    session: AsyncSession, role_id: uuid.UUID, scope: str
) -> tuple[IAMRole, list[str]]:
    """Remove one scope from a role; its holders' sessions end so the narrowing applies."""
    role = await get_role_by_id(session, role_id)
    if role is None:
        raise NotFoundError("Role not found.")
    before = await active_admin_count(session)
    rows = await session.exec(
        select(IAMRoleScope).where(
            IAMRoleScope.role_id == role_id, IAMRoleScope.scope_name == scope
        )
    )
    found = rows.all()
    if not found:
        raise NotFoundError(f"Role '{role.name}' does not grant '{scope}'.")
    for row in found:
        await session.delete(row)
    members = await _role_members(session, role_id)
    await session.flush()
    await _keep_an_admin(session, before)
    await session.commit()
    await end_sessions(members)
    return role, await load_role_scopes(session, role_id)


async def set_role_active(
    session: AsyncSession, role_id: uuid.UUID, active: bool
) -> tuple[IAMRole, list[str]]:
    """Enable or disable a role. A disabled role keeps its members and scopes but grants nothing."""
    role = await get_role_by_id(session, role_id)
    if role is None:
        raise NotFoundError("Role not found.")
    before = await active_admin_count(session)
    changed = role.is_active != active
    role.is_active = active
    members = await _role_members(session, role_id) if changed and not active else []
    await session.flush()
    await _keep_an_admin(session, before)
    await session.commit()
    await session.refresh(role)
    await end_sessions(members)
    return role, await load_role_scopes(session, role_id)


async def assign_role(session: AsyncSession, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    if await get_user_by_id(session, user_id) is None:
        raise NotFoundError("User not found.")
    if await get_role_by_id(session, role_id) is None:
        raise NotFoundError("Role not found.")

    existing = await session.exec(
        select(IAMUserRole).where(IAMUserRole.user_id == user_id, IAMUserRole.role_id == role_id)
    )
    if existing.first() is not None:
        return  # idempotent

    session.add(IAMUserRole(user_id=user_id, role_id=role_id))
    await session.commit()


async def revoke_role(session: AsyncSession, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    result = await session.exec(
        select(IAMUserRole).where(IAMUserRole.user_id == user_id, IAMUserRole.role_id == role_id)
    )
    link = result.first()
    if link is None:
        raise NotFoundError("Assignment not found.")
    before = await active_admin_count(session)
    await session.delete(link)
    await session.flush()
    await _keep_an_admin(session, before)
    await session.commit()
    await end_sessions([user_id])


# ── Role membership (the ``roles`` API group) ──────────────────────────────────


def check_can_manage_role(caller_scopes: list[str], role_name: str, role_scopes: list[str]) -> None:
    """Refuse a membership change that would let the caller hand out more than they hold.

    An administrator (``iam:admin``) may change any role. Anyone else may change a role only if every
    scope it grants is one they hold themselves, and never a role that carries ``iam:admin`` or
    ``roles:manage``; those change hands through an administrator or ``tuvl iam``. Without this a
    role manager could add themselves to a stronger role.
    """
    held = set(caller_scopes)
    if ADMIN_SCOPE in held:
        return
    granted = set(role_scopes)
    protected = sorted(granted & _PROTECTED_SCOPES)
    if protected:
        raise ForbiddenError(
            f"Role '{role_name}' carries {', '.join(protected)}; only an administrator can change who holds it."
        )
    beyond = sorted(granted - held)
    if beyond:
        raise ForbiddenError(
            f"Role '{role_name}' grants scopes you do not hold ({', '.join(beyond)}); "
            "you can only manage roles within your own access."
        )


async def list_role_summaries(
    session: AsyncSession, role_id: uuid.UUID | None = None
) -> list[tuple[IAMRole, list[str], int]]:
    """Roles with their scopes and member count, ordered by name (one role when *role_id* is given)."""
    stmt = select(IAMRole).order_by(col(IAMRole.name))
    if role_id is not None:
        stmt = stmt.where(IAMRole.id == role_id)
    roles = (await session.exec(stmt)).all()
    counts = {
        rid: int(n)
        for rid, n in (
            await session.exec(
                select(IAMUserRole.role_id, func.count()).group_by(col(IAMUserRole.role_id))
            )
        ).all()
    }
    return [(r, await load_role_scopes(session, r.id), counts.get(r.id, 0)) for r in roles]


async def find_users(
    session: AsyncSession,
    *,
    q: str | None = None,
    role_id: uuid.UUID | None = None,
    limit: int = 25,
    after: str | None = None,
) -> tuple[list[IAMUser], str | None]:
    """One page of users ordered by email: the members of *role_id*, or everyone, optionally matching *q*.

    *after* is the cursor a previous page returned (the last email). The second value is the
    cursor for the next page, ``None`` on the last one.
    """
    stmt = select(IAMUser)
    if role_id is not None:
        stmt = stmt.join(IAMUserRole, col(IAMUserRole.user_id) == IAMUser.id).where(
            IAMUserRole.role_id == role_id
        )
    if q:
        stmt = stmt.where(
            or_(
                col(IAMUser.email).icontains(q, autoescape=True),
                col(IAMUser.first_name).icontains(q, autoescape=True),
                col(IAMUser.last_name).icontains(q, autoescape=True),
            )
        )
    if after:
        stmt = stmt.where(col(IAMUser.email) > after)
    rows = list((await session.exec(stmt.order_by(col(IAMUser.email)).limit(limit + 1))).all())
    more = len(rows) > limit
    page = rows[:limit]
    return page, (page[-1].email if more and page else None)


async def reset_admin(
    session: AsyncSession, *, email: str, password: str
) -> tuple[IAMUser, list[str]]:
    """Break-glass recovery: make *email* an active administrator with *password*.

    Creates the user when absent, re-enables a disabled account, and makes sure the
    ``superadmin`` role exists, is active, grants ``iam:admin`` and is held by the user. Their
    sessions end. There is no authentication here: only code that already reaches the database
    (a shell in the cluster) can call it.
    """
    role = await _ensure_superadmin_role(session, [ADMIN_SCOPE])
    user = await get_user_by_login(session, email)
    hashed = await aget_password_hash(password)
    if user is None:
        user = IAMUser(email=email, hashed_password=hashed, is_active=True)
        session.add(user)
        await session.flush()
    else:
        user.hashed_password = hashed
        user.is_active = True
    held = await session.exec(
        select(IAMUserRole).where(IAMUserRole.user_id == user.id, IAMUserRole.role_id == role.id)
    )
    if held.first() is None:
        session.add(IAMUserRole(user_id=user.id, role_id=role.id))
    await session.commit()
    await session.refresh(user)
    await end_sessions([user.id])
    logger.warning("auth.iam.reset_admin", admin_email=email)
    roles = await load_user_roles(session, user)
    return user, [r.name for r in roles]


# ── Federation provider administration ─────────────────────────────────────────
#
# These manage the YAML files in the project's federation/ directory. All file
# I/O runs in the threadpool so neither transport's event loop blocks on disk
# access (previously true only on the REST path — see module docstring).

_FEDERATION_NAME_RE = re.compile(r"^[\w.\-]+$")


def safe_federation_path(name: str) -> Path:
    """Return the absolute path for federation/{name}.yaml, preventing path
    traversal. The single sanitizer used by both transports."""
    if not _FEDERATION_NAME_RE.match(name):
        raise ValidationError("Invalid provider name.")
    fed_dir = settings.dir_for("federation")
    resolved = (fed_dir / f"{name}.yaml").resolve()
    if not str(resolved).startswith(str(fed_dir.resolve())):
        raise ValidationError("Path traversal detected.")
    return resolved


async def list_federation_providers() -> list[str]:
    fed_dir = settings.dir_for("federation")
    if not fed_dir.exists():
        return []
    return await run_in_threadpool(lambda: sorted(f.stem for f in fed_dir.glob("*.yaml")))


async def get_federation_provider(name: str) -> dict:
    path = safe_federation_path(name)
    if not path.exists():
        raise NotFoundError(f"Provider '{name}' not found.")
    try:
        content = _yaml.safe_load(await run_in_threadpool(path.read_text))
    except _yaml.YAMLError as exc:
        raise ParseError(f"YAML parse error: {exc}") from exc

    # Return the raw YAML structure (with env-var references intact, not resolved)
    # but redact client_secret so the admin endpoint cannot be used to exfiltrate
    # OAuth credentials that have been resolved from environment variables.
    safe: dict = content or {}
    if isinstance(safe.get("spec"), dict) and "client_secret" in safe["spec"]:
        safe = copy.deepcopy(safe)
        safe["spec"]["client_secret"] = "***"
    return safe


async def save_federation_provider(name: str, body: dict) -> None:
    path = safe_federation_path(name)

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_yaml.dump(body, allow_unicode=True, sort_keys=False))

    await run_in_threadpool(_write)
    # Reload the in-memory registry so the new provider is immediately usable.
    await run_in_threadpool(load_all_federation_providers)
    logger.info("auth.federation.provider_saved", provider=name)


async def delete_federation_provider(name: str) -> None:
    path = safe_federation_path(name)
    if not path.exists():
        raise NotFoundError(f"Provider '{name}' not found.")
    await run_in_threadpool(path.unlink)
    # Reload the in-memory registry to remove the deleted provider.
    await run_in_threadpool(load_all_federation_providers)
    logger.info("auth.federation.provider_deleted", provider=name)
