"""A signal the spec waives is not the spec's to constrain: the workflow still has to route it."""

from tuvl.contract.schema import WorkflowDocument
from tuvl_insight.codegen import scaffold
from tuvl_insight.spec.grammar import parse_structure
from tuvl_insight.spec.parse import parse_text
from tuvl_insight.spec.verify import verify_spec

SPEC = """---
name: items
workflow: items
---
# Data
- **Item** (new) — `title` (string; required)

# Flows

## Flow: items
Trigger: POST /items · signed-in
Input: `title` (str)
Ends: default 201 · failed 500

1. `save` — tool db create Item · in `title` · out `item` (Item)
   - conflict: waived — a title may repeat
"""


def verify(routes: dict[str, str], spec_text: str = SPEC):
    spec = parse_text(spec_text, path="specs/items.md", default_name="items")
    structure = parse_structure(spec_text)
    raw = scaffold.workflow_skeleton(spec, structure.flows["items"], structure)
    agent = raw["spec"]["agents"][0]
    agent["engine"] = "tool"
    agent["tool"] = {
        "use": "db",
        "model": "Item",
        "op": "create",
        "values": {"title": "{{ title }}"},
    }
    agent["routes"] = routes
    models = {"Item": scaffold.model_definition(structure.data["Item"], structure)["spec"]}
    return verify_spec(spec, spec_text, {"items": WorkflowDocument.model_validate(raw)}, models)


def test_a_waived_signal_may_be_routed_to_an_end() -> None:
    result = verify({"conflict": "END", "default": "END", "error": "END.failed"})
    assert [f for f in result.errors if f.code == "R004"] == []


def test_a_signal_the_spec_does_not_mention_is_still_caught() -> None:
    result = verify({"conflict": "END", "bogus": "END", "default": "END", "error": "END.failed"})
    assert [f.message for f in result.errors if f.code == "R004"] == [
        "`save`: the workflow routes bogus → END, which the spec doesn't"
    ]


def test_a_routed_signal_must_still_match() -> None:
    routed = SPEC.replace(
        "   - conflict: waived — a title may repeat", "   - conflict → END.failed"
    )
    result = verify({"conflict": "END", "default": "END", "error": "END.failed"}, routed)
    assert any(f.code == "R004" and "conflict" in f.message for f in result.errors)
