"""Role membership over HTTP: the ``roles`` API group.

What an application's own screen needs at runtime and nothing more: list the roles, see and pick users,
add a user to a role, remove one, disable or re-enable a role. Creating roles, changing what a role
grants, creating users and federation stay development-time (``RoleSeed``, ``tuvl iam``) and are not
here. A production server leaves this group unmounted until ``roles`` is in ``spec.api.expose`` /
``TUVL_API_EXPOSE``. It needs the ``roles:manage`` scope (``iam:admin`` also passes), and
:func:`iam_service.check_can_manage_role` stops a manager handing out access they do not hold.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth.access import require_scope
from tuvl.core.auth.iam_service import ConflictError, ForbiddenError, IamError, NotFoundError
from tuvl.core.auth.models import IAMRole, IAMUser
from tuvl.core.auth.router import _get_iam_session
from tuvl.core.auth.schemas import TokenUser
from tuvl.core.logger import logger

roles_router = APIRouter(prefix="/auth/roles", tags=["roles"])

IamSession = Annotated[AsyncSession, Depends(_get_iam_session)]
RoleManager = Annotated[TokenUser, Depends(require_scope(iam_service.ROLE_MANAGE_SCOPE))]


class RoleSummary(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    #: what the role grants, read-only here; changed with ``tuvl iam`` or ``RoleSeed``
    scopes: list[str]
    member_count: int


class Person(BaseModel):
    id: uuid.UUID
    email: str
    first_name: str | None
    last_name: str | None
    is_active: bool


class PersonPage(BaseModel):
    items: list[Person]
    #: pass as ``cursor`` for the next page; ``None`` on the last
    next_cursor: str | None


class RoleStateRequest(BaseModel):
    is_active: bool = Field(
        ..., description="false disables the role (it grants nothing); true re-enables it."
    )


def _fail(exc: IamError) -> HTTPException:
    code: dict[type[IamError], int] = {
        NotFoundError: status.HTTP_404_NOT_FOUND,
        ConflictError: status.HTTP_409_CONFLICT,
        ForbiddenError: status.HTTP_403_FORBIDDEN,
    }
    return HTTPException(status_code=code[type(exc)], detail=str(exc))


def _person(user: IAMUser) -> Person:
    return Person(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
    )


def _summary(row: tuple[IAMRole, list[str], int]) -> RoleSummary:
    role, scopes, count = row
    return RoleSummary(
        id=role.id,
        name=role.name,
        description=role.description,
        is_active=role.is_active,
        scopes=sorted(scopes),
        member_count=count,
    )


async def _manageable(session: AsyncSession, caller: TokenUser, role_id: uuid.UUID) -> IAMRole:
    """The role, if it exists and *caller* may change who holds it."""
    role = await iam_service.get_role_by_id(session, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found.")
    scopes = await iam_service.load_role_scopes(session, role_id)
    try:
        iam_service.check_can_manage_role(caller.scopes, role.name, scopes)
    except ForbiddenError as exc:
        raise _fail(exc) from exc
    return role


def _audit(caller: TokenUser, action: str, role: IAMRole, user_id: uuid.UUID | None = None) -> None:
    logger.info(
        "auth.iam.roles",
        actor=caller.user_id,
        action=action,
        role=role.name,
        user=str(user_id) if user_id else None,
    )


@roles_router.get("", response_model=list[RoleSummary], summary="List roles")
async def list_roles(session: IamSession, _caller: RoleManager) -> list[RoleSummary]:
    return [_summary(row) for row in await iam_service.list_role_summaries(session)]


@roles_router.get("/candidates", response_model=PersonPage, summary="Find users to add to a role")
async def find_candidates(
    session: IamSession,
    _caller: RoleManager,
    q: Annotated[str | None, Query(max_length=100, description="Part of an email or name.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> PersonPage:
    users, nxt = await iam_service.find_users(session, q=q, limit=limit, after=cursor)
    return PersonPage(items=[_person(u) for u in users], next_cursor=nxt)


@roles_router.get("/{role_id}/members", response_model=PersonPage, summary="List a role's members")
async def list_members(
    role_id: uuid.UUID,
    session: IamSession,
    _caller: RoleManager,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> PersonPage:
    if await iam_service.get_role_by_id(session, role_id) is None:
        raise HTTPException(status_code=404, detail="Role not found.")
    users, nxt = await iam_service.find_users(session, role_id=role_id, limit=limit, after=cursor)
    return PersonPage(items=[_person(u) for u in users], next_cursor=nxt)


@roles_router.put(
    "/{role_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Add a user to a role (idempotent)",
)
async def add_member(
    role_id: uuid.UUID, user_id: uuid.UUID, session: IamSession, caller: RoleManager
) -> None:
    role = await _manageable(session, caller, role_id)
    try:
        await iam_service.assign_role(session, user_id, role_id)
    except (NotFoundError, ConflictError) as exc:
        raise _fail(exc) from exc
    _audit(caller, "add_member", role, user_id)


@roles_router.delete(
    "/{role_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a user from a role",
)
async def remove_member(
    role_id: uuid.UUID, user_id: uuid.UUID, session: IamSession, caller: RoleManager
) -> None:
    role = await _manageable(session, caller, role_id)
    try:
        await iam_service.revoke_role(session, user_id, role_id)
    except (NotFoundError, ConflictError) as exc:
        raise _fail(exc) from exc
    _audit(caller, "remove_member", role, user_id)


@roles_router.patch("/{role_id}", response_model=RoleSummary, summary="Disable or re-enable a role")
async def set_role_state(
    role_id: uuid.UUID, body: RoleStateRequest, session: IamSession, caller: RoleManager
) -> RoleSummary:
    role = await _manageable(session, caller, role_id)
    try:
        await iam_service.set_role_active(session, role_id, body.is_active)
    except (NotFoundError, ConflictError) as exc:
        raise _fail(exc) from exc
    _audit(caller, "enable" if body.is_active else "disable", role)
    return _summary((await iam_service.list_role_summaries(session, role_id))[0])
