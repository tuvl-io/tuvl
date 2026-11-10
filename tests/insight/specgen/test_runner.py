import json
import sys
from pathlib import Path

import pytest

from tuvl_insight.coding_agents import runner

# what `claude -p --output-format json` printed in a real run (trimmed)
CLAUDE_OK = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "I created hello.txt.",
        "total_cost_usd": 0.0267,
        "num_turns": 2,
        "permission_denials": [],
    }
)


def test_the_claude_command_is_restricted_and_takes_the_prompt_on_stdin() -> None:
    argv = runner.ClaudeAdapter().command(
        "/bin/claude", runner.Limits(budget_usd=0.5, model="sonnet")
    )
    assert argv[:2] == ["/bin/claude", "-p"]
    for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert (
        argv[argv.index("--max-budget-usd") + 1] == "0.5"
        and argv[argv.index("--model") + 1] == "sonnet"
    )
    assert "--dangerously-skip-permissions" not in argv and "Bash" not in " ".join(argv)
    assert "--max-budget-usd" not in runner.ClaudeAdapter().command("c", runner.Limits())


def test_claude_output_is_parsed() -> None:
    ok = runner.ClaudeAdapter().parse(CLAUDE_OK, "", 0)
    assert ok.ok and ok.text == "I created hello.txt." and ok.cost_usd == 0.0267 and ok.turns == 2
    bad = runner.ClaudeAdapter().parse(
        json.dumps({"is_error": True, "subtype": "error_max_budget_usd", "result": "budget"}), "", 1
    )
    assert not bad.ok and "budget" in bad.error
    denied = runner.ClaudeAdapter().parse(
        json.dumps(
            {"subtype": "success", "result": "x", "permission_denials": [{"tool_name": "Bash"}]}
        ),
        "",
        0,
    )
    assert denied.ok and denied.denials == ["Bash"]
    assert not runner.ClaudeAdapter().parse("not json", "boom", 1).ok
    assert "boom" in runner.ClaudeAdapter().parse("not json", "boom", 1).error


def test_unknown_agents_and_a_commandless_generic_are_refused() -> None:
    with pytest.raises(runner.AgentError, match="cannot drive 'aider'"):
        runner.adapter_for("aider")
    with pytest.raises(runner.AgentError, match="no command"):
        runner.adapter_for("generic")
    with pytest.raises(runner.AgentError, match="not found"):
        runner.binary_for("generic", ("definitely-not-a-program",))


def test_run_agent_passes_the_prompt_on_stdin_in_the_working_directory(tmp_path: Path) -> None:
    script = tmp_path / "echo.py"
    script.write_text("import sys, os\nprint(os.getcwd() + '|' + sys.stdin.read())\n")
    adapter = runner.adapter_for("generic", (sys.executable, str(script)))
    work = tmp_path / "w"
    work.mkdir()
    result = runner.run_agent(
        adapter, sys.executable, "the prompt", workdir=work, limits=runner.Limits(timeout=20)
    )
    assert result.ok and result.text.endswith("|the prompt") and str(work.resolve()) in result.text


def test_run_agent_reports_failure_timeout_and_a_missing_binary(tmp_path: Path) -> None:
    fail = tmp_path / "fail.py"
    fail.write_text("import sys\nprint('nope', file=sys.stderr)\nsys.exit(4)\n")
    adapter = runner.adapter_for("generic", (sys.executable, str(fail)))
    failed = runner.run_agent(
        adapter, sys.executable, "p", workdir=tmp_path, limits=runner.Limits(timeout=20)
    )
    assert not failed.ok and failed.returncode == 4 and "nope" in failed.error
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(30)\n")
    timed = runner.run_agent(
        runner.adapter_for("generic", (sys.executable, str(slow))),
        sys.executable,
        "p",
        workdir=tmp_path,
        limits=runner.Limits(timeout=0.5),
    )
    assert not timed.ok and "timed out" in timed.error
    missing = runner.run_agent(
        runner.adapter_for("generic", ("/no/such/binary",)),
        "/no/such/binary",
        "p",
        workdir=tmp_path,
        limits=runner.Limits(),
    )
    assert not missing.ok and "could not start" in missing.error
