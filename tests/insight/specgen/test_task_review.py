"""The review step inside `codegen --spec`: static errors block a task, the agent's opinion is advisory by default."""

import json
import subprocess
from pathlib import Path

import pytest

from tuvl_insight import taskrun
from tuvl_insight.audit import read_events
from tuvl_insight.coding_agents import runner
from tuvl_insight.taskrun import Job, ReviewPlan

JOB = Job("TASK-SK-0004-01-002", "score", "agent", {"workflow": "w", "agent": "score"}, "flow-w")
GOOD = 'from tuvl import agent\n\n\n@agent("score")\nasync def score(inp, ctx):\n    """d"""\n    return {"y": 1}, "ok"\n'
BAD = GOOD.replace(
    'return {"y": 1}, "ok"',
    'import subprocess\n    subprocess.run(["ls"])\n    return {"y": 1}, "ok"',
)
CONCERNS = json.dumps(
    {
        "verdict": "concerns",
        "findings": [
            {
                "file": "agents/score.py",
                "line": 7,
                "severity": "high",
                "rule": "wrong-total",
                "summary": "double counts",
            },
            {
                "file": "agents/score.py",
                "line": 7,
                "severity": "low",
                "rule": "no-example",
                "summary": "no example",
            },
        ],
    }
)


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "a@b.c"],
        ["config", "user.name", "A"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / ".gitkeep").touch()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def call(
    wt: Path,
    project: Path,
    plan: ReviewPlan,
    run=None,
    *,
    source: str = GOOD,
    changed=("agents/score.py",),
):
    (wt / "agents" / "score.py").write_text(source)
    return taskrun._review(
        project,
        wt,
        JOB,
        list(changed),
        plan,
        spec_id="SPEC-SK-0004-01",
        flow_text="## Flow: w",
        run=run
        or (lambda *a, **k: runner.RunResult(True, text=CONCERNS, stdout=CONCERNS, cost_usd=0.03)),
    )


def agent_plan(blocking: str = "off") -> ReviewPlan:
    return ReviewPlan(
        "agent",
        blocking,
        runner.ClaudeAdapter(),
        "claude",
        "Claude Code 2",
        runner.Limits(timeout=5),
    )


def test_a_step_without_python_is_not_reviewed(wt: Path, tmp_path: Path) -> None:
    called = []
    found, md = call(
        wt,
        tmp_path / "proj",
        agent_plan(),
        run=lambda *a, **k: called.append(1),
        changed=("workflows/w.yaml",),
    )
    assert found == {"blocking": [], "cost": 0.0} and md == "" and called == []


def test_a_static_error_blocks_the_task(wt: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    found, md = call(wt, project, ReviewPlan("static"), source=BAD)
    assert (
        md == ""
        and len(found["blocking"]) == 1
        and found["blocking"][0].startswith("C002 agents/score.py:")
    )
    done = read_events(project)[-1]
    assert done["kind"] == "review.completed" and done["result"] == "failed"


def test_clean_python_passes_static_without_calling_the_agent(wt: Path, tmp_path: Path) -> None:
    called = []
    found, md = call(
        wt, tmp_path / "proj", ReviewPlan("static"), run=lambda *a, **k: called.append(1)
    )
    assert found["blocking"] == [] and md == "" and called == []


def test_the_agents_opinion_is_advisory_and_goes_to_the_pull_request(
    wt: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    found, md = call(wt, project, agent_plan())
    assert found["blocking"] == [] and found["cost"] == 0.03
    assert "2 finding(s). Advisory" in md and "double counts" in md
    kinds = [e["kind"] for e in read_events(project)]
    assert kinds == ["review.requested", "review.finding", "review.finding", "review.completed"]


def test_blocking_findings_fail_the_task_with_what_to_fix(wt: Path, tmp_path: Path) -> None:
    found, _ = call(wt, tmp_path / "proj", agent_plan("high"))
    assert (
        len(found["blocking"]) == 1
        and "[high] agents/score.py:7 wrong-total: double counts" in found["blocking"][0]
    )
    found, _ = call(wt, tmp_path / "proj", agent_plan("low"))
    assert len(found["blocking"]) == 2
    found, _ = call(wt, tmp_path / "proj", agent_plan("off"))
    assert found["blocking"] == []


def test_a_review_that_could_not_be_done_does_not_block(wt: Path, tmp_path: Path) -> None:
    broken = lambda *a, **k: runner.RunResult(False, error="boom")  # noqa: E731
    found, md = call(wt, tmp_path / "proj", agent_plan("high"), run=broken)
    assert found["blocking"] == [] and "could not be completed" in md


def test_the_review_prompt_holds_the_diff_the_files_and_the_spec(wt: Path, tmp_path: Path) -> None:
    seen = {}

    def run(adapter, binary, prompt, *, workdir, limits, env=None):
        seen["prompt"] = prompt
        seen["read_only"] = limits.read_only
        return runner.RunResult(True, text='{"findings": []}', stdout="")

    call(wt, tmp_path / "proj", agent_plan(), run=run)
    assert "TASK-SK-0004-01-002 — score (SPEC-SK-0004-01)" in seen["prompt"]
    assert "+async def score" in seen["prompt"] and "<<<FILE agents/score.py" in seen["prompt"]
    assert "## Flow: w" in seen["prompt"] and seen["read_only"] is True
