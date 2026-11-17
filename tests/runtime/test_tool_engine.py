"""The tool engine (P1.6b): db on real Postgres, http on a mock transport,
mcp against a real stdio MCP server — all driven by the runner."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import Field, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.contract.schema import WorkflowDocument
from tuvl.core.models import loader as model_loader
from tuvl.core.models.schemas import SCHEMA_REGISTRY
from tuvl.runtime import journal, mcp_pool, queue
from tuvl.runtime.models import SystemRun
from tuvl.runtime.queue import Worker
from tuvl.runtime.runner import Runner, workflow_hash


class TicketSchema(BaseModel):
    id: int | None = None
    ref: str
    priority: int = 0


class Ticket(SQLModel, table=True):
    __tablename__ = "test_tool_tickets"
    id: int | None = Field(default=None, primary_key=True)
    ref: str = Field(sa_column_kwargs={"unique": True})
    priority: int = 0


@pytest.fixture
async def db(pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: Ticket.__table__.drop(c, checkfirst=True))  # type: ignore[attr-defined]
        await conn.run_sync(lambda c: Ticket.__table__.create(c))  # type: ignore[attr-defined]
    monkeypatch.setitem(model_loader.MODEL_REGISTRY, "Ticket", Ticket)
    variants = dict.fromkeys(("read", "create", "update", "expanded"), TicketSchema)
    monkeypatch.setitem(SCHEMA_REGISTRY, "Ticket", variants)
    return pg_engine


def tool_agent(tool: dict[str, Any], routes: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "id": "t",
        "description": "tool",
        "engine": "tool",
        "tool": tool,
        "routes": routes,
        **extra,
    }


def workflow(
    agent: dict[str, Any], trigger: dict[str, str], ends: dict[str, Any]
) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "wf"},
            "spec": {
                "models": ["Ticket"],
                "trigger": {"input": trigger},
                "agents": [{"inputs": trigger, **agent}],
                "outputs": ends,
            },
        }
    )


async def execute(db: AsyncEngine, doc: WorkflowDocument, payload: dict[str, Any]) -> SystemRun:
    async with AsyncSession(db) as s:
        run_id = await queue.enqueue(
            s, workflow="wf", workflow_hash=workflow_hash(doc), trigger="test", input=payload
        )
        await s.commit()
    worker = Worker(db, Runner(db, {"wf": doc}), owner="w")
    await worker.claim_some()
    await worker.drain()
    async with AsyncSession(db) as s:
        run = await s.get(SystemRun, run_id)
        assert run is not None
        return run


async def refs(db: AsyncEngine) -> list[tuple[str, int]]:
    async with AsyncSession(db) as s:
        rows = await s.exec(sa.select(Ticket.ref, Ticket.priority).order_by(Ticket.ref))  # type: ignore[call-overload,arg-type]
        return [tuple(r) for r in rows.all()]


ENDS = {
    "default": {"body": "{{ ticket.ref }}/{{ ticket.priority }}"},
    "missing": {"status": 404},
    "dup": {"status": 409},
    "failed": {"status": 500},
}


# ── db ──────────────────────────────────────────────────────────────────────


async def test_db_create_read_update_delete(db: AsyncEngine) -> None:
    create = workflow(
        tool_agent(
            {
                "use": "db",
                "model": "Ticket",
                "op": "create",
                "values": {"ref": "{{ ref }}", "priority": "{{ p }}"},
            },
            {"default": "END", "conflict": "END.dup", "error": "END.failed"},
            outputs={"ticket": "Ticket"},
        ),
        {"ref": "str", "p": "int"},
        ENDS,
    )
    made = await execute(db, create, {"ref": "T-1", "p": 2})
    assert made.output == {"status": 200, "body": "T-1/2"}
    dup = await execute(db, create, {"ref": "T-1", "p": 3})
    assert dup.end_name == "dup" and await refs(db) == [("T-1", 2)]

    update = workflow(
        tool_agent(
            {
                "use": "db",
                "model": "Ticket",
                "op": "update",
                "where": {"ref": "{{ ref }}"},
                "values": {"priority": "{{ p }}"},
            },
            {
                "default": "END",
                "conflict": "END.dup",
                "not_found": "END.missing",
                "error": "END.failed",
            },
            outputs={"ticket": "Ticket"},
        ),
        {"ref": "str", "p": "str"},
        ENDS,
    )
    assert (await execute(db, update, {"ref": "T-1", "p": "7"})).output == {
        "status": 200,
        "body": "T-1/7",
    }  # coerced
    assert (await execute(db, update, {"ref": "nope", "p": "1"})).end_name == "missing"
    assert (
        await execute(db, update, {"ref": "T-1", "p": "high"})
    ).end_name == "failed"  # tool_args_invalid

    read = workflow(
        tool_agent(
            {"use": "db", "model": "Ticket", "op": "read", "where": {"ref": "{{ ref }}"}},
            {"default": "END", "not_found": "END.missing", "error": "END.failed"},
            outputs={"ticket": "Ticket"},
        ),
        {"ref": "str"},
        ENDS,
    )
    assert (await execute(db, read, {"ref": "T-1"})).output == {"status": 200, "body": "T-1/7"}

    delete = workflow(
        tool_agent(
            {"use": "db", "model": "Ticket", "op": "delete", "where": {"ref": "{{ ref }}"}},
            {"default": "END.missing", "not_found": "END.failed", "error": "END.failed"},
        ),
        {"ref": "str"},
        ENDS,
    )
    assert (await execute(db, delete, {"ref": "T-1"})).end_name == "missing"
    assert await refs(db) == []
    assert (await execute(db, delete, {"ref": "T-1"})).end_name == "failed"


async def test_db_list_and_upsert(db: AsyncEngine) -> None:
    upsert = workflow(
        tool_agent(
            {
                "use": "db",
                "model": "Ticket",
                "op": "upsert",
                "where": {"ref": "{{ ref }}"},
                "values": {"priority": "{{ p }}"},
            },
            {"default": "END", "conflict": "END.dup", "error": "END.failed"},
            outputs={"ticket": "Ticket"},
        ),
        {"ref": "str", "p": "int"},
        ENDS,
    )
    for ref, p in (("A", 1), ("B", 5), ("A", 9)):
        await execute(db, upsert, {"ref": ref, "p": p})
    assert await refs(db) == [("A", 9), ("B", 5)]

    listing = workflow(
        tool_agent(
            {"use": "db", "model": "Ticket", "op": "list", "order_by": ["-priority"], "limit": 1},
            {"default": "END", "error": "END.failed"},
            outputs={"tickets": "list[Ticket]"},
        ),
        {},
        {"default": {"body": "{{ tickets }}"}, "failed": {"status": 500}},
    )
    body = (await execute(db, listing, {})).output["body"]  # type: ignore[index]
    assert [t["ref"] for t in body] == ["A"]


# ── http ────────────────────────────────────────────────────────────────────


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/refunds":
            return httpx.Response(201, json={"id": "rf_1", "amount": 10})
        if request.url.path == "/gone":
            return httpx.Response(410)
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * 500)
        if request.url.path == "/query":
            return httpx.Response(200, json={"seen": dict(request.url.params)})
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("tuvl.core.infra.http_client.get_http_client", lambda timeout=30.0: client)
    monkeypatch.setenv("PAY_KEY", "sk_test")
    return seen


HTTP_ENDS = {
    "default": {"body": "{{ refund_ref }}"},
    "gone": {"status": 410},
    "failed": {"status": 502},
}


async def test_http_maps_the_response_and_sends_idempotency(
    db: AsyncEngine, http: list[httpx.Request]
) -> None:
    doc = workflow(
        tool_agent(
            {
                "use": "http",
                "method": "POST",
                "url": "https://pay.test/refunds",
                "headers": {"Authorization": "Bearer ${PAY_KEY}"},
                "body": {"amount": "{{ amount }}"},
                "map": {"refund_ref": "response.body.id"},
                "idempotency": "auto",
            },
            {"default": "END", "error": "END.failed"},
            outputs={"refund_ref": "str"},
        ),
        {"amount": "int"},
        HTTP_ENDS,
    )
    run = await execute(db, doc, {"amount": 10})
    assert run.output == {"status": 200, "body": "rf_1"}
    request = http[0]
    assert request.headers["Authorization"] == "Bearer sk_test"
    assert request.headers["Idempotency-Key"].startswith(f"{run.id}:t:")
    assert request.content == b'{"amount":10}'


async def test_http_sends_typed_values_as_json(db: AsyncEngine, http: list[httpx.Request]) -> None:
    doc = workflow(
        tool_agent(
            {
                "use": "http",
                "method": "POST",
                "url": "https://pay.test/refunds",
                "body": {"id": "{{ ref }}", "on": "{{ day }}"},
                "query": {"ref": "{{ ref }}"},
            },
            {"default": "END", "error": "END.failed"},
        ),
        {"ref": "uuid", "day": "date"},
        HTTP_ENDS,
    )
    run = await execute(
        db, doc, {"ref": "6f1c7f5e-0000-4000-8000-000000000001", "day": "2026-10-08"}
    )
    assert run.end_name == "default"
    assert http[-1].content == b'{"id":"6f1c7f5e-0000-4000-8000-000000000001","on":"2026-10-08"}'
    assert http[-1].url.params["ref"] == "6f1c7f5e-0000-4000-8000-000000000001"
    async with AsyncSession(db) as s:
        types = [e.type for e in await journal.events(s, run.id)]
    assert "tool_call" in types and "tool_result" in types


async def test_http_status_signals_and_retryable_errors(
    db: AsyncEngine, http: list[httpx.Request]
) -> None:
    gone = workflow(
        tool_agent(
            {"use": "http", "method": "GET", "url": "https://pay.test/gone"},
            {"default": "END", "http_410": "END.gone", "error": "END.failed"},
        ),
        {},
        HTTP_ENDS,
    )
    assert (await execute(db, gone, {})).end_name == "gone"

    import tuvl.runtime.runner as runner_mod

    runner_mod._BACKOFF_BASE_SECONDS = 0.01
    down = workflow(
        tool_agent(
            {"use": "http", "method": "GET", "url": "https://pay.test/down"},
            {"default": "END", "error": "END.failed"},
            budget={"retry": {"attempts": 3, "errors": ["transient"]}},
        ),
        {},
        HTTP_ENDS,
    )
    http.clear()
    assert (await execute(db, down, {})).end_name == "failed"
    assert len(http) == 3


async def test_missing_env_var_is_an_error(db: AsyncEngine, http: list[httpx.Request]) -> None:
    doc = workflow(
        tool_agent(
            {
                "use": "http",
                "method": "GET",
                "url": "https://pay.test/refunds",
                "headers": {"X-Key": "${TUVL_TEST_UNSET_VAR}"},
            },
            {"default": "END", "error": "END.failed"},
        ),
        {},
        {"default": {}, "failed": {"status": 500}},
    )
    assert (await execute(db, doc, {})).end_name == "failed" and http == []


def _echo_agent(**tool: Any) -> dict[str, Any]:
    return tool_agent(
        {"use": "http", "method": "GET", **tool},
        {"default": "END", "error": "END.failed"},
    )


async def test_http_input_is_never_read_as_an_environment_reference(
    db: AsyncEngine, http: list[httpx.Request]
) -> None:
    doc = workflow(
        _echo_agent(url="https://pay.test/items/{{ note }}", headers={"X-Note": "{{ note }}"}),
        {"note": "str"},
        HTTP_ENDS,
    )
    await execute(db, doc, {"note": "${PAY_KEY}"})
    assert http[0].url.path == "/items/${PAY_KEY}"
    assert http[0].headers["X-Note"] == "${PAY_KEY}"


async def test_http_static_environment_references_still_expand(
    db: AsyncEngine, http: list[httpx.Request]
) -> None:
    doc = workflow(
        _echo_agent(
            url="https://pay.test/bot${PAY_KEY}/send/{{ who }}",
            headers={"Authorization": "Bearer ${PAY_KEY}", "X-Who": "{{ who }}"},
        ),
        {"who": "str"},
        HTTP_ENDS,
    )
    await execute(db, doc, {"who": "ann"})
    assert http[0].url.path == "/botsk_test/send/ann"
    assert http[0].headers["Authorization"] == "Bearer sk_test"
    assert http[0].headers["X-Who"] == "ann"


async def test_http_journal_and_errors_do_not_hold_the_expanded_value(
    db: AsyncEngine, http: list[httpx.Request]
) -> None:
    doc = workflow(_echo_agent(url="https://pay.test/down/${PAY_KEY}"), {}, HTTP_ENDS)
    run = await execute(db, doc, {})
    assert http[0].url.path == "/down/sk_test"
    async with AsyncSession(db) as s:
        events = await journal.events(s, run.id)
    assert "sk_test" not in json.dumps([e.payload for e in events])
    call = next(e for e in events if e.type == "tool_call")
    assert call.payload["url"] == "https://pay.test/down/${PAY_KEY}"
    assert "sk_test" not in json.dumps(run.error or {})


async def test_http_keeps_a_query_string_written_in_the_url(
    db: AsyncEngine, http: list[httpx.Request]
) -> None:
    doc = workflow(
        _echo_agent(
            url="https://pay.test/query?a=1",
            query={"b": "{{ b }}"},
            map={"seen": "response.body.seen"},
        ),
        {"b": "str"},
        {"default": {"body": "{{ seen.a }}-{{ seen.b }}"}, "failed": {"status": 500}},
    )
    doc.spec.agents[0].outputs = {"seen": "json"}  # type: ignore[assignment]
    run = await execute(db, doc, {"b": "2"})
    assert dict(http[0].url.params) == {"a": "1", "b": "2"}
    assert run.output == {"status": 200, "body": "1-2"}
    alone = workflow(_echo_agent(url="https://pay.test/query?only=1"), {}, HTTP_ENDS)
    await execute(db, alone, {})
    assert dict(http[-1].url.params) == {"only": "1"}


async def test_http_stops_reading_a_response_over_the_cap(
    db: AsyncEngine, http: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "tuvl_http_tool_max_bytes", 100)
    doc = workflow(_echo_agent(url="https://pay.test/big"), {}, HTTP_ENDS)
    run = await execute(db, doc, {})
    assert run.end_name == "failed"
    async with AsyncSession(db) as s:
        done = [e for e in await journal.events(s, run.id) if e.type == "agent_completed"]
    assert done[0].payload["error_type"] == "too_large"
    monkeypatch.setattr(settings, "tuvl_http_tool_max_bytes", 1000)
    assert (await execute(db, doc, {})).end_name == "default"


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "[::1]", "localhost"])
async def test_http_refuses_a_host_the_caller_chose_when_it_is_inside(
    db: AsyncEngine, http: list[httpx.Request], host: str
) -> None:
    doc = workflow(_echo_agent(url="http://{{ host }}/refunds"), {"host": "str"}, HTTP_ENDS)
    run = await execute(db, doc, {"host": host})
    assert run.end_name == "failed" and http == []  # nothing was sent
    async with AsyncSession(db) as s:
        done = [e for e in await journal.events(s, run.id) if e.type == "agent_completed"]
    assert done[0].payload["error_type"] == "destination_refused"


async def test_http_connects_to_the_address_it_checked_for_a_host_the_caller_chose(
    db: AsyncEngine, http: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl.webhooks import guard

    async def resolved(host: str, port: int, *, allow_private: bool = False) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(guard, "resolve", resolved)
    doc = workflow(_echo_agent(url="https://{{ host }}/refunds"), {"host": "str"}, HTTP_ENDS)
    run = await execute(db, doc, {"host": "api.partner.test"})
    assert run.end_name == "default"
    assert http[0].url.host == "93.184.216.34"
    assert http[0].headers["host"] == "api.partner.test"
    assert http[0].extensions["sni_hostname"] == "api.partner.test"


async def test_http_allow_private_admits_an_inside_host_but_never_the_metadata_address(
    db: AsyncEngine, http: list[httpx.Request]
) -> None:
    doc = workflow(
        _echo_agent(url="http://{{ host }}/refunds", allow_private=True),
        {"host": "str"},
        HTTP_ENDS,
    )
    assert (await execute(db, doc, {"host": "10.1.2.3"})).end_name == "default"
    assert http[-1].url.host == "10.1.2.3"
    http.clear()
    assert (await execute(db, doc, {"host": "169.254.169.254"})).end_name == "failed"
    assert http == []


async def test_http_calls_a_host_the_workflow_names_even_when_it_is_inside(
    db: AsyncEngine, http: list[httpx.Request]
) -> None:
    doc = workflow(_echo_agent(url="http://orders.internal:8080/refunds"), {}, HTTP_ENDS)
    assert (await execute(db, doc, {})).end_name == "default"  # the author wrote it: trusted
    assert http[0].url.host == "orders.internal"
    metadata = workflow(_echo_agent(url="http://169.254.169.254/latest/meta-data"), {}, HTTP_ENDS)
    assert (await execute(db, metadata, {})).end_name == "failed"  # but never the metadata address


# ── mcp ─────────────────────────────────────────────────────────────────────


async def test_mcp_stdio_tool(
    db: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = tmp_path / "server.py"
    server.write_text(
        textwrap.dedent("""
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("t")

        @mcp.tool()
        def add_comment(ticket: str, body: str) -> dict:
            return {"comment_id": f"{ticket}:{len(body)}"}

        @mcp.tool()
        def explode() -> str:
            raise ValueError("boom")

        mcp.run()
    """)
    )
    config = {"transport": "stdio", "command": sys.executable, "args": [str(server)]}
    monkeypatch.setattr("tuvl.core.artifacts.registry.resolve_structured", lambda ref, **_: config)

    ok = workflow(
        tool_agent(
            {
                "use": "mcp",
                "server": "artifact://helpdesk@1",
                "name": "add_comment",
                "args": {"ticket": "{{ ticket }}", "body": "hello"},
            },
            {"default": "END", "error": "END.failed"},
            outputs={"comment_id": "str"},
        ),
        {"ticket": "str"},
        {"default": {"body": "{{ comment_id }}"}, "failed": {"status": 502}},
    )
    assert (await execute(db, ok, {"ticket": "T-9"})).output == {"status": 200, "body": "T-9:5"}

    bad = workflow(
        tool_agent(
            {"use": "mcp", "server": "artifact://helpdesk@1", "name": "explode"},
            {"default": "END", "error": "END.failed"},
        ),
        {},
        {"default": {}, "failed": {"status": 502}},
    )
    failed = await execute(db, bad, {})
    assert failed.end_name == "failed"
    await mcp_pool.aclose_all()


async def test_db_where_values_take_the_column_type(db: AsyncEngine) -> None:
    create = workflow(
        tool_agent(
            {
                "use": "db",
                "model": "Ticket",
                "op": "create",
                "values": {"ref": "T-9", "priority": 7},
            },
            {"default": "END", "conflict": "END.dup", "error": "END.failed"},
            outputs={"ticket": "Ticket"},
        ),
        {},
        ENDS,
    )
    assert (await execute(db, create, {})).end_name == "default"
    read = workflow(
        tool_agent(
            {"use": "db", "model": "Ticket", "op": "read", "where": {"priority": "{{ p }}"}},
            {"default": "END", "not_found": "END.missing", "error": "END.failed"},
            outputs={"ticket": "Ticket"},
        ),
        {"p": "str"},
        ENDS,
    )
    assert (await execute(db, read, {"p": "7"})).output == {
        "status": 200,
        "body": "T-9/7",
    }  # "7" → 7
    bad = await execute(db, read, {"p": "high"})
    assert bad.end_name == "failed"
    async with AsyncSession(db) as s:
        messages = [
            str(e.payload.get("message"))
            for e in await journal.events(s, bad.id)
            if e.type == "agent_completed"
        ]
    assert any("Ticket.priority: 'high' is not a valid" in m for m in messages), messages


def test_a_uuid_rendered_into_a_string_column_is_stored_as_text() -> None:
    import uuid

    from tuvl.runtime.tool import _coerce

    ident = uuid.uuid4()
    assert _coerce(Ticket, {"ref": ident}).ref == str(ident)
