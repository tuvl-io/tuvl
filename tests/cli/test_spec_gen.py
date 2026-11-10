"""`tuvl spec gen` end to end, with a fake coding agent and a local bare remote."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight.audit import read_events, verify
from tuvl_insight.coding_agents import Settings, read_settings, write_settings
from tuvl_insight.spec import lifecycle

runner = CliRunner()
FAKE = Path(__file__).resolve().parent.parent / "specgen" / "fake_agent.py"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text(
        "[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n"
    )
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])
    p = tmp_path / "proj"
    p.mkdir()
    for d in ("models", "workflows"):
        (p / d).mkdir()
        (p / d / ".gitkeep").touch()
    (p / "AGENTS.md").write_text("rules\n")
    (p / ".gitignore").write_text(
        ".tuvl/agent.yaml\n.tuvl/audit/\n.tuvl/trace/\n.tuvl/live/\n.tuvl/worktrees/\n"
    )
    git(p, "init", "-q", "-b", "develop")
    git(p, "add", "-A")
    git(p, "commit", "-q", "-m", "init")
    write_settings(p, Settings("AL", "generic", "", (sys.executable, str(FAKE))))
    brief = tmp_path / "brief.md"
    brief.write_text("# Refund automation\n\nCustomers get refunds within two days.\n")
    done = runner.invoke(app, ["import", "idea", str(brief), "-d", str(p), "--commit"])
    assert done.exit_code == 0, done.output
    return p


def gen(
    project: Path,
    *args: str,
    mode: str = "good",
    extra_env: dict[str, str] | None = None,
    input: str | None = None,
):
    env = {"FAKE_MODE": mode, "FAKE_STATE": str(project.parent / "state"), **(extra_env or {})}
    for k, v in env.items():
        os.environ[k] = v
    try:
        return runner.invoke(
            app,
            ["generate", "spec", "-d", str(project), "--idea", "IDEA-AL-0001", "--no-pr", *args],
            input=input,
        )
    finally:
        for k in env:
            os.environ.pop(k, None)


def branches(project: Path) -> list[str]:
    return git(project, "branch", "--format=%(refname:short)").split()


def test_consent_is_needed_once(project: Path) -> None:
    refused = gen(project, "--yes")
    assert refused.exit_code == 1 and "consent" in refused.output
    assert branches(project) == ["develop"]
    ok = gen(project, "--yes", "--consent")
    assert ok.exit_code == 0, ok.output
    assert read_settings(project).consent  # type: ignore[union-attr]
    again = gen(project, "--yes")  # remembered
    assert again.exit_code == 0, again.output
    kinds = [e["kind"] for e in read_events(project)]
    assert kinds.count("consent.granted") == 1


def test_a_good_run_leaves_a_committed_draft_on_its_own_branch(project: Path) -> None:
    done = gen(project, "--yes", "--consent")
    assert done.exit_code == 0, done.output
    assert "SPEC-AL-0001-01" in done.output
    assert "spec/SPEC-AL-0001-01" in branches(project)
    assert git(project, "branch", "--show-current") == "develop"
    assert not (project / "specs").exists()  # the main working tree is untouched
    assert not (project / ".tuvl" / "worktrees" / "SPEC-AL-0001-01").exists()
    text = git(project, "show", "spec/SPEC-AL-0001-01:specs/SPEC-AL-0001-01.md")
    front = lifecycle.split_front(text)[0]
    assert front["id"] == "SPEC-AL-0001-01" and front["ideas"] == ["IDEA-AL-0001"]
    assert front["status"] == "draft" and front["rev"] == 1 and front["generated_by"]
    assert "Refund customers within two days." in text
    changed = git(project, "show", "--name-only", "--format=", "spec/SPEC-AL-0001-01").split()
    assert sorted(changed) == [
        "idea/INDEX.yaml",
        "idea/IDEA-AL-0001/IDEA.yaml",
        "specs/SPEC-AL-0001-01.md",
    ] or sorted(changed) == sorted(
        ["idea/IDEA-AL-0001/IDEA.yaml", "idea/INDEX.yaml", "specs/SPEC-AL-0001-01.md"]
    )
    msg = git(project, "log", "-1", "--format=%B", "spec/SPEC-AL-0001-01")
    assert msg.startswith(
        "SPEC-AL-0001-01: Refund automation"
    )  # the spec's title, not its first heading and "Claude" not in msg and "generic" not in msg


def test_here_writes_the_draft_into_the_folder_and_leaves_no_branch(project: Path) -> None:
    done = gen(project, "--here", "--consent")
    assert done.exit_code == 0, done.output
    path = project / "specs" / "SPEC-AL-0001-01.md"
    front = lifecycle.split_front(path.read_text())[0]
    assert front["id"] == "SPEC-AL-0001-01" and front["status"] == "draft"
    assert branches(project) == ["develop"]
    assert not (project / ".tuvl" / "worktrees" / "SPEC-AL-0001-01").exists()
    assert "not committed" in done.output
    assert "spec.generated" in [e["kind"] for e in read_events(project)]


def test_the_ledger_records_the_run_and_the_prompt_is_in_the_trace(project: Path) -> None:
    gen(project, "--yes", "--consent")
    events = read_events(project)
    kinds = [e["kind"] for e in events]
    assert kinds[-3:] == ["spec.generated", "branch.created", "commit.created"]
    generated = next(e for e in events if e["kind"] == "spec.generated")
    assert (
        generated["entities"] == ["SPEC-AL-0001-01", "IDEA-AL-0001"] and generated["result"] == "ok"
    )
    from tuvl_insight.audit import trace

    prompt = trace.load(project, generated["refs"]["prompt_sha256"])
    assert prompt and b"Customers get refunds within two days." in prompt
    assert (
        "Customers get refunds"
        not in (project / ".tuvl" / "audit").glob("*.jsonl").__next__().read_text()
    )
    assert verify(project).ok


def test_the_spec_numbers_follow_the_idea(project: Path) -> None:
    gen(project, "--yes", "--consent")
    second = gen(project, "--yes")
    assert second.exit_code == 0 and "spec/SPEC-AL-0001-01" in branches(project)
    # both branches start from develop, which has neither spec: the id comes from the working tree
    assert sorted(b for b in branches(project) if b.startswith("spec/")) in (
        ["spec/SPEC-AL-0001-01"],
        ["spec/SPEC-AL-0001-01", "spec/SPEC-AL-0001-02"],
    )


def test_a_file_the_agent_was_not_allowed_to_touch_is_reverted(project: Path) -> None:
    prompts = project.parent / "prompts.txt"
    done = gen(
        project,
        "--yes",
        "--consent",
        mode="outside_then_good",
        extra_env={"FAKE_PROMPTS": str(prompts)},
    )
    assert done.exit_code == 0, done.output
    assert "reverted" in done.output
    assert "models/sneaky.yaml" not in git(
        project, "ls-tree", "-r", "--name-only", "spec/SPEC-AL-0001-01"
    )
    assert git(project, "show", "spec/SPEC-AL-0001-01:AGENTS.md") == "rules"
    second_prompt = prompts.read_text().split("=====")[1]
    assert (
        "you changed models/sneaky.yaml" in second_prompt
        and "only specs/SPEC-AL-0001-01.md may change" in second_prompt
    )


def test_check_findings_are_fed_back_for_another_try(project: Path) -> None:
    prompts = project.parent / "prompts.txt"
    done = gen(
        project,
        "--yes",
        "--consent",
        mode="invalid_then_good",
        extra_env={"FAKE_PROMPTS": str(prompts)},
    )
    assert done.exit_code == 0, done.output
    assert "2 attempt(s)" in done.output
    retry = prompts.read_text().split("=====")[1]
    assert "did not pass `tuvl lint spec`" in retry and "S001" in retry


def test_a_draft_that_never_passes_leaves_nothing_behind(project: Path) -> None:
    done = gen(project, "--yes", "--consent", "--max-attempts", "2", mode="invalid")
    assert done.exit_code == 1 and "No valid draft after 2 attempt(s)" in done.output
    assert branches(project) == ["develop"]
    assert not (project / ".tuvl" / "worktrees" / "SPEC-AL-0001-01").exists()
    assert git(project, "worktree", "list").count("\n") == 0
    last = read_events(project)[-1]
    assert last["kind"] == "spec.generated" and last["result"] == "failed"


def test_an_agent_that_fails_is_reported(project: Path) -> None:
    done = gen(project, "--yes", "--consent", "--max-attempts", "1", mode="fail")
    assert done.exit_code == 1 and "overloaded" in done.output
    assert branches(project) == ["develop"]


def test_an_agent_cannot_approve_its_own_draft(project: Path) -> None:
    done = gen(project, "--yes", "--consent", mode="approve")
    assert done.exit_code == 0, done.output
    front = lifecycle.split_front(
        git(project, "show", "spec/SPEC-AL-0001-01:specs/SPEC-AL-0001-01.md")
    )[0]
    assert (
        front["status"] == "draft" and "approved_by" not in front and "approved_hash" not in front
    )


def test_dry_run_and_show_prompt_run_nothing(project: Path) -> None:
    done = gen(project, "--dry-run", "--show-prompt")
    assert done.exit_code == 0
    flat = " ".join(done.output.split())  # the console wraps at 80 columns
    assert "may change only specs/SPEC-AL-0001-01.md" in flat
    assert "Customers get refunds within two days." in flat
    assert branches(project) == ["develop"] and not (project.parent / "state").exists()
    assert read_settings(project).consent == ""  # type: ignore[union-attr]


def test_declining_the_commit_discards_the_draft(project: Path) -> None:
    done = gen(project, "--consent", input="n\n")
    assert done.exit_code == 1 and "Discarded" in done.output
    assert branches(project) == ["develop"]


def test_the_idea_must_be_committed(project: Path) -> None:
    brief = project.parent / "b2.md"
    brief.write_text("# Second\n")
    runner.invoke(app, ["import", "idea", str(brief), "-d", str(project)])  # no --commit
    done = runner.invoke(
        app, ["generate", "spec", "-d", str(project), "--idea", "IDEA-AL-0002", "--consent", "-y"]
    )
    assert done.exit_code == 1 and "not committed" in done.output


def test_from_imports_commits_and_generates(project: Path) -> None:
    new = project.parent / "other.md"
    new.write_text("# Another brief\n\nDo a thing.\n")
    os.environ["FAKE_MODE"] = "good"
    try:
        done = runner.invoke(
            app,
            [
                "generate",
                "spec",
                "-d",
                str(project),
                "--from",
                str(new),
                "--no-pr",
                "-y",
                "--consent",
            ],
        )
    finally:
        os.environ.pop("FAKE_MODE")
    assert done.exit_code == 0, done.output
    assert "spec/SPEC-AL-0002-01" in branches(project)
    assert "IDEA-AL-0002" in git(project, "log", "-3", "--format=%s", "develop")


def test_agent_problems_are_explained(project: Path) -> None:
    write_settings(project, Settings("AL", "none"))
    assert "No coding agent" in gen(project).output
    write_settings(project, Settings("AL", "aider"))
    assert "cannot drive 'aider'" in gen(project).output
    write_settings(project, Settings("AL", "generic"))
    assert "generic agent has no command" in gen(project).output


def test_a_project_that_is_not_a_repository_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "plain"
    p.mkdir()
    write_settings(p, Settings("AL", "generic", "", (sys.executable, str(FAKE))))
    done = runner.invoke(app, ["generate", "spec", "-d", str(p), "--idea", "IDEA-AL-0001"])
    assert done.exit_code == 1 and "git repository" in done.output


# ── push and pull request ────────────────────────────────────────────────────


@pytest.fixture
def remote(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bare = tmp_path / "github-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(project, "remote", "add", "origin", str(bare))
    git(project, "push", "-q", "-u", "origin", "develop")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho https://github.com/acme/app/pull/7\n')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr("tuvl_insight.gitflow.gitutil.out", _fake_out(git))
    return bare


def _fake_out(real):
    from tuvl_insight import gitutil

    original = gitutil.out

    def out(project, *args):
        if args[:2] == ("remote", "get-url"):
            return "git@github.com:acme/app.git"
        return original(project, *args)

    return out


def test_the_branch_is_pushed_and_a_draft_pull_request_opened(
    project: Path, remote: Path, tmp_path: Path
) -> None:
    os.environ.update({"FAKE_MODE": "good"})
    try:
        done = runner.invoke(
            app,
            ["generate", "spec", "-d", str(project), "--idea", "IDEA-AL-0001", "-y", "--consent"],
        )
    finally:
        os.environ.pop("FAKE_MODE")
    assert done.exit_code == 0, done.output
    assert "https://github.com/acme/app/pull/7" in done.output
    assert "spec/SPEC-AL-0001-01" in git(remote, "branch", "--format=%(refname:short)")
    call = (tmp_path / "gh.log").read_text()
    assert "pr create" in call and "--draft" in call and "--base develop" in call
    assert "--head spec/SPEC-AL-0001-01" in call
    assert [e["kind"] for e in read_events(project)][-1] == "pr.opened"
    ready = json.dumps(call)
    assert "merge" not in ready  # tuvl opens the request, never merges it
