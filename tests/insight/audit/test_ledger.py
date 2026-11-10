import json
import threading
from pathlib import Path

import pytest

from tuvl_insight.audit import AuditError, append, emit, read_events, select, verify
from tuvl_insight.audit import ledger as lg

WHO = {"initials": "SK", "git": "Ada <a@b.c>"}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    t = [1_760_000_000.0]

    def tick() -> float:
        t[0] += 1
        return t[0]

    monkeypatch.setattr(lg, "clock", tick)
    return tmp_path


def add(project: Path, kind: str = "spec.approved", **kw) -> dict:
    kw.setdefault("command", "spec approve")
    kw.setdefault("who", WHO)
    return append(project, kind, **kw)


def test_events_are_chained_and_verify(project: Path) -> None:
    first = add(project, entities=["SPEC-SK-0004-01"])
    second = add(project, "tasks.derived", entities=["SPEC-SK-0004-01"])
    assert first["prev"] == "" and second["prev"] == lg._digest(lg._line(first))
    result = verify(project)
    assert result.ok and result.events == 2


def test_ids_sort_in_time_order(project: Path) -> None:
    ids = [add(project)["id"] for _ in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5 and all(len(i) == 26 for i in ids)


def test_an_edited_line_is_found(project: Path) -> None:
    add(project)
    add(project, "tasks.derived")
    add(project, "spec.reopened")
    path = lg.files(project)[0]
    lines = path.read_text().splitlines()
    event = json.loads(lines[1])
    event["detail"] = "changed"
    lines[1] = lg._line(event)
    path.write_text("\n".join(lines) + "\n")
    result = verify(project)
    assert not result.ok and result.where.endswith(":3") and "chain" in result.problem


def test_a_removed_line_is_found(project: Path) -> None:
    for _ in range(3):
        add(project)
    path = lg.files(project)[0]
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not verify(project).ok


def test_reordered_lines_are_found(project: Path) -> None:
    for _ in range(3):
        add(project)
    path = lg.files(project)[0]
    a, b, c = path.read_text().splitlines()
    path.write_text("\n".join([a, c, b]) + "\n")
    assert not verify(project).ok


def test_the_chain_continues_across_months(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add(project)
    monkeypatch.setattr(lg, "clock", lambda: 1_760_000_000.0 + 40 * 86400)
    add(project)
    names = [p.name for p in lg.files(project)]
    assert len(names) == 2
    assert verify(project).ok
    lg.files(project)[0].unlink()  # losing a whole month breaks the chain
    assert not verify(project).ok


def test_unknown_kinds_and_results_are_refused(project: Path) -> None:
    with pytest.raises(AuditError, match="kind"):
        add(project, "made.up")
    with pytest.raises(AuditError, match="result"):
        add(project, result="maybe")


def test_secrets_and_long_values_are_scrubbed(project: Path) -> None:
    event = add(
        project,
        refs={"api_key": "sk-123", "nested": {"password": "p", "ok": "fine"}, "long": "x" * 900},
    )
    assert event["refs"]["api_key"] == "<redacted>"
    assert event["refs"]["nested"] == {"password": "<redacted>", "ok": "fine"}
    assert len(event["refs"]["long"]) == 300
    assert "sk-123" not in lg.files(project)[0].read_text()


def test_emit_never_raises(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    emit(project, "made.up", command="x")  # unknown kind
    monkeypatch.setattr(lg, "_locked", lambda _p: (_ for _ in ()).throw(OSError("read-only")))
    emit(project, "spec.approved", command="x")
    assert read_events(project) == []


def test_select_by_entity_covers_what_derives_from_an_idea(project: Path) -> None:
    add(project, "idea.imported", entities=["IDEA-SK-0004"])
    add(project, "spec.approved", entities=["SPEC-SK-0004-01"])
    add(project, "task.started", entities=["TASK-SK-0004-01-002"])
    add(project, "idea.imported", entities=["IDEA-SK-0005"])
    events = read_events(project)
    assert len(select(events, entity="IDEA-SK-0004")) == 3
    assert len(select(events, entity="SPEC-SK-0004-01")) == 2  # itself and its task
    assert len(select(events, entity="TASK-SK-0004-01-002")) == 1
    assert len(select(events, kind="idea.")) == 2
    assert len(select(events, actor_match="ada")) == 4
    assert select(events, since="2999-01-01") == []


def test_concurrent_writers_keep_the_chain(project: Path) -> None:
    def work() -> None:
        for _ in range(10):
            add(project)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    result = verify(project)
    assert result.ok and result.events == 40


def test_a_stale_lock_from_a_crashed_process_is_cleared(project: Path) -> None:
    import os

    root = lg.audit_dir(project)
    root.mkdir(parents=True)
    lock = root / ".lock"
    lock.write_text("")
    os.utime(lock, (0, 0))
    add(project)
    assert verify(project).ok and not lock.exists()


def test_ids_stay_ordered_inside_one_millisecond_and_after_a_clock_step_back() -> None:
    first = lg._ulid(1_760_000_000.0)
    same_ms = lg._ulid(1_760_000_000.0, after=first)
    earlier = lg._ulid(1_759_999_999.0, after=same_ms)
    assert first < same_ms < earlier
