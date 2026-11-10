"""Validator rules for the ``parallel`` engine: V040 (read-only branches), V041 (inputs and outputs), V042."""

from __future__ import annotations

from typing import Any

from tuvl.contract.schema import WorkflowDocument
from tuvl.validate import Catalog, validate_workflow


def get(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": name,
        "engine": "tool",
        "inputs": {"key": "str"},
        "outputs": {name: "int"},
        "tool": {"use": "http", "method": "GET", "url": f"https://x.test/{name}/{{{{ key }}}}"},
        **extra,
    }


def step(
    branches: list[dict[str, Any]] | None = None,
    outputs: dict[str, str] | None = None,
    inputs: dict[str, str] | None = None,
    on_error: str = "fail",
    **agent: Any,
) -> dict[str, Any]:
    branches = branches or [get("crm"), get("kb")]
    return {
        "id": "gather",
        "description": "Look it up",
        "engine": "parallel",
        "inputs": inputs or {"key": "str"},
        "outputs": outputs or {"crm": "int", "kb": "int"},
        "parallel": {"branches": branches, "on_error": on_error},
        "routes": {"default": "END", "error": "END.failed"},
        **agent,
    }


def doc(agent: dict[str, Any] | None = None, **spec: Any) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "gather_all"},
            "spec": {
                "trigger": {"input": {"key": "str"}},
                "agents": [agent or step()],
                "outputs": {"default": {"body": "{{ crm }}"}, "failed": {"status": 500}},
                **spec,
            },
        }
    )


def codes(d: WorkflowDocument) -> set[str]:
    report = validate_workflow(d, Catalog())
    return {x.code for x in report.diagnostics if x.severity == "error"}


def messages(d: WorkflowDocument, code: str) -> str:
    report = validate_workflow(d, Catalog())
    return " | ".join(x.message for x in report.diagnostics if x.code == code)


def test_a_valid_parallel_step_is_clean() -> None:
    report = validate_workflow(doc(), Catalog())
    assert report.diagnostics == [], [(d.code, d.message) for d in report.diagnostics]


def test_branches_only_read() -> None:
    write = get("crm", tool={"use": "http", "method": "POST", "url": "https://x.test/a"})
    assert "V040" in codes(doc(step([write, get("kb")])))
    assert "only read" in messages(doc(step([write, get("kb")])), "V040")
    mcp = get("crm", tool={"use": "mcp", "server": "artifact://crm@1", "name": "find"})
    assert "V040" in codes(doc(step([mcp, get("kb")])))
    code_write = {
        "id": "crm",
        "engine": "code",
        "inputs": {"key": "str"},
        "outputs": {"crm": "int"},
        "code": {"run": "lookup_crm"},
    }
    assert "V040" in codes(doc(step([code_write, get("kb")])))  # a code agent defaults to write
    assert "V040" not in codes(doc(step([{**code_write, "effect": "read"}, get("kb")])))


def test_a_branch_input_must_come_from_the_step() -> None:
    other = get("crm", inputs={"customer": "str"})
    assert "V041" in codes(doc(step([other, get("kb")])))
    assert "does not take" in messages(doc(step([other, get("kb")])), "V041")
    wrong_type = get("crm", inputs={"key": "int"})
    assert "V007" in codes(doc(step([wrong_type, get("kb")])))


def test_the_steps_outputs_are_exactly_the_branches() -> None:
    twice = get("kb", outputs={"crm": "int"})
    assert "produced by both" in messages(doc(step([get("crm"), twice])), "V041")
    assert "does not declare it" in messages(doc(step(outputs={"crm": "int"})), "V041")
    assert "not produced by any branch" in messages(
        doc(step(outputs={"crm": "int", "kb": "int", "ghost": "int"})), "V041"
    )
    assert "V007" in codes(doc(step(outputs={"crm": "str", "kb": "int"})))


def test_collect_needs_optional_outputs() -> None:
    assert "declare it optional" in messages(doc(step(on_error="collect")), "V041")
    ok = step(on_error="collect", outputs={"crm": "int?", "kb": "int?"})
    assert "V041" not in codes(doc(ok))


def test_not_under_a_per_run_transaction() -> None:
    assert "V042" in codes(doc(transaction="per_run"))
    assert "V042" not in codes(doc(transaction="per_agent"))


def test_the_steps_worst_case_is_the_sum_of_its_branches() -> None:
    llm = {
        "id": "gist",
        "engine": "llm",
        "inputs": {"key": "str"},
        "outputs": {"gist": "str"},
        "llm": {"model": "m", "prompt": "Say hi about {{ key }}"},
        "budget": {"max_tokens": 700, "timeout": "30s"},
    }
    other = {
        **llm,
        "id": "alt",
        "outputs": {"alt": "str"},
        "budget": {"max_tokens": 300, "timeout": "30s"},
    }
    agent = step([llm, other], outputs={"gist": "str", "alt": "str"})
    report = validate_workflow(doc(agent), Catalog())
    assert report.profile is not None and report.profile.worst_case_tokens == 1000


def test_codegen_sees_the_code_branches() -> None:
    from tuvl_insight.codegen.schemas import class_base

    code = {
        "id": "crm",
        "engine": "code",
        "effect": "read",
        "inputs": {"key": "str"},
        "outputs": {"crm": "int"},
        "code": {"run": "lookup_crm"},
    }
    d = doc(step([code, get("kb")]))
    assert [a.id for a in d.spec.all_agents()] == ["gather", "crm", "kb"]
    assert ("gather_all", "crm") in class_base({"gather_all": d})
