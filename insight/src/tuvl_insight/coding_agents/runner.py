"""Run a coding agent headlessly on a prompt, in a working directory.

An adapter turns "this prompt, in this directory, with these limits" into the agent's own command
line and reads its answer. tuvl never trusts the agent to stay in scope: the caller checks what
changed afterwards. Where the agent can enforce limits itself (Claude Code's ``--restricted``: no
code-running tools, file tools confined to the directory) the adapter asks it to.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class AgentError(RuntimeError):
    pass


@dataclass
class RunResult:
    ok: bool
    text: str = ""
    error: str = ""
    returncode: int = 0
    seconds: float = 0.0
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None
    denials: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class Limits:
    timeout: float = 600.0
    budget_usd: float | None = None
    model: str | None = None
    #: the agent may look but not change anything (a reviewer)
    read_only: bool = False


class Usage:
    """What the agents run by this process used, so a command can say so when it ends."""

    def __init__(self) -> None:
        self.calls = 0
        self.input = self.output = 0
        self.cost = 0.0
        self.unreported: set[str] = set()

    def add(self, agent: str, result: RunResult) -> None:
        self.calls += 1
        if result.input_tokens is None and result.output_tokens is None:
            self.unreported.add(agent)
        self.input += result.input_tokens or 0
        self.output += result.output_tokens or 0
        self.cost += result.cost_usd or 0.0

    def line(self) -> str:
        if not self.calls:
            return ""
        parts = [f"{self.input:,} in", f"{self.output:,} out"] if self.input or self.output else []
        if self.cost:
            parts.append(f"${self.cost:.3f}")
        if self.unreported:
            parts.append(f"not reported by {', '.join(sorted(self.unreported))}")
        return f"tokens · {' · '.join(parts)} · {self.calls} agent call(s)"


usage = Usage()


class Adapter(Protocol):
    id: str
    #: tuvl lets it change files only when its headless mode can confine the change
    can_write: bool
    #: the prompt goes on stdin; otherwise it is the last argument (and has a length limit)
    stdin: bool

    def command(self, binary: str, limits: Limits) -> list[str]: ...

    def parse(self, stdout: str, stderr: str, returncode: int) -> RunResult: ...


class ClaudeAdapter:
    """Claude Code: ``claude -p`` with the prompt on stdin.

    ``--restricted`` removes Bash and the other code-running tools and confines the file tools to
    the working directory; ``--permission-prompts none`` makes anything that would ask be denied;
    ``--strict-mcp-config`` keeps MCP servers out; no session is saved.
    """

    id = "claude"
    can_write = True
    stdin = True

    def command(self, binary: str, limits: Limits) -> list[str]:
        argv = [
            binary,
            "-p",
            "--restricted",
            "--strict-mcp-config",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--output-format",
            "json",
        ]
        if limits.read_only:  # no Edit or Write tool exists at all
            argv += ["--tools", "Read,Glob,Grep"]
        else:
            argv += ["--permission-mode", "acceptEdits"]
        if limits.budget_usd is not None:
            argv += ["--max-budget-usd", f"{limits.budget_usd:g}"]
        if limits.model:
            argv += ["--model", limits.model]
        return argv

    def parse(self, stdout: str, stderr: str, returncode: int) -> RunResult:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return RunResult(
                False, error=(stderr or stdout or "no output")[:500], returncode=returncode
            )
        if not isinstance(data, dict):
            return RunResult(False, error="unexpected output", returncode=returncode)
        denied = [
            str(d.get("tool_name") or d) if isinstance(d, dict) else str(d)
            for d in data.get("permission_denials") or []
        ]
        raw = data.get("usage")
        used: dict[str, Any] = raw if isinstance(raw, dict) else {}
        sent = [
            used.get(k)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ]
        failed = bool(data.get("is_error")) or data.get("subtype") not in (None, "success")
        return RunResult(
            ok=not failed and returncode == 0,
            text=str(data.get("result") or ""),
            error=str(data.get("result") or data.get("subtype") or "failed") if failed else "",
            returncode=returncode,
            cost_usd=data.get("total_cost_usd"),
            input_tokens=sum(n for n in sent if isinstance(n, int)) if used else None,
            output_tokens=used.get("output_tokens") if used else None,
            turns=data.get("num_turns"),
            denials=denied,
        )


class GenericAdapter:
    """Any command that reads the prompt on stdin and edits its working directory.

    It cannot restrict the agent, so the caller's check of what changed is the only limit.
    """

    id = "generic"
    can_write = True  # whatever the developer's own command does: tuvl only checks the result
    stdin = True

    def __init__(self, argv: tuple[str, ...]) -> None:
        if not argv:
            raise AgentError(
                "the generic agent has no command: tuvl use agent generic --command '…'"
            )
        self.argv = list(argv)

    def command(self, binary: str, limits: Limits) -> list[str]:
        return list(self.argv)

    def parse(self, stdout: str, stderr: str, returncode: int) -> RunResult:
        return RunResult(
            ok=returncode == 0,
            text=stdout.strip(),
            error="" if returncode == 0 else (stderr or stdout or f"exit {returncode}")[:500],
            returncode=returncode,
        )


class _PlainAdapter:
    """An agent whose final answer is its standard output (the default of these CLIs); no cost is reported."""

    id = ""
    can_write = False
    stdin = True
    arg_limit = 100_000

    def parse(self, stdout: str, stderr: str, returncode: int) -> RunResult:
        ok = returncode == 0
        return RunResult(
            ok=ok,
            text=stdout.strip(),
            error="" if ok else (stderr or stdout or f"exit {returncode}").strip()[:500],
            returncode=returncode,
        )


class CodexAdapter(_PlainAdapter):
    """OpenAI Codex CLI: ``codex exec -`` reads the prompt from stdin; its sandbox confines writes to the folder."""

    id = "codex"
    can_write = True

    def command(self, binary: str, limits: Limits) -> list[str]:
        sandbox = "read-only" if limits.read_only else "workspace-write"
        argv = [binary, "exec", "--sandbox", sandbox, "--skip-git-repo-check", "--ephemeral"]
        if limits.model:
            argv += ["--model", limits.model]
        return [*argv, "-"]


class GeminiAdapter(_PlainAdapter):
    """Gemini CLI: headless when stdin is not a terminal; ``auto_edit`` approves edit tools only, ``plan`` is read-only."""

    id = "gemini"
    can_write = True

    def command(self, binary: str, limits: Limits) -> list[str]:
        argv = [binary, "--approval-mode", "plan" if limits.read_only else "auto_edit"]
        if limits.model:
            argv += ["--model", limits.model]
        return argv


class AntigravityAdapter(_PlainAdapter):
    """Antigravity CLI (``agy -p``): unapproved tools are denied by default, which is what a review wants.

    ``--mode accept-edits`` approves file edits inside the working folder only: a shell command or an edit
    outside the folder is still denied, and nothing is skipped (never ``--dangerously-skip-permissions``).
    Seen with agy 1.4.10 (it is in ``agy --help``, not on the headless page); a review leaves it off.
    """

    id = "antigravity"
    can_write = True
    stdin = False
    _DENIED = re.compile(r'a tool required the "([\w.-]+)" permission')

    def command(self, binary: str, limits: Limits) -> list[str]:
        argv = [binary, "--print-timeout", f"{max(int(limits.timeout), 1)}s"]
        if not limits.read_only:
            argv += ["--mode", "accept-edits"]
        if limits.model:
            argv += ["--model", limits.model]
        return [*argv, "-p"]

    def parse(self, stdout: str, stderr: str, returncode: int) -> RunResult:
        result = super().parse(stdout, stderr, returncode)
        denied = self._DENIED.search(stderr)
        if denied:  # agy exits 0 when a tool was denied, and says so only on stderr
            result.denials.append(denied.group(1))
        return result


class CursorAdapter(_PlainAdapter):
    """Cursor CLI (``agent -p``): without ``--force`` changes are only proposed, so it can review but not build."""

    id = "cursor"
    stdin = False

    def command(self, binary: str, limits: Limits) -> list[str]:
        argv = [binary]
        if limits.model:
            argv += ["--model", limits.model]
        return [*argv, "-p"]


class CopilotAdapter(_PlainAdapter):
    """GitHub Copilot CLI (``copilot -p -s``): no tool is allowed unless granted, so it reviews."""

    id = "copilot"
    stdin = False

    def command(self, binary: str, limits: Limits) -> list[str]:
        argv = [binary, "-s"]
        if limits.model:
            argv += [f"--model={limits.model}"]
        return [*argv, "-p"]


class AmpAdapter(_PlainAdapter):
    """Amp execute mode (``amp -x``) prints the agent's final message; used to review."""

    id = "amp"
    stdin = False

    def command(self, binary: str, limits: Limits) -> list[str]:
        return [binary, "-x"]


