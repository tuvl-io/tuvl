"""The terminal UI of `tuvl dev`, driven with Textual's pilot: a tree, actions that run the CLI, three logs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from textual.widgets import Button, Tree

from tuvl_insight.audit import ledger, live
from tuvl_insight.coding_agents import Settings, write_settings
from tuvl_insight.spec import lifecycle
from tuvl_insight.spec.ideas import import_idea, plan_import
from tuvl_insight.tui import clipboard, commands
from tuvl_insight.tui.app import ConfirmScreen, PromptScreen, TuvlDevApp
from tuvl_insight.tui.engine import Engine
from tuvl_insight.tui.model import Node, build_tree


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    write_settings(p, Settings("SK", "claude"))
    brief = tmp_path / "brief.md"
    brief.write_text("# Refunds\n\nPay refunds.\n")
    import_idea(
        p, plan_import([brief]), initials="SK", title="Refunds", supersedes=None, imported_by="Ada"
    )
    specs = p / "specs"
    specs.mkdir()
    text = lifecycle.with_front(
        "# Intent\nx\n",
        {
            "id": "SPEC-SK-0001-01",
            "title": "Refunds spec",
            "ideas": ["IDEA-SK-0001"],
            "status": "draft",
        },
    )
    (specs / "SPEC-SK-0001-01.md").write_text(text)
    (specs / "SPEC-SK-0001-01.tasks.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "TaskPlan",
                "version": "tuvl/v2",
                "metadata": {"name": "r", "spec": "specs/SPEC-SK-0001-01.md", "spec_hash": "x"},
                "spec": {
                    "tasks": [
                        {
                            "id": "TASK-SK-0001-01-001",
                            "title": "Refund model",
                            "type": "model",
                            "target": {"model": "Refund"},
                        },
                        {
                            "id": "TASK-SK-0001-01-002",
                            "title": "extract (llm default)",
                            "type": "agent",
                            "target": {"workflow": "w", "agent": "extract"},
                        },
                    ]
                },
            }
        )
    )
    (specs / "legacy.md").write_text("---\nname: legacy\n---\n# Intent\nold\n")
    lifecycle.sync_idea_links(p)
    return p


def fake_cli(record: list[list[str]], output: str = "hello"):
    """A `tuvl` stand-in: records the arguments and prints a line."""

    def argv_for(args, project):
        record.append(list(args))
        return [sys.executable, "-c", f"print({output!r}); print({' '.join(args)!r})"]

    return argv_for


def tree_labels(app: TuvlDevApp) -> list[str]:
    tree = app.query_one(Tree)
    out: list[str] = []

    def walk(node, depth=0):
        out.append("  " * depth + str(node.label))
        for c in node.children:
            walk(c, depth + 1)

    walk(tree.root)
    return out


def select(app: TuvlDevApp, node_id: str) -> None:
    app.choose(node_id)


def buttons(app: TuvlDevApp) -> list[str]:
    return [str(b.label) for b in app.query(Button)]


def log_text(app: TuvlDevApp, which: str) -> str:
    return "\n".join(app.logs[which])


async def test_the_tree_shows_ideas_specs_and_tasks(project: Path) -> None:
    app = TuvlDevApp(project, argv_for=fake_cli([]), poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        labels = tree_labels(app)
        joined = "\n".join(labels)
        assert "IDEA-SK-0001  Refunds" in joined and "SPEC-SK-0001-01  [draft]" in joined
        assert "Refund model" in joined and "extract (llm default)" in joined
        assert "Specs without an idea" in joined and "legacy.md  [legacy]" in joined


async def test_selecting_a_thing_shows_its_details_and_what_can_be_done(project: Path) -> None:
    app = TuvlDevApp(project, argv_for=fake_cli([]), poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app, "IDEA-SK-0001")
        await pilot.pause()
        assert any("Draft the spec with my agent" in b for b in buttons(app)) and any(
            "Write the spec myself" in b for b in buttons(app)
        )
        select(app, "SPEC-SK-0001-01")
        await pilot.pause()
        labels = buttons(app)
        assert (
            "Check" in labels and "Approve" in labels and not any("Derive" in b for b in labels)
        )  # tasks only after approval
        select(app, "SPEC-SK-0001-01#TASK-SK-0001-01-002")
        await pilot.pause()
        assert any("Build this task with my agent" in b for b in buttons(app))
        assert any("Show this task in full" in b for b in buttons(app))


async def test_a_plain_action_runs_the_cli_and_streams_its_output(project: Path) -> None:
    seen: list[list[str]] = []
    app = TuvlDevApp(project, argv_for=fake_cli(seen, "all good"), poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app, "SPEC-SK-0001-01")
        await pilot.pause()
        await pilot.click(next(b for b in app.query(Button) if str(b.label) == "Check"))
        for _ in range(40):
            await pilot.pause(0.05)
            if "exit 0" in log_text(app, "command"):
                break
        text = log_text(app, "command")
        assert (
            "$ tuvl lint spec SPEC-SK-0001-01" in text and "all good" in text and "✓ exit 0" in text
        )
        assert ["lint", "spec", "SPEC-SK-0001-01"] in seen


async def test_an_approval_asks_first_and_cancel_runs_nothing(project: Path) -> None:
    seen: list[list[str]] = []
    app = TuvlDevApp(project, argv_for=fake_cli(seen), poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app, "SPEC-SK-0001-01")
        await pilot.pause()
        await pilot.click(next(b for b in app.query(Button) if str(b.label) == "Approve"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert ["approve", "spec", "SPEC-SK-0001-01"] not in seen and not isinstance(
            app.screen, ConfirmScreen
        )
        await pilot.click(next(b for b in app.query(Button) if str(b.label) == "Approve"))
        await pilot.pause()
        await pilot.press("y")
        for _ in range(40):
            await pilot.pause(0.05)
            if ["approve", "spec", "SPEC-SK-0001-01"] in seen:
                break
        assert ["approve", "spec", "SPEC-SK-0001-01"] in seen


async def test_an_action_that_needs_text_asks_for_it(project: Path) -> None:
    seen: list[list[str]] = []
    app = TuvlDevApp(project, argv_for=fake_cli(seen), poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app, "SPEC-SK-0001-01")
        await pilot.pause()
        await pilot.click(next(b for b in app.query(Button) if "Send back" in str(b.label)))
        await pilot.pause()
        assert isinstance(app.screen, PromptScreen)
        await pilot.press(*"too vague", "enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if any(a[:2] == ["reject", "spec"] for a in seen):
                break
        assert ["reject", "spec", "SPEC-SK-0001-01", "--reason", "too vague"] in seen


async def test_actions_that_spend_tokens_say_so_and_carry_consent_only_once(project: Path) -> None:
    seen: list[list[str]] = []
    app = TuvlDevApp(project, argv_for=fake_cli(seen), poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app, "IDEA-SK-0001")
        await pilot.pause()
        assert any("($)" in b for b in buttons(app))  # marked in the button
        await pilot.click(next(b for b in app.query(Button) if "Draft the spec" in str(b.label)))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert (
            "spends tokens" in app.screen.body
            and "consent" in app.screen.body
            and "--consent" in app.screen.command
        )
        await pilot.press("y")
        for _ in range(40):
            await pilot.pause(0.05)
            if any(a[:2] == ["generate", "spec"] for a in seen):
                break
        gen = next(a for a in seen if a[:2] == ["generate", "spec"])
        assert gen == [
            "generate",
            "spec",
            "--idea",
            "IDEA-SK-0001",
            "--yes",
            "--here",
            "--consent",
        ]
    write_settings(project, Settings("SK", "claude", "2026-10-19T00:00:00Z"))
    seen.clear()
    app2 = TuvlDevApp(project, argv_for=fake_cli(seen), poll=0.2)
    async with app2.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app2, "IDEA-SK-0001")
        await pilot.pause()
        await pilot.click(next(b for b in app2.query(Button) if "Draft the spec" in str(b.label)))
        await pilot.pause()
        assert "consent" not in app2.screen.body
        await pilot.press("y")
        for _ in range(40):
            await pilot.pause(0.05)
            if seen:
                break
        assert "--consent" not in next(a for a in seen if a[:2] == ["generate", "spec"])


async def test_live_codegen_progress_shows_in_the_tree(project: Path) -> None:
    app = TuvlDevApp(project, argv_for=fake_cli([]), poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        live.set_task(
            project,
            "specs/SPEC-SK-0001-01.md",
            "TASK-SK-0001-01-002",
            "running",
            attempt=2,
            max_attempts=3,
            gate="validate",
        )
        for _ in range(30):
            await pilot.pause(0.1)
            if "running 2/3 · validate" in "\n".join(tree_labels(app)):
                break
        assert "running 2/3 · validate" in "\n".join(tree_labels(app))


async def test_derived_statuses_come_from_spec_status(project: Path) -> None:
    payload = json.dumps(
        {
            "done": False,
            "plans": [
                {
                    "spec": "specs/SPEC-SK-0001-01.md",
                    "tasks": [{"id": "TASK-SK-0001-01-001", "status": "done"}],
                }
            ],
        },
        indent=2,
    )

    def argv_for(args, project):
        code = (
            f'print(\'{{"event": "config.loaded"}}\'); print({payload!r})'
            if args[:2] == ("list", "tasks")
            else "pass"
        )
        return [sys.executable, "-c", code]

    app = TuvlDevApp(project, argv_for=argv_for, poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        for _ in range(40):
            await pilot.pause(0.1)
            if "[done]" in "\n".join(tree_labels(app)):
                break
        assert any(
            "✓" in line and "Refund model" in line and "[done]" in line for line in tree_labels(app)
        )


async def test_the_audit_tab_follows_the_ledger(project: Path) -> None:
    ledger.append(
        project,
        "idea.imported",
        command="spec import",
        entities=["IDEA-SK-0001"],
        who={"initials": "SK"},
    )
    app = TuvlDevApp(project, argv_for=fake_cli([]), poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        assert "idea.imported" in log_text(app, "audit")
        ledger.append(
            project,
            "spec.approved",
            command="spec approve",
            entities=["SPEC-SK-0001-01"],
            who={"initials": "SK"},
        )
        for _ in range(40):
            await pilot.pause(0.1)
            if "spec.approved" in log_text(app, "audit"):
                break
        assert "✓" in log_text(app, "audit") and "spec.approved  SPEC-SK-0001-01" in log_text(
            app, "audit"
        )


async def test_the_engine_log_streams_and_quitting_stops_the_engine(project: Path) -> None:
    engine = Engine(
        [sys.executable, "-c", "import time\nprint('engine is up', flush=True)\ntime.sleep(60)"],
        dict(os.environ),
    )
    app = TuvlDevApp(
        project,
        engine=engine,
        insight_url="http://127.0.0.1:1/insight/",
        argv_for=fake_cli([]),
        poll=0.2,
    )
    async with app.run_test(size=(150, 45)) as pilot:
        for _ in range(60):
            await pilot.pause(0.1)
            if "engine is up" in log_text(app, "engine"):
                break
        assert "engine is up" in log_text(app, "engine") and engine.alive
        assert "http://127.0.0.1:1/insight/" in app.sub_title
        await pilot.press("q")
        await pilot.pause(0.5)
    assert engine.proc is not None and engine.proc.returncode is not None


async def test_a_command_can_be_stopped(project: Path) -> None:
    def slow(args, project):
        return [sys.executable, "-c", "import time\nprint('started', flush=True)\ntime.sleep(60)"]

    app = TuvlDevApp(project, argv_for=slow, poll=0.2)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        app.run_worker(app.execute(["lint", "project"]), exclusive=False)
        for _ in range(60):
            await pilot.pause(0.05)
            if "started" in log_text(app, "command"):
                break
        assert app.runner.running
        await pilot.press("x")
        for _ in range(60):
            await pilot.pause(0.05)
            if not app.runner.running:
                break
        assert not app.runner.running and "stopped" in log_text(app, "command")


def test_the_model_groups_specs_under_their_idea(project: Path) -> None:
    root = build_tree(project, {"TASK-SK-0001-01-001": "done"})
    kinds = {n.id: n.kind for n in root.walk()}
    assert (
        kinds["IDEA-SK-0001"] == "idea"
        and kinds["SPEC-SK-0001-01"] == "spec"
        and kinds["SPEC-SK-0001-01#TASK-SK-0001-01-001"] == "task"
    )
    idea = next(n for n in root.walk() if n.id == "IDEA-SK-0001")
    assert [c.id for c in idea.children] == ["SPEC-SK-0001-01"]
    loose = next(n for n in root.walk() if n.id == "loose")
    assert [c.id for c in loose.children] == ["specs/legacy.md"]


def test_every_action_is_a_real_tuvl_command(project: Path) -> None:
    from tuvl.cli import app as cli
    from tuvl_insight.tui.actions import actions_for

    known = {c.name for c in cli.registered_commands} | {g.name for g in cli.registered_groups}
    root = build_tree(project)
    for node in root.walk():
        for action in actions_for(node):
            assert action.args[0] in known, action
    nodes: list[Node] = [n for n in root.walk() if n.kind in ("spec", "idea", "task")]
    assert nodes and commands.cli_argv(("lint", "project"), project)[-2:] == ["-d", str(project)]


def test_the_ui_starts_only_on_a_terminal_unless_declined() -> None:
    from tuvl_insight.cli.commands.dev import tui_unavailable

    assert tui_unavailable(no_tui=False, tty=True) is None
    assert tui_unavailable(no_tui=True, tty=True) == ""  # asked not to: silent
    assert (
        tui_unavailable(no_tui=False, tty=False) == ""
    )  # CI, a pipe, ssh without a tty: as before


def test_a_toolchain_without_textual_says_how_to_get_the_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tuvl_insight.cli.commands.dev import tui_unavailable

    monkeypatch.setattr("tuvl.extras.available", lambda name: name != "tui")
    why = tui_unavailable(no_tui=False, tty=True)
    assert why and "tuvl[standard]" in why and "running the engine here" in why


async def test_project_actions_are_offered_whatever_is_selected(project: Path) -> None:
    app = TuvlDevApp(project, poll=60)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app, "SPEC-SK-0001-01")
        await pilot.pause()
        labels = [str(b.label) for b in app.query(Button)]
        assert "Check" in labels and "Run the tests" in labels and "Show the audit log" in labels


async def test_every_command_ends_with_its_token_use(project: Path) -> None:
    app = TuvlDevApp(project, argv_for=fake_cli([], "all good"), poll=60)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await app.execute(["lint", "project"])
        assert "tokens · none used" in log_text(app, "command")


async def test_the_command_output_can_be_selected_for_copying(project: Path) -> None:
    from textual.widgets import TextArea

    app = TuvlDevApp(project, argv_for=fake_cli([], "all good"), poll=60)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await app.execute(["lint", "project"])
        area = app.query_one("#command-log", TextArea)
        area.select_all()
        assert "all good" in area.selected_text


async def test_up_and_down_move_between_the_action_buttons(project: Path) -> None:
    app = TuvlDevApp(project, argv_for=fake_cli([]), poll=60)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app, "IDEA-SK-0001")
        await pilot.pause()
        found = [b for b in app.query(Button) if not b.disabled]
        found[0].focus()
        await pilot.press("down")
        assert app.focused is found[1]
        await pilot.press("down", "up")
        assert app.focused is found[1]
        await pilot.press("up", "up")
        assert app.focused is found[0]


async def test_copy_uses_the_system_clipboard_for_the_selection(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import TextArea

    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or True)
    app = TuvlDevApp(project, argv_for=fake_cli([], "all good"), poll=60)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await app.execute(["lint", "project"])
        area = app.query_one("#command-log", TextArea)
        area.focus()
        area.select_all()
        await pilot.press("c")
        assert copied and "all good" in copied[-1]
        copied.clear()
        await pilot.press("ctrl+c")
        assert copied and "all good" in copied[-1]


async def test_selecting_with_the_mouse_copies_the_selection(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import TextArea

    copied: list[str] = []
    monkeypatch.setattr(clipboard, "copy", lambda text: copied.append(text) or True)
    app = TuvlDevApp(project, argv_for=fake_cli([], "all good"), poll=60)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await app.execute(["lint", "project"])
        area = app.query_one("#command-log", TextArea)
        await pilot.mouse_down(area, offset=(1, 0))
        await pilot.hover(area, offset=(30, 0))
        await pilot.mouse_up(area, offset=(30, 0))
        await pilot.pause()
        assert copied and copied[-1].strip() and copied[-1] == area.selected_text


async def test_the_screen_carries_the_name_and_the_malayalam_word(project: Path) -> None:
    from textual.widgets import Static

    app = TuvlDevApp(project, argv_for=fake_cli([]), poll=60)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        brand = app.query_one("#brand", Static)
        lines = [
            "".join(seg.text for seg in line) for line in app.screen._compositor.render_strips()
        ]
        shown = "\n".join(lines)
        assert "തൂവൽ" in shown and "feather" in shown
        assert "Intelligent Workflows." in shown and "Deterministic Execution." in shown
        assert brand.region.height >= 6


async def test_a_task_can_be_shown_in_full(project: Path) -> None:
    seen: list[list[str]] = []
    app = TuvlDevApp(project, argv_for=fake_cli(seen, "the task"), poll=60)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        select(app, "SPEC-SK-0001-01#TASK-SK-0001-01-002")
        await pilot.pause()
        await pilot.click(next(b for b in app.query(Button) if "Show this task" in str(b.label)))
        for _ in range(40):
            await pilot.pause(0.05)
            if any(a[:2] == ["show", "task"] for a in seen):
                break
        shown = next(a for a in seen if a[:2] == ["show", "task"])
        assert shown == ["show", "task", "TASK-SK-0001-01-002", "--spec", "SPEC-SK-0001-01"]
        assert "the task" in log_text(app, "command")
