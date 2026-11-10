"""Guardrails: the closed check set, the three gates (input/output/tools), the
reserved ``guardrail_violation`` exit, and validator coverage."""

from __future__ import annotations

import json

import pytest

from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY, register_artifact
from tuvl.core.engine.guardrails import (
    GuardrailViolationError,
    _check_json_schema,
    run_guardrails,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    ARTIFACT_REGISTRY.clear()
    yield
    ARTIFACT_REGISTRY.clear()


def _guardrail(name: str, checks: list[dict]) -> None:
    register_artifact(
        name=name, type_="guardrail", version=1, content={"checks": checks}, source="file"
    )


# ── deterministic checks ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_chars_and_regex_deny():
    _guardrail("caps", [{"check": "max_chars", "limit": 10}])
    with pytest.raises(GuardrailViolationError, match="max_chars"):
        await run_guardrails(["artifact://caps"], "x" * 11, gate="output", step_id="s")
    assert await run_guardrails(["artifact://caps"], "short", gate="output", step_id="s")

    _guardrail("no-keys", [{"check": "regex_deny", "patterns": ["(?i)api[_-]?key"]}])
    with pytest.raises(GuardrailViolationError, match="regex_deny"):
        await run_guardrails(
            ["artifact://no-keys"], "here is my API_KEY=abc", gate="output", step_id="s"
        )


@pytest.mark.asyncio
async def test_json_schema_check():
    _guardrail(
        "shape",
        [{"check": "json_schema", "schema": {"type": "object", "required": ["brief"]}}],
    )
    with pytest.raises(GuardrailViolationError, match="not valid JSON"):
        await run_guardrails(["artifact://shape"], "plain text", gate="output", step_id="s")
    with pytest.raises(GuardrailViolationError, match="missing required key"):
        await run_guardrails(["artifact://shape"], json.dumps({"x": 1}), gate="output", step_id="s")
    assert await run_guardrails(
        ["artifact://shape"], json.dumps({"brief": "ok"}), gate="output", step_id="s"
    )


def test_json_schema_subset_semantics():
    assert _check_json_schema({"type": "object"}, {}) is None
    assert _check_json_schema({"type": "integer"}, True) is not None  # bool is not int
    assert _check_json_schema({"enum": ["a", "b"]}, "c") is not None
    assert _check_json_schema({"type": "array", "items": {"type": "string"}}, ["x", 1]) is not None
    assert (
        _check_json_schema(
            {"type": "object", "properties": {"n": {"type": "number"}}}, {"n": "NaN"}
        )
        is not None
    )


@pytest.mark.asyncio
async def test_pii_mask_transforms_never_fails(monkeypatch):
    from tuvl.core.models import loader as model_loader

    monkeypatch.setattr(model_loader, "SECURE_FIELDS", {"ssn"})
    _guardrail("mask", [{"check": "pii_mask"}])
    out = await run_guardrails(
        ["artifact://mask"],
        json.dumps({"ssn": "123-45-6789", "name": "ada"}),
        gate="output",
        step_id="s",
    )
    parsed = json.loads(out)
    assert parsed["ssn"] != "123-45-6789"
    assert parsed["name"] == "ada"


@pytest.mark.asyncio
async def test_pii_mask_also_masks_a_json_list(monkeypatch):
    from tuvl.core.models import loader as model_loader

    monkeypatch.setattr(model_loader, "SECURE_FIELDS", {"ssn"})
    _guardrail("mask", [{"check": "pii_mask"}])
    out = await run_guardrails(
        ["artifact://mask"],
        json.dumps([{"ssn": "123-45-6789", "name": "ada"}, [{"ssn": "9"}]]),
        gate="output",
        step_id="s",
    )
    parsed = json.loads(out)
    assert parsed[0]["ssn"] != "123-45-6789" and parsed[0]["name"] == "ada"
    assert parsed[1][0]["ssn"] != "9"


# ── agent integration: the three gates ────────────────────────────────────────


# ── validator coverage ────────────────────────────────────────────────────────


def test_validate_guardrail_artifact_checks(tmp_path):
    import yaml

    from tuvl_insight.cli.commands.validate import ValidateResult, _validate_artifacts

    d = tmp_path / "artifacts"
    d.mkdir()
    (d / "bad.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "Artifact",
                "metadata": {"name": "bad-guard"},
                "spec": {
                    "type": "guardrail",
                    "checks": [
                        {"check": "nonsense"},
                        {"check": "regex_deny"},  # missing patterns
                        {"check": "llm_judge", "model": "m"},  # removed in 2.0
                    ],
                },
            }
        )
    )
    result = ValidateResult()
    _validate_artifacts(tmp_path, result)
    msgs = [i.message for i in result.errors]
    assert any("'nonsense' invalid" in m for m in msgs)
    assert any("regex_deny requires 'patterns'" in m for m in msgs)
    assert any("llm_judge is gone in 2.0" in m for m in msgs)
