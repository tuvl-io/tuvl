"""Probe an agent live and check a draft against pinned fixtures."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tuvl.api.dev.validation import DraftRequest, validate, validate_draft
from tuvl.core.dev.project import project_dir

router = APIRouter()


class ProbeRequest(BaseModel):
    workflow: str
    agent: str
    inputs: dict[str, Any] = {}


@router.post("/agents/probe", summary="Run one agent with given inputs (live)")
async def probe_agent(request: ProbeRequest) -> dict[str, Any]:
    """Runs a one-agent copy of the workflow (every route ends the run), so the
    agent executes exactly as in a real run; the probe is journaled like any
    dev run and appears on the Runs page."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from tuvl.contract.schema import WorkflowDocument
    from tuvl.runtime import journal
    from tuvl.runtime.host import InputError, get_host

    host = get_host()
    doc = host.registry().get(request.workflow)
    agent = next((a for a in doc.spec.agents if a.id == request.agent), None) if doc else None
    if doc is None or agent is None:
        raise HTTPException(404, f"no agent {request.agent!r} in {request.workflow!r}")
    name = f"probe__{request.workflow}__{request.agent}"[:64].rstrip("_")
    single = agent.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    single["routes"] = dict.fromkeys(single.get("routes", {}), "END")
    single.pop("compensate", None)
    probe = WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {
                "name": name,
                "description": f"Probe of {request.workflow}.{request.agent}",
            },
            "spec": {
                "models": list(doc.spec.models),
                "trigger": {"input": dict(agent.inputs)},
                "agents": [single],
            },
        }
    )
    workflows = host.registry()
    if isinstance(workflows, dict):
        workflows[name] = probe
    try:
        run_id = await host.enqueue(probe, request.inputs, principal=None, trigger="probe")
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc
    run = await host.run_sync(probe, run_id)
    async with AsyncSession(host.engine) as s:
        events = await journal.events(s, run_id)
        checkpoint = await journal.latest_checkpoint(s, run_id)
    completed = next((e for e in events if e.type == "agent_completed"), None)
    return {
        "run_id": str(run_id),
        "status": run.status,
        "signal": completed.payload.get("signal") if completed else None,
        "outputs": {k: (checkpoint.context or {}).get(k) for k in agent.outputs}
        if checkpoint
        else {},
        "error": run.error,
        "tokens": run.tokens_used,
    }


# ── change engine: check a draft against pinned fixtures ────────────────────


class ChangeCheckRequest(BaseModel):
    name: str
    text: str
    agent: str
    live: bool = False


@router.post(
    "/change/check",
    summary="Replay a workflow's pinned fixtures against a draft where one agent changed",
)
async def change_check(request: ChangeCheckRequest) -> dict[str, Any]:
    """Every other agent is served from the recording; the changed agent
    executes (offline unless *live*). Each fixture must reach the same end
    and output. The validator's profile is returned before and after."""
    import yaml as pyyaml
    from pydantic import ValidationError

    from tuvl.contract.registry import WORKFLOWS
    from tuvl.contract.schema import WorkflowDocument
    from tuvl_insight.cli.commands.replay import replay_test
    from tuvl_insight.testing import run_test

    project = project_dir()
    try:
        draft = WorkflowDocument.model_validate(pyyaml.safe_load(request.text))
    except (pyyaml.YAMLError, ValidationError) as exc:
        raise HTTPException(422, f"the draft is not a valid workflow: {exc}") from exc
    name = draft.metadata.name
    fixtures = sorted((project / "tests" / "fixtures" / name).glob("*.journal.json"))
    workflows = {**WORKFLOWS, name: draft}
    replays = []
    for fixture in fixtures:
        result = await run_test(
            replay_test(fixture, project),
            workflows,
            base=project,
            live=request.live,
            live_agents=frozenset({request.agent}),
        )
        replays.append(result.to_json())
    before = (await validate())["profiles"].get(name, {})
    after = (await validate_draft(DraftRequest(name=request.name, text=request.text)))[
        "profiles"
    ].get(name, {})
    return {
        "fixtures": len(fixtures),
        "passed": all(r["passed"] for r in replays) if replays else None,
        "replays": replays,
        "before": before.get("summary"),
        "after": after.get("summary"),
    }
