"""The human engine and approval decisions (P1.6d), through the real runner."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.contract.schema import WorkflowDocument
from tuvl.runtime import human, journal, queue
from tuvl.runtime.human import (
    ApprovalClosedError,
    ApprovalForbiddenError,
    ApprovalInvalidError,
    ApprovalNotFoundError,
    Reviewer,
)
from tuvl.runtime.models import SystemApproval, SystemRun
from tuvl.runtime.queue import Worker
from tuvl.runtime.runner import Runner, workflow_hash

REQUESTER = "user-requester"
DECIDE = frozenset({"approvals:decide"})
MANAGER = Reviewer("user-manager", frozenset({"support_manager"}), DECIDE)


def workflow(**human_params: Any) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "wf"},
            "spec": {
                "trigger": {"input": {"order_id": "str", "amount": "int", "message": "str"}},
                "agents": [
                    {
                        "id": "approval",
                        "description": "Manager approves a refund",
                        "engine": "human",
                        "inputs": {"order_id": "str", "amount": "int", "message": "str"},
                        "outputs": {"approved": "bool", "note": "str?"},
                        "human": {
                            "title": "Refund {{ amount }} for {{ order_id }}",
                            "instruction": "Approve this refund?",
                            "show": ["order_id", "message"],
                            "form": {"approved": {"label": "Approve?", "widget": "toggle"}},
                            "required_group": "support_manager",
                            "assignee": "owner-of-{{ order_id }}",
                            "route_on": "approved",
                            **human_params,
                        },
                        "routes": {
                            "true": "END",
                            "false": "END.rejected",
                            "expired": "END.timed_out",
                            "error": "END",
                        },
                    }
                ],
                "outputs": {
                    "default": {"body": "approved {{ order_id }}: {{ note }}"},
                    "rejected": {"status": 409, "body": "rejected"},
                    "timed_out": {"status": 408},
                },
            },
        }
    )


async def start(
    db: AsyncEngine, doc: WorkflowDocument, principal: str | None = REQUESTER
) -> uuid.UUID:
    async with AsyncSession(db) as s:
        run_id = await queue.enqueue(
            s,
            workflow="wf",
            workflow_hash=workflow_hash(doc),
            trigger="test",
            input={"order_id": "o-1", "amount": 40, "message": "charged twice"},
            principal=principal,
        )
        await s.commit()
    await drive(db, doc)
    return run_id


async def drive(db: AsyncEngine, doc: WorkflowDocument, owner: str = "w1") -> None:
    worker = Worker(db, Runner(db, {"wf": doc}), owner=owner)
    while await worker.claim_some():
        await worker.drain()


async def get(db: AsyncEngine, run_id: uuid.UUID) -> SystemRun:
    async with AsyncSession(db) as s:
        run = await s.get(SystemRun, run_id)
        assert run is not None
        return run


async def approvals(db: AsyncEngine, run_id: uuid.UUID) -> list[SystemApproval]:
    async with AsyncSession(db) as s:
        rows = await s.exec(sa.select(SystemApproval).where(SystemApproval.run_id == run_id))  # type: ignore[call-overload,arg-type]
        return [r[0] for r in rows.all()]


async def decide(
    db: AsyncEngine,
    doc: WorkflowDocument,
    run_id: uuid.UUID,
    values: dict[str, Any],
    reviewer: Reviewer = MANAGER,
) -> None:
    async with AsyncSession(db) as s:
        await human.decide(s, run_id, "approval", values, reviewer, workflows={"wf": doc})
        await s.commit()


async def test_request_then_decision_on_another_worker(pg_engine: AsyncEngine) -> None:
    doc = workflow()
    run_id = await start(pg_engine, doc)
    waiting = await get(pg_engine, run_id)
    assert waiting.status == "waiting_human" and waiting.current_agent == "approval"

    [request] = await approvals(pg_engine, run_id)
    assert request.status == "pending" and request.requested_by == REQUESTER
    assert request.payload["title"] == "Refund 40 for o-1"
    assert request.payload["show"] == {"order_id": "o-1", "message": "charged twice"}
    assert request.payload["assignee"] == "owner-of-o-1"
    assert request.payload["form"]["approved"] == {
        "type": "bool",
        "required": True,
        "label": "Approve?",
        "widget": "toggle",
    }
    assert request.payload["form"]["note"] == {"type": "str?", "required": False}

    await drive(pg_engine, doc)  # nothing to do while waiting
    await decide(pg_engine, doc, run_id, {"approved": True, "note": "ok by policy"})
    await drive(pg_engine, doc, owner="w2")
    done = await get(pg_engine, run_id)
    assert done.status == "completed" and done.output == {
        "status": 200,
        "body": "approved o-1: ok by policy",
    }
    assert done.claims == 2 and len(await approvals(pg_engine, run_id)) == 1

    async with AsyncSession(pg_engine) as s:
        types = [e.type for e in await journal.events(s, run_id)]
    assert (
        types.index("approval_requested") < types.index("approval_decided") < types.index("resumed")
    )


async def test_rejection_routes_false(pg_engine: AsyncEngine) -> None:
    doc = workflow()
    run_id = await start(pg_engine, doc)
    await decide(pg_engine, doc, run_id, {"approved": False})
    await drive(pg_engine, doc)
    assert (await get(pg_engine, run_id)).end_name == "rejected"


async def test_authorisation_rules(pg_engine: AsyncEngine) -> None:
    doc = workflow()
    run_id = await start(pg_engine, doc)
    self_approval = Reviewer(REQUESTER, frozenset({"support_manager"}), frozenset({"iam:admin"}))
    with pytest.raises(ApprovalForbiddenError, match="cannot approve it"):
        await decide(pg_engine, doc, run_id, {"approved": True}, self_approval)
    with pytest.raises(ApprovalForbiddenError, match="approvals:decide"):
        await decide(
            pg_engine,
            doc,
            run_id,
            {"approved": True},
            Reviewer("manager", frozenset({"support_manager"})),
        )
    with pytest.raises(ApprovalForbiddenError, match="support_manager"):
        await decide(
            pg_engine, doc, run_id, {"approved": True}, Reviewer("someone-else", scopes=DECIDE)
        )
    await decide(
        pg_engine,
        doc,
        run_id,
        {"approved": True},
        Reviewer("admin", scopes=frozenset({"iam:admin"})),
    )

    open_doc = workflow(required_group=None)
    owned = await start(pg_engine, open_doc)
    with pytest.raises(ApprovalForbiddenError, match="only the principal"):
        await decide(pg_engine, open_doc, owned, {"approved": True}, MANAGER)
    await decide(pg_engine, open_doc, owned, {"approved": True}, Reviewer(REQUESTER, scopes=DECIDE))

    anonymous = await start(pg_engine, open_doc, principal=None)
    with pytest.raises(ApprovalForbiddenError):
        await decide(
            pg_engine, open_doc, anonymous, {"approved": True}, Reviewer(None, scopes=DECIDE)
        )

    scoped_doc = workflow(required_group=None, required_scope="finance_controllers")
    scoped_run = await start(pg_engine, scoped_doc)
    [scoped_req] = await approvals(pg_engine, scoped_run)
    assert scoped_req.required_scope == "finance_controllers" and scoped_req.required_group is None
    with pytest.raises(ApprovalForbiddenError, match="cannot approve it"):
        await decide(
            pg_engine,
            scoped_doc,
            scoped_run,
            {"approved": True},
            Reviewer(REQUESTER, scopes=frozenset({"approvals:decide", "finance_controllers"})),
        )
    with pytest.raises(ApprovalForbiddenError, match="'finance_controllers' scope"):
        await decide(
            pg_engine,
            scoped_doc,
            scoped_run,
            {"approved": True},
            Reviewer("custom-user", groups=frozenset({"custom_role"}), scopes=DECIDE),
        )
    await decide(
        pg_engine,
        scoped_doc,
        scoped_run,
        {"approved": True},
        Reviewer(
            "custom-user",
            groups=frozenset({"custom_role"}),
            scopes=frozenset({"approvals:decide", "finance_controllers"}),
        ),
    )


async def test_form_validation_and_closed_requests(pg_engine: AsyncEngine) -> None:
    doc = workflow()
    run_id = await start(pg_engine, doc)
    with pytest.raises(ApprovalInvalidError, match="approved"):
        await decide(pg_engine, doc, run_id, {"note": "forgot the toggle"})
    with pytest.raises(ApprovalInvalidError, match="approved"):
        await decide(pg_engine, doc, run_id, {"approved": "maybe"})
    with pytest.raises(ApprovalNotFoundError):
        await decide(pg_engine, doc, uuid.uuid4(), {"approved": True})
    await decide(pg_engine, doc, run_id, {"approved": True})
    with pytest.raises(ApprovalClosedError, match="already decided"):
        await decide(pg_engine, doc, run_id, {"approved": False})


async def test_expiry_takes_the_expired_route(pg_engine: AsyncEngine) -> None:
    doc = workflow(expires="1h")
    run_id = await start(pg_engine, doc)
    [request] = await approvals(pg_engine, run_id)
    assert request.expires_at is not None
    async with AsyncSession(pg_engine) as s:
        await s.exec(  # type: ignore[call-overload]
            sa.update(SystemApproval)
            .where(SystemApproval.id == request.id)  # type: ignore[arg-type]
            .values(expires_at=sa.func.now() - sa.text("interval '1 minute'"))
        )
        await s.commit()
    with pytest.raises(ApprovalClosedError, match="expired"):
        await decide(pg_engine, doc, run_id, {"approved": True})
    await drive(pg_engine, doc)  # the worker's sweep expires it and re-queues the run
    final = await get(pg_engine, run_id)
    assert final.status == "completed" and final.end_name == "timed_out"
    assert [a.status for a in await approvals(pg_engine, run_id)] == ["expired"]


async def test_a_spurious_resume_waits_again_without_a_second_request(
    pg_engine: AsyncEngine,
) -> None:
    doc = workflow()
    run_id = await start(pg_engine, doc)
    async with AsyncSession(pg_engine) as s:
        assert await queue.requeue(s, run_id)
        await s.commit()
    await drive(pg_engine, doc)
    assert (await get(pg_engine, run_id)).status == "waiting_human"
    assert len(await approvals(pg_engine, run_id)) == 1
