"""The ``tool`` engine (spec §9.3): one engine, four ``use`` variants.

Parameters are templates rendered against the agent's view (``{{ x }}``
alone keeps its type). ``${VAR}`` in http urls/headers and MCP server config
reads the environment.

* ``db`` — the result lands in the agent's single output (spec Q11):
  ``read``/``create``/``update``/``upsert`` a row, ``list`` a list of rows,
  ``delete`` nothing. Missing rows emit ``not_found``, constraint violations
  ``conflict``. Writes run in the agent's transaction.
* ``http`` — ``expect.status`` decides success; another status becomes
  ``http_<status>`` when that signal is routed, else ``error``. ``map`` fills
  outputs from ``response.status|headers|body`` paths; with no ``map`` a
  single output receives the body. ``idempotency: auto`` sends the step's
  idempotency key.
* ``storage`` — one object operation on a named ``kind: Storage`` store. The result lands in
  the single output (``put``/``head``/``copy`` the blob, ``get`` the content, ``list`` the items,
  ``presign`` the URL) or is spread by ``map``; bodies and URLs are never journaled.
* ``mcp`` — calls one tool on a ``type: mcp`` artifact server. An object
  result that has every declared output spreads across them; otherwise a
  single output receives the structured content (or the parsed text).
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from functools import partial
from typing import Any

import httpx
import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from tuvl.contract.tel import compile_expr
from tuvl.runtime.engines import (
    AgentResult,
    EngineContext,
    EngineError,
    derived,
    register_engine,
    render_params,
)

_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


render = render_params


def jsonable(value: Any) -> Any:
    """Typed view values (uuid, date, decimal…) as JSON-safe values for the wire."""
    return to_jsonable_python(value)


def _env(value: str, source: str) -> str:
    from tuvl.core.engine.templating import expand_env

    try:
        return expand_env(value, source=source) or ""
    except OSError as exc:
        raise EngineError("tool_args_invalid", str(exc)) from exc


def _single_output(agent: Any, signal: str = "default") -> str | None:
    outputs = agent.outputs_for(signal)
    return next(iter(outputs)) if len(outputs) == 1 else None


# ── db ───────────────────────────────────────────────────────────────────────


def _model(name: str) -> type[SQLModel]:
    from tuvl.core.models.loader import MODEL_REGISTRY

    cls = MODEL_REGISTRY.get(name)
    if cls is None:
        raise EngineError("db_error", f"model '{name}' is not loaded")
    return cls


def _select(cls: type[SQLModel], where: dict[str, Any]) -> Any:
    unknown = set(where) - set(cls.model_fields)
    if unknown:
        raise EngineError(
            "tool_args_invalid", f"'{cls.__name__}' has no field(s) {sorted(unknown)}"
        )
    typed: dict[str, Any] = {}
    for key, value in where.items():  # "…-…" for a uuid column, "2026-01-01" for a date
        try:
            ann = cls.model_fields[key].annotation
            adapter: TypeAdapter[Any] = derived(cls, ("where", key), partial(TypeAdapter, ann))
            typed[key] = adapter.validate_python(value)
        except ValidationError as exc:
            raise EngineError(
                "tool_args_invalid",
                f"{cls.__name__}.{key}: {value!r} is not a valid {exc.errors()[0]['msg'].lower()}",
            ) from exc
    return sa.select(cls).filter_by(**typed)


def _as_text(cls: type[SQLModel], values: dict[str, Any]) -> dict[str, Any]:
    """A uuid, number or date rendered into a string column is stored as its text
    (``subject_id: "{{ community.id }}"`` keeps the raw uuid of a single placeholder)."""
    out = dict(values)
    for key, value in values.items():
        field = cls.model_fields.get(key)
        if field is None or value is None or isinstance(value, str | bool | dict | list):
            continue
        if field.annotation in (str, str | None):
            out[key] = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return out


def _coerce(cls: type[SQLModel], values: dict[str, Any]) -> SQLModel:
    """Validate *values* into a row (table models skip validation on init)."""
    try:
        return cls.model_validate(_as_text(cls, values))
    except ValueError as exc:
        raise EngineError("tool_args_invalid", f"{cls.__name__}: {exc}") from exc


async def _db(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    tool = agent.tool
    cls = _model(tool.model)
    session = ctx.session
    where = render(tool.where or {}, view)
    values = render(tool.values or {}, view)

    async def first() -> SQLModel | None:
        rows = await session.execute(_select(cls, where).limit(2))
        found = rows.scalars().all()
        if len(found) > 1 and tool.op != "upsert":
            raise EngineError("db_error", f"'where' matched more than one {cls.__name__}")
        return found[0] if found else None

    result: Any = None
    try:
        if tool.op == "list":
            stmt = _select(cls, where)
            for key in tool.order_by or []:
                column = getattr(cls, key.lstrip("-"))
                stmt = stmt.order_by(column.desc() if key.startswith("-") else column)
            stmt = stmt.limit(tool.limit or 100)
            result = [r.model_dump() for r in (await session.execute(stmt)).scalars().all()]
        elif tool.op == "create":
            row = _coerce(cls, values)
            session.add(row)
            await session.flush()
            result = row.model_dump()
        else:
            existing = await first()
            if tool.op == "upsert" and existing is None:
                row = _coerce(cls, {**where, **values})
                session.add(row)
                await session.flush()
                result = row.model_dump()
            elif existing is None:
                return AgentResult("not_found", message=f"no {cls.__name__} matches {where}")
            elif tool.op == "read":
                result = existing.model_dump()
            elif tool.op == "delete":
                await session.delete(existing)
                await session.flush()
            else:  # update / upsert of an existing row
                merged = _coerce(cls, {**existing.model_dump(), **values})
                for key in values:
                    setattr(existing, key, getattr(merged, key))
                await session.flush()
                result = existing.model_dump()
    except IntegrityError as exc:
        return AgentResult("conflict", message=str(exc.orig).splitlines()[0])

    key = _single_output(agent)
    detail = {"model": tool.model, "op": tool.op}
    return AgentResult(
        "default", outputs={key: result} if key and tool.op != "delete" else {}, detail=detail
    )


# ── http ─────────────────────────────────────────────────────────────────────


async def _http(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    from tuvl.core.config import settings
    from tuvl.core.infra.http_client import get_http_client

    tool = agent.tool
    where = f"agent '{agent.id}'"
    # ${VAR} is read from the workflow's own text, before any input is rendered into it: a value
    # that arrives through {{ }} is data and must never be read as an environment reference.
    url = str(render(_env(tool.url, f"{where} url"), view))
    shown = str(render(tool.url, view))  # what the journal and errors say: ${VAR} left as written
    headers = {
        k: str(render(_env(v, f"{where} header '{k}'"), view)) for k, v in tool.headers.items()
    }
    if tool.idempotency == "auto" and tool.method in _UNSAFE_METHODS:
        headers.setdefault("Idempotency-Key", ctx.idempotency_key)
    query = {k: v for k, v in jsonable(render(tool.query, view)).items() if v is not None}
    body = jsonable(render(tool.body, view))

    try:
        target = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise EngineError("tool_args_invalid", f"{tool.method} {shown}: not a valid URL") from exc
    if query:  # httpx replaces a URL's own query when params are given, so merge them here
        target = target.copy_merge_params(query)
    target, headers, extensions = await _destination(tool, target, headers, shown)

    await ctx.emit("tool_call", payload={"use": "http", "method": tool.method, "url": shown})
    limit = settings.tuvl_http_tool_max_bytes
    try:
        async with get_http_client().stream(
            tool.method,
            target,
            headers=headers,
            json=body if body is not None else None,
            extensions=extensions,
        ) as response:
            raw = await _read_capped(response, limit)
    except _TooLargeError:
        return AgentResult(
            "too_large" if "too_large" in agent.routes else "error",
            error_type="too_large",
            message=f"{tool.method} {shown}: the response is larger than {limit} bytes",
        )
    except httpx.TimeoutException as exc:
        raise EngineError("timeout", f"{tool.method} {shown}: {type(exc).__name__}") from exc
    except httpx.HTTPError as exc:
        reason = str(exc).replace(url, shown)
        raise EngineError(
            "transient", f"{tool.method} {shown}: {type(exc).__name__}: {reason}"
        ) from exc

    text = raw.decode(response.encoding or "utf-8", errors="replace")
    try:
        parsed: Any = json.loads(text) if raw else None
    except ValueError:
        parsed = text
    data = {
        "response": {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": parsed,
        }
    }
    await ctx.emit("tool_result", payload={"status": response.status_code})
    detail = {"status": response.status_code}

    if response.status_code not in tool.expect.status:
        mapped = f"http_{response.status_code}"
        if mapped in agent.routes:
            return AgentResult(mapped, outputs=_map(agent, tool, data, mapped), detail=detail)
        error_type = (
            "rate_limit"
            if response.status_code == 429
            else "transient"
            if response.status_code >= 500
            else "http_error"
        )
        return AgentResult(
            "error",
            error_type=error_type,
            message=f"{tool.method} {shown} returned {response.status_code}",
            detail=detail,
        )
    return AgentResult("default", outputs=_map(agent, tool, data, "default"), detail=detail)


class _TooLargeError(Exception):
    """A response is over the cap."""


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """The body of *response*, or ``_TooLargeError`` as soon as it is known to be over *limit* bytes (0: no cap)."""
    declared = response.headers.get("content-length", "")
    if limit and declared.isdigit() and int(declared) > limit:
        raise _TooLargeError
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if limit and size > limit:
            raise _TooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


def _host_is_dynamic(template: str) -> bool:
    """Whether the host of a url template comes from a ``{{ }}`` placeholder: a caller, or a model, chose it."""
    _, scheme, rest = template.partition("://")
    authority = rest.split("/", 1)[0].split("?", 1)[0] if scheme else template
    return "{{" in authority


async def _destination(
    tool: Any, target: httpx.URL, headers: dict[str, str], shown: str
) -> tuple[httpx.URL, dict[str, str], dict[str, Any]]:
    """Refuse a destination that must not be reached.

    A host the workflow's author wrote is trusted (an in-cluster service is a fine target), except that no
    one may name a cloud-metadata address. A host that arrives through ``{{ }}`` is not: it must resolve to
    a public address (``allow_private`` admits inside ones), and the request goes to the address that was
    checked, so the name cannot answer differently the second time."""
    from tuvl.webhooks import guard

    host = target.host
    literal = guard._literal_ip(host)
    if literal is not None and guard.is_metadata(literal):
        raise EngineError(
            "destination_refused",
            f"{tool.method} {shown}: a cloud-metadata address is never a destination",
        )
    if not _host_is_dynamic(tool.url):
        return target, headers, {}
    port = target.port or (443 if target.scheme == "https" else 80)
    try:
        addresses = await guard.resolve(host, port, allow_private=tool.allow_private)
    except guard.BlockedAddress as exc:
        raise EngineError("destination_refused", f"{tool.method} {shown}: {exc}") from exc
    except OSError as exc:
        raise EngineError(
            "transient", f"{tool.method} {shown}: the host name did not resolve"
        ) from exc
    authority = host if target.port is None else f"{host}:{target.port}"
    extensions = {"sni_hostname": host} if target.scheme == "https" else {}
    return target.copy_with(host=addresses[0]), {**headers, "Host": authority}, extensions


def _map(agent: Any, tool: Any, data: dict[str, Any], signal: str) -> dict[str, Any]:
    if not tool.map:
        key = _single_output(agent, signal)
        return {key: data["response"]["body"]} if key else {}
    declared = agent.outputs_for(signal)
    return {
        key: compile_expr(path).evaluate(data) for key, path in tool.map.items() if key in declared
    }


# ── mcp ──────────────────────────────────────────────────────────────────────


def mcp_server_config(server_ref: str, where: str) -> dict[str, Any]:
    """The ``type: mcp`` artifact's config with ``${VAR}`` in env, url and headers resolved."""
    from tuvl.core.artifacts.registry import resolve_structured

    try:
        server = dict(resolve_structured(server_ref, site="mcp.server", where=where))
    except Exception as exc:
        raise EngineError("mcp_error", str(exc)) from exc
    if server.get("transport", "sse") == "stdio":
        server["env"] = {
            k: _env(str(v), f"{where} env") for k, v in (server.get("env") or {}).items()
        }
    else:
        server["url"] = _env(str(server.get("url", "")), f"{where} url")
        server["headers"] = {
            k: _env(str(v), f"{where} headers") for k, v in (server.get("headers") or {}).items()
        }
    return server


