"""`tuvl review`: the C-rules and ruff, then the agent's second opinion."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight.audit import read_events
from tuvl_insight.coding_agents import Settings, write_settings

runner = CliRunner()
FAKE = Path(__file__).resolve().parent.parent / "specgen" / "fake_agent.py"

WORKFLOW = """kind: Workflow
version: tuvl/v2
metadata: {name: w}
spec:
  trigger: {input: {x: int}}
  agents:
    - id: score
      description: score it
      engine: code
      effect: read
      code: {run: score}
      inputs: {x: int}
      outputs: {y: int}
      routes: {ok: END, error: END.failed}
"""
GOOD = '''from tuvl import Ctx, agent


@agent("score")
async def score(inp, ctx):
    """Score it"""
    return {"y": inp.x * 2}, "ok"
'''
BAD = GOOD.replace(
    'return {"y": inp.x * 2}, "ok"',
    'import subprocess\n    subprocess.run(["ls"])\n    return {"y": 1}, "high"',
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gc"))
    (tmp_path / "gc").write_text("[user]\n\tname = Ada\n\temail = a@b.c\n")
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])
    monkeypatch.setattr("tuvl_insight.cli.agent_run.detect", lambda *a, **k: [])
    p = tmp_path / "p"
    (p / "agents").mkdir(parents=True)
    (p / "workflows").mkdir()
    (p / "workflows" / "w.yaml").write_text(WORKFLOW)
    (p / "agents" / "score.py").write_text(GOOD)
    (p / ".gitignore").write_text(".tuvl/\n")
    git(p, "init", "-q", "-b", "develop")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "init")
    write_settings(p, Settings("AL", "generic", "", (sys.executable, str(FAKE))))
    return p


def review(project: Path, *args: str, review_mode: str | None = None):
    if review_mode:
        os.environ["FAKE_REVIEW"] = review_mode
    try:
        return runner.invoke(app, ["review", "code", "-d", str(project), *args])
    finally:
        os.environ.pop("FAKE_REVIEW", None)


def test_a_clean_agent_passes(project: Path) -> None:
    done = review(project, "--no-ruff")
    assert done.exit_code == 0 and "1 file(s) · 0 error(s)" in done.output


def test_findings_name_the_rule_file_and_line(project: Path) -> None:
    (project / "agents" / "score.py").write_text(BAD)
    done = review(project, "--no-ruff", "--json")
    report = json.loads(done.output)
    assert done.exit_code == 1 and report["ok"] is False
    found = {(f["code"], f["line"]) for f in report["findings"]}
    assert ("C002", 8) in found or any(c == "C002" for c, _ in found)
    assert any(f["code"] == "C011" and "high" in f["message"] for f in report["findings"])
    assert {f["file"] for f in report["findings"]} == {"agents/score.py"}


def test_ruff_findings_are_reported_as_c900(project: Path) -> None:
    (project / "agents" / "score.py").write_text(
        GOOD.replace("from tuvl import Ctx, agent", "import os\nfrom tuvl import Ctx, agent")
    )
    done = review(project, "--json")
    report = json.loads(done.output)
    ruff = [f for f in report["findings"] if f["code"] == "C900"]
    assert ruff and "F401" in ruff[0]["message"]
    assert done.exit_code == 0  # an unused import is a warning
    assert review(project, "--strict").exit_code == 1  # --strict makes warnings fail
    assert review(project, "--no-ruff", "--strict").exit_code == 0


def test_only_the_files_changed_since_a_branch_can_be_reviewed(project: Path) -> None:
    (project / "agents" / "other.py").write_text(BAD.replace('"score"', '"other"'))
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "other")
    all_files = json.loads(review(project, "--no-ruff", "--json").output)["files"]
    assert sorted(all_files) == ["agents/other.py", "agents/score.py"]
    changed = json.loads(review(project, "--no-ruff", "--json", "--base", "HEAD~1").output)["files"]
    assert changed == ["agents/other.py"]


def test_paths_and_explain(project: Path) -> None:
    (project / "agents" / "score.py").write_text(BAD)
    only = review(project, "--no-ruff", "--json", str(project / "agents" / "score.py"))
    assert json.loads(only.output)["files"] == ["agents/score.py"]
    done = review(project, "--explain", "c005")
    assert done.exit_code == 0 and "C005" in done.output and "ctx.storage" in done.output
    assert review(project, "--explain", "C999").exit_code == 2


def test_a_project_without_python_says_so(tmp_path: Path) -> None:
    done = runner.invoke(app, ["review", "code", "-d", str(tmp_path)])
    assert done.exit_code == 0 and "No Python to review" in done.output


# ── the agent's opinion ──────────────────────────────────────────────────────


def test_the_agent_review_is_advisory_unless_made_blocking(project: Path) -> None:
    done = review(project, "--no-ruff", "--agent", "--consent", review_mode="concerns")
    assert done.exit_code == 0
    flat = " ".join(done.output.split())
    assert "Agent review" in flat and "uses float for money" in flat and "use Decimal" in flat
    kinds = [e["kind"] for e in read_events(project)]
    assert kinds.count("review.finding") == 2 and kinds[-1] == "review.completed"
    blocked = review(project, "--no-ruff", "--agent", "--blocking", "high", review_mode="concerns")
    assert blocked.exit_code == 1
    only_low = review(project, "--no-ruff", "--agent", "--blocking", "high", review_mode="clean")
    assert only_low.exit_code == 0


def test_an_unusable_review_fails_instead_of_passing(project: Path) -> None:
    done = review(project, "--no-ruff", "--agent", "--consent", review_mode="garbage")
    assert done.exit_code == 1 and "unusable" in " ".join(done.output.split())


def test_the_reviewer_cannot_change_files(project: Path) -> None:
    done = review(project, "--no-ruff", "--agent", "--consent", review_mode="writes")
    assert done.exit_code == 0
    assert not (project / "agents" / "injected.py").exists()
    assert "undone" in " ".join(done.output.split())


def test_the_agent_review_needs_consent_and_python(project: Path, tmp_path: Path) -> None:
    refused = review(project, "--agent", review_mode="clean")
    assert refused.exit_code == 1 and "consent" in refused.output
    empty = tmp_path / "empty"
    empty.mkdir()
    write_settings(empty, Settings("AL", "generic", "x", (sys.executable, str(FAKE))))
    none = runner.invoke(app, ["review", "code", "-d", str(empty), "--agent"])
    assert none.exit_code == 0 and "No Python" in none.output


def test_the_review_goes_to_json(project: Path) -> None:
    done = review(project, "--no-ruff", "--agent", "--consent", "--json", review_mode="concerns")
    data = json.loads(done.output)
    assert (
        data["agent_review"]["verdict"] == "concerns" and len(data["agent_review"]["findings"]) == 2
    )
    assert data["agent_review"]["findings"][0]["rule"] == "money-float"


def test_the_review_can_be_posted_on_the_pull_request(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl_insight import gitutil

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\necho "$@" > {tmp_path}/gh.args\necho https://github.com/a/b/pull/3\n'
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    real = gitutil.out
    monkeypatch.setattr(
        gitutil,
        "out",
        lambda p, *a: "git@github.com:a/b.git" if a[:2] == ("remote", "get-url") else real(p, *a),
    )
    done = review(project, "--no-ruff", "--agent", "--consent", "--post", review_mode="concerns")
    assert done.exit_code == 0 and "pull/3" in done.output
    sent = (tmp_path / "gh.args").read_text()
    assert sent.startswith("pr comment --body") and "Advisory: a person decides" in sent
