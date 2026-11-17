"""A run that is compensating keeps its lease, and one whose worker is lost half way is resumed, not stranded."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.contract.schema import WorkflowDocument
from tuvl.runtime import queue
from tuvl.runtime.code import AGENT_REGISTRY
from tuvl.runtime.models import SystemRun
from tuvl.runtime.queue import Worker
from tuvl.runtime.runner import Runner, workflow_hash

pytestmark = pytest.mark.asyncio

STARTED: list[str] = []
COMPLETED: list[str] = []
NAP: dict[str, float] = {}


def _agent(name: str) -> Any:
    async def run(inp: Any, ctx: Any) -> None:
        STARTED.append(name)
        await asyncio.sleep(NAP.get(name, 0.0))
        COMPLETED.append(name)

    return run


async def _ship(inp: Any, ctx: Any) -> None:
    STARTED.append("ship")
    raise RuntimeError("carrier unavailable")


@pytest.fixture(autouse=True)
def _agents(monkeypatch: pytest.MonkeyPatch) -> None:
    STARTED.clear()
    COMPLETED.clear()
    NAP.clear()
    for name in ("reserve", "charge", "release", "refund"):
        monkeypatch.setitem(AGENT_REGISTRY, name, _agent(name))
    monkeypatch.setitem(AGENT_REGISTRY, "ship", _ship)


def order(rollback_end: bool) -> WorkflowDocument:
    """reserve → charge → ship (fails). Either the failure is unrouted, or it routes to an end that rolls back."""

    def step(name: str, routes: dict[str, str], **extra: Any) -> dict[str, Any]:
        return {
            "id": name,
            "description": name,
            "engine": "code",
            "code": {"run": name},
            "routes": routes,
            **extra,
        }

    ship_routes = {"default": "END", "error": "END.failed"} if rollback_end else {"default": "END"}
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "order"},
            "spec": {
                "trigger": {"input": {}},
                "agents": [
                    step("reserve", {"default": "charge"}, compensate="release"),
                    step("charge", {"default": "ship"}, compensate="refund"),
                    step("ship", ship_routes),
                    step("release", {"default": "END"}),
                    step("refund", {"default": "END"}),
                ],
                "outputs": {
                    "default": {},
                    "failed": {"status": 502, "rollback": True, "body": "cancelled"},
                },
            },
        }
    )


async def start(engine: AsyncEngine, doc: WorkflowDocument) -> uuid.UUID:
    async with AsyncSession(engine) as s:
        run_id = await queue.enqueue(
            s, workflow="order", workflow_hash=workflow_hash(doc), trigger="test", input={}
        )
        await s.commit()
    return run_id


async def fetch(engine: AsyncEngine, run_id: uuid.UUID) -> SystemRun:
    async with AsyncSession(engine) as s:
        run = await s.get(SystemRun, run_id)
        assert run is not None
        return run


@pytest.mark.parametrize("rollback_end", [False, True], ids=["unrouted-error", "rollback-end"])
async def test_a_compensating_run_keeps_its_lease(
    pg_engine: AsyncEngine, rollback_end: bool
) -> None:
    doc = order(rollback_end)
    NAP.update(refund=0.9, release=0.9)  # compensation takes 1.8 s on a 0.5 s lease
    run_id = await start(pg_engine, doc)
    stolen: list[uuid.UUID] = []

    async def thief() -> None:
        for _ in range(40):
            await asyncio.sleep(0.1)
            async with AsyncSession(pg_engine) as s:
                stolen.extend(await queue.claim(s, "thief", lease_seconds=0.5))
                await s.commit()

    worker = Worker(
        pg_engine,
        Runner(pg_engine, {"order": doc}),
        owner="w1",
        lease_seconds=0.5,
        heartbeat_seconds=0.1,
    )
    assert await worker.claim_some() == 1
    robbing = asyncio.create_task(thief())  # after the claim: a thief that gets there first leaves the worker nothing to hold
    await worker.drain()
    await robbing
    assert stolen == []
    assert (await fetch(pg_engine, run_id)).status == "compensated"
    assert [STARTED.count(n) for n in ("ship", "refund", "release")] == [1, 1, 1]


@pytest.mark.parametrize("rollback_end", [False, True], ids=["unrouted-error", "rollback-end"])
async def test_a_worker_lost_mid_compensation_is_resumed_not_stranded(
    pg_engine: AsyncEngine, rollback_end: bool
) -> None:
    doc = order(rollback_end)
    NAP["release"] = 30.0  # the second compensator is still running when the worker goes
    run_id = await start(pg_engine, doc)
    first = Worker(
        pg_engine,
        Runner(pg_engine, {"order": doc}),
        owner="w1",
        lease_seconds=0.5,
        heartbeat_seconds=0.1,
    )
    assert await first.claim_some() == 1
    for _ in range(100):
        if "release" in STARTED:
            break
        await asyncio.sleep(0.05)
    assert "release" in STARTED
    await first.drain(0.05)  # the process is gone; its lease will run out

    NAP["release"] = 0.0
    second = Worker(pg_engine, Runner(pg_engine, {"order": doc}), owner="w2", lease_seconds=0.5)
    for _ in range(80):
        if await second.claim_some():
            break
        await asyncio.sleep(0.1)
    await second.drain()

    run = await fetch(pg_engine, run_id)
    assert run.status == "compensated"
    # nothing that had finished ran again, and the agent that failed was not tried a second time
    assert [STARTED.count(n) for n in ("reserve", "charge", "ship", "refund")] == [1, 1, 1, 1]
    assert COMPLETED.count("refund") == 1 and COMPLETED.count("release") == 1
    if rollback_end:
        assert run.end_name == "failed" and run.output == {"status": 502, "body": "cancelled"}
    else:
        assert run.error is not None and run.error["error_type"] == "code_exception"
