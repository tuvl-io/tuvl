"""The engine that runs a project does not load the tooling (tuvl_insight) that lints, generates or edits it."""

from __future__ import annotations

import subprocess
import sys

import pytest

CHECK = """
import sys
import {module}

loaded = sorted(m for m in sys.modules if m == "tuvl_insight" or m.startswith("tuvl_insight."))
print(",".join(loaded))
"""


@pytest.mark.parametrize(
    "module",
    [
        "tuvl.runtime.runner",
        "tuvl.runtime.loop",
        "tuvl.runtime.host",
        "tuvl.contract.surface",
        "tuvl.api.uploads",
        "tuvl.core.artifacts.loader",
    ],
)
def test_the_engine_does_not_load_the_tooling(module: str) -> None:
    done = subprocess.run(
        [sys.executable, "-c", CHECK.format(module=module)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "", f"importing {module} loaded {done.stdout.strip()}"
