"""The validator state shared by every group of checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tuvl.contract.facts import field_types
from tuvl.contract.schema import (
    WorkflowDocument,
)
from tuvl.validate.catalog import Catalog
from tuvl.validate.diagnostics import Collector, Loc


@dataclass
class _Flow:
    """Per-agent data-flow facts (on-spine agents reachable from the entry)."""

    available: dict[str, frozenset[str]] = field(default_factory=dict)
    producers: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)
    preds: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    end_available: dict[str, frozenset[str]] = field(default_factory=dict)
    end_producers: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)


class _Base:
    def __init__(self, doc: WorkflowDocument, catalog: Catalog) -> None:
        self.doc = doc
        self.spec = doc.spec
        self.cat = catalog
        self.out = Collector()
        self.pos: dict[str, int] = {}
        self.by_id: dict[str, Any] = {}
        self.off_spine: set[str] = set()
        self.flow = _Flow()
        self.trigger_types = field_types(self.spec.trigger.input)

    if TYPE_CHECKING:
        # Defined by sibling groups; declared here so the groups can call each other.
        def check_collection(self, name: Any, loc: Loc) -> None: ...
        def spine_edges(self) -> dict[str, list[str]]: ...

    def loc(self, agent_id: str, *rest: str | int) -> Loc:
        return ("spec", "agents", self.pos[agent_id], *rest)

    def is_spine(self, agent_id: str) -> bool:
        return agent_id in self.by_id and agent_id not in self.off_spine
