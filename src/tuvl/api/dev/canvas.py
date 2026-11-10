"""Canvas layout, run pinning and offline replay."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from tuvl.core.dev.project import project_dir

router = APIRouter()

# ── canvas layout ────────────────────────────────────────────────────────────


def _layout_path(workflow: str) -> Path:
    if not workflow.replace("_", "").isalnum():
        raise HTTPException(400, "invalid workflow name")
    return project_dir() / ".tuvl" / "layout" / f"{workflow}.json"


@router.get("/layout/{workflow}", summary="Node positions of a workflow's canvas")
async def get_layout(workflow: str) -> dict[str, Any]:
    path = _layout_path(workflow)
    return json.loads(path.read_text()) if path.is_file() else {}


@router.put("/layout/{workflow}", summary="Save node positions (.tuvl/layout/<workflow>.json)")
async def put_layout(workflow: str, layout: dict[str, Any] = Body(...)) -> dict[str, str]:
    path = _layout_path(workflow)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n")
    return {"saved": str(path.relative_to(project_dir()))}


# ── runs: pin and replay ─────────────────────────────────────────────────────


class PinRequest(BaseModel):
    name: str | None = None


@router.post(
    "/runs/{run_id}/pin", summary="Pin a run as tests/fixtures/<workflow>/<name>.journal.json"
)
async def pin_run(run_id: uuid.UUID, request: PinRequest) -> dict[str, str]:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tuvl.runtime import journal
    from tuvl.runtime.host import get_host

    try:
        async with AsyncSession(get_host().engine) as s:
            document = await journal.export_run(s, run_id)
    except journal.JournalError as exc:
        raise HTTPException(404, str(exc)) from exc
    name = request.name or str(run_id)
    if not name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "fixture names use letters, digits, - and _")
    target = (
        project_dir() / "tests" / "fixtures" / document["run"]["workflow"] / f"{name}.journal.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2) + "\n")
    return {"pinned": str(target.relative_to(project_dir()))}


class ReplayRequest(BaseModel):
    fixtures: list[str]


@router.post("/runs/replay", summary="tuvl replay runs --json: re-run pinned journals offline")
async def replay(request: ReplayRequest) -> dict[str, Any]:
    from tuvl.contract.registry import WORKFLOWS
    from tuvl_insight.cli.commands.replay import replay_test
    from tuvl_insight.testing import run_test

    project = project_dir()
    results = []
    for fixture in request.fixtures:
        path = (project / fixture).resolve()
        if not path.is_relative_to(project / "tests") or not path.is_file():
            raise HTTPException(400, f"{fixture} is not a fixture under tests/")
        results.append(await run_test(replay_test(path, project), dict(WORKFLOWS), base=project))
    return {"passed": all(r.passed for r in results), "replays": [r.to_json() for r in results]}
