"""What every command that runs a coding agent needs first: which agent, and the developer's consent."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from tuvl_insight import audit, gitutil
from tuvl_insight.coding_agents import detect, grant_consent, read_settings, runner
from tuvl_insight.spec import lifecycle

console = Console()


@dataclass(frozen=True)
class Chosen:
    id: str
    command: tuple[str, ...]
    label: str


def choose_agent(project: Path, override: str | None, *, write: bool = True) -> Chosen:
    """The agent to run: ``override`` or the developer's own choice, checked to be drivable and installed.

    ``write`` (the default: drafting and building change files) refuses an agent that can only review.
    """
    settings = read_settings(project)
    agent_id = (override or (settings.agent if settings else "none")).strip().lower()
    if agent_id == "none":
        console.print("[red]No coding agent selected.[/red] Choose one: `tuvl use agent <agent>`.")
        raise typer.Exit(1)
    command = settings.command if settings else ()
    try:
        runner.adapter_for(agent_id, command, write=write)
        runner.binary_for(agent_id, command)
    except runner.AgentError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from None
    label = agent_id
    for found in detect():
        if found.spec.id == agent_id:
            version = found.version.replace(f"({found.spec.name})", "").strip()
            label = f"{found.spec.name} {version}".strip()
    return Chosen(agent_id, command, label)


def need_git(project: Path, what: str) -> None:
    if not gitutil.available() or not gitutil.inside_work_tree(project):
        console.print(
            f"[red]`{what}` needs a git repository[/red] (the work is done on branches): `git init -b develop`."
        )
        raise typer.Exit(1)


def ensure_consent(project: Path, chosen: Chosen, *, flag: bool, what: str, command: str) -> None:
    """Idea and project files go to a third party's agent: ask once, remember, record."""
    settings = read_settings(project)
    if settings and settings.consent:
        return
    agreed = flag
    if not agreed and sys.stdin.isatty():
        agreed = typer.confirm(
            f"{what} will be sent to {chosen.label}, run from your own installation. tuvl sends nothing itself. Agree?",
            default=False,
        )
    if not agreed:
        console.print(
            f"[red]Needs your consent[/red] to send {what} to {escape(chosen.label)}: run again with "
            "--consent (asked once; stored in .tuvl/agent.yaml)."
        )
        raise typer.Exit(1)
    grant_consent(project, now=lifecycle.now_stamp())
    audit.emit(project, "consent.granted", command=command, detail=chosen.id)


def choose_reviewer(project: Path, override: str | None, author: str) -> Chosen:
    """The reviewer: the one asked for, else a different drivable agent than the author's when one is
    installed, else the author's own (a fresh session that has not seen the work)."""
    if override:
        return choose_agent(project, override, write=False)
    settings = read_settings(project)
    if settings and settings.agent not in ("none", ""):
        installed = {d.spec.id for d in detect()}
        for other in runner.DRIVEN:
            if other != author and other in installed and other != "generic":
                return choose_agent(project, other, write=False)
    return choose_agent(project, author if author != "none" else None, write=False)
