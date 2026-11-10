"""Give an idea a new number and carry every id derived from it along.

Specs and tasks embed their idea's number (``SPEC-SK-0004-01``), so renumbering ``IDEA-SK-0004``
renames its spec and task-list files and rewrites the ids inside them, the index and any idea that
supersedes it. The whole-spec hash recorded in a task list is refreshed because the front matter moved.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tuvl_insight.spec import ids


class RenumberError(ValueError):
    pass


@dataclass
class Renumbered:
    old: str
    new: str
    changed: list[str] = field(default_factory=list)


_SPEC_HASH = re.compile(r"^(\s*spec_hash:\s*)\S+", re.M)


def _remapper(old: str, new: str) -> Callable[[str], str]:
    old_m, new_m = ids.IDEA_ID.match(old), ids.IDEA_ID.match(new)
    assert old_m and new_m
    old_key = (old_m.group(1), old_m.group(2))
    new_key = (new_m.group(1), new_m.group(2))

    def remap(token: str) -> str:
        for pattern, kind in ((ids.IDEA_ID, "IDEA"), (ids.SPEC_ID, "SPEC"), (ids.TASK_ID, "TASK")):
            m = pattern.match(token)
            if m:
                if (m.group(1), m.group(2)) != old_key:
                    return token
                tail = "".join(f"-{g}" for g in m.groups()[2:])
                return f"{kind}-{new_key[0]}-{new_key[1]}{tail}"
        return token

    return lambda text: ids.TOKEN.sub(lambda m: remap(m.group(0)), text)


@dataclass
class _Op:
    src: Path
    dst: Path
    text: str | None  # None: copy the bytes unchanged


def renumber_idea(project: Path, old: str, new: str | None = None) -> Renumbered:
    if not ids.IDEA_ID.match(old):
        raise RenumberError(f"'{old}' is not an idea id")
    taken = ids.idea_ids(project)
    if old not in taken:
        raise RenumberError(f"{old} is not an idea in this project")
    if new is None:
        new = ids.next_idea_id(project, ids.IDEA_ID.match(old).group(1))  # type: ignore[union-attr]
    elif not ids.IDEA_ID.match(new):
        raise RenumberError(f"'{new}' is not a valid idea id")
    if new in taken:
        raise RenumberError(f"{new} already exists")
    if new == old:
        raise RenumberError("the new id is the old id")
    new_prefix = new.removeprefix("IDEA-")
    for spec in ids.spec_ids(project):
        if spec.removeprefix("SPEC-").startswith(new_prefix + "-"):
            raise RenumberError(f"{spec} already uses the number of {new}")

    remap = _remapper(old, new)
    root = ids.idea_dir(project)
    specs = project / ids.SPECS_DIR
    old_dir, new_dir = root / old, root / new
    ops: list[_Op] = []

    if (root / "INDEX.yaml").is_file():
        index = root / "INDEX.yaml"
        ops.append(_Op(index, index, remap(index.read_text())))
    if root.is_dir():
        for idea_yaml in sorted(root.glob("*/IDEA.yaml")):
            dst = new_dir / "IDEA.yaml" if idea_yaml.parent == old_dir else idea_yaml
            ops.append(_Op(idea_yaml, dst, remap(idea_yaml.read_text())))
    if old_dir.is_dir():
        for file in sorted(p for p in old_dir.rglob("*") if p.is_file() and p.name != "IDEA.yaml"):
            ops.append(_Op(file, new_dir / file.relative_to(old_dir), None))
    spec_text: dict[str, str] = {}
    if specs.is_dir():
        for path in sorted(specs.glob("*.md")) + sorted(specs.glob("*.tasks.yaml")):
            dst = path.with_name(remap(path.name))
            text = remap(path.read_text())
            if path.suffix == ".md":
                spec_text[dst.name] = text
            ops.append(_Op(path, dst, text))

    from tuvl_insight.spec.parse import content_hash

    for op in ops:
        if op.text is not None and op.dst.name.endswith(".tasks.yaml") and op.dst.parent == specs:
            body = spec_text.get(op.dst.name.replace(".tasks.yaml", ".md"))
            if body is not None:
                digest = content_hash(body)
                op.text = _SPEC_HASH.sub(rf"\g<1>{digest}", op.text)

    changed: list[str] = []
    for op in ops:
        original = op.src.read_text() if op.text is not None else None
        if op.dst == op.src and op.text == original:
            continue
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        if op.text is None:
            op.dst.write_bytes(op.src.read_bytes())
        else:
            op.dst.write_text(op.text)
        changed.append(str(op.dst.relative_to(project)))
    for op in ops:
        if op.dst != op.src and op.src.exists():
            op.src.unlink()
    if old_dir.is_dir():
        for sub in sorted(old_dir.rglob("*"), reverse=True):
            sub.rmdir()
        old_dir.rmdir()
    return Renumbered(old, new, sorted(changed))
