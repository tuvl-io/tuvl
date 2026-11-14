"""tuvl IAM router — authentication, user/role administration, and federation.

Covers the standard OAuth2 password flow (POST /auth/token), superadmin
bootstrap (POST /auth/bootstrap), full user/role CRUD under /auth/admin/*,
and federated login via OAuth 2.0 authorization-code for Google, GitHub, and
Microsoft (GET /auth/oauth/{provider}/start and /callback).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlmodel import select
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
    ParseError,
    PersistenceError,
    ValidationError,
)
from tuvl.core.auth.models import IAMRole, IAMUser, IAMUserRole
from tuvl.core.auth.schemas import TokenUser
from tuvl.core.auth.state_store import oauth_state_store
from tuvl.core.config import settings
from tuvl.core.infra.http_client import get_http_client
from tuvl.core.logger import logger

router = APIRouter(prefix="/auth", tags=["auth"])

#: IAM administration: first-admin bootstrap, user/role CRUD and federation-provider admin.
#: Mounted as its own API group (``iam``) that a production server leaves out by default;
#: production IAM is run in the cluster with ``tuvl iam``. Login, refresh and logout stay on ``router``.
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
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: IamSession,
) -> TokenResponse:
    """Standard OAuth2 password-grant endpoint.

    The ``username`` field must be the user's **email address**.

    On success returns ``{"access_token": "<jwt>", "token_type": "bearer"}``: an
    Ed25519-signed JWT whose ``sub``, ``groups`` and ``scopes`` claims come from
    the user's roles.
    """
    try:
        token = await iam_service.login(session, login_str=form.username, password=form.password)
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
    summary="The public key that verifies tuvl access tokens (JWKS)",
)
async def jwks() -> dict[str, list[dict[str, str]]]:
    """Ed25519 public key (RFC 8037) for gateways and services that verify tokens
    themselves. Verification alone can't see logouts or ended sessions — call
    ``/auth/me`` (or tuvl) when that matters."""
    from tuvl.core.auth.keys import get_key_manager

    return {"keys": [get_key_manager().jwk]}


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

    The old token is immediately added to the revocation blacklist so it
    cannot be reused.  The caller should discard the old token after calling
    this endpoint.
    """
    current_user_info: TokenUser = user_from_claims(claims)

    try:
        new_token = await iam_service.refresh(
            session, user_id=current_user_info.user_id, raw_token=raw_token
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
    _claims: Annotated[Claims, Depends(verify_token)],
) -> None:
    """Invalidate the current bearer token.

    After calling this endpoint the token will be rejected by all tuvl
    workers that share the Redis blacklist.  Clients should discard the
    token immediately.
    """
    await iam_service.revoke_token(raw_token)
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
async def list_users(session: IamSession, _admin: AdminUser) -> list[UserResponse]:
    pairs = await iam_service.list_users(session)
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
async def list_roles(session: IamSession, _admin: AdminUser) -> list[RoleResponse]:
    pairs = await iam_service.list_roles(session)
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


# Built-in URL/scope defaults for well-known providers.
# Can be overridden per-provider in the YAML spec.
_BUILTIN_URLS: dict[str, dict[str, str]] = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
    "github": {
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scope": "read:user user:email",
    },
    "microsoft": {
        "auth_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "userinfo_url": "https://graph.microsoft.com/v1.0/me",
        "scope": "openid email profile User.Read",
    },
}


def _provider_config(provider: str) -> tuple[dict[str, str], str, str]:
    """Return (urls_dict, client_id, client_secret) for *provider* or raise 404/501.

    Resolution order:
    1. FEDERATION_REGISTRY (YAML files in project's federation/ directory)
    2. Legacy env-var fallback (TUVL_OAUTH_<PROVIDER>_* settings)
    """
    from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY

    reg_entry = FEDERATION_REGISTRY.get(provider)

    if reg_entry is not None:
        spec = reg_entry.get("spec", {})
        provider_type: str = spec.get("provider", provider)
        tenant: str = spec.get("tenant_id", "common")

        # Start from built-in defaults for this provider type, then overlay YAML overrides
        base = {
            k: v.replace("{tenant}", tenant)
            for k, v in _BUILTIN_URLS.get(provider_type, {}).items()
        }
        for key in ("auth_url", "token_url", "userinfo_url", "scope"):
            if spec.get(key):
                base[key] = spec[key]

        client_id: str = spec.get("client_id", "")
        client_secret: str = spec.get("client_secret", "")
    else:
        # Legacy env-var fallback (settings-based, no YAML)
        builtin = _BUILTIN_URLS.get(provider)
        if builtin is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown provider '{provider}'. Configure a federation/{provider}.yaml or use: {list(_BUILTIN_URLS)}.",
            )
        tenant = settings.tuvl_oauth_microsoft_tenant_id or "common"
        base = {k: v.replace("{tenant}", tenant) for k, v in builtin.items()}
        client_id = getattr(settings, f"tuvl_oauth_{provider}_client_id", "")
        client_secret = getattr(settings, f"tuvl_oauth_{provider}_client_secret", "")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=501,
            detail=(
                f"Federation provider '{provider}' is not fully configured — "
                f"client_id or client_secret is missing. "
                f"Edit federation/{provider}.yaml and set the credentials."
            ),
        )

    return base, client_id, client_secret


