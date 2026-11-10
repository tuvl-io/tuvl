"""What the terminal UI shows: the project as a tree of ideas, specs and tasks, read from files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tuvl_insight.audit import live
from tuvl_insight.spec import ids
from tuvl_insight.spec.views import ideas_view, lifecycle_view

_STATUS = {
    "todo": "·",
    "in_progress": "◐",
    "implemented": "◑",
    "tested": "●",
    "done": "✓",
    "untracked": "?",
}


@dataclass
class Node:
    id: str
    kind: str  # project | group | idea | spec | task
    label: str
    data: dict[str, Any] = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)

    def walk(self) -> list[Node]:
        out = [self]
        for c in self.children:
            out += c.walk()
        return out


def spec_label(spec: dict[str, Any]) -> str:
    if not spec["managed"]:
        state = "legacy"
    elif spec["stale"]:
        state = "approved, changed since"
    else:
        state = spec["status"]
    return f"{spec['id'] or spec['name']}  [{state}]"


def _short(task_id: str) -> str:
    """``TASK-SK-0004-01-003`` as ``#003``; a legacy ``T3`` stays."""
    return f"#{task_id.rsplit('-', 1)[-1]}" if task_id.startswith("TASK-") else task_id


def task_label(task: dict[str, Any], derived: str | None, live_entry: dict[str, Any] | None) -> str:
    if live_entry:
        now = live.describe(live_entry)
        return f"▶ {_short(str(task['id']))} {task.get('title', '')}  — {now}"
    mark = _STATUS.get(derived or "", "·")
    return f"{mark} {_short(str(task['id']))} {task.get('title', '')}" + (
        f"  [{derived}]" if derived and derived != "todo" else ""
    )


def _plan_tasks(project: Path, spec_name: str) -> list[dict[str, Any]]:
    path = project / ids.SPECS_DIR / spec_name.replace(".md", ".tasks.yaml")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    return [
        t
        for t in (data.get("spec") or {}).get("tasks") or []
        if isinstance(t, dict) and not t.get("obsolete")
    ]


def build_tree(project: Path, derived: dict[str, str] | None = None) -> Node:
    """Project → ideas → specs → tasks; specs without an idea sit under their own group."""
    derived = derived or {}
    overlay = live.read(project)
    failures = live.read_failures(project)
    specs = lifecycle_view(project)
    root = Node("project", "project", project.name, {"path": str(project)})

    def spec_node(spec: dict[str, Any]) -> Node:
        node = Node(spec["id"] or spec["path"], "spec", spec_label(spec), spec)
        for task in _plan_tasks(project, spec["name"]):
            entry = (overlay.get(spec["path"]) or {}).get(str(task["id"]))
            failure = (failures.get(spec["path"]) or {}).get(str(task["id"]))
            if (
                failure
                and entry is None
                and derived.get(str(task["id"])) not in ("implemented", "tested", "done")
            ):
                entry = {"state": "failed", "finding": str(failure.get("summary", ""))[:120]}
            else:
                failure = failure if entry and entry.get("state") == "failed" else None
            node.children.append(
                Node(
                    f"{node.id}#{task['id']}",
                    "task",
                    task_label(task, derived.get(str(task["id"])), entry),
                    {
                        **task,
                        "spec": spec,
                        "live": entry,
                        "failure": failure,
                        "derived": derived.get(str(task["id"])),
                    },
                )
            )
        return node

    placed: set[str] = set()
    ideas = Node("ideas", "group", "Ideas")
    for idea in ideas_view(project):
        node = Node(idea["id"], "idea", f"{idea['id']}  {idea['title']}", idea)
        for spec in specs:
            if idea["id"] in spec["ideas"]:
                node.children.append(spec_node(spec))
                placed.add(spec["path"])
        ideas.children.append(node)
    root.children.append(ideas)
    loose = [s for s in specs if s["path"] not in placed]
    if loose:
        root.children.append(
            Node("loose", "group", "Specs without an idea", children=[spec_node(s) for s in loose])
        )
    return root


def signature(node: Node) -> str:
    return "|".join(f"{n.id}:{n.label}" for n in node.walk())
