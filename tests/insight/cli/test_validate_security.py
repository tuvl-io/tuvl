"""`tuvl validate` coverage for the security-hardening surface: workflow
``trigger.public``, model ``spec.access`` (scopes + groups), and
``.tuvl/system.yaml``."""

from __future__ import annotations

import yaml

from tuvl_insight.cli.commands.validate import (
    ValidateResult,
    _validate_model_access,
    _validate_system_config,
)

# ── trigger.public ────────────────────────────────────────────────────────────


# ── spec.access (model scope/group ACLs) ──────────────────────────────────────


def test_access_unknown_key_warns():
    result = ValidateResult()
    _validate_model_access({"bogus_key": "x"}, "Widget", "models/widget.yaml", result)
    assert any("unknown key 'spec.access.bogus_key'" in i.message for i in result.warnings)


def test_access_bad_scope_charset_errors():
    result = ValidateResult()
    _validate_model_access({"read_scope": "bad scope!"}, "Widget", "models/widget.yaml", result)
    assert any("not a valid scope name" in i.message for i in result.errors)


def test_access_group_non_string_entry_errors():
    result = ValidateResult()
    _validate_model_access({"read_groups": [123]}, "Widget", "models/widget.yaml", result)
    assert any("not a valid group name" in i.message for i in result.errors)


def test_access_bare_string_group_accepted():
    result = ValidateResult()
    _validate_model_access({"read_groups": "eng"}, "Widget", "models/widget.yaml", result)
    assert result.errors == []
    assert result.warnings == []


# ── .tuvl/system.yaml ──────────────────────────────────────────────────────────


def _write_system_yaml(tmp_path, doc: dict) -> None:
    path = tmp_path / ".tuvl" / "system.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


def test_system_config_wrong_kind_errors(tmp_path):
    _write_system_yaml(tmp_path, {"kind": "Bogus", "spec": {}})
    result = ValidateResult()
    _validate_system_config(tmp_path, result)
    assert any("Expected kind='SystemConfig'" in i.message for i in result.errors)


def test_system_config_root_level_api_errors(tmp_path):
    _write_system_yaml(tmp_path, {"kind": "SystemConfig", "api": {"expose_model_crud": True}})
    result = ValidateResult()
    _validate_system_config(tmp_path, result)
    assert any("No 'spec' block" in i.message for i in result.errors)


def test_system_config_non_bool_expose_model_crud_errors(tmp_path):
    _write_system_yaml(
        tmp_path, {"kind": "SystemConfig", "spec": {"api": {"expose_model_crud": "yes"}}}
    )
    result = ValidateResult()
    _validate_system_config(tmp_path, result)
    assert any("must be a boolean" in i.message for i in result.errors)


def test_system_config_unknown_api_key_warns(tmp_path):
    _write_system_yaml(tmp_path, {"kind": "SystemConfig", "spec": {"api": {"mystery": 1}}})
    result = ValidateResult()
    _validate_system_config(tmp_path, result)
    assert any("Unknown key 'spec.api.mystery'" in i.message for i in result.warnings)


def test_system_config_bad_deployment_mode_errors(tmp_path):
    _write_system_yaml(
        tmp_path, {"kind": "SystemConfig", "spec": {"deployment_mode": "bogus_mode"}}
    )
    result = ValidateResult()
    _validate_system_config(tmp_path, result)
    assert any("Invalid deployment_mode" in i.message for i in result.errors)


def test_system_config_absent_file_is_clean(tmp_path):
    result = ValidateResult()
    _validate_system_config(tmp_path, result)
    assert result.issues == []


# ── spec.datasource cross-reference (TD-35) ──────────────────────────────────


def test_unknown_explicit_datasource_warns(tmp_path):
    from tuvl_insight.cli.commands.validate import _validate_cross_references

    models = tmp_path / "models"
    models.mkdir()
    (models / "m.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ModelDefinition",
                "metadata": {"name": "Thing"},
                "spec": {"datasource": "nope", "fields": [{"name": "id", "type": "uuid"}]},
            }
        )
    )
    result = ValidateResult()
    _validate_cross_references(
        sorted(models.glob("*.yaml")), tmp_path, {"Thing"}, {"default"}, result
    )
    assert any("matches no DataSource" in i.message for i in result.warnings)


def test_implicit_datasource_does_not_warn(tmp_path):
    from tuvl_insight.cli.commands.validate import _validate_cross_references

    models = tmp_path / "models"
    models.mkdir()
    (models / "m.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ModelDefinition",
                "metadata": {"name": "Thing"},
                "spec": {"fields": [{"name": "id", "type": "uuid"}]},
            }
        )
    )
    result = ValidateResult()
    _validate_cross_references(
        sorted(models.glob("*.yaml")), tmp_path, {"Thing"}, {"default"}, result
    )
    assert not result.warnings
