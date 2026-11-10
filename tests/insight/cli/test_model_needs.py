"""Model safeguards: references resolve (V028), credentials are declared (V029), and the
project reports which models and variables it needs."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl.core.models.requirements import need_for
from tuvl_insight.cli.commands.validate import run_validation

runner = CliRunner()


def _project(tmp_path: Path, model_ref: str, agent_model: dict | None) -> Path:
    wf = {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "classify"},
        "spec": {
            "trigger": {
                "http": {"path": "/api/classify", "method": "POST"},
                "input": {"text": "str"},
            },
            "agents": [
                {
                    "id": "label",
                    "description": "Label the text",
                    "engine": "llm",
                    "inputs": {"text": "str"},
                    "outputs": {"label": "enum[good, bad]"},
                    "budget": {"max_tokens": 200, "timeout": "10s"},
                    "llm": {"model": model_ref, "prompt": "Label {{ text }}"},
                    "routes": {
                        "good": "END",
                        "bad": "END",
                        "parse_error": "END",
                        "timeout": "END",
                        "budget_exceeded": "END",
                        "error": "END",
                    },
                }
            ],
        },
    }
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "classify.yaml").write_text(yaml.safe_dump(wf))
    if agent_model is not None:
        (tmp_path / "llms").mkdir()
        doc = {
            "kind": "AgentModel",
            "version": "v1",
            "metadata": {"name": "default"},
            "spec": agent_model,
        }
        (tmp_path / "llms" / "default.yaml").write_text(yaml.safe_dump(doc))
    return tmp_path


def _codes(project: Path) -> dict[str, list[str]]:
    result, *_ = run_validation(project, echo=False)
    out: dict[str, list[str]] = {}
    for issue in result.issues:
        out.setdefault(issue.code, []).append(issue.message)
    return out


def test_an_unknown_model_reference_is_an_error_even_without_any_agent_model(
    tmp_path: Path,
) -> None:
    codes = _codes(_project(tmp_path, "default", None))
    assert any("unknown AgentModel 'default'" in m for m in codes["V028"])


def test_a_provider_model_id_needs_no_agent_model(tmp_path: Path) -> None:
    project = _project(tmp_path, "gemini/gemini-3.1-flash-lite", None)
    assert "V028" not in _codes(project)
    result, *_ = run_validation(project, echo=False)
    assert result.models["gemini/gemini-3.1-flash-lite"]["env"] == ["GEMINI_API_KEY"]


def test_validate_reports_which_models_and_variables_the_project_needs(tmp_path: Path) -> None:
    project = _project(tmp_path, "default", {"model": "gemini/gemini-3.1-flash-lite"})
    res = runner.invoke(app, ["lint", "project", "-d", str(project)])
    assert "needs models:" in res.output and "default (gemini · GEMINI_API_KEY)" in res.output
    res = runner.invoke(app, ["lint", "project", "-d", str(project), "--json"])
    models = yaml.safe_load(res.output)["models"]
    assert models["default"]["used_by"] == ["classify.label"]


def test_a_literal_api_key_is_flagged(tmp_path: Path) -> None:
    project = _project(
        tmp_path, "default", {"model": "openai/gpt-5.4-mini", "api_key": "sk-live-123"}
    )
    assert any("literal value" in m for m in _codes(project)["V029"])


def test_ship_treats_missing_credentials_as_an_error(tmp_path: Path) -> None:
    project = _project(tmp_path, "default", {"model": "acme/model-1"})
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.0.0"\n')
    res = runner.invoke(app, ["ship", "-d", str(project), "--no-build"])
    assert res.exit_code == 1 and "V029" in res.output


@pytest.mark.parametrize(
    ("spec", "env", "problems"),
    [
        ({"model": "gemini/x"}, ["GEMINI_API_KEY"], 0),
        ({"model": "ollama/llama3.1", "api_base": "http://localhost:11434"}, [], 0),
        ({"model": "openai/x", "api_key": "${MY_KEY}"}, ["MY_KEY"], 0),
        ({"model": "openai/x", "api_key": "${MY_KEY:}"}, [], 0),
        ({"model": "acme/x"}, [], 1),
        (
            {
                "type": "decision",
                "provider": "laya",
                "model": "auto",
                "api_base": "${LAYA_API_BASE}",
            },
            ["LAYA_API_BASE"],
            0,
        ),
    ],
)
def test_need_for(spec: dict, env: list[str], problems: int) -> None:
    need = need_for("m", spec)
    assert need.env == env and len(need.problems) == problems


def test_boot_refuses_in_production_and_records_in_development(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl.contract import registry
    from tuvl.core.config_loader import load_all_configs
    from tuvl.core.models import requirements

    project = _project(tmp_path, "default", {"model": "gemini/gemini-3.1-flash-lite"})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    try:
        load_all_configs(project)
        with pytest.raises(requirements.ModelConfigError, match="needs GEMINI_API_KEY"):
            requirements.check_at_boot(production=True)
        requirements.check_at_boot(production=False)
        status = requirements.MODEL_STATUS["default"]
        assert status["configured"] is False and status["missing"] == ["GEMINI_API_KEY"]
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        requirements.check_at_boot(production=True)
        assert requirements.MODEL_STATUS["default"]["configured"] is True
    finally:
        registry.clear()
        requirements.MODEL_STATUS.clear()
