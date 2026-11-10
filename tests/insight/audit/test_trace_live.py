import json
import os
import time
from pathlib import Path

import pytest

from tuvl_insight.audit import live, trace


def test_trace_blobs_are_stored_once_by_hash(tmp_path: Path) -> None:
    a = trace.store(tmp_path, "the prompt")
    b = trace.store(tmp_path, b"the prompt")
    assert a == b and len(a) == 64
    assert trace.load(tmp_path, a) == b"the prompt"
    assert trace.load(tmp_path, "0" * 64) is None
    assert trace.load(tmp_path, "../../etc/passwd") is None


def test_prune_removes_only_old_files(tmp_path: Path) -> None:
    old = trace.store(tmp_path, "old")
    new = trace.store(tmp_path, "new")
    path = trace.blob_dir(tmp_path) / old
    os.utime(path, (time.time() - 40 * 86400,) * 2)
    assert trace.prune(tmp_path, 30) == 1
    assert trace.load(tmp_path, old) is None and trace.load(tmp_path, new) == b"new"
    assert trace.prune(tmp_path / "nowhere", 30) == 0


def test_live_state_round_trip_and_clear(tmp_path: Path) -> None:
    assert live.read(tmp_path) == {}
    live.set_task(
        tmp_path, "specs/a.md", "T1", "running", attempt=2, max_attempts=3, gate="validate"
    )
    live.set_task(tmp_path, "specs/a.md", "T2", "queued")
    live.set_task(tmp_path, "specs/b.md", "T1", "failed", finding="V012")
    state = live.read(tmp_path)
    assert state["specs/a.md"]["T1"]["attempt"] == 2 and set(state["specs/a.md"]) == {"T1", "T2"}
    live.clear(tmp_path, "specs/a.md")
    assert list(live.read(tmp_path)) == ["specs/b.md"]
    live.clear(tmp_path)
    assert live.read(tmp_path) == {} and not (tmp_path / live.LIVE_FILE).exists()


def test_live_rejects_unknown_states(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        live.set_task(tmp_path, "s", "T1", "exploding")


def test_a_damaged_live_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / live.LIVE_FILE
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert live.read(tmp_path) == {}
    path.write_text(json.dumps([1]))
    assert live.read(tmp_path) == {}


def test_describe() -> None:
    assert (
        live.describe({"state": "running", "attempt": 2, "max_attempts": 3, "gate": "test"})
        == "running 2/3 · test"
    )
    assert live.describe({"state": "running"}) == "running"
    assert live.describe({"state": "failed", "finding": "V012"}) == "failed: V012"
    assert live.describe({"state": "failed"}) == "failed"
    assert live.describe({"state": "in_review", "pr": {"number": 12}}) == "in review #12"
    assert live.describe({"state": "queued"}) == "queued"
