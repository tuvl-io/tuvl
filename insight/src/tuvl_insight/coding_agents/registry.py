"""The coding agents tuvl knows, with how each is run headlessly.

``verified`` means the headless invocation below was checked against the vendor's own
documentation (or the installed ``--help``) on :data:`VERIFIED_ON`. Only verified agents can be
driven by tuvl; the others are listed so a developer sees them, with that said.
"""

from __future__ import annotations

from dataclasses import dataclass

VERIFIED_ON = "2026-10-19"


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    #: executable names, tried in order
    binaries: tuple[str, ...]
    #: the documented non-interactive invocation
    headless: str
    docs: str
    verified: bool
    #: a generic executable name (``agent``) is accepted only if its output contains this
    identify: str = ""
    version_args: tuple[str, ...] = ("--version",)
    notes: str = ""


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        "claude",
        "Claude Code",
        ("claude",),
        'claude -p "<prompt>" --output-format stream-json --permission-mode … --allowedTools …',
        "installed `claude --help`",
        True,
    ),
    AgentSpec(
        "codex",
        "OpenAI Codex CLI",
        ("codex",),
        'codex exec --sandbox workspace-write --json "<prompt>"',
        "https://developers.openai.com/codex/noninteractive",
        True,
    ),
    AgentSpec(
        "gemini",
        "Gemini CLI",
        ("gemini",),
        'gemini -p "<prompt>" --output-format stream-json --approval-mode …',
        "https://geminicli.com/docs/cli/headless/",
        True,
    ),
    AgentSpec(
        "antigravity",
        "Google Antigravity CLI",
        ("agy",),
        'agy --mode accept-edits -p "<prompt>"',
        "https://antigravity.google/docs/cli/headless (--mode: `agy --help`)",
        True,
        notes="--mode accept-edits approves edits in the working folder only; a shell command is denied (checked on agy 1.4.10)",
    ),
    AgentSpec(
        "cursor",
        "Cursor CLI",
        ("cursor-agent", "agent"),
        'agent -p --force --output-format stream-json "<prompt>"',
        "https://cursor.com/docs/cli/headless",
        True,
        identify="cursor",
        notes="the executable is `agent` (older installs: `cursor-agent`)",
    ),
    AgentSpec(
        "copilot",
        "GitHub Copilot CLI",
        ("copilot",),
        'copilot -p "<prompt>" --allow-tool=… --output-format json -s',
        "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference",
        True,
    ),
    AgentSpec(
        "amp",
        "Amp",
        ("amp",),
        'amp -x "<prompt>"',
        "https://ampcode.com/docs/cli/execute-mode",
        True,
    ),
    AgentSpec(
        "qwen",
        "Qwen Code",
        ("qwen",),
        'qwen --prompt "<prompt>"',
        "https://www.mintlify.com/QwenLM/qwen-code/cli/overview",
        False,
        notes="headless flags not yet checked against the vendor's docs",
    ),
    AgentSpec(
        "aider",
        "Aider",
        ("aider",),
        'aider --message "<prompt>" --yes-always',
        "https://aider.chat/docs/scripting.html",
        False,
        notes="headless flags not yet checked against the vendor's docs",
    ),
    AgentSpec(
        "opencode",
        "OpenCode",
        ("opencode",),
        'opencode run "<prompt>"',
        "https://opencode.ai/docs/cli/",
        False,
        notes="headless flags not yet checked against the vendor's docs",
    ),
    AgentSpec(
        "goose",
        "Goose",
        ("goose",),
        'goose run -t "<prompt>"',
        "https://block.github.io/goose/docs/guides/goose-cli-commands",
        False,
        notes="headless flags not yet checked against the vendor's docs",
    ),
)

_BY_ID = {a.id: a for a in AGENTS}


def get(agent_id: str) -> AgentSpec | None:
    return _BY_ID.get(agent_id)
