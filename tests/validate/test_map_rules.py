"""Validator rules for the ``map`` engine (TD-52): V035, V036, and the V007/V015/V004 checks it adds."""

from __future__ import annotations

from typing import Any

import pytest

from tuvl.contract.schema import WorkflowDocument
from tuvl_insight.validate import Catalog, validate_workflow


def wf(
    name: str, trigger: dict[str, str], agents: list[dict[str, Any]], **spec: Any
) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": name},
            "spec": {
                "trigger": {"input": trigger},
                "agents": agents,
                "outputs": {"default": {"body": "{{ out }}"}, "failed": {"status": 500}},
                **spec,
            },
        }
    )


def http_get() -> dict[str, Any]:
    return {
        "id": "fetch",
        "description": "Fetch",
        "engine": "tool",
        "inputs": {"item": "str"},
        "outputs": {"out": "int"},
        "tool": {"use": "http", "method": "GET", "url": "https://x.test/{{ item }}"},
        "routes": {"default": "END", "error": "END.failed"},
    }


def child(
    trigger: dict[str, str] | None = None, agents: list[dict[str, Any]] | None = None
) -> WorkflowDocument:
    return wf("score_item", trigger or {"item": "str"}, agents or [http_get()])


def mapper(
    inputs: dict[str, str] | None = None, outputs: dict[str, str] | None = None, **params: Any
) -> dict[str, Any]:
    return {
        "id": "score_each",
        "description": "Score each",
        "engine": "map",
        "inputs": inputs or {"ids": "list[str]"},
        "outputs": outputs or {"out": "list[json]"},
        "map": {"over": "ids", "workflow": "score_item", "as": "item", **params},
        "routes": {"default": "END", "error": "END.failed"},
    }


def parent(
    agent: dict[str, Any] | None = None, trigger: dict[str, str] | None = None
) -> WorkflowDocument:
    return wf("score_all", trigger or {"ids": "list[str]"}, [agent or mapper()])


def check(doc: WorkflowDocument, *children: WorkflowDocument) -> Any:
    cat = Catalog(workflows={c.metadata.name: c for c in children or (child(),)})
    return validate_workflow(doc, cat)


def codes(report: Any, severity: str = "error") -> set[str]:
    return {d.code for d in report.diagnostics if d.severity == severity}


def messages(report: Any, code: str) -> str:
    return " | ".join(d.message for d in report.diagnostics if d.code == code)


def test_a_valid_map_is_clean() -> None:
    report = check(parent())
    assert report.diagnostics == [], [(d.code, d.message) for d in report.diagnostics]


def test_over_must_be_a_list_input() -> None:
    assert "V035" in codes(check(parent(mapper(over="nope"))))
    scalar = parent(mapper(inputs={"ids": "str"}), trigger={"ids": "str"})
    assert "not a list" in messages(check(scalar), "V035")


def test_the_workflow_must_exist_and_not_be_itself() -> None:
    assert "not defined" in messages(check(parent(mapper(workflow="ghost"))), "V035")
    me = parent(mapper(workflow="score_all"))
    assert "cannot map over itself" in messages(check(me, child()), "V035")


def test_as_with_and_required_child_inputs() -> None:
    assert "no trigger input 'thing'" in messages(check(parent(mapper(**{"as": "thing"}))), "V035")
    assert "no trigger input 'bogus'" in messages(
        check(parent(mapper(**{"with": {"bogus": "1"}}))), "V035"
    )
    assert "receives each item" in messages(
        check(parent(mapper(**{"with": {"item": "x"}}))), "V035"
    )
    needy = child({"item": "str", "tenant": "str"})
    assert "needs tenant" in messages(check(parent(), needy), "V035")
    ok = parent(mapper(inputs={"ids": "list[str]", "t": "str"}, **{"with": {"tenant": "{{ t }}"}}))
    assert "V035" not in codes(check(ok, needy))


