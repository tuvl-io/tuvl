"""MCP export (spec §15.3): workflows with ``trigger.mcp.enabled`` as tools on
``/mcp`` (streamable HTTP, stateless).

* Tool name = workflow name; description = ``metadata.description``; input
  schema = ``trigger.input``. The output is an envelope that standard clients
  can validate on every non-error answer: ``{outcome: "completed", run_id,
  end, status, body}`` with ``body`` typed as one of the 2xx end bodies, or
  ``{outcome: "pending", run_id, state, current_agent, approval?}``.
* Annotations are derived from the workflow's strongest effect.
* Auth is the REST auth: a bearer token (JWT) with ``mcp:connect``, and each
  tool still needs its workflow's gates. ``tools/list`` shows only the
  workflows the caller may run.
* A call runs the workflow inline and sends a progress notification per
  agent. A run that waits on a person or approval, or outlives the call
  window, answers ``pending``; an error end (≥ 400), failure or abort is an
  ``isError`` result.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import uuid
from contextlib import AsyncExitStack
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from tuvl.api.auth import current_user, has_scope, run_denial
from tuvl.api.runs import _pending_approval, approval_view
from tuvl.contract.schema import WorkflowDocument
from tuvl.contract.surface import end_schema, input_schema, workflow_effect
from tuvl.core.auth.schemas import TokenUser
from tuvl.extras import require
from tuvl.runtime import journal, queue, wakeup
from tuvl.runtime.host import InputError, RuntimeHost, WorkflowUnavailableError, get_host
from tuvl.runtime.models import SystemRun

require("mcp")

from mcp import types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402

logger = structlog.get_logger(__name__)

MCP_CONNECT = "mcp:connect"
CALL_WINDOW_SECONDS = 120.0
_POLL_SECONDS = 1.0  # fallback poll: commits made in this process wake the call at once
_WAITING = ("waiting_human", "waiting_approval", "paused")

_USER: contextvars.ContextVar[TokenUser | None] = contextvars.ContextVar("mcp_user", default=None)


def exported(host: RuntimeHost) -> dict[str, WorkflowDocument]:
    return {
        name: doc
        for name, doc in host.registry().items()
        if doc.enabled and doc.spec.trigger.mcp is not None and doc.spec.trigger.mcp.enabled
    }


def output_schema(doc: WorkflowDocument) -> dict[str, Any]:
    """The result envelope; ``body`` is any of the workflow's 2xx end bodies."""
    bodies: list[dict[str, Any]] = []
    for end, spec in doc.spec.outputs.items():
        if spec.status < 400:
            body = end_schema(doc, end)
            bodies.append(body if body is not None else {"type": "null"})
    unique = [b for i, b in enumerate(bodies) if b not in bodies[:i]]
    return {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["completed", "pending"]},
            "run_id": {"type": "string"},
            "end": {"type": "string"},
            "status": {"type": "integer"},
            "body": unique[0] if len(unique) == 1 else {"anyOf": unique},
            "state": {"type": "string"},
            "current_agent": {"type": ["string", "null"]},
            "approval": {"type": "object"},
        },
        "required": ["outcome", "run_id"],
    }


def tool_for(doc: WorkflowDocument) -> types.Tool:
    effect = workflow_effect(doc)
    return types.Tool(
        name=doc.metadata.name,
        description=doc.metadata.description or doc.metadata.name,
        inputSchema=input_schema(doc),
        outputSchema=output_schema(doc),
        annotations=types.ToolAnnotations(
            readOnlyHint=effect == "read",
            destructiveHint=effect == "destructive",
            idempotentHint=False,
            openWorldHint=False,
        ),
    )


