"""Graph, data-flow and cross-reference validation of a 2.0 workflow
(spec Appendix B). The schema guarantees shape; this module proves that the
graph is closed, every signal has a route, every input is produced on every
path, types line up, and every expression type-checks against its inputs.

Rules implemented here: V002–V012, V014 (tool resolution), V016–V019, V022.
"""

from __future__ import annotations

from tuvl.contract.facts import (
    artifact_refs,
    derived_effect,
    determinism_of,
    document_artifact_refs,
)
from tuvl.contract.schema import WorkflowDocument
from tuvl_insight.validate.catalog import Catalog
from tuvl_insight.validate.diagnostics import Diagnostic, line_for, line_index
from tuvl_insight.validate.workflow.engines import _Engines
from tuvl_insight.validate.workflow.expressions import _Expressions
from tuvl_insight.validate.workflow.flow import _DataFlow
from tuvl_insight.validate.workflow.policy import _Policy
from tuvl_insight.validate.workflow.profile import _Profiling
from tuvl_insight.validate.workflow.report import Profile, WorkflowReport, render_path
from tuvl_insight.validate.workflow.topology import _Topology

__all__ = [
    "Profile",
    "WorkflowReport",
    "artifact_refs",
    "derived_effect",
    "determinism_of",
    "document_artifact_refs",
    "render_path",
    "validate_workflow",
]


class _Validator(_Topology, _Engines, _Expressions, _Policy, _DataFlow, _Profiling):
    # ── driver ───────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.check_ids()
        self.collect_off_spine()
        self.check_entry()
        self.check_trigger()
        self.check_models()
        self.check_transaction()
        for agent in self.spec.agents:
            if self.by_id.get(agent.id) is not agent:
                continue
            self.check_routes(agent)
            self.check_budget(agent)
            self.check_engine(agent)
            self.check_expressions(agent)
            self.check_artifacts(agent)
            self.check_types_resolve(agent)
        self.check_workflow_artifacts()
        self.analyse_flow()
        self.check_flow()
        self.check_cycles()


def validate_workflow(
    doc: WorkflowDocument,
    catalog: Catalog,
    *,
    text: str | None = None,
) -> WorkflowReport:
    """Run every 2.0 rule over *doc*. *text* (the YAML source) adds line numbers."""
    v = _Validator(doc, catalog)
    v.run()
    profile = v.profile()
    v.check_policy(profile)
    raw = doc.model_dump(mode="json")
    lines = line_index(text) if text else {}
    diagnostics = [
        Diagnostic(
            d.code, d.severity, d.loc, d.message, render_path(d.loc, raw), line_for(lines, d.loc)
        )
        for d in v.out.items
    ]
    diagnostics.sort(key=lambda d: (d.line or 0, d.code))
    return WorkflowReport(doc.metadata.name, diagnostics, profile)
