"""`spec.webhooks`: what a workflow may declare, and what `tuvl validate` says about it."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tests.insight.validate.test_workflow_rules import found, run
from tuvl.contract.schema import Webhook

GOOD: dict[str, Any] = {
    "name": "ops",
    "url": "${OPS_URL}",
    "secret": "${OPS_SECRET}",
    "events": ["completed"],
}


def hook(**over: Any) -> Webhook:
    return Webhook.model_validate({**GOOD, **over})


def test_a_declared_webhook_takes_its_url_and_secret_from_the_environment() -> None:
    w = hook(url="https://hooks.example.com/x", previous_secret="${OLD}", stages=["load_order"])
    assert w.allow_private is False and w.stages == ["load_order"]


@pytest.mark.parametrize(
    "bad",
    [
        {"secret": "a-literal-secret-that-is-long-enough-12345"},
        {"secret": "${A:default-in-yaml}"},
        {"previous_secret": "literal"},
        {"url": "http://hooks.example.com/x"},
        {"url": "https://127.0.0.1/x"},
        {"url": "https://user:pw@hooks.example.com/x"},
        {"events": []},
        {"events": ["completed", "completed"]},
        {"events": ["finished"]},
        {"name": "Has Space"},
        {"extra": 1},
    ],
)
def test_unsafe_or_malformed_declarations_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        hook(**bad)


def test_allow_private_is_the_only_way_to_an_inside_or_plain_http_target() -> None:
    assert hook(url="http://127.0.0.1:9000/x", allow_private=True).allow_private
    with pytest.raises(ValidationError):
        hook(url="http://127.0.0.1:9000/x")


def test_webhook_names_are_unique_per_workflow() -> None:
    from tuvl.contract.schema import WorkflowSpec

    base = {
        "agents": [
            {"id": "a", "description": "d", "engine": "pending", "routes": {"default": "END"}}
        ]
    }
    with pytest.raises(ValidationError, match="unique"):
        WorkflowSpec.model_validate({**base, "webhooks": [GOOD, GOOD]})


def test_v038_stages_must_name_agents_and_allow_private_warns() -> None:
    def declare(**over: Any):
        def mutate(doc: dict[str, Any]) -> None:
            doc["spec"]["webhooks"] = [{**GOOD, **over}]

        return mutate

    assert not [d for d in run(declare()).diagnostics if d.code == "V038"]
    assert found(run(declare(stages=["nope"])), "V038", "no agent 'nope'")
    assert found(
        run(declare(url="http://127.0.0.1:1/x", allow_private=True)),
        "V038",
        "internal addresses",
        severity="warning",
    )
