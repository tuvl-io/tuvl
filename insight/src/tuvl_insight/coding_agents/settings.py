"""The developer's own settings, ``.tuvl/agent.yaml`` — never committed.

Initials stamp the ids of ideas and specs this developer creates; ``agent`` is the coding agent
tuvl may run for them (``none`` when they drive their agent themselves).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SETTINGS_RELPATH = ".tuvl/agent.yaml"
_INITIALS = re.compile(r"^[A-Z][A-Z0-9]{1,3}$")


@dataclass(frozen=True)
class Settings:
    initials: str = ""
    agent: str = "none"
    #: when this developer agreed that idea and project files may be sent to their coding agent
    consent: str = ""
    #: argv of the `generic` agent: any CLI that reads a prompt on stdin and edits the working directory
    command: tuple[str, ...] = ()


def valid_initials(value: str) -> bool:
    return _INITIALS.match(value) is not None


def initials_from(name: str) -> str:
    """``Ada Lovelace`` → ``AL``; empty when no usable letters."""
    letters = [w[0] for w in re.findall(r"[A-Za-z0-9]+", name)][:3]
    out = "".join(letters).upper()
    return out if valid_initials(out) else ""


def read_settings(project: Path | str) -> Settings | None:
    path = Path(project) / SETTINGS_RELPATH
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    dev = data.get("developer")
    dev = dev if isinstance(dev, dict) else {}
    command = data.get("command")
    return Settings(
        str(dev.get("initials", "")),
        str(data.get("agent") or "none"),
        str(data.get("consent") or ""),
        tuple(str(c) for c in command) if isinstance(command, list) else (),
    )


def write_settings(project: Path | str, settings: Settings) -> Path:
    path = Path(project) / SETTINGS_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "kind": "DeveloperSettings",
        "version": "tuvl/v2",
        "developer": {"initials": settings.initials},
        "agent": settings.agent,
    }
    if settings.command:
        body["command"] = list(settings.command)
    if settings.consent:
        body["consent"] = settings.consent
    path.write_text(
        "# This developer's settings on this machine; git-ignored.\n"
        + yaml.safe_dump(body, sort_keys=False)
    )
    return path


def grant_consent(project: Path | str, *, now: str) -> Settings:
    """Record that the developer agreed idea and project files may be sent to their coding agent."""
    current = read_settings(project) or Settings()
    updated = Settings(current.initials, current.agent, now, current.command)
    write_settings(project, updated)
    return updated
