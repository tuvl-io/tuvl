"""Dev API v2 (P4.0): project, text files, validate, schema, codegen, tests, specs, layout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tuvl.api.dev import router
from tuvl.core.config import use_project
from tuvl_insight.cli.json_output import COMMANDS


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("dev")
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("TUVL_", "GEMINI_", "POSTGRES_"))
    }
    made = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tuvl.cli import app; app()",
            "init",
            "demo",
            "--sample",
            "--preset",
            "ollama",
            "-y",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert made.returncode == 0, made.stdout + made.stderr
    return root / "demo"


@pytest.fixture
async def client(project: Path) -> AsyncIterator[httpx.AsyncClient]:
    from tuvl.core.config_loader import load_all_configs

    use_project(project)
    load_all_configs(project)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev"
    ) as c:
        yield c


async def test_project_and_text_files(client: httpx.AsyncClient, project: Path) -> None:
    summary = (await client.get("/dev/project/summary")).json()
    assert summary["workflows"][0]["name"] == "triage_ticket" and summary["lock"] is True
    assert summary["workflows"][0]["unavailable"] == []
    assert summary["specs"] == [
        {"path": "specs/triage-ticket.md", "name": "triage-ticket", "has_plan": True}
    ]
    assert "triage_ticket.yaml" in (await client.get("/dev/text/workflows")).json()
    text = (await client.get("/dev/text/workflows/triage_ticket.yaml")).text
    assert text.startswith("# ╔")  # comments come back verbatim
    saved = await client.put(
        "/dev/text/workflows/triage_ticket.yaml",
        content=text + "# kept\n",
        headers={"content-type": "text/plain"},
    )
    assert saved.status_code == 200 and (
        project / "workflows/triage_ticket.yaml"
    ).read_text().endswith("# kept\n")
    bad = await client.put(
        "/dev/text/workflows/x.yaml", content="a: [", headers={"content-type": "text/plain"}
    )
    assert bad.status_code == 422
    assert (await client.get("/dev/text/workflows/..%2F..%2Fetc%2Fpasswd.yaml")).status_code in (
        400,
        404,
    )
    assert (await client.get("/dev/text/secrets/x.yaml")).status_code == 400
    assert (await client.get("/dev/text/specs/triage-ticket.md")).text.startswith("---")


async def test_checks_match_the_cli_json(client: httpx.AsyncClient) -> None:
    COMMANDS["lint project"].model_validate((await client.post("/dev/validate")).json())
    tests = (await client.post("/dev/tests/run", json={})).json()
    COMMANDS["run tests"].model_validate(tests)
    assert tests["passed"] is True
    status = (await client.get("/dev/specs/status")).json()
    COMMANDS["list tasks"].model_validate(status)
    assert status["done"] is True
    COMMANDS["show drift"].model_validate((await client.get("/dev/specs/diff")).json())
    COMMANDS["lint spec"].model_validate((await client.get("/dev/specs/check")).json())
    verify = (await client.get("/dev/specs/verify")).json()
    COMMANDS["verify spec"].model_validate(verify)
    assert verify["ok"] is True
    assert (await client.post("/dev/codegen", json={})).json()["would_change"] == []
    assert (await client.get("/dev/schema/Workflow")).json()["$id"].endswith("workflow.json")
    assert "list tasks" in (await client.get("/dev/schema/cli")).json()


async def test_specs_new_and_layout(client: httpx.AsyncClient, project: Path) -> None:
    created = await client.post("/dev/specs/new", json={"name": "Refund Automation"})
    assert created.json() == {"created": "specs/refund-automation.md"}
    assert (
        await client.post("/dev/specs/new", json={"name": "refund-automation"})
    ).status_code == 409
    assert (
        await client.post("/dev/specs/analyse", json={"spec": "specs/refund-automation.md"})
    ).status_code in (404, 405)
    assert (await client.get("/dev/layout/triage_ticket")).json() == {}
    await client.put("/dev/layout/triage_ticket", json={"classify": {"x": 10, "y": 20}})
    assert (await client.get("/dev/layout/triage_ticket")).json() == {
        "classify": {"x": 10, "y": 20}
    }
    assert (project / ".tuvl/layout/triage_ticket.json").is_file()
    assert (await client.get("/dev/layout/..%2Fx")).status_code in (400, 404)


async def test_replay_endpoint(client: httpx.AsyncClient, project: Path) -> None:
    fixture = project / "tests" / "fixtures" / "triage_ticket" / "routine.journal.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(
        '{"format": "tuvl.journal/v1", "events": [], "run": {"workflow": "triage_ticket", "status": "completed",'
        ' "end_name": "default", "input": {"subject": "s", "body": "b", "customer_email": "a@example.com"},'
        ' "output": {"status": 201}}, "checkpoints": [{"seq": 2, "agent_id": "classify", "signal": "default",'
        ' "next": "prioritise", "context": {"category": "account", "urgency": "low", "summary": "x"}}]}'
    )
    report = (
        await client.post(
            "/dev/runs/replay",
            json={"fixtures": ["tests/fixtures/triage_ticket/routine.journal.json"]},
        )
    ).json()
    COMMANDS["replay runs"].model_validate(report)
    assert report["passed"] is True, report
    assert (
        await client.post("/dev/runs/replay", json={"fixtures": ["../../etc/passwd"]})
    ).status_code == 400


async def test_validate_draft_checks_unsaved_text(client: httpx.AsyncClient, project: Path) -> None:
    path = project / "workflows" / "triage_ticket.yaml"
    before = path.read_text()
    broken = before.replace("default: prioritise", "default: nowhere", 1)
    report = (
        await client.post(
            "/dev/validate/draft", json={"name": "triage_ticket.yaml", "text": broken}
        )
    ).json()
    COMMANDS["lint project"].model_validate(report)
    assert any("[V003]" in e["message"] for e in report["errors"])
    assert path.read_text() == before  # nothing written
    clean = (
        await client.post(
            "/dev/validate/draft", json={"name": "triage_ticket.yaml", "text": before}
        )
    ).json()
    assert clean["valid"] is True
    assert (await client.get("/dev/project/summary")).json()[
        "workflows"
    ]  # the process still points at the project


ROUTINE = {
    "format": "tuvl.journal/v1",
    "events": [],
    "run": {
        "workflow": "triage_ticket",
        "status": "completed",
        "end_name": "default",
        "input": {"subject": "Reset", "body": "How?", "customer_email": "a@example.com"},
        "output": {
            "status": 201,
            "body": {
                "id": "11111111-1111-1111-1111-111111111111",
                "category": "account",
                "urgency": "low",
            },
        },
    },
    "checkpoints": [
        {
            "seq": 2,
            "agent_id": "classify",
            "signal": "default",
            "next": "prioritise",
            "context": {"category": "account", "urgency": "low", "summary": "Asks how to reset."},
        },
        {"seq": 4, "agent_id": "prioritise", "signal": "normal", "next": "store", "context": {}},
        {
            "seq": 6,
            "agent_id": "store",
            "signal": "default",
            "next": "END",
            "context": {
                "ticket": {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "subject": "Reset",
                    "body": "How?",
                    "customer_email": "a@example.com",
                    "category": "account",
                    "urgency": "low",
                    "summary": "Asks how to reset.",
                }
            },
        },
    ],
}

AS_DECIDE = """      engine: decide
      outputs: { lane: "enum[urgent, normal]" }
      decide:
        rules:
          - { when: "urgency == 'high' or category == 'billing'", then: urgent }
          - { when: "true", then: normal }
