"""IAM database schema — users, roles, scope assignments.

Tables
------
tuvl_system_iam_users       — end-user credentials (email + bcrypt hash OR federated sub).
tuvl_system_iam_roles       — named roles, e.g. "hr_manager", "recruiter".
tuvl_system_iam_user_roles  — many-to-many: which user holds which role.
tuvl_system_iam_role_scopes — which scopes a role grants, e.g. "requisition:write".

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

import sqlalchemy as sa
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
