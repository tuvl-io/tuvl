"""`tuvl agent` and the coding-agent part of `tuvl init`."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tuvl_insight.coding_agents import Settings, read_settings, write_settings

ENTRY = "from tuvl.cli import main; main()"


def tuvl(*args: str, path: str, cwd: Path, stdin: str = "") -> subprocess.CompletedProcess[str]:
    home = cwd / ".home"
    home.mkdir(exist_ok=True)
    (home / ".gitconfig").write_text("[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n")
    env = {
        "PATH": path,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "TUVL_TOOLCHAIN": "1",
        "NO_COLOR": "1",
        "COLUMNS": "200",
        **({"PYTHONPATH": os.environ["PYTHONPATH"]} if "PYTHONPATH" in os.environ else {}),
    }
    return subprocess.run(
        [sys.executable, "-c", ENTRY, *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
        cwd=cwd,
        check=False,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "p").mkdir()
    return tmp_path / "p"


def test_list_json_marks_installed_and_selected(agent_bin, project: Path) -> None:
    agent_bin("claude", version="2.0 (Claude Code)")
    write_settings(project, Settings("AL", "claude"))
    done = tuvl("list", "agents", "-d", str(project), "--json", path=agent_bin.path, cwd=project)
    data = json.loads(done.stdout)
    rows = {r["id"]: r for r in data["agents"]}
    assert data["selected"] == "claude"
    assert rows["claude"]["installed"] and rows["claude"]["selected"] and rows["claude"]["verified"]
    assert not rows["codex"]["installed"]


def test_use_unknown_and_missing_agents_are_refused(agent_bin, project: Path) -> None:
    unknown = tuvl("use", "agent", "nope", "-d", str(project), path=agent_bin.path, cwd=project)
    assert unknown.returncode == 2
    missing = tuvl("use", "agent", "codex", "-d", str(project), path=agent_bin.path, cwd=project)
    assert missing.returncode == 1
    assert read_settings(project) is None


def test_use_records_the_choice_and_keeps_the_initials(agent_bin, project: Path) -> None:
    agent_bin("codex")
    write_settings(project, Settings("AL", "none"))
    done = tuvl("use", "agent", "codex", "-d", str(project), path=agent_bin.path, cwd=project)
    assert done.returncode == 0
    assert read_settings(project) == Settings("AL", "codex")
    tuvl("use", "agent", "none", "-d", str(project), path=agent_bin.path, cwd=project)
    assert read_settings(project) == Settings("AL", "none")


def test_use_force_selects_an_agent_that_is_not_installed(agent_bin, project: Path) -> None:
    done = tuvl(
        "use", "agent", "gemini", "--force", "-d", str(project), path=agent_bin.path, cwd=project
    )
    assert done.returncode == 0
    assert read_settings(project).agent == "gemini"  # type: ignore[union-attr]


def test_an_unverified_agent_is_recorded_with_a_warning(agent_bin, project: Path) -> None:
    agent_bin("aider")
    done = tuvl("use", "agent", "aider", "-d", str(project), path=agent_bin.path, cwd=project)
    assert done.returncode == 0
    assert "cannot run it yet" in done.stdout


def test_test_needs_a_selected_agent_that_is_installed(agent_bin, project: Path) -> None:
    assert (
        tuvl(
            "check", "agent", "-d", str(project), "-y", path=agent_bin.path, cwd=project
        ).returncode
        == 1
    )
    write_settings(project, Settings("AL", "claude"))
    missing = tuvl("check", "agent", "-d", str(project), "-y", path=agent_bin.path, cwd=project)
    assert missing.returncode == 1 and "not found" in missing.stdout
    write_settings(project, Settings("AL", "codex"))
    assert (
        tuvl(
            "check", "agent", "-d", str(project), "-y", path=agent_bin.path, cwd=project
        ).returncode
        == 1
    )


def test_generic_agent_selection_and_a_real_trial_run(tmp_path: Path, monkeypatch) -> None:
    import os
    import sys

    from typer.testing import CliRunner

    from tuvl.cli import app

    fake = Path(__file__).resolve().parent.parent / "specgen" / "fake_agent.py"
    run = CliRunner()
    project = tmp_path / "p"
    project.mkdir()
    assert (
        run.invoke(app, ["use", "agent", "generic", "-d", str(project)]).exit_code == 2
    )  # needs --command
    done = run.invoke(
        app,
        ["use", "agent", "generic", "--command", f"{sys.executable} {fake}", "-d", str(project)],
    )
    assert done.exit_code == 0 and "cannot limit" in " ".join(done.output.split())
    assert read_settings(project).command == (sys.executable, str(fake))  # type: ignore[union-attr]
    monkeypatch.setitem(os.environ, "FAKE_MODE", "hello")
    ok = run.invoke(app, ["check", "agent", "-d", str(project), "-y"])
    assert ok.exit_code == 0 and "edits files in its working directory" in ok.output
    monkeypatch.setitem(os.environ, "FAKE_MODE", "fail")
    bad = run.invoke(app, ["check", "agent", "-d", str(project), "-y"])
    assert bad.exit_code == 1 and "failed" in bad.output
    monkeypatch.setitem(os.environ, "FAKE_MODE", "good")  # answers, but does not do the task
    lazy = run.invoke(app, ["check", "agent", "-d", str(project), "-y"])
    assert lazy.exit_code == 1


# ── tuvl init ────────────────────────────────────────────────────────────────


def test_init_yes_writes_settings_and_a_git_repo_on_develop(agent_bin, project: Path) -> None:
    agent_bin("claude")
    done = tuvl("init", "app", "-y", path=agent_bin.path, cwd=project)
    assert done.returncode == 0, done.stderr
    app = project / "app"
    assert read_settings(app) == Settings("AL", "none")  # --yes never starts an agent
    head = (app / ".git" / "HEAD").read_text().strip()
    assert head == "ref: refs/heads/develop"
    ignored = (app / ".gitignore").read_text().splitlines()
    for entry in (".tuvl/agent.yaml", ".tuvl/audit/", ".tuvl/worktrees/"):
        assert entry in ignored
    assert ".tuvl/" not in ignored  # project.yaml and system.yaml stay committed


def test_init_agent_flag_and_initials(agent_bin, project: Path) -> None:
    agent_bin("claude")
    done = tuvl(
        "init",
        "app",
        "-y",
        "--agent",
        "claude",
        "--initials",
        "sk",
        path=agent_bin.path,
        cwd=project,
    )
    assert done.returncode == 0, done.stderr
    assert read_settings(project / "app") == Settings("SK", "claude")


def test_init_refuses_a_bad_agent_or_initials(agent_bin, project: Path) -> None:
    bad_agent = tuvl("init", "a", "-y", "--agent", "nope", path=agent_bin.path, cwd=project)
    bad_initials = tuvl("init", "b", "-y", "--initials", "1x", path=agent_bin.path, cwd=project)
    assert bad_agent.returncode == 2 and bad_initials.returncode == 2
    assert not (project / "a").exists() and not (project / "b").exists()


def test_init_no_git_and_commit(agent_bin, project: Path) -> None:
    tuvl("init", "plain", "-y", "--no-git", path=agent_bin.path, cwd=project)
    assert not (project / "plain" / ".git").exists()
    tuvl(
        "init", "committed", "-y", "--commit", "--branch", "main", path=agent_bin.path, cwd=project
    )
    repo = project / "committed"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, check=False
    )
    assert "Initial tuvl project" in log.stdout
    assert (repo / ".git" / "HEAD").read_text().strip() == "ref: refs/heads/main"


def test_init_inside_an_existing_repository_leaves_git_alone(agent_bin, project: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=project, check=True)
    done = tuvl("init", "inner", "-y", path=agent_bin.path, cwd=project)
    assert done.returncode == 0
    assert not (project / "inner" / ".git").exists()
    assert "already inside a repository" in done.stdout


def test_init_asks_for_the_agent_among_those_found(agent_bin, project: Path) -> None:
    agent_bin("claude")
    agent_bin("codex")
    # answers: postgres? n, LLM? n, laya? n, initials, agent
    done = tuvl("init", "ask", path=agent_bin.path, cwd=project, stdin="n\nn\nn\nZZ\ncodex\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert read_settings(project / "ask") == Settings("ZZ", "codex")


def test_list_agents_shows_the_table_and_writes_nothing(agent_bin, project: Path) -> None:
    agent_bin("claude")
    done = tuvl("init", "ghost", "--list-agents", path=agent_bin.path, cwd=project)
    assert done.returncode == 0 and "Claude Code" in done.stdout
    assert not (project / "ghost").exists()


def test_doctor_agent_group(agent_bin, project: Path) -> None:
    agent_bin("claude", version="2.0")

    def report() -> dict[str, str]:
        done = tuvl(
            "check",
            "system",
            "-d",
            str(project),
            "--json",
            "--only",
            "agent",
            path=agent_bin.path,
            cwd=project,
        )
        data = json.loads(done.stdout[done.stdout.index('{\n  "healthy"') :])
        return {c["id"]: c["status"] for c in data["checks"]}

    assert report() == {"settings": "warn"}
    write_settings(project, Settings("AL", "none"))
    assert report() == {"initials": "ok", "agent": "ok"}
    write_settings(project, Settings("AL", "claude"))
    assert report()["agent"] == "ok"
    write_settings(project, Settings("AL", "codex"))
    assert report()["agent"] == "fail"
    assert os.path.exists(agent_bin.dir)


# ── tuvl init --ci ───────────────────────────────────────────────────────────


def _tuvl_commands_in(text: str) -> set[str]:
    import re

    return set(re.findall(r"uv run tuvl ([a-z][a-z-]*)", text))


def test_init_writes_the_gate_as_github_actions(agent_bin, project: Path) -> None:
    done = tuvl("init", "app", "-y", "--ci", "github", path=agent_bin.path, cwd=project)
    assert done.returncode == 0, done.stderr
    path = project / "app" / ".github" / "workflows" / "tuvl.yml"
    import yaml

    doc = yaml.safe_load(path.read_text())
    assert set(doc["jobs"]) == {"gate", "strict"}
    gate = [s["run"] for s in doc["jobs"]["gate"]["steps"] if "run" in s]
    assert "uv run tuvl review code" in gate and "uv run tuvl generate code --check" in gate
    assert all(
        "--strict" not in c for c in gate
    )  # a half-built stack must not fail on pending steps
    strict = [s["run"] for s in doc["jobs"]["strict"]["steps"] if "run" in s]
    assert (
        "uv run tuvl list tasks --strict" in strict and "uv run tuvl review code --strict" in strict
    )


def test_init_writes_the_gate_as_a_gitlab_pipeline(agent_bin, project: Path) -> None:
    done = tuvl("init", "app", "-y", "--ci", "gitlab", path=agent_bin.path, cwd=project)
    assert done.returncode == 0, done.stderr
    import yaml

    doc = yaml.safe_load((project / "app" / ".gitlab-ci.yml").read_text())
    assert (
        doc["gate"]["script"][0] == "uv sync" and "uv run tuvl review code" in doc["gate"]["script"]
    )
    assert "uv run tuvl review code --strict" in doc["strict"]["script"]


def test_every_command_in_the_generated_ci_exists() -> None:
    from tuvl.cli import app
    from tuvl.cli.commands import init_templates as tpl

    known = {c.name for c in app.registered_commands} | {g.name for g in app.registered_groups}
    used = _tuvl_commands_in(tpl.github_workflow()) | _tuvl_commands_in(tpl.gitlab_pipeline())
    assert used and used <= known


def test_init_has_no_ci_unless_asked_and_refuses_an_unknown_kind(agent_bin, project: Path) -> None:
    tuvl("init", "plain", "-y", path=agent_bin.path, cwd=project)
    assert (
        not (project / "plain" / ".github").exists()
        and not (project / "plain" / ".gitlab-ci.yml").exists()
    )
    bad = tuvl("init", "x", "-y", "--ci", "jenkins", path=agent_bin.path, cwd=project)
    assert bad.returncode == 2 and not (project / "x").exists()


# ── who can do what ──────────────────────────────────────────────────────────


def test_agent_list_says_which_agents_write_and_which_only_review(agent_bin, project: Path) -> None:
    for name in ("claude", "cursor"):
        agent_bin(name, help_text="Cursor Agent")
    done = tuvl("list", "agents", "-d", str(project), "--json", path=agent_bin.path, cwd=project)
    rows = {r["id"]: r for r in json.loads(done.stdout)["agents"]}
    assert rows["claude"]["capability"] == "write" and rows["cursor"]["capability"] == "review"
    assert rows["aider"]["capability"] == "" and rows["codex"]["capability"] == "write"


def test_choosing_a_review_only_agent_says_so_and_it_cannot_write(
    agent_bin, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_bin("cursor-agent", help_text="Cursor Agent")
    done = tuvl("use", "agent", "cursor", "-d", str(project), path=agent_bin.path, cwd=project)
    assert (
        done.returncode == 0
        and "can review code through tuvl but cannot draft specs" in " ".join(done.stdout.split())
    )
    import typer

    from tuvl.cli import agent_run
    from tuvl_insight.coding_agents import Detected, get

    fake = [Detected(get("cursor"), "/x/agent", "1.0")]  # type: ignore[arg-type]
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: fake)
    with pytest.raises(typer.Exit):  # the refusal is printed and nothing runs
        agent_run.choose_agent(project, "cursor")
    assert agent_run.choose_reviewer(project, "cursor", "claude").id == "cursor"


def test_antigravity_can_be_chosen_to_draft_and_build(agent_bin, project: Path) -> None:
    agent_bin("agy")
    done = tuvl("use", "agent", "antigravity", "-d", str(project), path=agent_bin.path, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "cannot draft" not in " ".join(done.stdout.split())
    assert read_settings(project).agent == "antigravity"


def test_init_offers_a_writer_by_default(agent_bin, project: Path) -> None:
    agent_bin("cursor-agent", help_text="Cursor Agent")
    agent_bin("claude")
    done = tuvl("init", "ask", path=agent_bin.path, cwd=project, stdin="n\nn\nn\nZZ\n\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert read_settings(project / "ask").agent == "claude"  # not the first one found


def test_init_asks_for_the_agent_with_the_menu_when_there_is_a_terminal(
    agent_bin, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from tuvl.cli import app, select

    agent_bin("claude")
    agent_bin("agy")
    asked: dict[str, object] = {}

    def pick(title, choices, default="", **_):  # type: ignore[no-untyped-def]
        asked.update(choices=[(c.value, c.hint) for c in choices], default=default)
        return "antigravity"

    monkeypatch.setattr(select, "pick", pick)
    monkeypatch.setenv("PATH", f"{agent_bin.path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.chdir(project)
    done = CliRunner().invoke(app, ["init", "menu"], input="n\nn\nn\nZZ\n\n")
    assert done.exit_code == 0, done.output
    assert asked["default"] == "claude"
    assert asked["choices"] == [
        ("claude", "drafts specs and builds tasks"),
        ("antigravity", "drafts specs and builds tasks"),
        ("none", "choose later with `tuvl use agent <agent>`"),
    ]
    assert read_settings(project / "menu").agent == "antigravity"
