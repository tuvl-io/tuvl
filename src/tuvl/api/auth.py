"""Principal resolution for the 2.0 REST surface.

Auth requirements here depend on the target (a workflow's trigger policy,
the run's owner), so routes resolve the bearer token themselves instead of
declaring a fixed FastAPI dependency.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status

from tuvl.contract.schema import WorkflowDocument
from tuvl.core.auth.schemas import TokenUser
from tuvl.core.auth.workflow_policy import WorkflowAuthPolicy

ADMIN = "iam:admin"

#: ``user`` argument of a start function: the caller has not been authenticated yet
UNSET: Any = object()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def current_user(request: Request) -> TokenUser | None:
    """The verified principal, or ``None`` when the request carries no token.
    A token that is present but invalid is always a 401."""
    from tuvl.core.auth.access import get_current_user, verify_bearer_token
    from tuvl.core.context import current_tenant_id, current_user_id

    token = _bearer(request)
    if token is None:
        return None
    user = get_current_user(await verify_bearer_token(token))
    current_user_id.set(user.user_id)
    if user.tenant_id:
        current_tenant_id.set(user.tenant_id)
    return user


def has_scope(user: TokenUser | None, scope: str) -> bool:
    return user is not None and (ADMIN in user.scopes or scope in user.scopes)


def require(user: TokenUser | None, scope: str) -> TokenUser:
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not has_scope(user, scope):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Access denied. Required scope: '{scope}'.")
    return user


def workflow_policy(doc: WorkflowDocument) -> WorkflowAuthPolicy:
    http = doc.spec.trigger.active_http
    return WorkflowAuthPolicy(
        public=bool(http and http.public),
        required_scope=doc.metadata.required_scope,
        required_group=doc.metadata.required_group,
    )


def run_denial(user: TokenUser | None, doc: WorkflowDocument) -> tuple[int, str] | None:
    """Why *user* may not start *doc* — ``(http status, message)`` — or ``None``:
    default-deny, ``public: true`` opt-in, and the workflow's
    ``required_scope``/``required_group`` (``iam:admin`` bypasses)."""
    policy = workflow_policy(doc)
    if policy.anonymous_ok():
        return None
    if user is None:
        return status.HTTP_401_UNAUTHORIZED, "Not authenticated"
    if ADMIN in user.scopes:
        return None
    if policy.required_scope and policy.required_scope not in user.scopes:
        return (
            status.HTTP_403_FORBIDDEN,
            f"Access denied. Required scope: '{policy.required_scope}'.",
        )
    if policy.required_group and policy.required_group not in user.groups:
        return (
            status.HTTP_403_FORBIDDEN,
            f"Access denied. Required group: '{policy.required_group}'.",
        )
    return None


async def trigger_user(request: Request, doc: WorkflowDocument) -> TokenUser | None:
    """Who may start *doc* (see :func:`run_denial`)."""
    user = await current_user(request)
    denial = run_denial(user, doc)
    if denial is not None:
        code, message = denial
        headers = {"WWW-Authenticate": "Bearer"} if code == status.HTTP_401_UNAUTHORIZED else None
        raise HTTPException(code, message, headers=headers)
    return user
