"""The HTTP route table of the app, in dev mode and in production, is pinned.

The thin-core split moves code between packages but must not add, drop or rename a route. Regenerate a
snapshot with `UPDATE_SNAPSHOTS=1 pytest tests/test_route_table.py` and say why in the commit.
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
from tuvl.main import app

rows = set()


def walk(routes, prefix=""):
    for route in routes:
        path = prefix + getattr(route, "path", "")
        if getattr(route, "methods", None):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                rows.add(f"{method} {path}")
        elif getattr(route, "routes", None):
            walk(route.routes, path)


walk(app.routes)
print(json.dumps(sorted(rows)))
"""

DEV = {
    "TUVL_ENV": "development",
    "TUVL_DEV_MODE": "true",
    "TUVL_ALLOW_DEV_AUTH": "true",
    "TUVL_DEV_API_KEY": "route-table-key",
}
PRODUCTION = {"TUVL_ENV": "production", "TUVL_SIGNING_KEY": "k" * 40}


def _routes(mode: dict[str, str], cwd: Path) -> list[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TUVL_")}
    env.update(mode)
    env["TUVL_TOOLCHAIN"] = "1"
    done = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, cwd=cwd, check=False
    )
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(("name", "mode"), [("dev", DEV), ("production", PRODUCTION)])
def test_the_route_table_is_unchanged(name: str, mode: dict[str, str], tmp_path: Path) -> None:
    snapshot = SNAPSHOTS / f"routes_{name}.json"
    current = _routes(mode, tmp_path)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        snapshot.write_text(json.dumps(current, indent=1) + "\n")
    expected = json.loads(snapshot.read_text())
    assert current == expected, (
        f"routes added: {sorted(set(current) - set(expected))}; "
        f"routes dropped: {sorted(set(expected) - set(current))}"
    )
