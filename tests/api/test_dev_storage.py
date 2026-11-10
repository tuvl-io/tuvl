"""Dev API: the Storage page's endpoints — list, check, browse — over a local store."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tests.dev_app import router
from tuvl.core.config import use_project


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    (tmp_path / "storage").mkdir()
    (tmp_path / "storage" / "files.yaml").write_text(
        "kind: Storage\nversion: v1\nmetadata: { name: files }\n"
        "spec: { provider: local, root: ./blobs, prefix: up/ }\n"
    )
    (tmp_path / "storage" / "cloud.yaml").write_text(
        "kind: Storage\nversion: v1\nmetadata: { name: cloud }\n"
        "spec: { provider: s3, bucket: b, access_key: '${NEEDED_KEY}', secret_key: '${NEEDED_SECRET:x}' }\n"
    )
    (tmp_path / "storage" / "bad.yaml").write_text(
        "kind: Storage\nversion: v1\nmetadata: { name: bad }\nspec: { provider: s3 }\n"
    )
    use_project(tmp_path)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev"
    ) as c:
        yield c


async def test_list_reports_stores_and_problems(client: httpx.AsyncClient) -> None:
    rows = {r["name"]: r for r in (await client.get("/dev/storage")).json()}
    assert rows["files"]["provider"] == "local" and rows["files"]["missing"] == []
    assert rows["cloud"]["missing"] == ["NEEDED_KEY"]
    assert "needs 'bucket'" in rows["bad"]["error"]


async def test_check_and_browse_roundtrip(client: httpx.AsyncClient, tmp_path: Path) -> None:
    checked = (await client.post("/dev/storage/files/check")).json()
    assert checked["ok"] is True
    assert [s["step"] for s in checked["steps"]] == ["list", "put", "head", "get", "delete"]
    (tmp_path / "blobs" / "up").mkdir(parents=True, exist_ok=True)
    (tmp_path / "blobs" / "up" / "a.txt").write_text("hi")
    listed = (await client.get("/dev/storage/files/objects", params={"prefix": ""})).json()
    assert [o["key"] for o in listed["items"]] == ["a.txt"]
    assert listed["items"][0]["size"] == 2


async def test_unknown_or_invalid_store_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/dev/storage/nope/check")).status_code == 404
    assert (await client.post("/dev/storage/bad/check")).status_code == 404
    assert (
        await client.get("/dev/storage/files/objects", params={"prefix": "../x"})
    ).status_code == 400


async def test_storage_files_are_editable_as_text(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    body = "kind: Storage\nversion: v1\nmetadata: { name: new }\nspec: { provider: local }\n"
    saved = await client.put(
        "/dev/text/storage/new.yaml", content=body, headers={"content-type": "text/plain"}
    )
    assert saved.status_code == 200
    assert (tmp_path / "storage" / "new.yaml").read_text() == body
    assert "new.yaml" in (await client.get("/dev/text/storage")).json()
