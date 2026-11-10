"""Spec parsing (P3.2): front matter, anchors, hashes, tuvl-example extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from tuvl_insight.spec import SpecError, example_to_test, load_specs, parse_text

SPEC = """---
name: refund-automation
owner: support-platform
workflow: refund_flow
---
# Intent
Customers request refunds.

# Flows
1. Small refunds are automatic.

## Escalation
Large ones go to a manager.

```python
# not a heading
```

# Acceptance
```tuvl-example
name: small damaged refund is automatic
input:  { order_id: o1, reason: damaged }
expect: { end: default, path_includes: [issue_refund], output: { body: { amount: 40 } } }
```
```tuvl-example
name: large refund needs a manager
input:  { order_id: o2, reason: damaged }
expect: { end_or_wait: approval, decision: { triage_refund: { source: "rule#1" } } }
```
- The reply must state the amount.
"""


def test_front_matter_sections_and_hashes() -> None:
    spec = parse_text(SPEC, path="specs/refund.md")
    assert spec.name == "refund-automation" and spec.front["owner"] == "support-platform"
    assert [(s.anchor, s.level) for s in spec.sections] == [
        ("intent", 1),
        ("flows", 1),
        ("escalation", 2),
        ("acceptance", 1),
    ]
    assert spec.section("flows").line == 9
    assert "Large ones" in spec.section("flows").text  # a # section covers its ## children


def test_hashes_move_only_with_their_section() -> None:
    before = parse_text(SPEC)
    after = parse_text(SPEC.replace("Large ones go", "Big ones go"))
    changed = {s.anchor for s in before.sections if s.hash != after.section(s.anchor).hash}
    assert changed == {"flows", "escalation"} and before.hash != after.hash
    spaced = parse_text(
        SPEC.replace("Customers request refunds.", "Customers request refunds.   \n\n\n")
    )
    assert spaced.section("intent").hash == before.section("intent").hash  # whitespace-only edit


def test_examples_become_tests() -> None:
    spec = parse_text(SPEC, path="specs/refund.md")
    small, large = (example_to_test(spec, e) for e in spec.examples)
    assert small.metadata.name == "small-damaged-refund-is-automatic"
    assert small.metadata.generated_from == "specs/refund.md#acceptance"
    assert small.spec.workflow == "refund_flow" and small.spec.expect.path_includes == [
        "issue_refund"
    ]
    assert large.spec.expect.wait == "approval" and large.spec.expect.end is None
    assert large.spec.expect.decisions["triage_refund"].source == "rule#1"


def test_extraction_is_deterministic() -> None:
    a = [example_to_test(s, e).model_dump() for s in [parse_text(SPEC)] for e in s.examples]
    b = [example_to_test(s, e).model_dump() for s in [parse_text(SPEC)] for e in s.examples]
    assert a == b


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("name: x\ninput: {}", "names no workflow"),
        ("name: x\nworkflow: w\nexpect: { ends: default }", "unknown expect keys ['ends']"),
        ("name: x\nworkflow: w\nexpect: { end: default, wait: human }", "either 'end' or 'wait'"),
        ("input: {}", "needs a mapping with a 'name'"),
        ("name: [x", "is not YAML"),
    ],
)
def test_bad_examples_point_at_the_line(block: str, message: str) -> None:
    text = f"# Acceptance\n\n```tuvl-example\n{block}\n```\n"
    with pytest.raises(SpecError, match="<spec>:3") as info:
        spec = parse_text(text)
        for example in spec.examples:
            example_to_test(spec, example)
    assert message in str(info.value)


def test_unclosed_fence_and_bad_front_matter() -> None:
    with pytest.raises(SpecError, match="unclosed code fence"):
        parse_text("# A\n```tuvl-example\nname: x\n")
    with pytest.raises(SpecError, match="front matter must be a mapping"):
        parse_text("---\n- a\n---\n# A\n")


def test_load_specs(tmp_path: Path) -> None:
    assert load_specs(tmp_path) == []
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "b.md").write_text("# B\n")
    (tmp_path / "specs" / "a.md").write_text(SPEC)
    assert [(s.path, s.name) for s in load_specs(tmp_path)] == [
        ("specs/a.md", "refund-automation"),
        ("specs/b.md", "b"),
    ]
