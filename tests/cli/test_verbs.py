"""The action-first command tree: every new command resolves, every old name still works, and the
commands that replaced a command (`run`, `review`) still answer it."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tuvl.cli import app, verbs

runner = CliRunner()


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


NEW = [
    "init", "create spec", "import idea", "use agent", "set project", "upgrade project",
    "register project", "update skills", "generate spec", "generate tasks", "generate code",
    "generate ui-spec", "generate lock", "generate key", "generate rls", "approve spec",
    "reject spec", "reopen spec", "renumber idea", "lint project", "lint spec", "verify spec",
    "verify lock", "verify ui-spec", "verify rls", "verify audit", "check system", "check agent",
    "check storage", "review code", "run workflow", "run tests", "serve app", "serve mcp",
    "follow run", "pin run", "replay runs", "list ideas", "list specs", "list tasks", "list runs",
    "list agents", "list objects", "show idea", "show task", "show run", "show project", "show audit",
    "show event", "show drift", "export audit", "export schema", "prune traces", "dev", "ship",
    "deploy",
]  # fmt: skip


@pytest.mark.parametrize("command", NEW)
def test_every_new_command_resolves(command: str) -> None:
    res = runner.invoke(app, [*command.split(), "--help"])
    assert res.exit_code == 0, res.output
    assert f"Usage: tuvl {command}" in " ".join(_plain(res.output).split())


def test_the_help_lists_only_the_verbs() -> None:
    res = runner.invoke(app, ["--help"])
    rows = {
        line.strip("│ ").split()[0]
        for line in _plain(res.output).splitlines()
        if line.startswith("│ ")
    }
    for verb, _, _ in verbs.VERBS:
        assert verb in rows
    for old in ("validate", "codegen", "doctor", "spec", "runs", "audit", "agent", "keys", "db"):
        assert old not in rows


@pytest.fixture
def exec_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str]]]:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake(file: str, args: list[str], env: dict[str, str]) -> None:
        calls.append((args, env))
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvpe", fake)
    monkeypatch.setattr(os, "name", "posix")
    return calls


@pytest.mark.parametrize("argv", [["serve", "app"]])
def test_the_server_replaces_the_process_so_it_receives_sigterm(
    exec_calls: list[tuple[list[str], dict[str, str]]], argv: list[str], tmp_path: Path
) -> None:
    res = runner.invoke(
        app,
        [
            *argv,
            "--project-dir", str(tmp_path),
            "--port", "9123",
            "--forwarded-allow-ips", "10.0.0.0/8",
            "--limit-max-requests", "5000",
            "--drain-timeout", "7",
        ],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    [(cmd, env)] = exec_calls
    assert cmd[1:4] == ["-m", "uvicorn", "tuvl.core.app:app"]
    pairs = dict(zip(cmd[4:], cmd[5:], strict=False))
    assert pairs["--port"] == "9123" and pairs["--timeout-graceful-shutdown"] == "20"
    assert pairs["--forwarded-allow-ips"] == "10.0.0.0/8"
    assert pairs["--limit-max-requests"] == "5000"
    assert "--no-server-header" in cmd
    assert env["TUVL_SHUTDOWN_DRAIN_SECONDS"] == "7.0" and env["TUVL_ROLE"] == "all"


def test_the_server_defaults_are_safe(
    exec_calls: list[tuple[list[str], dict[str, str]]], tmp_path: Path
) -> None:
    runner.invoke(app, ["serve", "app", "-d", str(tmp_path)])
    [(cmd, _)] = exec_calls
    assert "--forwarded-allow-ips" not in cmd and "--limit-max-requests" not in cmd
    assert cmd[cmd.index("--timeout-graceful-shutdown") + 1] == "20"


def test_a_toolchain_command_without_the_toolchain_says_how_to_install_it() -> None:
    group = verbs._Group("lint", "x")
    group.toolchain = False
    group.add("project", lambda: None)
    res = runner.invoke(group.app, ["project"])
    out = _plain(res.output)
    assert res.exit_code == 2 and "developer toolchain" in out and "tuvl[standard]" in out


# ── on a real project ───────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from unittest import mock

    with mock.patch("tuvl_insight.coding_agents.detect", lambda *a, **k: []):
        root = tmp_path_factory.mktemp("verbs")
        res = runner.invoke(
            app, ["init", str(root / "demo"), "--sample", "--preset", "ollama", "-y", "--no-git"]
        )
    assert res.exit_code == 0, res.output
    return root / "demo"


def call(sample: Path, *words: str) -> Any:
    return runner.invoke(app, [*words, "-d", str(sample)])


@pytest.mark.parametrize(
    "words",
    [
        ("lint", "project"), ("lint", "spec"), ("verify", "lock", "--no-mcp"),
        ("verify", "ui-spec", "--no-tests"), ("verify", "audit"), ("list", "specs"),
        ("list", "tasks", "--no-tests"), ("list", "ideas"), ("show", "project"),
        ("show", "drift"), ("show", "audit"),
    ],
)  # fmt: skip
def test_the_new_commands_do_what_the_old_ones_did(sample: Path, words: tuple[str, ...]) -> None:
    res = call(sample, *words)
    assert res.exit_code == 0, res.output


def test_verify_lock_fails_when_the_lock_is_stale(sample: Path, tmp_path: Path) -> None:
    import shutil

    copy = tmp_path / "copy"
    shutil.copytree(sample, copy)
    (copy / "tuvl.lock").unlink()
    res = call(copy, "verify", "lock", "--no-mcp")
    assert res.exit_code == 1 and "tuvl generate lock" in res.output
    assert call(copy, "generate", "lock", "--no-mcp").exit_code == 0
    assert call(copy, "verify", "lock", "--no-mcp").exit_code == 0


def test_review_can_stay_on_one_spec_or_one_task(sample: Path) -> None:
    whole = json.loads(call(sample, "review", "code", "--no-ruff", "--json").output)
    scoped = json.loads(
        call(sample, "review", "code", "--spec", "triage-ticket", "--no-ruff", "--json").output
    )
    assert scoped["files"] == ["agents/prioritise_ticket.py"]
    assert set(scoped["files"]) <= set(whole["files"])

    plan = next((sample / "specs").glob("*.tasks.yaml")).read_text()
    agent_task = next(
        t for t in plan.split("- id: ")[1:] if "prioritise" in t and "type: agent" in t
    ).split()[0]
    one = json.loads(
        call(
            sample, "review", "code", "--spec", "triage-ticket", "--task", agent_task, "--no-ruff",
            "--json",
        ).output
    )  # fmt: skip
    assert one["files"] == ["agents/prioritise_ticket.py"]

    model_task = next(t for t in plan.split("- id: ")[1:] if "type: model" in t).split()[0]
    none = call(
        sample, "review", "code", "--spec", "triage-ticket", "--task", model_task, "--no-ruff"
    )
    assert none.exit_code == 0 and "No code agent belongs" in none.output


def test_review_names_a_spec_or_task_it_cannot_find(sample: Path) -> None:
    res = call(sample, "review", "code", "--spec", "nope")
    assert res.exit_code == 2 and "nope" in res.output
    res = call(sample, "review", "code", "--spec", "triage-ticket", "--task", "T99")
    assert res.exit_code == 2 and "T99" in res.output


def _render(width: int) -> str:
    from rich.console import Console

    from tuvl.cli import logo

    console = Console(width=width, record=True, force_terminal=False)
    console.print(logo.header("2.0.0", width))
    return console.export_text()


def test_the_default_screen_draws_the_feather_when_it_fits() -> None:
    wide = _render(100)
    assert "⢕" in wide and "v2.0.0" in wide  # ⢕ is in the stippled feather and nowhere else


def test_the_default_screen_drops_the_feather_on_a_narrow_terminal() -> None:
    narrow = _render(60)
    assert "⢕" not in narrow and "v2.0.0" in narrow


def test_the_dev_reload_does_not_watch_the_runtime_folders(tmp_path) -> None:
    from tuvl_insight.cli.commands.dev import _reload_excludes

    (tmp_path / ".git").mkdir()
    shown = _reload_excludes(tmp_path)
    watched = [shown[i + 1] for i, flag in enumerate(shown) if flag == "--reload-exclude"]
    assert str(tmp_path / ".tuvl" / "worktrees") in watched and str(tmp_path / ".git") in watched
    assert str(tmp_path / "node_modules") not in watched  # absent folders are not excluded
