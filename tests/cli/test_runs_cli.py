"""tuvl runs: SSE parsing and rendering, replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tuvl.cli.commands.runs import parse_sse, render


def test_parse_sse_blocks_and_comments() -> None:
    lines = [
        "id: 1",
        "event: run_queued",
        'data: {"seq": 1, "type": "run_queued"}',
        "",
        ": keep-alive",
        "",
        "id: 2",
        "event: agent_completed",
        'data: {"seq": 2, "type": "agent_completed", "agent_id": "a", "payload": {"signal": "default"}}',
        "",
    ]
    events = list(parse_sse(lines))
    assert [e["seq"] for e in events] == [1, 2]


def test_render_failures_and_ends() -> None:
    failed = render(
        {"seq": 9, "type": "run_failed", "payload": {"error_type": "http_error", "message": "502"}}
    )
    assert "run_failed" in failed and "http_error" in failed
    done = render(
        {
            "seq": 7,
            "type": "run_completed",
            "payload": {"end": "default", "status": 200},
            "tokens": 40,
        }
    )
    assert "END.default (200)" in done and "40 tok" in done


@pytest.mark.requires_insight
def test_replay_checks_a_pinned_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tuvl.cli import app

    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "w.yaml").write_text(
        "kind: Workflow\nversion: tuvl/v2\nmetadata: { name: w }\nspec:\n"
        "  trigger: { input: { text: str } }\n"
        "  agents:\n"
        "    - { id: shout, description: Shout., engine: llm, inputs: { text: str }, outputs: { loud: str },\n"
        "        budget: { timeout: 5s, max_tokens: 10 }, llm: { model: openai/x, prompt: Shout. },\n"
        "        routes: { default: END, error: END.failed, parse_error: END.failed, timeout: END.failed,"
        " budget_exceeded: END.failed } }\n"
        "  outputs: { default: { body: { loud: '{{ loud }}' } }, failed: { status: 500 } }\n"
    )
    journal = {
        "format": "tuvl.journal/v1",
        "run": {
            "workflow": "w",
            "status": "completed",
            "end_name": "default",
            "input": {"text": "hi"},
            "output": {"status": 200, "body": {"loud": "HI"}},
        },
        "events": [],
        "checkpoints": [
            {
                "seq": 2,
                "agent_id": "shout",
                "signal": "default",
                "next": None,
                "context": {"text": "hi", "loud": "HI"},
                "completed": ["shout"],
            }
        ],
    }
    fixture = tmp_path / "tests" / "fixtures" / "w" / "r.journal.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(json.dumps(journal))
    runner = CliRunner()
    same = runner.invoke(app, ["replay", "runs", str(fixture), "-d", str(tmp_path)])
    assert same.exit_code == 0, same.output
    journal["run"]["output"]["body"]["loud"] = "HELLO"  # type: ignore[index]
    fixture.write_text(json.dumps(journal))
    diff = runner.invoke(app, ["replay", "runs", str(fixture), "-d", str(tmp_path), "--json"])
    assert diff.exit_code == 1
    report = json.loads(diff.output[diff.output.index("{") :])
    assert "output.body.loud: expected 'HELLO', got 'HI'" in report["replays"][0]["failures"]
