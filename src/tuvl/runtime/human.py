"""The ``human`` engine and approval decisions (spec §9.6).

First call: the engine opens an approval request (in the agent's transaction,
so it commits with the suspend checkpoint) and suspends the run as
``waiting_human``. :func:`decide` records a reviewer's form, validated
against the agent's outputs, and re-queues the run; the engine then runs
again for the same step, finds the decision and emits its signal —
``route_on`` maps a ``bool`` field to ``"true"``/``"false"`` and an ``enum``
field to its value. :func:`expire_due` closes requests past ``expires`` and
re-queues their runs, which then take the ``expired`` route.

Authorisation (spec §9.6, §16.5): the reviewer needs ``approvals:decide``.
With ``required_group`` they must also be in that group (``iam:admin``
bypasses both) and must never be the principal who triggered the run.
Without it only that principal or ``iam:admin`` decides.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tuvl.contract.facts import field_types
from tuvl.contract.schema import HumanAgent, WorkflowDocument, duration_seconds
from tuvl.contract.signals import is_reserved
from tuvl.contract.tel import compile_template
from tuvl.contract.types import ModelResolver, build_model, format_type, registry_resolver
from tuvl.runtime import journal, queue
from tuvl.runtime.engines import AgentResult, EngineContext, register_engine
from tuvl.runtime.models import SystemApproval, SystemRun

_APPROVALS = SystemApproval.__table__  # type: ignore[attr-defined]
ADMIN_SCOPE = "iam:admin"
DECIDE_SCOPE = "approvals:decide"


@dataclass(frozen=True)
class Reviewer:
    user_id: str | None
    groups: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_admin(self) -> bool:
        return ADMIN_SCOPE in self.scopes


class ApprovalError(Exception):
    """Base of the decision errors; ``status`` is the HTTP status to map to."""

    status = 400


class ApprovalNotFoundError(ApprovalError):
    status = 404


class ApprovalForbiddenError(ApprovalError):
    status = 403


class ApprovalClosedError(ApprovalError):
    status = 409


class ApprovalInvalidError(ApprovalError):
    status = 422


# ── engine ───────────────────────────────────────────────────────────────────


def _render(text: str | None, view: Mapping[str, Any]) -> str | None:
    if text is None or "{{" not in text:
        return text
    value = compile_template(text).render(view)
    return None if value is None else str(value)


def form_of(agent: HumanAgent) -> dict[str, dict[str, Any]]:
    """The reviewer form: one entry per output, decorated by ``human.form``."""
    form: dict[str, dict[str, Any]] = {}
    for key, t in field_types(agent.outputs).items():
        extra = agent.human.form.get(key)
        entry: dict[str, Any] = {"type": format_type(t), "required": not t.optional}
        if extra is not None:
            entry.update(extra.model_dump(exclude_none=True))
        form[key] = entry
    return form


def signal_for(agent: HumanAgent, values: Mapping[str, Any]) -> str:
    route_on = agent.human.route_on
    if route_on is None:
        return next((s for s in agent.routes if not is_reserved(s)), "default")
    value = values.get(route_on)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _latest(session: AsyncSession, run_id: uuid.UUID, agent_id: str) -> SystemApproval | None:
    stmt = (
        sa.select(SystemApproval)
        .where(_APPROVALS.c.run_id == run_id, _APPROVALS.c.agent_id == agent_id)
        .order_by(_APPROVALS.c.created_at.desc())
        .limit(1)
    )
    found: SystemApproval | None = (await queue._execute(session, stmt)).scalars().first()
    return found


async def human_engine(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    session = ctx.session
    approval = await _latest(session, ctx.run_id, agent.id)
    if approval is not None and approval.payload.get("step") == ctx.idempotency_key:
        if approval.status == "decided":
            values = dict(approval.decision or {})
            return AgentResult(
                signal_for(agent, values),
                outputs=values,
                detail={"decided_by": approval.decided_by},
            )
        if approval.status == "expired":
            return AgentResult("expired", message=f"no decision within {agent.human.expires}")
        return AgentResult("default", suspend="waiting_human")  # still pending

    params = agent.human
    unknown = [k for k in params.show if k not in view]
    payload = {
        "step": ctx.idempotency_key,
        "title": _render(params.title, view),
        "instruction": _render(params.instruction, view),
        "show": {k: view.get(k) for k in params.show if k not in unknown},
        "form": form_of(agent),
        "assignee": _render(params.assignee, view),
    }
    expires_at = None
    if params.expires:
        expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(
            seconds=duration_seconds(params.expires)
        )
    approval = SystemApproval(
        run_id=ctx.run_id,
        agent_id=agent.id,
        kind="human",
        payload=journal.to_json(payload),
        required_group=params.required_group,
        requested_by=ctx.principal,
        expires_at=expires_at,
    )
    session.add(approval)
    await session.flush()
    await journal.append_event(
        session,
        ctx.run_id,
        "approval_requested",
        agent_id=agent.id,
        payload={
            "approval_id": str(approval.id),
            "title": payload["title"],
            "required_group": params.required_group,
        },
    )
    return AgentResult("default", suspend="waiting_human", detail={"approval_id": str(approval.id)})


register_engine("human", human_engine)


# ── decisions ────────────────────────────────────────────────────────────────


def authorise(approval: SystemApproval, reviewer: Reviewer) -> None:
    if not (reviewer.is_admin or DECIDE_SCOPE in reviewer.scopes):
        raise ApprovalForbiddenError(f"deciding requires the '{DECIDE_SCOPE}' scope")
    if approval.required_group:
        if reviewer.user_id is not None and reviewer.user_id == approval.requested_by:
            raise ApprovalForbiddenError("the principal who triggered this run cannot approve it")
        if not (reviewer.is_admin or approval.required_group in reviewer.groups):
            raise ApprovalForbiddenError(
                f"deciding this requires the '{approval.required_group}' group"
            )
        return
    is_owner = approval.requested_by is not None and approval.requested_by == reviewer.user_id
    if not (is_owner or reviewer.is_admin):
        raise ApprovalForbiddenError(
            "only the principal who triggered this run, or an admin, can decide"
        )


async def decide(
    session: AsyncSession,
    run_id: uuid.UUID,
    agent_id: str,
    values: Mapping[str, Any],
    reviewer: Reviewer,
    *,
    workflows: Mapping[str, WorkflowDocument] | None = None,
    resolver: ModelResolver = registry_resolver,
) -> SystemApproval:
    """Record a reviewer's form for the pending request of *agent_id* and
    re-queue the run. The caller commits."""
    stmt = (
        sa.select(SystemApproval)
        .where(
            _APPROVALS.c.run_id == run_id,
            _APPROVALS.c.agent_id == agent_id,
            _APPROVALS.c.kind == "human",
        )
        .order_by(_APPROVALS.c.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    approval: SystemApproval | None = (await queue._execute(session, stmt)).scalars().first()
    if approval is None:
        raise ApprovalNotFoundError(f"run {run_id} has no request for '{agent_id}'")
    if approval.status != "pending":
        raise ApprovalClosedError(f"this request is already {approval.status}")
    if approval.expires_at is not None and approval.expires_at <= _dt.datetime.now(_dt.UTC):
        raise ApprovalClosedError("this request has expired")
    authorise(approval, reviewer)

    run = await session.get(SystemRun, run_id)
    if workflows is None:
        from tuvl.contract.registry import WORKFLOWS

        workflows = WORKFLOWS
    doc = workflows.get(run.workflow) if run is not None else None
    agent = next((a for a in doc.spec.agents if a.id == agent_id), None) if doc else None
    if not isinstance(agent, HumanAgent):
        raise ApprovalNotFoundError(f"'{agent_id}' is not a human agent of this run's workflow")
    try:
        form = build_model(f"{agent_id}.form", field_types(agent.outputs), resolver)
        clean = form.model_validate(dict(values)).model_dump(mode="json", by_alias=True)
    except ValidationError as exc:
        raise ApprovalInvalidError(
            "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_url=False)
            )
        ) from exc
    signal = signal_for(agent, clean)
    if signal not in agent.routes:
        raise ApprovalInvalidError(
            f"'{agent.human.route_on}' = {signal!r} is not one of {sorted(agent.routes)}"
        )

    approval.status = "decided"
    approval.decision = clean
    approval.decided_by = reviewer.user_id
    approval.decided_at = _dt.datetime.now(_dt.UTC)
    await journal.append_event(
        session,
        run_id,
        "approval_decided",
        agent_id=agent_id,
        payload={"approval_id": str(approval.id), "decided_by": reviewer.user_id, "signal": signal},
    )
    await queue.requeue(session, run_id)
    return approval


async def decide_by_id(
    session: AsyncSession,
    approval_id: uuid.UUID,
    values: Mapping[str, Any],
    reviewer: Reviewer,
    **kwargs: Any,
) -> SystemApproval:
    """:func:`decide`, addressed by the approval's id."""
    approval = await session.get(SystemApproval, approval_id)
    if approval is None or approval.kind != "human":
        raise ApprovalNotFoundError(f"no human approval {approval_id}")
    return await decide(session, approval.run_id, approval.agent_id, values, reviewer, **kwargs)


