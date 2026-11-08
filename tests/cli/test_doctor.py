"""`tuvl doctor`: check groups, selection, strictness and the safe fixes."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tuvl.cli.json_output import COMMANDS

ENTRY = "from tuvl.cli import main; main()"


def _run(project: Path, *args: str, toolchain: str = "1") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TUVL_TOOLCHAIN": toolchain, "NO_COLOR": "1", "COLUMNS": "200"}
    return subprocess.run(
        [sys.executable, "-c", ENTRY, "check", "system", "-d", str(project), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _report(project: Path, *args: str) -> dict:
    done = _run(project, "--json", *args)
    start = done.stdout.index('{\n  "healthy"')
    return json.loads(done.stdout[start:])


def _checks(report: dict, group: str) -> dict[str, str]:
    return {c["id"]: c["status"] for c in report["checks"] if c["group"] == group}


def _detail(report: dict, group: str, check: str) -> str:
    return next(c["detail"] for c in report["checks"] if c["group"] == group and c["id"] == check)


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("doctor")
    env = {**os.environ, "TUVL_TOOLCHAIN": "1"}
    subprocess.run(
        [sys.executable, "-c", ENTRY, "init", "demo", "--sample", "-y"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )
    return root / "demo"


def test_the_json_report_matches_its_schema(sample: Path) -> None:
    report = _report(sample)
    COMMANDS["check system"].model_validate(report)
    assert not [c for c in report["checks"] if c["status"] == "fail" and c["group"] != "models"]
    assert {"project", "env", "config", "models", "git", "skills"} <= set(report["groups"])
    assert _checks(report, "config") == {"load": "ok"}
    assert _checks(report, "env")["python"] == "ok"


def test_only_and_skip_select_groups(sample: Path) -> None:
    assert _report(sample, "--only", "project,env")["groups"] == ["project", "env"]
    assert "git" not in _report(sample, "--skip", "git")["groups"]


def test_an_unknown_group_is_refused(sample: Path) -> None:
    done = _run(sample, "--only", "nonsense")
    assert done.returncode == 2
    assert "unknown group" in done.stderr


def test_a_bare_install_runs_only_the_production_groups(sample: Path) -> None:
    done = _run(sample, "--json", toolchain="0")
    start = done.stdout.index('{\n  "healthy"')
    groups = json.loads(done.stdout[start:])["groups"]
    assert "git" not in groups and "skills" not in groups
    assert {"project", "env", "config"} <= set(groups)


def test_a_project_that_does_not_load_fails_config_and_skips_the_rest(tmp_path: Path) -> None:
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "w.yaml").write_text(
        "kind: Workflow\nversion: tuvl/v2\nmetadata: {name: w}\n"
        "spec: {agents: [{id: a, engine: nope}]}\n"
    )
    done = _run(tmp_path, "--json", "--only", "config,models")
    assert done.returncode == 1
    report = json.loads(done.stdout[done.stdout.index('{\n  "healthy"') :])
    assert _checks(report, "config")["load"] == "fail"
    assert _checks(report, "models")["models"] == "skip"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_fix_initialises_git_on_develop_and_ignores_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n")
    before = _report(tmp_path, "--only", "git")
    assert _checks(before, "git")["repository"] == "warn"
    done = _run(tmp_path, "--fix", "--json", "--only", "git,env")
    report = json.loads(done.stdout[done.stdout.index('{\n  "healthy"') :])
    assert "git init -b develop" in report["fixed"]
    assert _checks(report, "git")["repository"] == "ok"
    assert _detail(report, "git", "branch") == "develop"
    # .env is only checked inside a repository, so the second pass finds and fixes it
    again = _run(tmp_path, "--fix", "--json", "--only", "env")
    fixed = json.loads(again.stdout[again.stdout.index('{\n  "healthy"') :])
    assert fixed["fixed"] == ["add .env to .gitignore"]
    assert ".env" in (tmp_path / ".gitignore").read_text().splitlines()
    assert _checks(fixed, "env")[".env ignored"] == "ok"


def test_strict_turns_warnings_into_exit_2(tmp_path: Path) -> None:
    assert _run(tmp_path, "--only", "project").returncode == 0
    assert _run(tmp_path, "--only", "project", "--strict").returncode == 2


def test_quiet_prints_only_what_needs_attention(tmp_path: Path) -> None:
    done = _run(tmp_path, "--only", "project,env", "--quiet")
    assert "project.yaml" in done.stdout
    assert "python" not in done.stdout


def test_a_required_variable_that_is_not_set_fails_the_env_group(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "env.yaml").write_text(
        "kind: EnvConfig\nspec:\n  variables:\n"
        "    - {name: DOCTOR_TEST_SECRET_XYZ, secret: true, required: true}\n"
    )
    report = _report(tmp_path, "--only", "env")
    assert _checks(report, "env")["variables"] == "fail"
    assert "DOCTOR_TEST_SECRET_XYZ" in _detail(report, "env", "variables")

    (tmp_path / ".env").write_text("DOCTOR_TEST_SECRET_XYZ=x\n")
    assert _checks(_report(tmp_path, "--only", "env"), "env")["variables"] == "ok"


def test_a_project_without_an_env_config_gets_no_variables_check(tmp_path: Path) -> None:
    assert "variables" not in _checks(_report(tmp_path, "--only", "env"), "env")
