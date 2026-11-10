"""The workflow report: profile, diagnostics and path rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tuvl_insight.validate.diagnostics import Diagnostic, Loc


@dataclass(frozen=True)
class Profile:
    agents: int
    off_spine: int
    deterministic: int
    bounded: int
    external: int
    pending: int
    #: Worst-case LLM tokens for one run; ``None`` when a token-spending cycle
    #: makes it unbounded.
    worst_case_tokens: int | None
    has_judges: bool

    def summary(self) -> str:
        parts = [
            f"{self.agents} agents" + (f" ({self.off_spine} off-spine)" if self.off_spine else "")
        ]
        for count, label in (
            (self.deterministic, "deterministic"),
            (self.bounded, "bounded"),
            (self.external, "external"),
            (self.pending, "pending"),
        ):
            if count:
                parts.append(f"{count} {label}")
        if self.worst_case_tokens is None:
            cost = "worst-case unbounded (token-spending cycle)"
        else:
            cost = f"worst-case {_tokens(self.worst_case_tokens)} tok"
        parts.append(cost + (" + judges" if self.has_judges else ""))
        return " · ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "off_spine": self.off_spine,
            "deterministic": self.deterministic,
            "bounded": self.bounded,
            "external": self.external,
            "pending": self.pending,
            "worst_case_tokens": self.worst_case_tokens,
            "has_judges": self.has_judges,
        }


def _tokens(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


@dataclass
class WorkflowReport:
    name: str
    diagnostics: list[Diagnostic]
    profile: Profile

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]


def render_path(loc: Loc, data: Any) -> str:
    """``("spec", "agents", 2, "routes")`` → ``spec.agents[approval].routes``."""
    out = ""
    node = data
    for key in loc:
        if isinstance(key, int):
            item = node[key] if isinstance(node, list) and 0 <= key < len(node) else None
            label = (
                item.get("id")
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                else key
            )
            out += f"[{label}]"
            node = item
        else:
            out += ("." if out else "") + key
            node = node.get(key) if isinstance(node, dict) else None
    return out
