"""Developer controls (P4.5): breakpoints, step mode, context edit at a pause, probe."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.dev_app import router as dev_router
from tuvl.api.runs import router as runs_router
from tuvl.contract.schema import WorkflowDocument
from tuvl.core.config import settings
from tuvl.runtime import devtools
from tuvl.runtime.code import AGENT_REGISTRY
from tuvl.runtime.host import RuntimeHost, set_host


async def _double(inp: Any, ctx: Any) -> dict[str, Any]:
    return {"twice": inp.n * 2}


async def _plus(inp: Any, ctx: Any) -> dict[str, Any]:
    return {"total": inp.twice + 1}


FLOW = WorkflowDocument.model_validate(
    {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "calc"},
        "spec": {
            "trigger": {"input": {"n": "int"}},
            "agents": [
                {
                    "id": "double",
                    "description": "Double",
                    "engine": "code",
                    "code": {"run": "double"},
                    "inputs": {"n": "int"},
                    "outputs": {"twice": "int"},
                    "routes": {"default": "plus", "error": "END"},
                },
                {
                    "id": "plus",
                    "description": "Add one",
                    "engine": "code",
                    "code": {"run": "plus"},
                    "inputs": {"twice": "int"},
                    "outputs": {"total": "int"},
                    "routes": {"default": "END", "error": "END"},
                },
            ],
            "outputs": {"default": {"body": {"total": "{{ total }}"}}},
        },
    }
)


@pytest.fixture
async def setup(
    pg_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, RuntimeHost]]:
    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))
    monkeypatch.setitem(AGENT_REGISTRY, "double", _double)
    monkeypatch.setitem(AGENT_REGISTRY, "plus", _plus)
    devtools.BREAKPOINTS.clear()
    host = RuntimeHost(pg_engine, workflows={"calc": FLOW}, role="api")
    set_host(host)
    app = FastAPI()
    app.include_router(dev_router)
    app.include_router(runs_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev"
    ) as client:
        yield client, host
    devtools.BREAKPOINTS.clear()
    await host.stop()
    set_host(None)


async def drain(host: RuntimeHost) -> None:
    while await host.worker.claim_some():
        await host.worker.drain()


async def test_breakpoint_pause_edit_and_resume(
    setup: tuple[httpx.AsyncClient, RuntimeHost], tmp_path: Path
) -> None:
    client, host = setup
    assert (await client.put("/dev/breakpoints/calc", json=["plus"])).json() == ["plus"]
    assert (tmp_path / ".tuvl" / "breakpoints.json").is_file()
    run_id = (
        await client.post("/dev/runs/start", json={"workflow": "calc", "input": {"n": 5}})
    ).json()["run_id"]
    await drain(host)
    paused = (await client.get(f"/api/runs/{run_id}")).json()
    assert paused["status"] == "paused" and paused["current_agent"] == "plus"
    context = (await client.get(f"/dev/runs/{run_id}/context")).json()
    assert (
        context["next"] == "plus"
        and context["view"] == {"twice": 10}
        and context["editable"] is True
    )
    edited = (await client.put(f"/dev/runs/{run_id}/context", json={"twice": 40})).json()
    assert edited["view"] == {"twice": 40}
    await client.post(f"/api/runs/{run_id}/resume")
    await drain(host)
    done = (await client.get(f"/api/runs/{run_id}")).json()
    assert done["status"] == "completed" and done["output"]["body"] == {
        "total": 41
    }  # the edit took effect
    assert (await client.put(f"/dev/runs/{run_id}/context", json={"twice": 1})).status_code == 409
    events = (await client.get(f"/api/runs/{run_id}/export")).json()["events"]
    assert {"type": "paused", "reason": "breakpoint"} in [
        {"type": e["type"], "reason": e["payload"].get("reason")} for e in events
    ]


async def test_step_mode_pauses_before_every_agent(
    setup: tuple[httpx.AsyncClient, RuntimeHost],
) -> None:
    client, host = setup
    run_id = (
        await client.post(
            "/dev/runs/start", json={"workflow": "calc", "input": {"n": 1}, "step": True}
        )
    ).json()["run_id"]
    stops = []
    for _ in range(3):
        await drain(host)
        run = (await client.get(f"/api/runs/{run_id}")).json()
        if run["status"] != "paused":
            break
        stops.append(run["current_agent"])
        await client.post(f"/api/runs/{run_id}/resume")
    assert stops == ["double", "plus"] and run["status"] == "completed"


async def test_probe_runs_one_agent(setup: tuple[httpx.AsyncClient, RuntimeHost]) -> None:
    client, _ = setup
    result = (
        await client.post(
            "/dev/agents/probe", json={"workflow": "calc", "agent": "plus", "inputs": {"twice": 7}}
        )
    ).json()
    assert (
        result["status"] == "completed"
        and result["signal"] == "default"
        and result["outputs"] == {"total": 8}
    )
    assert (
        await client.post("/dev/agents/probe", json={"workflow": "calc", "agent": "nope"})
    ).status_code == 404
    assert (
        await client.post(
            "/dev/agents/probe",
            json={"workflow": "calc", "agent": "plus", "inputs": {"twice": "x"}},
        )
    ).status_code == 422


async def test_no_pauses_outside_dev_mode(
    setup: tuple[httpx.AsyncClient, RuntimeHost], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, host = setup
    await client.put("/dev/breakpoints/calc", json=["plus"])
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    run_id = (
        await client.post(
            "/dev/runs/start", json={"workflow": "calc", "input": {"n": 2}, "step": True}
        )
    ).json()["run_id"]
    await drain(host)
    import uuid

    assert (
        await host.get(uuid.UUID(run_id))
    ).status == "completed"  # anonymous reads need dev mode


HUMAN = WorkflowDocument.model_validate(
    {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "ask"},
        "spec": {
            "trigger": {"input": {"n": "int"}},
            "agents": [
                {
                    "id": "review",
                    "description": "Approve",
                    "engine": "human",
                    "inputs": {"n": "int"},
                    "outputs": {"ok": "bool"},
                    "human": {"title": "ok?", "route_on": "ok"},
                    "routes": {"true": "END", "false": "END", "error": "END"},
                }
            ],
        },
    }
)


async def test_a_breakpoint_fires_once_even_when_the_agent_resumes(
    setup: tuple[httpx.AsyncClient, RuntimeHost],
) -> None:
    client, host = setup
    host.runner.workflows["ask"] = HUMAN  # type: ignore[index]
    await client.put("/dev/breakpoints/ask", json=["review"])
    run_id = (
        await client.post("/dev/runs/start", json={"workflow": "ask", "input": {"n": 1}})
    ).json()["run_id"]
    await drain(host)
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "paused"
    await client.post(f"/api/runs/{run_id}/resume")
    await drain(host)
    waiting = (await client.get(f"/api/runs/{run_id}")).json()
    assert waiting["status"] == "waiting_human"
    from tests.api.test_runs_api import token

    decided = await client.post(
        f"/api/approvals/{waiting['approval']['approval_id']}",
        json={"values": {"ok": True}},
        headers=token("root", ["iam:admin"]),
    )
    assert decided.status_code == 200, decided.text
    await drain(host)
    assert (await client.get(f"/api/runs/{run_id}")).json()[
        "status"
    ] == "completed"  # not paused again


async def test_schedules_report_next_fires_last_run_and_why_one_will_not_fire(
    setup: tuple[httpx.AsyncClient, RuntimeHost], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl.contract import registry

    client, host = setup
    scheduled = WorkflowDocument.model_validate(
        {
            **FLOW.model_dump(mode="json", by_alias=True, exclude_none=True),
            "spec": {
                **FLOW.model_dump(mode="json", by_alias=True, exclude_none=True)["spec"],
                "trigger": {
                    "input": {"n": "int"},
                    "schedule": {"cron": "0 6 * * *", "input": {"n": 2}},
                },
            },
        }
    )
    monkeypatch.setattr(registry, "WORKFLOWS", {"calc": scheduled})
    await host.enqueue(
        scheduled, {"n": 2}, principal=None, trigger="schedule:2026-07-25T06:00:00+00:00"
    )

    out = (await client.get("/dev/schedules")).json()
    assert out["scheduler"]["enabled"] is True
    [row] = out["workflows"]
    assert row["workflow"] == "calc" and row["cron"] == "0 6 * * *" and row["enabled"] is True
    assert row["firing"] is True and len(row["next"]) == 3 and row["warnings"] == []
    assert row["last_run"]["status"] == "queued"

    monkeypatch.setattr(settings, "tuvl_scheduler_enabled", False)
    [off] = (await client.get("/dev/schedules")).json()["workflows"]
    assert off["firing"] is False and off["next"] == []


async def test_a_file_can_be_stored_for_the_run_form(
    setup: tuple[httpx.AsyncClient, RuntimeHost], tmp_path: Path
) -> None:
    from tuvl.storage import STORE_REGISTRY, close_all, register_store_from_dict

    client, host = setup
    STORE_REGISTRY.clear()
    register_store_from_dict(
        {
            "kind": "Storage",
            "metadata": {"name": "inbox"},
            "spec": {"provider": "local", "root": str(tmp_path / "blobs")},
        }
    )
    try:
        files = {"file": ("a.txt", b"hello", "text/plain")}
        assert (await client.post("/dev/workflows/calc/upload", files=files)).status_code == 422
        host.registry()["calc"].spec.trigger.http = (
            type(host.registry()["calc"].spec.trigger)
            .model_validate(
                {"http": {"path": "/x", "upload": {"store": "inbox"}}, "input": {"n": "int"}}
            )
            .http
        )
        ref = (await client.post("/dev/workflows/calc/upload", files=files)).json()
        assert ref["name"] == "a.txt" and ref["size"] == 5 and ref["store"] == "inbox"
        assert (await client.post("/dev/workflows/nope/upload", files=files)).status_code == 404
    finally:
        host.registry()["calc"].spec.trigger.http = None
        STORE_REGISTRY.clear()
        await close_all()
