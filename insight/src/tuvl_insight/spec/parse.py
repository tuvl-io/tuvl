"""Spec files (spec §17.2): front matter, section anchors + hashes, and the
deterministic extraction of ``tuvl-example`` blocks into ``Test`` documents.

No LLM is involved here. A section runs from its ``#``/``##`` heading to the
next heading at the same or a higher level, so a ``#`` section's hash moves
when anything under it changes. Headings inside fenced code are ignored.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from tuvl.contract.schema import TestDocument

_FRONT = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
_HEADING = re.compile(r"^(#{1,2})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^[ \t]*(```+|~~~+)[ \t]*([\w-]*)")
_EXAMPLE_LANG = "tuvl-example"
_HASH_LEN = 12


class SpecError(ValueError):
    """A spec file that cannot be read as a tuvl spec."""


@dataclass(frozen=True)
class Section:
    anchor: str
    title: str
    level: int
    line: int
    hash: str
    text: str


@dataclass(frozen=True)
class Example:
    name: str
    anchor: str | None
    line: int
    data: dict[str, Any]


@dataclass
class Spec:
    path: str
    name: str
    front: dict[str, Any]
    hash: str
    sections: list[Section] = field(default_factory=list)
    examples: list[Example] = field(default_factory=list)

    def section(self, anchor: str) -> Section | None:
        return next((s for s in self.sections if s.anchor == anchor), None)


def content_hash(text: str) -> str:
    """Hash of *text* that ignores trailing spaces and blank-line runs."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    normal = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return hashlib.sha256(normal.encode()).hexdigest()[:_HASH_LEN]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def parse_spec(path: Path, *, root: Path | None = None) -> Spec:
    shown = str(path.relative_to(root)) if root and path.is_relative_to(root) else str(path)
    return parse_text(path.read_text(), path=shown, default_name=path.stem)


def parse_text(text: str, *, path: str = "<spec>", default_name: str = "spec") -> Spec:
    front: dict[str, Any] = {}
    body, offset = text, 0
    match = _FRONT.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise SpecError(f"{path}: front matter is not YAML: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SpecError(f"{path}: front matter must be a mapping")
        front = loaded
        body, offset = text[match.end() :], text[: match.end()].count("\n")
    spec = Spec(
        path=path, name=str(front.get("name") or default_name), front=front, hash=content_hash(text)
    )
    lines = body.splitlines()
    headings: list[tuple[int, int, str]] = []  # (index, level, title)
    examples: list[tuple[int, list[str]]] = []
    fence: str | None = None
    block: list[str] | None = None
    start = 0
    for i, line in enumerate(lines):
        opened = _FENCE.match(line)
        if fence is None:
            if opened:
                fence = opened.group(1)
                if opened.group(2) == _EXAMPLE_LANG:
                    block, start = [], i
                continue
            heading = _HEADING.match(line)
            if heading:
                headings.append((i, len(heading.group(1)), heading.group(2).strip()))
        elif opened and opened.group(1).startswith(fence) and not opened.group(2):
            fence = None
            if block is not None:
                examples.append((start, block))
                block = None
        elif block is not None:
            block.append(line)
    if fence is not None:
        raise SpecError(f"{path}:{offset + start + 1}: unclosed code fence")

    seen: dict[str, int] = {}
    for n, (index, level, title) in enumerate(headings):
        end = next((h[0] for h in headings[n + 1 :] if h[1] <= level), len(lines))
        anchor = slug(title)
        seen[anchor] = seen.get(anchor, 0) + 1
        if seen[anchor] > 1:
            anchor = f"{anchor}-{seen[anchor]}"
        content = "\n".join(lines[index:end])
        spec.sections.append(
            Section(anchor, title, level, offset + index + 1, content_hash(content), content)
        )

    for index, block_lines in examples:
        line_no = offset + index + 1
        try:
            data = yaml.safe_load("\n".join(block_lines)) or {}
        except yaml.YAMLError as exc:
            raise SpecError(f"{path}:{line_no}: tuvl-example is not YAML: {exc}") from exc
        if not isinstance(data, dict) or not str(data.get("name") or "").strip():
            raise SpecError(f"{path}:{line_no}: a tuvl-example needs a mapping with a 'name'")
        owner = [h for h in headings if h[0] < index]
        under = _anchor_at(spec, offset + owner[-1][0] + 1) if owner else None
        spec.examples.append(Example(str(data["name"]), under, line_no, data))
    return spec


def _anchor_at(spec: Spec, line: int) -> str | None:
    return next((s.anchor for s in spec.sections if s.line == line), None)


_EXPECT_KEYS = {
    "end",
    "path",
    "path_includes",
    "decisions",
    "output",
    "assertions",
    "judge",
    "wait",
}


def example_to_test(spec: Spec, example: Example) -> TestDocument:
    """Render one ``tuvl-example`` as a ``Test`` document (deterministic)."""
    data = example.data
    where = f"{spec.path}:{example.line} ({example.name!r})"
    workflow = data.get("workflow") or spec.front.get("workflow")
    if not workflow:
        raise SpecError(
            f"{where}: names no workflow — add 'workflow:' to the example or the front matter"
        )
    unknown = set(data) - {"name", "workflow", "given", "as", "input", "mocks", "replay", "expect"}
    if unknown:
        raise SpecError(f"{where}: unknown keys {sorted(unknown)}")
    expect = dict(data.get("expect") or {})
    if "decision" in expect:
        expect["decisions"] = expect.pop("decision")
    if "end_or_wait" in expect:  # the spec's shorthand: a person, or a named end
        target = expect.pop("end_or_wait")
        expect["wait" if target in ("human", "approval") else "end"] = target
    unknown = set(expect) - _EXPECT_KEYS
    if unknown:
        raise SpecError(f"{where}: unknown expect keys {sorted(unknown)}")
    document = {
        "kind": "Test",
        "version": "tuvl/v2",
        "metadata": {
            "name": slug(example.name),
            "generated_from": f"{spec.path}#{example.anchor}" if example.anchor else spec.path,
        },
        "spec": {
            "workflow": workflow,
            **({"given": data["given"]} if data.get("given") else {}),
            **({"as": data["as"]} if data.get("as") else {}),
            "input": data.get("input") or {},
            "mocks": data.get("mocks") or {},
            **({"replay": data["replay"]} if data.get("replay") else {}),
            "expect": expect,
        },
    }
    try:
        return TestDocument.model_validate(document)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise SpecError(f"{where}: {problems}") from exc


def load_specs(project: Path) -> list[Spec]:
    """Every ``specs/*.md`` in *project*, sorted by path."""
    directory = project / "specs"
    if not directory.is_dir():
        return []
    return [parse_spec(p, root=project) for p in sorted(directory.glob("*.md"))]
