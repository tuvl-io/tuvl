"""A spec step's tool signals follow ``tuvl lint project``: db, http and storage tools."""

from __future__ import annotations

import pytest

from tuvl.contract.schema import WorkflowDocument
from tuvl_insight.spec import parse_text
from tuvl_insight.spec.check import check_spec
from tuvl_insight.spec.verify import verify_spec

FLOW = """# Flows
## Flow: x
Trigger: POST /x · public
Input: `k` (str)
Ends: default 200 · missing 404 · failed 500

1. `s` — tool {tool} · in `k` · out `r` (json)
{routes}
"""


def _findings(tool: str, routes: str, examples: str = "") -> list[str]:
    text = FLOW.format(tool=tool, routes=routes) + examples
    result = check_spec(parse_text(text), text)
    return [f"{f.code} {f.message}" for f in result.errors if f.code in ("S004", "S012")]


@pytest.mark.parametrize(
    ("tool", "routes"),
    [
        ("storage get inbox", "   - not_found → END.missing"),
        ("storage head inbox", "   - not_found → END.missing"),
        ("storage get inbox", "   - not_found → END.missing\n   - too_large → END.failed"),
        ("storage put inbox", "   - conflict → END.failed"),
        ("storage copy inbox", "   - not_found → END.missing\n   - conflict → END.failed"),
        ("storage list inbox", ""),
        ("http GET kb", "   - http_404 → END.missing"),
        ("http GET kb", ""),
    ],
)
def test_the_signals_a_tool_can_emit_are_accepted(tool: str, routes: str) -> None:
    assert _findings(tool, routes) == []


@pytest.mark.parametrize(
    ("tool", "routes", "message"),
    [
        ("storage get inbox", "", "can emit 'not_found'"),
        ("storage put inbox", "", "can emit 'conflict'"),
        ("storage list inbox", "   - not_found → END.missing", "never emits 'not_found'"),
        ("storage put inbox", "   - conflict → END.failed\n   - not_found → END.missing", "never"),
        ("http GET kb", "   - not_found → END.missing", "never emits 'not_found'"),
    ],
)
def test_a_signal_a_tool_cannot_emit_is_still_refused(tool: str, routes: str, message: str) -> None:
    assert any(message in f for f in _findings(tool, routes))


def test_a_storage_signal_can_be_waived() -> None:
    routes = "   - conflict: waived — the key is new each run"
    assert _findings("storage put inbox", routes) == []


def test_an_example_may_mock_a_storage_signal() -> None:
    example = (
        "\n# Acceptance\n```tuvl-example\nname: no such file\nworkflow: x\ninput: { k: a }\n"
        "mocks: { s: { signal: not_found } }\nexpect: { end: missing }\n```\n"
    )
    findings = _findings("storage get inbox", "   - not_found → END.missing", example)
    assert not [f for f in findings if f.startswith("S012")]


def _workflow(op: str) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "x"},
            "spec": {
                "trigger": {
                    "http": {"path": "/x", "method": "POST", "public": True},
                    "input": {"k": "str"},
                },
                "agents": [
                    {
                        "id": "s",
                        "description": "s: tool storage",
                        "engine": "tool",
                        "tool": {"use": "storage", "store": "inbox", "op": op, "key": "{{ k }}"},
                        "inputs": {"k": "str"},
                        "outputs": {"r": "json"},
                        "routes": {
                            "not_found": "END.missing",
                            "error": "END.failed",
                            "default": "END",
                        },
                    }
                ],
                "outputs": {
                    "default": {"status": 200},
                    "missing": {"status": 404},
                    "failed": {"status": 500},
                },
            },
        }
    )


def test_verify_spec_matches_a_storage_step_with_its_workflow_agent() -> None:
    text = FLOW.format(tool="storage get inbox", routes="   - not_found → END.missing")
    result = verify_spec(parse_text(text), text, {"x": _workflow("get")}, {})
    assert [f.message for f in result.errors if f.code == "R002"] == []


def test_verify_spec_still_reports_a_different_storage_operation() -> None:
    text = FLOW.format(tool="storage get inbox", routes="   - not_found → END.missing")
    result = verify_spec(parse_text(text), text, {"x": _workflow("head")}, {})
    assert any(f.code == "R002" for f in result.errors)
