"""The built-in check groups."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

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
