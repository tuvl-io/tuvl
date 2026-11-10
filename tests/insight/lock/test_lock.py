"""tuvl.lock (P3.4): inventory, resolution, drift, MCP hashing, the runtime warning."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from tuvl.contract.schema import TestDocument, WorkflowDocument
from tuvl.core.artifacts import registry as artifacts
from tuvl.core.artifacts.registry import ArtifactEntry, ArtifactVersion
from tuvl.core.models.agent_models import AGENT_MODEL_REGISTRY
from tuvl_insight import lock

BUDGET = {"timeout": "5s", "max_tokens": 10}
LLM_ROUTES = {
    "default": "END",
    "error": "END",
    "parse_error": "END",
    "timeout": "END",
    "budget_exceeded": "END",
}

FLOW = WorkflowDocument.model_validate(
    {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "w"},
        "spec": {
            "agents": [
                {
                    "id": "write",
                    "description": "Write.",
                    "engine": "llm",
                    "outputs": {"text": "str"},
                    "budget": BUDGET,
                    "llm": {"model": "writer", "system": "artifact://style", "prompt": "Go."},
                    "guardrails": {"output": ["artifact://tone@1", "artifact://no-pii@1"]},
                    "routes": LLM_ROUTES,
                },
                {
                    "id": "pick",
                    "description": "Pick.",
                    "engine": "decide",
                    "inputs": {"text": "str"},
                    "outputs": {"lane": "enum[a, b]"},
                    "decide": {"model": {"ref": "picker", "min_confidence": 0.5}},
                    "routes": {"a": "dig", "b": "END", "error": "END", "low_confidence": "END"},
                },
                {
                    "id": "dig",
                    "description": "Dig.",
                    "engine": "loop",
                    "outputs": {"answer": "str"},
                    "budget": BUDGET,
                    "loop": {
                        "model": "openai/gpt-x",
                        "steering": "Find it.",
                        "tools": [{"mcp": "artifact://web@1", "allow": ["fetch"]}],
                    },
                    "routes": {**LLM_ROUTES, "max_iterations": "END"},
                },
            ],
            "outputs": {"default": {}},
        },
    }
)
TEST = TestDocument.model_validate(
    {
        "kind": "Test",
        "version": "tuvl/v2",
        "metadata": {"name": "t"},
        "spec": {
            "workflow": "w",
            "expect": {"judge": [{"judge": "artifact://quality@1", "target": "x"}]},
        },
    }
)


def version(content: Any, digest: str) -> ArtifactVersion:
    return ArtifactVersion(content=content, content_hash=digest, source="file")


@pytest.fixture(autouse=True)
def registries(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = {
        "style": ArtifactEntry(
            "style", "prompt", versions={1: version("a", "h1"), 2: version("b", "h2")}
        ),
        "tone": ArtifactEntry(
            "tone", "judge", versions={1: version({"model": "judge-model", "rubric": "r"}, "ht")}
        ),
        "no-pii": ArtifactEntry(
            "no-pii", "guardrail", versions={1: version({"pattern": "x"}, "hp")}
        ),
        "quality": ArtifactEntry(
            "quality", "judge", versions={1: version({"model": "openai/j", "rubric": "r"}, "hq")}
        ),
        "web": ArtifactEntry("web", "mcp", versions={1: version({"command": "x"}, "hw")}),
    }
    for name, entry in entries.items():
        monkeypatch.setitem(artifacts.ARTIFACT_REGISTRY, name, entry)
    monkeypatch.setitem(AGENT_MODEL_REGISTRY, "writer", {"model": "gemini/flash"})
    monkeypatch.setitem(AGENT_MODEL_REGISTRY, "judge-model", {"model": "anthropic/haiku"})
    monkeypatch.setitem(
        AGENT_MODEL_REGISTRY,
        "picker",
        {"type": "decision", "provider": "llm", "model": "gemini/x"},
    )


def test_inventory_and_build() -> None:
    inv = lock.inventory({"w": FLOW}, [TEST])
    assert inv.models == {"writer", "openai/gpt-x"} and inv.decision_models == {"picker"}
    assert inv.mcp == {"artifact://web@1": {"fetch"}}
    doc = lock.build(inv)
    assert doc["models"] == {
        "judge-model": "anthropic/haiku",
        "writer": "gemini/flash",
    }  # direct strings pin themselves
    assert doc["artifacts"]["artifact://style"] == {
        "version": 2,
        "sha256": "h2",
    }  # unversioned → latest, pinned
    assert doc["judges"] == {
        "artifact://quality@1": {"model": "openai/j"},
        "artifact://tone@1": {"model": "anthropic/haiku"},
    }
    # the llm adapter is not the model: its patch version must not make the lock drift
    assert "provider_version" not in doc["decision_models"]["picker"]
    assert doc["decision_models"]["picker"]["provider"] == "llm"
    assert doc["mcp"] == {"artifact://web@1": {"fetch": "unlisted"}}


def test_unknown_artifact_is_a_lock_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(artifacts.ARTIFACT_REGISTRY, "style")
    with pytest.raises(lock.LockError, match="artifact://style"):
        lock.build(lock.inventory({"w": FLOW}))


def test_differences_and_unlisted_mcp() -> None:
    inv = lock.inventory({"w": FLOW})
    pinned = lock.build(inv, {"artifact://web@1": {"fetch": "abc"}})
    assert (
        lock.differences(
            pinned, lock.build(inv, {"artifact://web@1": {"fetch": "abc"}}), live_mcp=True
        )
        == []
    )
    offline = lock.merge_unlisted(lock.build(inv), pinned)
    assert offline["mcp"]["artifact://web@1"]["fetch"] == "abc"  # kept, not reset to "unlisted"
    drifted = lock.build(inv, {"artifact://web@1": {"fetch": "zzz"}})
    assert lock.differences(pinned, drifted, live_mcp=False) == []  # names only offline
    assert lock.differences(pinned, drifted, live_mcp=True) == [
        "[V014] mcp.artifact://web@1.fetch: input schema changed"
    ]
    artifacts.ARTIFACT_REGISTRY["style"].versions[2] = version("c", "h3")
    assert lock.differences(pinned, lock.build(inv), live_mcp=False) == [
        "artifacts.artifact://style: {'version': 2, 'sha256': 'h2'} → {'version': 2, 'sha256': 'h3'}"
    ]


def fake_session(tools: list[Any]) -> Any:
    @contextlib.asynccontextmanager
    async def session(server: str, where: str) -> AsyncIterator[Any]:
        async def list_tools() -> Any:
            return SimpleNamespace(tools=tools)

        yield SimpleNamespace(list_tools=list_tools)

    return session


async def test_mcp_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    from tuvl.runtime import tool

    schema = {"type": "object", "properties": {"url": {"type": "string"}}}
    monkeypatch.setattr(
        tool,
        "mcp_session",
        fake_session(
            [
                SimpleNamespace(name="fetch", inputSchema=schema),
                SimpleNamespace(name="other", inputSchema={}),
            ]
        ),
    )
    inv = lock.inventory({"w": FLOW})
    assert await lock.mcp_hashes(inv) == {"artifact://web@1": {"fetch": lock.schema_hash(schema)}}
    monkeypatch.setattr(tool, "mcp_session", fake_session([]))
    with pytest.raises(lock.LockError, match=r"does not offer \['fetch'\]"):
        await lock.mcp_hashes(inv)


def test_runtime_warns_once_on_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl.contract.lockfile._WARNED", set())
    (tmp_path / "tuvl.lock").write_text(
        lock.render({"mcp": {"artifact://web@1": {"fetch": lock.schema_hash({"a": 1})}}})
    )
    with structlog.testing.capture_logs() as logs:
        lock.check_live_mcp(tmp_path, "artifact://web@1", "fetch", {"a": 1})
        lock.check_live_mcp(tmp_path, "artifact://web@1", "other", {"b": 2})  # not pinned: silent
    assert logs == []
    monkeypatch.setattr("tuvl.contract.lockfile._WARNED", set())
    with structlog.testing.capture_logs() as logs:
        lock.check_live_mcp(tmp_path, "artifact://web@1", "fetch", {"a": 2})
        lock.check_live_mcp(tmp_path, "artifact://web@1", "fetch", {"a": 2})
    assert [entry["event"] for entry in logs] == ["mcp.schema_drift"]
