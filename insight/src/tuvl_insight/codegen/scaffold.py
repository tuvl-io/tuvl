"""Models and workflow skeletons derived from a spec — no model, no tokens.

``# Data`` fixes a new model's fields, so its ``ModelDefinition`` is written whole. ``# Flows`` fixes
a workflow's trigger, ends and every step's id, inputs, outputs, routes and budget, but not how a step
is implemented, so each step is written as a ``pending`` agent: the contract's own way to say "this
step is specified, not built yet". A task then turns one pending step into a real one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tuvl_insight.spec.check import routes_of
from tuvl_insight.spec.grammar import DataField, DataModel, Flow, Step, Structure
from tuvl_insight.spec.parse import Spec

_COLUMN = {
    "string": "string",
    "text": "text",
    "integer": "integer",
    "numeric": "numeric",
    "boolean": "boolean",
    "uuid": "uuid",
    "date": "date",
    "timestamptz": "timestamptz",
    "jsonb": "jsonb",
    "enum": "enum",
}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def table_name(model: str) -> str:
    word = snake(model)
    if word.endswith("y") and not word.endswith(("ay", "ey", "oy", "uy")):
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def _default(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def model_definition(model: DataModel, structure: Structure) -> dict[str, Any]:
    """The ``ModelDefinition`` document for a ``(new)`` model of the spec."""
    fields: list[dict[str, Any]] = []
    if "id" not in model.fields:
        fields.append(
            {"name": "id", "type": "uuid", "primary_key": True, "default": "uuid4", "input": False}
        )
    for f in model.fields.values():
        fields.append(_field(f, structure))
    if "created_at" not in model.fields:
        fields.append({"name": "created_at", "type": "timestamptz", "input": False})
    return {
        "kind": "ModelDefinition",
        "enabled": True,
        "metadata": {
            "name": model.name,
            "schema_version": "v1",
            "description": f"{model.name}, from the spec's # Data.",
        },
        "spec": {"tablename": table_name(model.name), "fields": fields},
    }


def _field(f: DataField, structure: Structure) -> dict[str, Any]:
    out: dict[str, Any] = {"name": f.name, "type": _COLUMN.get(f.type, f.type)}
    if f.type == "enum":
        out["enum_values"] = list(f.enum)
    if f.ref:
        target = structure.data.get(f.ref)
        out["foreign_key"] = f"{table_name(f.ref)}.id" if target is not None or f.ref else None
    for flag in ("required", "unique", "secure"):
        if flag in f.flags:
            out[flag] = True
    if f.default is not None:
        out["default"] = _default(f.default)
    if f.name == "id" and f.type == "uuid":  # the convention every model follows
        out.update({"primary_key": True, "default": "uuid4", "input": False})
    elif f.name in ("created_at", "updated_at") and f.type == "timestamptz":
        out["input"] = False
    if out.get("foreign_key") is None:
        out.pop("foreign_key", None)
    return out


def dump(document: dict[str, Any]) -> str:
    return str(yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100))


# ── workflows ────────────────────────────────────────────────────────────────

_BUDGET = {"tok": "max_tokens", "iterations": "max_iterations", "calls": "max_tool_calls"}


def _models_in(flow: Flow, structure: Structure) -> list[str]:
    text = " ".join(
        [
            *flow.inputs.values(),
            *(" ".join(s.detail) for s in flow.steps),
            *(t for s in flow.steps for t in s.outputs.values()),
            *(t for s in flow.steps for r in s.routes for t in r.outputs.values()),
        ]
    )
    return [m for m in structure.data if re.search(rf"\b{re.escape(m)}\b", text)]


def _typed(name: str, known: dict[str, str], optional: set[str]) -> str:
    t = known.get(name, "json")
    return t + "?" if name in optional and not t.endswith("?") else t


def workflow_skeleton(spec: Spec, flow: Flow, structure: Structure) -> dict[str, Any]:
    """The ``Workflow`` document of *flow* with every step ``pending``."""
    known = dict(flow.inputs)
    agents: list[dict[str, Any]] = []
    for step in flow.steps:
        agents.append(_pending(step, flow, known))
        known.update(step.outputs)
        for r in step.routes:
            known.update(r.outputs)
    trigger: dict[str, Any] = {}
    if flow.method and flow.path:
        trigger["http"] = {"path": flow.path, "method": flow.method}
        if flow.access == "public":
            trigger["http"]["public"] = True
    if flow.schedule:
        trigger["schedule"] = flow.schedule
    if flow.inputs:
        trigger["input"] = dict(flow.inputs)
    meta: dict[str, Any] = {
        "name": flow.name,
        "description": f"{flow.name}, from {spec.path}.",
    }
    if flow.access and flow.access.startswith("group "):
        meta["required_scope"] = flow.access[6:]
    document: dict[str, Any] = {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": meta,
        "spec": {
            "models": _models_in(flow, structure),
            "trigger": trigger,
            "agents": agents,
            "outputs": {end: {"status": status} for end, status in flow.ends.items()},
        },
    }
    if not document["spec"]["models"]:
        del document["spec"]["models"]
    return document


def _pending(step: Step, flow: Flow, known: dict[str, str]) -> dict[str, Any]:
    agent: dict[str, Any] = {
        "id": step.id,
        "description": f"{step.id}: {' '.join(step.detail[:3])}",
        "engine": "pending",
        "inputs": {name: _typed(name, known, step.optional_inputs) for name in step.inputs},
    }
    per_signal = {sig: dict(r.outputs) for r in step.routes if r.outputs for sig in r.signals}
    if per_signal:
        agent["outputs"] = per_signal
    elif step.outputs:
        agent["outputs"] = dict(step.outputs)
    budget: dict[str, Any] = {
        key: int(step.budget[k]) for k, key in _BUDGET.items() if k in step.budget
    }
    if "s" in step.budget:
        budget["timeout"] = f"{step.budget['s']:g}s"
    if budget:
        agent["budget"] = budget
    agent["routes"] = routes_of(flow, step)
    if step.compensate:
        agent["compensate"] = step.compensate
    return agent


def write_scaffold(project: Path, spec: Spec, structure: Structure) -> list[str]:
    """Create the missing ``models/<name>.yaml`` and ``workflows/<flow>.yaml``; never touch an existing file."""
    created: list[str] = []
    existing = _existing_models(project)
    for model in structure.data.values():
        if model.new and model.name not in existing:
            path = project / "models" / f"{snake(model.name)}.yaml"
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump(model_definition(model, structure)))
            created.append(str(path.relative_to(project)))
    names = _existing_workflows(project)
    for flow in structure.flows.values():
        if flow.name in names:
            continue
        path = project / "workflows" / f"{flow.name}.yaml"
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump(workflow_skeleton(spec, flow, structure)))
        created.append(str(path.relative_to(project)))
    return created


def _documents(directory: Path, kind: str) -> set[str]:
    out: set[str] = set()
    for path in sorted(directory.rglob("*.y*ml")) if directory.is_dir() else []:
        try:
            for doc in yaml.safe_load_all(path.read_text()):
                if isinstance(doc, dict) and doc.get("kind") == kind:
                    name = (doc.get("metadata") or {}).get("name")
                    if name:
                        out.add(str(name))
        except yaml.YAMLError:
            continue
    return out


def _existing_models(project: Path) -> set[str]:
    return _documents(project / "models", "ModelDefinition")


def _existing_workflows(project: Path) -> set[str]:
    return _documents(project / "workflows", "Workflow")
