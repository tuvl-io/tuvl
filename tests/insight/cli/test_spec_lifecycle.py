"""`tuvl spec approve | reject | reopen | tasks | list | new --idea` and the lifecycle check rules."""

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight.cli.commands.init import _SAMPLE_TRIAGE_SPEC
from tuvl_insight.coding_agents import Settings, write_settings
from tuvl_insight.spec import ids, lifecycle

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


def spec_file(project: Path, name: str = "triage.md") -> Path:
    return project / "specs" / name


def test_tasks_need_an_approved_spec(project: Path) -> None:
    done = run(project, "generate", "tasks", "triage")
    assert done.exit_code == 1 and "is draft" in done.output
    assert not spec_file(project, "triage.tasks.yaml").exists()


def test_dry_run_previews_without_approval_or_writing(project: Path) -> None:
    done = run(project, "generate", "tasks", "triage", "--dry-run")
    assert done.exit_code == 0
    plan = yaml.safe_load(done.output)
    assert plan["kind"] == "TaskPlan" and plan["spec"]["tasks"]
    assert not spec_file(project, "triage.tasks.yaml").exists()


def test_approve_then_derive_then_check_is_clean(project: Path) -> None:
    approved = run(project, "approve", "spec", "triage")
    assert approved.exit_code == 0, approved.output
    front = lifecycle.split_front(spec_file(project).read_text())[0]
    assert front["status"] == "approved" and front["approved_by"] == "Ada <a@b.c>"
    done = run(project, "generate", "tasks", "triage")
    assert done.exit_code == 0, done.output
    assert "task(s)" in done.output
    check = runner.invoke(app, ["lint", "spec", "-d", str(project), "--json"])
    report = json.loads(check.output[check.output.index("{") :])
    codes = {f["code"] for r in report["specs"] for f in r["findings"]}
    assert not codes & {"P001", "P002", "P003", "P005", "S017"}, codes


def test_the_spec_commands_take_an_id_a_name_or_a_file(project: Path) -> None:
    run(project, "approve", "spec", "triage")
    run(project, "generate", "tasks", "triage")
    spec_id = lifecycle.split_front(spec_file(project).read_text())[0].get("id") or "triage"
    for ref in (str(spec_id), "triage", "specs/triage.md"):
        listed = run(project, "list", "tasks", ref, "--no-tests")
        assert listed.exit_code == 0 and "No task list" not in listed.output, (ref, listed.output)
        assert run(project, "lint", "spec", ref).exception is None
        assert run(project, "verify", "spec", ref, "--no-tests").exception is None
    missing = run(project, "lint", "spec", "nope")
    assert missing.exit_code == 1 and "no spec 'nope'" in missing.output


def test_a_task_shows_what_it_builds_and_the_spec_section_it_came_from(project: Path) -> None:
    run(project, "approve", "spec", "triage")
    run(project, "generate", "tasks", "triage")
    task = json.loads(run(project, "list", "tasks", "triage", "--json", "--no-tests").output)
    first = task["plans"][0]["tasks"][0]["id"]
    shown = run(project, "show", "task", first, "--spec", "triage")
    assert shown.exit_code == 0, shown.output
    assert first in shown.output and "From the spec:" in shown.output
    as_json = json.loads(run(project, "show", "task", first, "--spec", "triage", "--json").output)
    assert as_json["id"] == first and as_json["section"]["text"].strip()
    missing = run(project, "show", "task", "TASK-NOPE-0000-00-000")
    assert missing.exit_code == 1 and "No task" in missing.output


def test_editing_an_approved_spec_goes_stale_and_blocks_tasks(project: Path) -> None:
    run(project, "approve", "spec", "triage")
    run(project, "generate", "tasks", "triage")
    file = spec_file(project)
    file.write_text(file.read_text().replace("# Intent", "# Intent\nA new sentence.", 1))
    listing = json.loads(run(project, "list", "specs", "--json").output)
    assert listing["specs"][0]["stale"] is True
    blocked = run(project, "generate", "tasks", "triage")
    assert blocked.exit_code == 1 and "changed since" in blocked.output
    check = json.loads(runner.invoke(app, ["lint", "spec", "-d", str(project), "--json"]).output)
    assert any(
        f["code"] == "S017" and f["severity"] == "error" for f in check["specs"][0]["findings"]
    )
    assert check["ok"] is False
    assert run(project, "approve", "spec", "triage").exit_code == 0  # renewed


def test_approve_refuses_a_spec_that_fails_its_check(project: Path) -> None:
    file = spec_file(project)
    file.write_text(
        file.read_text().replace("`classify`", "`classify` extra bogus line", 1)
        + "\n# Flows\nnot a flow\n"
    )
    done = run(project, "approve", "spec", "triage")
    assert done.exit_code == 1 and "blocking" in done.output
    assert run(project, "approve", "spec", "triage", "--force").exit_code == 0


