"""The terminal UI of ``tuvl dev``: the project's ideas, specs and tasks, what can be done to each, and the logs.

It is a launcher. Every action runs the same ``tuvl`` command a person would type, so the gates and the
audit trail are the ones everything else goes through. It never reads or writes a project file itself.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.markup import escape
from textual import on
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)

from tuvl.cli import logo
from tuvl_insight.audit import ledger
from tuvl_insight.coding_agents import read_settings
from tuvl_insight.tui import clipboard, commands
from tuvl_insight.tui.actions import Action, actions_for
from tuvl_insight.tui.engine import Engine
from tuvl_insight.tui.model import Node, build_tree, signature


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "No"), Binding("y", "yes", "Yes"), Binding("n", "no", "No")]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 72; height: auto; border: round $warning; background: $surface; padding: 1 2; }
    #buttons { height: auto; margin-top: 1; }
    """

    def __init__(self, title: str, body: str, command: str) -> None:
        super().__init__()
        self.title_text, self.body, self.command = title, body, command

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"[b]{escape(self.title_text)}[/b]")
            yield Static(escape(self.body))
            yield Static(f"[dim]{escape(self.command)}[/dim]")
            with Horizontal(id="buttons"):
                yield Button("Run it", id="yes", variant="warning")
                yield Button("Cancel", id="no")

    @on(Button.Pressed, "#yes")
    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


class PromptScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    #pbox { width: 72; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="pbox"):
            yield Label(escape(self.question))
            yield Input(id="answer")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def describe(node: Node, *, insight_url: str, agent: str) -> str:
    d = node.data
    if node.kind == "project":
        return f"[b]{escape(node.label)}[/b]\n{escape(d.get('path', ''))}\n\nInsight  {insight_url}\nYour coding agent  {agent}"
    if node.kind == "group":
        return f"[b]{escape(node.label)}[/b]\n{len(node.children)} item(s)"
    if node.kind == "idea":
        return (
            f"[b]{escape(d['title'])}[/b]\n{d['id']}\nimported {d['imported_at']} by {escape(d.get('imported_by') or '?')}\n"
            f"{d['files']} file(s) · specs: {', '.join(d['specs']) or 'none yet'}"
            + (f"\nsuperseded by {d['superseded_by']}" if d.get("superseded_by") else "")
        )
    if node.kind == "spec":
        who = (
            f"\napproved by {escape(d['approved_by'])} at {d['approved_at']}"
            if d.get("approved_by") and d["effective"] == "approved"
            else ""
        )
        note = f"\nsent back: {escape(d['review_note'])}" if d.get("review_note") else ""
        return f"[b]{escape(d['title'])}[/b]\n{d['id'] or d['path']}\nstatus: {d['effective']}{' (body changed since approval)' if d['stale'] else ''}{who}{note}\nideas: {', '.join(d['ideas']) or 'none'}"
    if node.kind == "task":
        live_now = d.get("live")
        from tuvl_insight.audit import live

        return (
            f"[b]{escape(str(d.get('title', '')))}[/b]\n{d['id']} · {d.get('type')}\n"
            f"status: {d.get('derived') or 'run Refresh to derive it'}"
            + (f"\nnow: {live.describe(live_now)}" if live_now else "")
            + (
                "\n[red]" + escape(str(d["failure"].get("summary", ""))) + "[/red]\n"
                "Select “Show this task in full” for the report of each attempt."
                if d.get("failure")
                else ""
            )
            + (
                "\ntarget: " + ", ".join(f"{k}={v}" for k, v in (d.get("target") or {}).items())
                if d.get("target")
                else ""
            )
        )
    return ""


