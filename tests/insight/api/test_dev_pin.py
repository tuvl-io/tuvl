"""`/dev/runs/{id}/pin`: a run becomes a replayable fixture (Insight's dev API over the engine's run journal)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tests.api.test_runs_api import ALICE, WORKFLOWS
from tuvl.api.runs import router
from tuvl.api.triggers import mount_triggers
from tuvl.core.config import settings
from tuvl.runtime.host import RuntimeHost


async def test_dev_pin_writes_a_replayable_fixture(
    host: RuntimeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.insight.dev_app import router as dev_router

    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    app.include_router(dev_router)
    mount_triggers(app, WORKFLOWS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        run_id = (await c.post("/t/greet", json={"name": "ada"}, headers=ALICE)).headers[
            "x-tuvl-run-id"
        ]
        pinned = (await c.post(f"/dev/runs/{run_id}/pin", json={"name": "ada-hello"})).json()
        assert pinned == {"pinned": "tests/fixtures/greet/ada-hello.journal.json"}
        document = json.loads((tmp_path / pinned["pinned"]).read_text())
        assert document["run"]["workflow"] == "greet" and document["checkpoints"]
        assert (await c.post(f"/dev/runs/{run_id}/pin", json={"name": "../x"})).status_code == 400
        assert (await c.post(f"/dev/runs/{uuid.uuid4()}/pin", json={})).status_code == 404
