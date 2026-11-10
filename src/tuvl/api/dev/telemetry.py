"""Developer telemetry and team activity."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from tuvl.core.dev.project import project_dir

router = APIRouter()


class DevTelemetryBody(BaseModel):
    enabled: bool = False
    server: str = ""
    identity: str = "initials"
    commands: bool = True
    token_env: str = "TUVL_DEV_TELEMETRY_TOKEN"


class DevRegisterBody(BaseModel):
    name: str
    email: str
    team_code: str


@router.get("/dev-telemetry", summary="tuvl show dev-telemetry --json")
async def get_dev_telemetry() -> dict[str, Any]:
    from tuvl_insight import devtelemetry

    return devtelemetry.status(project_dir())


@router.put("/dev-telemetry", summary="tuvl set dev-telemetry")
async def put_dev_telemetry(body: DevTelemetryBody) -> dict[str, Any]:
    from tuvl_insight import devtelemetry

    try:
        devtelemetry.save(project_dir(), devtelemetry.Config(**body.model_dump()))
    except devtelemetry.TelemetryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return devtelemetry.status(project_dir())


@router.post("/dev-telemetry/register", summary="tuvl register developer")
async def register_developer(body: DevRegisterBody) -> dict[str, Any]:
    from tuvl_insight import devtelemetry

    config = devtelemetry.load(project_dir())
    if config is None or not config.server:
        raise HTTPException(status_code=409, detail="Save the collector's address first.")
    try:
        await asyncio.to_thread(
            devtelemetry.register,
            config.server,
            name=body.name,
            email=body.email,
            team_code=body.team_code,
        )
    except devtelemetry.TelemetryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return devtelemetry.status(project_dir())


@router.post("/dev-telemetry/rotate", summary="tuvl set dev-telemetry --rotate-token")
async def rotate_developer_token() -> dict[str, Any]:
    from tuvl_insight import devtelemetry

    try:
        await asyncio.to_thread(devtelemetry.rotate, project_dir())
    except devtelemetry.TelemetryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return devtelemetry.status(project_dir())


@router.delete("/dev-telemetry/token", summary="tuvl set dev-telemetry --forget")
async def forget_developer_token() -> dict[str, Any]:
    from tuvl_insight import devtelemetry

    config = devtelemetry.load(project_dir())
    if config is not None and config.server:
        devtelemetry.forget(config.server)
    return devtelemetry.status(project_dir())


@router.post("/dev-telemetry/check", summary="tuvl check dev-telemetry [--flush]")
async def check_dev_telemetry(flush: bool = False) -> dict[str, Any]:
    from tuvl_insight import devtelemetry

    project = project_dir()
    error = await asyncio.to_thread(devtelemetry.ping, project)
    sent = 0
    if not error and flush:
        result = await asyncio.to_thread(devtelemetry.flush, project, timeout=30.0)
        sent, error = result["sent"], result["error"]
    return {**devtelemetry.status(project), "ok": not error, "error": error, "sent": sent}


@router.get("/team-events", summary="tuvl list team-activity --json")
async def team_events(
    actor: str | None = None,
    entity: str | None = None,
    kind: str | None = None,
    before: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    from tuvl.core.project_info import load_project
    from tuvl_insight import devtelemetry

    record = load_project(project_dir()) or {}
    try:
        events = await asyncio.to_thread(
            devtelemetry.team_events,
            project_dir(),
            project=str(record.get("id", "")),
            actor=actor,
            entity=entity,
            kind=kind,
            before=before,
            limit=limit,
        )
    except devtelemetry.TelemetryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"events": events}
