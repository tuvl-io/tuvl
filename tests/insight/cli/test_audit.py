"""`tuvl audit`, the events the commands record, and `spec status --watch`."""

import csv
import io
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight.audit import ledger as lg
from tuvl_insight.audit import live, read_events, verify
from tuvl_insight.cli.commands.init import _SAMPLE_TRIAGE_SPEC
from tuvl_insight.coding_agents import Settings, write_settings

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "proj"
    (p / "specs").mkdir(parents=True)
    write_settings(p, Settings("SK", "none"))
    monkeypatch.setattr(
        "tuvl_insight.gitutil.config_get",
        lambda _p, key: {"user.name": "Ada", "user.email": "a@b.c"}.get(key, ""),
    )
    (p / "specs" / "triage.md").write_text(_SAMPLE_TRIAGE_SPEC)
    return p


def run(project: Path, *args: str):
    return runner.invoke(app, [*args, "-d", str(project)])


def kinds(project: Path) -> list[tuple[str, str]]:
    return [(e["kind"], e["result"]) for e in read_events(project)]


def test_a_workflow_of_commands_leaves_a_verified_trail(project: Path, tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("# Triage\n")
    assert run(project, "import", "idea", str(brief)).exit_code == 0
    assert run(project, "create", "spec", "refunds", "--idea", "IDEA-SK-0001").exit_code == 0
    assert run(project, "generate", "tasks", "SPEC-SK-0001-01").exit_code == 1  # still a draft
    assert run(project, "approve", "spec", "SPEC-SK-0001-01", "--force").exit_code == 0
    assert run(project, "generate", "tasks", "SPEC-SK-0001-01").exit_code == 0
    assert run(project, "reopen", "spec", "SPEC-SK-0001-01").exit_code == 0
    assert run(project, "approve", "spec", "SPEC-SK-0001-01", "--force").exit_code == 0
    assert run(project, "reject", "spec", "SPEC-SK-0001-01", "--reason", "limits").exit_code == 0
    assert run(project, "approve", "spec", "SPEC-SK-0001-01", "--force").exit_code == 0
    assert (
        run(project, "approve", "spec", "SPEC-SK-0001-01", "--force").exit_code == 1
    )  # already approved
    assert run(project, "renumber", "idea", "IDEA-SK-0001").exit_code == 0
    assert kinds(project) == [
        ("idea.imported", "ok"),
        ("spec.created", "ok"),
        ("tasks.derived", "refused"),
        ("spec.approved", "ok"),
        ("tasks.derived", "ok"),
        ("spec.reopened", "ok"),
        ("spec.approved", "ok"),
        ("spec.rejected", "ok"),
        ("spec.approved", "ok"),
        ("spec.approved", "refused"),
        ("id.renumbered", "ok"),
    ]
    assert verify(project).ok
    first = read_events(project)[0]
    assert first["actor"] == {"initials": "SK", "git": "Ada <a@b.c>"}
    assert first["entities"] == ["IDEA-SK-0001"]


def test_a_refusal_because_of_check_errors_records_the_codes(project: Path) -> None:
    file = project / "specs" / "triage.md"
    file.write_text(file.read_text() + "\n# Flows\nnot a flow\n")
    assert run(project, "approve", "spec", "triage").exit_code == 1
    event = read_events(project)[-1]
    assert event["result"] == "refused" and event["refs"]["codes"]
    assert event["entities"] == ["specs/triage.md"]  # a spec without an id is named by its path


def test_agent_use_is_recorded(project: Path) -> None:
    assert run(project, "use", "agent", "none").exit_code == 0
    assert kinds(project) == [("agent.selected", "ok")]


def test_init_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])
    done = runner.invoke(app, ["init", str(tmp_path / "new"), "-y", "--no-git"])
    assert done.exit_code == 0, done.output
    events = read_events(tmp_path / "new")
    assert [e["kind"] for e in events] == ["init.completed"]
    assert events[0]["detail"] == "new"


