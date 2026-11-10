"""The ``storage`` tool variant: schema rules, validator rules, and runs against a local store."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.runtime.test_tool_engine import execute, tool_agent, workflow
from tuvl.contract.schema import StorageTool, WorkflowDocument
from tuvl.runtime import journal
from tuvl.storage import STORE_REGISTRY, register_store_from_dict
from tuvl_insight.validate import Catalog, validate_workflow

ENDS = {
    "default": {"body": "{{ out }}"},
    "missing": {"status": 404},
    "dup": {"status": 409},
    "big": {"status": 413},
    "failed": {"status": 500},
}
ROUTES = {
    "default": "END",
    "not_found": "END.missing",
    "conflict": "END.dup",
    "error": "END.failed",
}


@pytest.fixture(autouse=True)
def stores(tmp_path: Path) -> Iterator[Path]:
    STORE_REGISTRY.clear()
    for name, extra in (("docs", {"prefix": "d/"}), ("ro", {"readonly": True})):
        register_store_from_dict(
            {
                "kind": "Storage",
                "metadata": {"name": name},
                "spec": {"provider": "local", "root": str(tmp_path / "blobs"), **extra},
            }
        )
    yield tmp_path / "blobs"
    STORE_REGISTRY.clear()


def storage_wf(
    tool: dict[str, Any],
    trigger: dict[str, str],
    outputs: dict[str, str] | None = None,
    routes: dict[str, str] | None = None,
) -> WorkflowDocument:
    agent = tool_agent(
        {"use": "storage", "store": "docs", **tool},
        routes or ROUTES,
        outputs=outputs or {},
    )
    doc = workflow(agent, trigger, {**ENDS, "default": {"body": "{{ out }}"}})  # type: ignore[arg-type]
    doc.spec.models = []
    return doc


def two_step(
    first: dict[str, Any], second: dict[str, Any], out_type: str = "str"
) -> WorkflowDocument:
    a = tool_agent(
        {"use": "storage", "store": "docs", **first},
        {"default": "b", "conflict": "END.dup", "error": "END.failed"},
        outputs={"blob": "json"},
    )
    a["id"] = "a"
    b = tool_agent({"use": "storage", "store": "docs", **second}, ROUTES, outputs={"out": out_type})
    b["id"] = "b"
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "wf"},
            "spec": {
                "models": [],
                "trigger": {"input": {"name": "str", "text": "str"}},
                "agents": [
                    {"inputs": {"name": "str", "text": "str"}, **a},
                    {"inputs": {"name": "str"}, **b},
                ],
                "outputs": {**ENDS},
            },
        }
    )


# ── schema ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "message"),
    [
        ({"op": "put", "key": "k"}, "needs 'body'"),
        ({"op": "put", "body": "x"}, "needs 'key'"),
        ({"op": "get", "key": "k", "body": "x"}, "does not take: body"),
        ({"op": "list", "key": "k"}, "does not take: key"),
        ({"op": "copy", "key": "k"}, "needs 'to_key'"),
        ({"op": "delete", "key": "k", "if_exists": "overwrite"}, "does not take: if_exists"),
        ({"op": "presign", "key": "k", "as": "json"}, "does not take: as"),
    ],
)
def test_op_fields_are_checked(tool: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        StorageTool.model_validate({"use": "storage", "store": "docs", **tool})


def test_valid_ops_parse() -> None:
    for tool in (
        {"op": "put", "key": "k", "body": "x", "encoding": "base64", "content_type": "a/b"},
        {"op": "get", "key": "k", "as": "json", "max_bytes": 10},
        {"op": "list", "prefix": "a/", "limit": 5},
        {"op": "copy", "key": "a", "to_key": "b", "to_store": "other"},
        {"op": "presign", "key": "k", "method": "PUT", "expires_in": 60},
    ):
        StorageTool.model_validate({"use": "storage", "store": "docs", **tool})


# ── validator ───────────────────────────────────────────────────────────────


def codes(doc: WorkflowDocument, cat: Catalog | None = None) -> set[str]:
    cat = cat or Catalog(stores=dict(STORE_REGISTRY))
    report = validate_workflow(doc, cat)
    return {d.code for d in report.diagnostics if d.severity == "error"}


def one(tool: dict[str, Any], **kw: Any) -> WorkflowDocument:
    return storage_wf(tool, {"name": "str", "text": "str"}, **kw)


def test_valid_storage_workflow_is_clean() -> None:
    put = one(
        {"op": "put", "key": "{{ name }}", "body": "{{ text }}"},
        outputs={"out": "json"},
        routes={"default": "END", "conflict": "END.dup", "error": "END.failed"},
    )
    assert codes(put) == set()
    head = one(
        {"op": "head", "key": "{{ name }}"},
        outputs={"out": "json"},
        routes={"default": "END", "not_found": "END.missing", "error": "END.failed"},
    )
    assert codes(head) == set()


def test_v030_unknown_store() -> None:
    doc = one({"store": "ghost", "op": "head", "key": "k"}, outputs={"out": "json"})
    assert "V030" in codes(doc)
    assert "V030" not in codes(doc, Catalog())  # stores not scanned


def test_v031_readonly_store() -> None:
    doc = one({"store": "ro", "op": "put", "key": "k", "body": "x"}, outputs={"out": "json"})
    assert "V031" in codes(doc)
    assert "V031" not in codes(
        one({"store": "ro", "op": "head", "key": "k"}, outputs={"out": "json"})
    )


@pytest.mark.parametrize("key", ["../x", "/abs", "a//b"])
def test_v032_unsafe_literal_key(key: str) -> None:
    assert "V032" in codes(one({"op": "head", "key": key}, outputs={"out": "json"}))
    assert "V032" not in codes(one({"op": "head", "key": "{{ name }}"}, outputs={"out": "json"}))


def test_v033_presign_lifetime() -> None:
    doc = one({"op": "presign", "key": "k", "expires_in": 999_999}, outputs={"out": "str"})
    assert "V033" in codes(doc)


def test_v004_requires_routing_for_conflict_and_not_found() -> None:
    routes = {"default": "END", "error": "END.failed"}
    put = one({"op": "put", "key": "k", "body": "x"}, outputs={"out": "json"}, routes=routes)
    assert "V004" in codes(put)
    get = one({"op": "get", "key": "k"}, outputs={"out": "str"}, routes=routes)
    assert "V004" in codes(get)
    overwrite = one(
        {"op": "put", "key": "k", "body": "x", "if_exists": "overwrite"},
        outputs={"out": "json"},
        routes=routes,
    )
    assert "V004" not in codes(overwrite)


def test_v007_output_types() -> None:
    assert "V007" in codes(one({"op": "get", "key": "k"}, outputs={"out": "int"}))
    assert "V007" in codes(one({"op": "put", "key": "k", "body": "x"}, outputs={"out": "str"}))
    assert "V007" in codes(one({"op": "delete", "key": "k"}, outputs={"out": "str"}))
    assert "V007" in codes(one({"op": "get", "key": "k"}, outputs={"a": "str", "b": "str"}))
    two = one(
        {"op": "get", "key": "k", "map": {"a": "content", "b": "blob.size"}},
        outputs={"a": "str", "b": "int"},
    )
    assert "V007" not in codes(two)


def test_effects_are_derived() -> None:
    from tuvl_insight.validate.workflow import derived_effect

    def effect(tool: dict[str, Any]) -> str | None:
        return derived_effect(
            one({"op": "head", "key": "k", **tool}, outputs={"out": "json"}).spec.agents[0]
        )

    assert effect({}) == "read"
    assert effect({"op": "put", "body": "x"}) == "write"
    assert effect({"op": "delete"}) == "destructive"
    assert effect({"op": "presign", "method": "PUT"}) == "write"
    assert effect({"op": "presign"}) == "read"


# ── runs ────────────────────────────────────────────────────────────────────


async def test_put_then_get_roundtrip(pg_engine: AsyncEngine, stores: Path) -> None:
    doc = two_step(
        {"op": "put", "key": "{{ name }}.txt", "body": "{{ text }}", "content_type": "text/plain"},
        {"op": "get", "key": "{{ name }}.txt"},
    )
    run = await execute(pg_engine, doc, {"name": "n1", "text": "héllo"})
    assert run.status == "completed", run.error
    assert run.output["body"] == "héllo"
    assert (stores / "d" / "n1.txt").read_text() == "héllo"
    again = await execute(pg_engine, doc, {"name": "n1", "text": "different"})
    assert again.end_name == "dup"


async def test_missing_object_routes_not_found(pg_engine: AsyncEngine) -> None:
    doc = storage_wf({"op": "get", "key": "ghost"}, {"x": "str"}, outputs={"out": "str"})
    assert (await execute(pg_engine, doc, {"x": "1"})).end_name == "missing"


async def test_base64_body_and_json_content(pg_engine: AsyncEngine, stores: Path) -> None:
    payload = base64.b64encode(b'{"a": 1}').decode()
    doc = two_step(
        {"op": "put", "key": "j.json", "body": "{{ text }}", "encoding": "base64"},
        {"op": "get", "key": "j.json", "as": "json"},
        out_type="json",
    )
    run = await execute(pg_engine, doc, {"name": "x", "text": payload})
    assert run.output["body"] == {"a": 1}
    bad = await execute(pg_engine, doc, {"name": "x", "text": "!!notbase64!!"})
    assert bad.end_name == "failed"


async def test_list_and_delete_and_presign_unsupported_locally(pg_engine: AsyncEngine) -> None:
    put = storage_wf(
        {"op": "put", "key": "l/{{ n }}", "body": "x"}, {"n": "str"}, outputs={"out": "json"}
    )
    for n in ("1", "2"):
        await execute(pg_engine, put, {"n": n})
    lst = storage_wf({"op": "list", "prefix": "l/"}, {"p": "str"}, outputs={"out": "list[json]"})
    run = await execute(pg_engine, lst, {"p": "x"})
    assert [b["key"] for b in run.output["body"]] == ["l/1", "l/2"]
    gone = storage_wf({"op": "delete", "key": "l/1"}, {"p": "str"})
    assert (await execute(pg_engine, gone, {"p": "x"})).status == "completed"
    assert len((await execute(pg_engine, lst, {"p": "x"})).output["body"]) == 1
    sign = storage_wf({"op": "presign", "key": "l/2"}, {"p": "str"}, outputs={"out": "str"})
    assert (await execute(pg_engine, sign, {"p": "x"})).end_name == "failed"


async def test_unsafe_rendered_key_fails_the_step(pg_engine: AsyncEngine) -> None:
    doc = storage_wf({"op": "head", "key": "{{ k }}"}, {"k": "str"}, outputs={"out": "json"})
    assert (await execute(pg_engine, doc, {"k": "../etc/passwd"})).end_name == "failed"


async def test_body_and_content_never_reach_the_journal(pg_engine: AsyncEngine) -> None:
    doc = two_step(
        {"op": "put", "key": "s.txt", "body": "{{ text }}"},
        {"op": "get", "key": "s.txt"},
    )
    run = await execute(pg_engine, doc, {"name": "x", "text": "TOP-SECRET-PAYLOAD"})
    async with AsyncSession(pg_engine) as s:
        events = await journal.events(s, run.id)
    assert "TOP-SECRET-PAYLOAD" not in repr([e.payload for e in events])
    assert any(e.type == "tool_result" and e.payload.get("sha256") for e in events)