async def _login_with_access_token(
    provider: str, urls: dict[str, str], access_token: str, session: AsyncSession
) -> str:
    """Profile → verified identity → IAM user (create/link) → tuvl access token."""
    client = get_http_client(timeout=15.0)
    profile_resp = await client.get(
        urls["userinfo_url"],
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )

    if profile_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch user profile from provider.")

    profile: dict[str, Any] = profile_resp.json()

    from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY as _FED

    allowed_users = [
        str(u).lower()
        for u in ((_FED.get(provider) or {}).get("spec", {}).get("allowed_users") or [])
    ]
    if allowed_users and str(profile.get("login") or "").lower() not in allowed_users:
        logger.warning("Federated login blocked: user not in allowed_users", provider=provider)
        raise HTTPException(
            status_code=403, detail="This account is not permitted for this provider."
        )

    # Normalize email + subject across providers
    email: str | None = profile.get("email")
    # Whether the identity provider asserts this email is verified. OIDC carries
    # the standard ``email_verified`` claim; for GitHub we only trust the email
    # fetched from the verified /user/emails path below. Defaults to False so an
    # unverified email can never auto-link to an existing account.
    email_verified: bool = bool(profile.get("email_verified", False))
    first_name: str | None = profile.get("given_name") or profile.get("first_name")
    last_name: str | None = profile.get("family_name") or profile.get("last_name")
    if not first_name and not last_name and profile.get("name"):
        parts = profile["name"].split(" ", 1)
        first_name = parts[0]
        if len(parts) > 1:
            last_name = parts[1]
    if not email and provider == "github":
        # GitHub users with private email settings don't expose email in the
        # primary profile endpoint.  Fall back to the /user/emails API which
        # lists all addresses including the primary/verified private one.
        emails_client = get_http_client(timeout=15.0)
        emails_resp = await emails_client.get(
            "https://api.github.com/user/emails",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if emails_resp.status_code == 200:
            email_entries: list[dict[str, Any]] = emails_resp.json()
            # Prefer the primary+verified address; fall back to first verified.
            primary = next(
                (e["email"] for e in email_entries if e.get("primary") and e.get("verified")),
                None,
            )
            if primary is None:
                primary = next(
                    (e["email"] for e in email_entries if e.get("verified")),
                    None,
                )
            email = primary
            # ``primary`` was selected only from entries with verified=True.
            if primary is not None:
                email_verified = True

    if not email:
        raise HTTPException(
            status_code=400,
            detail="Provider did not supply an email address. Ensure the email scope is granted.",
        )

    # Domain restriction: check allowed_domains from FEDERATION_REGISTRY
    from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY as _FED_REG

    _reg_entry = _FED_REG.get(provider)
    if _reg_entry is not None:
        _allowed_domains: list[str] = _reg_entry.get("spec", {}).get("allowed_domains") or []
        if _allowed_domains:
            _email_domain = email.split("@")[-1].lower()
            if _email_domain not in [d.lower() for d in _allowed_domains]:
                logger.warning(
                    "Federated login blocked: '%s' domain not in allowed list for provider '%s'.",
                    email,
                    provider,
                )
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Your email domain '{_email_domain}' is not permitted for this provider. "
                        f"Contact your administrator."
                    ),
                )

    sub = str(profile.get("sub") or profile.get("id") or email)

    # Upsert: find by federated sub, fall back to email
    result = await session.exec(
        select(IAMUser).where(
            IAMUser.federated_provider == provider,
            IAMUser.federated_sub == sub,
        )
    )
    user = result.first()

    if user is None:
        # An account with this email may already exist (e.g. a password account
        # or a different federated identity). Only auto-link to it when the IdP
        # has VERIFIED the email — otherwise an attacker who registers an
        # unverified IdP identity using a victim's email address could take
        # over the victim's account.
        existing_by_email = await iam_service.get_user_by_login(session, email)
        if existing_by_email is not None:
            if not email_verified:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "An account with this email already exists, but the identity "
                        "provider did not verify the email address. Sign in with your "
                        "existing credentials and link this provider from account settings."
                    ),
                )
            user = existing_by_email

    if user is None:
        # Create new federated user (no password)
        user = IAMUser(
            email=email,
            first_name=first_name,
            last_name=last_name,
            federated_provider=provider,
            federated_sub=sub,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info("auth.federation.user_created", email=email, provider=provider)

        # Auto-assign default_role if the provider YAML specifies one.
        # This prevents new federated users from arriving with zero permissions.
        _default_role_name: str | None = (
            _FED_REG.get(provider, {}).get("spec", {}).get("default_role")
        )
        if _default_role_name:
            default_role_result = await session.exec(
                select(IAMRole).where(IAMRole.name == _default_role_name)
            )
            default_role = default_role_result.first()
            if default_role:
                session.add(IAMUserRole(user_id=user.id, role_id=default_role.id))
                await session.commit()
                logger.info(
                    "Auto-assigned role '%s' to new federated user '%s'.",
                    _default_role_name,
                    email,
                )
            else:
                logger.warning(
                    "default_role '%s' not found in DB — skipping auto-assignment for '%s'.",
                    _default_role_name,
                    email,
                )
    else:
        # Stitch federation info onto existing account if missing
        if user.federated_provider is None:
            user.federated_provider = provider
            user.federated_sub = sub
            await session.commit()

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled.")

    tuvl_token = await iam_service.build_token_for_user(session, user)
    logger.info("auth.federation.login", email=email, provider=provider)
    return tuvl_token


@router.get(
    "/oauth/{provider}/start",
    summary="Redirect to an OAuth2 provider for federated login",
    response_class=RedirectResponse,
)
async def oauth_start(provider: str) -> RedirectResponse:
    """Redirect the browser to the OAuth2 provider's authorization page.

    Supported providers: ``google``, ``github``, ``microsoft``.
    """
    urls, client_id, _ = _provider_config(provider)
    state = await oauth_state_store.new_state(provider)
    redirect_uri = f"{settings.tuvl_oauth_base_url}/auth/oauth/{provider}/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": urls["scope"],
        "state": state,
    }
    qs = urlencode(params, quote_via=quote_plus)
    return RedirectResponse(url=f"{urls['auth_url']}?{qs}", status_code=302)