def test_reject_and_reopen(project: Path) -> None:
    assert run(project, "reject", "spec", "triage").exit_code == 2  # --reason is required
    run(project, "approve", "spec", "triage")
    assert run(project, "reopen", "spec", "triage").exit_code == 0
    assert lifecycle.state_of(spec_file(project).read_text()).status == "draft"
    assert run(project, "reopen", "spec", "triage").exit_code == 1  # nothing to reopen
    run(project, "approve", "spec", "triage")
    done = run(project, "reject", "spec", "triage", "--reason", "limits missing")
    assert done.exit_code == 0
    assert (
        lifecycle.split_front(spec_file(project).read_text())[0]["review_note"] == "limits missing"
    )


def test_re_running_tasks_keeps_ids_and_the_plan_hash_follows_the_front_matter(
    project: Path,
) -> None:
    run(project, "approve", "spec", "triage")
    run(project, "generate", "tasks", "triage")
    plan = spec_file(project, "triage.tasks.yaml")
    first = yaml.safe_load(plan.read_text())
    run(project, "reopen", "spec", "triage")  # a front-matter-only change
    after = yaml.safe_load(plan.read_text())
    from tuvl_insight.spec.parse import parse_spec

    assert after["metadata"]["spec_hash"] == parse_spec(spec_file(project)).hash
    run(project, "approve", "spec", "triage")
    run(project, "generate", "tasks", "triage")
    again = yaml.safe_load(plan.read_text())
    assert [t["id"] for t in again["spec"]["tasks"]] == [t["id"] for t in first["spec"]["tasks"]]


def test_actions_need_to_know_who_you_are(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl_insight.gitutil.config_get", lambda _p, _k: "")
    (project / ".tuvl" / "agent.yaml").unlink()
    done = run(project, "approve", "spec", "triage")
    assert done.exit_code == 1 and "who you are" in done.output


# ── numbered specs ───────────────────────────────────────────────────────────


@pytest.fixture
def with_idea(project: Path) -> Path:
    brief = project.parent / "brief.md"
    brief.write_text("# Triage\n")
    assert run(project, "import", "idea", str(brief)).exit_code == 0
    return project


def test_new_with_an_idea_numbers_and_links_the_spec(with_idea: Path) -> None:
    done = run(with_idea, "create", "spec", "refunds", "--idea", "IDEA-SK-0001")
    assert done.exit_code == 0, done.output
    file = with_idea / "specs" / "SPEC-SK-0001-01.md"
    front = lifecycle.split_front(file.read_text())[0]
    assert (
        front["id"] == "SPEC-SK-0001-01"
        and front["ideas"] == ["IDEA-SK-0001"]
        and front["status"] == "draft"
    )
    second = run(with_idea, "create", "spec", "more", "--idea", "IDEA-SK-0001")
    assert (with_idea / "specs" / "SPEC-SK-0001-02.md").is_file(), second.output
    assert [r["specs"] for r in ids.read_index(with_idea)] == [
        ["SPEC-SK-0001-01", "SPEC-SK-0001-02"]
    ]
    shown = yaml.safe_load((with_idea / "idea" / "IDEA-SK-0001" / "IDEA.yaml").read_text())
    assert shown["specs"] == ["SPEC-SK-0001-01", "SPEC-SK-0001-02"]


def test_new_with_an_unknown_idea_is_refused(project: Path) -> None:
    assert run(project, "create", "spec", "x", "--idea", "IDEA-SK-0042").exit_code == 1


def test_numbered_specs_derive_numbered_tasks_and_resolve_by_id(with_idea: Path) -> None:
    run(with_idea, "create", "spec", "refunds", "--idea", "IDEA-SK-0001")
    assert run(with_idea, "approve", "spec", "SPEC-SK-0001-01", "--force").exit_code == 0
    assert run(with_idea, "generate", "tasks", "SPEC-SK-0001-01").exit_code == 0
    plan = yaml.safe_load((with_idea / "specs" / "SPEC-SK-0001-01.tasks.yaml").read_text())
    assert plan["spec"]["tasks"][0]["id"] == "TASK-SK-0001-01-001"


def test_lifecycle_check_rules_for_ideas(with_idea: Path) -> None:
    run(with_idea, "create", "spec", "refunds", "--idea", "IDEA-SK-0001")
    file = with_idea / "specs" / "SPEC-SK-0001-01.md"
    file.write_text(
        lifecycle.with_front(file.read_text(), {"ideas": ["IDEA-SK-0001", "IDEA-SK-0099"]})
    )

    def codes() -> set[str]:
        out = runner.invoke(app, ["lint", "spec", str(file), "-d", str(with_idea), "--json"])
        return {
            f["code"]
            for r in json.loads(out.output[out.output.index("{") :])["specs"]
            for f in r["findings"]
        }

    assert "S018" in codes()  # IDEA-SK-0099 does not exist
    file.write_text(lifecycle.with_front(file.read_text(), {"ideas": ["IDEA-SK-0001"]}))
    assert "S018" not in codes()
    (with_idea / "idea" / "IDEA-SK-0001" / "source" / "brief.md").write_text("tampered")
    assert "S021" in codes()  # an idea's files never change after import
    idx = ids.read_index(with_idea)
    idx.append({"id": "IDEA-SK-0002", "supersedes": "IDEA-SK-0001", "specs": []})
    ids.write_index(with_idea, idx)
    assert "S020" in codes()
