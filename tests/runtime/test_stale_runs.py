"""A deploy that changes a workflow fails its unfinished runs; the host says how many, at boot."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.contract.schema import WorkflowDocument
from tuvl.runtime import queue
from tuvl.runtime.host import RuntimeHost
from tuvl.runtime.runner import workflow_hash

pytestmark = pytest.mark.asyncio


def doc(name: str, description: str = "noop") -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": name},
            "spec": {
                "trigger": {"input": {}},
                "agents": [
                    {
                        "id": "echo",
                        "description": description,
                        "engine": "code",
                        "code": {"run": "noop"},
                        "routes": {"default": "END", "error": "END"},
                    }
                ],
            },
        }
    )


async def start(engine: AsyncEngine, workflow: str, digest: str, status: str) -> None:
    async with AsyncSession(engine) as s:
        run_id = await queue.enqueue(
            s, workflow=workflow, workflow_hash=digest, trigger="api", input={}
        )
        if status != "queued":
            await queue._execute(
                s, sa.update(queue._RUNS).where(queue._RUNS.c.id == run_id).values(status=status)
            )
        await s.commit()


async def test_unfinished_runs_of_a_changed_workflow_are_counted(pg_engine: AsyncEngine) -> None:
    current, changed = doc("current"), doc("changed", "now says something else")
    old_digest = workflow_hash(doc("changed", "what it said before"))
    await start(pg_engine, "current", workflow_hash(current), "queued")  # same version: fine
    await start(pg_engine, "changed", old_digest, "queued")
    await start(pg_engine, "changed", old_digest, "waiting_human")
    await start(pg_engine, "changed", old_digest, "paused")
    await start(pg_engine, "changed", old_digest, "completed")  # finished: nothing to fail
    await start(pg_engine, "removed", "any", "waiting_approval")  # its workflow is gone
    host = RuntimeHost(pg_engine, workflows={"current": current, "changed": changed})
    found: list[tuple[str, str, int]] = await host.stale_runs()
    assert sorted(found) == [
        ("changed", "workflow_changed", 3),
        ("removed", "workflow_not_found", 1),
    ]


async def test_nothing_is_stale_when_every_unfinished_run_is_on_the_current_version(
    pg_engine: AsyncEngine,
) -> None:
    current = doc("current")
    await start(pg_engine, "current", workflow_hash(current), "waiting_human")
    host = RuntimeHost(pg_engine, workflows={"current": current})
    assert await host.stale_runs() == []
