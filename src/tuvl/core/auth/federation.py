"""Federated login: OAuth 2.0 authorization-code and device flows, and federation-provider admin.

Google, GitHub and Microsoft sign in through ``GET /auth/oauth/{provider}/start`` and ``/callback``;
GitHub also offers a device flow for CLIs (``POST /auth/oauth/{provider}/device/start`` and ``/poll``).
Provider settings come from the project's ``federation/*.yaml`` files (``FEDERATION_REGISTRY``) or, for
the built-in providers, from ``TUVL_OAUTH_*`` settings. ``router`` belongs to the ``auth`` API group;
``admin_router`` (``/auth/admin/federation``) to the ``iam`` group.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any
from urllib.parse import quote_plus, urlencode

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth.iam_service import NotFoundError, ParseError, ValidationError
from tuvl.core.auth.models import IAMRole, IAMUser, IAMUserRole
from tuvl.core.auth.router import AdminUser, IamSession, TokenResponse
from tuvl.core.auth.state_store import oauth_state_store
from tuvl.core.config import settings
from tuvl.core.infra.http_client import get_http_client
from tuvl.core.logger import logger

router = APIRouter(prefix="/auth", tags=["auth"])
admin_router = APIRouter(prefix="/auth", tags=["iam"])


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
        "userinfo_url": "https://graph.microsoft.com/oidc/userinfo",
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


#: Cookie that ties a browser sign-in to the browser that started it (holds a hash of the state).
STATE_COOKIE = "tuvl_oauth_state"
_STATE_COOKIE_PATH = "/auth/oauth"
#: Providers that take PKCE unless the federation file says ``pkce: false``.
_PKCE_BY_DEFAULT = frozenset({"google", "microsoft"})


def _provider_type(provider: str) -> str:
    """What kind of provider *provider* is (``google``, ``github``, ``microsoft``, ``custom``): its
    federation file may give it another name."""
    from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY

    entry = FEDERATION_REGISTRY.get(provider)
    return str((entry or {}).get("spec", {}).get("provider", provider))


def _pkce_enabled(provider: str) -> bool:
    from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY

    spec = (FEDERATION_REGISTRY.get(provider) or {}).get("spec", {})
    return bool(spec["pkce"]) if "pkce" in spec else _provider_type(provider) in _PKCE_BY_DEFAULT


def _state_binding(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


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

    provider_type = _provider_type(provider)

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
    if not email and provider_type == "microsoft":
        # Graph's /me has no ``email``: the mailbox address is ``mail``; a work or school account without
        # one is still signed in as its principal name, when that is an address.
        principal = profile.get("userPrincipalName")
        email = profile.get("mail") or (
            principal if isinstance(principal, str) and "@" in principal else None
        )
    first_name: str | None = (
        profile.get("given_name") or profile.get("first_name") or profile.get("givenName")
    )
    last_name: str | None = (
        profile.get("family_name") or profile.get("last_name") or profile.get("surname")
    )
    if not first_name and not last_name and profile.get("name"):
        parts = profile["name"].split(" ", 1)
        first_name = parts[0]
        if len(parts) > 1:
            last_name = parts[1]
    if not email and provider_type == "github":
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
                    "auth.federation.domain_refused", domain=_email_domain, provider=provider
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
        logger.info("auth.federation.user_created", user_id=str(user.id), provider=provider)

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
                    "auth.federation.role_assigned",
                    role=_default_role_name,
                    user_id=str(user.id),
                )
            else:
                logger.warning(
                    "auth.federation.default_role_missing",
                    role=_default_role_name,
                    user_id=str(user.id),
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
    logger.info("auth.federation.login", user_id=str(user.id), provider=provider)
    return tuvl_token


@router.get(
    "/oauth/{provider}/start",
    summary="Redirect to an OAuth2 provider for federated login",
    response_class=RedirectResponse,
)
async def oauth_start(provider: str, session: IamSession) -> RedirectResponse:
    """Redirect the browser to the OAuth2 provider's authorization page.

    Supported providers: ``google``, ``github``, ``microsoft``. The state is kept for the callback (with a
    PKCE verifier where the provider takes one) and a cookie ties it to this browser, so a link someone else
    started cannot be finished here.
    """
    urls, client_id, _ = _provider_config(provider)
    verifier = challenge = None
    if _pkce_enabled(provider):
        verifier, challenge = _pkce_pair()
    state = await oauth_state_store.new_state(
        provider, {"verifier": verifier} if verifier else {}, session=session
    )
    redirect_uri = f"{settings.tuvl_oauth_base_url}/auth/oauth/{provider}/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": urls["scope"],
        "state": state,
    }
    if challenge:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    qs = urlencode(params, quote_via=quote_plus)
    response = RedirectResponse(url=f"{urls['auth_url']}?{qs}", status_code=302)
    if settings.tuvl_oauth_state_cookie:
        response.set_cookie(
            STATE_COOKIE,
            _state_binding(state),
            max_age=600,
            path=_STATE_COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=settings.tuvl_oauth_base_url.startswith("https://"),
        )
    return response


@router.get(
    "/oauth/{provider}/callback",
    response_model=TokenResponse,
    summary="Handle OAuth2 provider callback and return an access token",
)
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    request: Request,
    response: Response,
    session: IamSession,
) -> TokenResponse | RedirectResponse:
    """Exchange authorization code for user profile, upsert the IAM user,
    and return an access token with the same claims as a password-grant token.
    """
    payload = await oauth_state_store.consume_state(state, provider, session=session)
    if payload is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state.")
    if settings.tuvl_oauth_state_cookie and not hmac.compare_digest(
        request.cookies.get(STATE_COOKIE, ""), _state_binding(state)
    ):
        logger.warning("auth.federation.state_cookie_mismatch", provider=provider)
        raise HTTPException(status_code=400, detail="This sign-in was not started in this browser.")

    urls, client_id, client_secret = _provider_config(provider)
    redirect_uri = f"{settings.tuvl_oauth_base_url}/auth/oauth/{provider}/callback"

    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if payload.get("verifier"):
        form["code_verifier"] = payload["verifier"]
    client = get_http_client(timeout=15.0)
    token_resp = await client.post(
        urls["token_url"], data=form, headers={"Accept": "application/json"}
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
        redirect = RedirectResponse(url=redirect_url, status_code=302)
        redirect.delete_cookie(STATE_COOKIE, path=_STATE_COOKIE_PATH)
        return redirect

    response.delete_cookie(STATE_COOKIE, path=_STATE_COOKIE_PATH)
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
async def oauth_device_start(provider: str, session: IamSession) -> DeviceStartResponse:
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
        provider,
        {"device_code": data["device_code"], "interval": interval},
        ttl=expires_in,
        session=session,
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
    pending = await oauth_state_store.get_device(body.handle, provider, session=session)
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
        await oauth_state_store.drop_device(body.handle, session=session)
        raise HTTPException(status_code=400, detail=f"Device login failed: {error or 'no token'}.")
    await oauth_state_store.drop_device(body.handle, session=session)
    tuvl_token = await _login_with_access_token(provider, urls, data["access_token"], session)
    return TokenResponse(access_token=tuvl_token)


# These endpoints manage the YAML files in the project's federation/ directory.
# They require the ``iam:admin`` scope and replace the dev-file-bridge paths
# (/dev/files/federation/*) for production deployments where /dev is not mounted.
#
# The path-sanitization regex, threadpool file I/O, and registry-reload logic
# live once in iam_service —
# see iam_service.safe_federation_path / list_federation_providers / etc.


@admin_router.get(
    "/admin/federation",
    response_model=list[str],
    summary="List configured federation providers",
)
async def list_federation_providers(_admin: AdminUser) -> list[str]:
    """Return the names (without .yaml extension) of all configured federation providers."""
    return await iam_service.list_federation_providers()


@admin_router.get(
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


@admin_router.put(
    "/admin/federation/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Save (create or update) a federation provider config",
)
async def save_federation_provider(name: str, body: dict, _admin: AdminUser) -> None:
    try:
        await iam_service.save_federation_provider(name, body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@admin_router.delete(
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
