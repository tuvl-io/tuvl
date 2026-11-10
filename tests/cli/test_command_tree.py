"""The CLI's command tree, with and without the developer toolchain, is pinned.

On a bare install every toolchain command still exists as a stub that prints the install hint; on a full
install it is the real command. The thin-core split must leave both lists exactly as they are.
Regenerate with `UPDATE_SNAPSHOTS=1 pytest tests/cli/test_command_tree.py` and say why in the commit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SNAPSHOTS = Path(__file__).parent / "snapshots"

PROBE = """
import json

import typer
from typer._click.core import Context

from tuvl.cli import app

root = typer.main.get_command(app)
found = []


def walk(cmd, path, ctx):
    if hasattr(cmd, "list_commands"):
        for name in sorted(cmd.list_commands(ctx)):
            sub = cmd.get_command(ctx, name)
            if sub is not None:
                walk(sub, [*path, name], Context(sub, parent=ctx, info_name=name))
    elif path:
        found.append({"command": " ".join(path), "hidden": bool(getattr(cmd, "hidden", False))})


walk(root, [], Context(root, info_name="tuvl"))
print(json.dumps(sorted(found, key=lambda row: row["command"])))
"""


def _tree(toolchain: str) -> list[dict[str, object]]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TUVL_")}
    env["TUVL_TOOLCHAIN"] = toolchain
    done = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, check=False
    )
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("name", "toolchain"),
    [pytest.param("full", "1", marks=pytest.mark.requires_insight), ("bare", "0")],
)
def test_the_command_tree_is_unchanged(name: str, toolchain: str) -> None:
    snapshot = SNAPSHOTS / f"command_tree_{name}.json"
    current = _tree(toolchain)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        snapshot.write_text(json.dumps(current, indent=1) + "\n")
    expected = json.loads(snapshot.read_text())
    have = {row["command"] for row in current}
    want = {row["command"] for row in expected}
    assert current == expected, f"added: {sorted(have - want)}; dropped: {sorted(want - have)}"
