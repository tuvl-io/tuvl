"""A one-choice menu for the terminal: Up and Down (or j and k, or a number) move, Enter chooses.

Needs a real terminal and no extra package. Without one (a pipe, CI, a coding agent) :func:`pick`
returns ``None`` and the caller asks in plain text instead.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TextIO

UP, DOWN, ENTER, ABORT = "up", "down", "enter", "abort"

_CYAN, _DIM, _BOLD, _OFF = "\x1b[36m", "\x1b[2m", "\x1b[1m", "\x1b[0m"


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    hint: str = ""


class Menu:
    """The state of the menu and what it shows, apart from the terminal, so both can be tested."""

    def __init__(self, choices: Sequence[Choice], default: str = "") -> None:
        if not choices:
            raise ValueError("a menu needs at least one choice")
        self.choices = list(choices)
        self.index = next((i for i, c in enumerate(self.choices) if c.value == default), 0)

    @property
    def current(self) -> Choice:
        return self.choices[self.index]

    def press(self, key: str) -> bool:
        """Apply a key; True when it chose the highlighted item."""
        if key in (UP, "k"):
            self.index = (self.index - 1) % len(self.choices)
        elif key in (DOWN, "j"):
            self.index = (self.index + 1) % len(self.choices)
        elif key.isdigit() and 1 <= int(key) <= len(self.choices):
            self.index = int(key) - 1
        return key == ENTER

    def lines(self) -> list[str]:
        width = max(len(c.label) for c in self.choices)
        out = []
        for i, c in enumerate(self.choices):
            hint = f"  {_DIM}{c.hint}{_OFF}" if c.hint else ""
            if i == self.index:
                out.append(f"  {_CYAN}❯ {_BOLD}{c.label.ljust(width)}{_OFF}{hint}")
            else:
                out.append(f"    {c.label.ljust(width)}{hint}")
        return out


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only

    @contextlib.contextmanager
    def keyboard() -> Iterator[None]:
        yield

    def read_key() -> str:
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": UP, "P": DOWN}.get(msvcrt.getwch(), "")
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):
            return ENTER
        return ABORT if ch == "\x04" else ch

else:

    @contextlib.contextmanager
    def keyboard() -> Iterator[None]:
        """Keys arrive one at a time, without Enter, for as long as the menu is up (Ctrl-C still works)."""
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def read_key() -> str:
        import select

        fd = sys.stdin.fileno()
        data = os.read(fd, 1)
        if data == b"\x1b":  # an arrow key arrives as ESC [ A, or ESC O A
            while select.select([fd], [], [], 0.05)[0] and len(data) < 8:
                data += os.read(fd, 1)
                if data[-1:].isalpha() or data[-1:] == b"~":
                    break
        if data in (b"\x1b[A", b"\x1bOA"):
            return UP
        if data in (b"\x1b[B", b"\x1bOB"):
            return DOWN
        if data in (b"\r", b"\n"):
            return ENTER
        if data in (b"\x04", b""):
            return ABORT
        return data.decode("utf-8", errors="ignore")


def pick(
    title: str,
    choices: Sequence[Choice],
    default: str = "",
    *,
    keys: Callable[[], str] = read_key,
    out: TextIO | None = None,
) -> str | None:
    """Show the menu and return the chosen value, or ``None`` when there is no terminal to show it on.

    Ctrl-C raises ``KeyboardInterrupt``, as a plain prompt does; Ctrl-D raises ``EOFError``.
    """
    if out is None:
        if not interactive():
            return None
        out = sys.stdout
        if sys.platform == "win32":  # pragma: no cover
            os.system("")  # turns on ANSI escapes in the Windows console
    menu = Menu(choices, default)
    with keyboard() if keys is read_key else contextlib.nullcontext():
        return _run(title, menu, keys, out)


def _run(title: str, menu: Menu, keys: Callable[[], str], out: TextIO) -> str:
    out.write(f"{title}  {_DIM}(Up/Down to move, Enter to choose){_OFF}\n")
    out.write("\x1b[?25l")  # hide the cursor while the menu is up
    drawn = 0
    try:
        while True:
            if drawn:
                out.write(f"\x1b[{drawn}A\x1b[J")
            lines = menu.lines()
            out.write("\n".join(lines) + "\n")
            out.flush()
            drawn = len(lines)
            key = keys()
            if key == ABORT:
                raise EOFError
            if menu.press(key):
                break
    finally:
        out.write("\x1b[?25h")
        out.flush()
    out.write(f"\x1b[{drawn + 1}A\x1b[J{title}: {_BOLD}{menu.current.label}{_OFF}\n")
    out.flush()
    return menu.current.value
