"""A workflow's hash must survive an upgrade, or the runs waiting on it fail with ``workflow_changed``.

``workflow_hash`` hashes the whole dump of the document, defaults included, so a field added to a model changes the hash of
every workflow that holds it. A new field with a default is left out of the dump while it holds the default
(``HttpTool.allow_private``, ``LoopParams.tool_choice``). ``hash_fixtures/hashes.json`` holds what tuvl 2.0.3 computed for each
workflow below; a failure here means a change to the document model would fail waiting runs when a deployment upgrades.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tuvl.contract.schema import ApprovalWhen, HttpTool, HumanParams, WorkflowDocument
from tuvl.runtime.runner import workflow_hash

HERE = Path(__file__).parent
FIXTURES = HERE / "hash_fixtures"
WORKFLOWS = sorted(FIXTURES.glob("*.yaml")) + [HERE / "fixtures" / "refund_flow.yaml"]
RECORDED: dict[str, str] = json.loads((FIXTURES / "hashes.json").read_text())["hashes"]


def document(path: Path) -> WorkflowDocument:
    return WorkflowDocument.model_validate(yaml.safe_load(path.read_text()))


def http_tool(**extra: Any) -> HttpTool:
    return HttpTool.model_validate(
        {"use": "http", "method": "GET", "url": "https://example.com/a", **extra}
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_the_hash_is_the_one_a_release_computed(path: Path) -> None:
    assert workflow_hash(document(path)) == RECORDED[path.name], (
        f"{path.name}: the hash changed. A run waiting on this workflow would fail with workflow_changed after an upgrade. "
        "Leave a new field's default out of the dump (see HttpTool.allow_private); only change hashes.json for a "
        "change that is meant to fail those runs, and say so in the changelog."
    )


def test_every_workflow_has_a_recorded_hash_and_every_hash_a_workflow() -> None:
    assert sorted(RECORDED) == sorted(p.name for p in WORKFLOWS)


def test_the_hash_moves_when_the_workflow_does() -> None:
    path = FIXTURES / "scheduled_digest.yaml"
    data = yaml.safe_load(path.read_text())
    data["metadata"]["description"] = "changed"
    assert workflow_hash(WorkflowDocument.model_validate(data)) != RECORDED[path.name]


def test_an_http_tool_leaves_allow_private_out_while_it_is_false() -> None:
    assert "allow_private" not in http_tool().model_dump(mode="json")
    assert "allow_private" not in http_tool(allow_private=False).model_dump(mode="json")


def test_an_http_tool_dumps_allow_private_once_it_is_set() -> None:
    tool = http_tool(allow_private=True)
    dumped = tool.model_dump(mode="json")
    assert dumped["allow_private"] is True
    assert HttpTool.model_validate(dumped).allow_private is True


def test_human_and_approval_when_leave_required_scope_out_while_none() -> None:
    hp = HumanParams(title="Review")
    assert "required_scope" not in hp.model_dump(mode="json")
    hp_scoped = HumanParams(title="Review", required_scope="finance_controllers")
    assert hp_scoped.model_dump(mode="json")["required_scope"] == "finance_controllers"

    aw = ApprovalWhen.model_validate({"when": "args.amount > 10"})
    assert "required_scope" not in aw.model_dump(mode="json")
    aw_scoped = ApprovalWhen.model_validate(
        {"when": "args.amount > 10", "required_scope": "finance_controllers"}
    )
    assert aw_scoped.model_dump(mode="json")["required_scope"] == "finance_controllers"
