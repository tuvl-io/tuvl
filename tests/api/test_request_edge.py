"""What a caller sees at the edge: who is asked first, how big a body may be, and what an error says."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tuvl.api.runs import router
from tuvl.api.triggers import mount_triggers
from tuvl.contract.schema import WorkflowDocument
from tuvl.core.auth.token import mint_token
from tuvl.core.body_limit import BodyLimitMiddleware
from tuvl.core.config import settings
from tuvl.runtime.code import AGENT_REGISTRY
from tuvl.runtime.host import RuntimeHost, set_host

pytestmark = pytest.mark.asyncio


def _doc(name: str, trigger: dict[str, Any], **meta: Any) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": name, **meta},
            "spec": {
                "trigger": trigger,
                "agents": [
                    {
                        "id": name,
                        "description": name,
                        "engine": "code",
                        "code": {"run": name},
                        "inputs": dict(trigger.get("input", {})),
                        "routes": {"default": "END"},
                    }
                ],
                "outputs": {"default": {"body": "ok"}},
            },
        }
    )


WORKFLOWS = {
    "private_boom": _doc(
        "private_boom",
        {"http": {"path": "/t/private-boom", "method": "POST"}, "input": {"text": "str"}},
        required_scope="boom:run",
    ),
    "public_boom": _doc(
        "public_boom", {"http": {"path": "/t/public-boom", "method": "POST", "public": True}}
    ),
    "public_slow": _doc(
        "public_slow",
        {
            "http": {
                "path": "/t/public-slow",
                "method": "POST",
                "public": True,
                "sync_timeout": "100ms",
            }
        },
    ),
}


async def _boom(inp: Any, ctx: Any) -> None:
    raise RuntimeError("kaboom with a private detail")


async def _slow(inp: Any, ctx: Any) -> None:
    await asyncio.sleep(0.6)


def token(*scopes: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_token('caller', [], list(scopes), ttl_seconds=86400)}"}


@pytest.fixture
async def client(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    monkeypatch.setattr(settings, "tuvl_expose_errors", None)
    for name, fn in {"private_boom": _boom, "public_boom": _boom, "public_slow": _slow}.items():
        monkeypatch.setitem(AGENT_REGISTRY, name, fn)
    host = RuntimeHost(pg_engine, workflows=WORKFLOWS, role="api")
    set_host(host)
    app = FastAPI()
    app.include_router(router)
    mount_triggers(app, WORKFLOWS)
    app.add_middleware(BodyLimitMiddleware, limit=2048)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    await host.stop()
    set_host(None)


async def test_a_protected_trigger_asks_who_you_are_before_it_reads_the_body(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/t/private-boom", content=b"{this is not json")
    assert response.status_code == 401  # not 400: nobody has been let in to be told the body is bad


async def test_an_oversized_body_is_413_and_starts_no_run(client: httpx.AsyncClient) -> None:
    big = {"text": "x" * 5000}
    response = await client.post("/t/private-boom", json=big, headers=token("boom:run"))
    assert response.status_code == 413
    runs = await client.get("/api/runs", headers=token("iam:admin"))
    assert runs.json() == []


async def test_an_anonymous_caller_is_told_the_kind_of_error_and_not_its_text(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/t/public-boom")
    assert response.status_code == 500
    assert response.json()["error"] == {"error_type": "code_exception"}
    run = await client.get(
        f"/api/runs/{response.headers['x-tuvl-run-id']}", headers=token("iam:admin")
    )
    assert "kaboom" in run.json()["error"]["message"]  # the operator still has it


async def test_an_authenticated_caller_is_told_the_text_too(client: httpx.AsyncClient) -> None:
    response = await client.post("/t/private-boom", json={"text": "hi"}, headers=token("boom:run"))
    assert response.status_code == 500
    assert "kaboom" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("dev_mode", "expose", "shown"), [(True, None, True), (False, True, True), (True, False, False)]
)
async def test_the_text_for_an_anonymous_caller_follows_the_environment(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    dev_mode: bool,
    expose: bool | None,
    shown: bool,
) -> None:
    monkeypatch.setattr(settings, "tuvl_dev_mode", dev_mode)
    monkeypatch.setattr(settings, "tuvl_expose_errors", expose)
    response = await client.post("/t/public-boom")
    assert ("message" in response.json()["error"]) is shown


async def test_an_anonymous_runs_202_carries_no_urls_it_could_not_read(
    client: httpx.AsyncClient,
) -> None:
    slow = await client.post("/t/public-slow")
    assert slow.status_code == 202
    assert "status_url" not in slow.json() and "events_url" not in slow.json()
    assert slow.json()["run_id"]
