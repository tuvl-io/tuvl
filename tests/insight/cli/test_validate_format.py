"""`tuvl validate` accepts ONLY the spec-wrapped document form (and `type: enum`).

The spec envelope is the single accepted document shape — the legacy flat form
(body fields at root) is rejected with a pointed error, for Workflow and
AgentModel alike. This pins the shape contract shared with
``api.manager._normalize_workflow`` so validator and runtime cannot drift.
"""

from __future__ import annotations

import yaml

from tuvl_insight.cli.commands.validate import (
    _FIELD_TYPES,
    ValidateResult,
    _validate_llms,
    _validate_models,
)


def _write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


# ── field types sourced from the loader (includes enum) ───────────────────────
def test_field_types_include_enum():
    assert "enum" in _FIELD_TYPES


def test_enum_model_field_validates(tmp_path):
    models = tmp_path / "models"
    _write(
        models / "book.yaml",
        {
            "kind": "ModelDefinition",
            "metadata": {"name": "Book", "schema_version": "v1"},
            "enabled": True,
            "spec": {
                "tablename": "books",
                "fields": [
                    {"name": "id", "type": "uuid", "primary_key": True, "input": False},
                    {"name": "status", "type": "enum", "enum_values": ["draft", "published"]},
                ],
            },
        },
    )
    result = ValidateResult()
    _validate_models(sorted(models.glob("*.yaml")), tmp_path, result)
    assert not any("unknown type" in i.message for i in result.errors)


# ── AgentModel: spec-wrapped resolves; flat form is rejected ──────────────────
def _agentmodel(spec_wrapped: bool) -> dict:
    base = {"kind": "AgentModel", "metadata": {"name": "default"}, "enabled": True}
    fields = {"model": "openai/gpt-4o-mini", "temperature": 0.1}
    return {**base, "spec": fields} if spec_wrapped else {**base, **fields}


def test_spec_wrapped_agentmodel_model_found(tmp_path):
    llms = tmp_path / "llms"
    _write(llms / "default.yaml", _agentmodel(spec_wrapped=True))
    result = ValidateResult()
    known = _validate_llms(sorted(llms.glob("*.yaml")), tmp_path, result)
    assert "default" in known
    assert not any("'model' field is required" in i.message for i in result.errors)


def test_flat_agentmodel_rejected(tmp_path):
    llms = tmp_path / "llms"
    _write(llms / "default.yaml", _agentmodel(spec_wrapped=False))
    result = ValidateResult()
    known = _validate_llms(sorted(llms.glob("*.yaml")), tmp_path, result)
    assert "default" not in known
    assert any("No 'spec' block" in i.message for i in result.errors)


# ── Workflow: spec-wrapped steps deeply validated; flat form rejected ─────────
