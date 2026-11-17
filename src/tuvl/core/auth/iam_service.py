"""Transport-neutral IAM service layer.

This module holds the CORE logic for every IAM operation — bootstrap, login,
token refresh/logout, user/role CRUD, and federation-provider administration —
extracted from the hardened REST implementation (``tuvl.core.auth.router``).

``tuvl.core.auth.router`` and ``tuvl.core.auth.federation`` are THIN adapters over these functions: they
parse request input, call into this module, and map the result (or a raised
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
import math
import re
import secrets
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml as _yaml
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import case, delete, distinct, func, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.crypto import aget_password_hash, averify_password, get_password_hash
from tuvl.core.auth.federation_loader import load_all_federation_providers
from tuvl.core.auth.models import (
    IAMLoginAttempt,
    IAMPasswordToken,
    IAMRole,
    IAMRoleScope,
    IAMUser,
    IAMUserRole,
)
from tuvl.core.auth.schemas import SCOPE_NAME_RE
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
#   ValidationError        | 422 (400 on the federation admin routes)
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


class ThrottledError(AuthenticationError):
    """Too many failed sign-ins for this login or address: try again in ``retry_after`` seconds."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Too many failed attempts. Try again in {retry_after} seconds.")
        self.retry_after = retry_after


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


# ── Input rules ────────────────────────────────────────────────────────────────

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72  # bcrypt ignores everything after 72 bytes
#: Held by whoever creates the first administrator or seeds a fresh database, so two replicas booting
#: (or two bootstrap calls) cannot each find the database empty. One key for every replica of one database.
SEED_ADVISORY_KEY = 7_411_902_017


def validate_password(password: str) -> None:
    """The rule for every password a person sets: set-password links, admin-created users, the bootstrap
    and seeded administrators, ``reset-admin``. bcrypt would cut anything past 72 bytes, so two different
    long passwords could match."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValidationError(f"The password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password.encode()) > PASSWORD_MAX_BYTES:
        raise ValidationError(f"The password must be at most {PASSWORD_MAX_BYTES} bytes.")


def validate_scope_name(name: str) -> str:
    """*name* when a token can carry it; a scope the token minter would refuse is refused where it is written."""
    if not SCOPE_NAME_RE.fullmatch(name):
        raise ValidationError(
            f"{name!r} is not a valid scope: use letters, digits and _ : . - / only (no spaces or *)."
        )
    return name


def validate_role_name(name: str) -> str:
    """*name* when a token can carry it as a group."""
    if not SCOPE_NAME_RE.fullmatch(name):
        raise ValidationError(
            f"{name!r} is not a valid role name: use letters, digits and _ : . - / only (no spaces)."
        )
    return name


def normalize_email(email: str) -> str:
    """How an email is stored and compared: trimmed and lower case, so ``Ann@X.com`` and ``ann@x.com`` are one person."""
    return email.strip().lower()


def _first_line(exc: BaseException) -> str:
    """The driver's own first line (the constraint). The rest can carry the failing row, password hash included."""
    orig = getattr(exc, "orig", None) or exc
    return (str(orig).splitlines() or [""])[0]


async def _take_seed_lock(session: AsyncSession) -> None:
    """Serialise with every other writer of the first administrator, until this transaction ends (PostgreSQL)."""
    connection = await session.connection()
    if connection.dialect.name == "postgresql":
        await connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SEED_ADVISORY_KEY})


# ── Shared queries ─────────────────────────────────────────────────────────────


