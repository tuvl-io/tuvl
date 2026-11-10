"""``tuvl test`` runs a map offline: its child workflows run for real, with their agents mocked or deterministic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tuvl.contract.schema import TestDocument, WorkflowDocument
from tuvl.runtime.code import AGENT_REGISTRY
from tuvl_insight.testing import run_test


async def _double(inp: Any, ctx: Any) -> dict[str, Any]:
    return {"score": int(inp.item) * 2}


@pytest.fixture(autouse=True)
def agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(AGENT_REGISTRY, "double", _double)


def child(agent: dict[str, Any]) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "score_item"},
            "spec": {
                "trigger": {"input": {"item": "str"}},
                "agents": [agent],
                "outputs": {"default": {"body": "{{ score }}"}, "failed": {"status": 502}},
            },
        }
    )


CODE = {
    "id": "double",
    "description": "Double the item",
    "engine": "code",
    "inputs": {"item": "str"},
    "outputs": {"score": "int"},
    "code": {"run": "double"},
    "routes": {"default": "END", "error": "END.failed"},
}
FETCH = {
    "id": "fetch",
    "description": "Fetch the item's score",
    "engine": "tool",
    "inputs": {"item": "str"},
    "outputs": {"score": "int"},
    "tool": {"use": "http", "method": "GET", "url": "https://api.test/{{ item }}"},
    "routes": {"default": "END", "error": "END.failed"},
}
PARENT = WorkflowDocument.model_validate(
    {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "score_all"},
        "spec": {
            "trigger": {"input": {"ids": "list[str]"}},
            "agents": [
                {
                    "id": "score_each",
                    "description": "Score every item",
                    "engine": "map",
                    "inputs": {"ids": "list[str]"},
                    "outputs": {"scores": "list[json]"},
                    "map": {"over": "ids", "workflow": "score_item", "as": "item"},
                    "routes": {"default": "END", "error": "END.failed"},
                }
            ],
            "outputs": {"default": {"body": "{{ scores }}"}, "failed": {"status": 500}},
        },
    }
)


def test_doc(**spec: Any) -> TestDocument:
    return TestDocument.model_validate(
        {
            "kind": "Test",
            "version": "tuvl/v2",
            "metadata": {"name": "t"},
            "spec": {"workflow": "score_all", "input": {"ids": ["1", "2", "3"]}, **spec},
        }
    )


async def test_a_deterministic_child_runs_for_real(tmp_path: Path) -> None:
    result = await run_test(
        test_doc(expect={"output": {"body": [2, 4, 6]}, "path": ["score_each"]}),
        {"score_all": PARENT, "score_item": child(CODE)},
        base=tmp_path,
        models={},
    )
    assert result.passed, result.failures


async def test_a_mocked_child_agent_answers_for_every_item(tmp_path: Path) -> None:
    result = await run_test(
        test_doc(
            mocks={"fetch": {"outputs": {"score": 7}}}, expect={"output": {"body": [7, 7, 7]}}
        ),
        {"score_all": PARENT, "score_item": child(FETCH)},
        base=tmp_path,
        models={},
    )
    assert result.passed, result.failures


async def test_an_unmocked_external_child_agent_fails_the_items_and_says_why(
    tmp_path: Path,
) -> None:
    result = await run_test(
        test_doc(expect={"end": "default"}),
        {"score_all": PARENT, "score_item": child(FETCH)},
        base=tmp_path,
        models={},
    )
    assert not result.passed and result.end == "failed"


async def test_many_concurrent_items_keep_the_journal_consistent(tmp_path: Path) -> None:
    ids = [str(i) for i in range(30)]
    parent = PARENT.model_copy(deep=True)
    parent.spec.agents[0].map.concurrency = 16
    result = await run_test(
        test_doc(input={"ids": ids}, expect={"output": {"body": [int(i) * 2 for i in ids]}}),
        {"score_all": parent, "score_item": child(CODE)},
        base=tmp_path,
        models={},
    )
    assert result.passed, result.failures
