"""First-boot IAM seed: ``kind: RoleSeed`` roles and the administrator from the environment.

A fresh production database has no users and no roles, and production has no Insight to make them.
So the project may carry its roles as ``RoleSeed`` documents, and the operator may name a first
administrator with ``TUVL_BOOTSTRAP_ADMIN_EMAIL`` and ``TUVL_BOOTSTRAP_ADMIN_PASSWORD`` (or
``TUVL_BOOTSTRAP_ADMIN_PASSWORD_FILE``).

The seed runs **once in the life of a database**. Whatever happens first, the ``iam_seeded`` marker is
written to ``tuvl_system_registry`` in the same transaction as the seed, so deleting every role later
does not seed again. A database that already has a user or a role is never touched: the marker is
written and the seed is ignored. From then on roles, scopes and users are changed with ``tuvl iam``.

Several replicas boot at once: a transaction-scoped advisory lock lets one seed and the others find
the marker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth.crypto import aget_password_hash
from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole
from tuvl.core.logger import logger

ROLE_SEED_KIND = "RoleSeed"
SEEDED_KEY = "iam_seeded"
_ADVISORY_KEY = 7_411_902_017  # arbitrary; shared by every replica of one database
_ROLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]*$")
_SCOPE = re.compile(r"^[^\s]+$")


@dataclass(frozen=True)
class RoleSeedEntry:
    name: str
    description: str | None = None
    scopes: tuple[str, ...] = ()


#: seeds loaded from the project, in file order; cleared and refilled by ``load_all_configs``
ROLE_SEED_REGISTRY: list[RoleSeedEntry] = []


def parse_role_seed(data: dict[str, Any]) -> list[RoleSeedEntry]:
    """The roles of one ``RoleSeed`` document; ``ValueError`` names what is wrong."""
    spec = data.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a mapping with a `roles` list")
    extra = set(spec) - {"roles"}
    if extra:
        raise ValueError(f"unknown spec field(s): {', '.join(sorted(extra))}")
    roles = spec.get("roles")
    if not isinstance(roles, list) or not roles:
        raise ValueError("spec.roles must be a non-empty list")
    entries: list[RoleSeedEntry] = []
    seen: set[str] = set()
    for i, raw in enumerate(roles):
        where = f"spec.roles[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be a mapping with a `name`")
        unknown = set(raw) - {"name", "description", "scopes"}
        if unknown:
            raise ValueError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")
        name = raw.get("name")
        if not isinstance(name, str) or not _ROLE_NAME.match(name):
            raise ValueError(f"{where}.name must be a role name (letters, digits, _ . : -)")
        if name in seen:
            raise ValueError(f"{where}: role '{name}' is listed twice")
        seen.add(name)
        description = raw.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError(f"{where}.description must be a string")
        scopes = raw.get("scopes") or []
        if not isinstance(scopes, list) or not all(
            isinstance(s, str) and _SCOPE.match(s) for s in scopes
        ):
            raise ValueError(f"{where}.scopes must be a list of scope strings such as `runs:read`")
        entries.append(RoleSeedEntry(name, description, tuple(dict.fromkeys(scopes))))
    return entries


def register_role_seed_from_dict(data: dict[str, Any], filename: str = "<unknown>") -> None:
    try:
        entries = parse_role_seed(data)
    except ValueError as exc:
        raise ValueError(f"{filename}: {exc}") from exc
    known = {e.name for e in ROLE_SEED_REGISTRY}
    for entry in entries:
        if entry.name in known:
            raise ValueError(
                f"{filename}: role '{entry.name}' is already defined by another RoleSeed"
            )
        known.add(entry.name)
        ROLE_SEED_REGISTRY.append(entry)


@dataclass
class SeedResult:
    status: str  # "seeded" | "already_seeded" | "populated" | "nothing_to_seed"
    roles: int = 0
    admin_email: str | None = None
    notes: list[str] = field(default_factory=list)


def read_admin_from_env() -> tuple[str | None, str | None]:
    """``(email, password)`` named by the environment; a password file wins over the plain variable."""
    from tuvl.core.config import settings

    email = (settings.tuvl_bootstrap_admin_email or "").strip() or None
    password: str | None = settings.tuvl_bootstrap_admin_password or None
    path = settings.tuvl_bootstrap_admin_password_file
    if path:
        try:
            password = Path(path).read_text().rstrip("\r\n") or None
        except OSError as exc:
            raise RuntimeError(
                f"TUVL_BOOTSTRAP_ADMIN_PASSWORD_FILE ({path}) cannot be read: {exc.strerror}"
            ) from exc
    return email, password


async def _marker_exists(session: AsyncSession) -> bool:
    from tuvl.core.bootstrap import SystemRegistry

    return await session.get(SystemRegistry, SEEDED_KEY) is not None


async def _write_marker(session: AsyncSession) -> None:
    from tuvl.core.bootstrap import SystemRegistry

    session.add(SystemRegistry(key=SEEDED_KEY, value="1"))


async def seed_iam(
    session: AsyncSession,
    *,
    roles: list[RoleSeedEntry],
    admin_email: str | None,
    admin_password: str | None,
) -> SeedResult:
    """Seed *roles* and the environment administrator when this database never had IAM data.

    Idempotent and safe to call on every boot; the caller owns the transaction and commits.
    """
    connection = await session.connection()
    if connection.dialect.name == "postgresql":
        await connection.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_KEY})

    if await _marker_exists(session):
        return SeedResult("already_seeded")

    has_admin_input = bool(admin_email and admin_password)
    notes: list[str] = []
    if bool(admin_email) != bool(admin_password):
        notes.append(
            "TUVL_BOOTSTRAP_ADMIN_EMAIL and TUVL_BOOTSTRAP_ADMIN_PASSWORD(_FILE) must both be set; "
            "no administrator was created"
        )
    if not roles and not has_admin_input:
        return SeedResult("nothing_to_seed", notes=notes)

    occupied = (await session.exec(select(IAMUser).limit(1))).first() is not None or (
        await session.exec(select(IAMRole).limit(1))
    ).first() is not None
    if occupied:
        await _write_marker(session)
        return SeedResult("populated")

    for entry in roles:
        role = IAMRole(name=entry.name, description=entry.description)
        session.add(role)
        await session.flush()
        for scope in entry.scopes:
            session.add(IAMRoleScope(role_id=role.id, scope_name=scope))
    await session.flush()

    created_admin: str | None = None
    if has_admin_input:
        assert admin_email and admin_password
        role = await iam_service._ensure_superadmin_role(session, [iam_service.ADMIN_SCOPE])
        user = IAMUser(
            email=admin_email,
            hashed_password=await aget_password_hash(admin_password),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        session.add(IAMUserRole(user_id=user.id, role_id=role.id))
        created_admin = admin_email
    elif not any(iam_service.ADMIN_SCOPE in e.scopes for e in roles):
        notes.append(
            "no administrator exists: set TUVL_BOOTSTRAP_ADMIN_EMAIL and TUVL_BOOTSTRAP_ADMIN_PASSWORD, "
            "or run `tuvl iam reset-admin` in the cluster"
        )

    await _write_marker(session)
    return SeedResult("seeded", roles=len(roles), admin_email=created_admin, notes=notes)


async def seed_iam_at_boot() -> SeedResult | None:
    """Run the seed on the primary datasource (a no-op without one)."""
    from tuvl.core.datasources import postgres

    if postgres._PRIMARY_DS not in postgres._sessions_rw:
        return None
    email, password = read_admin_from_env()
    result: SeedResult | None = None
    async for session in postgres.get_primary_rw_session():
        result = await seed_iam(
            session, roles=list(ROLE_SEED_REGISTRY), admin_email=email, admin_password=password
        )
        await session.commit()
    if result is None:
        return None
    for note in result.notes:
        logger.warning("iam.seed.note", note=note)
    if result.status == "seeded":
        logger.info(
            "iam.seed.done", roles=result.roles, admin=result.admin_email or "none (not requested)"
        )
    elif result.status == "populated":
        logger.info(
            "iam.seed.ignored",
            reason="the IAM database already has users or roles; the RoleSeed and "
            "TUVL_BOOTSTRAP_ADMIN_* apply to a fresh database only",
        )
    return result