async def cancel_pending(session: AsyncSession, run_id: uuid.UUID) -> int:
    """Close a run's open requests (the run was aborted). The caller commits."""
    result = await queue._execute(
        session,
        sa.update(_APPROVALS)
        .where(_APPROVALS.c.run_id == run_id, _APPROVALS.c.status == "pending")
        .values(status="cancelled"),
    )
    return int(result.rowcount or 0)


async def expire_due(session: AsyncSession) -> int:
    """Close pending requests past their deadline and re-queue their runs.
    The caller commits."""
    expired = await queue._execute(
        session,
        sa.update(_APPROVALS)
        .where(
            _APPROVALS.c.status == "pending",
            _APPROVALS.c.expires_at.is_not(None),
            _APPROVALS.c.expires_at <= queue._now(session),
        )
        .values(status="expired")
        .returning(_APPROVALS.c.id, _APPROVALS.c.run_id, _APPROVALS.c.agent_id),
    )
    rows = expired.all()
    for approval_id, run_id, agent_id in rows:
        await journal.append_event(
            session,
            run_id,
            "approval_decided",
            agent_id=agent_id,
            payload={"approval_id": str(approval_id), "status": "expired"},
        )
        await queue.requeue(session, run_id)
    return len(rows)


queue.SWEEPS.append(expire_due)
