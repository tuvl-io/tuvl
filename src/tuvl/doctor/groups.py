"""The built-in check groups."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

from tuvl import gitutil
from tuvl.doctor import Check, Context, Fix, group

PYTHON_RANGE = ((3, 13), (3, 15))
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_ENV_IGNORED = (".env", "/.env", ".env*")


def _ignored_lines(project: Path) -> list[str]:
    gi = project / ".gitignore"
    return [ln.strip() for ln in gi.read_text().splitlines()] if gi.is_file() else []


def _add_gitignore(project: Path, entry: str) -> None:
    gi = project / ".gitignore"
    existing = gi.read_text() if gi.is_file() else ""
    with gi.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{entry}\n")


@group("project")
def project_group(ctx: Context) -> list[Check]:
    from tuvl.core.project_info import id_is_valid, load_project

    record = load_project(ctx.project)
    if record is None:
        return [
            Check(
                "project",
                "project.yaml",
                "warn",
                "no .tuvl/project.yaml (or it does not parse)",
                "tuvl upgrade project",
            )
        ]
    out = [Check("project", "project.yaml", "ok", f"{record.get('name', '')}".strip())]
    pid = str(record.get("id", ""))
    out.append(
        Check("project", "id", "ok", pid)
        if id_is_valid(pid)
        else Check(
            "project", "id", "fail", f"'{pid}' is not a valid project id", "tuvl upgrade project"
        )
    )
    version = str(record.get("version", ""))
    out.append(
        Check("project", "version", "ok", version)
        if _SEMVER.match(version)
        else Check(
            "project",
            "version",
            "fail",
            f"'{version}' is not semver",
            "tuvl set project version 0.1.0",
        )
    )
    if not str(record.get("author", "")).strip():
        out.append(Check("project", "author", "warn", "no author", "tuvl set project author …"))
    if str(record.get("type", "")) not in ("poc", "dev", "production"):
        out.append(
            Check(
                "project",
                "type",
                "warn",
                f"type is '{record.get('type', '')}'",
                "tuvl set project type dev",
            )
        )
    return out


@group("env")
def env_group(ctx: Context) -> list[Check]:
    from tuvl.extras import toolchain_installed

    out: list[Check] = []
    here = sys.version_info[:2]
    lo, hi = PYTHON_RANGE
    ok = lo <= here < hi
    out.append(
        Check(
            "env",
            "python",
            "ok" if ok else "fail",
            f"{here[0]}.{here[1]}",
            "" if ok else f"tuvl supports Python >={lo[0]}.{lo[1]},<{hi[0]}.{hi[1]}",
        )
    )
    env_file = ctx.project / ".env"
    if not env_file.is_file():
        out.append(Check("env", ".env", "warn", "no .env", "secrets and provider keys go here"))
    else:
        out.append(Check("env", ".env", "ok", "present"))
        if (ctx.project / ".git").exists() and not any(
            ln in _ENV_IGNORED for ln in _ignored_lines(ctx.project)
        ):
            out.append(
                Check(
                    "env",
                    ".env ignored",
                    "fail",
                    ".env is not in .gitignore",
                    "add it so secrets are never committed",
                    Fix("add .env to .gitignore", lambda: _add_gitignore(ctx.project, ".env")),
                )
            )
        elif (ctx.project / ".git").exists():
            out.append(Check("env", ".env ignored", "ok", "in .gitignore"))
    from tuvl.core import envconfig

    manifest = envconfig.load_manifest(ctx.project)
    if manifest:
        dotenv = envconfig.read_dotenv(ctx.project)
        absent = [
            name
            for name, var in manifest.items()
            if var.required
            and var.default is None
            and not (os.environ.get(name) or dotenv.get(name))
        ]
        out.append(
            Check(
                "env",
                "variables",
                "fail" if absent else "ok",
                f"required but not set: {', '.join(absent)}"
                if absent
                else f"{len(manifest)} declared, every required one is set",
                "set them in .env (Insight: Settings → Environment); production refuses to start without them"
                if absent
                else "",
            )
        )
    if toolchain_installed():
        uv = shutil.which("uv")
        out.append(
            Check("env", "uv", "ok", uv)
            if uv
            else Check("env", "uv", "warn", "uv not found", "https://docs.astral.sh/uv/")
        )
    return out


@group("config")
def config_group(ctx: Context) -> list[Check]:
    from tuvl.core.config import use_project
    from tuvl.core.config_loader import load_all_configs

    try:
        use_project(str(ctx.project))
        load_all_configs(ctx.project)
    except Exception as exc:
        ctx.data["loaded"] = False
        return [
            Check(
                "config",
                "load",
                "fail",
                f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}",
            )
        ]
    ctx.data["loaded"] = True
    return [Check("config", "load", "ok", "models, workflows and datasources load")]


@group("models")
def models_group(ctx: Context) -> list[Check]:
    if not ctx.data.get("loaded"):
        return [Check("models", "models", "skip", "the project did not load")]
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.models.agent_models import AGENT_MODEL_REGISTRY
    from tuvl.core.models.requirements import project_needs
    from tuvl.doctor.probe import probe_all

    needs, unknown = project_needs(WORKFLOWS.values(), AGENT_MODEL_REGISTRY)
    report: dict[str, dict] = {
        name: {"configured": False, "problems": ["unknown AgentModel"], "used_by": users}
        for name, users in unknown
    }
    for need in needs:
        report[need.name] = need.to_json()
    if ctx.live:
        ready = [n.name for n in needs if report[n.name]["configured"]]
        errors = asyncio.run(probe_all(ready, AGENT_MODEL_REGISTRY))
        for name, error in zip(ready, errors, strict=True):
            report[name]["live"] = "ok" if error is None else error
            report[name]["configured"] = error is None
    ctx.data["models"] = report
    if not report:
        return [Check("models", "models", "ok", "no workflow uses a model")]
    out: list[Check] = []
    for name, info in sorted(report.items()):
        parts = [f"{info.get('provider', '?')} {info.get('model', '')}".strip()]
        if info.get("env"):
            parts.append("needs " + ", ".join(info["env"]))
        if info.get("missing"):
            parts.append("missing " + ", ".join(info["missing"]))
        parts += list(info.get("problems", []))
        if "live" in info:
            parts.append("live ok" if info["live"] == "ok" else f"live: {info['live']}")
        parts.append("used by " + ", ".join(info.get("used_by", [])))
        out.append(Check("models", name, "ok" if info["configured"] else "fail", " · ".join(parts)))
    return out


@group("storage")
def storage_group(ctx: Context) -> list[Check]:
    if not ctx.data.get("loaded"):
        return [Check("storage", "storage", "skip", "the project did not load")]
    from tuvl.storage import STORE_REGISTRY, required_env

    report = {
        name: {
            "provider": spec.provider,
            "missing": [v for v in required_env(spec) if v not in os.environ],
        }
        for name, spec in STORE_REGISTRY.items()
    }
    ctx.data["storage"] = report
    return [
        Check(
            "storage",
            name,
            "fail" if info["missing"] else "ok",
            f"{info['provider']}"
            + (f" · missing {', '.join(info['missing'])}" if info["missing"] else ""),
        )
        for name, info in sorted(report.items())
    ]


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
    from tuvl.coding_agents import detect, get, read_settings, runner

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
    from tuvl.spec import ids

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
    from tuvl.audit import ledger

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
