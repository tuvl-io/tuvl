"""``tuvl test`` runs a parallel step offline when every branch is mocked, and says which one is not."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tuvl.contract.schema import TestDocument, WorkflowDocument
from tuvl_insight.testing import run_test


def get(name: str) -> dict[str, Any]:
    return {
        "id": name,
        "engine": "tool",
        "inputs": {"key": "str"},
        "outputs": {name: "int"},
        "tool": {"use": "http", "method": "GET", "url": f"https://x.test/{name}/{{{{ key }}}}"},
    }


FLOW = WorkflowDocument.model_validate(
    {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "gather_all"},
        "spec": {
            "trigger": {"input": {"key": "str"}},
            "agents": [
                {
                    "id": "gather",
                    "description": "Look it up",
                    "engine": "parallel",
                    "inputs": {"key": "str"},
                    "outputs": {"crm": "int", "kb": "int"},
                    "parallel": {"branches": [get("crm"), get("kb")]},
                    "routes": {"default": "END", "error": "END.failed"},
                }
            ],
            "outputs": {
                "default": {"body": {"crm": "{{ crm }}", "kb": "{{ kb }}"}},
                "failed": {"status": 500},
            },
        },
    }
)


def test_doc(**spec: Any) -> TestDocument:
    return TestDocument.model_validate(
        {
            "kind": "Test",
            "version": "tuvl/v2",
            "metadata": {"name": "t"},
            "spec": {"workflow": "gather_all", "input": {"key": "k"}, **spec},
        }
    )


async def test_every_branch_mocked_runs_offline(tmp_path: Path) -> None:
    result = await run_test(
        test_doc(
            mocks={"crm": {"outputs": {"crm": 3}}, "kb": {"outputs": {"kb": 2}}},
            expect={"output": {"body": {"crm": 3, "kb": 2}}},
        ),
        {"gather_all": FLOW},
        base=tmp_path,
        models={},
    )
    assert result.passed, result.failures


async def test_an_unmocked_branch_is_named(tmp_path: Path) -> None:
    result = await run_test(
        test_doc(mocks={"crm": {"outputs": {"crm": 3}}}, expect={"end": "default"}),
        {"gather_all": FLOW},
        base=tmp_path,
        models={},
    )
    assert not result.passed and result.end == "failed"
    assert any("add mocks.kb" in f for f in result.failures), result.failures


async def test_the_whole_step_can_be_mocked_instead(tmp_path: Path) -> None:
    result = await run_test(
        test_doc(
            mocks={"gather": {"outputs": {"crm": 9, "kb": 8}}},
            expect={"output": {"body": {"crm": 9, "kb": 8}}},
        ),
        {"gather_all": FLOW},
        base=tmp_path,
        models={},
    )
    assert result.passed, result.failures
