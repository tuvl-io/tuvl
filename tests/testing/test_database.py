"""The offline test database: concurrent sessions share one connection without undoing each other."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

from tuvl.contract.schema import TestDocument, WorkflowDocument
from tuvl.core.models.loader import MODEL_REGISTRY
from tuvl.runtime import journal, queue
from tuvl.runtime.code import AGENT_REGISTRY
from tuvl.runtime.models import SystemRunEvent
from tuvl_insight.testing import run_test
from tuvl_insight.testing.runner import _database


async def test_a_rollback_in_one_session_does_not_undo_a_seq_another_allocated() -> None:
    engine = await _database({})
    try:
        async with AsyncSession(engine) as s:
            run_id = await queue.enqueue(
                s, workflow="w", workflow_hash="h", trigger="test", input={}
            )
            await s.commit()
        async with AsyncSession(engine) as a, AsyncSession(engine) as b:
            await b.execute(sa.select(1))
            seq = await journal.allocate_seq(a, run_id)
            await b.rollback()
            a.add(SystemRunEvent(run_id=run_id, seq=seq, type="agent_started"))
            await a.commit()
        async with AsyncSession(engine) as s:
            await journal.append_event(s, run_id, "agent_completed")
            await s.commit()
            assert [e.type for e in await journal.events(s, run_id)] == [
                "run_queued",
                "agent_started",
                "agent_completed",
            ]
    finally:
        await engine.dispose()


class Note(SQLModel, table=True):
    __tablename__ = "database_test_notes"
    id: int | None = SQLField(default=None, primary_key=True)
    text: str


async def _add(inp: Any, ctx: Any) -> dict[str, Any]:
    await ctx.db.create("Note", text="x")
    return {"n": 1}


async def _count(inp: Any, ctx: Any) -> dict[str, Any]:
    return {"n": len(await ctx.db.list("Note"))}


async def test_a_per_run_transaction_that_writes_does_not_block_the_journal(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(MODEL_REGISTRY, "Note", Note)
    monkeypatch.setitem(AGENT_REGISTRY, "add_note", _add)
    monkeypatch.setitem(AGENT_REGISTRY, "count_notes", _count)

    def agent(id_: str, nxt: str) -> dict[str, Any]:
        return {
            "id": id_,
            "description": id_,
            "engine": "code",
            "code": {"run": id_},
            "outputs": {"n": "int"},
            "routes": {"default": nxt, "error": "END.failed"},
        }

    wf = WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "notes"},
            "spec": {
                "transaction": "per_run",
                "models": ["Note"],
                "trigger": {"input": {}},
                "agents": [agent("add_note", "count_notes"), agent("count_notes", "END")],
                "outputs": {"default": {"body": "{{ n }}"}, "failed": {"status": 500}},
            },
        }
    )
    test = TestDocument.model_validate(
        {
            "kind": "Test",
            "version": "tuvl/v2",
            "metadata": {"name": "t"},
            "spec": {"workflow": "notes", "expect": {"output": {"body": 1}}},
        }
    )
    result = await run_test(test, {"notes": wf}, base=tmp_path, models={"Note": Note})
    assert result.passed, result.failures
