"""The dev-mode artifact routes (`/dev/artifacts`, `/dev/ping`) live in the engine and edit the artifact registry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY
from tuvl.core.config import use_project
from tuvl.core.dev.artifacts import router


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    ARTIFACT_REGISTRY.clear()
    use_project(tmp_path)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev"
    ) as c:
        yield c
    ARTIFACT_REGISTRY.clear()


async def test_ping(client: httpx.AsyncClient) -> None:
    assert (await client.get("/dev/ping")).json() == {"status": "ok", "dev": True}


async def test_a_prose_artifact_is_created_listed_read_and_deleted(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    made = await client.post(
        "/dev/artifacts",
        json={"name": "tone", "type": "prompt", "content": "Be brief.", "description": "style"},
    )
    assert made.status_code == 200 and made.json()["version"] == 1
    assert (tmp_path / "artifacts" / "tone.md").read_text().endswith("Be brief.")

    listed = (await client.get("/dev/artifacts")).json()["artifacts"]
    assert [(a["name"], a["type"], a["source"]) for a in listed] == [("tone", "prompt", "file")]

    one = (await client.get("/dev/artifacts/tone")).json()
    assert one["content"] == "Be brief." and one["version"] == 1

    gone = await client.delete("/dev/artifacts/tone")
    assert gone.status_code == 200 and not (tmp_path / "artifacts" / "tone.md").exists()
    assert (await client.get("/dev/artifacts/tone")).status_code == 404


@pytest.mark.parametrize(
    "body, message",
    [
        ({"name": "../x", "type": "prompt", "content": "c"}, "Invalid artifact name"),
        ({"name": "x", "type": "nope", "content": "c"}, "type must be one of"),
        ({"name": "x", "type": "prompt"}, "content (string) is required"),
    ],
)
async def test_bad_artifacts_are_refused(
    client: httpx.AsyncClient, body: dict, message: str
) -> None:
    result = await client.post("/dev/artifacts", json=body)
    assert result.status_code == 400 and message in result.text
