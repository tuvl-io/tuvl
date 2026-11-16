"""Durable run storage (spec §14.2): runs, their append-only event journal,
per-agent checkpoints, and pending approvals. All four live on the primary
datasource and are tenant-scoped in multi-tenant mode."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from tuvl.core.models.base import DynamicTenantModel

#: JSONB on Postgres, plain JSON elsewhere (unit tests run on SQLite).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

RunStatus = Literal[
    "queued",
    "running",
    "waiting_human",
    "waiting_approval",
    "paused",
    "completed",
    "failed",
    "aborted",
    "compensating",
    "compensated",
]
RUN_STATUSES: tuple[str, ...] = RunStatus.__args__  # type: ignore[attr-defined]
#: A run in one of these states never runs again.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "aborted", "compensated"})

RunTrigger = Literal["http", "api", "schedule", "mcp", "child", "test"]
ApprovalKind = Literal["human", "tool_call"]
ApprovalStatus = Literal["pending", "approved", "edited", "rejected", "expired"]


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(**kw: Any) -> Any:
    return Field(
        default_factory=_now, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, **kw)
    )


def _ts_opt(**kw: Any) -> Any:
    return Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True, **kw))


class SystemRun(DynamicTenantModel, table=True):
    """One execution of a workflow — the unit the queue leases to a worker."""

    __tablename__ = "tuvl_system_runs"
    __table_args__ = (
        sa.Index("ix_tuvl_runs_claim", "status", "lease_expires_at"),
        sa.Index("ix_tuvl_runs_workflow_created", "workflow", "created_at"),
        # the queue: oldest queued runs first, without sorting the backlog
        sa.Index(
            "ix_tuvl_runs_queued_created",
            "created_at",
            postgresql_where=sa.text("status = 'queued'"),
            sqlite_where=sa.text("status = 'queued'"),
        ),
        # GET /api/runs: a caller's newest runs, and an administrator's
        sa.Index("ix_tuvl_runs_principal_created", "principal", sa.text("created_at DESC")),
        sa.Index("ix_tuvl_runs_created", sa.text("created_at DESC")),
        # one run per scheduled fire (the trigger names the fire time and tenant)
        sa.Index(
            "uq_tuvl_runs_schedule",
            "workflow",
            "trigger",
            unique=True,
            postgresql_where=sa.text("trigger LIKE 'schedule:%'"),
            sqlite_where=sa.text("trigger LIKE 'schedule:%'"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workflow: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    #: sha256 of the workflow document the run started with (replays and
    #: resumes must run the same graph).
    workflow_hash: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    status: str = Field(default="queued", sa_column=sa.Column(sa.String, nullable=False))
    trigger: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    principal: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    parent_run_id: uuid.UUID | None = Field(
        default=None, sa_column=sa.Column(sa.Uuid, nullable=True, index=True)
    )
    current_agent: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    end_name: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    input: dict[str, Any] = Field(default_factory=dict, sa_column=sa.Column(_JSON, nullable=False))
    #: The rendered end: ``{"status": int, "body": ...}``.
    output: dict[str, Any] | None = Field(default=None, sa_column=sa.Column(_JSON, nullable=True))
    #: ``{"error_type", "message", "agent_id"}`` when the run failed.
    error: dict[str, Any] | None = Field(default=None, sa_column=sa.Column(_JSON, nullable=True))
    #: Operator / supervisor requests read by the runner at agent and turn
    #: boundaries: ``{"action": "pause" | "abort" | "steer", ...}``.
    control: dict[str, Any] | None = Field(default=None, sa_column=sa.Column(_JSON, nullable=True))
    tokens_used: int = Field(
        default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0")
    )
    #: Next journal sequence number — allocated with UPDATE … RETURNING so
    #: concurrent writers can never collide.
    next_seq: int = Field(
        default=1, sa_column=sa.Column(sa.Integer, nullable=False, server_default="1")
    )
    #: How many times a worker has claimed this run (crash recovery re-claims).
    claims: int = Field(
        default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0")
    )
    lease_owner: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    lease_expires_at: datetime | None = _ts_opt()
    created_at: datetime = _ts()
    updated_at: datetime = _ts()
    started_at: datetime | None = _ts_opt()
    finished_at: datetime | None = _ts_opt()


class SystemRunEvent(DynamicTenantModel, table=True):
    """Append-only journal entry (spec Appendix C.3 event types)."""

    __tablename__ = "tuvl_system_run_events"

    run_id: uuid.UUID = Field(sa_column=sa.Column(sa.Uuid, primary_key=True))
    seq: int = Field(sa_column=sa.Column(sa.Integer, primary_key=True, autoincrement=False))
    type: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    agent_id: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=sa.Column(_JSON, nullable=False)
    )
    tokens: int = Field(
        default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0")
    )
    latency_ms: float | None = Field(default=None, sa_column=sa.Column(sa.Float, nullable=True))
    created_at: datetime = _ts()


class SystemRunCheckpoint(DynamicTenantModel, table=True):
    """Run context after a completed agent — the resume point after a pause
    or a crash. Stored unredacted, since it is the resume state; secure fields
    are redacted when a run is exported."""

    __tablename__ = "tuvl_system_run_checkpoints"

    run_id: uuid.UUID = Field(sa_column=sa.Column(sa.Uuid, primary_key=True))
    seq: int = Field(sa_column=sa.Column(sa.Integer, primary_key=True, autoincrement=False))
    agent_id: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    signal: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    #: The agent or ``END.<name>`` the run continues at.
    next: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    context: dict[str, Any] = Field(
        default_factory=dict, sa_column=sa.Column(_JSON, nullable=False)
    )
    #: Agent ids completed so far, in order — the compensation stack.
    completed: list[str] = Field(default_factory=list, sa_column=sa.Column(_JSON, nullable=False))
    created_at: datetime = _ts()


class SystemApproval(DynamicTenantModel, table=True):
    """A pending human decision: a ``human`` agent's form, or an in-loop tool
    call that needs approval (spec §9.6, §11.4)."""

    __tablename__ = "tuvl_system_approvals"
    __table_args__ = (sa.Index("ix_tuvl_approvals_status", "status", "created_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(sa_column=sa.Column(sa.Uuid, nullable=False, index=True))
    agent_id: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    kind: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    #: What the approver sees: the form and shown inputs, or the tool call.
    payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=sa.Column(_JSON, nullable=False)
    )
    required_group: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    requested_by: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    status: str = Field(default="pending", sa_column=sa.Column(sa.String, nullable=False))
    decision: dict[str, Any] | None = Field(default=None, sa_column=sa.Column(_JSON, nullable=True))
    decided_by: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    decided_at: datetime | None = _ts_opt()
    expires_at: datetime | None = _ts_opt()
    created_at: datetime = _ts()


class SystemWebhookDelivery(DynamicTenantModel, table=True):
    """One notification owed to one declared webhook (a transactional outbox: written with the
    journal event, delivered later). It holds no URL, secret or run data — the destination is read
    from the workflow's current declaration when it is sent."""

    __tablename__ = "tuvl_system_webhook_deliveries"
    __table_args__ = (sa.Index("ix_tuvl_webhooks_due", "status", "next_attempt_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    run_id: uuid.UUID = Field(sa_column=sa.Column(sa.Uuid, nullable=False, index=True))
    workflow: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    webhook: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    type: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    seq: int = Field(sa_column=sa.Column(sa.Integer, nullable=False))
    agent_id: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    #: pending → delivered | dead
    status: str = Field(default="pending", sa_column=sa.Column(sa.String, nullable=False))
    attempts: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False))
    next_attempt_at: datetime = _ts()
    last_status: int | None = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    last_error: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
    delivered_at: datetime | None = _ts_opt()
    created_at: datetime = _ts()


class SystemRunDelta(DynamicTenantModel, table=True):
    """A piece of an agent's answer, written while the model is still producing it. Not part of the
    journal: it exists so another process can show a draft, and is removed a minute after the run ends."""

    __tablename__ = "tuvl_system_run_deltas"
    __table_args__ = (
        sa.Index("ix_tuvl_run_deltas_run", "run_id", "id"),
        sa.Index("ix_tuvl_run_deltas_created", "created_at"),
    )

    id: int | None = Field(
        default=None,
        sa_column=sa.Column(sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
    )
    run_id: uuid.UUID = Field(sa_column=sa.Column(sa.Uuid, nullable=False))
    agent_id: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    #: ``delta`` adds ``text`` to the draft; ``reset`` throws the draft away (the agent starts again).
    kind: str = Field(default="delta", sa_column=sa.Column(sa.String, nullable=False))
    text: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False))
    created_at: datetime = _ts()


RUNTIME_TABLES = (
    SystemRun,
    SystemRunEvent,
    SystemRunCheckpoint,
    SystemApproval,
    SystemWebhookDelivery,
    SystemRunDelta,
)
