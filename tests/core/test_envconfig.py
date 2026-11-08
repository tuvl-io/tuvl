"""`kind: EnvConfig`, the `${VAR}` scanner, the inventory, the boot check and `.env` writing."""

from __future__ import annotations

from pathlib import Path

import pytest

from tuvl.core import envconfig
from tuvl.core.envconfig import (
    ENGINE_CATALOG,
    MANIFEST,
    EnvConfigError,
    EnvVar,
    check_at_boot,
    gitignored,
    inventory,
    missing_required,
    parse_env_config,
    register_env_config_from_dict,
    scan_references,
    ship_variables,
    unused_declarations,
    write_dotenv,
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


def _manifest_file(root: Path, *variables) -> None:
    import yaml

    (root / "env.yaml").write_text(yaml.safe_dump(_doc(*variables)))


# ── scanning ──────────────────────────────────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    (tmp_path / "datasources").mkdir()
    (tmp_path / "datasources" / "main.yaml").write_text(
        "kind: DataSource\nmetadata: {name: main}\nspec:\n  connection:\n"
        "    host: ${POSTGRES_HOST}\n    port: ${POSTGRES_PORT:5432}\n"
        "    password: ${POSTGRES_PASSWORD}\n"
    )
    (tmp_path / "llms").mkdir()
    (tmp_path / "llms" / "m.yaml").write_text(
        "kind: AgentModel\nmetadata: {name: m}\nspec:\n  api_key: ${GEMINI_API_KEY}\n"
        "  api_base: ${GEMINI_BASE:https://x.example}\n"
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "tool.py").write_text(
        'import os\nKEY = os.environ["CRM_TOKEN"]\nURL = os.environ.get("CRM_URL")\n'
        'OTHER = os.getenv("CRM_REGION")\nDYN = os.environ.get(name)\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text('import os\nos.environ["ONLY_IN_TESTS"]\n')
    (tmp_path / "notes.yaml").write_text("kind: Test\nspec:\n  x: ${DEV_ONLY}\n")
    return tmp_path


def test_the_scan_finds_references_in_yaml_and_python(tmp_path):
    refs = scan_references(_project(tmp_path))
    assert set(refs) == {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_PASSWORD",
        "GEMINI_API_KEY",
        "GEMINI_BASE",
        "CRM_TOKEN",
        "CRM_URL",
        "CRM_REGION",
    }
    assert refs["POSTGRES_PORT"].all_have_default and refs["GEMINI_BASE"].all_have_default
    assert not refs["POSTGRES_HOST"].all_have_default
    assert refs["GEMINI_API_KEY"].files == ["llms/m.yaml"]


def test_a_commented_out_reference_is_not_a_reference(tmp_path):
    (tmp_path / "a.yaml").write_text("kind: DataSource\n# host: ${COMMENTED}\nspec: {}\n")
    assert scan_references(tmp_path) == {}


def test_the_manifest_documents_themselves_are_not_scanned(tmp_path):
    (tmp_path / "env.yaml").write_text(
        "kind: EnvConfig\nspec:\n  variables:\n    - name: A\n      description: uses ${B}\n"
    )
    assert scan_references(tmp_path) == {}


# ── inventory ─────────────────────────────────────────────────────────────────


def test_the_inventory_merges_catalog_manifest_and_references(tmp_path):
    root = _project(tmp_path)
    _manifest_file(
        root,
        {"name": "GEMINI_API_KEY", "description": "key", "secret": True, "required": True},
        {"name": "UNUSED", "default": "1"},
    )
    entries = {
        e.var.name: e
        for e in inventory(root, environ={"GEMINI_API_KEY": "abc", "POSTGRES_HOST": "db"})
    }

    gemini = entries["GEMINI_API_KEY"]
    assert gemini.source == "project" and gemini.is_set and gemini.needed
    assert gemini.value is None, "a secret's value is never returned"
    assert gemini.referenced_by == ["llms/m.yaml"]

    host = entries["POSTGRES_HOST"]
    assert host.source == "engine" and host.value == "db"

    assert entries["POSTGRES_PASSWORD"].var.secret and not entries["POSTGRES_PASSWORD"].is_set
    assert entries["CRM_TOKEN"].source == "referenced" and entries["CRM_TOKEN"].var.secret
    assert not entries["CRM_URL"].var.secret
    assert "UNUSED" in entries and entries["UNUSED"].referenced_by == []
    # engine variables nobody refers to or sets stay out of the way
    assert "TUVL_API_DOCS" not in entries


def test_a_value_in_dotenv_counts_as_set(tmp_path):
    (tmp_path / ".env").write_text("TUVL_API_DOCS=off\nSECRET_TOKEN=shh\n")
    (tmp_path / "a.yaml").write_text("kind: DataSource\nspec: {x: '${SECRET_TOKEN}'}\n")
    entries = {e.var.name: e for e in inventory(tmp_path, environ={})}
    assert entries["TUVL_API_DOCS"].is_set and entries["TUVL_API_DOCS"].value == "off"
    assert entries["SECRET_TOKEN"].is_set and entries["SECRET_TOKEN"].value is None


def test_unused_declarations_are_listed(tmp_path):
    root = _project(tmp_path)
    _manifest_file(root, {"name": "GEMINI_API_KEY"}, {"name": "UNUSED"})
    assert unused_declarations(root) == ["UNUSED"]


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


# ── ship ──────────────────────────────────────────────────────────────────────


def test_ship_splits_secrets_from_plain_values(tmp_path):
    root = _project(tmp_path)
    _manifest_file(
        root,
        {"name": "GEMINI_API_KEY", "secret": True},
        {"name": "THRESHOLD", "default": "0.8"},
        {"name": "REGION"},
    )
    secrets, plain = ship_variables(root)
    secret_names = {e.var.name for e in secrets}
    assert {"GEMINI_API_KEY", "POSTGRES_PASSWORD", "TUVL_SIGNING_KEY", "CRM_TOKEN"} <= secret_names
    assert [e.var.name for e in plain] == ["THRESHOLD"]
    assert ENGINE_CATALOG["TUVL_SIGNING_KEY"].secret


# ── .env ──────────────────────────────────────────────────────────────────────


def test_write_dotenv_sets_and_removes_and_keeps_the_rest(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep me\nA=1\nB=2\n")
    write_dotenv(tmp_path, {"A": "changed", "C": "has space", "B": None})
    text = env.read_text()
    assert "# keep me" in text and "A=changed" in text and "B=" not in text
    assert envconfig.read_dotenv(tmp_path) == {"A": "changed", "C": "has space"}


def test_write_dotenv_creates_a_private_file(tmp_path):
    write_dotenv(tmp_path, {"A": "1"})
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_write_dotenv_refuses_bad_names_and_multiline_values(tmp_path):
    with pytest.raises(ValueError, match="not an environment variable name"):
        write_dotenv(tmp_path, {"bad name": "x"})
    with pytest.raises(ValueError, match="one line"):
        write_dotenv(tmp_path, {"A": "x\ny"})


def test_gitignored(tmp_path):
    assert not gitignored(tmp_path)
    (tmp_path / ".gitignore").write_text("node_modules\n.env\n")
    assert gitignored(tmp_path)


# ── validate ──────────────────────────────────────────────────────────────────


def _validate(root: Path):
    from tuvl.cli.commands.validate import ValidateResult, _validate_env_config

    result = ValidateResult()
    _validate_env_config(sorted(root.glob("env*.yaml")), root, result)
    return result


def test_validate_holds_a_project_with_a_manifest_to_it(tmp_path):
    root = _project(tmp_path)
    (root / "env.yaml").write_text(
        "kind: EnvConfig\nspec:\n  variables:\n"
        "    - {name: GEMINI_API_KEY, secret: true}\n    - {name: STALE}\n"
    )
    result = _validate(root)
    codes = {(i.code, i.severity) for i in result.issues}
    assert ("V046", "error") in codes  # CRM_TOKEN etc. are not declared
    assert ("V047", "warning") in codes  # STALE is unused
    undeclared = [i.message for i in result.issues if i.code == "V046"]
    assert any("CRM_TOKEN" in m for m in undeclared)
    assert not any("POSTGRES_HOST" in m for m in undeclared), "engine variables need no declaration"
    assert not any("GEMINI_API_KEY" in m for m in undeclared)


def test_validate_leaves_a_project_without_a_manifest_alone(tmp_path):
    root = _project(tmp_path)
    assert _validate(root).issues == []


def test_validate_reports_a_malformed_or_duplicated_manifest(tmp_path):
    (tmp_path / "env.yaml").write_text(
        "kind: EnvConfig\nspec:\n  variables: [{name: A}, {name: B, secret: true, default: x}]\n"
    )
    (tmp_path / "env2.yaml").write_text("kind: EnvConfig\nspec:\n  variables: [{name: A}]\n")
    result = _validate(tmp_path)
    messages = " ".join(i.message for i in result.issues if i.code == "V045")
    assert "secret and has a default" in messages
