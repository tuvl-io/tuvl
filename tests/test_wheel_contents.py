"""What each built wheel contains: the engine ships no developer tooling, and the tooling ships no engine.

Builds both wheels (a few seconds, offline once hatchling is cached) and lists them.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: top-level packages and modules that moved to tuvl_insight; none may be in the engine wheel
MOVED = [
    "spec", "specgen", "taskrun", "codegen", "coding_agents", "skills", "review", "audit", "uispec", "tui",
    "devmcp", "devtelemetry", "testing", "gitflow", "gitutil", "shipgate", "shiparchive", "validate", "lock",
]  # fmt: skip


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str]]:
    out = tmp_path_factory.mktemp("dist")
    names: dict[str, list[str]] = {}
    for package in ("tuvl", "tuvl-insight"):
        done = subprocess.run(
            ["uv", "build", "--package", package, "--wheel", "--no-sources", "--out-dir", str(out)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr[-800:]
    for wheel in out.glob("*.whl"):
        with zipfile.ZipFile(wheel) as z:
            names[wheel.name.split("-")[0]] = z.namelist()
    return names


def test_the_engine_wheel_holds_no_tooling(wheels: dict[str, list[str]]) -> None:
    files = wheels["tuvl"]
    assert any(f == "tuvl/main.py" for f in files)
    leaked = [f for f in files if f.startswith(("tuvl_insight/", "insight/"))]
    assert not leaked, leaked[:5]
    for name in MOVED:
        assert not [
            f for f in files if f in {f"tuvl/{name}.py"} or f.startswith(f"tuvl/{name}/")
        ], name
    assert not [f for f in files if f.startswith("tuvl/cli/ui")]


def test_the_engine_wheel_keeps_only_the_production_commands(wheels: dict[str, list[str]]) -> None:
    commands = {
        Path(f).stem
        for f in wheels["tuvl"]
        if f.startswith("tuvl/cli/commands/") and f.endswith(".py")
    }
    assert commands == {
        "__init__",
        "run",
        "runs",
        "db",
        "doctor",
        "iam",
        "keys",
        "memory",
        "storage",
    }


def test_the_tooling_wheel_holds_no_engine(wheels: dict[str, list[str]]) -> None:
    files = wheels["tuvl_insight"]
    assert any(f == "tuvl_insight/__init__.py" for f in files)
    assert not [f for f in files if f.startswith("tuvl/")], (
        "tuvl_insight must not ship engine files"
    )
    assert any(f.startswith("tuvl_insight/skills/") and f.endswith("SKILL.md") for f in files), (
        "skill data"
    )
    assert any(f.startswith("tuvl_insight/validate/") for f in files)
