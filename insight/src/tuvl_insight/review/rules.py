"""The review rules (``C``): what a code agent's Python may not do, and why."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning", "info"]

#: code → (severity, what it means)
RULES: dict[str, tuple[Severity, str]] = {
    "C001": (
        "error",
        'A code agent is `@agent("run")` on an `async def f(inp, ctx)` with exactly those two parameters',
    ),
    "C002": (
        "error",
        "Runs a process or evaluates code (`subprocess`, `os.system`, `eval`, `exec`, `pickle.loads`, …)",
    ),
    "C003": (
        "error",
        "Calls the network directly (`requests`, `httpx`, `urllib`, `socket`, …): use `ctx.http`, which carries the idempotency key",
    ),
    "C004": (
        "error",
        "Reads the process environment (`os.environ`, `os.getenv`, dotenv): settings come in as `code.with` parameters, secrets stay in `.env` for the engine",
    ),
    "C005": (
        "error",
        "Writes, moves or deletes files: use `ctx.storage(name)`. Reading a file is a warning",
    ),
    "C006": (
        "warning",
        "Not a pure function of its inputs (random, uuid, time, `datetime.now`): a replay or a test will differ; pass the value in or derive it from `ctx.run_id`",
    ),
    "C007": ("error", "Does work when the module is imported (a bare call at module level)"),
    "C008": (
        "error",
        "Keeps state between runs in the module (`global`, or mutating a module-level list/dict/set)",
    ),
    "C009": ("warning", "A hard-coded URL or host: make it a `code.with` parameter"),
    "C010": ("error", "A secret in the source (a key, token or private key in a string)"),
    "C011": ("error", "Returns a signal the workflow does not route"),
    "C012": (
        "error",
        "Writes through `ctx.db` (or `ctx.storage`) in an agent declared `effect: read`",
    ),
    "C013": ("warning", "An `@agent` that no `engine: code` agent in a workflow runs"),
    "C900": (
        "warning",
        "A finding of `ruff` (the project's own lint configuration, else its defaults)",
    ),
}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    file: str
    line: int
    message: str

    def to_json(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
        }

    def text(self) -> str:
        return f"{self.code} {self.file}:{self.line}: {self.message}"


def finding(
    code: str, file: str, line: int, message: str, severity: Severity | None = None
) -> Finding:
    return Finding(code, severity or RULES[code][0], file, line, message)
