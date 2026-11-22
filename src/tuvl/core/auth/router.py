"""tuvl IAM router — authentication and user/role administration.

Covers the standard OAuth2 password flow (POST /auth/token), superadmin
bootstrap (POST /auth/bootstrap) and full user/role CRUD under /auth/admin/*.
Federated login (Google, GitHub, Microsoft) and federation-provider admin live
in :mod:`tuvl.core.auth.federation`.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth.access import (
    Claims,
    _oauth2_scheme,
    require_scope,
    user_from_claims,
    verify_token,
)
from tuvl.core.auth.iam_service import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PersistenceError,
    ThrottledError,
    ValidationError,
)
from tuvl.core.auth.schemas import TokenUser
from tuvl.core.logger import logger

router = APIRouter(prefix="/auth", tags=["auth"])

#: IAM administration: first-admin bootstrap and user/role CRUD (federation-provider admin joins it from
#: ``federation.admin_router``). Mounted as its own API group (``iam``) that a production server leaves
#: out by default; production IAM is run in the cluster with ``tuvl iam``. Login, refresh and logout stay on ``router``.
iam_router = APIRouter(prefix="/auth", tags=["iam"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CreateUserRequest(BaseModel):
    email: str = Field(..., description="User email (used as login username).")
    phone_number: str | None = Field(default=None, description="Optional phone number for login.")
    first_name: str | None = None
    last_name: str | None = None
    password: str | None = Field(
        default=None,
        description="Plaintext password (will be hashed). Omit for federated-only accounts.",
    )
    is_active: bool = True


class InviteRequest(BaseModel):
    ttl_hours: int = Field(default=72, ge=1, le=720, description="How long the link works.")


class InviteResponse(BaseModel):
    token: str = Field(..., description="Give this to the user once; it cannot be read back.")
    expires_at: datetime.datetime


class SetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=20, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)


class UpdateUserRequest(BaseModel):
    phone_number: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    password: str | None = Field(
        default=None,
        description="New plaintext password (will be hashed). Omit to leave unchanged.",
    )
    is_active: bool | None = Field(
        default=None,
        description="Set to false to disable login, true to re-enable.",
    )


class CreateRoleRequest(BaseModel):
    name: str = Field(..., description="Role name, e.g. 'hr_manager'.")
    description: str | None = None
    scopes: list[str] = Field(
        default_factory=list,
        description="Initial scope strings, e.g. ['requisition:write', 'candidate:read'].",
    )


class UpdateRoleRequest(BaseModel):
    description: str | None = Field(
        default=None,
        description="Role description. Pass null to clear when included; omit to leave unchanged.",
    )
    is_active: bool | None = Field(
        default=None,
        description="Set to false to disable the role (it grants nothing), true to re-enable.",
    )


class UpdateRoleScopesRequest(BaseModel):
    scopes: list[str] = Field(
        ...,
        description="Complete replacement set of scopes for this role.",
    )


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    phone_number: str | None
    first_name: str | None
    last_name: str | None
    is_active: bool
    roles: list[str]


class RoleResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    scopes: list[str]
    is_active: bool = True


class BootstrapRequest(BaseModel):
    email: str
    phone_number: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    password: str
    admin_scope: str = "iam:admin"


async def _get_iam_session() -> AsyncIterator[AsyncSession]:
    """Always-RW session on the primary datasource. Used by IAM routes."""
    from tuvl.core.datasources.postgres import get_primary_rw_session

    async for session in get_primary_rw_session():
        yield session


IamSession = Annotated[AsyncSession, Depends(_get_iam_session)]
AdminUser = Annotated[TokenUser, Depends(require_scope("iam:admin"))]


@iam_router.post(
    "/bootstrap",
    response_model=TokenResponse,
    summary="Create first superadmin user (one-time setup)",
)
async def bootstrap(req: BootstrapRequest, session: IamSession) -> TokenResponse:
    """Bootstrap the IAM with the first administrator account.

    This endpoint is only available when the ``tuvl_system_iam_users`` table is empty.
    On all subsequent calls it returns **409 Conflict**.

    The created user automatically receives a ``superadmin`` role that carries
    the ``iam:admin`` scope (plus any additional scope provided in the request).
    """
    try:
        token = await iam_service.bootstrap(
            session,
            email=req.email,
            password=req.password,
            admin_scope=req.admin_scope,
            phone_number=req.phone_number,
            first_name=req.first_name,
            last_name=req.last_name,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TokenResponse(access_token=token)


@router.post(
    "/password/set",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set your own password with a one-time link token",
)
async def set_password(req: SetPasswordRequest, session: IamSession) -> None:
    """Public: the token is the credential. It works once and expires; the user's open sessions end."""
    try:
        await iam_service.set_password_with_token(session, req.token, req.password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Exchange email + password for an access token (JWT)",
)
async def login(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: IamSession,
) -> TokenResponse:
    """Standard OAuth2 password-grant endpoint.

    The ``username`` field must be the user's **email address**.

    On success returns ``{"access_token": "<jwt>", "token_type": "bearer"}``: an
    Ed25519-signed JWT whose ``sub``, ``groups`` and ``scopes`` claims come from
    the user's roles.

    After 5 wrong passwords a login waits 2, 4, 8 ... seconds (up to 15 minutes) before the next
    attempt is judged, and answers **429** with ``Retry-After`` meanwhile, whether or not the login exists.
    """
    try:
        token = await iam_service.login(
            session,
            login_str=form.username,
            password=form.password,
            client_ip=request.client.host if request.client else None,
        )
    except ThrottledError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return TokenResponse(access_token=token)


