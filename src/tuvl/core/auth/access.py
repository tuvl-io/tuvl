"""FastAPI dependencies for token authentication and authorisation.

Tokens are Ed25519-signed JWTs (:mod:`tuvl.core.auth.token`).

    OAuth2PasswordBearer (header extraction)
        └── verify_token(token) -> claims
                └── get_current_user(claims) -> TokenUser
                        └── require_access(scope, groups) -> TokenUser  [guard]

- ``verify_token``     — verifies the signature, issuer, expiry, the revocation list and
                         the user's session end; returns the claims.
- ``get_current_user`` — the claims as a ``TokenUser`` (shape-checked).
- ``require_access`` / ``require_scope`` / ``require_groups`` — 403 unless the token
  carries the scope and/or one of the groups (``iam:admin`` passes everything).
"""

from __future__ import annotations

import hmac
import os
import sys
import time
import uuid
from collections.abc import Callable
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer

# get_key_manager (not the key_manager attribute) so importing this module never
# constructs the key pair — that needs TUVL_SIGNING_KEY outside dev mode and
# belongs at the first verify, not at import.
from tuvl.core.auth.keys import get_key_manager
from tuvl.core.auth.schemas import SCOPE_NAME_RE, TokenUser
from tuvl.core.auth.token import ALGORITHM, ISSUER
from tuvl.core.logger import logger as _auth_logger

#: Verified token claims (see :mod:`tuvl.core.auth.token`).
Claims = dict[str, Any]


# Boot-time refusal: prevent the dev-mode superuser bypass from being silently
# active in production due to a leaked TUVL_DEV_MODE env var.  Both checks
# sys.exit(1) so a misconfigured boot cannot continue past import.
def _enforce_dev_mode_sentinel() -> None:
    from tuvl.core.config import settings as _settings

    if not _settings.tuvl_dev_mode:
        return

    # Layer 1: production + dev_mode is *never* allowed, ack flag or not.
    if _settings.is_production:
        _auth_logger.critical(
            "🚨 TUVL_DEV_MODE=true is set in a TUVL_ENV=production process. "
            "The dev-mode auth shortcut grants iam:admin to anyone with "
            "TUVL_DEV_API_KEY and must NEVER be active in production. "
            "Unset TUVL_DEV_MODE (or set TUVL_ENV=development for local work)."
        )
        sys.exit(1)

    # Layer 2: outside production, still require explicit acknowledgement.
    sentinel = os.environ.get("TUVL_ALLOW_DEV_AUTH", "").strip().lower()
    if sentinel == "true":
        return

    _auth_logger.critical(
        "🚨 TUVL_DEV_MODE is enabled without TUVL_ALLOW_DEV_AUTH=true. "
        "The dev-mode auth shortcut (TUVL_DEV_API_KEY superuser bypass) "
        "is a development-only feature and must be explicitly acknowledged. "
        "Set TUVL_ALLOW_DEV_AUTH=true to enable, or unset TUVL_DEV_MODE for production."
    )
    sys.exit(1)


_enforce_dev_mode_sentinel()

# OAuth2PasswordBearer declares the /token endpoint for OpenAPI docs and
# extracts the raw token string from "Authorization: Bearer <token>".
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

# HTTPBearer adds a "Bearer Token" field to the Swagger UI Authorize modal.
_bearer_scheme = HTTPBearer(bearerFormat="JWT", auto_error=False)

# Dev superuser — granted when the dev API key is used in dev mode.
_DEV_SCOPES = ["iam:admin"]
# Stable synthetic UUID: rows keyed by user_id (runs, approvals) are UUID-typed,
# so the dev principal must be one — and a constant, so audit rows correlate.
_DEV_USER_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "dev.tuvl.internal"))
_DEV_GROUP = "dev"


def dev_claims() -> Claims:
    """The claims of the synthetic dev superuser (dev mode's API key)."""
    return {
        "iss": ISSUER,
        "sub": _DEV_USER_ID,
        "groups": [_DEV_GROUP],
        "scopes": list(_DEV_SCOPES),
        "iat": time.time(),
    }


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def decode_token(token: str) -> Claims:
    """Check *token*'s signature (EdDSA only — no ``none``, no HMAC with the public
    key), issuer, required claims and expiry; return its claims. Raises PyJWT's
    ``InvalidTokenError`` (``ExpiredSignatureError`` when expired). Doesn't
    consult the revocation list or session ends — :func:`verify_bearer_token` does."""
    keys = get_key_manager()
    claims: Claims = jwt.decode(
        token,
        keys.verification_key(jwt.get_unverified_header(token).get("kid")),
        algorithms=[ALGORITHM],
        issuer=ISSUER,
        options={"require": ["iss", "sub", "iat"]},
    )
    return claims


