"""Derived task status (P3.6): statuses, overlays, issue attribution, spec diff, CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from tuvl.contract.schema import TaskPlanDocument, TestDocument, WorkflowDocument
from tuvl_insight.spec import parse_text
from tuvl_insight.spec.tasks import ProjectState, TestOutcome, derive, spec_diff

SPEC = "# Flows\nRefund it.\n\n# Data\nRefund model.\n\n# Intent\nWhy.\n"


def workflow(engine: str = "code") -> WorkflowDocument:
    body: dict[str, Any] = {
        "id": "issue",
        "description": "Issue.",
        "engine": engine,
        "routes": {"default": "END", "error": "END"},
    }
    if engine == "code":
        body["code"] = {"run": "issue"}
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "refund"},
            "spec": {"agents": [body]},
        }
    )


def plan(*tasks: dict[str, Any]) -> TaskPlanDocument:
    return TaskPlanDocument.model_validate(
        {
            "kind": "TaskPlan",
            "version": "tuvl/v2",
            "metadata": {"name": "r", "spec": "specs/r.md", "spec_hash": "x"},
            "spec": {"tasks": list(tasks)},
        }
    )


def task(id_: str, type_: str, **kw: Any) -> dict[str, Any]:
    return {"id": id_, "title": f"task {id_}", "type": type_, **kw}


def make_test(
    name: str,
    *,
    assertions: list[str] | None = None,
    judge: bool = False,
    generated: str | None = None,
) -> TestDocument:
    expect: dict[str, Any] = {"assertions": assertions or []}
    if judge:
        expect["judge"] = [{"judge": "artifact://tone@1", "target": "x"}]
    return TestDocument.model_validate(
        {
            "kind": "Test",
            "version": "tuvl/v2",
            "metadata": {"name": name, "generated_from": generated},
            "spec": {"workflow": "refund", "expect": expect},
        }
    )


def state(**kw: Any) -> ProjectState:
    base = ProjectState(
        workflows={"refund": workflow()},
        workflow_files={"refund": "workflows/refund.yaml"},
        models={"Refund": "models/refund.yaml"},
        artifacts={"style"},
        tests=[],
        lock_current=True,
    )
    for key, value in kw.items():
        setattr(base, key, value)
    return base


def statuses(p: TaskPlanDocument, s: ProjectState, spec: Any = None) -> dict[str, str]:
    return {t.id: t.status for t in derive(p, s, spec)}


def test_targets_climb_from_todo_to_done() -> None:
    p = plan(
        task("T1", "model", target={"model": "Refund"}),
        task("T2", "model", target={"model": "Gone"}),
        task("T3", "agent", target={"workflow": "refund", "agent": "issue"}),
        task("T4", "artifact", target={"artifact": "style"}),
        task("T5", "integration"),
    )
    assert statuses(p, state()) == {
        "T1": "done",
        "T2": "todo",
        "T3": "implemented",
        "T4": "done",
        "T5": "untracked",
    }
    passing = [TestOutcome(make_test("t"), True, ["issue"])]
    assert statuses(p, state(tests=passing))["T3"] == "done"
    assert (
        statuses(p, state(tests=passing, lock_current=False, lock_note="tuvl.lock is missing"))[
            "T3"
        ]
        == "tested"
    )
    pending = state(workflows={"refund": workflow("pending")}, tests=passing)
    assert statuses(p, pending)["T3"] == "todo"


def test_issues_attach_to_their_target() -> None:
    s = state(tests=[TestOutcome(make_test("t"), True, ["issue"])])
    s.add_issue(
        "error", "workflows/refund.yaml:4", "[V006] spec.agents[issue].inputs: amount is never set"
    )
    s.add_issue("warning", "models/refund.yaml", "[V0xx] something soft")
    p = plan(
        task("T1", "agent", target={"workflow": "refund", "agent": "issue"}),
        task("T2", "workflow", target={"workflow": "refund"}),
        task("T3", "model", target={"model": "Refund"}),
    )
    result = {t.id: t for t in derive(p, s)}
    assert result["T1"].status == "in_progress" and "amount is never set" in result["T1"].reasons[0]
    assert result["T2"].status == "in_progress"
    assert result["T3"].status == "implemented" and result["T3"].reasons == [
        "warning: [V0xx] something soft"
    ]


def test_spec_examples_must_pass_and_test_tasks_follow_their_tests() -> None:
    spec = parse_text(SPEC, path="specs/r.md")
    failing = [
        TestOutcome(make_test("ok"), True, ["issue"]),
        TestOutcome(make_test("ex", generated="specs/r.md#flows"), False),
    ]
    p = plan(
        task("T1", "workflow", target={"workflow": "refund"}),
        task("T2", "test", target={"workflow": "refund", "assertion": "end == 'default'"}),
        task("T3", "artifact", target={"workflow": "refund", "needs": "judge"}),
    )
    result = {t.id: t for t in derive(p, state(tests=failing), spec)}
    assert (
        result["T1"].status == "implemented" and "spec examples failing: ex" in result["T1"].reasons
    )
    assert result["T2"].status == "todo" and result["T3"].status == "todo"
    carrying = [TestOutcome(make_test("a", assertions=["end == 'default'"], judge=True), False)]
    assert statuses(p, state(tests=carrying))["T2"] == "implemented"
    carrying[0].passed = True
    assert statuses(p, state(tests=carrying)) == {"T1": "done", "T2": "done", "T3": "done"}
    assert derive(p, state(tests=None))[1].reasons == ["tests not run"]
    structural = make_test("b").model_copy(deep=True)
    structural.spec.expect.end = "default"  # `end == 'default'` is what expect.end checks
    assert statuses(p, state(tests=[TestOutcome(structural, True)]))["T2"] == "done"


def test_overlays_manual_and_obsolete() -> None:
    spec = parse_text(SPEC, path="specs/r.md")
    flows = spec.section("flows")
    assert flows is not None
    p = plan(
        task("T1", "model", target={"model": "Gone"}),
        task("T2", "manual", status="in_progress", depends_on=["T1"]),
        task(
            "T3", "model", target={"model": "Refund"}, spec_ref={"anchor": "flows", "hash": "old"}
        ),
        task(
            "T4",
            "model",
            target={"model": "Refund"},
            spec_ref={"anchor": "flows", "hash": flows.hash},
        ),
        task("T5", "model", target={"model": "Refund"}, obsolete=True, depends_on=["T1"]),
        task("T6", "model", target={"model": "Refund"}, depends_on=["T5"]),
    )
    result = {t.id: t for t in derive(p, state(), spec)}
    assert result["T2"].status == "in_progress" and result["T2"].blocked
    assert result["T3"].stale and not result["T4"].stale
    assert result["T5"].obsolete and not result["T5"].blocked
    assert not result["T6"].blocked  # an obsolete dependency does not block


def test_spec_diff() -> None:
    spec = parse_text(SPEC, path="specs/r.md")
    p = plan(
        task("T1", "model", spec_ref={"anchor": "flows", "hash": "old"}),
        task("T2", "model", spec_ref={"anchor": "gone", "hash": "x"}),
        task("T3", "model", spec_ref={"anchor": "data", "hash": spec.section("data").hash}),
    )  # type: ignore[union-attr]
    diff = spec_diff(p, spec)
    assert diff.changed and diff.stale == [
        ("T1", "section #flows changed"),
        ("T2", "section #gone was removed"),
    ]
    assert diff.uncovered == ["intent"]


def test_cli_status_on_a_project(tmp_path: Path) -> None:
    from tuvl.cli import app

    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows/refund.yaml").write_text(
        yaml.safe_dump(workflow("pending").model_dump(mode="json", exclude_defaults=True))
    )
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs/r.md").write_text(SPEC)
    (tmp_path / "specs/r.tasks.yaml").write_text(
        yaml.safe_dump(
            plan(task("T1", "agent", target={"workflow": "refund", "agent": "issue"})).model_dump(
                mode="json", exclude_defaults=True
            )
        )
    )
    runner = CliRunner()
    result = runner.invoke(app, ["list", "tasks", "-d", str(tmp_path), "--json", "--strict"])
    assert result.exit_code == 1, result.output
    report = json.loads(result.output[result.output.index("{") :])
    assert report["done"] is False and report["plans"][0]["tasks"][0]["status"] == "todo"
    diff = runner.invoke(app, ["show", "drift", "-d", str(tmp_path), "--json"])
    assert (
        diff.exit_code == 0
        and json.loads(diff.output[diff.output.index("{") :])["specs"][0]["changed"] is True
    )


def test_a_loop_tool_is_covered_through_its_loop() -> None:
    doc = WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "refund"},
            "spec": {
                "agents": [
                    {
                        "id": "investigate",
                        "description": "Investigate.",
                        "engine": "loop",
                        "outputs": {"note": "str"},
                        "budget": {
                            "max_iterations": 2,
                            "max_tokens": 100,
                            "max_tool_calls": 2,
                            "timeout": "10s",
                        },
                        "loop": {"model": "m", "steering": "s", "tools": [{"agent": "lookup"}]},
                        "routes": {
                            "done": "END",
                            "max_iterations": "END",
                            "budget_exceeded": "END",
                            "timeout": "END",
                            "aborted": "END",
                            "error": "END",
                        },
                    },
                    {
                        "id": "lookup",
                        "description": "Look up.",
                        "engine": "code",
                        "code": {"run": "lookup"},
                        "routes": {"default": "END"},
                    },
                ]
            },
        }
    )
    p = plan(task("T1", "agent", target={"workflow": "refund", "agent": "lookup"}))
    through_loop = [TestOutcome(make_test("t"), True, ["investigate"])]
    st = state(tests=through_loop)
    st.workflows = {"refund": doc}
    assert statuses(p, st)["T1"] == "done"


# ── the task list: written by the coding agent, checked by tuvl (P001–P005) ──


def _plan_project(tmp_path: Path, tasks: list[dict[str, Any]]) -> tuple[Path, Any, Any]:
    from tuvl_insight.spec import parse_text
    from tuvl_insight.spec.grammar import parse_structure

    text = (Path(__file__).parent / "grammar" / "refund-request.md").read_text()
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "refund-request.md").write_text(text)
    spec = parse_text(text, path="specs/refund-request.md")
    doc = {
        "kind": "TaskPlan",
        "version": "tuvl/v2",
        "metadata": {"name": "refund-request", "spec": spec.path, "spec_hash": spec.hash},
        "spec": {"tasks": tasks},
    }
    (tmp_path / "specs" / "refund-request.tasks.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False)
    )
    return tmp_path, spec, parse_structure(text)


def _codes(findings: list[Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in findings:
        out.setdefault(f.code, []).append(f.message)
    return out


def test_no_task_list_is_a_coverage_warning(tmp_path: Path) -> None:
    from tuvl_insight.spec import parse_text
    from tuvl_insight.spec.grammar import parse_structure
    from tuvl_insight.spec.tasks import plan_findings

    text = (Path(__file__).parent / "grammar" / "refund-request.md").read_text()
    findings = plan_findings(
        tmp_path, parse_text(text, path="specs/refund-request.md"), parse_structure(text)
    )
    assert [(f.code, f.severity) for f in findings] == [("P003", "warning")]


def test_task_list_findings(tmp_path: Path) -> None:
    from tuvl_insight.spec.tasks import plan_findings

    project, spec, structure = _plan_project(
        tmp_path,
        [
            {
                "id": "T1",
                "title": "Refund model",
                "type": "model",
                "target": {"model": "Refund"},
                "effort": {"size": "S"},
            },
            {
                "id": "T2",
                "title": "Load",
                "type": "agent",
                "target": {"workflow": "refund_request", "agent": "load_ordr"},
            },
            {"id": "T3", "title": "Nothing", "type": "agent"},
            {
                "id": "T4",
                "title": "Typed status",
                "type": "agent",
                "status": "done",
                "target": {"workflow": "refund_request", "agent": "check_amount"},
                "spec_ref": {"anchor": "flow-refund-request", "hash": "old"},
            },
            {
                "id": "T5",
                "title": "Ghost",
                "type": "test",
                "target": {"workflow": "refund_request", "example": "nope"},
            },
        ],
    )
    codes = _codes(plan_findings(project, spec, structure))
    assert any("T1" in m and "estimate" in m for m in codes["P004"])
    assert any("T4" in m and "hand-set status" in m for m in codes["P004"])
    assert any("T3" in m and "no target" in m for m in codes["P001"])
    assert any("load_ordr" in m for m in codes["P002"]) and any(
        "'nope'" in m for m in codes["P002"]
    )
    assert any("T4" in m and "changed" in m for m in codes["P005"])
    uncovered = " ".join(codes["P003"])
    assert "`classify_reason`" in uncovered and "a small refund is paid automatically" in uncovered
    assert "`pay_refund` calls an external system" in uncovered  # tool http → a credentials task
    assert "model Refund" not in uncovered and "model Order" not in uncovered  # Order is (existing)


def test_findings_carry_the_task_list_file_and_line(tmp_path: Path) -> None:
    from tuvl_insight.spec.tasks import plan_findings

    project, spec, structure = _plan_project(
        tmp_path, [{"id": "T1", "title": "x", "type": "model", "target": {"model": "Nope"}}]
    )
    p002 = next(f for f in plan_findings(project, spec, structure) if f.code == "P002")
    assert p002.file == "specs/refund-request.tasks.yaml" and p002.line > 1
