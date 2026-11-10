"""No two routes of the application answer the same method and path."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

SCRIPT = """
import json
from collections import Counter
from tuvl.core.app import app

seen = Counter((m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or ()))
print(json.dumps({"total": len(seen), "dupes": [list(k) for k, n in seen.items() if n > 1]}))
"""


@pytest.mark.requires_insight
def test_every_method_and_path_is_served_by_one_route() -> None:
    env = {
        **os.environ,
        "TUVL_DEV_MODE": "true",
        "TUVL_ENV": "development",
        "TUVL_ALLOW_DEV_AUTH": "true",
        "TUVL_DEV_API_KEY": "route-check",
    }
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, check=True
    )
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["total"] > 100  # dev routes are mounted, so the check covers them
    assert report["dupes"] == []