async def verify_token(
    oauth_token: Annotated[str | None, Depends(_oauth2_scheme)] = None,
    bearer_token: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> Claims:
    """Verify the bearer token and return its claims (FastAPI dependency).

    In dev mode the dev API key is also accepted, as the synthetic superuser.
    """
    token = oauth_token or (bearer_token.credentials if bearer_token else None)
    return await verify_bearer_token(token)


async def verify_bearer_token(token: str | None) -> Claims:
    """Verify a raw bearer token; return its claims or raise 401.

    Checks, in order: the signature (EdDSA only — no ``none``, no HMAC with the
    public key), the issuer, required claims, expiry, the revocation list, and
    whether the token predates its user's last session end.
    """
    if not token:
        raise _unauthorized("Not authenticated")
    from tuvl.core.config import settings

    if (
        settings.tuvl_dev_mode
        and settings.tuvl_dev_api_key
        and hmac.compare_digest(token, settings.tuvl_dev_api_key)
    ):
        return dev_claims()

    try:
        claims = decode_token(token)
    except jwt.ExpiredSignatureError as exc:
        _auth_logger.warning("auth.verify_token.rejected", reason="expired")
        raise _unauthorized("Token has expired or failed authorization checks.") from exc
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("Invalid or expired token.") from exc

    from tuvl.core.auth.blacklist import SessionStoreUnavailableError, token_blacklist

    # revoked (logout, refresh), or issued before the user's sessions ended (deactivated, deleted,
    # a role or scope taken away): see tuvl.core.auth.blacklist for where that is kept
    try:
        refused = await token_blacklist.refusal(token, claims)
    except SessionStoreUnavailableError as exc:
        _auth_logger.error("auth.session_store.unavailable", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sessions cannot be checked right now.",
        ) from exc
    if refused:
        raise _unauthorized(refused)
    return claims


def _str_list(claims: Claims, key: str) -> list[str]:
    value = claims.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _unauthorized(f"Token claim '{key}' must be a list of strings.")
    return value


def user_from_claims(claims: Claims) -> TokenUser:
    """The principal in verified *claims*; 401 when a claim has the wrong shape."""
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise _unauthorized("Token is missing its subject.")
    tenant = claims.get("tenant")
    if tenant is not None:
        from tuvl.core.datasources.postgres import _TENANT_ID_RE

        # defence in depth: the same allowlist the database session enforces
        if not isinstance(tenant, str) or not _TENANT_ID_RE.fullmatch(tenant):
            raise _unauthorized("Token carries a malformed tenant claim.")
    return TokenUser(
        user_id=subject,
        groups=_str_list(claims, "groups"),
        scopes=_str_list(claims, "scopes"),
        tenant_id=tenant,
    )


def get_current_user(claims: Annotated[Claims, Depends(verify_token)]) -> TokenUser:
    """The authenticated principal (FastAPI dependency)."""
    return user_from_claims(claims)


async def bind_principal_context(
    user: Annotated[TokenUser, Depends(get_current_user)],
) -> TokenUser:
    """FastAPI dependency that binds the authenticated principal into
    request-scoped ContextVars.

    MUST stay ``async``: FastAPI runs sync dependencies in the threadpool,
    where ContextVar writes land in the worker thread's copied context and
    never reach the endpoint. As a coroutine it runs in the request task,
    so the vars below are visible downstream.

    * ``tuvl.core.context.current_user_id`` — the verified user id, so
      structlog and OTel spans annotate every record/span with the caller.
    * ``tuvl.core.context.current_tenant_id`` — from the token's ``tenant``
      claim when present; in multi-tenant deployments the database session's
      ``after_begin`` hook binds it into ``SET LOCAL``.
    """
    from tuvl.core.context import current_tenant_id, current_user_id

    current_user_id.set(user.user_id)
    if user.tenant_id:
        current_tenant_id.set(user.tenant_id)
    return user


class TokenUnauthorizedError(Exception):
    """A token could not be authorized at all. Callers map this to HTTP 401."""


class TokenForbiddenError(Exception):
    """An authenticated token lacks the required scope/group. Callers map this to
    HTTP 403. ``detail`` is safe to surface."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def authorize_token(
    claims: Claims,
    *,
    required_scope: str | None = None,
    required_groups: list[str] | None = None,
) -> TokenUser:
    """Authorize verified *claims* against a scope and/or group requirement.

    A scope AND a group are both required when both are given (not OR);
    ``iam:admin`` satisfies every requirement.
    """
    try:
        user = user_from_claims(claims)
    except HTTPException as exc:
        raise TokenUnauthorizedError(str(exc.detail)) from exc
    if "iam:admin" in user.scopes:  # superuser bypass
        return user
    if required_scope and required_scope not in user.scopes:
        raise TokenForbiddenError(f"Access denied. Required scope: '{required_scope}'.")
    if required_groups and not set(required_groups) & set(user.groups):
        raise TokenForbiddenError(f"Access denied. Required group(s): {required_groups}.")
    return user


def require_access(
    required_scope: str | None = None,
    required_groups: list[str] | None = None,
) -> Callable[..., TokenUser]:
    """Dependency factory enforcing a scope and/or group membership.

    Scope AND group are both required when both are given; ``iam:admin``
    bypasses everything. Names are validated when the guard is declared.
    """
    names = ([required_scope] if required_scope else []) + list(required_groups or [])
    for name in names:
        if not SCOPE_NAME_RE.match(name):
            raise ValueError(
                f"Invalid scope/group '{name}'. Only word chars, ':', '.', '-', '/' are allowed."
            )

    def _dependency(claims: Annotated[Claims, Depends(verify_token)]) -> TokenUser:
        try:
            return authorize_token(
                claims, required_scope=required_scope, required_groups=required_groups
            )
        except TokenForbiddenError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail) from exc
        except TokenUnauthorizedError as exc:
            raise _unauthorized(str(exc)) from exc

    return _dependency


def require_groups(allowed_groups: list[str]) -> Callable[..., TokenUser]:
    """Dependency factory: 403 unless the token carries one of *allowed_groups*."""
    return require_access(required_groups=allowed_groups)


def require_scope(required_scope: str):  # noqa: ANN201
    """Dependency factory: 403 unless the token carries *required_scope*."""
    return require_access(required_scope=required_scope)
