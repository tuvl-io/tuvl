"""Which signals each engine can emit (spec §8.3, Appendix C), and the type
compatibility rule for data flowing between agents (V007)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tuvl.contract.facts import enum_outputs, field_types
from tuvl.contract.schema import (
    DecideAgent,
    HumanAgent,
    LlmAgent,
    LoopAgent,
    MapAgent,
    ParallelAgent,
    PendingAgent,
    ToolAgent,
)
from tuvl.contract.signals import HTTP_STATUS, is_reserved
from tuvl.contract.types import TypeRef

_HTTP_STATUS = HTTP_STATUS


@dataclass(frozen=True)
class Signals:
    """What an agent can emit.

    ``normal`` signals carry the agent's outputs; ``required`` reserved
    signals must be routed; ``optional`` may be routed (e.g. ``http_404``).
    ``error`` is always emittable and treated separately (unmapped → the run
    fails, a warning rather than an error).
    """

    normal: frozenset[str]
    required: frozenset[str]
    optional: frozenset[str] = frozenset()

    def can_emit(self, signal: str) -> bool:
        return (
            signal == "error"
            or signal in self.normal
            or signal in self.required
            or signal in self.optional
            or (_HTTP_STATUS.fullmatch(signal) is not None and "http_*" in self.optional)
        )


def signals_of(agent: Any) -> Signals:
    declared = frozenset(k for k in agent.routes if not is_reserved(k))
    guarded = (
        frozenset({"guardrail_violation"})
        if getattr(agent, "guardrails", None)
        and any((agent.guardrails.input, agent.guardrails.output, agent.guardrails.tools))
        else frozenset()
    )

    if isinstance(agent, PendingAgent):
        return Signals(normal=declared or frozenset({"default"}), required=frozenset())
    if isinstance(agent, ToolAgent):
        tool = agent.tool
        required: set[str] = set()
        optional: set[str] = set()
        if tool.use == "db":
            if tool.op in ("read", "delete"):
                required.add("not_found")
            elif tool.op in ("create", "update", "upsert"):
                required.add("conflict")
        elif tool.use == "http":
            optional.add("http_*")
        elif tool.use == "storage":
            if tool.op in ("get", "head", "copy"):
                required.add("not_found")
            if tool.op in ("put", "copy") and tool.if_exists == "fail":
                required.add("conflict")
            if tool.op in ("put", "get", "copy"):
                optional.add("too_large")
        return Signals(frozenset({"default"}), frozenset(required), frozenset(optional))
    if isinstance(agent, MapAgent):
        # ``partial`` carries the results too, so it is a normal signal: emitted only when
        # failed items are collected and the workflow routes it.
        partial = agent.map.on_error == "collect" and "partial" in agent.routes
        return Signals(
            frozenset({"default", "partial"}) if partial else frozenset({"default"}), frozenset()
        )
    if isinstance(agent, ParallelAgent):
        partial = agent.parallel.on_error == "collect" and "partial" in agent.routes
        return Signals(
            frozenset({"default", "partial"}) if partial else frozenset({"default"}), frozenset()
        )
    if isinstance(agent, LlmAgent):
        return Signals(
            normal=declared or frozenset({"default"}),
            required=frozenset({"parse_error", "timeout", "budget_exceeded"}) | guarded,
        )
    if isinstance(agent, LoopAgent):
        return Signals(
            normal=declared,
            required=frozenset({"max_iterations", "budget_exceeded", "timeout", "aborted"})
            | guarded,
        )
    if isinstance(agent, DecideAgent):
        enums = enum_outputs(agent)
        values = next(iter(enums.values())).values if len(enums) == 1 else ()
        model_decides = agent.decide.model is not None and not agent.decide.shadow
        return Signals(
            normal=frozenset(values),
            required=frozenset({"low_confidence"}) if model_decides else frozenset(),
        )
    if isinstance(agent, HumanAgent):
        normal: frozenset[str] = frozenset({"default"})
        route_on = agent.human.route_on
        if route_on:
            t = field_types(agent.outputs).get(route_on)
            if t is not None and t.kind == "scalar" and t.name == "bool":
                normal = frozenset({"true", "false"})
            elif t is not None and t.kind == "enum":
                normal = frozenset(t.values)
        return Signals(
            normal=normal,
            required=frozenset({"expired"}) if agent.human.expires else frozenset(),
        )
    # code: its signals are whatever the function returns — the routes declare them.
    return Signals(normal=declared or frozenset({"default"}), required=frozenset())


# ── Type compatibility (V007) ────────────────────────────────────────────────


def _strip(ref: TypeRef) -> TypeRef:
    return TypeRef(ref.kind, ref.name, ref.variant, ref.values, ref.item, False)


def assignable(src: TypeRef, dst: TypeRef) -> str | None:
    """Why a value of type *src* cannot flow into a slot of type *dst*, or None."""
    if src.optional and not dst.optional:
        return "may be null but the input is not optional"
    s, d = _strip(src), _strip(dst)
    if (d.kind == "scalar" and d.name == "json") or (s.kind == "scalar" and s.name == "json"):
        return None
    if s.kind == "scalar" and d.kind == "scalar":
        if s.name == d.name or (s.name == "int" and d.name == "float"):
            return None
        return f"{s} is not {d}"
    if s.kind == "enum" and d.kind == "enum":
        extra = sorted(set(s.values) - set(d.values))
        return f"{s} has values {extra} that {d} does not" if extra else None
    if s.kind == "enum" and d.kind == "scalar" and d.name == "str":
        return None
    if s.kind == "list" and d.kind == "list":
        assert s.item is not None and d.item is not None
        inner = assignable(s.item, d.item)
        return f"list items: {inner}" if inner else None
    if s.kind == "model" and d.kind == "model" and s.name == d.name:
        if s.variant == d.variant or (s.variant == "expanded" and d.variant == "read"):
            return None
        return f"{s} is not {d}"
    return f"{s} is not {d}"
