"""What an agent run used is read from the agent's answer and said when the command ends."""

from __future__ import annotations

import json

from tuvl_insight.coding_agents.runner import ClaudeAdapter, RunResult, Usage


def test_claude_reports_its_tokens() -> None:
    out = json.dumps(
        {
            "result": "ok",
            "total_cost_usd": 0.5,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": 7,
            },
        }
    )
    r = ClaudeAdapter().parse(out, "", 0)
    assert (r.input_tokens, r.output_tokens) == (1110, 7)


def test_usage_line_adds_up_runs_and_names_agents_that_do_not_report() -> None:
    u = Usage()
    assert u.line() == ""
    u.add("claude", RunResult(True, input_tokens=1000, output_tokens=200, cost_usd=0.25))
    u.add("claude", RunResult(True, input_tokens=500, output_tokens=50, cost_usd=0.25))
    assert u.line() == "tokens · 1,500 in · 250 out · $0.500 · 2 agent call(s)"
    u.add("codex", RunResult(True))
    assert "not reported by codex" in u.line()