async def get_user_by_login(session: AsyncSession, login_str: str) -> IAMUser | None:
    """The user whose email (any case) or phone number is *login_str*.

    A database that already holds two spellings of one email answers with the exact one first, then the oldest.
    """
    login = login_str.strip()
    result = await session.exec(
        select(IAMUser)
        .where(or_(func.lower(IAMUser.email) == login.lower(), IAMUser.phone_number == login))
        .order_by(case((col(IAMUser.email) == login, 0), else_=1), col(IAMUser.created_at))
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


def _usable(user: IAMUser, kind: str, names: list[str]) -> list[str]:
    """The *names* a token can carry. One stored before names were checked (``orders:*``) is left out and
    logged, so it cannot stop every member of its role from signing in."""
    kept: list[str] = []
    for name in names:
        if SCOPE_NAME_RE.fullmatch(name):
            kept.append(name)
        else:
            logger.error(
                "auth.token.invalid_entry_skipped",
                kind=kind,
                name=name,
                user_id=str(user.id),
                hint="remove it with `tuvl iam`",
            )
    return kept


async def build_token_for_user(session: AsyncSession, user: IAMUser) -> str:
    """Assemble roles + scopes then mint an access token for *user*; a disabled role adds neither."""
    roles = await load_user_roles(session, user, active_only=True)
    role_names = _usable(user, "group", [r.name for r in roles])
    scopes = _usable(
        user, "scope", await load_role_scopes_for_roles(session, [r.id for r in roles])
    )
    ttl = settings.tuvl_token_ttl_seconds or None
    return mint_token(str(user.id), role_names, scopes, ttl_seconds=ttl)


async def revoke_token(
    raw_token: str, claims: Mapping[str, Any] | None = None, *, session: AsyncSession | None = None
) -> None:
    """Refuse *raw_token* from now on, until it would have expired anyway (for good when it has no
    expiry). Shared by logout and refresh. With *session* the caller commits the write.
    """
    await token_blacklist.revoke(raw_token, claims=claims, session=session)


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
    email = normalize_email(email)
    validate_password(password)
    validate_scope_name(admin_scope)
    await _take_seed_lock(session)
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


# ── Login throttle ─────────────────────────────────────────────────────────────

#: Wrong passwords a login may have before it starts to wait; then 2, 4, 8 ... seconds, up to the cap.
LOGIN_FREE_FAILURES = 5
LOGIN_MAX_DELAY_SECONDS = 900
#: A login with no failure for this long is forgotten.
LOGIN_FORGET_SECONDS = 24 * 3600
#: The window in which one address's failures are counted (``settings.tuvl_login_ip_failures`` of them).
LOGIN_IP_WINDOW_SECONDS = 900
_IP_TABLE_MAX = 10_000
_PURGE_EVERY_SECONDS = 60

#: address -> (start of its window, failures in it); kept per process, so a restart forgets it
_ip_failures: dict[str, tuple[datetime.datetime, int]] = {}
_last_purge: datetime.datetime | None = None


def reset_login_throttle() -> None:
    """Forget the per-address counters this process keeps."""
    global _last_purge
    _ip_failures.clear()
    _last_purge = None


def _login_key(login_str: str) -> str:
    return hashlib.sha256(normalize_email(login_str).encode()).hexdigest()


def _ip_wait(ip: str | None, now: datetime.datetime) -> int:
    """Seconds an address still has to wait (0: it may try)."""
    if not ip or settings.tuvl_login_ip_failures <= 0:
        return 0
    entry = _ip_failures.get(ip)
    if entry is None:
        return 0
    started, failures = entry
    left = LOGIN_IP_WINDOW_SECONDS - (now - started).total_seconds()
    if left <= 0:
        del _ip_failures[ip]
        return 0
    return math.ceil(left) if failures >= settings.tuvl_login_ip_failures else 0


def _ip_failed(ip: str | None, now: datetime.datetime) -> None:
    if not ip or settings.tuvl_login_ip_failures <= 0:
        return
    entry = _ip_failures.get(ip)
    if entry is not None and (now - entry[0]).total_seconds() < LOGIN_IP_WINDOW_SECONDS:
        _ip_failures[ip] = (entry[0], entry[1] + 1)
        return
    if len(_ip_failures) >= _IP_TABLE_MAX:
        for stale in [
            a
            for a, (s, _) in _ip_failures.items()
            if (now - s).total_seconds() >= LOGIN_IP_WINDOW_SECONDS
        ]:
            del _ip_failures[stale]
        if len(_ip_failures) >= _IP_TABLE_MAX:
            _ip_failures.pop(next(iter(_ip_failures)))
    _ip_failures[ip] = (now, 1)


async def _purge_old_attempts(session: AsyncSession, now: datetime.datetime) -> None:
    """Forget logins with no failure for a day (at most once a minute per process)."""
    global _last_purge
    if _last_purge is not None and (now - _last_purge).total_seconds() < _PURGE_EVERY_SECONDS:
        return
    _last_purge = now
    cutoff = now - datetime.timedelta(seconds=LOGIN_FORGET_SECONDS)
    await session.exec(delete(IAMLoginAttempt).where(col(IAMLoginAttempt.updated_at) < cutoff))


async def _record_failure(session: AsyncSession, key: str, ip: str | None) -> int:
    """Count one wrong password against *key* and *ip*; the new count of the login.

    The row is locked while it is counted, so guesses made at once are all counted; two that create it
    together are told apart by the primary key.
    """
    now = _now()
    _ip_failed(ip, now)
    await _purge_old_attempts(session, now)
    for _ in range(3):
        row = await session.get(IAMLoginAttempt, key, populate_existing=True, with_for_update=True)
        if row is None:
            row = IAMLoginAttempt(key=key, failures=0, updated_at=now)
            session.add(row)
        row.failures += 1
        row.updated_at = now
        row.locked_until = (
            now
            + datetime.timedelta(
                seconds=min(
                    LOGIN_MAX_DELAY_SECONDS, 2 ** min(row.failures - LOGIN_FREE_FAILURES, 20)
                )
            )
            if row.failures > LOGIN_FREE_FAILURES
            else None
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # another attempt created the row first: count on top of it
            continue
        return row.failures
    logger.warning("auth.login.failure_not_counted", login_hash=key[:16])
    return 0


async def _verified_user(
    session: AsyncSession, *, login_str: str, password: str, client_ip: str | None = None
) -> IAMUser:
    """The active user these credentials belong to, or :class:`AuthenticationError`.

    The message never distinguishes "no such user" from "wrong password" (user-enumeration guard), and a
    login that has failed too often waits (:class:`ThrottledError`) whether or not it exists. While it
    waits even the right password is refused, and no password is checked.
    """
    now = _now()
    key = _login_key(login_str)
    attempt = await session.get(IAMLoginAttempt, key, populate_existing=True)
    wait = _ip_wait(client_ip, now)
    if not wait and attempt is not None and attempt.locked_until and attempt.locked_until > now:
        wait = max(1, math.ceil((attempt.locked_until - now).total_seconds()))
    if wait:
        logger.warning("auth.login.throttled", login_hash=key[:16], ip=client_ip, retry_after=wait)
        raise ThrottledError(wait)

    user = await get_user_by_login(session, login_str)

    # Constant-time-ish: always run one bcrypt verification, even for a missing
    # or passwordless account, so response timing can't be used to enumerate
    # which emails/phones exist.
    candidate_hash = user.hashed_password if (user and user.hashed_password) else DUMMY_PW_HASH
    password_ok = await averify_password(password, candidate_hash)

    if user is None or not user.is_active or user.hashed_password is None or not password_ok:
        user_id = (
            str(user.id) if user is not None else None
        )  # before counting: a rollback expires `user`
        failures = await _record_failure(session, key, client_ip)
        logger.warning(
            "auth.login.failed",
            login_hash=key[:16],
            known=user_id is not None,
            user_id=user_id,
            failures=failures,
            ip=client_ip,
        )
        raise AuthenticationError("Incorrect email or password.")
    if attempt is not None:
        await session.delete(attempt)
        await session.commit()
    return user


async def unlock_login(session: AsyncSession, login_str: str) -> bool:
    """Let a login that is waiting after failed attempts try again now. True when it had failures."""
    row = await session.get(IAMLoginAttempt, _login_key(login_str), populate_existing=True)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def authenticate_admin(
    session: AsyncSession, *, login_str: str, password: str, client_ip: str | None = None
) -> IAMUser:
    """Verify credentials and require ``iam:admin`` through an active role (used by ``tuvl iam``)."""
    user = await _verified_user(
        session, login_str=login_str, password=password, client_ip=client_ip
    )
    roles = await load_user_roles(session, user, active_only=True)
    scopes = await load_role_scopes_for_roles(session, [r.id for r in roles])
    if ADMIN_SCOPE not in scopes:
        raise AuthenticationError(f"{user.email} does not hold {ADMIN_SCOPE}.")
    return user


async def login(
    session: AsyncSession, *, login_str: str, password: str, client_ip: str | None = None
) -> str:
    """Verify credentials and mint a token. Raises :class:`AuthenticationError`
    on any failure — the message never distinguishes "no such user" from
    "wrong password" (user-enumeration guard) — and :class:`ThrottledError` while
    the login or the *client_ip* has to wait."""
    user = await _verified_user(
        session, login_str=login_str, password=password, client_ip=client_ip
    )
    token = await build_token_for_user(session, user)
    logger.info("auth.login.password", email=user.email)
    return token


async def refresh(
    session: AsyncSession,
    *,
    user_id: str,
    raw_token: str,
    claims: Mapping[str, Any] | None = None,
) -> str:
    """Revoke *raw_token* and mint a fresh one for the user it belonged to.

    Raises :class:`AuthenticationError` if the user is gone or disabled.
    """
    user = await get_user_by_id(session, uuid.UUID(user_id))
    if user is None or not user.is_active:
        raise AuthenticationError("User not found or inactive.")

    await revoke_token(raw_token, claims, session=session)
    await session.commit()

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
    email = normalize_email(email)
    if not email:
        raise ValidationError("An email is required.")
    if password:
        validate_password(password)
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
        logger.error("iam.create_user.db_error", error=_first_line(exc))
        raise PersistenceError("Database error while creating user.") from None
    await session.refresh(user)

    return user, []


_IN_CHUNK = 5000  # ids per IN (...) list


async def _role_names_by_user(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """The names of the roles each of *user_ids* holds, in a query per few thousand users instead of one each."""
    names: dict[uuid.UUID, list[str]] = {}
    for start in range(0, len(user_ids), _IN_CHUNK):
        rows = await session.exec(
            select(IAMUserRole.user_id, IAMRole.name)
            .join(IAMRole, col(IAMRole.id) == IAMUserRole.role_id)
            .where(col(IAMUserRole.user_id).in_(user_ids[start : start + _IN_CHUNK]))
            .order_by(col(IAMRole.name))
        )
        for user_id, name in rows.all():
            names.setdefault(user_id, []).append(name)
    return names


async def _scopes_by_role(
    session: AsyncSession, role_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """The scope names of each of *role_ids*, in a query per few thousand roles instead of one each."""
    scopes: dict[uuid.UUID, list[str]] = {}
    for start in range(0, len(role_ids), _IN_CHUNK):
        rows = await session.exec(
            select(IAMRoleScope.role_id, IAMRoleScope.scope_name)
            .where(col(IAMRoleScope.role_id).in_(role_ids[start : start + _IN_CHUNK]))
            .order_by(col(IAMRoleScope.scope_name))
        )
        for role_id, scope in rows.all():
            scopes.setdefault(role_id, []).append(scope)
    return scopes


async def list_users(
    session: AsyncSession, *, limit: int | None = None, offset: int = 0
) -> list[tuple[IAMUser, list[str]]]:
    """Users by email with their role names: two queries however many there are. No *limit*: all of them."""
    stmt = select(IAMUser).order_by(col(IAMUser.email)).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    users = list((await session.exec(stmt)).all())
    names = await _role_names_by_user(session, [u.id for u in users])
    return [(u, names.get(u.id, [])) for u in users]


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> tuple[IAMUser, list[str]]:
    user = await get_user_by_id(session, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    roles = await load_user_roles(session, user)
    return user, [r.name for r in roles]


async def end_sessions(session: AsyncSession, user_ids: list[uuid.UUID]) -> None:
    """Refuse the tokens these users already hold: their access just shrank.

    Call it before the commit of the change that narrowed the access: with Postgres as the store the two
    commit or roll back together, and with Redis a mark that outlives a failed change only ends sessions early.
    """
    ttl = settings.tuvl_token_ttl_seconds or None
    for user_id in user_ids:
        await token_blacklist.end_sessions(str(user_id), ttl, session=session)


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
    await end_sessions(session, [user_id])
    await session.commit()


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
    if password is not UNSET and password is not None:
        validate_password(password)

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
    if ends_sessions:
        await end_sessions(session, [user_id])
    await session.commit()
    await session.refresh(user)
    roles = await load_user_roles(session, user)
    return user, [r.name for r in roles]


# ── Set-password links ─────────────────────────────────────────────────────────

PASSWORD_TOKEN_DEFAULT_HOURS = 72
PASSWORD_TOKEN_MAX_HOURS = 24 * 30


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
    validate_password(password)
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
    await end_sessions(session, [user.id])
    await session.commit()
    logger.info("auth.password_token.used", user_id=str(user.id))


# ── Roles ──────────────────────────────────────────────────────────────────────


async def create_role(
    session: AsyncSession,
    *,
    name: str,
    description: str | None = None,
    scopes: list[str] | None = None,
) -> tuple[IAMRole, list[str]]:
    scopes = [validate_scope_name(s) for s in scopes or []]
    validate_role_name(name)
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


async def list_roles(
    session: AsyncSession, *, limit: int | None = None, offset: int = 0
) -> list[tuple[IAMRole, list[str]]]:
    """Roles by name with their scopes: two queries however many there are. No *limit*: all of them."""
    stmt = select(IAMRole).order_by(col(IAMRole.name)).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    roles = list((await session.exec(stmt)).all())
    scopes = await _scopes_by_role(session, [r.id for r in roles])
    return [(r, scopes.get(r.id, [])) for r in roles]


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
    await end_sessions(session, members)
    await session.commit()


async def update_role_scopes(
    session: AsyncSession, role_id: uuid.UUID, scopes: list[str]
) -> tuple[IAMRole, list[str]]:
    """Atomically replace the full scope set for a role (delete-all then
    re-insert). An empty list removes all scopes."""
    scopes = [validate_scope_name(s) for s in scopes]
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
    await end_sessions(
        session, members
    )  # only a narrowed role ends sessions; added scopes wait for sign-in
    await session.commit()
    await session.refresh(role)
    return role, scopes


async def grant_scope(
    session: AsyncSession, role_id: uuid.UUID, scope: str
) -> tuple[IAMRole, list[str]]:
    """Add one scope to a role (idempotent). Holders get it at their next sign-in."""
    validate_scope_name(scope)
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
    await end_sessions(session, members)
    await session.commit()
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
    await end_sessions(session, members)
    await session.commit()
    await session.refresh(role)
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
    try:
        await session.commit()
    except IntegrityError:
        # another request made the same assignment between the check above and this commit
        await session.rollback()
        again = await session.exec(
            select(IAMUserRole).where(
                IAMUserRole.user_id == user_id, IAMUserRole.role_id == role_id
            )
        )
        if again.first() is None:
            raise NotFoundError("User or role no longer exists.") from None


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
    await end_sessions(session, [user_id])
    await session.commit()


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
    scopes = await _scopes_by_role(session, [r.id for r in roles])
    return [(r, scopes.get(r.id, []), counts.get(r.id, 0)) for r in roles]


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
    email = normalize_email(email)
    validate_password(password)
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
    waiting = await session.get(IAMLoginAttempt, _login_key(email), populate_existing=True)
    if waiting is not None:
        await session.delete(waiting)
    await end_sessions(session, [user.id])
    await session.commit()
    await session.refresh(user)
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
    from tuvl.core.auth.federation_loader import provider_problems

    path = safe_federation_path(name)
    body = copy.deepcopy(body)
    spec = body.get("spec") if isinstance(body, dict) else None
    if isinstance(spec, dict) and spec.get("client_secret") == "***":
        # what an admin screen got back from the read: keep the secret that is stored
        stored: Any = None
        if path.exists():
            try:
                stored = (
                    (_yaml.safe_load(await run_in_threadpool(path.read_text)) or {})
                    .get("spec", {})
                    .get("client_secret")
                )
            except (_yaml.YAMLError, AttributeError):
                stored = None
        if not stored:
            raise ValidationError(
                "client_secret is required: '***' stands for a stored secret and there is none."
            )
        spec["client_secret"] = stored
    problems = provider_problems(body, name=name, strict=True)
    if problems:
        raise ValidationError("; ".join(problems))

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
