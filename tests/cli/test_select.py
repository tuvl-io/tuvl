"""The arrow-key menu, on its own and in a real terminal."""

import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tuvl_insight.cli import select
from tuvl_insight.cli.select import DOWN, ENTER, UP, Choice, Menu

CHOICES = [
    Choice("claude", "Claude Code", "builds"),
    Choice("cursor", "Cursor CLI"),
    Choice("none", "none"),
]


def keys(*seq: str):
    it = iter(seq)
    return lambda: next(it)


def test_the_default_is_highlighted_and_unknown_defaults_fall_back_to_the_first() -> None:
    assert Menu(CHOICES, "cursor").current.value == "cursor"
    assert Menu(CHOICES, "missing").current.value == "claude"
    assert Menu(CHOICES).current.value == "claude"


def test_arrows_wrap_around_and_letters_and_numbers_move() -> None:
    m = Menu(CHOICES)
    assert m.press(UP) is False and m.current.value == "none"  # wraps to the end
    m.press(DOWN)
    assert m.current.value == "claude"
    m.press("j")
    m.press("j")
    assert m.current.value == "none"
    m.press("k")
    assert m.current.value == "cursor"
    m.press("1")
    assert m.current.value == "claude"
    m.press("9")  # beyond the list: ignored
    m.press("x")
    assert m.current.value == "claude"
    assert m.press(ENTER) is True


def test_only_the_highlighted_line_has_the_marker() -> None:
    lines = Menu(CHOICES, "cursor").lines()
    assert [("❯" in line) for line in lines] == [False, True, False]
    assert "builds" in lines[0] and "Claude Code" in lines[0]


def test_pick_returns_the_value_after_the_keys_and_leaves_one_line() -> None:
    out = io.StringIO()
    chosen = select.pick("Agent", CHOICES, "claude", keys=keys(DOWN, DOWN, ENTER), out=out)
    assert chosen == "none"
    assert out.getvalue().endswith("Agent: \x1b[1mnone\x1b[0m\n")
    assert out.getvalue().count("\x1b[?25l") == 1 and out.getvalue().count("\x1b[?25h") == 1


def test_pick_enter_takes_the_default() -> None:
    assert select.pick("Agent", CHOICES, "cursor", keys=keys(ENTER), out=io.StringIO()) == "cursor"


def test_ctrl_d_ends_the_input_and_the_cursor_comes_back() -> None:
    out = io.StringIO()
    with pytest.raises(EOFError):
        select.pick("Agent", CHOICES, keys=keys(select.ABORT), out=out)
    assert out.getvalue().endswith("\x1b[?25h")


def test_without_a_terminal_there_is_no_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(select, "interactive", lambda: False)
    assert select.pick("Agent", CHOICES) is None


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pseudo-terminal")
def test_in_a_real_terminal_the_arrow_keys_move_and_enter_chooses(tmp_path: Path) -> None:
    import pty
    import select as io_select

    script = tmp_path / "ask.py"
    script.write_text(
        textwrap.dedent(
            """
            from tuvl_insight.cli.select import Choice, pick
            got = pick("Agent", [Choice("a", "Alpha"), Choice("b", "Beta"), Choice("c", "Gamma")], "a")
            print("RESULT=" + str(got))
            """
        )
    )
    master, slave = pty.openpty()
    env = {**os.environ, "TERM": "xterm", "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    seen = b""

    def read_until(marker: bytes, timeout: float = 10.0) -> None:
        nonlocal seen
        import time

        end = time.time() + timeout
        while marker not in seen and time.time() < end:
            if io_select.select([master], [], [], 0.2)[0]:
                try:
                    seen += os.read(master, 4096)
                except OSError:
                    return

    try:
        read_until(b"Alpha")  # the menu is up
        os.write(master, b"\x1b[B")  # Down
        os.write(master, b"\x1b[B")  # Down
        os.write(master, b"\x1b[A")  # Up
        os.write(master, b"\r")
        read_until(b"RESULT=")
        read_until(b"\n", 2)
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)
    assert b"RESULT=b" in seen, seen
