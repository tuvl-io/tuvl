"""Derive a spec's task list from its structured sections — no model involved.

One task per new model in ``# Data``, per step of each ``## Flow:``, per ``tuvl-example``, and a
manual task for each step that calls an external system. Re-running keeps every existing task's id,
refreshes its section hash, marks tasks whose step or example left the spec ``obsolete`` and adds the
new ones; manual tasks someone added are left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from tuvl_insight.spec import ids
from tuvl_insight.spec.grammar import Flow, Structure
from tuvl_insight.spec.parse import Spec

DERIVED_TYPES = {"model", "agent", "test", "manual"}


@dataclass
class Derived:
    type: str
    title: str
    target: dict[str, str]
    anchor: str
    deps: list[tuple] = field(default_factory=list)  # keys of the tasks it follows

    @property
    def key(self) -> tuple:
        return (self.type, tuple(sorted(self.target.items())))


def _key(task: dict[str, Any]) -> tuple:
    return (task.get("type"), tuple(sorted((task.get("target") or {}).items())))


def _flow_anchor(spec: Spec, flow: Flow) -> str:
    for section in spec.sections:
        if section.title == f"Flow: {flow.name}":
            return section.anchor
    return "flows"


def _external(step: Any) -> bool:
    return step.engine == "tool" and step.detail[1:2] in (["http"], ["mcp"])


def derive(spec: Spec, structure: Structure) -> list[Derived]:
    out: list[Derived] = []
    models = [
        Derived("model", f"{m.name} model", {"model": m.name}, "data")
        for m in structure.data.values()
        if m.new
    ]
    out += models
    model_keys = [m.key for m in models]
    by_workflow: dict[str, list[Derived]] = {}
    for flow in structure.flows.values():
        anchor = _flow_anchor(spec, flow)
        tasks = {
            step.id: Derived(
                "agent",
                f"{step.id} ({' '.join(step.detail[:2])})",
                {"workflow": flow.name, "agent": step.id},
                anchor,
            )
            for step in flow.steps
        }
        followers: dict[str, list[str]] = {}
        for step in flow.steps:
            for route in step.routes:
                if route.target in tasks and route.target != step.id:
                    followers.setdefault(route.target, []).append(step.id)
        for step in flow.steps:
            task = tasks[step.id]
            before = followers.get(step.id, [])
            task.deps = [tasks[p].key for p in before] if before else list(model_keys)
        out += tasks.values()
        by_workflow[flow.name] = list(tasks.values())
        out += [
            Derived(
                "manual",
                f"{step.id}: credentials and access",
                {"workflow": flow.name, "agent": step.id},
                anchor,
            )
            for step in flow.steps
            if _external(step)
        ]
    only = next(iter(structure.flows), None) if len(structure.flows) == 1 else None
    for example in spec.examples:
        workflow = example.data.get("workflow") or spec.front.get("workflow") or only
        if workflow not in by_workflow:
            continue
        out.append(
            Derived(
                "test",
                f"Example: {example.name}",
                {"workflow": str(workflow), "example": example.name},
                "acceptance",
                [t.key for t in by_workflow[workflow]],
            )
        )
    return out


def _sequence(task_id: str) -> int:
    tail = task_id.rsplit("-", 1)[-1] if task_id.startswith("TASK-") else task_id.lstrip("T")
    return int(tail) if tail.isdigit() else 0


def merge(spec: Spec, structure: Structure, existing: dict[str, Any] | None) -> dict[str, Any]:
    """The task-list document for *spec*, reusing *existing* (a loaded task list) where it matches."""
    derived = derive(spec, structure)
    plan = existing or {
        "kind": "TaskPlan",
        "version": "tuvl/v2",
        "metadata": {},
        "spec": {"tasks": []},
    }
    tasks: list[dict[str, Any]] = [dict(t) for t in (plan.get("spec") or {}).get("tasks") or []]
    present = {_key(t): t for t in tasks}
    spec_id = str(spec.front.get("id") or "")
    prefixed = ids.SPEC_ID.match(spec_id) is not None
    if tasks:
        prefixed = any(str(t["id"]).startswith("TASK-") for t in tasks)
    counter = max((_sequence(str(t["id"])) for t in tasks), default=0)

    def new_id() -> str:
        nonlocal counter
        counter += 1
        return ids.task_id(spec_id, counter) if prefixed else f"T{counter}"

    ids_by_key: dict[tuple, str] = {}
    for d in derived:
        if d.key not in present:
            task: dict[str, Any] = {"id": new_id(), "title": d.title, "type": d.type}
            if d.type == "manual":
                task["status"] = "todo"
            present[d.key] = task
            tasks.append(task)
        ids_by_key[d.key] = str(present[d.key]["id"])
    wanted = {d.key for d in derived}
    for d in derived:
        task = present[d.key]
        section = spec.section(d.anchor)
        if section is not None:
            task["spec_ref"] = {"anchor": d.anchor, "hash": section.hash}
        task["target"] = dict(d.target)
        deps = [ids_by_key[k] for k in d.deps if k in ids_by_key]
        if deps:
            task["depends_on"] = deps
        else:
            task.pop("depends_on", None)
        task.pop("obsolete", None)
    for task in tasks:
        if (
            task.get("type") in DERIVED_TYPES
            and task.get("type") != "manual"
            and _key(task) not in wanted
        ):
            task["obsolete"] = True
    ordered = {"id", "title", "type", "spec_ref", "target", "depends_on", "status", "obsolete"}
    tasks = [
        {
            **{
                k: t[k]
                for k in (
                    "id",
                    "title",
                    "type",
                    "spec_ref",
                    "target",
                    "depends_on",
                    "status",
                    "obsolete",
                )
                if k in t
            },
            **{k: v for k, v in t.items() if k not in ordered},
        }
        for t in tasks
    ]
    return {
        "kind": "TaskPlan",
        "version": "tuvl/v2",
        "metadata": {"name": spec.name, "spec": spec.path, "spec_hash": spec.hash},
        "spec": {"tasks": tasks},
    }


def dump(document: dict[str, Any]) -> str:
    return str(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
