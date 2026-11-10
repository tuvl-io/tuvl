"""Facts about a parsed workflow that the runtime and the lint engine both rely on.

These read the contract only. They live here, not in `validate`, so the engine that runs a workflow does not
load the engine that lints it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from tuvl.contract.schema import (
    DecideAgent,
    HumanAgent,
    LlmAgent,
    LongFormField,
    LoopAgent,
    McpToolRef,
    ParallelAgent,
    PendingAgent,
    ToolAgent,
    WorkflowDocument,
)
from tuvl.contract.types import TypeParseError, TypeRef, parse_field

__all__ = [
    "artifact_refs",
    "derived_effect",
    "determinism_of",
    "document_artifact_refs",
    "enum_outputs",
    "field_types",
]


def field_types(fields: dict[str, Any] | None) -> dict[str, TypeRef]:
    """A schema field map (short or long form) → parsed types. Assumes the
    schema already rejected malformed types."""
    out: dict[str, TypeRef] = {}
    for name, spec in (fields or {}).items():
        raw = spec.model_dump(exclude_unset=True) if isinstance(spec, LongFormField) else spec
        try:
            out[name] = parse_field(raw).type
        except TypeParseError:
            continue
    return out


def enum_outputs(agent: Any) -> dict[str, TypeRef]:
    return {
        k: t for k, t in field_types(agent.outputs).items() if t.kind == "enum" and not t.optional
    }


def artifact_refs(agent: Any) -> Iterator[tuple[tuple[str | int, ...], str, str]]:
    """Every ``artifact://`` reference an agent declares: ``(loc, site, ref)``."""
    for k, ref in enumerate(agent.hooks):
        yield ("hooks", k), "hooks", ref
    guardrails = getattr(agent, "guardrails", None)
    if guardrails is not None:
        for gate in ("input", "output", "tools"):
            for k, ref in enumerate(getattr(guardrails, gate)):
                yield ("guardrails", gate, k), "guardrails", ref
    if isinstance(agent, LlmAgent):
        for key in ("system", "prompt"):
            text = getattr(agent.llm, key)
            if text and text.startswith("artifact://"):
                yield ("llm", key), key, text
        for k, ref in enumerate(agent.llm.skills):
            yield ("llm", "skills", k), "skills", ref
    elif isinstance(agent, LoopAgent):
        if agent.loop.steering.startswith("artifact://"):
            yield ("loop", "steering"), "steering", agent.loop.steering
        for k, ref in enumerate(agent.loop.skills):
            yield ("loop", "skills", k), "skills", ref
        for j, tool in enumerate(agent.loop.tools):
            if isinstance(tool, McpToolRef):
                yield ("loop", "tools", j, "mcp"), "mcp.server", tool.mcp
        if agent.loop.supervisor and agent.loop.supervisor.judge:
            yield ("loop", "supervisor", "judge"), "judge", agent.loop.supervisor.judge
    elif isinstance(agent, DecideAgent) and isinstance(agent.decide.rules, str):
        yield ("decide", "rules"), "decide.rules", agent.decide.rules
    elif isinstance(agent, ToolAgent) and agent.tool.use == "mcp":
        yield ("tool", "server"), "mcp.server", agent.tool.server


def document_artifact_refs(doc: WorkflowDocument) -> Iterator[tuple[str, str, str]]:
    """Every artifact reference in a workflow: ``(where, site, ref)``."""
    name = doc.metadata.name
    for agent in doc.spec.agents:
        for loc, site, ref in artifact_refs(agent):
            yield f"[{name}] agent '{agent.id}' {'.'.join(map(str, loc))}", site, ref
    for k, ref in enumerate(doc.spec.hooks):
        yield f"[{name}] spec.hooks[{k}]", "hooks", ref
    if doc.spec.supervisor and doc.spec.supervisor.judge:
        yield f"[{name}] spec.supervisor.judge", "judge", doc.spec.supervisor.judge


def determinism_of(agent: Any) -> str:
    """``deterministic`` | ``bounded`` (a model decides) | ``external`` (a
    human) | ``pending`` — the agent's determinism class (spec §19)."""
    if isinstance(agent, PendingAgent):
        return "pending"
    if isinstance(agent, HumanAgent):
        return "external"
    if isinstance(agent, ParallelAgent):
        order = ("deterministic", "bounded", "external", "pending")
        return max((determinism_of(b) for b in agent.parallel.branches), key=order.index)
    if isinstance(agent, (LlmAgent, LoopAgent)) or (
        isinstance(agent, DecideAgent)
        and agent.decide.model is not None
        and not agent.decide.shadow
    ):
        return "bounded"
    return "deterministic"


def derived_effect(agent: Any) -> str | None:
    """The effect a ``tool`` agent has by construction (spec §9.3). ``code``
    agents are not checked — their writes commit inside their own step's
    transaction; other engines have no side effects of their own."""
    if not isinstance(agent, ToolAgent):
        return None
    tool = agent.tool
    if tool.use == "db":
        return {"read": "read", "list": "read", "delete": "destructive"}.get(tool.op, "write")
    if tool.use == "http":
        return "read" if tool.method in ("GET", "HEAD") else "write"
    if tool.use == "storage":
        if tool.op == "delete":
            return "destructive"
        if tool.op in ("put", "copy") or (tool.op == "presign" and tool.method == "PUT"):
            return "write"
        return "read"
    return "write"