@contextlib.asynccontextmanager
async def mcp_connect(server_ref: str, server: dict[str, Any]) -> AsyncIterator[Any]:
    """An initialised MCP ClientSession for a resolved server config (``mcp_server_config``)."""
    from tuvl.extras import require

    require("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client

    read_timeout = timedelta(seconds=60)
    try:
        if server.get("transport", "sse") == "stdio":
            params = StdioServerParameters(
                command=server.get("command", "npx"),
                args=server.get("args") or [],
                env=server.get("env") or None,
            )
            transport = stdio_client(params)
        else:
            transport = sse_client(server["url"], headers=server.get("headers") or None)
        async with transport as (r, w), ClientSession(r, w, read_timeout_seconds=read_timeout) as s:
            await s.initialize()
            yield s
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError("mcp_error", f"{server_ref}: {type(exc).__name__}: {exc}") from exc


@contextlib.asynccontextmanager
async def mcp_session(server_ref: str, where: str) -> AsyncIterator[Any]:
    """An initialised MCP ClientSession for a ``type: mcp`` artifact server."""
    from tuvl.extras import require

    require("mcp")
    async with mcp_connect(server_ref, mcp_server_config(server_ref, where)) as session:
        yield session


def mcp_value(result: Any) -> tuple[bool, Any]:
    """``(ok, value)`` of an MCP tool result: structured content, else parsed text."""
    text = "".join(getattr(block, "text", "") for block in result.content)
    if result.isError:
        return False, text[:500]
    value: Any = result.structuredContent
    if value is None:
        try:
            value = json.loads(text)
        except ValueError:
            value = text
    return True, value


async def _mcp(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    tool = agent.tool
    args = jsonable(render(tool.args, view))
    await ctx.emit("tool_call", payload={"use": "mcp", "server": tool.server, "name": tool.name})
    from tuvl.runtime import mcp_pool

    result = await mcp_pool.call_tool(
        tool.server, f"agent '{agent.id}' tool.server", tool.name, args
    )
    ok, value = mcp_value(result)
    if not ok:
        return AgentResult("error", error_type="mcp_error", message=value or f"{tool.name} failed")
    await ctx.emit("tool_result", payload={"name": tool.name})
    declared = agent.outputs_for("default")
    if isinstance(value, dict) and declared and set(declared) <= set(value):
        return AgentResult("default", outputs={k: value[k] for k in declared})
    key = _single_output(agent)
    return AgentResult("default", outputs={key: value} if key else {})


# ── storage ──────────────────────────────────────────────────────────────────

INLINE_GET_BYTES = 1024 * 1024


def _body_bytes(tool: Any, body: Any, where: str) -> bytes:
    if tool.encoding == "base64":
        try:
            return base64.b64decode(str(body), validate=True)
        except ValueError as exc:
            raise EngineError("tool_args_invalid", f"{where}: body is not valid base64") from exc
    if tool.encoding == "json":
        return json.dumps(jsonable(body)).encode()
    return (body if isinstance(body, str) else str(body)).encode()


def _content(tool: Any, data: bytes, where: str) -> Any:
    if tool.as_ == "base64":
        return base64.b64encode(data).decode()
    try:
        text = data.decode()
        return json.loads(text) if tool.as_ == "json" else text
    except ValueError as exc:
        raise EngineError(
            "tool_args_invalid", f"{where}: content is not valid {tool.as_}; use as: base64"
        ) from exc


_STORAGE_PRIMARY = {
    "put": "blob",
    "head": "blob",
    "copy": "blob",
    "get": "content",
    "list": "items",
    "presign": "url",
}


def _storage_outputs(agent: Any, tool: Any, data: dict[str, Any]) -> dict[str, Any]:
    if not tool.map:
        key = _single_output(agent)
        primary = _STORAGE_PRIMARY.get(tool.op)
        return {key: data[primary]} if key and primary else {}
    declared = agent.outputs_for("default")
    return {
        key: compile_expr(path).evaluate(data) for key, path in tool.map.items() if key in declared
    }


async def _storage(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    from tuvl import storage

    tool = agent.tool
    where = f"agent '{agent.id}'"
    try:
        store = storage.get_store(tool.store)
        target = storage.get_store(tool.to_store) if tool.to_store else store
    except storage.StorageError as exc:
        raise EngineError("storage_config", f"{where}: {exc}") from exc
    key = str(render(tool.key, view)) if tool.key else ""
    to_key = str(render(tool.to_key, view)) if tool.to_key else ""
    await ctx.emit(
        "tool_call",
        payload={"use": "storage", "op": tool.op, "store": tool.store, "key": key or None},
    )
    data: dict[str, Any] = {}
    detail: dict[str, Any] = {"op": tool.op, "store": tool.store}
    try:
        if tool.op == "put":
            content_type = tool.content_type or (
                "application/json" if tool.encoding == "json" else ""
            )
            blob = await store.put(
                key,
                _body_bytes(tool, render(tool.body, view), where),
                content_type=str(render(content_type, view)) if content_type else "",
                metadata={k: str(render(v, view)) for k, v in tool.metadata.items()},
                if_exists=tool.if_exists,
            )
            data = {"blob": blob.to_dict()}
        elif tool.op == "get":
            raw, blob = await store.get(key, tool.max_bytes or INLINE_GET_BYTES)
            data = {"content": _content(tool, raw, where), "blob": blob.to_dict()}
        elif tool.op == "head":
            data = {"blob": (await store.head(key)).to_dict()}
        elif tool.op == "list":
            listing = await store.list(
                str(render(tool.prefix, view)) if tool.prefix else "",
                tool.limit or 100,
                str(render(tool.cursor, view)) if tool.cursor else None,
            )
            data = {"items": [b.to_dict() for b in listing.items], "cursor": listing.cursor}
        elif tool.op == "copy":
            blob = await store.copy(key, to_key, to_store=target, if_exists=tool.if_exists)
            data = {"blob": blob.to_dict()}
        elif tool.op == "delete":
            await store.delete(key)
        else:
            signed = await store.presign(key, tool.method, tool.expires_in or 900)
            data = {"url": signed.url, "expires_at": signed.expires_at}
    except storage.InvalidKey as exc:
        raise EngineError("tool_args_invalid", f"{where}: {exc}") from exc
    except (storage.NotFound, storage.Conflict, storage.TooLarge) as exc:
        if exc.code in agent.routes:
            return AgentResult(exc.code, message=str(exc), detail=detail)
        error_type = "storage_error" if isinstance(exc, storage.TooLarge) else exc.code
        return AgentResult("error", error_type=error_type, message=str(exc), detail=detail)
    except storage.StorageError as exc:
        return AgentResult("error", error_type=exc.code, message=str(exc), detail=detail)

    blob_info = data.get("blob") or {}
    await ctx.emit(
        "tool_result",
        payload={"op": tool.op, "size": blob_info.get("size"), "sha256": blob_info.get("sha256")},
    )
    return AgentResult("default", outputs=_storage_outputs(agent, tool, data), detail=detail)


_VARIANTS = {"db": _db, "http": _http, "mcp": _mcp, "storage": _storage}


async def tool_engine(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    return await _VARIANTS[agent.tool.use](agent, view, ctx)


register_engine("tool", tool_engine)
