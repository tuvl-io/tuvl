"""Mount each 2.0 workflow's ``trigger.http`` route (spec §15.1).

The request body (or, for ``GET``/``DELETE``, the query string) plus any
``{path}`` parameters form the run input, validated against
``trigger.input``. The response is the end's body and status, or ``202``
with the run descriptor when the run outlives ``sync_timeout`` or waits.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from tuvl.api.auth import trigger_user
from tuvl.api.runs import start_run
from tuvl.api.uploads import file_inputs, is_multipart, start_upload_run
from tuvl.contract.schema import WorkflowDocument
from tuvl.contract.surface import end_schema, input_schema
from tuvl.contract.types import ModelResolver
from tuvl.core.config import settings

logger = structlog.get_logger(__name__)

Endpoint = Callable[[Request], Coroutine[Any, Any, JSONResponse]]


async def _payload(request: Request, method: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if method in ("GET", "DELETE"):
        payload.update(request.query_params)
    else:
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not valid JSON") from exc
            if not isinstance(body, dict):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "body must be a JSON object"
                )
            payload.update(body)
    payload.update(request.path_params)
    return payload


def _endpoint(doc: WorkflowDocument, method: str) -> Endpoint:
    async def endpoint(request: Request) -> JSONResponse:
        user = await trigger_user(
            request, doc
        )  # who may start it is settled before the body is read
        if method not in ("GET", "DELETE") and is_multipart(request):
            return await start_upload_run(request, doc, user=user)
        return await start_run(
            request, doc, await _payload(request, method), trigger="http", user=user
        )

    endpoint.__name__ = f"trigger_{doc.metadata.name}"
    return endpoint


_RUN_DESCRIPTOR = {
    "type": "object",
    "properties": {
        "run_id": {"type": "string", "format": "uuid"},
        "workflow": {"type": "string"},
        "status": {"type": "string"},
        "current_agent": {"type": ["string", "null"]},
        "approval": {"type": "object"},
    },
    "required": ["run_id", "status"],
}


def trigger_openapi(doc: WorkflowDocument, resolver: ModelResolver | None = None) -> dict[str, Any]:
    """The OpenAPI operation fields of *doc*'s trigger route, from its contracts:
    path/query parameters or the JSON body from ``trigger.input``, one response
    per end status, and ``202`` with the run descriptor. ``x-tuvl-workflow``
    names the workflow for client generators."""
    http = doc.spec.trigger.http
    assert http is not None
    schema = input_schema(doc, resolver)
    props: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    in_path = re.findall(r"{([A-Za-z_][A-Za-z0-9_]*)}", http.path)
    parameters = [
        {"name": n, "in": "path", "required": True, "schema": props.get(n, {"type": "string"})}
        for n in in_path
    ]
    operation: dict[str, Any] = {"x-tuvl-workflow": doc.metadata.name}
    rest = {k: v for k, v in props.items() if k not in in_path}
    if http.method in ("GET", "DELETE"):
        parameters += [
            {"name": n, "in": "query", "required": n in required, "schema": rest[n]} for n in rest
        ]
    elif rest:
        files = {n: many for n, many in file_inputs(doc).items() if n in rest}
        needed = [n for n in schema.get("required", []) if n in rest]
        content: dict[str, Any] = {}
        if not set(needed) & set(files):
            json_props = {k: v for k, v in rest.items() if k not in files}
            content["application/json"] = {
                "schema": {
                    **schema,
                    "properties": json_props,
                    "required": [n for n in needed if n in json_props],
                }
            }
        if files and http.upload is not None:
            binary: dict[str, Any] = {"type": "string", "format": "binary"}
            form_props = {
                k: (
                    ({"type": "array", "items": binary} if files[k] else binary)
                    if k in files
                    else v
                )
                for k, v in rest.items()
            }
            content["multipart/form-data"] = {
                "schema": {
                    "type": "object",
                    "properties": form_props,
                    "required": needed,
                    "additionalProperties": False,
                },
                "encoding": {
                    n: {"contentType": ", ".join(http.upload.accept) or "application/octet-stream"}
                    for n in files
                },
            }
        if content:
            operation["requestBody"] = {"required": bool(needed), "content": content}
    if parameters:
        operation["parameters"] = parameters

    by_status: dict[int, list[tuple[str, dict[str, Any] | None]]] = {}
    for end, spec in doc.spec.outputs.items():
        by_status.setdefault(spec.status, []).append((end, end_schema(doc, end, resolver)))
    responses: dict[str, Any] = {}
    for code, ends in sorted(by_status.items()):
        names = ", ".join(f"END.{e}" if e != "default" else "END" for e, _ in ends)
        schemas = [s for _, s in ends if s is not None]
        entry: dict[str, Any] = {"description": names}
        if schemas:
            body_schema = schemas[0] if len(schemas) == 1 else {"oneOf": schemas}
            entry["content"] = {"application/json": {"schema": body_schema}}
        responses[str(code)] = entry
    responses.setdefault(
        "202",
        {
            "description": "The run outlived sync_timeout or waits on a person or approval",
            "content": {"application/json": {"schema": _RUN_DESCRIPTOR}},
        },
    )
    responses.setdefault("422", {"description": "The input does not match trigger.input"})
    operation["responses"] = responses
    return operation


def mount_triggers(app: FastAPI, workflows: dict[str, WorkflowDocument]) -> int:
    mounted = 0
    taken = {
        (m, getattr(r, "path", "")) for r in app.routes for m in getattr(r, "methods", ()) or ()
    }
    for name, doc in sorted(workflows.items()):
        http = doc.spec.trigger.active_http
        if http is None or not doc.enabled:
            continue
        if (http.method, http.path) in taken:
            logger.error(
                "runtime.trigger.path_taken", workflow=name, method=http.method, path=http.path
            )
            if settings.is_production:
                raise RuntimeError(
                    f"workflow '{name}' cannot be served: {http.method} {http.path} is already taken "
                    "by another workflow or route. Give it a path of its own."
                )
            continue
        app.add_api_route(
            http.path,
            _endpoint(doc, http.method),
            methods=[http.method],
            summary=doc.metadata.description or name,
            tags=["workflows"],
            openapi_extra=trigger_openapi(doc),
        )
        taken.add((http.method, http.path))
        mounted += 1
        logger.info("runtime.trigger.mounted", workflow=name, method=http.method, path=http.path)
    return mounted
