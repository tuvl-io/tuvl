"""`tuvl init` — interactive project scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app

runner = CliRunner()

# Decline both the postgres and LLM setup prompts.
_DECLINE_ALL = "n\nn\nn\n\n"  # postgres, LLM, laya; then the initials default


def test_init_minimal_scaffold_when_declining_pg_and_llm(tmp_path: Path) -> None:
    target = tmp_path / "myapp"
    res = runner.invoke(app, ["init", str(target)], input=_DECLINE_ALL)
    assert res.exit_code == 0, res.output

    for subdir in (
        "models",
        "workflows",
        "datasources",
        "llms",
        "agents",
        "federation",
        "idea",
        "specs",
        "artifacts",
    ):
        assert (target / subdir).is_dir()

    assert (target / ".env").exists()
    assert (target / ".env.example").exists()
    assert ".env" in (target / ".gitignore").read_text().splitlines()
    assert (target / "pyproject.toml").exists()
    assert (target / "README.md").exists()
    assert (target / "AGENTS.md").exists()  # --no-ai-skills not passed

    system_yaml = yaml.safe_load((target / ".tuvl" / "system.yaml").read_text())
    assert system_yaml["spec"]["deployment_mode"] == "single_tenant"

    # No postgres/LLM configured -> no datasource or llm YAML written.
    assert not (target / "datasources" / "postgres.yaml").exists()
    assert not (target / "llms" / "default.yaml").exists()

    # No sample files without --sample.
    assert not (target / "workflows" / "triage_ticket.yaml").exists()


def test_init_no_ai_skills_skips_agents_scaffold(tmp_path: Path) -> None:
    target = tmp_path / "myapp"
    res = runner.invoke(app, ["init", str(target), "--no-ai-skills"], input=_DECLINE_ALL)
    assert res.exit_code == 0, res.output
    assert not (target / "AGENTS.md").exists()
    assert not (target / ".agents").exists()


def test_init_sample_writes_a_2_0_project(tmp_path: Path) -> None:
    target = tmp_path / "myapp"
    res = runner.invoke(app, ["init", str(target), "--sample"], input=_DECLINE_ALL)
    assert res.exit_code == 0, res.output

    workflow = yaml.safe_load((target / "workflows" / "triage_ticket.yaml").read_text())
    assert workflow["version"] == "tuvl/v2"
    assert {a["engine"] for a in workflow["spec"]["agents"]} == {"llm", "code", "human", "tool"}
    assert (target / "agents" / "prioritise_ticket.py").exists()
    assert (target / "models" / "ticket.yaml").exists()
    assert not (target / "nodes").exists() and not (target / "tests" / "workflows").exists()


def test_init_refuses_existing_directory(tmp_path: Path) -> None:
    target = tmp_path / "myapp"
    target.mkdir()
    res = runner.invoke(app, ["init", str(target)], input=_DECLINE_ALL)
    assert res.exit_code == 1
    assert "already exists" in res.output


def test_init_multi_tenant_requires_postgres(tmp_path: Path) -> None:
    target = tmp_path / "myapp"
    # multi_tenant forces configure_pg=True (no confirm prompt for it), so the
    # remaining input only needs to answer the 5 postgres field prompts (all
    # defaulted) followed by declining the LLM prompt.
    res = runner.invoke(app, ["init", str(target), "--multi-tenant"], input="\n\n\n\n\nn\nn\n\n")
    assert res.exit_code == 0, res.output

    system_yaml = yaml.safe_load((target / ".tuvl" / "system.yaml").read_text())
    assert system_yaml["spec"]["deployment_mode"] == "multi_tenant"
    assert (target / "datasources" / "postgres.yaml").exists()


def test_init_configures_postgres_and_llm_with_defaults(tmp_path: Path) -> None:
    target = tmp_path / "myapp"
    # pg confirm(y) -> 5 defaulted field prompts -> llm confirm(y) -> provider,
    # value, model all defaulted (ollama) -> no decision model.
    res = runner.invoke(app, ["init", str(target)], input="\n" * 12)
    assert res.exit_code == 0, res.output

    env_content = (target / ".env").read_text()
    assert "POSTGRES_HOST=localhost" in env_content
    assert "OLLAMA_BASE_URL=" in env_content

    ds = yaml.safe_load((target / "datasources" / "postgres.yaml").read_text())
    assert ds["spec"]["connection"]["host"] == "${POSTGRES_HOST:localhost}"

    agent = yaml.safe_load((target / "llms" / "default.yaml").read_text())
    assert agent["spec"]["model"] == "ollama/llama3.1"


def test_init_laya_decision_model_scaffolds_the_model_env_and_compose(tmp_path: Path) -> None:
    target = tmp_path / "decider"
    res = runner.invoke(app, ["init", str(target), "-y", "--decision-model", "laya"])
    assert res.exit_code == 0, res.output

    model = yaml.safe_load((target / "llms" / "laya.yaml").read_text())
    assert model["spec"] == {
        "type": "decision",
        "provider": "laya",
        "model": "english",
        "api_base": "${LAYA_API_BASE:http://localhost:8000}",
        "api_key": "${LAYA_API_KEY:}",
    }
    compose = yaml.safe_load((target / "compose.yaml").read_text())
    assert compose["services"]["laya"]["ports"] == ["127.0.0.1:8000:8000"]
    assert compose["services"]["laya"]["image"].endswith("-en}")
    env = (target / ".env").read_text()
    assert "LAYA_API_BASE=http://localhost:8000" in env and "LAYA_API_KEY=" in env
    assert "## Decision model: Laya" in (target / "README.md").read_text()


def test_init_asks_about_laya_and_defaults_to_no(tmp_path: Path) -> None:
    target = tmp_path / "plain"
    res = runner.invoke(app, ["init", str(target)], input=_DECLINE_ALL)
    assert res.exit_code == 0, res.output
    assert "Laya decision model" in res.output
    assert not (target / "llms" / "laya.yaml").exists()
    assert not (target / "compose.yaml").exists()


def test_init_rejects_an_unknown_decision_model(tmp_path: Path) -> None:
    res = runner.invoke(app, ["init", str(tmp_path / "x"), "-y", "--decision-model", "jev"])
    assert res.exit_code == 2 and "Unknown decision model" in res.output


def _pyproject(tmp_path, monkeypatch, version: str | None) -> str:
    import importlib.metadata

    from tuvl_insight.cli.commands.init import _write_pyproject

    if version is None:

        def missing(_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError

        monkeypatch.setattr(importlib.metadata, "version", missing)
    else:
        monkeypatch.setattr(importlib.metadata, "version", lambda _name: version)
    path = tmp_path / "pyproject.toml"
    _write_pyproject(path, name="demo")
    return path.read_text()


def test_pyproject_python_range_matches_the_package(tmp_path, monkeypatch) -> None:
    text = _pyproject(tmp_path, monkeypatch, "2.0.0b6")
    assert 'requires-python = ">=3.13,<3.15"' in text


def test_pyproject_allows_prereleases_only_for_a_prerelease_tuvl(tmp_path, monkeypatch) -> None:
    import tomllib

    beta = tomllib.loads(_pyproject(tmp_path, monkeypatch, "2.0.0b6"))
    assert beta["tool"]["uv"]["prerelease"] == "allow"
    (tmp_path / "pyproject.toml").unlink()
    stable = tomllib.loads(_pyproject(tmp_path, monkeypatch, "2.0.0"))
    assert "prerelease" not in stable["tool"]["uv"]
    assert stable["dependency-groups"]["dev"] == ["tuvl[standard]>=2.0.0"]


@pytest.fixture(autouse=True)
def _no_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interactive init asks which agent to use only when one is installed; keep that out of these tests."""
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])