class MeResponse(BaseModel):
    user_id: str
    groups: list[str]
    scopes: list[str]


CurrentToken = Annotated[str, Depends(_oauth2_scheme)]


@router.get(
    "/jwks.json",
    summary="The public keys that verify tuvl access tokens (JWKS)",
)
async def jwks() -> dict[str, list[dict[str, str]]]:
    """Ed25519 public keys (RFC 8037) for gateways and services that verify tokens
    themselves: the current key first, then any retired key that still verifies tokens
    issued before a rotation (match on ``kid``). Verification alone can't see logouts or
    ended sessions — call ``/auth/me`` (or tuvl) when that matters."""
    from tuvl.core.auth.keys import get_key_manager

    return {"keys": get_key_manager().jwks}


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Return the identity and permissions embedded in the current token",
)
async def get_me(claims: Annotated[Claims, Depends(verify_token)]) -> MeResponse:
    """The calling token's user id, groups and scopes — after verification,
    revocation and session checks (the token's payload alone can't say whether
    it is still valid)."""
    token_user: TokenUser = user_from_claims(claims)
    return MeResponse(
        user_id=token_user.user_id,
        groups=token_user.groups,
        scopes=token_user.scopes,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh an access token (revokes the old one)",
)
async def refresh_token(
    raw_token: CurrentToken,
    claims: Annotated[Claims, Depends(verify_token)],
    session: IamSession,
) -> TokenResponse:
    """Exchange a valid token for a fresh one with a new expiry.

    The old token is revoked at once, on every worker, so it cannot be reused.
    The caller should discard it after calling this endpoint.
    """
    current_user_info: TokenUser = user_from_claims(claims)

    try:
        new_token = await iam_service.refresh(
            session, user_id=current_user_info.user_id, raw_token=raw_token, claims=claims
        )
    except AuthenticationError as exc:
        # NOTE: deliberately no WWW-Authenticate header here — the original
        # REST handler never set one on this 401 (unlike /token's), and that
        # asymmetry is preserved exactly.
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return TokenResponse(access_token=new_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current token (logout)",
)
async def logout(
    raw_token: CurrentToken,
    claims: Annotated[Claims, Depends(verify_token)],
    session: IamSession,
) -> None:
    """Invalidate the current bearer token.

    After calling this endpoint every tuvl worker rejects the token (the
    revocation is kept in Redis where a Redis datasource is deployed, in the
    database otherwise).  Clients should discard the token immediately.
    """
    await iam_service.revoke_token(raw_token, claims, session=session)
    await session.commit()
    logger.info("Token revoked via logout.")


