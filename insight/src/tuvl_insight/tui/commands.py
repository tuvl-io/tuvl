"""Running a `tuvl` command for the terminal UI: the same CLI a person or an agent would run."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

ENTRY = "from tuvl.cli import main; main()"


def cli_argv(args: tuple[str, ...] | list[str], project: Path) -> list[str]:
    """The command line for ``tuvl <args> -d <project>``, using this Python so it is the same tuvl."""
    return [sys.executable, "-c", ENTRY, *args, "-d", str(project)]


def shown(args: tuple[str, ...] | list[str]) -> str:
    return "tuvl " + " ".join(args)


class Runner:
    """Runs one command at a time, streaming its lines to a callback."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def run(
        self,
        argv: list[str],
        on_line: Callable[[str], None | Awaitable[None]],
        *,
        cwd: Path | None = None,
    ) -> tuple[int, float]:
        started = time.monotonic()
        env = {**os.environ, "NO_COLOR": "1", "COLUMNS": "120"}
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
        assert self.proc.stdout is not None
        async for raw in self.proc.stdout:
            result = on_line(raw.decode(errors="replace").rstrip("\n"))
            if asyncio.iscoroutine(result):
                await result
        code = await self.proc.wait()
        return code, time.monotonic() - started

    async def cancel(self) -> None:
        if self.running and self.proc is not None:
            self.proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 5)
            if self.proc.returncode is None:
                self.proc.kill()