"""


async def test_change_check_replays_fixtures_against_the_draft(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl.core.config import settings
    from tuvl.runtime import devtools

    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    monkeypatch.setitem(devtools.BREAKPOINTS, "triage_ticket", {"prioritise"})  # not for replays
    fixture = project / "tests" / "fixtures" / "triage_ticket" / "routine.journal.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(json.dumps(ROUTINE))
    text = (project / "workflows" / "triage_ticket.yaml").read_text()
    code_block = "      engine: code\n      effect: read\n      code: { run: prioritise_ticket }\n"
    assert code_block in text
    same = (
        await client.post(
            "/dev/change/check",
            json={
                "name": "triage_ticket.yaml",
                "agent": "prioritise",
                "text": text.replace(code_block, AS_DECIDE),
            },
        )
    ).json()
    assert same["fixtures"] >= 1 and same["passed"] is True, same["replays"]
    assert "worst-case" in same["before"] and same["after"]
    always_urgent = AS_DECIDE.replace("urgency == 'high' or category == 'billing'", "true")
    diff = (
        await client.post(
            "/dev/change/check",
            json={
                "name": "triage_ticket.yaml",
                "agent": "prioritise",
                "text": text.replace(code_block, always_urgent),
            },
        )
    ).json()
    assert diff["passed"] is False  # the routine ticket would now wait for a lead
    bad = await client.post(
        "/dev/change/check", json={"name": "triage_ticket.yaml", "agent": "x", "text": "kind: ["}
    )
    assert bad.status_code == 422


JUDGE = """kind: Artifact
metadata: { name: reply-judge, version: 1 }
spec:
  type: judge
  model: openai/judge-1
  rubric: The reply states the refund amount.
  calibration:
    - { input: fixtures/judge/ok-reply.json, expect: pass }
