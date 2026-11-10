"""`tuvl init --sample` passes the full CI gate as created (P3.7, the TD-46 class of bug).

Each command runs in its own process, as a user or CI would run it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_insight


GATE = [
    ["lint", "project", "--strict"],
    ["generate", "code", "--check"],
    ["generate", "lock", "--check"],
    ["run", "tests"],
    ["list", "tasks", "--strict"],
]


def tuvl(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("TUVL_", "GEMINI_", "POSTGRES_"))
    }
    return subprocess.run(
        [sys.executable, "-c", "from tuvl.cli import app; app()", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize("preset", ["gemini", "ollama"])
def test_sample_passes_the_ci_gate(tmp_path: Path, preset: str) -> None:
    created = tuvl(tmp_path, "init", "demo", "--sample", "--preset", preset, "-y")
    assert created.returncode == 0, created.stdout + created.stderr
    project = tmp_path / "demo"
    assert (project / "tuvl.lock").is_file() and (
        project / "specs" / "triage-ticket.tasks.yaml"
    ).is_file()
    assert not (project / "llms" / "analysis.yaml").exists()  # no planning model: the agent plans
    for command in GATE:
        result = tuvl(project, *command)
        assert result.returncode == 0, (
            f"tuvl {' '.join(command)}:\n{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
        )
    for command in (
        ["run", "tests", "-d", "demo"],
        ["list", "tasks", "--strict", "-d", "demo"],
    ):  # from elsewhere
        result = tuvl(tmp_path, *command)
        assert result.returncode == 0, f"tuvl {' '.join(command)}:\n{result.stdout[-3000:]}"
