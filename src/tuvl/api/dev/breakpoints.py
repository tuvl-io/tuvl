"""Developer controls: breakpoints, step mode and paused-run context."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, HTTPException, UploadFile
from pydantic import BaseModel

from tuvl.core.dev.project import project_dir

router = APIRouter()

# ── developer controls: breakpoints, step mode, context, probe ──────────────


@router.get("/breakpoints/{workflow}", summary="Agents a dev run pauses before")
async def get_breakpoints(workflow: str) -> list[str]:
    from tuvl.runtime import devtools

    return sorted(devtools.BREAKPOINTS.get(workflow, set()))


@router.put("/breakpoints/{workflow}", summary="Set the agents a dev run pauses before")
async def put_breakpoints(workflow: str, agents: list[str] = Body(...)) -> list[str]:
    from tuvl.runtime import devtools

    devtools.set_breakpoints(project_dir(), workflow, agents)
    return sorted(devtools.BREAKPOINTS.get(workflow, set()))


@router.post("/workflows/{name}/upload", summary="Store a file for a workflow's file input")
async def upload_for_run(name: str, file: UploadFile) -> dict[str, Any]:
    from tuvl.api.uploads import store_dev_file
    from tuvl.runtime.host import get_host

    doc = get_host().registry().get(name)
    if doc is None:
        raise HTTPException(404, f"no workflow {name!r}")
    return (await store_dev_file(doc, file)).model_dump()


class DevRunRequest(BaseModel):
    workflow: str
    input: dict[str, Any] = {}
    step: bool = False


@router.post("/runs/start", summary="Start a dev run, optionally in step mode")
async def start_dev_run(request: DevRunRequest) -> dict[str, str]:
    from tuvl.runtime import devtools
    from tuvl.runtime.host import InputError, WorkflowUnavailableError, get_host

    host = get_host()
    doc = host.registry().get(request.workflow)
    if doc is None:
        raise HTTPException(404, f"no workflow {request.workflow!r}")
    try:
        run_id = await host.enqueue(doc, request.input, principal=None, trigger="dev")
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except WorkflowUnavailableError as exc:
        raise HTTPException(409, str(exc)) from exc
    if request.step:
        devtools.STEP_RUNS.add(run_id)
    return {"run_id": str(run_id)}


async def _paused_run(run_id: uuid.UUID) -> Any:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tuvl.runtime.host import get_host
    from tuvl.runtime.models import SystemRun

    async with AsyncSession(get_host().engine) as s:
        run = await s.get(SystemRun, run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    return run


@router.get("/runs/{run_id}/context", summary="The context a paused run will resume with")
async def run_context(run_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tuvl.runtime import journal
    from tuvl.runtime.host import get_host

    run = await _paused_run(run_id)
    async with AsyncSession(get_host().engine) as s:
        checkpoint = await journal.latest_checkpoint(s, run_id)
    context = dict(checkpoint.context) if checkpoint else dict(run.input or {})
    doc = get_host().registry().get(run.workflow)
    agent = next((a for a in doc.spec.agents if a.id == run.current_agent), None) if doc else None
    view = {k: context.get(k) for k in agent.inputs} if agent is not None else {}
    return {
        "status": run.status,
        "next": run.current_agent,
        "context": context,
        "view": view,
        "editable": run.status == "paused",
    }


@router.put("/runs/{run_id}/context", summary="Change a paused run's context before it resumes")
async def edit_run_context(run_id: uuid.UUID, values: dict[str, Any] = Body(...)) -> dict[str, Any]:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncSession

    from tuvl.runtime import journal
    from tuvl.runtime.host import get_host
    from tuvl.runtime.runner import _CHECKPOINTS, _RUNS

    run = await _paused_run(run_id)
    if run.status != "paused":
        raise HTTPException(
            409, f"run is {run.status}; the context can be edited only while paused"
        )
    async with AsyncSession(get_host().engine) as s:
        checkpoint = await journal.latest_checkpoint(s, run_id)
        checkpoints: Any = _CHECKPOINTS
        runs: Any = _RUNS
        if checkpoint is not None:
            await s.execute(
                sa.update(checkpoints)
                .where(checkpoints.c.run_id == run_id, checkpoints.c.seq == checkpoint.seq)
                .values(context={**checkpoint.context, **values})
            )
        else:
            await s.execute(
                sa.update(runs)
                .where(runs.c.id == run_id)
                .values(input={**(run.input or {}), **values})
            )
        await journal.append_event(
            s, run_id, "checkpoint", agent_id=run.current_agent, payload={"edited": sorted(values)}
        )
        await s.commit()
    return await run_context(run_id)
