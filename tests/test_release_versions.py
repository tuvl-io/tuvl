"""The release version lives in several files; they must agree, and the bump script must set all of them."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
assert spec and spec.loader
bump = importlib.util.module_from_spec(spec)
sys.modules["bump_version"] = bump
spec.loader.exec_module(bump)


def test_the_repository_versions_agree() -> None:
    assert bump.check(ROOT) == []


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    for site in bump.SITES:
        target = tmp_path / site.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / site.path, target)
    return tmp_path


def test_bump_sets_every_site(copy: Path) -> None:
    bump.write(copy, "9.8.7b2")
    assert set(bump.read(copy).values()) == {"9.8.7b2"}
    assert bump.check(copy) == []


def test_check_names_the_site_that_drifted(copy: Path) -> None:
    pyproject = copy / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace('"tuvl-insight==', '"tuvl-insight==0.0.1+', 1)
    )
    problems = bump.check(copy)
    assert any(line.startswith("tuvl-insight pin") for line in problems)


@pytest.mark.parametrize("bad", ["2", "v2.0.1", "2.0.1-beta.1", "two", ""])
def test_bump_rejects_non_pep440_versions(copy: Path, bad: str) -> None:
    with pytest.raises(SystemExit):
        bump.write(copy, bad)


@pytest.mark.parametrize("good", ["2.0.1", "2.1.0b1", "2.0.0rc2", "3.0.0.post1", "2.2.0.dev3"])
def test_bump_accepts_pep440_versions(copy: Path, good: str) -> None:
    bump.write(copy, good)
    assert set(bump.read(copy).values()) == {good}
