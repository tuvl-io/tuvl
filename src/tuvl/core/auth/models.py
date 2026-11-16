"""IAM database schema — users, roles, scope assignments.

Tables
------
tuvl_system_iam_users       — end-user credentials (email + bcrypt hash OR federated sub).
tuvl_system_iam_roles       — named roles, e.g. "hr_manager", "recruiter".
tuvl_system_iam_user_roles  — many-to-many: which user holds which role.
tuvl_system_iam_role_scopes — which scopes a role grants, e.g. "requisition:write".
tuvl_system_iam_password_tokens — one-time links that let a user set their own password (hashed, expiring).
tuvl_system_iam_session_ends — when a user's sessions were last ended (tokens issued before are refused).
tuvl_system_iam_revoked_tokens — tokens refused until they would have expired anyway (logout, refresh).

Design notes
------------
- All PKs are UUID so records are safe to expose in tokens and APIs.
- ``hashed_password`` is nullable to support federated-only users.
- ``federated_provider`` / ``federated_sub`` support Google, GitHub, Microsoft.
- ``IAMUser.email`` is the canonical identity across both auth paths.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class IAMUser(SQLModel, table=True):
    """An end-user that can authenticate with tuvl.

    Either ``hashed_password`` (internal auth) or
    ``federated_provider`` + ``federated_sub`` (federation) must be set.
    Both may be set for a user that links both methods.
    """

    __tablename__ = "tuvl_system_iam_users"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
    )
    email: str = Field(
        sa_column=sa.Column(sa.String, unique=True, index=True, nullable=False),
    )
    phone_number: str | None = Field(
        default=None,
        sa_column=sa.Column(sa.String, unique=True, index=True, nullable=True),
    )
    first_name: str | None = Field(default=None)
    last_name: str | None = Field(default=None)
    hashed_password: str | None = Field(default=None)
    is_active: bool = Field(default=True)
    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow,
        sa_column=sa.Column(sa.DateTime, default=datetime.datetime.utcnow, nullable=False),
    )

    # Federation fields — set when the account was created via OAuth2.
    federated_provider: str | None = Field(default=None)
    federated_sub: str | None = Field(default=None)


class IAMRole(SQLModel, table=True):
    """A named role that bundles one or more access scopes.

    Examples: "hr_manager" (requisition:write, candidate:read),
              "recruiter"  (candidate:read, interview:read).
    """

    __tablename__ = "tuvl_system_iam_roles"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
    )
    name: str = Field(
        sa_column=sa.Column(sa.String, unique=True, index=True, nullable=False),
    )
    description: str | None = Field(default=None)
    #: A disabled role keeps its members and scopes but grants nothing and adds no group.
    is_active: bool = Field(
        default=True,
        sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.true()),
    )


class IAMUserRole(SQLModel, table=True):
    """Association table — which user holds which role (many-to-many)."""

    __tablename__ = "tuvl_system_iam_user_roles"

    user_id: uuid.UUID = Field(
        foreign_key="tuvl_system_iam_users.id",
        primary_key=True,
        ondelete="CASCADE",
    )
    role_id: uuid.UUID = Field(
        foreign_key="tuvl_system_iam_roles.id",
        primary_key=True,
        ondelete="CASCADE",
    )


class IAMRoleScope(SQLModel, table=True):
    """Maps a role to a single scope string.

    One row per scope per role, e.g.:
        role_id=<hr_manager>, scope_name="requisition:write"
        role_id=<hr_manager>, scope_name="candidate:read"
    """

    __tablename__ = "tuvl_system_iam_role_scopes"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
    )
    role_id: uuid.UUID = Field(
        sa_column=sa.Column(
            sa.Uuid,
            sa.ForeignKey("tuvl_system_iam_roles.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
    )
    scope_name: str = Field()


class IAMPasswordToken(SQLModel, table=True):
    """A one-time link to set a password. Only the SHA-256 of the token is kept; the token itself goes to the user."""

    __tablename__ = "tuvl_system_iam_password_tokens"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="tuvl_system_iam_users.id",
        ondelete="CASCADE",
        index=True,
    )
    token_hash: str = Field(sa_column=sa.Column(sa.String, unique=True, index=True, nullable=False))
    expires_at: datetime.datetime = Field(sa_column=sa.Column(sa.DateTime, nullable=False))
    used_at: datetime.datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime, nullable=True)
    )
    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow,
        sa_column=sa.Column(sa.DateTime, default=datetime.datetime.utcnow, nullable=False),
    )


class IAMLoginAttempt(SQLModel, table=True):
    """Failed password attempts per login, so a guesser is made to wait.

    Keyed by a hash of the normalised login rather than by user: an unknown login is slowed exactly like a
    known one, so the answer never says which logins exist. Nothing is stored until a login fails; a
    success deletes the row; ``locked_until`` is when the next attempt is judged again."""

    __tablename__ = "tuvl_system_iam_login_attempts"

    key: str = Field(primary_key=True)
    failures: int = Field(default=0)
    locked_until: datetime.datetime | None = Field(
        default=None, sa_column=sa.Column(sa.DateTime, nullable=True)
    )
    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.utcnow,
        sa_column=sa.Column(sa.DateTime, nullable=False, index=True),
    )


class IAMOAuthState(SQLModel, table=True):
    """What a provider sign-in keeps between its start and its end: a ``state`` (the CSRF token of an
    authorization-code sign-in, with its PKCE verifier) or a ``device`` handle. Single-use and short-lived.

    Used when no Redis datasource is deployed (see ``tuvl.core.auth.state_store``), so a callback that lands
    on another replica than the start still finds it."""

    __tablename__ = "tuvl_system_oauth_states"

    handle: str = Field(primary_key=True)
    kind: str = Field()
    provider: str = Field()
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=sa.Column(sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    )
    expires_at: datetime.datetime = Field(
        sa_column=sa.Column(sa.DateTime, nullable=False, index=True)
    )


class IAMSessionEnd(SQLModel, table=True):
    """When a user's sessions were last ended: a token issued before then is refused.

    Keyed by the token's subject, not by a foreign key, so it outlives a deleted user. Used when no Redis
    datasource is deployed (see ``tuvl.core.auth.blacklist``)."""

    __tablename__ = "tuvl_system_iam_session_ends"

    user_id: str = Field(primary_key=True)
    ended_at_ms: int = Field(sa_column=sa.Column(sa.BigInteger, nullable=False))


class IAMRevokedToken(SQLModel, table=True):
    """A token refused until it would have expired anyway (``expires_at_ms`` is null: never expires)."""

    __tablename__ = "tuvl_system_iam_revoked_tokens"

    jti: str = Field(primary_key=True)
    expires_at_ms: int | None = Field(
        default=None, sa_column=sa.Column(sa.BigInteger, nullable=True, index=True)
    )
