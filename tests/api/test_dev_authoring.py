"""Dev API: ideas, spec lifecycle and the audit ledger, read-only, as Insight reads them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tuvl.api.dev import router
from tuvl.core.config import use_project
from tuvl_insight.audit import ledger
from tuvl_insight.spec import ids, lifecycle
from tuvl_insight.spec.ideas import import_idea, plan_import


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # the spec commands record who acted from git's user.name: give them one that does not depend on the machine
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Ada\n\temail = ada@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    p = tmp_path / "proj"
    p.mkdir()
    brief = tmp_path / "brief"
    brief.mkdir()
    (brief / "overview.md").write_text("# Refund automation\n\nCustomers get refunds.\n")
    (brief / "big.md").write_text("x" * 9000)
    done = import_idea(
        p,
        plan_import([brief]),
        initials="SK",
        title="Refund automation",
        supersedes=None,
        imported_by="Ada <a@b.c>",
    )
    assert done.id == "IDEA-SK-0001"
    import_idea(
        p,
        plan_import([brief / "overview.md"]),
        initials="SK",
        title="Second",
        supersedes="IDEA-SK-0001",
        imported_by="Ada",
    )
    specs = p / "specs"
    specs.mkdir()
    (specs / "SPEC-SK-0001-01.md").write_text(
        lifecycle.approve(
            lifecycle.with_front(
                "# Intent\nx\n",
                {
                    "id": "SPEC-SK-0001-01",
                    "title": "Refunds",
                    "ideas": ["IDEA-SK-0001"],
                    "status": "draft",
                },
            ),
            by="Ada",
        )
    )
    (specs / "legacy.md").write_text("---\nname: legacy\n---\n# Intent\nold\n")
    lifecycle.sync_idea_links(p)
    return p


@pytest.fixture
async def client(project: Path) -> AsyncIterator[httpx.AsyncClient]:
    use_project(project)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev"
    ) as c:
        yield c


async def test_spec_lifecycle_lists_state_for_every_spec(
    client: httpx.AsyncClient, project: Path
) -> None:
    rows = {r["name"]: r for r in (await client.get("/dev/specs/lifecycle")).json()["specs"]}
    managed = rows["SPEC-SK-0001-01.md"]
    assert (
        managed["status"] == "approved"
        and managed["effective"] == "approved"
        and managed["approved_by"] == "Ada"
    )
    assert (
        managed["ideas"] == ["IDEA-SK-0001"]
        and managed["title"] == "Refunds"
        and managed["managed"] is True
    )
    assert rows["legacy.md"]["managed"] is False and rows["legacy.md"]["id"] == ""
    path = project / "specs" / "SPEC-SK-0001-01.md"
    path.write_text(path.read_text().replace("# Intent", "# Intent\nedited", 1))
    changed = {r["name"]: r for r in (await client.get("/dev/specs/lifecycle")).json()["specs"]}[
        "SPEC-SK-0001-01.md"
    ]
    assert changed["stale"] is True and changed["effective"] == "draft"


async def test_ideas_join_the_index_the_specs_and_supersession(client: httpx.AsyncClient) -> None:
    rows = {r["id"]: r for r in (await client.get("/dev/ideas")).json()["ideas"]}
    first, second = rows["IDEA-SK-0001"], rows["IDEA-SK-0002"]
    assert (
        first["title"] == "Refund automation"
        and first["files"] == 2
        and first["specs"] == ["SPEC-SK-0001-01"]
    )
    assert first["superseded_by"] == "IDEA-SK-0002" and second["supersedes"] == "IDEA-SK-0001"
    assert first["imported_by"] == "Ada <a@b.c>"


async def test_one_idea_shows_previews_and_its_specs(client: httpx.AsyncClient) -> None:
    detail = (await client.get("/dev/ideas/IDEA-SK-0001")).json()
    files = {f["path"]: f for f in detail["files_detail"]}
    assert files["source/overview.md"]["preview"].startswith("# Refund automation")
    assert (
        files["source/big.md"]["truncated"] is True
        and len(files["source/big.md"]["preview"]) == 6000
    )
    assert [s["id"] for s in detail["specs_detail"]] == ["SPEC-SK-0001-01"]


async def test_idea_ids_are_validated_and_unknown_ones_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/dev/ideas/..%2F..%2Fetc")).status_code in (400, 404)
    assert (await client.get("/dev/ideas/not-an-id")).status_code == 400
    assert (await client.get("/dev/ideas/IDEA-SK-0099")).status_code == 404


async def test_the_audit_ledger_is_filtered_and_its_chain_reported(
    client: httpx.AsyncClient, project: Path
) -> None:
    who = {"initials": "SK", "git": "Ada"}
    a = ledger.append(
        project, "idea.imported", command="spec import", entities=["IDEA-SK-0001"], who=who
    )
    ledger.append(
        project, "spec.approved", command="spec approve", entities=["SPEC-SK-0001-01"], who=who
    )
    ledger.append(
        project, "idea.imported", command="spec import", entities=["IDEA-SK-0002"], who=who
    )
    data = (await client.get("/dev/audit")).json()
    assert [e["kind"] for e in data["events"]] == [
        "idea.imported",
        "spec.approved",
        "idea.imported",
    ]
    assert (
        data["chain"] == {"ok": True, "events": 3, "problem": "", "where": ""}
        and "task.implemented" in data["kinds"]
    )
    by_idea = (await client.get("/dev/audit", params={"entity": "IDEA-SK-0001"})).json()["events"]
    assert [e["kind"] for e in by_idea] == [
        "idea.imported",
        "spec.approved",
    ]  # an idea also covers its specs
    assert len((await client.get("/dev/audit", params={"kind": "spec."})).json()["events"]) == 1
    assert len((await client.get("/dev/audit", params={"limit": 1})).json()["events"]) == 1
    newer = (await client.get("/dev/audit", params={"after": a["id"]})).json()["events"]
    assert len(newer) == 2


async def test_a_tampered_ledger_shows_in_the_chain(
    client: httpx.AsyncClient, project: Path
) -> None:
    who = {"initials": "SK"}
    for _ in range(3):
        ledger.append(project, "spec.approved", command="x", who=who)
    path = ledger.files(project)[0]
    path.write_text(path.read_text().replace('"result":"ok"', '"result":"failed"', 1))
    chain = (await client.get("/dev/audit")).json()["chain"]
    assert chain["ok"] is False and chain["where"].endswith(":2")


async def test_a_bad_cursor_is_refused_and_everything_is_read_only(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/dev/audit", params={"after": "nonsense"})).status_code == 422
    for path in ("/dev/audit", "/dev/ideas/IDEA-SK-0001", "/dev/specs/lifecycle"):
        assert (await client.post(path)).status_code == 405
        assert (await client.delete(path)).status_code == 405


async def test_an_empty_project_answers_empty(tmp_path: Path) -> None:
    use_project(tmp_path)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev"
    ) as c:
        assert (await c.get("/dev/ideas")).json()["ideas"] == []
        assert (await c.get("/dev/specs/lifecycle")).json() == {"specs": []}
        audit = (await c.get("/dev/audit")).json()
        assert audit["events"] == [] and audit["chain"]["ok"] is True
    assert ids.read_index(tmp_path) == []


async def test_a_spec_can_be_reopened_sent_back_and_approved_from_insight(
    client: httpx.AsyncClient, project: Path
) -> None:
    name = "SPEC-SK-0001-01.md"

    def state() -> str:
        return lifecycle.state_of((project / "specs" / name).read_text()).status

    assert (await client.post(f"/dev/specs/{name}/reopen")).status_code == 200
    assert state() == "draft"
    assert (await client.post(f"/dev/specs/{name}/reject", json={"reason": " "})).status_code == 422
    sent = await client.post(f"/dev/specs/{name}/reject", json={"reason": "too vague"})
    assert sent.status_code == 200 and state() == "draft"
    refused = await client.post(f"/dev/specs/{name}/approve")  # `# Intent\nx` has blocking findings
    assert refused.status_code == 422 and "blocking" in refused.json()["detail"]
    kinds = [e["kind"] for e in ledger.read_events(project)]
    assert "spec.reopened" in kinds and "spec.rejected" in kinds
    assert (await client.post("/dev/specs/..%2Fx.md/approve")).status_code in (404, 422)
    assert (await client.post("/dev/specs/legacy.txt/approve")).status_code == 422


async def test_a_task_shows_its_plan_entry_and_the_spec_section_it_comes_from(
    client: httpx.AsyncClient, project: Path
) -> None:
    from tuvl_insight.spec import tasks as plans
    from tuvl_insight.spec.parse import parse_spec

    name = "SPEC-SK-0001-01.md"
    spec_path = project / "specs" / name
    spec_path.write_text(spec_path.read_text() + "\n# Data\nAn Item has a name.\n")
    section = parse_spec(spec_path, root=project).section("data")
    assert section is not None
    (project / "specs" / "SPEC-SK-0001-01.tasks.yaml").write_text(
        "kind: TaskPlan\nversion: tuvl/v2\nmetadata:\n  name: refunds\n"
        f"  spec: specs/{name}\n  spec_hash: x\nspec:\n  tasks:\n"
        "  - id: TASK-SK-0001-01-001\n    title: Item model\n    type: model\n"
        f"    spec_ref: {{anchor: data, hash: {section.hash}}}\n    target: {{model: Item}}\n"
        "  - id: TASK-SK-0001-01-002\n    title: Save\n    type: agent\n"
        f"    spec_ref: {{anchor: data, hash: stale}}\n    depends_on: [TASK-SK-0001-01-001]\n"
    )
    assert plans.plans(project)
    one = (await client.get(f"/dev/specs/{name}/tasks/TASK-SK-0001-01-001")).json()
    assert one["title"] == "Item model" and one["target"] == {"model": "Item"}
    assert "An Item has a name." in one["section"]["text"] and one["section"]["changed"] is False
    two = (await client.get(f"/dev/specs/{name}/tasks/TASK-SK-0001-01-002")).json()
    assert two["depends_on"] == [{"id": "TASK-SK-0001-01-001", "title": "Item model"}]
    assert two["section"]["changed"] is True
    assert (await client.get(f"/dev/specs/{name}/tasks/TASK-NOPE")).status_code == 404
    assert (await client.get("/dev/specs/legacy.md/tasks/T1")).status_code == 404


async def test_a_task_is_built_with_the_agent_only_after_consent(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sys

    from tuvl.api.dev import jobs as dev

    monkeypatch.setattr(
        dev,
        "_cli_argv",
        lambda args: [sys.executable, "-c", "import sys; print(*sys.argv[1:])", *args],
    )
    url = "/dev/specs/SPEC-SK-0001-01.md/tasks/TASK-SK-0001-01-001/build"
    refused = await client.post(url, json={})
    assert refused.status_code == 409 and "coding agent" in refused.json()["detail"]
    started = await client.post(url, json={"consent": True})
    assert started.status_code == 200
    job = started.json()["job"]
    for _ in range(100):
        state = (await client.get(f"/dev/jobs/{job}")).json()
        if state["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert state["status"] == "ok" and state["exit_code"] == 0
    shown = "\n".join(state["lines"])
    assert "generate code --spec specs/SPEC-SK-0001-01.md --task TASK-SK-0001-01-001" in shown
    assert "--no-pr" in shown and "--consent" in shown
    assert (await client.get(f"/dev/jobs/{job}", params={"after": state["next"]})).json()[
        "lines"
    ] == []
    assert (await client.get("/dev/jobs/nope")).status_code == 404


async def test_a_blocked_task_says_why_and_is_not_built(
    client: httpx.AsyncClient, project: Path
) -> None:
    name = "SPEC-SK-0001-01.md"
    (project / "specs" / "SPEC-SK-0001-01.tasks.yaml").write_text(
        "kind: TaskPlan\nversion: tuvl/v2\nmetadata:\n  name: refunds\n"
        f"  spec: specs/{name}\n  spec_hash: x\nspec:\n  tasks:\n"
        "  - id: TASK-SK-0001-01-001\n    title: Item model\n    type: agent\n    target: {workflow: w, agent: a}\n"
        "  - id: TASK-SK-0001-01-002\n    title: Save\n    type: agent\n    target: {workflow: w, agent: a}\n"
        "    depends_on: [TASK-SK-0001-01-001, TASK-SK-0001-01-001]\n"
    )
    report = (await client.get("/dev/specs/status", params={"tests": "false"})).json()
    tasks = {t["id"]: t for t in report["plans"][0]["tasks"]}
    second = tasks["TASK-SK-0001-01-002"]
    assert second["blocked"] and second["waiting_on"] == ["TASK-SK-0001-01-001"]
    assert second["depends_on"] == ["TASK-SK-0001-01-001"] and second["live"] is None
    assert "Item model" in second["reasons"][-1]
    assert (
        second["reasons"][-1].count("TASK-SK-0001-01-001") == 1
    )  # a repeated dependency is said once
    refused = await client.post(
        f"/dev/specs/{name}/tasks/TASK-SK-0001-01-002/build", json={"consent": True}
    )
    assert refused.status_code == 409 and "waiting on" in refused.json()["detail"]


async def test_a_failed_run_is_reported_with_every_attempt(
    client: httpx.AsyncClient, project: Path
) -> None:
    from tuvl_insight.audit import live

    name = "SPEC-SK-0001-01.md"
    (project / "specs" / "SPEC-SK-0001-01.tasks.yaml").write_text(
        "kind: TaskPlan\nversion: tuvl/v2\nmetadata:\n  name: refunds\n"
        f"  spec: specs/{name}\n  spec_hash: x\nspec:\n  tasks:\n"
        "  - id: TASK-SK-0001-01-001\n    title: Save\n    type: manual\n"
    )
    live.set_failure(
        project,
        f"specs/{name}",
        "TASK-SK-0001-01-001",
        {
            "summary": "refunds.save is pending",
            "agent": "claude",
            "max_attempts": 2,
            "cost_usd": 0.1,
            "attempts": [
                {"attempt": 1, "gate": "validate", "problems": ["refunds.save is pending"]}
            ],
        },
    )
    report = (await client.get("/dev/specs/status", params={"tests": "false"})).json()
    task = report["plans"][0]["tasks"][0]
    assert (
        task["live"]["state"] == "failed" and task["failure"]["attempts"][0]["gate"] == "validate"
    )
    one = (await client.get(f"/dev/specs/{name}/tasks/TASK-SK-0001-01-001")).json()
    assert one["failure"]["summary"] == "refunds.save is pending"


async def test_only_an_agent_task_is_built_by_the_agent_and_a_failed_command_says_why(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sys

    from tuvl.api.dev import jobs as dev

    name = "SPEC-SK-0001-01.md"
    (project / "specs" / "SPEC-SK-0001-01.tasks.yaml").write_text(
        "kind: TaskPlan\nversion: tuvl/v2\nmetadata:\n  name: refunds\n"
        f"  spec: specs/{name}\n  spec_hash: x\nspec:\n  tasks:\n"
        "  - id: TASK-SK-0001-01-001\n    title: Item model\n    type: model\n"
    )
    url = f"/dev/specs/{name}/tasks/TASK-SK-0001-01-001/build"
    refused = await client.post(url, json={"consent": True})
    assert (
        refused.status_code == 409 and "not built by the coding agent" in refused.json()["detail"]
    )

    script = (
        'import sys; print(\'{"event": "boom", "level": "error", "logger": "tuvl.x", "code": 7}\'); '
        "sys.exit(3)"
    )
    monkeypatch.setattr(dev, "_cli_argv", lambda args: [sys.executable, "-c", script, *args])
    started = await client.post(url, json={"scaffold": True})
    assert started.status_code == 200
    for _ in range(100):
        state = (await client.get(f"/dev/jobs/{started.json()['job']}")).json()
        if state["status"] != "running":
            break
        await asyncio.sleep(0.05)
    shown = "\n".join(state["lines"])
    assert state["status"] == "failed" and state["exit_code"] == 3
    assert "--no-agent" in shown and "exit code 3" in shown
    assert "error · tuvl.x · boom (code=7)" in shown


async def test_a_command_survives_a_restart_of_the_dev_server(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sys

    from tuvl.api.dev import jobs as dev

    script = "import time; print('first'); time.sleep(0.6); print('second')"
    monkeypatch.setattr(dev, "_cli_argv", lambda args: [sys.executable, "-c", script])
    started = await client.post(
        "/dev/specs/SPEC-SK-0001-01.md/tasks/TASK-SK-0001-01-001/build", json={"scaffold": True}
    )
    job = started.json()["job"]
    await asyncio.sleep(0.2)
    dev._JOBS.clear()  # the dev server reloaded: its memory of the command is gone
    seen = (await client.get(f"/dev/jobs/{job}")).json()
    assert seen["status"] == "running" and "--no-agent" in "\n".join(seen["lines"])
    assert (
        await client.post(
            "/dev/specs/SPEC-SK-0001-01.md/tasks/TASK-SK-0001-01-001/build", json={"scaffold": True}
        )
    ).status_code == 409
    for _ in range(100):
        state = (await client.get(f"/dev/jobs/{job}")).json()
        if state["status"] != "running":
            break
        await asyncio.sleep(0.05)
    shown = "\n".join(state["lines"])
    assert state["status"] == "ok" and "first" in shown and "second" in shown


async def test_a_commands_log_lines_are_not_mixed_into_what_it_prints(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sys

    from tuvl.api.dev import jobs as dev

    script = (
        "print('2026-10-22T10:02:10Z INFO  tuvl.models Loaded model  model=Order'); print('scaffold: 2 files'); "
        "print('2026-10-22T10:02:11Z ERROR tuvl.codegen could not write  path=models/x.yaml'); raise SystemExit(1)"
    )
    monkeypatch.setattr(dev, "_cli_argv", lambda args: [sys.executable, "-c", script])
    job = (
        await client.post(
            "/dev/specs/SPEC-SK-0001-01.md/tasks/TASK-SK-0001-01-001/build", json={"scaffold": True}
        )
    ).json()["job"]
    for _ in range(100):
        state = (await client.get(f"/dev/jobs/{job}")).json()
        if state["status"] != "running":
            break
        await asyncio.sleep(0.05)
    shown = "\n".join(state["lines"])
    assert "scaffold: 2 files" in shown and "Loaded model" not in shown
    assert "exit code 1" in shown and "could not write" in shown


async def test_the_branch_report_is_served(client: httpx.AsyncClient, project: Path) -> None:
    report = (await client.get("/dev/specs/branches")).json()
    assert set(report) == {"base", "current", "dirty", "specs"}


async def test_developer_telemetry_is_set_registered_and_read_from_insight(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    valid = ["tdev_x"]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if self.path == "/v1/token/rotate":
                if self.headers.get("authorization") != f"Bearer {valid[0]}":
                    return self._send(401, {"detail": "unknown token"})
                valid[0] = "tdev_y"
                return self._send(200, {"token": "tdev_y"})
            if body.get("team_code") != "join_ok":
                return self._send(403, {"detail": "that join code is not valid"})
            self._send(
                201,
                {
                    "token": "tdev_x",
                    "developer": {"name": body["name"], "email": body["email"], "team": "payments"},
                },
            )

        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("authorization") != f"Bearer {valid[0]}":
                return self._send(401, {"detail": "unknown token"})
            self._send(
                200,
                {
                    "events": [
                        {
                            "id": "01",
                            "kind": "task.failed",
                            "developer": {"name": "Bob"},
                            "entities": ["T"],
                        }
                    ]
                },
            )

        def log_message(self, *a: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TUVL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TUVL_DEV_TELEMETRY_TOKEN", raising=False)
    try:
        shown = (await client.get("/dev/dev-telemetry")).json()
        assert shown["configured"] is False and shown["queued"] == 0
        refused = await client.put(
            "/dev/dev-telemetry", json={"enabled": True, "server": "http://example.com"}
        )
        assert refused.status_code == 422 and "https" in refused.json()["detail"]
        early = await client.post(
            "/dev/dev-telemetry/register",
            json={"name": "Ada", "email": "a@b.c", "team_code": "join_ok"},
        )
        assert early.status_code == 409  # no collector saved yet
        url = f"http://127.0.0.1:{server.server_port}"
        saved = await client.put(
            "/dev/dev-telemetry", json={"enabled": True, "server": url, "identity": "full"}
        )
        assert saved.status_code == 200 and saved.json()["identity"] == "full"
        assert (
            "token_env: TUVL_DEV_TELEMETRY_TOKEN"
            in (project / ".tuvl" / "dev-telemetry.yaml").read_text()
        )
        wrong = await client.post(
            "/dev/dev-telemetry/register",
            json={"name": "Ada", "email": "a@b.c", "team_code": "nope"},
        )
        assert wrong.status_code == 422 and "join code" in wrong.json()["detail"]
        ok = await client.post(
            "/dev/dev-telemetry/register",
            json={"name": "Ada", "email": "a@b.c", "team_code": "join_ok"},
        )
        assert (
            ok.status_code == 200
            and ok.json()["developer"]["team"] == "payments"
            and ok.json()["token_source"] == "registered"
        )
        checked = (await client.post("/dev/dev-telemetry/check")).json()
        assert checked["ok"] is True and checked["error"] == ""
        rotated = await client.post("/dev/dev-telemetry/rotate")
        assert rotated.status_code == 200 and rotated.json()["token_source"] == "registered"
        assert valid == ["tdev_y"]
        assert (await client.post("/dev/dev-telemetry/check")).json()[
            "ok"
        ] is True  # the new token works
        team = (await client.get("/dev/team-events", params={"entity": "T"})).json()
        assert team["events"][0]["developer"]["name"] == "Bob"
        gone = (await client.delete("/dev/dev-telemetry/token")).json()
        assert gone["token_set"] is False
        assert (await client.get("/dev/team-events")).status_code == 502
    finally:
        server.shutdown()


async def test_the_ideas_list_says_who_you_are_and_which_agent_you_use(
    client: httpx.AsyncClient, project: Path
) -> None:
    from tuvl_insight.coding_agents import Settings, write_settings

    assert (await client.get("/dev/ideas")).json()["me"] == {
        "initials": "",
        "agent": "none",
        "consent": False,
    }
    write_settings(project, Settings("SK", "claude-code", "2026-10-22T10:00:00Z"))
    assert (await client.get("/dev/ideas")).json()["me"] == {
        "initials": "SK",
        "agent": "claude-code",
        "consent": True,
    }


async def test_an_idea_is_uploaded_as_text_and_as_a_folder_and_numbered_by_the_cli(
    client: httpx.AsyncClient, project: Path
) -> None:
    text = await client.post(
        "/dev/ideas",
        data={"text": "# Loyalty points\n\nEarn a point per order.\n", "initials": "SK"},
    )
    assert text.status_code == 200, text.text
    made = text.json()
    assert made["id"] == "IDEA-SK-0003" and made["idea"]["title"] == "Loyalty points"
    assert (project / "idea/IDEA-SK-0003/source/idea.md").read_text().startswith("# Loyalty")

    folder = await client.post(
        "/dev/ideas",
        data={"title": "Returns portal", "supersedes": "IDEA-SK-0003"},
        files=[
            ("files", ("returns/overview.md", b"# Returns\n\nA portal.\n", "text/markdown")),
            ("files", ("returns/notes/flow.md", b"steps\n", "text/markdown")),
        ],
    )
    assert folder.status_code == 200, folder.text
    second = folder.json()
    assert second["id"] == "IDEA-SK-0004" and second["idea"]["title"] == "Returns portal"
    assert (project / "idea/IDEA-SK-0004/source/returns/notes/flow.md").is_file()
    assert second["idea"]["supersedes"] == "IDEA-SK-0003"
    assert any(
        e["kind"] == "idea.imported" and "IDEA-SK-0004" in e["entities"]
        for e in (await client.get("/dev/audit")).json()["events"]
    )


async def test_an_upload_that_cannot_be_trusted_or_cannot_be_imported_is_refused(
    client: httpx.AsyncClient, project: Path
) -> None:
    nothing = await client.post("/dev/ideas", data={"initials": "SK"})
    assert nothing.status_code == 422 and "at least one file" in nothing.json()["detail"]
    for name in (
        "../evil.md",
        "a/../../evil.md",
        "C:/x.md",
    ):  # a leading "/" is stripped, not refused
        sneaky = await client.post(
            "/dev/ideas", data={"initials": "SK"}, files=[("files", (name, b"x", "text/plain"))]
        )
        assert sneaky.status_code == 422, name
    assert not (project.parent / "evil.md").exists()
    bad = await client.post("/dev/ideas", data={"text": "x", "supersedes": "nonsense"})
    assert bad.status_code == 422
    unknown = await client.post(
        "/dev/ideas", data={"text": "x", "initials": "SK", "supersedes": "IDEA-SK-0099"}
    )
    assert unknown.status_code == 422 and "IDEA-SK-0099" in unknown.json()["detail"]
    assert set(ids.idea_ids(project)) == {"IDEA-SK-0001", "IDEA-SK-0002"}  # nothing was left behind


async def test_links_are_checked_before_anything_is_fetched(
    client: httpx.AsyncClient, project: Path
) -> None:
    for bad in ("notalink", "ftp://a.test/x", "file:///etc/passwd", "https://a.test/a b"):
        refused = await client.post("/dev/ideas", data={"urls": [bad], "initials": "SK"})
        assert refused.status_code == 422, bad
    many = await client.post(
        "/dev/ideas", data={"urls": [f"https://a.test/{n}" for n in range(11)]}
    )
    assert many.status_code == 413
    internal = await client.post(
        "/dev/ideas", data={"urls": ["http://127.0.0.1:9/x"], "initials": "SK"}
    )
    assert internal.status_code == 422 and "--allow-private" in internal.json()["detail"]
    assert set(ids.idea_ids(project)) == {"IDEA-SK-0001", "IDEA-SK-0002"}


async def test_a_link_on_the_network_of_the_developer_is_snapshotted_when_allowed(
    client: httpx.AsyncClient, project: Path
) -> None:
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<main><h1>Loyalty brief</h1><p>Earn one point per order.</p></main>")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    link = f"http://127.0.0.1:{server.server_address[1]}/brief"
    try:
        done = await client.post(
            "/dev/ideas", data={"urls": [link], "allow_private": "true", "initials": "SK"}
        )
    finally:
        server.shutdown()
    assert done.status_code == 200, done.text
    idea = done.json()["idea"]
    assert idea["title"] == "Loyalty brief"
    assert idea["files_detail"][0]["origin"]["url"] == link


async def test_a_spec_is_generated_with_the_agent_only_when_one_is_chosen_and_consent_given(
    client: httpx.AsyncClient, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sys

    from tuvl.api.dev import jobs as dev
    from tuvl_insight.coding_agents import Settings, write_settings

    monkeypatch.setattr(
        dev,
        "_cli_argv",
        lambda args: [sys.executable, "-c", "import sys; print(*sys.argv[1:])", *args],
    )

    async def finished(job: str) -> str:
        for _ in range(100):
            state = (await client.get(f"/dev/jobs/{job}")).json()
            if state["status"] != "running":
                return "\n".join(state["lines"])
            await asyncio.sleep(0.05)
        raise AssertionError("the job did not finish")

    url = "/dev/ideas/IDEA-SK-0001/spec"
    no_agent = await client.post(url, json={"consent": True})
    assert no_agent.status_code == 409 and "tuvl use agent" in no_agent.json()["detail"]

    write_settings(project, Settings("SK", "claude-code"))
    refused = await client.post(url, json={})
    assert refused.status_code == 409 and "coding agent" in refused.json()["detail"]
    assert (
        await client.post("/dev/ideas/IDEA-SK-0099/spec", json={"consent": True})
    ).status_code == 404
    assert (await client.post(url, json={"consent": True, "where": "elsewhere"})).status_code == 422

    here = await client.post(url, json={"consent": True, "title": "Refunds"})
    assert here.status_code == 200
    shown = await finished(here.json()["job"])
    assert "generate spec --idea IDEA-SK-0001 --yes --here" in shown
    assert "--consent" in shown and "--title Refunds" in shown and "--no-pr" not in shown

    on_branch = await client.post(url, json={"consent": True, "where": "branch"})
    shown = await finished(on_branch.json()["job"])
    assert "--no-pr" in shown and "--here" not in shown

    write_settings(project, Settings("SK", "claude-code", "2026-10-22T10:00:00Z"))
    agreed = await client.post(url, json={})  # consent was recorded the first time
    assert agreed.status_code == 200
    assert "--consent" not in await finished(agreed.json()["job"])
