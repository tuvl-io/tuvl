"""The dev engine as a child process whose output the terminal UI shows."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable


class Engine:
    """``uvicorn tuvl.main:app --reload …`` started by ``tuvl dev``; stopped when the UI quits."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.argv, self.env = argv, env
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env,
        )

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def pump(self, on_line: Callable[[str], None | Awaitable[None]]) -> int:
        assert self.proc is not None and self.proc.stdout is not None
        async for raw in self.proc.stdout:
            result = on_line(raw.decode(errors="replace").rstrip("\n"))
            if asyncio.iscoroutine(result):
                await result
        return await self.proc.wait()

    async def stop(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except TimeoutError:
            self.proc.kill()
            with contextlib.suppress(Exception):
                await self.proc.wait()