_ADAPTERS: dict[str, type[Adapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "antigravity": AntigravityAdapter,
    "cursor": CursorAdapter,
    "copilot": CopilotAdapter,
    "amp": AmpAdapter,
}
DRIVEN = (*_ADAPTERS, "generic")
WRITERS = tuple(i for i, cls in _ADAPTERS.items() if cls.can_write) + ("generic",)


def capability(agent_id: str) -> str:
    """``write`` (drafts and builds), ``review`` (can only read and answer) or ``""`` (not driven yet)."""
    if agent_id in WRITERS:
        return "write"
    return "review" if agent_id in _ADAPTERS else ""


def adapter_for(agent_id: str, command: tuple[str, ...] = (), *, write: bool = False) -> Adapter:
    """The adapter for *agent_id*; with ``write=True`` it must be one that may change files."""
    if agent_id == "generic":
        return GenericAdapter(command)
    cls = _ADAPTERS.get(agent_id)
    if cls is None:
        raise AgentError(
            f"tuvl cannot drive '{agent_id}' yet; it can run: {', '.join(DRIVEN)} "
            "(more adapters are planned, see `tuvl list agents`)"
        )
    if write and not cls.can_write:
        raise AgentError(
            f"{agent_id} can review code through tuvl, but cannot draft or build yet: its headless mode has no way "
            f"to allow writes in one folder only. Writers: {', '.join(WRITERS)}"
        )
    return cls()


def binary_for(agent_id: str, command: tuple[str, ...] = ()) -> str:
    if agent_id == "generic":
        path = shutil.which(command[0]) if command else None
        if not path:
            raise AgentError(f"'{command[0] if command else ''}' was not found on PATH")
        return path
    from tuvl_insight.coding_agents import detect

    for found in detect():
        if found.spec.id == agent_id:
            return found.path
    raise AgentError(f"{agent_id} was not found on PATH")


def run_agent(
    adapter: Adapter,
    binary: str,
    prompt: str,
    *,
    workdir: Path,
    limits: Limits,
    env: dict[str, str] | None = None,
) -> RunResult:
    """Run the agent in *workdir* with *prompt* on stdin. Never raises for an agent that failed."""
    argv = adapter.command(binary, limits)
    stdin: str | None = prompt
    if not adapter.stdin:
        limit = getattr(adapter, "arg_limit", 100_000)
        if len(prompt) > limit:
            return RunResult(
                False,
                error=f"the prompt ({len(prompt):,} characters) is too long for {adapter.id}, which takes it as an "
                f"argument (limit {limit:,}): use a smaller input or another agent",
            )
        argv, stdin = [*argv, prompt], None
    started = time.monotonic()
    try:
        stdio: dict[str, Any] = (
            {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        )
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=limits.timeout,
            env={**os.environ, **(env or {})},
            check=False,
            **stdio,
        )
    except subprocess.TimeoutExpired:
        return RunResult(
            False, error=f"timed out after {limits.timeout:g}s", seconds=time.monotonic() - started
        )
    except OSError as exc:
        return RunResult(
            False, error=f"could not start the agent: {exc}", seconds=time.monotonic() - started
        )
    result = adapter.parse(done.stdout, done.stderr, done.returncode)
    result.seconds = time.monotonic() - started
    result.stdout, result.stderr = done.stdout, done.stderr
    usage.add(adapter.id, result)
    return result