# ── the pieces init is made of ───────────────────────────────────────────────


def test_the_target_follows_the_name_and_the_project_dir(tmp_path: Path) -> None:
    from tuvl_insight.cli.commands.init import _resolve_target

    assert _resolve_target("trade", None) == (Path.cwd() / "trade").resolve()
    assert _resolve_target("trade", tmp_path) == (tmp_path / "trade").resolve()
    assert _resolve_target(".", tmp_path / "trade") == (tmp_path / "trade").resolve()


def test_options_are_refused_before_anything_is_asked(tmp_path: Path) -> None:
    import typer

    from tuvl_insight.cli.commands.init import _check_options

    ok = {"preset": None, "decision_model": None, "ci": "none", "agent": None, "initials": None}
    target = tmp_path / "new"
    assert _check_options(**ok, project_dir=target) == (None, None)
    assert _check_options(**{**ok, "initials": " ab "}, project_dir=target) == ("AB", None)
    for bad, code in (
        ({"preset": "nope"}, 2),
        ({"decision_model": "x"}, 2),
        ({"ci": "svn"}, 2),
        ({"agent": "no-such-agent"}, 2),
        ({"initials": "1x"}, 2),
    ):
        with pytest.raises(typer.Exit) as exc:
            _check_options(**{**ok, **bad}, project_dir=target)
        assert exc.value.exit_code == code, bad
    tmp_path.joinpath("there").mkdir()
    with pytest.raises(typer.Exit) as exc:
        _check_options(**ok, project_dir=tmp_path / "there")
    assert exc.value.exit_code == 1


def test_the_answers_become_the_env_file() -> None:
    from tuvl_insight.cli.commands.init import Answers, LlmAnswers, PgAnswers, _env_values

    answers = Answers(
        pg=PgAnswers("h", "1", "d", "u", "pw"),
        llm=LlmAnswers("openai", "OPENAI_API_KEY", "sk-x", True, "m"),
        use_laya=True,
        initials="AB",
        agent="none",
    )
    env = _env_values(answers)
    assert env["POSTGRES_PASSWORD"] == ("pw", True) and env["POSTGRES_HOST"] == ("h", False)
    assert env["OPENAI_API_KEY"] == ("sk-x", True) and "LAYA_API_BASE" in env
    assert _env_values(Answers(None, None, False, "AB", "none")) == {}


def test_yes_accepts_the_postgres_defaults_and_asks_nothing() -> None:
    from tuvl_insight.cli.commands.init import (
        PgAnswers,
        _ask_decision_model,
        _ask_llm,
        _ask_postgres,
    )

    assert _ask_postgres(multi_tenant=False, yes=True) == PgAnswers(
        "localhost", "5432", "tuvl", "postgres", "postgres"
    )
    assert _ask_llm(None, yes=True) is None
    assert _ask_decision_model(None, yes=True) is False
    assert _ask_decision_model("laya", yes=True) is True
    preset = _ask_llm("ollama", yes=True)
    assert preset is not None and preset.provider == "ollama"
