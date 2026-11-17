"""What a model is shown: `secure` field values in a prompt's inputs and in a loop's tool observations are
masked unless the workflow's policy says `allow_secure_to_llm`."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.runtime.test_llm_engine import llm  # noqa: F401 - the scripted model fixture
from tests.runtime.test_loop_engine import (  # noqa: F401 - the scripted loop model
    call,
    drive,
    model,
)
from tuvl.contract.schema import WorkflowDocument
from tuvl.core.models import loader
from tuvl.runtime import queue
from tuvl.runtime.code import AGENT_REGISTRY
from tuvl.runtime.models import SystemRun
from tuvl.runtime.runner import workflow_hash

SSN = "123-45-6789"
PERSON = {"name": "Ann", "ssn": SSN}


@pytest.fixture(autouse=True)
def secure_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "SECURE_FIELDS", {"ssn"})
    monkeypatch.setattr(loader, "SECURE_BY_MODEL", {"Person": frozenset({"ssn"})}, raising=False)


async def start(db: AsyncEngine, doc: WorkflowDocument, payload: dict[str, Any]) -> SystemRun:
    async with AsyncSession(db) as s:
        run_id = await queue.enqueue(
            s,
            workflow="wf",
            workflow_hash=workflow_hash(doc),
            trigger="test",
            input=payload,
            principal="caller",
        )
        await s.commit()
    await drive(db, {"wf": doc}, "w1")
    async with AsyncSession(db) as s:
        run = await s.get(SystemRun, run_id)
        assert run is not None
        return run


# ── llm ───────────────────────────────────────────────────────────────────────


def llm_doc(allow: bool) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "wf"},
            "spec": {
                "trigger": {"input": {"person": "json"}},
                "policy": {"allow_secure_to_llm": allow},
                "agents": [
                    {
                        "id": "ask",
                        "description": "ask the model",
                        "engine": "llm",
                        "inputs": {"person": "json"},
                        "outputs": {"team": "str"},
                        "llm": {
                            "model": "openai/gpt-test",
                            "system": "You classify.",
                            "prompt": "Classify the person.",
                        },
                        "routes": {
                            "default": "END",
                            "error": "END.failed",
                            "parse_error": "END.failed",
                            "timeout": "END.failed",
                            "budget_exceeded": "END.failed",
                        },
                    }
                ],
                "outputs": {"default": {"body": "{{ team }}"}, "failed": {"status": 502}},
            },
        }
    )


async def test_the_prompt_shows_a_secure_value_as_masked(pg_engine: AsyncEngine, llm: Any) -> None:  # noqa: F811
    fake = llm({"team": "billing"})
    run = await start(pg_engine, llm_doc(False), {"person": PERSON})
    assert run.status == "completed"
    prompt = fake.calls[0]["messages"][1]["content"]
    assert "Ann" in prompt and SSN not in prompt and "*****" in prompt


async def test_the_policy_can_let_it_through(pg_engine: AsyncEngine, llm: Any) -> None:  # noqa: F811
    fake = llm({"team": "billing"})
    await start(pg_engine, llm_doc(True), {"person": PERSON})
    assert SSN in fake.calls[0]["messages"][1]["content"]


async def test_a_value_that_is_not_a_secure_name_is_untouched(
    pg_engine: AsyncEngine,
    llm: Any,  # noqa: F811
) -> None:
    fake = llm({"team": "billing"})
    await start(pg_engine, llm_doc(False), {"person": {"name": "Ann", "ssn_note": "kept"}})
    prompt = fake.calls[0]["messages"][1]["content"]
    assert "kept" in prompt and "*****" not in prompt


# ── loop ──────────────────────────────────────────────────────────────────────


async def _person_lookup(inp: Any, ctx: Any) -> dict[str, Any]:
    return {"person": PERSON}


def loop_doc(allow: bool) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "wf"},
            "spec": {
                "trigger": {"input": {"ticket": "str"}},
                "policy": {"allow_secure_to_llm": allow},
                "agents": [
                    {
                        "id": "resolve",
                        "description": "Resolve the ticket",
                        "engine": "loop",
                        "inputs": {"ticket": "str"},
                        "outputs": {"resolution": "str"},
                        "budget": {
                            "max_iterations": 4,
                            "max_tokens": 10_000,
                            "max_tool_calls": 4,
                            "timeout": "30s",
                        },
                        "loop": {
                            "model": "openai/gpt-test",
                            "steering": "Resolve tickets.",
                            "tools": [{"agent": "lookup_person"}],
                        },
                        "routes": {
                            "resolved": "END",
                            "error": "END.failed",
                            "max_iterations": "END.failed",
                            "budget_exceeded": "END.failed",
                            "timeout": "END.failed",
                            "aborted": "END.failed",
                        },
                    },
                    {
                        "id": "lookup_person",
                        "description": "Look a person up",
                        "engine": "code",
                        "code": {"run": "person_lookup"},
                        "effect": "read",
                        "inputs": {"ticket": "str"},
                        "outputs": {"person": "json"},
                        "routes": {"default": "END"},
                    },
                ],
                "outputs": {"default": {"body": "{{ resolution }}"}, "failed": {"status": 500}},
            },
        }
    )


async def _loop_observation(
    db: AsyncEngine,
    model: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    allow: bool,
) -> str:
    monkeypatch.setitem(AGENT_REGISTRY, "person_lookup", _person_lookup)
    script = model(
        [call("lookup_person", ticket="t-1")],
        [call("finish", outcome="resolved", resolution="done")],
    )
    run = await start(db, loop_doc(allow), {"ticket": "t-1"})
    assert run.status == "completed", run.error
    return next(m["content"] for m in script.seen[-1] if m["role"] == "tool")


async def test_a_loop_observation_masks_a_secure_value(
    pg_engine: AsyncEngine,
    model: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _loop_observation(pg_engine, model, monkeypatch, allow=False)
    assert "Ann" in seen and SSN not in seen and "*****" in seen


async def test_the_policy_lets_a_loop_observation_through(
    pg_engine: AsyncEngine,
    model: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _loop_observation(pg_engine, model, monkeypatch, allow=True)
    assert SSN in seen