class LogArea(TextArea):
    """A log you can select with the mouse or keyboard and copy: read-only, wrapped, no line numbers.

    Copying goes through the system clipboard tool, since most terminals ignore the escape code Textual uses.
    """

    BINDINGS = [Binding("ctrl+c", "copy", "Copy", show=False), Binding("c", "copy", "Copy")]

    def on_mouse_up(self) -> None:
        """Releasing the mouse on a selection copies it: terminals do not pass Cmd+C to the app."""
        if self.selected_text.strip():
            self.action_copy()

    def action_copy(self) -> None:
        text = self.selected_text or self.text
        if not text.strip():
            self.app.notify("Nothing to copy yet.")
            return
        if not clipboard.copy(text):
            self.app.copy_to_clipboard(text)
        what = "selection" if self.selected_text else "whole log"
        self.app.notify(f"Copied the {what} ({len(text.splitlines())} line(s)).")


def _log(widget_id: str) -> LogArea:
    return LogArea(
        "", id=widget_id, read_only=True, soft_wrap=True, show_line_numbers=False, show_cursor=False
    )


class TuvlDevApp(App[None]):
    TITLE = "tuvl dev"
    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("x", "cancel", "Stop command"),
        Binding("c", "copy", "Copy output"),
        Binding("down", "step(1)", "Next action", show=False, priority=True),
        Binding("up", "step(-1)", "Previous action", show=False, priority=True),
    ]
    CSS = """
    #left { width: 46; border-right: solid $primary-darken-2; }
    #brand { height: auto; padding: 0 1; border-bottom: solid $primary-darken-2; }
    #detail { height: 7; padding: 0 1; border-bottom: solid $primary-darken-2; }
    #actions { height: auto; max-height: 16; padding: 0 1; }
    #actions Label { color: $text-muted; margin-top: 1; }
    #actions Button { width: 100%; height: 1; min-width: 10; border: none; margin: 0; }
    TextArea { height: 1fr; border: none; padding: 0 1; }
    """

    def __init__(
        self,
        project: Path,
        *,
        engine: Engine | None = None,
        insight_url: str = "",
        runner: commands.Runner | None = None,
        argv_for: Callable[[tuple[str, ...] | list[str], Path], list[str]] = commands.cli_argv,
        poll: float = 2.0,
    ) -> None:
        super().__init__()
        self.project, self.engine, self.insight_url = project, engine, insight_url
        self.runner, self.argv_for, self.poll = runner or commands.Runner(), argv_for, poll
        self.derived: dict[str, str] = {}
        self.tree_signature = ""
        self.selected: Node | None = None
        self.audit_seen = 0
        self.last_exit: int | None = None
        self._actions: dict[str, Action] = {}
        self._gen = 0
        #: the same lines as the log widgets, kept as plain text (a hidden tab renders late)
        self.logs: dict[str, list[str]] = {"command": [], "engine": [], "audit": []}
        self.last_output: list[str] = []

    # ── layout ──
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Static(logo.brand(), id="brand")
                yield Tree("project", id="tree")
            with Vertical(id="right"):
                yield Static("", id="detail")
                with VerticalScroll(id="actions"):
                    pass
                with TabbedContent(id="logs", initial="command"):
                    with TabPane("Command", id="command"):
                        yield _log("command-log")
                    with TabPane("Engine", id="engine"):
                        yield _log("engine-log")
                    with TabPane("Audit", id="audit"):
                        yield _log("audit-log")
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = (
            f"{self.project.name}  ·  {self.insight_url}" if self.insight_url else self.project.name
        )
        self.query_one("#tree", Tree).show_root = True
        self.refresh_tree()
        for e in ledger.read_events(self.project)[-15:]:
            self._audit_line(e)
        self.audit_seen = len(ledger.read_events(self.project))
        self.set_interval(self.poll, self.refresh_tree)
        self.set_interval(1.0, self.poll_audit)
        if self.engine is not None:
            self.run_worker(self._pump_engine(), exclusive=False)
        self.run_worker(self.refresh_status(), exclusive=False)

    # ── tree and detail ──
    def refresh_tree(self) -> None:
        root = build_tree(self.project, self.derived)
        sig = signature(root)
        if sig == self.tree_signature:
            return
        self.tree_signature = sig
        tree = self.query_one("#tree", Tree)
        expanded = {n.data.id for n in _walk(tree.root) if n.is_expanded and n.data is not None}
        chosen = self.selected.id if self.selected else None
        tree.clear()
        tree.root.data = root
        tree.root.set_label(f"[b]{escape(root.label)}[/b]")
        self._fill(tree.root, root, expanded or {n.id for n in root.walk()})
        tree.root.expand()
        if chosen:
            for n in _walk(tree.root):
                if n.data is not None and n.data.id == chosen:
                    self.selected = n.data
                    break
        else:
            self.selected = root
        self.show_selected()

    def _fill(self, parent: Any, node: Node, expanded: set[str]) -> None:
        for child in node.children:
            branch = (
                parent.add(escape(child.label), data=child, expand=child.id in expanded)
                if child.children
                else parent.add_leaf(escape(child.label), data=child)
            )
            if child.children:
                self._fill(branch, child, expanded)

    @on(Tree.NodeHighlighted)
    def on_highlight(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data is not None:
            self.selected = event.node.data
            self.show_selected()

    def show_selected(self) -> None:
        node = self.selected
        if node is None:
            return
        settings = read_settings(self.project)
        agent = settings.agent if settings else "none"
        self.query_one("#detail", Static).update(
            describe(node, insight_url=self.insight_url, agent=agent)
        )
        box = self.query_one("#actions", VerticalScroll)
        box.remove_children()
        self._actions = {}
        busy = self.runner.running
        self._gen += 1  # new ids each time: the old buttons are still being removed
        heading = None
        for n, action in enumerate(actions_for(node)):
            if action.group != heading:
                heading = action.group
                box.mount(Label(f"[b]{escape(heading)}[/b]"))
            bid = f"act-{self._gen}-{n}-{action.key}"
            self._actions[bid] = action
            box.mount(
                Button(
                    escape(action.label) + ("  ($)" if action.agent else ""), id=bid, disabled=busy
                )
            )

    def write_log(self, which: str, line: str) -> None:
        self.logs[which].append(line)
        area = self.query_one(f"#{which}-log", TextArea)
        area.insert(line + "\n", area.document.end)
        area.scroll_end(animate=False)

    def write_tokens(self, line: str) -> None:
        """The token line stands out in the log: ruled off above and below, with a marker."""
        rule = "─" * 60
        for text in (rule, f"▶ {line}", rule):
            self.write_log("command", text)

    def choose(self, node_id: str) -> None:
        """Select a node by id (what moving the cursor does)."""
        tree = self.query_one("#tree", Tree)
        for n in _walk(tree.root):
            if n.data is not None and n.data.id == node_id:
                parent = n.parent
                while parent is not None:
                    parent.expand()
                    parent = parent.parent
                self.selected = n.data
                self.show_selected()
                return

    # ── running commands ──
    @on(Button.Pressed)
    def on_button(self, event: Button.Pressed) -> None:
        action = self._actions.get(event.button.id or "")
        if action is not None:
            self.run_worker(self.perform(action), exclusive=False)  # dialogs need a worker

    async def perform(self, action: Action) -> None:
        text = ""
        if action.prompt:
            answer = await self.push_screen_wait(PromptScreen(action.prompt))
            if not answer:
                return
            text = answer
        args = [a.replace("{}", text) for a in action.args]
        settings = read_settings(self.project)
        extra = list(action.extra)
        if action.agent and not (settings and settings.consent):
            extra.append("--consent")
        if action.confirm:
            note = action.confirm
            if "--consent" in extra:
                note += (
                    " Running it also records your consent to send the files to your coding agent."
                )
            if not await self.push_screen_wait(
                ConfirmScreen(action.label, note, commands.shown([*args, *extra]))
            ):
                return
        await self.execute([*args, *extra])

    async def execute(self, args: list[str]) -> None:
        if self.runner.running:
            return
        self.query_one("#logs", TabbedContent).active = "command"
        self.last_output = []
        self.write_log("command", f"$ {commands.shown(args)}")
        self.show_selected()  # disables the buttons while it runs

        def on_line(ln: str) -> None:
            self.last_output.append(ln)
            if ln.startswith("tokens · "):
                self.write_tokens(ln)
            else:
                self.write_log("command", ln)

        code, seconds = await self.runner.run(
            self.argv_for(args, self.project), on_line, cwd=self.project
        )
        self.last_exit = code
        if not any(ln.startswith("tokens · ") for ln in self.last_output):
            self.write_tokens("tokens · none used (no coding agent ran)")
        self.write_log("command", f"{'✓' if code == 0 else '✗'} exit {code} ({seconds:.1f}s)")
        self.show_selected()
        self.refresh_tree()
        self.run_worker(self.refresh_status(), exclusive=False)

    async def action_cancel(self) -> None:
        if self.runner.running:
            await self.runner.cancel()
            self.write_log("command", "stopped")

    def action_step(self, delta: int) -> None:
        """Up and down move between the action buttons (Tab does too)."""
        buttons = [b for b in self.query("#actions Button") if isinstance(b, Button)]
        buttons = [b for b in buttons if not b.disabled]
        focused = self.focused
        if not isinstance(focused, Button) or focused not in buttons:
            raise SkipAction()
        target = buttons[max(0, min(len(buttons) - 1, buttons.index(focused) + delta))]
        target.focus()
        target.scroll_visible()

    def action_copy(self) -> None:
        tab = self.query_one("#logs", TabbedContent).active
        lines = self.last_output if tab == "command" and self.last_output else self.logs[tab]
        if not lines:
            self.notify("Nothing to copy yet.")
            return
        text = "\n".join(lines)
        if not clipboard.copy(text):
            self.copy_to_clipboard(text)
        self.notify(f"Copied {len(lines)} line(s) from the {tab} log.")

    # ── status, audit, engine ──
    async def refresh_status(self) -> None:
        argv = self.argv_for(("list", "tasks", "--json", "--no-tests"), self.project)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.project,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 120)
        except (TimeoutError, OSError):
            return
        text = out.decode(errors="replace")
        start = text.find('{\n  "done"')
        if start < 0:
            return
        try:
            data = json.loads(text[start:])
        except json.JSONDecodeError:
            return
        found = {t["id"]: t["status"] for p in data.get("plans", []) for t in p.get("tasks", [])}
        if found != self.derived:
            self.derived = found
            self.refresh_tree()

    def poll_audit(self) -> None:
        events = ledger.read_events(self.project)
        for e in events[self.audit_seen :]:
            self._audit_line(e)
        self.audit_seen = len(events)

    def _audit_line(self, e: dict[str, Any]) -> None:
        mark = {"ok": "✓", "refused": "!", "failed": "✗"}.get(e.get("result", ""), " ")
        who = (e.get("actor") or {}).get("initials") or ""
        self.write_log(
            "audit",
            f"{mark} {str(e.get('ts', ''))[11:19]} {who:<3} {e.get('kind', '')}  {' '.join(e.get('entities', []))}  {e.get('detail', '')}".rstrip(),
        )

    async def _pump_engine(self) -> None:
        assert self.engine is not None
        if self.engine.proc is None:
            await self.engine.start()
        await self.engine.pump(lambda ln: self.write_log("engine", ln))
        self.write_log("engine", "— the engine stopped —")

    # ── quit ──
    async def action_refresh(self) -> None:
        self.tree_signature = ""
        self.refresh_tree()
        self.run_worker(self.refresh_status(), exclusive=False)

    async def action_quit_app(self) -> None:
        await self.runner.cancel()
        if self.engine is not None:
            await self.engine.stop()
        self.exit()


def _walk(node: Any) -> list[Any]:
    out = [node]
    for c in node.children:
        out += _walk(c)
    return out


def run(project: Path, *, engine: Engine | None, insight_url: str) -> None:
    TuvlDevApp(project, engine=engine, insight_url=insight_url).run()
