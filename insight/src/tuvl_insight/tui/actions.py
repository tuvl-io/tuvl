"""What can be done to each thing in the tree: always a `tuvl` command, never anything of its own."""

from __future__ import annotations

from dataclasses import dataclass, field

from tuvl_insight.tui.model import Node


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    args: tuple[str, ...]
    #: ask before running; the text says what will happen
    confirm: str = ""
    #: ask for a line of text first; ``{}`` in args is replaced by it
    prompt: str = ""
    #: runs the developer's coding agent: spends their tokens, needs their consent
    agent: bool = False
    extra: tuple[str, ...] = field(default=())
    #: the heading it is listed under
    group: str = ""


def _num(task_id: str) -> str:
    return (
        task_id.rsplit("-", 1)[-1].lstrip("0") or "0"
        if task_id.startswith("TASK-")
        else task_id.lstrip("T")
    )


def node_actions(node: Node) -> list[Action]:
    if node.kind == "idea":
        return [
            Action(
                "gen",
                "Draft the spec with my agent",
                ("generate", "spec", "--idea", node.id),
                confirm="Your coding agent drafts a spec from this idea into specs/. Nothing is committed. It spends tokens of your own account.",
                agent=True,
                extra=("--yes", "--here"),
            ),
            Action(
                "new",
                "Write the spec myself…",
                ("create", "spec", "{}", "--idea", node.id),
                prompt="Spec name (e.g. refund-automation)",
            ),
            Action("idea", "Show the idea", ("show", "idea", node.id)),
        ]
    if node.kind == "spec":
        ref = node.data.get("id") or node.data.get("path") or node.id
        out = [
            Action("check", "Check", ("lint", "spec", ref)),
            Action("verify", "Verify against the project", ("verify", "spec", ref, "--no-tests")),
            Action("status", "Task status", ("list", "tasks", ref, "--no-tests")),
        ]
        if node.data.get("effective") != "approved":
            out.append(
                Action(
                    "approve",
                    "Approve",
                    ("approve", "spec", ref),
                    confirm=f"Approve {ref}: records you, the time and a hash of the body.",
                )
            )
            out.append(
                Action(
                    "reject",
                    "Send back with a reason…",
                    ("reject", "spec", ref, "--reason", "{}"),
                    prompt="Why does it go back to draft?",
                )
            )
        else:
            out.append(
                Action(
                    "reopen",
                    "Reopen (withdraw approval)",
                    ("reopen", "spec", ref),
                    confirm=f"Withdraw the approval of {ref} so it can be edited.",
                )
            )
            out.append(Action("tasks", "Derive the tasks", ("generate", "tasks", ref)))
            out.append(
                Action(
                    "scaffold",
                    "Scaffold models and workflows (no agent)",
                    ("generate", "code", "--spec", ref, "--no-agent", "--no-pr"),
                    confirm="Writes the models, workflow skeletons and generated files on a feature/…-scaffold branch. No agent, no tokens.",
                )
            )
            out.append(
                Action(
                    "build",
                    "Build every task with my agent",
                    ("generate", "code", "--spec", ref, "--all"),
                    confirm="Your coding agent implements each task on its own branch and opens draft pull requests. It spends tokens of your own account.",
                    agent=True,
                    extra=("--no-pr",),
                )
            )
        return out
    if node.kind == "task":
        spec = node.data.get("spec") or {}
        ref = spec.get("id") or spec.get("path") or ""
        return [
            Action(
                "show",
                "Show this task in full",
                ("show", "task", str(node.data.get("id", "")), "--spec", ref),
            ),
            Action(
                "task",
                "Build this task with my agent",
                ("generate", "code", "--spec", ref, "--task", _num(str(node.data.get("id", "")))),
                confirm="Your coding agent implements this task on a feature branch. It spends tokens of your own account.",
                agent=True,
                extra=("--no-pr",),
            ),
        ]
    return []


PROJECT_ACTIONS = [
    Action("lint", "Lint the project", ("lint", "project")),
    Action("test", "Run the tests", ("run", "tests")),
    Action("check", "Check the system", ("check", "system")),
    Action("review", "Review the Python", ("review", "code")),
    Action("flow", "Run a workflow…", ("run", "workflow", "{}"), prompt="Workflow name"),
    Action("ui", "Write the UI specs", ("generate", "ui-spec")),
    Action(
        "import", "Import an idea…", ("import", "idea", "{}"), prompt="File or folder to import"
    ),
    Action("info", "Show the project", ("show", "project")),
    Action("audit", "Show the audit log", ("show", "audit")),
    Action(
        "ship",
        "Ship (archive and image)",
        ("ship", "--format", "both", "--no-image"),
        confirm="Runs the deterministic gate, then writes the archive. Nothing is deployed.",
    ),
]

_HEADINGS = {"idea": "This idea", "spec": "This spec", "task": "This task"}


def actions_for(node: Node) -> list[Action]:
    """What the selected item offers, then what the whole project offers (always)."""
    from dataclasses import replace

    own = [replace(a, group=_HEADINGS.get(node.kind, "")) for a in node_actions(node)]
    return [*own, *(replace(a, group="Project") for a in PROJECT_ACTIONS)]
