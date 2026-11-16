"""Runs, events and approvals (spec §15.1, §15.2, §16.2).

* ``POST /api/workflows/{name}/runs`` — start a run (trigger policy applies).
* ``GET /api/runs/{id}`` and ``GET /api/runs/{id}/events`` (SSE, replay from
  ``?from_seq=`` or ``Last-Event-ID``, then live) — the run's owner (whoever
  started it), or ``iam:admin``.
* ``POST /api/runs/{id}/{pause|resume|abort}`` — ``runs:control``.
* ``GET /api/approvals``, ``POST /api/approvals/{id}`` and the alias
  ``POST /api/runs/{id}/human/{agent_id}`` — ``approvals:decide`` plus the
  agent's group rules (:mod:`tuvl.runtime.human`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tuvl.api.auth import ADMIN, UNSET, current_user, has_scope, require, trigger_user
from tuvl.api.uploads import is_multipart, reject_file_values, start_upload_run
from tuvl.contract.schema import WorkflowDocument
from tuvl.core.auth.schemas import TokenUser
from tuvl.runtime import deltas, human, journal, queue, wakeup
from tuvl.runtime.host import InputError, RuntimeHost, WorkflowUnavailableError, get_host
from tuvl.runtime.models import (
    TERMINAL_STATUSES,
    SystemApproval,
    SystemRun,
    SystemWebhookDelivery,
)

router = APIRouter(prefix="/api", tags=["runs"])

RUNS_READ = "runs:read"
RUNS_CONTROL = "runs:control"
# Fallback poll: commits in this process wake the stream at once (runtime.wakeup); this
# interval covers events written by other worker processes.
_SSE_POLL_SECONDS = 1.0
_SSE_KEEPALIVE_SECONDS = 15.0
_RUN_TERMINAL_HTTP = {"failed": 500, "aborted": 409, "compensated": 500}


# ── tenant isolation (multi-tenant mode) ─────────────────────────────────────
# A run, its journal and its approvals belong to the tenant that started them. Whatever scope
# a caller holds — admin and runs:control included — it reaches only its own tenant's runs, and a
# run of another tenant is "not found", never "forbidden".


def _multi_tenant() -> bool:
    import tuvl.core.bootstrap as bootstrap

    return bootstrap.LOCKED_DEPLOYMENT_MODE == "multi_tenant"


async def _enforce_tenant(user: TokenUser | None, table: str, row_id: uuid.UUID) -> None:
    if not _multi_tenant() or user is None:
        return
    async with AsyncSession(get_host().engine) as s:
        owner = (
            await s.execute(
                sa.text(f"SELECT tenant_id FROM {table} WHERE id = :id"), {"id": row_id}
            )
        ).scalar_one_or_none()
    if owner is not None and owner != (user.tenant_id or ""):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{row_id} not found")


# ── views ────────────────────────────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def run_view(run: SystemRun, approval: SystemApproval | None = None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "run_id": str(run.id),
        "workflow": run.workflow,
        "status": run.status,
        "trigger": run.trigger,
        "current_agent": run.current_agent,
        "end": run.end_name,
        "output": run.output,
        "error": run.error,
        "tokens_used": run.tokens_used,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "status_url": f"/api/runs/{run.id}",
        "events_url": f"/api/runs/{run.id}/events",
    }
    if approval is not None:
        view["approval"] = approval_view(approval)
    return view


def approval_view(approval: SystemApproval) -> dict[str, Any]:
    return {
        "approval_id": str(approval.id),
        "run_id": str(approval.run_id),
        "agent_id": approval.agent_id,
        "kind": approval.kind,
        "status": approval.status,
        "required_group": approval.required_group,
        "expires_at": _iso(approval.expires_at),
        "created_at": _iso(approval.created_at),
        "decide_url": f"/api/approvals/{approval.id}",
        **{k: v for k, v in approval.payload.items() if k not in ("step", "loop_state")},
    }


async def _pending_approval(session: AsyncSession, run_id: uuid.UUID) -> SystemApproval | None:
    stmt = (
        sa.select(SystemApproval)
        .where(SystemApproval.run_id == run_id, SystemApproval.status == "pending")  # type: ignore[arg-type]
        .order_by(SystemApproval.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    found: SystemApproval | None = (await queue._execute(session, stmt)).scalars().first()
    return found


def _anonymous(run: SystemRun) -> bool:
    """A run no token started, outside dev mode: its caller can read nothing about it afterwards."""
    from tuvl.core.config import settings

    return run.principal is None and not settings.tuvl_dev_mode


def _caller_error(run: SystemRun) -> Any:
    """The error to put in a caller's answer. An anonymous caller learns the kind of error, never its text
    (a message can name tables, hosts and values); the text stays in the journal for the run's owner and
    for ``iam:admin``. ``TUVL_EXPOSE_ERRORS`` overrides."""
    from tuvl.core.config import settings

    expose = settings.tuvl_expose_errors
    if expose is None:
        expose = not _anonymous(run)
    if expose or not isinstance(run.error, dict):
        return run.error
    return {"error_type": run.error.get("error_type")}


async def respond(host: RuntimeHost, run: SystemRun) -> JSONResponse:
    """The HTTP answer for a run after the synchronous window (spec §14.3)."""
    headers = {"X-Tuvl-Run-Id": str(run.id)}
    if run.output is not None and run.status in ("completed", "compensated"):
        return JSONResponse(
            run.output.get("body"), status_code=int(run.output.get("status", 200)), headers=headers
        )
    if run.status in _RUN_TERMINAL_HTTP:
        return JSONResponse(
            {"run_id": str(run.id), "status": run.status, "error": _caller_error(run)},
            status_code=_RUN_TERMINAL_HTTP[run.status],
            headers=headers,
        )
    async with AsyncSession(host.engine) as s:
        approval = await _pending_approval(s, run.id)
    view = run_view(run, approval)
    if _anonymous(run):  # nobody could read what they point to
        view.pop("status_url", None)
        view.pop("events_url", None)
    return JSONResponse(view, status_code=status.HTTP_202_ACCEPTED, headers=headers)


# ── starting runs ────────────────────────────────────────────────────────────


def _workflow(host: RuntimeHost, name: str) -> WorkflowDocument:
    doc = host.registry().get(name)
    if doc is None or not doc.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"workflow '{name}' not found")
    return doc


async def start_run(
    request: Request,
    doc: WorkflowDocument,
    payload: dict[str, Any],
    *,
    trigger: str,
    wait: bool = True,
    uploaded: bool = False,
    user: Any = UNSET,
) -> JSONResponse:
    if not uploaded:
        reject_file_values(doc, payload)
    host = get_host()
    if user is UNSET:
        user = await trigger_user(request, doc)
    try:
        run_id = await host.enqueue(
            doc, payload, principal=user.user_id if user else None, trigger=trigger
        )
    except InputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.errors) from exc
    except WorkflowUnavailableError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"error": "workflow_unavailable", "reasons": exc.reasons}
        ) from exc
    run = await host.run_sync(doc, run_id) if wait else await host.get(run_id)
    return await respond(host, run)


_START_BODY = {
    "requestBody": {
        "content": {
            "application/json": {
                "schema": {"type": "object", "properties": {"input": {"type": "object"}}}
            },
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "description": "One form field per input; file parts for `file` inputs",
                }
            },
        }
    }
}


@router.post("/workflows/{name}/runs", summary="Start a workflow run", openapi_extra=_START_BODY)
async def create_run(
    name: str,
    request: Request,
    wait: bool = Query(default=True, description="Run inline up to trigger.http.sync_timeout."),
) -> JSONResponse:
    doc = _workflow(get_host(), name)
    user = await trigger_user(request, doc)  # before the body is read
    if is_multipart(request):
        return await start_upload_run(request, doc, trigger="api", wait=wait, user=user)
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "body must be a JSON object")
    payload = body.get("input", {})
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "'input' must be an object")
    return await start_run(request, doc, payload, trigger="api", wait=wait, user=user)


# ── reading runs ─────────────────────────────────────────────────────────────


async def _readable_run(request: Request, run_id: uuid.UUID, scope: str = RUNS_READ) -> SystemRun:
    from tuvl.core.config import settings

    user = await current_user(request)
    host = get_host()
    async with AsyncSession(host.engine, expire_on_commit=False) as s:
        run = await s.get(SystemRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")
    if user is None and settings.tuvl_dev_mode and run.principal is None:
        return run  # dev mode: anonymous runs are readable anonymously
    await _enforce_tenant(user, "tuvl_system_runs", run_id)
    _authorise_run(user, run, scope)
    return run


def _authorise_run(user: TokenUser | None, run: SystemRun, scope: str) -> None:
    """Control needs ``runs:control`` (any run). Reading is for the run's owner (whoever started it may
    always follow it), or a token holding ``runs:read`` (any run in its tenant), or ``iam:admin``."""
    if scope == RUNS_CONTROL:
        require(user, RUNS_CONTROL)
        return
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if ADMIN in user.scopes or RUNS_READ in user.scopes:
        return
    if run.principal != user.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run.id} not found")


@router.get("/runs", summary="Recent runs the caller may read")
async def list_runs(
    request: Request,
    workflow: str | None = None,
    run_status: str | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Newest first. A caller with ``runs:read`` (or ``iam:admin``) sees every run of its tenant; others see
    their own (anonymous dev-mode callers see anonymous runs), as for ``GET /runs/{id}``."""
    from tuvl.core.config import settings

    user = await current_user(request)
    query = sa.select(SystemRun).order_by(SystemRun.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    if user is None and settings.tuvl_dev_mode:
        query = query.where(SystemRun.principal.is_(None))  # type: ignore[union-attr]
    else:
        if user is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if ADMIN not in user.scopes and RUNS_READ not in user.scopes:
            query = query.where(SystemRun.principal == user.user_id)  # type: ignore[arg-type]
        if _multi_tenant():
            query = query.where(SystemRun.__table__.c.tenant_id == (user.tenant_id or ""))  # type: ignore[attr-defined]
    if workflow:
        query = query.where(SystemRun.workflow == workflow)  # type: ignore[arg-type]
    if run_status:
        query = query.where(SystemRun.status == run_status)  # type: ignore[arg-type]
    async with AsyncSession(get_host().engine) as s:
        rows = (await s.execute(query)).scalars().all()
    return [run_view(r) for r in rows]


@router.get("/runs/{run_id}", summary="Run status, end, output and cost")
async def get_run(run_id: uuid.UUID, request: Request) -> dict[str, Any]:
    run = await _readable_run(request, run_id)
    async with AsyncSession(get_host().engine) as s:
        approval = await _pending_approval(s, run.id) if run.status.startswith("waiting_") else None
    return run_view(run, approval)


def _sse(event: Any, engines: dict[str, str]) -> str:
    data = {
        "run_id": str(event.run_id),
        "seq": event.seq,
        "ts": _iso(event.created_at),
        "type": event.type,
        "agent_id": event.agent_id,
        "engine": engines.get(event.agent_id or ""),
        "tokens": event.tokens,
        "latency_ms": event.latency_ms,
        "payload": event.payload,
    }
    return f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(data, default=str)}\n\n"


