"""A bare `pip install tuvl` has none of the extras and still boots and validates a project."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tuvl.extras import EXTRAS, MissingExtraError, require

ROOT = Path(__file__).resolve().parent.parent
BLOCK = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {mods!r}:
            raise ModuleNotFoundError(f"blocked: {{name}}")
sys.meta_path.insert(0, Block())
"""


def _meta() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_the_optional_packages_are_extras_not_base_dependencies() -> None:
    meta = _meta()
    base = [d.lower().replace("-", "_") for d in meta["dependencies"]]
    for name, extra in EXTRAS.items():
        assert not any(d.startswith(extra.package.replace("-", "_")) for d in base), name
        declared = meta["optional-dependencies"][name]
        assert any(d.lower().startswith(extra.package) for d in declared), name
    assert f"tuvl[{','.join(EXTRAS)}]" in meta["optional-dependencies"]["standard"]


@pytest.mark.requires_insight
def test_the_app_and_cli_import_without_the_extras() -> None:
    code = BLOCK.format(mods={e.module for e in EXTRAS.values()}) + (
        "import tuvl.core.app, tuvl.main, tuvl.cli\n"
        "from tuvl_insight.cli.commands import ship, validate, lock\n"
        "import tuvl.runtime.runner, tuvl.runtime.host, tuvl.api.runs, tuvl.api.triggers\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-800:]


def test_the_engine_imports_without_the_tooling_package() -> None:
    """A bare `pip install tuvl` has no tuvl_insight and none of its dependencies."""
    tooling = {"tuvl_insight", "libcst", "aiosqlite", "textual", "ruff", "pypdf"}
    # `sys.modules[name] = None` is how an absent package looks: `import` fails and `find_spec` says None
    code = (
        "import sys\n"
        f"for name in {sorted(tooling | {e.module for e in EXTRAS.values()})!r}:\n"
        "    sys.modules[name] = None\n"
        "import tuvl.core.app, tuvl.main, tuvl.cli, tuvl.cli.verbs\n"
        "import tuvl.runtime.runner, tuvl.runtime.host, tuvl.api.runs, tuvl.api.triggers, tuvl.doctor\n"
        "from tuvl.doctor import available_groups\n"
        "assert 'git' not in available_groups(), 'toolchain groups need tuvl_insight'\n"
        "from tuvl.extras import toolchain_installed\n"
        "assert not toolchain_installed()\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "TUVL_TOOLCHAIN"}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr[-800:]


@pytest.mark.parametrize("extra", ["mcp", "redis", "vector"])
def test_a_feature_without_its_extra_names_the_extra(extra: str, monkeypatch) -> None:
    monkeypatch.setattr("tuvl.extras.available", lambda name: False)
    with pytest.raises(MissingExtraError, match=rf"tuvl\[{extra}\]"):
        require(extra)


POSIX_ONLY = {"fcntl", "termios", "pwd", "grp", "resource", "pty"}


def test_no_module_imports_a_posix_only_module_at_the_top_level() -> None:
    import ast

    offenders = []
    for path in (ROOT / "src" / "tuvl").rglob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(n.split(".")[0] in POSIX_ONLY for n in names):
                offenders.append(f"{path.relative_to(ROOT)}: {names}")
    assert not offenders, offenders
