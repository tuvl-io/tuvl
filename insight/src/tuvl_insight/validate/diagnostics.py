"""Diagnostics and source locations for the 2.0 validator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

Severity = Literal["error", "warning"]
Loc = tuple[str | int, ...]


@dataclass(frozen=True)
class Diagnostic:
    """One finding, keyed by a spec Appendix B rule code."""

    code: str
    severity: Severity
    loc: Loc
    message: str
    path: str = ""
    line: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "message": self.message,
        }

    def __str__(self) -> str:
        where = f"{self.path}: " if self.path else ""
        return f"[{self.code}] {where}{self.message}"


@dataclass
class Collector:
    """Accumulates diagnostics while a validation pass runs."""

    items: list[Diagnostic] = field(default_factory=list)

    def error(self, code: str, loc: Loc, message: str) -> None:
        self.items.append(Diagnostic(code, "error", loc, message))

    def warn(self, code: str, loc: Loc, message: str) -> None:
        self.items.append(Diagnostic(code, "warning", loc, message))


def line_index(text: str) -> dict[Loc, int]:
    """Map every key/item path in a YAML document to its 1-based line."""
    index: dict[Loc, int] = {}
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return index

    def walk(n: yaml.Node | None, loc: Loc) -> None:
        if n is None:
            return
        index.setdefault(loc, n.start_mark.line + 1)
        if isinstance(n, yaml.MappingNode):
            for key_node, value_node in n.value:
                key = key_node.value
                if key in ("true", "false", "True", "False"):  # YAML 1.1 bool keys
                    key = key.lower()
                child = (*loc, key)
                index[child] = key_node.start_mark.line + 1
                walk(value_node, child)
        elif isinstance(n, yaml.SequenceNode):
            for i, item in enumerate(n.value):
                walk(item, (*loc, i))

    walk(node, ())
    return index


def line_for(index: dict[Loc, int], loc: Loc) -> int | None:
    """Line of the deepest indexed prefix of *loc*."""
    for end in range(len(loc), -1, -1):
        line = index.get(loc[:end])
        if line is not None:
            return line
    return None