def _text(data: Any) -> list[types.ContentBlock]:
    return [types.TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _error(message: str | dict[str, Any], **detail: Any) -> types.CallToolResult:
    return types.CallToolResult(content=_text({"error": message, **detail}), isError=True)


async def _progress(
    server: Server[Any, Any], host: RuntimeHost, run_id: uuid.UUID, cursor: int
) -> int:
    """Report each newly started agent as a progress notification."""
    ctx = server.request_context
    token = ctx.meta.progressToken if ctx.meta else None
    async with AsyncSession(host.engine) as s:
        batch = await journal.events(s, run_id, from_seq=cursor)
    for event in batch:
        cursor = event.seq + 1
        if token is not None and event.type == "agent_started" and event.agent_id:
            await ctx.session.send_progress_notification(
                token,
                float(event.seq),
                message=f"{event.agent_id} started",
                related_request_id=str(ctx.request_id),  # stateless: answer on the call's stream
            )
    return cursor


async def _answer(host: RuntimeHost, run: SystemRun) -> types.CallToolResult:
    if run.status in ("completed", "compensated") and run.output is not None:
        status = int(run.output.get("status", 200))
        body = run.output.get("body")
        if status >= 400 or run.status == "compensated":
            return types.CallToolResult(
                content=_text(body), isError=True, _meta={"tuvl": {"run_id": str(run.id)}}
            )
        result = {
            "outcome": "completed",
            "run_id": str(run.id),
            "end": run.end_name,
            "status": status,
            "body": body,
        }
        return types.CallToolResult(content=_text(body), structuredContent=result)
    if run.status in ("failed", "aborted"):
        return _error(run.error or run.status, run_id=str(run.id), status=run.status)
    async with AsyncSession(host.engine) as s:
        approval = await _pending_approval(s, run.id)
    pending = {
        "outcome": "pending",
        "run_id": str(run.id),
        "state": run.status,
        "current_agent": run.current_agent,
        **({"approval": approval_view(approval)} if approval else {}),
    }
    return types.CallToolResult(content=_text(pending), structuredContent=pending)


def build_server() -> Server[Any, Any]:
    server: Server[Any, Any] = Server("tuvl")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        user = _USER.get()
        return [
            tool_for(doc)
            for _, doc in sorted(exported(get_host()).items())
            if run_denial(user, doc) is None
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        host = get_host()
        doc = exported(host).get(name)
        user = _USER.get()
        if doc is None or run_denial(user, doc) is not None:
            return _error(
                f"unknown tool '{name}'"
            )  # never reveal a workflow the caller may not run
        try:
            run_id = await host.enqueue(
                doc, arguments or {}, principal=user.user_id if user else None, trigger="mcp"
            )
        except InputError as exc:
            return _error("the arguments do not match the tool's input schema", errors=exc.errors)
        except WorkflowUnavailableError as exc:
            return _error(str(exc))
        task = (
            asyncio.create_task(
                queue.run_inline(host.worker, run_id, timeout_seconds=CALL_WINDOW_SECONDS)
            )
            if host.role != "worker"
            else None
        )
        cursor = 0
        loop = asyncio.get_running_loop()
        started = loop.time()
        with wakeup.listen(run_id) as woken:
            while True:
                cursor = await _progress(server, host, run_id, cursor)
                run = await host.get(run_id)
                left = CALL_WINDOW_SECONDS - (loop.time() - started)
                if run.status not in ("queued", "running", "compensating") or left <= 0:
                    break
                # a commit here wakes it at once; one made by another process is found by the poll
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(woken.wait(), min(_POLL_SECONDS, left))
                woken.clear()
        if task is not None and task.done():
            task.result()
        return await _answer(host, run)

    return server


class _McpEndpoint:
    """``/mcp``: authenticate, then hand the request to the session manager."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        try:
            user = await current_user(request)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, exc.status_code)(scope, receive, send)
            return
        if user is None:
            await JSONResponse(
                {"detail": "Not authenticated"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
            return
        if not has_scope(user, MCP_CONNECT):
            await JSONResponse({"detail": f"Access denied. Required scope: '{MCP_CONNECT}'."}, 403)(
                scope, receive, send
            )
            return
        token = _USER.set(user)
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            _USER.reset(token)


async def mount_mcp(app: FastAPI, host: RuntimeHost, stack: AsyncExitStack) -> int:
    """Mount ``/mcp`` when at least one workflow is exported; the session
    manager runs until *stack* closes. Returns the number of exported tools."""
    tools = exported(host)
    if not tools:
        return 0
    manager = StreamableHTTPSessionManager(app=build_server(), stateless=True)
    await stack.enter_async_context(manager.run())
    endpoint = _McpEndpoint(manager)
    app.router.routes.append(Route("/mcp", endpoint=endpoint, methods=["GET", "POST", "DELETE"]))
    logger.info("runtime.mcp.mounted", tools=sorted(tools))
    return len(tools)
