"""Hierarchical ids: ``IDEA-SK-0004`` → ``SPEC-SK-0004-01`` → ``TASK-SK-0004-01-003``.

Only the idea id is allocated globally; a spec and a task inherit the number of their parent, so
two developers cannot collide below the idea unless they extend the same idea on separate branches.
Widths are minimums: ``0004`` becomes ``10000`` rather than being cut.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

IDEA_DIR = "idea"
SPECS_DIR = "specs"

_INI = r"[A-Z][A-Z0-9]{1,3}"
IDEA_ID = re.compile(rf"^IDEA-({_INI})-(\d{{4,}})$")
SPEC_ID = re.compile(rf"^SPEC-({_INI})-(\d{{4,}})-(\d{{2,}})$")
TASK_ID = re.compile(rf"^TASK-({_INI})-(\d{{4,}})-(\d{{2,}})-(\d{{3,}})$")
#: any id of the three kinds, inside text
TOKEN = re.compile(
    rf"\b(?:IDEA-{_INI}-\d{{4,}}|SPEC-{_INI}-\d{{4,}}-\d{{2,}}|TASK-{_INI}-\d{{4,}}-\d{{2,}}-\d{{3,}})(?![\w-])"
)
TASK_ID_PATTERN = rf"^(T\d+|TASK-{_INI}-\d{{4,}}-\d{{2,}}-\d{{3,}})$"


class IdError(ValueError):
    pass


def idea_id(initials: str, number: int) -> str:
    return f"IDEA-{initials}-{number:04d}"


def spec_id(idea: str, sequence: int) -> str:
    m = IDEA_ID.match(idea)
    if not m:
        raise IdError(f"'{idea}' is not an idea id")
    return f"SPEC-{m.group(1)}-{m.group(2)}-{sequence:02d}"


def task_id(spec: str, sequence: int) -> str:
    m = SPEC_ID.match(spec)
    if not m:
        raise IdError(f"'{spec}' is not a spec id")
    return f"TASK-{m.group(1)}-{m.group(2)}-{m.group(3)}-{sequence:03d}"


def idea_of(any_id: str) -> str:
    """The idea a spec or task id derives from."""
    for pattern in (SPEC_ID, TASK_ID, IDEA_ID):
        m = pattern.match(any_id)
        if m:
            return f"IDEA-{m.group(1)}-{m.group(2)}"
    raise IdError(f"'{any_id}' is not an idea, spec or task id")


def spec_of(task: str) -> str:
    m = TASK_ID.match(task)
    if not m:
        raise IdError(f"'{task}' is not a task id")
    return f"SPEC-{m.group(1)}-{m.group(2)}-{m.group(3)}"


# ── what exists in a project ─────────────────────────────────────────────────


def idea_dir(project: Path) -> Path:
    return project / IDEA_DIR


def index_path(project: Path) -> Path:
    return idea_dir(project) / "INDEX.yaml"


def read_index(project: Path) -> list[dict]:
    path = index_path(project)
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return []
    ideas = data.get("ideas") if isinstance(data, dict) else None
    return [i for i in ideas if isinstance(i, dict)] if isinstance(ideas, list) else []


def write_index(project: Path, ideas: list[dict]) -> None:
    path = index_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"kind": "IdeaIndex", "version": "tuvl/v2", "ideas": ideas}
    path.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True))


def idea_ids(project: Path) -> set[str]:
    """Idea ids on disk or in the index (an id leaves the index only by being renumbered)."""
    found = {str(i.get("id", "")) for i in read_index(project)}
    root = idea_dir(project)
    if root.is_dir():
        found |= {p.name for p in root.iterdir() if p.is_dir() and IDEA_ID.match(p.name)}
    return {i for i in found if IDEA_ID.match(i)}


def spec_ids(project: Path) -> dict[str, list[Path]]:
    """Spec ids from front matter (``id:``) and file names, with the files that carry them."""
    out: dict[str, list[Path]] = defaultdict(list)
    specs = project / SPECS_DIR
    if not specs.is_dir():
        return out
    for path in sorted(specs.glob("*.md")):
        found = _front_id(path)
        if found and SPEC_ID.match(found):
            out[found].append(path)
        if SPEC_ID.match(path.stem) and path.stem != found:
            out[path.stem].append(path)
    return out


def _front_id(path: Path) -> str:
    text = path.read_text()
    if not text.startswith("---"):
        return ""
    head = text.split("\n---", 1)[0]
    try:
        data = yaml.safe_load(head.removeprefix("---")) or {}
    except yaml.YAMLError:
        return ""
    return str(data.get("id", "")) if isinstance(data, dict) else ""


def next_idea_number(project: Path, initials: str) -> int:
    numbers = [
        int(m.group(2))
        for i in idea_ids(project)
        if (m := IDEA_ID.match(i)) and m.group(1) == initials
    ]
    return max(numbers, default=0) + 1


def next_idea_id(project: Path, initials: str) -> str:
    return idea_id(initials, next_idea_number(project, initials))


def next_spec_id(project: Path, idea: str) -> str:
    m = IDEA_ID.match(idea)
    if not m:
        raise IdError(f"'{idea}' is not an idea id")
    taken = [
        int(s.group(3))
        for found in spec_ids(project)
        if (s := SPEC_ID.match(found)) and s.group(1) == m.group(1) and s.group(2) == m.group(2)
    ]
    return spec_id(idea, max(taken, default=0) + 1)


@dataclass(frozen=True)
class Duplicate:
    id: str
    where: tuple[str, ...]


def duplicates(project: Path) -> list[Duplicate]:
    """Ids that more than one thing claims: two idea folders/index rows, two spec files."""
    out: list[Duplicate] = []
    rows: dict[str, int] = defaultdict(int)
    for row in read_index(project):
        rows[str(row.get("id", ""))] += 1
    out += [Duplicate(i, ("idea/INDEX.yaml",) * n) for i, n in rows.items() if n > 1 and i]
    for found, paths in spec_ids(project).items():
        if len(set(paths)) > 1:
            out.append(
                Duplicate(found, tuple(str(p.relative_to(project)) for p in sorted(set(paths))))
            )
    return out
