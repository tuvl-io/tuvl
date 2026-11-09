"""A workflow's typed public surface (spec §15): the input schema, each end's
body schema and the strongest effect. One source for OpenAPI, MCP export and
the generated clients.

An end's body type is ``outputs.<end>.type`` when the author declares it;
otherwise it is inferred from the body template — ``"{{ x }}"`` takes the
declared type of ``x`` (trigger input or an agent output, following model
fields for ``x.field``), literals take their JSON type, and anything else is
left open.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import TypeAdapter

from tuvl.contract.facts import field_types
from tuvl.contract.schema import (
    AgentToolRef,
    CodeAgent,
    CollectionToolRef,
    LoopAgent,
    MapAgent,
    ToolAgent,
    WorkflowDocument,
)
from tuvl.contract.types import (
    ModelResolver,
    TypeRef,
    TypeResolutionError,
    annotation,
    build_model,
    registry_resolver,
)

_REF = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}$")
_EFFECT_RANK = {"read": 0, "write": 1, "destructive": 2}


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace ``#/$defs/…`` references with their definitions, so the schema
    can be embedded anywhere (OpenAPI operations, MCP tool lists)."""
    defs = schema.get("$defs", {})

    def walk(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name in seen or name not in defs:
                    return {}  # recursive model: leave this level open
                merged = {
                    **copy.deepcopy(defs[name]),
                    **{k: v for k, v in node.items() if k != "$ref"},
                }
                return walk(merged, seen | {name})
            return {k: walk(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v, seen) for v in node]
        return node

    result: dict[str, Any] = walk(schema, frozenset())
    return result


def input_schema(doc: WorkflowDocument, resolver: ModelResolver | None = None) -> dict[str, Any]:
    model = build_model(
        f"{doc.metadata.name}_input",
        field_types(doc.spec.trigger.input),
        resolver or registry_resolver,
    )
    schema = inline_refs(model.model_json_schema())
    schema.pop("title", None)
    return schema


def context_types(doc: WorkflowDocument) -> dict[str, TypeRef]:
    """Every context key's declared type: trigger input, then agent outputs."""
    types = dict(field_types(doc.spec.trigger.input))
    for agent in doc.spec.agents:
        fields: dict[str, Any] = dict(agent.outputs)
        for extra in (agent.signal_outputs or {}).values():
            fields.update(extra)
        for key, ref in field_types(fields).items():
            types.setdefault(key, ref)
    return types


def _type_schema(ann: Any) -> dict[str, Any]:
    schema = inline_refs(TypeAdapter(ann).json_schema())
    schema.pop("title", None)
    return schema


def _path_schema(
    path: list[str], types: dict[str, TypeRef], resolver: ModelResolver
) -> dict[str, Any]:
    ref = types.get(path[0])
    if ref is None:
        return {}
    try:
        ann: Any = annotation(ref, resolver)
    except TypeResolutionError:
        return {}
    for segment in path[1:]:
        fields = getattr(ann, "model_fields", None)
        if not fields or segment not in fields:
            return {}
        ann = fields[segment].annotation
    return _type_schema(ann)


def _value_schema(value: Any, types: dict[str, TypeRef], resolver: ModelResolver) -> dict[str, Any]:
    if isinstance(value, str):
        match = _REF.match(value)
        if match:
            return _path_schema(match.group(1).split("."), types, resolver)
        return {"type": "string"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: _value_schema(v, types, resolver) for k, v in value.items()},
            "required": list(value),
        }
    return {}


def end_schema(
    doc: WorkflowDocument, end: str, resolver: ModelResolver | None = None
) -> dict[str, Any] | None:
    """The JSON Schema of *end*'s response body, or ``None`` when it has none."""
    resolve = resolver or registry_resolver
    spec = doc.spec.outputs.get(end)
    if spec is None:
        return None
    if spec.type is not None:
        model = build_model(f"{doc.metadata.name}_{end}", field_types(spec.type), resolve)
        schema = inline_refs(model.model_json_schema())
        schema.pop("title", None)
        return schema
    if spec.body is None:
        return None
    return _value_schema(spec.body, context_types(doc), resolve)


def workflow_effect(doc: WorkflowDocument) -> str:
    """The strongest effect any agent of *doc* can have: ``read``, ``write`` or
    ``destructive`` (spec §15.3 annotations). A loop's MCP and child-workflow
    tools count as ``write``: their effect is only known at runtime."""
    from tuvl.contract.facts import derived_effect

    strongest = "read"

    def bump(effect: str | None) -> None:
        nonlocal strongest
        if effect is not None and _EFFECT_RANK[effect] > _EFFECT_RANK[strongest]:
            strongest = effect

    for agent in doc.spec.agents:
        if isinstance(agent, (CodeAgent, MapAgent)):
            bump(agent.effect)
        elif isinstance(agent, ToolAgent):
            bump(agent.effect or derived_effect(agent))
        elif isinstance(agent, LoopAgent) and any(
            not isinstance(tool, AgentToolRef | CollectionToolRef) for tool in agent.loop.tools
        ):
            bump("write")
    return strongest
