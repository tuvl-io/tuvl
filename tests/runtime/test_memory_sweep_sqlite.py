"""The retention sweep leaves a database without the pgvector memory store alone."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tuvl.contract.registry import WORKFLOWS
from tuvl.contract.schema import WorkflowDocument
from tuvl.runtime import memory


def _flow_with_retention() -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "chat"},
            "spec": {
                "agents": [
                    {
                        "id": "a",
                        "description": "a",
                        "engine": "llm",
                        "llm": {
                            "model": "m",
                            "prompt": "p",
                            "memory": {"collection": "mem", "retention": "90d"},
                        },
                        "routes": {"default": "END"},
                    }
                ]
            },
        }
    )


async def test_the_sweep_does_nothing_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(WORKFLOWS, "chat", _flow_with_retention())
    monkeypatch.setattr(memory, "_last_retention", float("-inf"))
    assert memory.retained_collections()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with AsyncSession(engine) as session:
            assert await memory.retention_sweep(session) == 0
    finally:
        await engine.dispose()