@iam_router.post(
    "/admin/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new IAM user",
)
async def create_user(
    req: CreateUserRequest,
    session: IamSession,
    _admin: AdminUser,
) -> UserResponse:
    try:
        user, roles = await iam_service.create_user(
            session,
            email=req.email,
            password=req.password,
            phone_number=req.phone_number,
            first_name=req.first_name,
            last_name=req.last_name,
            is_active=req.is_active,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(status_code=500, detail=exc.detail) from exc

    return UserResponse(
        id=user.id,
        email=user.email,
        phone_number=user.phone_number,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
        roles=roles,
    )


@iam_router.post(
    "/admin/users/{user_id}/invite",
    response_model=InviteResponse,
    summary="Make a one-time link token for a user to set their own password",
)
async def invite_user(
    user_id: uuid.UUID,
    session: IamSession,
    _admin: AdminUser,
    req: InviteRequest | None = None,
) -> InviteResponse:
    try:
        token, expires = await iam_service.issue_password_token(
            session, user_id, ttl_hours=(req.ttl_hours if req else 72)
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return InviteResponse(token=token, expires_at=expires)


@iam_router.get(
    "/admin/users",
    response_model=list[UserResponse],
    summary="List all IAM users",
)
async def list_users(
    session: IamSession,
    _admin: AdminUser,
    limit: Annotated[
        int | None, Query(ge=1, le=1000, description="At most this many users; omit for all.")
    ] = None,
    offset: Annotated[int, Query(ge=0, description="Skip this many users (ordered by email).")] = 0,
) -> list[UserResponse]:
    pairs = await iam_service.list_users(session, limit=limit, offset=offset)
    return [
        UserResponse(
            id=u.id,
            email=u.email,
            phone_number=u.phone_number,
            first_name=u.first_name,
            last_name=u.last_name,
            is_active=u.is_active,
            roles=roles,
        )
        for u, roles in pairs
    ]


@iam_router.get(
    "/admin/users/{user_id}",
    response_model=UserResponse,
    summary="Get a single IAM user",
)
async def get_user(
    user_id: uuid.UUID,
    session: IamSession,
    _admin: AdminUser,
) -> UserResponse:
    try:
        user, roles = await iam_service.get_user(session, user_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return UserResponse(
        id=user.id,
        email=user.email,
        phone_number=user.phone_number,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
        roles=roles,
    )


@iam_router.delete(
    "/admin/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an IAM user",
)
async def delete_user(
    user_id: uuid.UUID,
    session: IamSession,
    _admin: AdminUser,
) -> None:
    try:
        await iam_service.delete_user(session, user_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@iam_router.patch(
    "/admin/users/{user_id}",
    response_model=UserResponse,
    summary="Update a user's password or active status",
)
async def update_user(
    user_id: uuid.UUID,
    req: UpdateUserRequest,
    session: IamSession,
    _admin: AdminUser,
) -> UserResponse:
    """Update an existing IAM user's password and/or ``is_active`` flag.

    Only fields provided (non-null) are changed.  To disable a user without
    changing their password, send ``{"is_active": false}``.
    """
    try:
        user, roles = await iam_service.update_user(
            session,
            user_id,
            password=req.password,
            is_active=req.is_active,
            first_name=req.first_name,
            last_name=req.last_name,
            phone_number=req.phone_number,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return UserResponse(
        id=user.id,
        email=user.email,
        phone_number=user.phone_number,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
        roles=roles,
    )


@iam_router.post(
    "/admin/roles",
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new role with scopes",
)
async def create_role(
    req: CreateRoleRequest,
    session: IamSession,
    _admin: AdminUser,
) -> RoleResponse:
    try:
        role, scopes = await iam_service.create_role(
            session, name=req.name, description=req.description, scopes=req.scopes
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        scopes=scopes,
        is_active=role.is_active,
    )


@iam_router.get(
    "/admin/roles",
    response_model=list[RoleResponse],
    summary="List all roles with their scopes",
)
async def list_roles(
    session: IamSession,
    _admin: AdminUser,
    limit: Annotated[
        int | None, Query(ge=1, le=1000, description="At most this many roles; omit for all.")
    ] = None,
    offset: Annotated[int, Query(ge=0, description="Skip this many roles (ordered by name).")] = 0,
) -> list[RoleResponse]:
    pairs = await iam_service.list_roles(session, limit=limit, offset=offset)
    return [
        RoleResponse(
            id=role.id,
            name=role.name,
            description=role.description,
            scopes=scopes,
            is_active=role.is_active,
        )
        for role, scopes in pairs
    ]


@iam_router.delete(
    "/admin/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a role (and all its scope and user assignments)",
)
async def delete_role(
    role_id: uuid.UUID,
    session: IamSession,
    _admin: AdminUser,
) -> None:
    try:
        await iam_service.delete_role(session, role_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@iam_router.patch(
    "/admin/roles/{role_id}",
    response_model=RoleResponse,
    summary="Update a role's description or active status",
)
async def update_role(
    role_id: uuid.UUID,
    req: UpdateRoleRequest,
    session: IamSession,
    _admin: AdminUser,
) -> RoleResponse:
    """Update an existing IAM role's ``description`` and/or ``is_active`` flag.

    Only fields present in the request body are changed; passing ``{"description": null}``
    clears the description, while omitting ``description`` leaves it unchanged.
    """
    fields = req.model_fields_set
    try:
        role, scopes = await iam_service.update_role(
            session,
            role_id,
            description=req.description if "description" in fields else iam_service.UNSET,
            is_active=(
                req.is_active
                if "is_active" in fields and req.is_active is not None
                else iam_service.UNSET
            ),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        scopes=scopes,
        is_active=role.is_active,
    )


@iam_router.patch(
    "/admin/roles/{role_id}/scopes",
    response_model=RoleResponse,
    summary="Replace all scopes on a role",
)
async def update_role_scopes(
    role_id: uuid.UUID,
    req: UpdateRoleScopesRequest,
    session: IamSession,
    _admin: AdminUser,
) -> RoleResponse:
    """Atomically replace the full scope set for a role.

    All existing ``IAMRoleScope`` rows for this role are deleted and replaced
    with the provided list.  An empty list removes all scopes.
    """
    try:
        role, scopes = await iam_service.update_role_scopes(session, role_id, req.scopes)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        scopes=scopes,
        is_active=role.is_active,
    )


@iam_router.post(
    "/admin/users/{user_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Assign a role to a user",
)
async def assign_role(
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    session: IamSession,
    _admin: AdminUser,
) -> None:
    try:
        await iam_service.assign_role(session, user_id, role_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@iam_router.delete(
    "/admin/users/{user_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a role from a user",
)
async def revoke_role(
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    session: IamSession,
    _admin: AdminUser,
) -> None:
    try:
        await iam_service.revoke_role(session, user_id, role_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