@router.get(
    "/oauth/{provider}/callback",
    response_model=TokenResponse,
    summary="Handle OAuth2 provider callback and return an access token",
)
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    session: IamSession,
) -> TokenResponse | RedirectResponse:
    """Exchange authorization code for user profile, upsert the IAM user,
    and return an access token with the same claims as a password-grant token.
    """
    if not await oauth_state_store.consume_state(state, provider):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")

    urls, client_id, client_secret = _provider_config(provider)
    redirect_uri = f"{settings.tuvl_oauth_base_url}/auth/oauth/{provider}/callback"

    client = get_http_client(timeout=15.0)
    token_resp = await client.post(
        urls["token_url"],
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
    )

    if token_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to exchange code with provider.")

    token_data: dict[str, Any] = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="Provider returned no access_token.")

    tuvl_token = await _login_with_access_token(provider, urls, access_token, session)

    # If a UI redirect URL is configured, send the browser back with the token
    # as a query param; otherwise the callback returns JSON.
    if settings.tuvl_oauth_ui_redirect_url:
        # a fragment is never sent to a server or written to access logs
        redirect_url = f"{settings.tuvl_oauth_ui_redirect_url}#{urlencode({'token': tuvl_token})}"
        return RedirectResponse(url=redirect_url, status_code=302)

    return TokenResponse(access_token=tuvl_token)


_DEVICE_URLS: dict[str, str] = {"github": "https://github.com/login/device/code"}
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceStartResponse(BaseModel):
    handle: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


class DevicePollRequest(BaseModel):
    handle: str = Field(min_length=16, max_length=128)


