"""`tuvl codegen --spec`: scaffold, then one stacked branch per task, with a fake agent."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight.audit import live, read_events
from tuvl_insight.coding_agents import Settings, write_settings

runner = CliRunner()
FAKE = Path(__file__).resolve().parent.parent / "specgen" / "fake_agent.py"
SPEC_ID = "SPEC-AL-0001-01"

TWO_STEPS = (
    "1. `save` — tool db create Item · in `title` · out `item` (Item)\n   - conflict → END.failed\n"
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def cli(project: Path, *args: str, mode: str = "good", env: dict[str, str] | None = None):
    merged = {"FAKE_MODE": mode, "FAKE_STATE": str(project.parent / "state"), **(env or {})}
    os.environ.update(merged)
    try:
        return runner.invoke(app, [*args, "-d", str(project)])
    finally:
        for k in merged:
            os.environ.pop(k, None)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text(
        "[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n"
    )
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])
    p = tmp_path / "proj"
    p.mkdir()
    for d in ("models", "workflows", "agents", "artifacts", "llms", "datasources"):
        (p / d).mkdir()
        (p / d / ".gitkeep").touch()
    (p / ".gitignore").write_text(
        ".tuvl/agent.yaml\n.tuvl/audit/\n.tuvl/trace/\n.tuvl/live/\n.tuvl/worktrees/\n"
    )
    git(p, "init", "-q", "-b", "develop")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "init")
    write_settings(p, Settings("AL", "generic", "", (sys.executable, str(FAKE))))
    brief = tmp_path / "brief.md"
    brief.write_text("# Items\n\nKeep items and a note per item.\n")
    assert cli(p, "import", "idea", str(brief), "--commit").exit_code == 0
    assert cli(p, "create", "spec", "items", "--idea", "IDEA-AL-0001").exit_code == 0
    spec = p / "specs" / f"{SPEC_ID}.md"
    text = spec.read_text()
    text = text.replace(
        TWO_STEPS,
        TWO_STEPS
        + "   - default → `note`\n2. `note` — tool db create Note · in `title` · out `note` (Note)\n"
        + "   - conflict → END.failed\n   - default → END\n",
    )
    item = next(ln for ln in text.splitlines() if ln.startswith("- **Item**"))
    text = text.replace(item, item + "\n- **Note** (new) — `title` (string; required)", 1)
    spec.write_text(text)
    assert cli(p, "approve", "spec", SPEC_ID).exit_code == 0
    assert cli(p, "generate", "tasks", SPEC_ID).exit_code == 0
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "the spec")
    return p


def branches(project: Path) -> list[str]:
    return git(project, "branch", "--format=%(refname:short)").split()


def tasks(project: Path) -> dict[str, dict]:
    plan = yaml.safe_load((project / "specs" / f"{SPEC_ID}.tasks.yaml").read_text())
    return {t["id"]: t for t in plan["spec"]["tasks"]}


def agent_task_ids(project: Path) -> list[str]:
    return [i for i, t in tasks(project).items() if t["type"] == "agent"]


def codegen(project: Path, *args: str, mode: str = "good", env: dict[str, str] | None = None):
    return cli(
        project,
        "generate",
        "code",
        "--spec",
        SPEC_ID,
        "--no-pr",
        "--consent",
        *args,
        mode=mode,
        env=env,
    )


def test_the_scaffold_alone_needs_no_agent(project: Path) -> None:
    done = codegen(project, "--no-agent")
    assert done.exit_code == 0, done.output
    assert branches(project) == ["develop", f"feature/{SPEC_ID}-scaffold"]
    files = git(project, "ls-tree", "-r", "--name-only", f"feature/{SPEC_ID}-scaffold").split()
    assert {
        "models/item.yaml",
        "models/note.yaml",
        "workflows/items.yaml",
        "agents/_generated/schemas.py",
    } <= set(files)
    assert not (project / "models" / "item.yaml").exists()  # the main working tree is untouched
    wf = yaml.safe_load(git(project, "show", f"feature/{SPEC_ID}-scaffold:workflows/items.yaml"))
    assert [a["engine"] for a in wf["spec"]["agents"]] == ["pending", "pending"]
    assert wf["spec"]["agents"][0]["routes"]["conflict"] == "END.failed"
    assert wf["spec"]["agents"][0]["routes"]["default"] == "note"
    kinds = [e["kind"] for e in read_events(project)]
    assert "codegen.deterministic" in kinds and "task.started" not in kinds


def test_naming_no_tasks_is_an_error_unless_scaffolding_only(project: Path) -> None:
    done = cli(project, "generate", "code", "--spec", SPEC_ID, "--consent")
    assert done.exit_code == 2 and "Name the tasks" in done.output


def test_each_task_gets_a_branch_stacked_on_the_one_before(project: Path) -> None:
    done = codegen(project, "--all")
    assert done.exit_code == 0, done.output
    first, second = agent_task_ids(project)
    scaffold = f"feature/{SPEC_ID}-scaffold"
    assert {scaffold, f"feature/{first}", f"feature/{second}"} <= set(branches(project))
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", "develop", scaffold], cwd=project
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", scaffold, f"feature/{first}"], cwd=project
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"feature/{first}", f"feature/{second}"],
            cwd=project,
        ).returncode
        == 0
    )
    final = yaml.safe_load(git(project, "show", f"feature/{second}:workflows/items.yaml"))
    assert [a["engine"] for a in final["spec"]["agents"]] == ["tool", "tool"]
    one = yaml.safe_load(git(project, "show", f"feature/{first}:workflows/items.yaml"))
    assert [a["engine"] for a in one["spec"]["agents"]] == [
        "tool",
        "pending",
    ]  # the second step waits for its own task
    changed = git(project, "show", "--name-only", "--format=", f"feature/{first}").split()
    assert "workflows/items.yaml" in changed and all(
        not p.startswith(("specs/", "idea/", "models/")) for p in changed
    )
    msg = git(project, "log", "-1", "--format=%s", f"feature/{first}")
    assert msg.startswith(first)


def test_the_ui_spec_of_a_workflow_lands_with_the_task_that_completes_it(project: Path) -> None:
    done = codegen(project, "--all")
    assert done.exit_code == 0, done.output
    first, second = agent_task_ids(project)
    after_first = git(project, "show", "--name-only", "--format=", f"feature/{first}").split()
    after_second = git(project, "show", "--name-only", "--format=", f"feature/{second}").split()
    assert "ui/specs/README.md" in after_first  # the index is always current
    assert "ui/specs/items.md" not in after_first  # the second step is still pending
    assert "ui/specs/items.md" in after_second  # the workflow is whole now
    text = git(project, "show", f"feature/{second}:ui/specs/items.md")
    assert "@tuvl/client" in text and "# UI spec — `items`" in text


def test_the_ledger_and_live_state_follow_the_run(project: Path) -> None:
    codegen(project, "--all")
    events = read_events(project)
    kinds = [e["kind"] for e in events]
    for expected in (
        "codegen.started",
        "codegen.deterministic",
        "task.started",
        "task.attempt",
        "gate.result",
        "task.implemented",
        "commit.created",
    ):
        assert expected in kinds, expected
    assert kinds.count("task.implemented") == 2
    state = live.read(project)[f"specs/{SPEC_ID}.md"]
    assert {v["state"] for v in state.values()} == {"implemented"} and len(state) == 2


def test_a_file_outside_the_task_is_reverted_and_the_agent_told(project: Path) -> None:
    prompts = project.parent / "prompts.txt"
    first = agent_task_ids(project)[0]
    done = codegen(
        project, "--task", first, mode="stray_then_implement", env={"FAKE_PROMPTS": str(prompts)}
    )
    assert done.exit_code == 0, done.output
    assert "specs/stray.md" not in git(project, "ls-tree", "-r", "--name-only", f"feature/{first}")
    assert "you changed specs/stray.md" in prompts.read_text().split("=====")[1]
    assert "scope.violation" in [e["kind"] for e in read_events(project)]


def test_a_task_that_never_passes_leaves_no_branch_and_stops_the_run(project: Path) -> None:
    first, second = agent_task_ids(project)
    done = codegen(project, "--all", "--max-attempts", "2", mode="break")
    assert done.exit_code == 1
    assert f"feature/{first}" not in branches(project) and f"feature/{second}" not in branches(
        project
    )
    assert "stopped after a failed task" in done.output
    assert [e["kind"] for e in read_events(project)].count("task.failed") == 1
    assert live.read(project)[f"specs/{SPEC_ID}.md"][first]["state"] == "failed"
    assert live.read(project)[f"specs/{SPEC_ID}.md"][second]["state"] == "blocked"
    assert git(project, "worktree", "list").count("\n") == 0


def test_a_failed_task_keeps_a_report_of_every_attempt(project: Path) -> None:
    first, _ = agent_task_ids(project)
    done = codegen(project, "--all", "--max-attempts", "2", mode="break")
    assert "full report: tuvl show task" in " ".join(done.output.split())
    report = live.read_failures(project)[f"specs/{SPEC_ID}.md"][first]
    assert [a["attempt"] for a in report["attempts"]] == [1, 2]
    assert report["attempts"][0]["gate"] == "validate" and report["attempts"][0]["problems"]
    assert report["summary"] and report["max_attempts"] == 2
    live.clear(project, f"specs/{SPEC_ID}.md")  # the next run wipes the live state, not the report
    shown = cli(project, "show", "task", first)
    assert shown.exit_code == 0 and "Last run failed" in shown.output
    assert "failed the validate check" in shown.output
    status = json.loads(cli(project, "list", "tasks", "--no-tests", "--json").output)
    task = next(t for t in status["plans"][0]["tasks"] if t["id"] == first)
    assert task["failure"]["summary"] and task["live"]["state"] == "failed"
    assert codegen(project, "--task", first).exit_code == 0  # a later success clears it
    assert live.read_failures(project) == {}


def test_findings_are_fed_back_for_another_attempt(project: Path) -> None:
    prompts = project.parent / "prompts.txt"
    first = agent_task_ids(project)[0]
    done = codegen(
        project, "--task", first, mode="break_then_implement", env={"FAKE_PROMPTS": str(prompts)}
    )
    assert done.exit_code == 0, done.output
    retry = prompts.read_text().split("=====")[1]
    assert "not accepted yet" in retry


def test_task_numbers_may_be_given_in_any_form(project: Path) -> None:
    first, second = agent_task_ids(project)
    number = str(int(first.rsplit("-", 1)[1]))
    done = codegen(project, "--task", f"T{number}")
    assert done.exit_code == 0, done.output
    assert f"feature/{first}" in branches(project) and f"feature/{second}" not in branches(project)
    assert codegen(project, "--task", "999").exit_code == 1


def test_what_is_already_built_is_skipped(project: Path) -> None:
    assert codegen(project, "--all").exit_code == 0
    git(
        project, "merge", "-q", "--no-edit", f"feature/{agent_task_ids(project)[1]}"
    )  # the stack lands on develop
    before = branches(project)
    again = codegen(project, "--all")
    assert again.exit_code == 0, again.output
    assert "already" in again.output
    assert set(branches(project)) <= set(before)  # nothing new; merged labels are cleaned up


def test_the_spec_must_be_approved_and_have_tasks(project: Path) -> None:
    spec = project / "specs" / f"{SPEC_ID}.md"
    spec.write_text(spec.read_text().replace("# Intent", "# Intent\nEdited after approval.", 1))
    git(project, "commit", "-qam", "edit")
    done = codegen(project, "--all")
    assert done.exit_code == 1 and "not approved" in " ".join(done.output.split())


def test_the_spec_needs_its_task_list(project: Path) -> None:
    (project / "specs" / f"{SPEC_ID}.tasks.yaml").unlink()
    git(project, "commit", "-qam", "no tasks")
    done = codegen(project, "--all")
    assert done.exit_code == 1 and "no task list" in " ".join(done.output.split())


def test_the_spec_must_be_on_the_base_branch(project: Path) -> None:
    git(project, "branch", "early", "HEAD~1")
    done = codegen(project, "--all", "--base", "early")
    assert done.exit_code == 1 and "is not on early" in done.output


def test_consent_is_needed(project: Path) -> None:
    done = cli(project, "generate", "code", "--spec", SPEC_ID, "--all", "--no-pr")
    assert done.exit_code == 1 and "consent" in done.output
    assert branches(project) == ["develop"]


# ── pull requests ────────────────────────────────────────────────────────────


@pytest.fixture
def remote(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bare = tmp_path / "github-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(project, "remote", "add", "origin", str(bare))
    git(project, "push", "-q", "-u", "origin", "develop")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\necho "$@" | tr "\\n" " " >> {tmp_path}/gh.log\necho >> {tmp_path}/gh.log\n'
        f"n=$(cat {tmp_path}/gh.count 2>/dev/null || echo 0); n=$((n+1)); echo $n > {tmp_path}/gh.count\n"
        "echo https://github.com/acme/app/pull/$n\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    from tuvl_insight import gitutil

    real = gitutil.out
    monkeypatch.setattr(
        gitutil,
        "out",
        lambda p, *a: (
            "git@github.com:acme/app.git" if a[:2] == ("remote", "get-url") else real(p, *a)
        ),
    )
    return bare


def test_pull_requests_are_opened_in_a_stack_and_never_merged(
    project: Path, remote: Path, tmp_path: Path
) -> None:
    os.environ["FAKE_MODE"] = "good"
    try:
        done = runner.invoke(
            app, ["generate", "code", "--spec", SPEC_ID, "--all", "--consent", "-d", str(project)]
        )
    finally:
        os.environ.pop("FAKE_MODE")
    assert done.exit_code == 0, done.output
    first, second = agent_task_ids(project)
    calls = (tmp_path / "gh.log").read_text().splitlines()
    assert len(calls) == 3 and all("pr create" in c and "--draft" in c for c in calls)
    scaffold = f"feature/{SPEC_ID}-scaffold"
    assert f"--base develop --head {scaffold}" in calls[0]
    assert f"--base {scaffold} --head feature/{first}" in calls[1]
    assert f"--base feature/{first} --head feature/{second}" in calls[2]
    assert "pr merge" not in " ".join(calls)  # tuvl opens requests, never merges them
    pushed = git(remote, "branch", "--format=%(refname:short)").split()
    assert {scaffold, f"feature/{first}", f"feature/{second}"} <= set(pushed)
    state = live.read(project)[f"specs/{SPEC_ID}.md"]
    assert state[first]["state"] == "in_review" and state[first]["pr"]["number"] == 2
    assert [e["kind"] for e in read_events(project)].count("pr.opened") == 3


def test_a_spec_written_in_place_rides_on_the_scaffold_branch(project: Path) -> None:
    git(project, "rm", "-q", "--cached", f"specs/{SPEC_ID}.md", f"specs/{SPEC_ID}.tasks.yaml")
    git(project, "commit", "-qm", "the spec was never committed")
    done = codegen(project, "--no-agent")
    assert done.exit_code == 0, done.output
    files = git(project, "ls-tree", "-r", "--name-only", f"feature/{SPEC_ID}-scaffold").split()
    assert {f"specs/{SPEC_ID}.md", f"specs/{SPEC_ID}.tasks.yaml", "models/item.yaml"} <= set(files)


def test_a_second_scaffold_run_says_the_branch_is_waiting_to_be_merged(project: Path) -> None:
    assert codegen(project, "--no-agent").exit_code == 0
    again = codegen(project, "--no-agent")
    text = " ".join(again.output.split())
    assert again.exit_code == 0 and "not merged yet" in text
    assert f"feature/{SPEC_ID}-scaffold" in text and "Nothing to scaffold" not in text


def test_the_branch_report_shows_the_stack_and_what_to_merge_next(project: Path) -> None:
    first, second = agent_task_ids(project)

    def stack() -> dict:
        out = cli(project, "list", "branches", "--json").output
        data = json.loads(out[out.index("{") :])
        return {e["branch"]: e for e in data["specs"][0]["stack"]} | {"_": data["specs"][0]}

    before = stack()
    assert {before[b]["state"] for b in (f"feature/{SPEC_ID}-scaffold", f"feature/{first}")} == {
        "none"
    }
    assert codegen(project, "--all").exit_code == 0
    built = stack()
    scaffold = built[f"feature/{SPEC_ID}-scaffold"]
    assert (
        scaffold["state"] == "open"
        and scaffold["ahead"] >= 1
        and scaffold["next"].startswith("git merge")
    )
    assert built[f"feature/{first}"]["merge_first"] == [f"feature/{SPEC_ID}-scaffold"]
    assert built[f"feature/{second}"]["next"] == f"merge feature/{first} first"
    assert built["_"]["merge_next"] == f"feature/{SPEC_ID}-scaffold"
    git(project, "merge", "-q", "--no-edit", f"feature/{second}")  # the stack lands on develop
    git(project, "branch", "-q", "-D", f"feature/{first}")
    landed = stack()
    assert landed[f"feature/{second}"]["state"] == "merged"
    assert landed[f"feature/{first}"]["state"] == "merged" and landed[f"feature/{first}"]["deleted"]
    assert landed["_"]["merge_next"] is None
    shown = cli(project, "list", "branches")
    assert (
        shown.exit_code == 0
        and "merged" in shown.output
        and "tuvl never merges" not in shown.output
    )


def test_branches_built_apart_show_as_parallel_and_list_their_commits(project: Path) -> None:
    first, second = agent_task_ids(project)
    assert codegen(project, "--task", second).exit_code == 0
    assert codegen(project, "--task", first).exit_code == 0
    out = cli(project, "list", "branches", "--json").output
    stack = {e["branch"]: e for e in json.loads(out[out.index("{") :])["specs"][0]["stack"]}
    a, b = stack[f"feature/{first}"], stack[f"feature/{second}"]
    scaffold = f"feature/{SPEC_ID}-scaffold"
    assert a["parent"] == scaffold and b["parent"] == scaffold
    assert a["parallel_with"] == [b["branch"]] and b["parallel_with"] == [a["branch"]]
    assert a["commits"] and a["commits"][0]["subject"].startswith(first)
    assert stack[scaffold]["parent"] == "develop" and stack[scaffold]["parallel_with"] == []