def _sse_draft(run_id: uuid.UUID, agent_id: str, text: str, replay: bool) -> str:
    data = {"run_id": str(run_id), "agent_id": agent_id, "delta": text, "replay": replay}
    return f"event: token_delta\ndata: {json.dumps(data)}\n\n"


def _sse_reset(run_id: uuid.UUID, agent_id: str) -> str:
    data = {"run_id": str(run_id), "agent_id": agent_id}
    return f"event: token_reset\ndata: {json.dumps(data)}\n\n"


@router.get(
    "/runs/{run_id}/events",
    summary="Run events (SSE: replay, then live)",
    description=(
        "Journal events, each with an `id` (resume with `Last-Event-ID` or `from_seq`). With `drafts=true`, "
        "while an agent is writing its answer the stream also carries `token_delta` (`agent_id`, `delta`) "
        "and `token_reset` (`agent_id`: discard the draft) events. They have no `id`, are not journaled and "
        "are not replayed; a client that connects mid-agent first gets the draft so far as one "
        "`token_delta` with `replay: true`. Without `drafts=true` the stream is exactly the journal. "
        "The finished result is in the journal and the run output as usual."
    ),
)
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    from_seq: int = Query(default=0, ge=0),
    follow: bool = Query(default=True, description="Keep streaming until the run ends."),
    drafts: bool = Query(
        default=False,
        description="Also send the live draft of an agent's answer (`token_delta`, `token_reset`).",
    ),
) -> StreamingResponse:
    run = await _readable_run(request, run_id)
    last_event_id = request.headers.get("last-event-id")
    cursor = int(last_event_id) + 1 if last_event_id and last_event_id.isdigit() else from_seq
    host = get_host()
    doc = host.registry().get(run.workflow)
    engines: dict[str, str] = {a.id: str(a.engine) for a in doc.spec.agents} if doc else {}

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        last_sent = time.monotonic()
        draft_cursor: int | None = None
        with wakeup.listen(run_id) as woken:
            while True:
                # Cleared before querying: a commit that lands during the queries sets it again.
                woken.clear()
                async with AsyncSession(host.engine) as s:
                    batch = await journal.events(s, run_id, from_seq=cursor)
                    current = await s.get(SystemRun, run_id)
                    pieces: list[Any] = []
                    replay: tuple[str, str] | None = None
                    if not drafts:
                        pass
                    elif draft_cursor is None:
                        agent = (
                            await deltas.running_agent(s, run_id)
                            if current and current.status == "running"
                            else None
                        )
                        text, draft_cursor = await deltas.draft(s, run_id, agent or "")
                        if agent and text:
                            replay = (agent, text)
                    else:
                        pieces = await deltas.after(s, run_id, draft_cursor)
                if replay is not None:
                    yield _sse_draft(run_id, replay[0], replay[1], True)
                for row in pieces:
                    draft_cursor = int(row.id)
                    yield (
                        _sse_reset(run_id, row.agent_id)
                        if row.kind == "reset"
                        else _sse_draft(run_id, row.agent_id, row.text, False)
                    )
                for event in batch:
                    yield _sse(event, engines)
                    cursor = event.seq + 1
                finished = current is None or current.status in TERMINAL_STATUSES
                if not follow or (finished and not batch and not pieces):
                    return
                if await request.is_disconnected():
                    return
                now = time.monotonic()
                if batch or pieces or replay is not None:
                    last_sent = now
                elif now - last_sent >= _SSE_KEEPALIVE_SECONDS:
                    last_sent = now
                    yield ": keep-alive\n\n"
                if finished:
                    continue  # one more read to confirm nothing follows the last batch
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(woken.wait(), _SSE_POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/export", summary="The run's journal as a replayable fixture")
async def export_run(run_id: uuid.UUID, request: Request) -> dict[str, Any]:
    """The ``tuvl.journal/v1`` document ``tuvl pin run`` writes to
    ``tests/fixtures/`` for ``replay:`` (secure fields redacted)."""
    run = await _readable_run(request, run_id)
    async with AsyncSession(get_host().engine) as s:
        return await journal.export_run(s, run.id)


# ── webhook deliveries ───────────────────────────────────────────────────────


def _delivery_view(row: SystemWebhookDelivery) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "webhook": row.webhook,
        "type": row.type,
        "seq": row.seq,
        "agent": row.agent_id,
        "status": row.status,
        "attempts": row.attempts,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "created_at": row.created_at.isoformat(),
        "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
    }