def test_item_type_must_fit_the_child_input() -> None:
    report = check(parent(), child({"item": "int"}, [{**http_get(), "inputs": {"item": "int"}}]))
    assert "V007" in codes(report) and "cannot be the input" in messages(report, "V007")
    assert "V007" not in codes(
        check(
            parent(mapper(inputs={"ids": "list[int]"}), {"ids": "list[int]"}),
            child({"item": "int"}, [{**http_get(), "inputs": {"item": "int"}}]),
        )
    )


def test_outputs_are_one_list() -> None:
    assert "V007" in codes(check(parent(mapper(outputs={"out": "int"}))))
    assert "V007" in codes(check(parent(mapper(outputs={"a": "list[json]", "b": "list[json]"}))))


def test_a_mapped_workflow_cannot_wait_for_a_person() -> None:
    asking = {
        "id": "ask",
        "description": "Ask",
        "engine": "human",
        "inputs": {"item": "str"},
        "outputs": {"out": "int"},
        "human": {"title": "Score", "form": {"out": {}}},
        "routes": {"default": "END"},
    }
    report = check(parent(), child(agents=[asking]))
    assert "V036" in codes(report) and "step 'ask'" in messages(report, "V036")
    pending = {**asking, "engine": "pending"}
    pending.pop("human")
    assert "V036" in codes(check(parent(), child(agents=[pending])))


def test_declared_effect_is_not_weaker_than_the_child() -> None:
    writer = {
        **http_get(),
        "tool": {
            "use": "http",
            "method": "POST",
            "url": "https://x.test/",
            "body": {"i": "{{ item }}"},
        },
    }
    report = check(parent(mapper() | {"effect": "read"}), child(agents=[writer]))
    assert "V015" in codes(report, "warning") and "weaker" in messages(report, "V015")
    assert "V015" not in codes(
        check(parent(mapper() | {"effect": "write"}), child(agents=[writer])), "warning"
    )


def test_partial_is_routable_only_when_failures_are_collected() -> None:
    routes = {"default": "END", "partial": "END", "error": "END.failed"}
    assert "V004" in codes(
        check(parent(mapper() | {"routes": routes}))
    )  # on_error: fail never emits it
    assert "V004" not in codes(check(parent(mapper(on_error="collect") | {"routes": routes})))
    assert "V004" not in codes(
        check(parent(mapper(on_error="collect")))
    )  # unrouted: falls to default


def test_with_templates_are_type_checked() -> None:
    bad = parent(mapper(**{"with": {"tenant": "{{ undeclared }}"}}))
    needy = child({"item": "str", "tenant": "str"})
    assert "V008" in codes(check(bad, needy))


def test_cost_and_class_come_from_the_child() -> None:
    thinker = {
        "id": "think",
        "description": "Ask a model",
        "engine": "llm",
        "inputs": {"item": "str"},
        "outputs": {"out": "int"},
        "llm": {"model": "default", "prompt": "Score {{ item }}"},
        "budget": {"max_tokens": 100, "timeout": "30s"},
        "routes": {
            "default": "END",
            "parse_error": "END.failed",
            "timeout": "END.failed",
            "budget_exceeded": "END.failed",
            "error": "END.failed",
        },
    }
    report = check(parent(mapper(max_items=10)), child(agents=[thinker]))
    assert report.profile.worst_case_tokens == 1000  # 100 tokens x 10 items
    assert report.profile.bounded == 1 and report.profile.deterministic == 0
    plain = check(parent())
    assert plain.profile.worst_case_tokens == 0 and plain.profile.deterministic == 1


@pytest.mark.parametrize("n", [1, 7])
def test_cost_scales_with_max_items(n: int) -> None:
    thinker = {
        "id": "think",
        "description": "Ask a model",
        "engine": "llm",
        "inputs": {"item": "str"},
        "outputs": {"out": "int"},
        "llm": {"model": "default", "prompt": "Score {{ item }}"},
        "budget": {"max_tokens": 50, "timeout": "30s"},
        "routes": {
            "default": "END",
            "parse_error": "END.failed",
            "timeout": "END.failed",
            "budget_exceeded": "END.failed",
            "error": "END.failed",
        },
    }
    assert (
        check(parent(mapper(max_items=n)), child(agents=[thinker])).profile.worst_case_tokens
        == 50 * n
    )
