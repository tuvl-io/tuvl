"""A spec can describe a ``map`` step, and ``spec verify`` matches it to the workflow it runs."""

from __future__ import annotations

from tuvl_insight.spec.grammar import parse_structure
from tuvl_insight.spec.verify import _agent_family, _family

SPEC = """---
name: score-all
owner: support-platform
workflow: score_all
---
# Intent
Score every item.

# Flows
## Flow: score
Trigger: POST /score · signed-in
Input: `ids` (list)
Ends: default 200 · failed 500

1. `score_each` — map score_item · in `ids` · out `scores` (list)
   - error → END.failed
"""


def test_a_map_step_parses_and_names_its_workflow() -> None:
    structure = parse_structure(SPEC)
    step = next(iter(structure.flows.values())).steps[0]
    assert step.engine == "map"
    assert not [p for p in structure.problems if "engine" in p.message]
    assert _family(step) == ("map", "score_item")


def test_a_map_step_without_a_workflow_is_a_problem() -> None:
    structure = parse_structure(SPEC.replace("map score_item", "map"))
    assert any("names the workflow" in p.message for p in structure.problems)


def test_the_family_of_a_map_agent_is_its_workflow() -> None:
    class Map:
        workflow = "score_item"

    class Agent:
        engine = "map"
        map = Map()

    assert _agent_family(Agent()) == ("map", "score_item")