def test_log_filters_and_limits(project: Path) -> None:
    for kind, entity in [
        ("idea.imported", "IDEA-SK-0004"),
        ("spec.approved", "SPEC-SK-0004-01"),
        ("idea.imported", "IDEA-SK-0005"),
    ]:
        lg.append(project, kind, command="x", entities=[entity], who={"initials": "SK", "git": ""})
    everything = json.loads(run(project, "show", "audit", "--json").output)["events"]
    assert len(everything) == 3
    by_idea = json.loads(
        run(project, "show", "audit", "--entity", "IDEA-SK-0004", "--json").output
    )["events"]
    assert [e["kind"] for e in by_idea] == ["idea.imported", "spec.approved"]
    newest = json.loads(run(project, "show", "audit", "-n", "1", "--json").output)["events"]
    assert newest[0]["entities"] == ["IDEA-SK-0005"]
    assert "spec.approved" in run(project, "show", "audit", "--kind", "spec.").output
    assert run(project, "show", "audit", "--since", "nonsense").exit_code == 2
    assert run(project, "show", "audit", "--since", "1d").exit_code == 0


def test_an_empty_ledger_says_so(project: Path) -> None:
    assert "No events" in run(project, "show", "audit").output


def test_show_by_event_id_prefix_and_by_entity(project: Path) -> None:
    event = lg.append(project, "spec.approved", command="x", entities=["SPEC-SK-0004-01"], who={})
    by_id = run(project, "show", "event", event["id"][:8])
    assert json.loads(by_id.output)["id"] == event["id"]
    assert run(project, "show", "event", "SPEC-SK-0004-01").exit_code == 0
    assert run(project, "show", "event", "SPEC-SK-9999-01").exit_code == 1


def test_verify_exits_1_on_a_tampered_ledger(project: Path) -> None:
    for _ in range(3):
        lg.append(project, "spec.approved", command="x", who={})
    assert run(project, "verify", "audit").exit_code == 0
    path = lg.files(project)[0]
    path.write_text(path.read_text().replace('"result":"ok"', '"result":"failed"', 1))
    bad = run(project, "verify", "audit")
    assert bad.exit_code == 1 and "chain" in bad.output
    doctor = runner.invoke(
        app, ["check", "system", "-d", str(project), "--json", "--only", "audit"]
    )
    assert doctor.exit_code == 1


def test_export_csv_and_json(project: Path, tmp_path: Path) -> None:
    lg.append(
        project,
        "idea.imported",
        command="spec import",
        entities=["IDEA-SK-0004"],
        detail="brief",
        who={"git": "Ada"},
    )
    csv_out = run(project, "export", "audit", "--format", "csv").output
    rows = list(csv.reader(io.StringIO(csv_out)))
    assert rows[0][:5] == ["id", "ts", "actor", "command", "kind"] and rows[1][4] == "idea.imported"
    target = tmp_path / "out.json"
    assert run(project, "export", "audit", "-o", str(target)).exit_code == 0
    assert json.loads(target.read_text())["events"][0]["entities"] == ["IDEA-SK-0004"]
    assert run(project, "export", "audit", "--format", "xml").exit_code == 2


def test_prune_touches_the_trace_only(project: Path) -> None:
    import os
    import time

    from tuvl_insight.audit import trace

    digest = trace.store(project, "prompt")
    os.utime(trace.blob_dir(project) / digest, (time.time() - 90 * 86400,) * 2)
    lg.append(project, "spec.approved", command="x", who={})
    assert "removed 1" in run(project, "prune", "traces", "--older-than", "30").output
    assert verify(project).ok and verify(project).events == 1


# ── spec status --watch ──────────────────────────────────────────────────────


def test_watch_shows_live_progress_over_the_derived_status(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(project, "approve", "spec", "triage", "--force")
    run(project, "generate", "tasks", "triage")
    plan = yaml.safe_load((project / "specs" / "triage.tasks.yaml").read_text())
    task = plan["spec"]["tasks"][1]["id"]
    live.set_task(
        project, "specs/triage.md", task, "running", attempt=2, max_attempts=3, gate="validate"
    )
    calls = {"n": 0}

    def pause(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("tuvl_insight.cli.commands.spec._pause", pause)
    done = runner.invoke(app, ["list", "tasks", "--watch", "--no-tests", "-d", str(project)])
    assert done.exit_code == 0, done.output
    flat = " ".join(done.output.split())  # the console wraps at 80 columns
    assert "running 2/3 · validate" in flat
    assert "specs/triage.md" in flat and "watching" in flat