@router.get("/runs/{run_id}/webhooks", summary="The webhook notifications of a run and their state")
async def run_webhooks(run_id: uuid.UUID, request: Request) -> list[dict[str, Any]]:
    await _readable_run(request, run_id)
    async with AsyncSession(get_host().engine) as s:
        rows = (
            (
                await s.execute(
                    sa.select(SystemWebhookDelivery)
                    .where(SystemWebhookDelivery.run_id == run_id)  # type: ignore[arg-type]
                    .order_by(SystemWebhookDelivery.created_at)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .all()
        )
    return [_delivery_view(r) for r in rows]


@router.post(
    "/runs/{run_id}/webhooks/{delivery_id}/redeliver", summary="Send a webhook notification again"
)
async def redeliver_webhook(
    run_id: uuid.UUID, delivery_id: uuid.UUID, request: Request
) -> dict[str, Any]:
    from tuvl.webhooks.dispatcher import redeliver

    await _readable_run(request, run_id, scope=RUNS_CONTROL)
    host = get_host()
    async with AsyncSession(host.engine) as s:
        row = await s.get(SystemWebhookDelivery, delivery_id)
    if row is None or row.run_id != run_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such delivery for this run")
    await redeliver(host.engine, delivery_id)
    return {"id": str(delivery_id), "status": "pending"}


# ── control ──────────────────────────────────────────────────────────────────


@router.post("/runs/{run_id}/{action}", summary="Pause, resume, abort or steer a run")
async def control_run(
    run_id: uuid.UUID,
    action: str,
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    if action not in ("pause", "resume", "abort", "steer"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown action '{action}'")
    run = await _readable_run(request, run_id, scope=RUNS_CONTROL)
    host = get_host()
    if run.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"run is already {run.status}")
    async with AsyncSession(host.engine) as s:
        if action == "resume":
            if run.status != "paused":
                raise HTTPException(status.HTTP_409_CONFLICT, f"run is {run.status}, not paused")
            from tuvl.runtime.loop import resume_paused

            await resume_paused(s, run_id)
            await queue.requeue(s, run_id)
        else:
            control: dict[str, Any] = {"action": action}
            if action == "steer":
                message = str(body.get("message") or "").strip()
                if not message:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_CONTENT, "steer needs a 'message'"
                    )
                control["message"] = message
            await queue._execute(
                s,
                sa.update(SystemRun)
                .where(SystemRun.id == run_id)  # type: ignore[arg-type]
                .values(control=control),
            )
            if action == "abort" and run.status in ("waiting_human", "waiting_approval", "paused"):
                await human.cancel_pending(s, run_id)
                await queue.requeue(s, run_id)  # a worker picks it up and honours the abort
        await s.commit()
    return run_view(await host.get(run_id))


# ── approvals ────────────────────────────────────────────────────────────────


def _reviewer(user: TokenUser) -> human.Reviewer:
    return human.Reviewer(user.user_id, frozenset(user.groups), frozenset(user.scopes))


@router.get("/approvals", tags=["approvals"], summary="Pending approvals the caller may decide")
async def list_approvals(
    request: Request, limit: int = Query(default=100, ge=1, le=500)
) -> list[dict[str, Any]]:
    user = require(await current_user(request), human.DECIDE_SCOPE)
    stmt = (
        sa.select(SystemApproval)
        .where(SystemApproval.status == "pending")  # type: ignore[arg-type]
        .order_by(SystemApproval.created_at)  # type: ignore[arg-type]
        .limit(limit)
    )
    if _multi_tenant():
        stmt = stmt.where(SystemApproval.__table__.c.tenant_id == (user.tenant_id or ""))  # type: ignore[attr-defined]
    async with AsyncSession(get_host().engine) as s:
        rows = (await queue._execute(s, stmt)).scalars().all()
    reviewer = _reviewer(user)
    visible = []
    for approval in rows:
        try:
            human.authorise(approval, reviewer)
        except human.ApprovalError:
            continue
        visible.append(approval_view(approval))
    return visible


async def _decide(request: Request, decide: Any) -> dict[str, Any]:
    user = await current_user(request)
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    host = get_host()
    async with AsyncSession(host.engine, expire_on_commit=False) as s:
        try:
            approval = await decide(s, _reviewer(user), host)
        except human.ApprovalError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        await s.commit()
    return approval_view(approval)


@router.post("/approvals/{approval_id}", tags=["approvals"], summary="Decide an approval")
async def decide_approval(
    approval_id: uuid.UUID, request: Request, body: dict[str, Any] = Body(default_factory=dict)
) -> dict[str, Any]:
    await _enforce_tenant(await current_user(request), "tuvl_system_approvals", approval_id)
    async with AsyncSession(get_host().engine) as s:
        found = await s.get(SystemApproval, approval_id)
    if found is not None and found.kind == "tool":
        from tuvl.runtime.loop import decide_tool

        return await _decide(
            request,
            lambda s, reviewer, host: decide_tool(
                s,
                approval_id,
                str(body.get("decision") or ""),
                reviewer,
                args=body.get("edited_args"),
                message=body.get("message"),
            ),
        )
    values = body.get("values", {})
    if not isinstance(values, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "'values' must be an object")
    return await _decide(
        request,
        lambda s, reviewer, host: human.decide_by_id(
            s,
            approval_id,
            values,
            reviewer,
            workflows=host.registry(),
            resolver=host.runner.resolver,
        ),
    )


@router.post(
    "/runs/{run_id}/human/{agent_id}", tags=["approvals"], summary="Submit a human agent's form"
)
async def submit_human(
    run_id: uuid.UUID,
    agent_id: str,
    request: Request,
    values: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    await _enforce_tenant(await current_user(request), "tuvl_system_runs", run_id)
    return await _decide(
        request,
        lambda s, reviewer, host: human.decide(
            s,
            run_id,
            agent_id,
            values,
            reviewer,
            workflows=host.registry(),
            resolver=host.runner.resolver,
        ),
    )


__all__ = ["router", "start_run", "has_scope"]
