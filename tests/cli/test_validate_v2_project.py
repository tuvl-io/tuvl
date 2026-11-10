"""validate on whole projects: a fresh `tuvl init --sample` passes --strict,
1.x workflows and tests are rejected, and code agents must exist."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl.cli.commands.validate import run_validation
from tuvl.runtime.code import AGENT_REGISTRY

cli_runner = CliRunner()


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


def _errors(project: Path) -> list[str]:
    result, *_ = run_validation(project, echo=False)
    return [i.message for i in result.errors]


def test_fresh_sample_project_passes_strict_validate(tmp_path: Path) -> None:
    target = tmp_path / "app"
    res = cli_runner.invoke(app, ["init", str(target), "--sample"], input="n\nn\nn\n\n")
    assert res.exit_code == 0, res.output
    with patch.dict(AGENT_REGISTRY):
        res = cli_runner.invoke(app, ["lint", "project", "--project-dir", str(target), "--strict"])
    assert res.exit_code == 0, res.output
    assert "triage_ticket" in res.output


def test_1x_workflows_and_tests_are_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path / "workflows" / "old.yaml",
        {"kind": "Workflow", "metadata": {"name": "old"}, "spec": {"steps": []}},
    )
    _write(tmp_path / "tests" / "t.yaml", {"kind": "test", "workflow": "old"})
    errors = _errors(tmp_path)
    assert any("tuvl 1.x workflow" in m for m in errors)
    assert any("'kind: test' files are not supported" in m for m in errors)


def test_code_agent_must_be_registered(tmp_path: Path) -> None:
    _write(
        tmp_path / "workflows" / "wf.yaml",
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": "wf"},
            "spec": {
                "agents": [
                    {
                        "id": "go",
                        "description": "d",
                        "engine": "code",
                        "code": {"run": "missing_one"},
                        "routes": {"default": "END", "error": "END"},
                    }
                ]
            },
        },
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "other.py").write_text(
        "from tuvl import agent\n\n\n@agent('other')\nasync def other(inp, ctx):\n    return None\n"
    )
    with patch.dict(AGENT_REGISTRY):
        errors = _errors(tmp_path)
    assert any("no @agent('missing_one')" in m for m in errors)


@pytest.fixture(autouse=True)
def _no_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])
