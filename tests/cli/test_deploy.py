"""`tuvl deploy` only announces managed hosting; it must never look like a deployment."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tuvl.cli import app

pytestmark = pytest.mark.requires_insight


runner = CliRunner()


def test_deploy_says_it_is_not_available_and_exits_non_zero() -> None:
    res = runner.invoke(app, ["deploy"])
    assert res.exit_code == 1
    assert "coming soon" in res.output
    assert "nothing was deployed" in res.output
    assert "tuvl ship" in res.output


def test_deploy_accepts_a_project_dir_and_touches_nothing(tmp_path) -> None:
    res = runner.invoke(app, ["deploy", "-d", str(tmp_path)])
    assert res.exit_code == 1
    assert list(tmp_path.iterdir()) == []


def test_deploy_is_listed_in_the_help_screen() -> None:
    assert "deploy" in runner.invoke(app, ["help"]).output
