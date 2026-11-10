"""`kind: EnvConfig` in the engine: the document, the registration and the boot check."""

from __future__ import annotations

import pytest

from tuvl.core.envconfig import (
    MANIFEST,
    EnvConfigError,
    EnvVar,
    check_at_boot,
    missing_required,
    parse_env_config,
    register_env_config_from_dict,
)


@pytest.fixture(autouse=True)
def _clean_manifest():
    MANIFEST.clear()
    yield
    MANIFEST.clear()


def _doc(*variables):
    return {"kind": "EnvConfig", "spec": {"variables": list(variables)}}


# ── the document ──────────────────────────────────────────────────────────────


def test_a_manifest_parses_with_its_flags():
    variables = parse_env_config(
        _doc(
            {"name": "GEMINI_API_KEY", "description": "key", "secret": True, "required": True},
            {"name": "THRESHOLD", "default": 0.8, "example": "0.9"},
        )
    )
    assert variables[0] == EnvVar("GEMINI_API_KEY", "key", secret=True, required=True)
    assert variables[1].default == "0.8" and variables[1].example == "0.9"


@pytest.mark.parametrize(
    "doc, message",
    [
        ({"kind": "EnvConfig"}, "spec must be"),
        (_doc(), "non-empty"),
        (_doc({"description": "x"}), "name"),
        (_doc({"name": "1BAD"}), "name"),
        (_doc({"name": "A"}, {"name": "A"}), "twice"),
        (_doc({"name": "A", "secret": "yes"}), "true or false"),
        (_doc({"name": "A", "oops": 1}), "unknown field"),
        (_doc({"name": "A", "secret": True, "default": "x"}), "secret and has a default"),
        (_doc({"name": "A", "required": True, "default": "x"}), "required and has a default"),
    ],
)
def test_a_malformed_manifest_says_what_is_wrong(doc, message):
    with pytest.raises(ValueError, match=message):
        parse_env_config(doc)


def test_a_variable_declared_in_two_documents_is_refused():
    register_env_config_from_dict(_doc({"name": "A"}), "a.yaml")
    with pytest.raises(ValueError, match="already declared"):
        register_env_config_from_dict(_doc({"name": "A"}), "b.yaml")


# ── boot ──────────────────────────────────────────────────────────────────────


def test_production_refuses_to_start_without_a_required_variable():
    register_env_config_from_dict(
        _doc({"name": "NEEDED", "required": True}, {"name": "OPTIONAL"}), "e.yaml"
    )
    assert missing_required({}) == ["NEEDED"]
    with pytest.raises(EnvConfigError, match="NEEDED"):
        check_at_boot(production=True, environ={})
    check_at_boot(production=True, environ={"NEEDED": "x"})
    check_at_boot(production=False, environ={})  # development only warns


def test_a_project_without_a_manifest_is_never_stopped():
    check_at_boot(production=True, environ={})
