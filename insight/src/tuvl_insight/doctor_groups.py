"""The check groups that need the developer toolchain: git, skills, coding agents, ideas, the audit ledger."""

from __future__ import annotations

import shutil
from pathlib import Path

from tuvl.doctor import Check, Context, Fix, group
from tuvl_insight import gitutil


@group("git", toolchain=True)
def git_group(ctx: Context) -> list[Check]:
    if shutil.which("git") is None:
        return [
            Check("git", "git", "warn", "git is not installed", "https://git-scm.com/downloads")
        ]
    out = [Check("git", "git", "ok", gitutil.out(ctx.project, "--version"))]
    if not gitutil.inside_work_tree(ctx.project):
        out.append(
            Check(
                "git",
                "repository",
                "warn",
                "not a git repository",
                "git init -b develop",
                Fix("git init -b develop", lambda: gitutil.init_repo(ctx.project, "develop")),
            )
        )
        return out
    out.append(Check("git", "repository", "ok", "git work tree"))
    branch = gitutil.out(ctx.project, "symbolic-ref", "--short", "-q", "HEAD")
    out.append(
        Check("git", "branch", "ok", branch)
        if branch
        else Check("git", "branch", "warn", "detached HEAD", "git switch develop")
    )
    for key in ("user.name", "user.email"):
        value = gitutil.config_get(ctx.project, key)
        out.append(
            Check("git", key, "ok", value)
            if value
            else Check("git", key, "warn", f"{key} is not set", f"git config --global {key} …")
        )
    remotes = gitutil.out(ctx.project, "remote").split()
    out.append(
        Check("git", "remote", "ok", ", ".join(remotes))
        if remotes
        else Check("git", "remote", "warn", "no remote configured", "git remote add origin …")
    )
    return out


@group("skills", toolchain=True)
def skills_group(ctx: Context) -> list[Check]:
    agents = ctx.project / "AGENTS.md"
    if not agents.is_file():
        return [Check("skills", "AGENTS.md", "warn", "no AGENTS.md", "tuvl update skills")]
    out = [Check("skills", "AGENTS.md", "ok", "present")]
    skills = ctx.project / ".agents" / "skills"
    out.append(
        Check("skills", ".agents/skills", "ok", f"{sum(1 for _ in skills.iterdir())} skills")
        if skills.is_dir()
        else Check(
            "skills", ".agents/skills", "warn", "no coding-agent skills", "tuvl update skills"
        )
    )
    return out


@group("agent", toolchain=True)
def agent_group(ctx: Context) -> list[Check]:
    from tuvl_insight.coding_agents import detect, get, read_settings, runner

    settings = read_settings(ctx.project)
    if settings is None:
        return [
            Check(
                "agent",
                "settings",
                "warn",
                "no .tuvl/agent.yaml (developer initials and coding agent)",
                "tuvl use agent <agent|none>",
            )
        ]
    out: list[Check] = []
    out.append(
        Check("agent", "initials", "ok", settings.initials)
        if settings.initials
        else Check("agent", "initials", "warn", "no developer initials", "edit .tuvl/agent.yaml")
    )
    if settings.agent == "none":
        out.append(Check("agent", "agent", "ok", "none selected (tuvl will not run an agent)"))
        return out
    if settings.agent == "generic":
        import shutil as _shutil

        first = settings.command[0] if settings.command else ""
        out.append(
            Check("agent", "agent", "ok", f"generic: {' '.join(settings.command)}")
            if first and _shutil.which(first)
            else Check(
                "agent",
                "agent",
                "fail",
                "generic has no command, or it is not on PATH",
                "tuvl use agent generic --command '…'",
            )
        )
        return out
    spec = get(settings.agent)
    found = {d.spec.id: d for d in detect()}
    if spec is None:
        out.append(
            Check("agent", "agent", "fail", f"unknown agent '{settings.agent}'", "tuvl list agents")
        )
    elif settings.agent not in found:
        out.append(
            Check(
                "agent",
                "agent",
                "fail",
                f"{spec.name} is selected but not on PATH",
                "tuvl use agent none",
            )
        )
    elif runner.capability(settings.agent) == "":
        out.append(
            Check("agent", "agent", "warn", f"{spec.name} found, but tuvl cannot run it yet")
        )
    elif runner.capability(settings.agent) == "review":
        out.append(
            Check(
                "agent",
                "agent",
                "warn",
                f"{spec.name} found: tuvl uses it to review, not to draft or build",
                "tuvl use agent <claude|codex|gemini>",
            )
        )
    else:
        out.append(Check("agent", "agent", "ok", f"{spec.name} {found[settings.agent].version}"))
    return out


@group("ideas", toolchain=True)
def ideas_group(ctx: Context) -> list[Check]:
    from tuvl_insight.spec import ids

    root = ids.idea_dir(ctx.project)
    if not root.is_dir():
        return [Check("ideas", "ideas", "ok", "no ideas yet")]
    out: list[Check] = []
    on_disk = {p.name for p in root.iterdir() if p.is_dir() and ids.IDEA_ID.match(p.name)}
    indexed = {str(r.get("id", "")) for r in ids.read_index(ctx.project)}
    out.append(Check("ideas", "ideas", "ok", f"{len(on_disk)} idea(s)"))
    missing = sorted(on_disk - indexed)
    gone = sorted(indexed - on_disk)
    if missing:
        out.append(Check("ideas", "index", "warn", f"not in idea/INDEX.yaml: {', '.join(missing)}"))
    if gone:
        out.append(
            Check("ideas", "index", "warn", f"in the index but no folder: {', '.join(gone)}")
        )
    if not missing and not gone:
        out.append(Check("ideas", "index", "ok", "idea/INDEX.yaml matches the folders"))
    dups = ids.duplicates(ctx.project)
    for d in dups:
        out.append(
            Check(
                "ideas",
                f"duplicate {d.id}",
                "fail",
                "claimed by " + ", ".join(sorted(set(d.where))),
                f"tuvl renumber idea {ids.idea_of(d.id)}",
            )
        )
    if not dups:
        out.append(Check("ideas", "duplicates", "ok", "every id is used once"))
    out += _base_conflicts(ctx.project, on_disk)
    return out


def _base_conflicts(project: Path, on_disk: set[str]) -> list[Check]:
    """Ideas whose id the base branch already uses for something else (two developers, one number)."""
    for ref in ("origin/develop", "develop"):
        if gitutil.out(project, "rev-parse", "--verify", "-q", ref):
            break
    else:
        return []
    out: list[Check] = []
    for idea in sorted(on_disk):
        theirs = gitutil.out(project, "rev-parse", "-q", "--verify", f"{ref}:idea/{idea}/IDEA.yaml")
        mine = gitutil.out(project, "hash-object", f"idea/{idea}/IDEA.yaml")
        if theirs and mine and theirs != mine:
            out.append(
                Check(
                    "ideas",
                    f"conflict {idea}",
                    "fail",
                    f"{ref} already has a different {idea}",
                    f"tuvl renumber idea {idea}",
                )
            )
    return out


@group("audit", toolchain=True)
def audit_group(ctx: Context) -> list[Check]:
    from tuvl_insight.audit import ledger

    if not ledger.files(ctx.project):
        return [Check("audit", "ledger", "ok", "no events recorded yet")]
    result = ledger.verify(ctx.project)
    if result.ok:
        return [Check("audit", "ledger", "ok", f"{result.events} event(s), chain intact")]
    return [
        Check(
            "audit",
            "ledger",
            "fail",
            f"{result.problem} at {result.where}",
            "the local ledger was altered; `tuvl verify audit`",
        )
    ]
