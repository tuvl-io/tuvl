"""The adapters beyond Claude Code: what each is asked to do, and which may write."""

import json
import stat
import sys
from pathlib import Path

import pytest

from tuvl_insight.coding_agents import runner

LIM = runner.Limits(timeout=30)
RO = runner.Limits(timeout=30, read_only=True, model="m1")


def cmd(agent: str, limits: runner.Limits = LIM) -> list[str]:
    return runner.adapter_for(agent).command(f"/bin/{agent}", limits)


def test_codex_writes_only_inside_the_folder_and_reads_its_prompt_from_stdin() -> None:
    argv = cmd("codex")
    assert argv[:2] == ["/bin/codex", "exec"] and argv[-1] == "-"  # `-` is stdin
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--skip-git-repo-check" in argv and "--ephemeral" in argv
    review = cmd("codex", RO)
    assert (
        review[review.index("--sandbox") + 1] == "read-only"
        and review[review.index("--model") + 1] == "m1"
    )


def test_gemini_approves_edits_only_and_reviews_in_plan_mode() -> None:
    assert cmd("gemini") == ["/bin/gemini", "--approval-mode", "auto_edit"]
    assert cmd("gemini", RO) == ["/bin/gemini", "--approval-mode", "plan", "--model", "m1"]
    assert "yolo" not in " ".join(cmd("gemini") + cmd("gemini", RO))


@pytest.mark.parametrize("agent", ["cursor", "copilot", "amp"])
def test_the_review_only_agents_take_the_prompt_as_an_argument_and_never_get_a_write_grant(
    agent: str,
) -> None:
    adapter = runner.adapter_for(agent)
    assert adapter.stdin is False and adapter.can_write is False
    joined = " ".join(cmd(agent, RO) + cmd(agent))
    for dangerous in (
        "--force",
        "--yolo",
        "--dangerously-skip-permissions",
        "--allow-all",
        "--allow-tool",
        "auto_edit",
    ):
        assert dangerous not in joined
    assert runner.capability(agent) == "review"


def test_the_flags_each_review_agent_uses() -> None:
    assert cmd("cursor", RO) == ["/bin/cursor", "--model", "m1", "-p"]
    assert cmd("copilot", RO) == ["/bin/copilot", "-s", "--model=m1", "-p"]
    assert cmd("amp") == ["/bin/amp", "-x"]
    assert cmd("antigravity", RO)[-1] == "-p" and "--print-timeout" in cmd("antigravity", RO)


def test_antigravity_may_edit_the_working_folder_only_and_reviews_without_that_mode() -> None:
    build, review = cmd("antigravity"), cmd("antigravity", RO)
    assert build[build.index("--mode") + 1] == "accept-edits" and build[-1] == "-p"
    assert "--mode" not in review
    joined = " ".join(build + review)
    for dangerous in ("--dangerously-skip-permissions", "--sandbox", "plan"):
        assert dangerous not in joined
    assert runner.adapter_for("antigravity", write=True).can_write
    assert runner.capability("antigravity") == "write"


def test_an_antigravity_denial_is_reported_though_the_agent_exits_zero() -> None:
    stderr = (
        'jetski: no output produced — a tool required the "write_file" permission that headless mode '
        "cannot prompt for, so it was auto-denied."
    )
    result = runner.adapter_for("antigravity").parse("", stderr, 0)
    assert result.ok and result.denials == ["write_file"]
    assert runner.adapter_for("antigravity").parse("DONE", "", 0).denials == []


def test_who_may_write() -> None:
    assert set(runner.WRITERS) == {"claude", "codex", "gemini", "antigravity", "generic"}
    assert runner.capability("codex") == "write" and runner.capability("aider") == ""
    for agent in ("cursor", "copilot", "amp"):
        with pytest.raises(
            runner.AgentError, match="can review code through tuvl, but cannot draft or build"
        ):
            runner.adapter_for(agent, write=True)
        runner.adapter_for(agent)  # reviewing is fine
    for agent in ("claude", "codex", "gemini", "antigravity"):
        assert runner.adapter_for(agent, write=True).can_write


def test_a_plain_answer_is_the_standard_output() -> None:
    adapter = runner.adapter_for("gemini")
    ok = adapter.parse("  the answer\n", "progress noise", 0)
    assert ok.ok and ok.text == "the answer" and ok.cost_usd is None
    bad = adapter.parse("", "quota exceeded", 1)
    assert not bad.ok and "quota exceeded" in bad.error


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    path = tmp_path / "fake"
    path.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        "data = sys.stdin.read() if not sys.stdin.isatty() else ''\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': data}))\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_prompt_goes_where_the_adapter_says(fake_cli: Path, tmp_path: Path) -> None:
    on_stdin = runner.run_agent(
        runner.adapter_for("gemini"), str(fake_cli), "PROMPT", workdir=tmp_path, limits=LIM
    )
    seen = json.loads(on_stdin.text)
    assert seen["stdin"] == "PROMPT" and "PROMPT" not in seen["argv"]
    as_arg = runner.run_agent(
        runner.adapter_for("cursor"), str(fake_cli), "PROMPT", workdir=tmp_path, limits=LIM
    )
    seen = json.loads(as_arg.text)
    assert seen["argv"][-2:] == ["-p", "PROMPT"] and seen["stdin"] == ""


def test_a_prompt_too_long_for_an_argument_is_refused_clearly(
    fake_cli: Path, tmp_path: Path
) -> None:
    result = runner.run_agent(
        runner.adapter_for("copilot"), str(fake_cli), "x" * 100_001, workdir=tmp_path, limits=LIM
    )
    assert (
        not result.ok and "too long for copilot" in result.error and "another agent" in result.error
    )
    assert runner.run_agent(
        runner.adapter_for("gemini"), str(fake_cli), "x" * 100_001, workdir=tmp_path, limits=LIM
    ).ok


def test_the_generic_agent_is_unchanged(tmp_path: Path) -> None:
    script = tmp_path / "g.py"
    script.write_text("import sys\nprint(len(sys.stdin.read()))\n")
    result = runner.run_agent(
        runner.adapter_for("generic", (sys.executable, str(script))),
        sys.executable,
        "abc",
        workdir=tmp_path,
        limits=LIM,
    )
    assert result.ok and result.text == "3"