"""


async def test_judge_try_label_and_calibrate_a_draft(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from tuvl import llm as llm_client

    async def fake(**kwargs: Any) -> Any:
        verdict = "pass" if "refund of 10" in kwargs["messages"][1]["content"] else "fail"
        content = json.dumps({"verdict": verdict, "score": 0.9, "reasons": ["because"]})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(total_tokens=5),
        )

    monkeypatch.setattr(llm_client, "acompletion", fake)
    ok = {"reply": "You get a refund of 10 EUR within 5 days."}
    tried = await client.post(
        "/dev/judges/try", json={"name": "reply-judge", "text": JUDGE, "subject": ok}
    )
    assert tried.json()["verdict"] == "pass", tried.text
    labelled = await client.post("/dev/judges/label", json={"label": "ok-reply", "subject": ok})
    assert labelled.json() == {"input": "fixtures/judge/ok-reply.json"}
    assert json.loads((project / "fixtures/judge/ok-reply.json").read_text()) == ok
    assert (
        await client.post("/dev/judges/label", json={"label": "ok-reply", "subject": ok})
    ).status_code == 409
    assert (
        await client.post("/dev/judges/label", json={"label": "../x", "subject": ok})
    ).status_code == 400
    calibration = (
        await client.post("/dev/judges/calibrate", json={"name": "reply-judge", "text": JUDGE})
    ).json()
    assert calibration["agreement"] == 1.0 and calibration["trusted"] is True
    flipped = JUDGE.replace("expect: pass", "expect: fail")
    assert (
        await client.post("/dev/judges/calibrate", json={"name": "reply-judge", "text": flipped})
    ).json()["trusted"] is False
    bad = await client.post(
        "/dev/judges/try",
        json={"name": "x", "text": "kind: Artifact\nspec: {type: judge}", "subject": ok},
    )
    assert bad.status_code == 422


RULES = """kind: Artifact
metadata: { name: lanes, version: 1 }
spec:
  type: rules
  rules:
    - { when: "urgency == 'high'", then: urgent }
    - { when: "category == 'billing'", then: urgent }
