"""The 2.0 REST surface (P1.7b): triggers, runs, SSE, control and approvals —
real routers, real runtime, Postgres."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tuvl.api.runs import router
from tuvl.api.triggers import mount_triggers
from tuvl.contract.schema import WorkflowDocument
from tuvl.core.auth.token import mint_token
from tuvl.core.config import settings
from tuvl.runtime.code import AGENT_REGISTRY
from tuvl.runtime.host import RuntimeHost, set_host


def _doc(name: str, spec: dict[str, Any], **meta: Any) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {"kind": "Workflow", "version": "tuvl/v2", "metadata": {"name": name, **meta}, "spec": spec}
    )


def _code(agent_id: str, routes: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "id": agent_id,
        "description": agent_id,
        "engine": "code",
        "code": {"run": agent_id},
        "routes": routes,
        **extra,
    }


WORKFLOWS = {
    "greet": _doc(
        "greet",
        {
            "trigger": {"http": {"path": "/t/greet", "method": "POST"}, "input": {"name": "str"}},
            "agents": [
                _code(
                    "greet",
                    {"default": "END", "error": "END.failed"},
                    inputs={"name": "str"},
                    outputs={"text": "str"},
                )
            ],
            "outputs": {
                "default": {"status": 201, "body": {"text": "{{ text }}"}},
                "failed": {"status": 500},
            },
        },
        required_scope="greet:run",
        description="Greet someone",
    ),
    "echo": _doc(
        "echo",
        {
            "trigger": {
                "http": {"path": "/t/echo/{word}", "method": "GET", "public": True},
                "input": {"word": "str", "times": "int?"},
            },
            "agents": [
                _code(
                    "echo",
                    {"default": "END"},
                    inputs={"word": "str", "times": "int?"},
                    outputs={"said": "str"},
                )
            ],
            "outputs": {"default": {"body": "{{ said }}"}},
        },
    ),
    "slow": _doc(
        "slow",
        {
            "trigger": {
                "http": {
                    "path": "/t/slow",
                    "method": "POST",
                    "public": True,
                    "sync_timeout": "100ms",
                }
            },
            "agents": [_code("nap", {"default": "END"})],
            "outputs": {"default": {"body": "rested"}},
        },
    ),
    "streamer": _doc(
        "streamer",
        {
            "trigger": {"http": {"path": "/t/streamer", "method": "POST", "public": True}},
            "agents": [_code("streamer", {"default": "END"}, outputs={"text": "str"})],
            "outputs": {"default": {"body": "{{ text }}"}},
        },
    ),
    "broken": _doc(
        "broken",
        {
            "trigger": {"http": {"path": "/t/broken", "method": "POST", "public": True}},
            "agents": [_code("crash", {"default": "END"})],
        },
    ),
    "refund": _doc(
        "refund",
        {
            "trigger": {
                "http": {"path": "/t/refund", "method": "POST"},
                "input": {"amount": "int"},
            },
            "agents": [
                {
                    "id": "approval",
                    "description": "approve",
                    "engine": "human",
                    "inputs": {"amount": "int"},
                    "outputs": {"approved": "bool"},
                    "human": {
                        "title": "Refund {{ amount }}",
                        "show": ["amount"],
                        "required_group": "managers",
                        "route_on": "approved",
                    },
                    "routes": {"true": "END", "false": "END.rejected", "error": "END"},
                },
            ],
            "outputs": {
                "default": {"body": "refunded {{ amount }}"},
                "rejected": {"status": 409, "body": "no"},
            },
        },
    ),
}


async def _greet(inp: Any, ctx: Any) -> dict[str, Any]:
    return {"text": f"hello {inp.name}"}


async def _echo(inp: Any, ctx: Any) -> dict[str, Any]:
    return {"said": " ".join([inp.word] * (inp.times or 1))}


async def _nap(inp: Any, ctx: Any) -> None:
    await asyncio.sleep(0.5)


#: What the ``streamer`` agent does, in order: ("write", text), ("reset",) or ("sleep", seconds).
SCRIPT: list[tuple[Any, ...]] = []


async def _streamer(inp: Any, ctx: Any) -> dict[str, Any]:
    stream = ctx._engine_ctx.stream
    for step in SCRIPT:
        if step[0] == "write":
            await stream.write(step[1])
        elif step[0] == "reset":
            await stream.reset()
        else:
            await asyncio.sleep(step[1])
    return {"text": "Hello"}


async def _crash(inp: Any, ctx: Any) -> None:
    raise RuntimeError("kaboom")


# minted at import: must outlive a full run of the suite, which takes ~10 minutes on a loaded runner
_TTL = 24 * 3600


def token(user: str, scopes: list[str] = (), groups: list[str] = ()) -> dict[str, str]:  # type: ignore[assignment]
    return {
        "Authorization": f"Bearer {mint_token(user, list(groups), list(scopes), ttl_seconds=_TTL)}"
    }


ALICE = token("alice", ["greet:run", "runs:read"])
MANAGER = token("mo", ["approvals:decide", "runs:read"], ["managers"])
ADMIN = token("root", ["iam:admin"])
OPERATOR = token("ops", ["runs:control"])


@pytest.fixture
async def host(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[RuntimeHost]:
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    for name, fn in {
        "greet": _greet,
        "echo": _echo,
        "nap": _nap,
        "crash": _crash,
        "streamer": _streamer,
    }.items():
        monkeypatch.setitem(AGENT_REGISTRY, name, fn)
    runtime = RuntimeHost(pg_engine, workflows=WORKFLOWS, role="api")
    set_host(runtime)
    yield runtime
    await runtime.stop()
    set_host(None)


@pytest.fixture
async def client(host: RuntimeHost) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(router)
    mount_triggers(app, WORKFLOWS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def drain(host: RuntimeHost) -> None:
    while await host.worker.claim_some():
        await host.worker.drain()


# ── triggers ────────────────────────────────────────────────────────────────


async def test_trigger_auth_input_and_sync_response(client: httpx.AsyncClient) -> None:
    assert (await client.post("/t/greet", json={"name": "ada"})).status_code == 401
    assert (await client.post("/t/greet", json={"name": "ada"}, headers=MANAGER)).status_code == 403
    bad = await client.post("/t/greet", json={"name": 7}, headers=ALICE)
    assert bad.status_code == 422 and bad.json()["detail"][0]["loc"] == ["name"]

    ok = await client.post("/t/greet", json={"name": "ada"}, headers=ALICE)
    assert ok.status_code == 201 and ok.json() == {"text": "hello ada"}
    run = await client.get(f"/api/runs/{ok.headers['x-tuvl-run-id']}", headers=ALICE)
    assert (
        run.status_code == 200
        and run.json()["status"] == "completed"
        and run.json()["end"] == "default"
    )


async def test_public_get_trigger_with_path_and_query(client: httpx.AsyncClient) -> None:
    resp = await client.get("/t/echo/hey?times=3")
    assert resp.status_code == 200 and resp.json() == "hey hey hey"


async def test_slow_runs_answer_202_then_finish(
    client: httpx.AsyncClient, host: RuntimeHost
) -> None:
    resp = await client.post("/t/slow")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running" and body["status_url"] == f"/api/runs/{body['run_id']}"
    for _ in range(40):
        await asyncio.sleep(0.05)
        if (await host.get(uuid.UUID(body["run_id"]))).status == "completed":
            break
    async with asyncio.timeout(5):
        await host.worker.drain()
    final = await host.get(uuid.UUID(body["run_id"]))
    assert final.status == "completed" and final.output == {"status": 200, "body": "rested"}


async def test_unhandled_errors_are_500_with_the_run_error(client: httpx.AsyncClient) -> None:
    resp = await client.post("/t/broken")
    assert resp.status_code == 500
    assert (
        resp.json()["error"]["error_type"] == "code_exception"
        and "kaboom" in resp.json()["error"]["message"]
    )


async def test_generic_start_without_waiting(client: httpx.AsyncClient, host: RuntimeHost) -> None:
    assert (await client.post("/api/workflows/nope/runs", json={"input": {}})).status_code == 404
    resp = await client.post(
        "/api/workflows/greet/runs?wait=false", json={"input": {"name": "bo"}}, headers=ALICE
    )
    assert resp.status_code == 202 and resp.json()["status"] == "queued"
    await drain(host)
    done = await client.get(resp.json()["status_url"], headers=ALICE)
    assert done.json()["output"] == {"status": 201, "body": {"text": "hello bo"}}


# ── reading runs and events ─────────────────────────────────────────────────


async def test_run_visibility(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/t/greet", json={"name": "x"}, headers=ALICE)).headers[
        "x-tuvl-run-id"
    ]
    assert (await client.get(f"/api/runs/{run_id}")).status_code == 401
    assert (
        await client.get(f"/api/runs/{run_id}", headers=token("alice"))
    ).status_code == 200  # whoever started a run may follow it, no runs:read needed
    assert (
        await client.get(f"/api/runs/{run_id}/events?follow=false", headers=token("alice"))
    ).status_code == 200
    mine = (await client.get("/api/runs", headers=token("alice"))).json()
    assert run_id in {r["run_id"] for r in mine}
    assert run_id not in {
        r["run_id"] for r in (await client.get("/api/runs", headers=MANAGER)).json()
    }
    assert (
        await client.get(f"/api/runs/{run_id}", headers=MANAGER)
    ).status_code == 404  # not theirs
    assert (await client.get(f"/api/runs/{run_id}", headers=ADMIN)).status_code == 200


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.strip().split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads(data[0]))
    return events


async def test_sse_replay_and_resume(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/t/greet", json={"name": "x"}, headers=ALICE)).headers[
        "x-tuvl-run-id"
    ]
    replay = await client.get(f"/api/runs/{run_id}/events", headers=ALICE)
    assert replay.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(replay.text)
    assert [e["type"] for e in events] == [
        "run_queued",
        "run_started",
        "agent_started",
        "agent_completed",
        "run_completed",
    ]
    assert events[2]["engine"] == "code" and events[0]["seq"] < events[-1]["seq"]
    resumed = _parse_sse(
        (
            await client.get(
                f"/api/runs/{run_id}/events",
                headers={**ALICE, "Last-Event-ID": str(events[2]["seq"])},
            )
        ).text
    )
    assert [e["type"] for e in resumed] == ["agent_completed", "run_completed"]


async def test_sse_follows_a_live_run(client: httpx.AsyncClient, host: RuntimeHost) -> None:
    run_id = (await client.post("/api/workflows/slow/runs?wait=false", json={})).json()["run_id"]

    async def work() -> None:
        await asyncio.sleep(0.2)
        await drain(host)

    worker = asyncio.create_task(work())
    async with asyncio.timeout(10):
        live = await client.get(f"/api/runs/{run_id}/events", headers=ADMIN)
    await worker
    assert [e["type"] for e in _parse_sse(live.text)][-1] == "run_completed"


async def _follow_and_time_the_end(client: httpx.AsyncClient, host: RuntimeHost) -> float:
    """Seconds between the run finishing and a stream that was already open ending."""
    run_id = (
        await client.post(
            "/api/workflows/greet/runs?wait=false", json={"input": {"name": "x"}}, headers=ALICE
        )
    ).json()["run_id"]
    done_at = 0.0

    async def work() -> None:
        nonlocal done_at
        await asyncio.sleep(0.2)
        await drain(host)
        done_at = time.monotonic()

    worker = asyncio.create_task(work())
    async with asyncio.timeout(10):
        live = await client.get(f"/api/runs/{run_id}/events", headers=ALICE)
    ended_at = time.monotonic()
    await worker
    assert [e["type"] for e in _parse_sse(live.text)][-1] == "run_completed"
    return ended_at - done_at


async def test_an_event_committed_in_process_wakes_the_stream_at_once(
    client: httpx.AsyncClient, host: RuntimeHost
) -> None:
    from tuvl.api import runs

    assert runs._SSE_POLL_SECONDS >= 1.0
    assert await _follow_and_time_the_end(client, host) < 0.4


async def test_events_from_another_process_arrive_on_the_fallback_poll(
    client: httpx.AsyncClient, host: RuntimeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl.runtime import wakeup

    monkeypatch.setattr(wakeup, "notify", lambda run_id: None)  # as if another worker wrote them
    assert await _follow_and_time_the_end(client, host) < 1.5


def _blocks(text: str) -> list[tuple[str, dict[str, Any], bool]]:
    """(event name, data, has an id line) of each server-sent event."""
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        data = [line[6:] for line in lines if line.startswith("data: ")]
        name = next((line[7:] for line in lines if line.startswith("event: ")), "")
        if data:
            out.append((name, json.loads(data[0]), any(line.startswith("id: ") for line in lines)))
    return out


async def _stream_a_run(
    client: httpx.AsyncClient, host: RuntimeHost, script: list[Any], drafts: bool = True
) -> str:
    SCRIPT[:] = script
    run_id = (await client.post("/api/workflows/streamer/runs?wait=false", json={})).json()[
        "run_id"
    ]

    async def work() -> None:
        await asyncio.sleep(0.2)
        await drain(host)

    worker = asyncio.create_task(work())
    async with asyncio.timeout(15):
        live = await client.get(
            f"/api/runs/{run_id}/events", params={"drafts": str(drafts).lower()}, headers=ADMIN
        )
    await worker
    assert live.status_code == 200
    return live.text


async def test_drafts_stream_alongside_the_journal(
    client: httpx.AsyncClient, host: RuntimeHost
) -> None:
    text = await _stream_a_run(
        client, host, [("write", "Hel"), ("sleep", 0.7), ("write", "lo"), ("sleep", 0.7)]
    )
    blocks = _blocks(text)
    drafts = [(n, d) for n, d, _ in blocks if n.startswith("token_")]
    assert [(n, d["delta"], d["replay"], d["agent_id"]) for n, d in drafts] == [
        ("token_delta", "Hel", False, "streamer"),
        ("token_delta", "lo", False, "streamer"),
    ]
    assert not any(
        has_id for n, _, has_id in blocks if n.startswith("token_")
    )  # Last-Event-ID is the journal's
    assert all(has_id for n, _, has_id in blocks if not n.startswith("token_"))
    journal_types = [d["type"] for n, d, _ in blocks if not n.startswith("token_")]
    assert journal_types[-1] == "run_completed" and "agent_completed" in journal_types
    first_delta = next(i for i, (n, _, _) in enumerate(blocks) if n == "token_delta")
    completed = next(i for i, (n, _, _) in enumerate(blocks) if n == "agent_completed")
    assert first_delta < completed


async def test_the_default_stream_is_exactly_the_journal(
    client: httpx.AsyncClient, host: RuntimeHost
) -> None:
    text = await _stream_a_run(
        client,
        host,
        [("write", "Hel"), ("sleep", 0.7), ("write", "lo"), ("sleep", 0.7)],
        drafts=False,
    )
    blocks = _blocks(text)
    assert blocks and not any(n.startswith("token_") for n, _, _ in blocks)
    assert all(has_id and "type" in d and "seq" in d for _, d, has_id in blocks)
    assert blocks[-1][1]["type"] == "run_completed"


async def test_a_reset_tells_the_client_to_drop_the_draft(
    client: httpx.AsyncClient, host: RuntimeHost
) -> None:
    text = await _stream_a_run(
        client,
        host,
        [
            ("write", "bad"),
            ("sleep", 0.7),
            ("reset",),
            ("sleep", 0.7),
            ("write", "good"),
            ("sleep", 0.7),
        ],
    )
    names = [(n, d.get("delta")) for n, d, _ in _blocks(text) if n.startswith("token_")]
    assert names == [("token_delta", "bad"), ("token_reset", None), ("token_delta", "good")]


async def test_a_client_that_joins_mid_agent_gets_the_draft_so_far(
    client: httpx.AsyncClient, host: RuntimeHost
) -> None:
    SCRIPT[:] = [("write", "Hel"), ("sleep", 1.0), ("write", "lo"), ("sleep", 0.8)]
    run_id = (await client.post("/api/workflows/streamer/runs?wait=false", json={})).json()[
        "run_id"
    ]
    worker = asyncio.create_task(drain(host))
    await asyncio.sleep(0.6)  # "Hel" is written and the agent is still running
    async with asyncio.timeout(15):
        joined = await client.get(f"/api/runs/{run_id}/events?drafts=true", headers=ADMIN)
    await worker
    drafts = [(d["delta"], d["replay"]) for n, d, _ in _blocks(joined.text) if n == "token_delta"]
    assert drafts == [("Hel", True), ("lo", False)]


async def test_the_last_piece_written_just_before_the_agent_returns_still_arrives(
    client: httpx.AsyncClient, host: RuntimeHost
) -> None:
    # the run ends within milliseconds of this write; the stream's next poll must still deliver it
    text = await _stream_a_run(client, host, [("write", "Hel"), ("sleep", 0.7), ("write", "lo")])
    pieces = [d["delta"] for n, d, _ in _blocks(text) if n == "token_delta"]
    assert "".join(pieces) == "Hello", pieces


async def test_an_ended_runs_drafts_are_not_replayed_and_the_sweep_removes_them(
    client: httpx.AsyncClient, host: RuntimeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncSession

    from tuvl.runtime import deltas
    from tuvl.runtime.models import SystemRunDelta

    text = await _stream_a_run(client, host, [("write", "x"), ("sleep", 0.7)])
    run_id = _blocks(text)[0][1]["run_id"]
    count = sa.select(sa.func.count()).select_from(SystemRunDelta.__table__)  # type: ignore[attr-defined]
    async with AsyncSession(host.engine) as s:
        assert (
            await s.execute(count)
        ).scalar_one() > 0  # kept for a moment: a client may still be reading
    again = await client.get(f"/api/runs/{run_id}/events?drafts=true", headers=ADMIN)
    assert "token_" not in again.text  # an ended run replays only its journal

    monkeypatch.setattr(deltas, "GRACE", timedelta(0))
    monkeypatch.setattr(deltas, "_last_sweep", float("-inf"))
    async with AsyncSession(host.engine) as s:
        await deltas.sweep(s)
        await s.commit()
        assert (await s.execute(count)).scalar_one() == 0


# ── approvals and control ───────────────────────────────────────────────────


async def test_human_approval_over_http(client: httpx.AsyncClient, host: RuntimeHost) -> None:
    resp = await client.post("/t/refund", json={"amount": 30}, headers=token("cust", ["runs:read"]))
    assert resp.status_code == 202
    waiting = resp.json()
    assert waiting["status"] == "waiting_human"
    approval = waiting["approval"]
    assert approval["title"] == "Refund 30" and approval["show"] == {"amount": 30}

    assert (await client.get("/api/approvals", headers=ALICE)).status_code == 403
    listed = (await client.get("/api/approvals", headers=MANAGER)).json()
    assert [a["approval_id"] for a in listed] == [approval["approval_id"]]

    bad = await client.post(
        approval["decide_url"], json={"values": {"approved": "perhaps"}}, headers=MANAGER
    )
    assert bad.status_code == 422
    decided = await client.post(
        approval["decide_url"], json={"values": {"approved": True}}, headers=MANAGER
    )
    assert decided.status_code == 200 and decided.json()["status"] == "decided"
    again = await client.post(
        f"/api/runs/{waiting['run_id']}/human/approval", json={"approved": False}, headers=MANAGER
    )
    assert again.status_code == 409

    await drain(host)
    done = (await client.get(waiting["status_url"], headers=ADMIN)).json()
    assert done["status"] == "completed" and done["output"] == {
        "status": 200,
        "body": "refunded 30",
    }


async def test_abort_a_waiting_run_and_control_rules(
    client: httpx.AsyncClient, host: RuntimeHost
) -> None:
    waiting = (await client.post("/t/refund", json={"amount": 5}, headers=token("cust"))).json()
    url = f"/api/runs/{waiting['run_id']}"
    assert (await client.post(f"{url}/abort", headers=ALICE)).status_code == 403
    assert (await client.post(f"{url}/steer", headers=OPERATOR, json={})).status_code == 422
    assert (await client.post(f"{url}/resume", headers=OPERATOR)).status_code == 409
    assert (await client.post(f"{url}/abort", headers=OPERATOR)).status_code == 200
    await drain(host)
    final = (await client.get(url, headers=ADMIN)).json()
    assert final["status"] == "aborted" and final["error"]["error_type"] == "aborted"
    assert (await client.get("/api/approvals", headers=MANAGER)).json() == []  # cancelled
    assert (await client.post(f"{url}/abort", headers=OPERATOR)).status_code == 409


async def test_export_is_a_replayable_journal(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/t/greet", json={"name": "ada"}, headers=ALICE)).headers[
        "x-tuvl-run-id"
    ]
    assert (
        await client.get(f"/api/runs/{run_id}/export", headers=MANAGER)
    ).status_code == 404  # not theirs
    exported = (await client.get(f"/api/runs/{run_id}/export", headers=ALICE)).json()
    assert exported["format"] == "tuvl.journal/v1" and exported["run"]["workflow"] == "greet"
    assert [c["agent_id"] for c in exported["checkpoints"]] == ["greet"]
    assert exported["checkpoints"][0]["context"]["text"] == "hello ada"


async def test_list_runs_is_scoped_to_the_caller(client: httpx.AsyncClient) -> None:
    mine = (await client.post("/t/greet", json={"name": "ada"}, headers=ALICE)).headers[
        "x-tuvl-run-id"
    ]
    (
        await client.post(
            "/t/greet", json={"name": "bob"}, headers=token("bob", ["greet:run", "runs:read"])
        )
    )
    listed = (await client.get("/api/runs", headers=ALICE)).json()
    assert [r["run_id"] for r in listed] == [mine]
    everyone = (
        await client.get("/api/runs", params={"workflow": "greet", "limit": 5}, headers=ADMIN)
    ).json()
    assert len(everyone) == 2 and everyone[0]["created_at"] >= everyone[1]["created_at"]
    assert (await client.get("/api/runs", params={"status": "failed"}, headers=ADMIN)).json() == []
    assert (
        await client.get("/api/runs", headers=token("nobody"))
    ).json() == []  # signed in, no runs
    assert (await client.get("/api/runs")).status_code == 401


def test_a_disabled_api_trigger_is_not_mounted() -> None:
    def flow(name: str, enabled: bool) -> Any:
        return _doc(
            name,
            {
                "trigger": {"http": {"path": f"/t/{name}", "public": True, "enabled": enabled}},
                "agents": [_code("nap", {"default": "END"})],
            },
        )

    app = FastAPI()
    assert mount_triggers(app, {"on": flow("on", True), "off": flow("off", False)}) == 1
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/t/on" in paths and "/t/off" not in paths
