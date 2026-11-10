"""Coding-agent skills (spec §20.3), shipped with tuvl and installed into a
project by ``tuvl init`` and ``tuvl update skills``.

A project gets ``AGENTS.md``, ``.agents/skills/<name>/SKILL.md`` and, for
Claude Code, a ``.claude/skills/`` mirror plus a ``CLAUDE.md`` that imports
``AGENTS.md``. Updating overwrites the managed files with this tuvl version's
copies and removes the nine 1.x skills; other skills in ``.agents/skills/``
are left alone.
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

#: The 1.x step-kind skills these replace.
LEGACY = frozenset(
    {
        "build-autonomous-agent",
        "create-custom-python-node",
        "create-database-model",
        "execute-mcp-tool",
        "implement-api-endpoint",
        "implement-llm-agent-step",
        "invoke-external-api",
        "perform-database-operations",
        "supervise-autonomous-agent",
    }
)
SKILL_DIRS = (Path(".agents/skills"), Path(".claude/skills"))
CLAUDE_IMPORT = "@AGENTS.md"


@dataclass
class Change:
    path: Path
    action: Literal["create", "update", "delete"]
    diff: str = ""


def bundled() -> dict[Path, str]:
    """``AGENTS.md`` and every skill, as project-relative path → text."""
    root = resources.files(__name__)
    files = {Path("AGENTS.md"): root.joinpath("AGENTS.md").read_text()}
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        skill = entry.joinpath("SKILL.md")
        if entry.is_dir() and skill.is_file():
            for base in SKILL_DIRS:
                files[base / entry.name / "SKILL.md"] = skill.read_text()
    return files


def skill_text(name: str) -> str:
    """One bundled skill's SKILL.md (without its front matter)."""
    text = resources.files(__name__).joinpath(name, "SKILL.md").read_text()
    return text.split("---", 2)[2].lstrip() if text.startswith("---") else text


def names() -> list[str]:
    return sorted({p.parent.name for p in bundled() if p.name == "SKILL.md"})


def _diff(path: Path, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            f"a/{path}" if old else "/dev/null",
            f"b/{path}",
        )
    )


def plan(project: Path) -> list[Change]:
    changes: list[Change] = []
    for path, text in bundled().items():
        target = project / path
        current = target.read_text() if target.is_file() else ""
        if current != text:
            changes.append(
                Change(path, "update" if target.exists() else "create", _diff(path, current, text))
            )
    claude = project / "CLAUDE.md"
    current = claude.read_text() if claude.is_file() else ""
    if CLAUDE_IMPORT not in current:
        new = f"{CLAUDE_IMPORT}\n" + (f"\n{current}" if current else "")
        changes.append(
            Change(
                Path("CLAUDE.md"),
                "update" if current else "create",
                _diff(Path("CLAUDE.md"), current, new),
            )
        )
    for base in SKILL_DIRS:
        for name in sorted(LEGACY):
            if (project / base / name).is_dir():
                changes.append(Change(base / name, "delete"))
    return changes


def apply(project: Path, changes: list[Change]) -> None:
    files = bundled()
    for change in changes:
        target = project / change.path
        if change.action == "delete":
            shutil.rmtree(target, ignore_errors=True)
        elif change.path == Path("CLAUDE.md"):
            current = target.read_text() if target.is_file() else ""
            target.write_text(f"{CLAUDE_IMPORT}\n" + (f"\n{current}" if current else ""))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[change.path])


def install(project: Path) -> list[Change]:
    changes = plan(project)
    apply(project, changes)
    return changes