"""


async def test_rules_try(client: httpx.AsyncClient) -> None:
    async def first(context: dict[str, Any]) -> dict[str, Any]:
        return (
            await client.post("/dev/rules/try", json={"text": RULES, "context": context})
        ).json()

    assert await first({"urgency": "high", "category": "account"}) == {
        "rule": 1,
        "then": "urgent",
        "error": None,
    }
    assert (await first({"urgency": "low", "category": "billing"}))["rule"] == 2
    assert (await first({"urgency": "low", "category": "account"}))["rule"] is None
    assert (
        await client.post("/dev/rules/try", json={"text": "spec: {type: rules}", "context": {}})
    ).status_code == 422


async def test_agent_source_shows_the_python_behind_code_run(client: httpx.AsyncClient) -> None:
    found = (await client.get("/dev/agents/prioritise_ticket/source")).json()
    assert found["file"] == "agents/prioritise_ticket.py" and found["line"] >= 1
    assert (
        '@agent("prioritise_ticket")' in found["source"]
        and "async def prioritise_ticket" in found["source"]
    )
    builtin = (await client.get("/dev/agents/tuvl.data_search/source")).json()
    assert builtin["builtin"] is True and builtin["source"] is None
    assert (await client.get("/dev/agents/nope/source")).json()["exists"] is False  # a stub


async def test_code_agent_editing_is_normalized_checked_and_reloaded(
    client: httpx.AsyncClient, project: Path
) -> None:
    path = project / "agents" / "prioritise_ticket.py"
    original = path.read_text()
    try:
        source = (await client.get("/dev/agents/prioritise_ticket/source")).json()
        assert source["file_text"] == original
        edited = original.replace('return "normal"', 'return "normal"  # edited in Insight')
        saved = await client.put("/dev/agents/prioritise_ticket/source", json={"text": edited})
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert "# edited in Insight" in path.read_text() and body["load_error"] is None
        broken = await client.put(
            "/dev/agents/prioritise_ticket/source", json={"text": edited + "\ndef (:\n"}
        )
        assert broken.status_code == 422 and broken.json()["detail"]["line"] is not None
        undecorated = await client.put(
            "/dev/agents/prioritise_ticket/source",
            json={"text": "async def x(inp, ctx):\n    return 1\n"},
        )
        assert (
            undecorated.status_code == 422 and "decorator" in undecorated.json()["detail"]["error"]
        )
        assert "# edited in Insight" in path.read_text()  # rejected saves never touch the file
    finally:
        path.write_text(original)


async def test_code_assist_returns_the_file_and_saves_nothing(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl import llm as llm_client

    seen: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        seen.update(kwargs)
        message = type(
            "M", (), {"content": "Billing tickets stay urgent.\n```python\nNEW = 1\n```"}
        )()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice], "usage": None})()

    monkeypatch.setattr(llm_client, "acompletion", fake)
    before = (project / "agents" / "prioritise_ticket.py").read_text()
    models = (await client.get("/dev/agents/assist/models")).json()
    assert any(m["name"] == "default" for m in models)
    reply = (
        await client.post(
            "/dev/agents/prioritise_ticket/assist",
            json={"prompt": "keep billing urgent", "code": before, "model": "default"},
        )
    ).json()
    assert reply["code"] == "NEW = 1\n" and reply["reply"] == "Billing tickets stay urgent."
    system = seen["messages"][0]["content"]
    assert "prioritise" in system and "urgent" in system and "ctx.db" in system  # contract + skill
    assert '"Ticket"' in system and "plain dicts" in system  # the models' real fields
    assert (project / "agents" / "prioritise_ticket.py").read_text() == before


async def test_a_new_code_agent_starts_from_a_stub_and_saving_creates_it(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl import llm as llm_client

    path = project / "agents" / "code_9.py"
    try:
        fresh = (
            await client.get("/dev/agents/code_9/source", params={"description": "Score it"})
        ).json()
        assert (
            fresh["exists"] is False and fresh["file"] == "agents/code_9.py" and not path.exists()
        )
        assert '@agent("code_9")' in fresh["file_text"] and "Score it" in fresh["file_text"]
        saved = await client.put("/dev/agents/code_9/source", json={"text": fresh["file_text"]})
        assert saved.status_code == 200 and path.is_file()
        assert (await client.get("/dev/agents/code_9/source")).json()["exists"] is True
    finally:
        path.unlink(missing_ok=True)

    seen: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        seen.update(kwargs)
        message = type("M", (), {"content": "Done."})()
        return type("R", (), {"choices": [type("C", (), {"message": message})()], "usage": None})()

    monkeypatch.setattr(llm_client, "acompletion", fake)
    draft = {
        "id": "code_9",
        "description": "Score it",
        "inputs": {"amount": "int"},
        "outputs": {"score": "int"},
    }
    await client.post(
        "/dev/agents/code_9/assist", json={"prompt": "score it", "model": "default", "agent": draft}
    )
    system = seen["messages"][0]["content"]
    assert "(unsaved draft)" in system and '"amount": "int"' in system


async def test_schedule_preview_validates_and_lists_the_next_fires(
    client: httpx.AsyncClient,
) -> None:
    ok = (
        await client.get("/dev/schedule/preview", params={"cron": "0 9 * * *", "count": 3})
    ).json()
    assert ok["ok"] is True and len(ok["next"]) == 3
    for bad in ("every hour", "61 * * * *"):
        out = (await client.get("/dev/schedule/preview", params={"cron": bad})).json()
        assert out["ok"] is False and out["error"]
