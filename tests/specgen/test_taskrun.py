import pytest

from tuvl_insight import taskrun
from tuvl_insight.taskrun import Job


def jobs() -> list[Job]:
    return [
        Job("TASK-SK-0004-01-001", "Refund model", "model", {"model": "Refund"}),
        Job(
            "TASK-SK-0004-01-002",
            "read",
            "agent",
            {"workflow": "w", "agent": "read"},
            "flow-w",
            ["TASK-SK-0004-01-001"],
        ),
        Job(
            "TASK-SK-0004-01-003",
            "store",
            "agent",
            {"workflow": "w", "agent": "store"},
            "flow-w",
            ["TASK-SK-0004-01-002"],
        ),
    ]


def test_task_tokens_in_any_form() -> None:
    j = jobs()
    for token in ("2", "002", "T2", "TASK-SK-0004-01-002"):
        assert [x.id for x in taskrun.resolve_tokens(j, [token])] == ["TASK-SK-0004-01-002"]
    assert [x.id for x in taskrun.resolve_tokens(j, ["3,1"])] == [
        "TASK-SK-0004-01-003",
        "TASK-SK-0004-01-001",
    ]
    assert len(taskrun.resolve_tokens(j, ["2", "2"])) == 1
    with pytest.raises(taskrun.TaskRunError, match="no task '9'"):
        taskrun.resolve_tokens(j, ["9"])


def test_legacy_task_ids_work_too() -> None:
    legacy = [Job("T1", "a", "agent", {}), Job("T12", "b", "agent", {})]
    assert [x.id for x in taskrun.resolve_tokens(legacy, ["12"])] == ["T12"]


def test_order_puts_dependencies_first_and_ignores_unselected_ones() -> None:
    j = jobs()
    assert [x.id[-3:] for x in taskrun.order([j[2], j[1]])] == ["002", "003"]
    assert [x.id[-3:] for x in taskrun.order([j[2]])] == [
        "003"
    ]  # its dependency is not part of this run
    circle = [
        Job("T1", "a", "agent", {}, depends_on=["T2"]),
        Job("T2", "b", "agent", {}, depends_on=["T1"]),
    ]
    with pytest.raises(taskrun.TaskRunError, match="circle"):
        taskrun.order(circle)


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("workflows/items.yaml", True),
        ("agents/verify_totals.py", True),
        ("artifacts/extract.md", True),
        ("agents/_generated/schemas.py", False),
        ("agents/notes.txt", False),
        ("specs/SPEC-SK-0004-01.md", False),
        ("idea/IDEA-SK-0004/source/a.md", False),
        ("tests/items/a.yaml", False),
        ("models/item.yaml", False),
        (".tuvl/agent.yaml", False),
        ("tuvl.lock", False),
        ("pyproject.toml", False),
    ],
)
def test_what_a_task_may_touch(path: str, ok: bool) -> None:
    assert taskrun.allowed(path) is ok


def test_the_prompt_names_the_step_the_rules_and_any_feedback() -> None:
    flow = "## Flow: w\n1. `read` — llm default · in `text`\n   - default → `store`\n2. `store` — tool db create Refund · in `x`\n   - default → END"
    assert taskrun._step_text(flow, "read").splitlines()[0].startswith("1. `read`")
    assert "tool db create" not in taskrun._step_text(flow, "read")
    assert taskrun._step_text(flow, "store").endswith("default → END")
    assert taskrun._step_text(flow, "nope") == ""
    job = jobs()[1]
    prompt = taskrun.build_prompt(
        job=job,
        spec_id="SPEC-SK-0004-01",
        engine="llm",
        step_text=taskrun._step_text(flow, "read"),
        flow_text=flow,
        data_text="# Data",
        workflow_path="workflows/w.yaml",
        workflow_text="engine: pending",
    )
    assert (
        "step `read` is still `engine: pending`" in prompt and "is in `workflows/w.yaml`" in prompt
    )
    assert "never `agents/_generated/`" in prompt and "You cannot run anything" in prompt
    assert "How to write a `llm` agent" in prompt and "engine: pending" in prompt
    assert "not accepted yet" not in prompt
    retry = taskrun.build_prompt(
        job=job,
        spec_id="s",
        engine="llm",
        step_text="",
        flow_text="",
        data_text="",
        workflow_path="workflows/w.yaml",
        workflow_text="",
        feedback=["[V004] bad route"],
    )
    assert "not accepted yet" in retry and "- [V004] bad route" in retry


def test_branch_names_use_the_task_id() -> None:
    assert taskrun.task_branch(jobs()[1], "SPEC-SK-0004-01") == "feature/TASK-SK-0004-01-002"
    assert taskrun.task_branch(Job("T3", "x", "agent", {}), "refunds") == "feature/refunds-T3"
