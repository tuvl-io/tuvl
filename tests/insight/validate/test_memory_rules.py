"""Validator rules for ``llm.memory`` (V043, V014) and its share of the worst-case token bound."""

from __future__ import annotations

from typing import Any

from tuvl.contract.schema import WorkflowDocument
from tuvl_insight.validate import Catalog, validate_workflow


def doc(
    memory: dict[str, Any] | None = None, public: bool = False, **agent: Any
) -> WorkflowDocument:
    trigger: dict[str, Any] = {"input": {"text": "str"}}
    if public:
        trigger["http"] = {"path": "/api/ask", "method": "POST", "public": True}
    llm = {"model": "m", "prompt": "Answer {{ text }}"}
    if memory is not None:
        llm["memory"] = memory  # type: ignore[assignment]
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "ask"},
            "spec": {
                "trigger": trigger,
                "agents": [
                    {
                        "id": "answer",
                        "description": "answer",
                        "engine": "llm",
                        "inputs": {"text": "str"},
                        "outputs": {"reply": "str"},
                        "llm": llm,
                        "budget": {"max_tokens": 500, "timeout": "30s"},
                        "routes": {
                            "default": "END",
                            "error": "END.failed",
                            "parse_error": "END.failed",
                            "budget_exceeded": "END.failed",
                            "timeout": "END.failed",
                        },
                        **agent,
                    }
                ],
                "outputs": {"default": {"body": "{{ reply }}"}, "failed": {"status": 500}},
            },
        }
    )


def check(d: WorkflowDocument, **cat: Any) -> Any:
    return validate_workflow(d, Catalog(**cat))


def codes(report: Any) -> set[str]:
    return {x.code for x in report.diagnostics if x.severity == "error"}


def messages(report: Any, code: str) -> str:
    return " | ".join(x.message for x in report.diagnostics if x.code == code)


def test_a_valid_memory_block_is_clean() -> None:
    report = check(doc({"collection": "kb"}), collections={"kb"})
    assert report.diagnostics == [], [(d.code, d.message) for d in report.diagnostics]


def test_the_collection_must_exist() -> None:
    assert "V014" in codes(check(doc({"collection": "ghost"}), collections={"kb"}))


def test_an_owner_named_from_input_is_refused_on_a_public_trigger() -> None:
    named = doc({"collection": "kb", "key": "{{ text }}"}, public=True)
    assert "anyone could" in messages(check(named, collections={"kb"}), "V043")
    assert "V043" not in codes(
        check(doc({"collection": "kb", "key": "{{ text }}"}), collections={"kb"})
    )
    assert "V043" not in codes(check(doc({"collection": "kb"}, public=True), collections={"kb"}))


def test_write_names_fields_the_agent_has_and_never_secure_ones() -> None:
    assert "neither an input nor an output" in messages(
        check(doc({"collection": "kb", "write": ["nope"]}), collections={"kb"}), "V043"
    )
    ok = check(doc({"collection": "kb", "write": ["text", "reply"]}), collections={"kb"})
    assert "V043" not in codes(ok)
    secure = doc({"collection": "kb", "write": ["card"]}, inputs={"text": "str", "card": "Card"})
    secure.spec.trigger.input = {"text": "str", "card": "Card"}  # type: ignore[assignment]
    report = check(secure, collections={"kb"}, secure_fields={"Card": {"number"}})
    assert "secure fields" in messages(report, "V043")


def test_recalled_tokens_count_towards_the_worst_case() -> None:
    plain = check(doc(), collections={"kb"}).profile
    with_memory = check(doc({"collection": "kb", "max_tokens": 700}), collections={"kb"}).profile
    assert plain is not None and with_memory is not None
    assert with_memory.worst_case_tokens == plain.worst_case_tokens + 700


def loop_doc(memory: dict[str, Any], public: bool = False) -> WorkflowDocument:
    trigger: dict[str, Any] = {"input": {"text": "str"}}
    if public:
        trigger["http"] = {"path": "/api/ask", "method": "POST", "public": True}
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "ask"},
            "spec": {
                "trigger": trigger,
                "agents": [
                    {
                        "id": "work",
                        "description": "work",
                        "engine": "loop",
                        "inputs": {"text": "str"},
                        "outputs": {"reply": "str"},
                        "loop": {
                            "model": "m",
                            "steering": "Help",
                            "tools": [{"agent": "look"}],
                            "memory": memory,
                        },
                        "budget": {
                            "max_iterations": 4,
                            "max_tokens": 900,
                            "max_tool_calls": 4,
                            "timeout": "30s",
                        },
                        "routes": {
                            "default": "END",
                            "error": "END.failed",
                            "max_iterations": "END.failed",
                            "budget_exceeded": "END.failed",
                            "timeout": "END.failed",
                        },
                    },
                    {
                        "id": "look",
                        "description": "look",
                        "engine": "code",
                        "effect": "read",
                        "inputs": {"text": "str"},
                        "outputs": {"hit": "str"},
                        "code": {"run": "look"},
                        "routes": {"default": "END"},
                    },
                ],
                "outputs": {"default": {"body": "{{ reply }}"}, "failed": {"status": 500}},
            },
        }
    )


def test_a_loop_agents_memory_gets_the_same_rules_and_token_bound() -> None:
    risky = loop_doc({"collection": "kb", "key": "{{ text }}"}, public=True)
    assert "anyone could" in messages(check(risky, collections={"kb"}), "V043")
    plain = check(loop_doc({"collection": "kb"}), collections={"kb"}).profile
    bigger = check(loop_doc({"collection": "kb", "max_tokens": 600}), collections={"kb"}).profile
    assert plain is not None and bigger is not None
    assert bigger.worst_case_tokens == plain.worst_case_tokens + 600 - 1000  # the default is 1000
    assert "V014" in codes(check(loop_doc({"collection": "ghost"}), collections={"kb"}))