def _device_url(provider: str) -> str:
    from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY

    entry = FEDERATION_REGISTRY.get(provider)
    kind = (entry or {}).get("spec", {}).get("provider", provider)
    url = _DEVICE_URLS.get(kind)
    if url is None:
        raise HTTPException(
            status_code=404, detail=f"Device login is not supported for '{provider}'."
        )
    return url


@router.post(
    "/oauth/{provider}/device/start",
    response_model=DeviceStartResponse,
    summary="Start a device-flow login for a CLI or other headless client",
)
async def oauth_device_start(provider: str) -> DeviceStartResponse:
    """Begin GitHub's device authorization. The device code never leaves the server."""
    device_url = _device_url(provider)
    urls, client_id, _ = _provider_config(provider)
    client = get_http_client(timeout=15.0)
    resp = await client.post(
        device_url,
        data={"client_id": client_id, "scope": urls["scope"]},
        headers={"Accept": "application/json"},
    )
    data: dict[str, Any] = resp.json() if resp.status_code == 200 else {}
    if not data.get("device_code") or not data.get("user_code"):
        raise HTTPException(
            status_code=502,
            detail=(
                "The provider refused the device request. Enable Device Flow on the "
                "OAuth app's settings page."
            ),
        )
    interval = int(data.get("interval", 5))
    expires_in = int(data.get("expires_in", 900))
    handle = await oauth_state_store.new_device(
        provider, {"device_code": data["device_code"], "interval": interval}, ttl=expires_in
    )
    return DeviceStartResponse(
        handle=handle,
        user_code=data["user_code"],
        verification_uri=data.get("verification_uri", "https://github.com/login/device"),
        interval=interval,
        expires_in=expires_in,
    )


@router.post(
    "/oauth/{provider}/device/poll",
    response_model=TokenResponse,
    summary="Poll a device-flow login; 428 while the user has not approved yet",
)
async def oauth_device_poll(
    provider: str, body: DevicePollRequest, session: IamSession
) -> TokenResponse:
    pending = await oauth_state_store.get_device(body.handle, provider)
    if pending is None:
        raise HTTPException(status_code=400, detail="Unknown or expired device login.")
    urls, client_id, _ = _provider_config(provider)
    client = get_http_client(timeout=15.0)
    resp = await client.post(
        urls["token_url"],
        data={
            "client_id": client_id,
            "device_code": pending["device_code"],
            "grant_type": _DEVICE_GRANT,
        },
        headers={"Accept": "application/json"},
    )
    data: dict[str, Any] = resp.json() if resp.status_code == 200 else {}
    error = data.get("error")
    if error in ("authorization_pending", "slow_down"):
        raise HTTPException(
            status_code=428,
            detail=error,
            headers={
                "Retry-After": str(int(pending["interval"]) + (5 if error == "slow_down" else 0))
            },
        )
    if error or not data.get("access_token"):
        await oauth_state_store.drop_device(body.handle)
        raise HTTPException(status_code=400, detail=f"Device login failed: {error or 'no token'}.")
    await oauth_state_store.drop_device(body.handle)
    tuvl_token = await _login_with_access_token(provider, urls, data["access_token"], session)
    return TokenResponse(access_token=tuvl_token)


# These endpoints manage the YAML files in the project's federation/ directory.
# They require the ``iam:admin`` scope and replace the dev-file-bridge paths
# (/dev/files/federation/*) for production deployments where /dev is not mounted.
#
# The path-sanitization regex, threadpool file I/O, and registry-reload logic
# live once in iam_service —
# see iam_service.safe_federation_path / list_federation_providers / etc.


@iam_router.get(
    "/admin/federation",
    response_model=list[str],
    summary="List configured federation providers",
)
async def list_federation_providers(_admin: AdminUser) -> list[str]:
    """Return the names (without .yaml extension) of all configured federation providers."""
    return await iam_service.list_federation_providers()


@iam_router.get(
    "/admin/federation/{name}",
    summary="Get a federation provider config",
)
async def get_federation_provider(name: str, _admin: AdminUser) -> dict:
    try:
        return await iam_service.get_federation_provider(name)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@iam_router.put(
    "/admin/federation/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Save (create or update) a federation provider config",
)
async def save_federation_provider(name: str, body: dict, _admin: AdminUser) -> None:
    try:
        await iam_service.save_federation_provider(name, body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@iam_router.delete(
    "/admin/federation/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a federation provider config",
)
async def delete_federation_provider(name: str, _admin: AdminUser) -> None:
    try:
        await iam_service.delete_federation_provider(name)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
