"""The process side of Insight's command jobs works without a POSIX shell (also run on Windows in CI)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tuvl_insight.api.dev import jobs


def test_the_runner_leaves_the_output_and_the_exit_code_in_files(tmp_path: Path) -> None:
    out = tmp_path / "job.out"
    subprocess.run(
        [
            sys.executable,
            "-c",
            jobs._RUNNER,
            str(out),
            sys.executable,
            "-c",
            "print('hi'); exit(3)",
        ],
        check=True,
    )
    assert out.read_text().strip() == "hi"
    assert Path(f"{out}.exit").read_text() == "3"


def test_a_running_command_is_seen_and_can_be_stopped() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **jobs._detach())
    try:
        assert jobs._alive(proc.pid)
        jobs._terminate(proc.pid)
        proc.wait(timeout=15)
        assert not jobs._alive(proc.pid)
    finally:
        proc.kill()
