"""``tuvl spec check``: the grammar trial's specs pass, and each fixture produces the findings
its front matter ``expect:`` names (proposals/spec-grammar-trial)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tuvl.spec import parse_text
from tuvl.spec.check import check_spec
from tuvl.spec.grammar import parse_structure

HERE = Path(__file__).parent / "grammar"
EXISTING = {
    "Order": {"id", "customer_email", "total", "status"},
    "SupportTicket": {"subject", "body", "category", "urgency", "resolution"},
    "AccountCredit": {"customer_id", "amount", "reason"},
}


def _check(path: Path):
    text = path.read_text()
    return check_spec(
        parse_text(text, path=str(path), default_name=path.stem), text, existing_models=EXISTING
    )


@pytest.mark.parametrize("name", ["refund-request", "support-triage"])
def test_real_specs_have_no_blocking_findings(name: str) -> None:
    result = _check(HERE / f"{name}.md")
    assert not result.errors, [(f.code, f.line, f.message) for f in result.errors]


@pytest.mark.parametrize("path", sorted(HERE.glob("f0*.md")), ids=lambda p: p.stem)
def test_fixture_produces_its_expected_findings(path: Path) -> None:
    result = _check(path)
    expected = set(parse_text(path.read_text()).front["expect"])
    assert expected <= {f.code for f in result.findings}, [
        (f.code, f.message) for f in result.findings
    ]


def test_free_text_acceptance_and_prose_are_reported_as_unchecked() -> None:
    result = _check(HERE / "refund-request.md")
    assert any(f.code == "S014" for f in result.warnings)
    assert {"intent", "policies"} <= set(result.unchecked)


def test_an_unparseable_line_is_never_skipped() -> None:
    text = "# Flows\n## Flow: x\nTrigger: POST /x · public\nEnds: default 200 · failed 500\n1. `a` — code\n   - whatever this is\n"
    result = check_spec(parse_text(text), text)
    assert any(f.code == "S001" and "whatever" in f.message for f in result.errors)


def test_an_unparseable_step_line_says_which_lines_a_step_takes() -> None:
    text = "# Flows\n## Flow: x\nTrigger: POST /x · public\nEnds: default 200 · failed 500\n1. `a` — code\n   - whatever this is\n"
    message = next(f.message for f in check_spec(parse_text(text), text).errors if f.code == "S001")
    assert "note: text" in message and "waived" in message and "otherwise" in message


def test_a_note_under_a_step_is_kept_and_not_checked() -> None:
    text = (
        "# Flows\n## Flow: x\nTrigger: POST /x · public\nInput: `name` (str)\n"
        "Ends: default 200 · failed 500\n\n1. `a` — code · in `name` · out `ok` (bool)\n"
        "   - default → END\n   - note: runs before the save because the id is needed\n"
    )
    structure = parse_structure(text)
    assert not structure.problems
    assert structure.flows["x"].steps[0].notes == ["runs before the save because the id is needed"]
    assert not [f for f in check_spec(parse_text(text), text).errors if f.code == "S001"]


FLOW_INPUT = """# Flows
## Flow: x
Trigger: POST /x · public
{input}
Ends: default 200 · failed 500

1. `a` — code · in `name` · out `ok` (bool)
   - default → END
"""


def test_an_optional_trigger_input_is_kept_as_optional() -> None:
    text = FLOW_INPUT.format(input="Input: `name` (str), `terms`? (str), `due`? (date?)")
    flow = parse_structure(text).flows["x"]
    assert flow.inputs == {"name": "str", "terms": "str?", "due": "date?"}
    assert not parse_structure(text).problems


def test_a_trigger_input_that_does_not_parse_is_reported_not_dropped() -> None:
    text = FLOW_INPUT.format(input="Input: `name` (str), `terms`: str")
    result = check_spec(parse_text(text), text)
    assert any(f.code == "S001" and "`terms`" in f.message for f in result.errors)


LOOP = """---
name: triage
---
# Data
- **Ticket** (new) — `subject` (string), `body` (text)

# Flows
## Flow: triage
Trigger: POST /triage · signed-in
Input: `subject` (str), `body` (str)
Ends: default 200 · gave_up 200 · failed 500

1. `investigate` — loop default · in `subject`, `body` · out `answer` (str) · budget 4000 tok, 60s, 8 iterations · tools: `search_kb`, `lookup`
   - resolved → END
   - max_iterations, budget_exceeded, timeout, aborted → END.gave_up
T1. `search_kb` — tool http GET kb · in `subject` · out `hits` (list[str]) · tool of `investigate`
T2. `orphan` — code · out `x` (str)
   - default → END

# Acceptance
```tuvl-example
name: resolved
input: { subject: a, body: b }
mocks: { investigate: { signal: resolved, outputs: { answer: ok } } }
expect: { end: default }
```
"""


def test_loop_tools_must_be_off_spine_steps_and_every_off_spine_step_is_used() -> None:
    result = check_spec(parse_text(LOOP), LOOP)
    s016 = [f.message for f in result.findings if f.code == "S016"]
    assert any("`lookup` is not a T step" in m for m in s016)
    assert any("`orphan` is an off-spine step nothing uses" in m for m in s016)
    assert not any("search_kb" in m for m in s016)
    assert any(f.code == "S011" and "gave_up" in f.message for f in result.warnings)
    assert {f.code for f in result.errors} == {"S016"}


def test_an_existing_model_resolves_to_the_project_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import create_model

    from tuvl.contract import types

    project_post = create_model("Post", author_id=(str, ...), title=(str, ...))
    monkeypatch.setattr(
        types, "registry_resolver", lambda name, variant: project_post if name == "Post" else None
    )
    monkeypatch.setattr("tuvl.spec.check.registry_resolver", types.registry_resolver)
    text = """# Data
- **Post** (existing)

# Flows
## Flow: fix
Trigger: POST /fix · signed-in
Input: `who` (str)
Ends: default 200 · failed 500

1. `load` — code · effect read · out `post` (Post)
2. `mine` — decide rules · in `post`, `who` · out `ok` (enum[yes, no])
   - `post.author_id == who` → yes
   - otherwise → no
   - yes, no → END
"""
    result = check_spec(parse_text(text), text)
    assert not [f for f in result.errors if f.code == "S015"], result.errors


def test_a_code_step_cannot_route_a_reserved_signal() -> None:
    text = """# Flows
## Flow: x
Trigger: POST /x · signed-in
Ends: default 200 · not_found 404 · failed 500

1. `load` — code · effect read · out `n` (int)
   - not_found → END.not_found
"""
    result = check_spec(parse_text(text), text)
    assert any(f.code == "S004" and "never emits 'not_found'" in f.message for f in result.errors)


def test_compensators_are_t_steps_off_the_routing_graph() -> None:
    text = """# Data
- **Note** (new) — `text` (string)

# Flows
## Flow: note
Trigger: POST /note · signed-in
Input: `text` (str)
Ends: default 201 · failed 500

1. `store` — tool db create Note · in `text` · out `note` (Note) · compensate `discard`
   - conflict → END.failed
2. `confirm` — code · effect read · in `note`

T1. `discard` — code · effect write · in `note`
"""
    result = check_spec(parse_text(text), text)
    assert not result.errors, [(f.code, f.message) for f in result.errors]
    bad = text.replace("T1. `discard`", "3. `discard`")
    result = check_spec(parse_text(bad), bad)
    assert any(f.code == "S016" and "must be a T step" in f.message for f in result.errors)
