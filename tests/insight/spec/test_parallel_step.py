"""A spec can describe a ``parallel`` step."""

from __future__ import annotations

from tuvl_insight.spec.grammar import parse_structure
from tuvl_insight.spec.verify import _family

SPEC = """---
name: gather
owner: support-platform
workflow: gather_all
---
# Intent
Look a customer up in several places at once.

# Flows
## Flow: gather
Trigger: POST /gather · signed-in
Input: `key` (text)
Ends: default 200 · failed 500

1. `gather` — parallel · in `key` · out `crm` (number), `kb` (number)
   - error → END.failed
"""


def test_a_parallel_step_parses() -> None:
    structure = parse_structure(SPEC)
    step = next(iter(structure.flows.values())).steps[0]
    assert step.engine == "parallel"
    assert not [p for p in structure.problems if "engine" in p.message], structure.problems
    assert _family(step) == ("parallel",)
