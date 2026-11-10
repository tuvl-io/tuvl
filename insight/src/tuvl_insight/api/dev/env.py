"""The project's environment variables: what it needs, what is set, and editing ``.env``.

Behind the dev key only. Values of secret variables are write-only: they go into ``.env`` and are never
returned. A change reaches the running server at its next start.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tuvl.core import envconfig
from tuvl.core.dev.project import project_dir
from tuvl_insight import envconfig_tools

router = APIRouter()

#: names written through this API since the server started; they take effect at the next start
_CHANGED: set[str] = set()

_MANIFEST_RELPATH = Path("config") / "env.yaml"
_ITEM_INDENT = re.compile(r"^(\s*)- name:", re.MULTILINE)


class EnvUpdate(BaseModel):
    set: dict[str, str] = Field(default_factory=dict, description="name → new value")
    unset: list[str] = Field(default_factory=list, description="names to remove from .env")


class Declaration(BaseModel):
    name: str
    description: str = ""
    secret: bool = False
    required: bool = False
    default: str | None = None


def _manifest_path(project: Path) -> Path:
    """The project's EnvConfig file: ``config/env.yaml`` if present, else the first one found, else
    the place a new one goes."""
    default = project / _MANIFEST_RELPATH
    if default.is_file():
        return default
    for path in envconfig.yaml_files(project):
        try:
            head = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r"^kind:\s*EnvConfig\s*$", head, re.MULTILINE):
            return path
    return default


def _payload(project: Path) -> dict[str, Any]:
    dotenv = envconfig.read_dotenv(project)
    entries = envconfig_tools.inventory(project)
    manifest = envconfig.load_manifest(project)
    rows = []
    for e in entries:
        var = e.var
        rows.append(
            {
                "name": var.name,
                "description": var.description,
                "secret": var.secret,
                "required": var.required,
                "default": var.default,
                "example": var.example,
                "group": var.group,
                "first_boot": var.first_boot,
                "source": e.source,
                "declared": var.name in manifest or e.source == "engine",
                "referenced_by": e.referenced_by,
                "needed": e.needed,
                "is_set": e.is_set,
                "in_dotenv": var.name in dotenv,
                "value": e.value,
                "restart_needed": var.name in _CHANGED,
            }
        )
    return {
        "variables": rows,
        "dotenv": {
            "path": ".env",
            "exists": envconfig.dotenv_path(project).is_file(),
            "gitignored": envconfig_tools.gitignored(project),
        },
        "manifest_file": (
            str(manifest_path.relative_to(project))
            if (manifest_path := _manifest_path(project)).is_file()
            else None
        ),
    }


@router.get("/env", summary="Environment variables the project needs and which are set")
async def get_env() -> dict[str, Any]:
    return _payload(project_dir())


@router.put("/env", summary="Set or remove variables in the project's .env")
async def put_env(body: EnvUpdate) -> dict[str, Any]:
    project = project_dir()
    overlap = set(body.set) & set(body.unset)
    if overlap:
        raise HTTPException(422, f"{', '.join(sorted(overlap))} is both set and unset")
    if not body.set and not body.unset:
        raise HTTPException(422, "nothing to change")
    updates: dict[str, str | None] = {**body.set, **dict.fromkeys(body.unset)}
    for name in updates:
        if name.startswith("TUVL_DEV_"):
            raise HTTPException(422, f"{name} is set by `tuvl dev` itself, not by .env")
    try:
        envconfig_tools.write_dotenv(project, updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _CHANGED.update(updates)
    return _payload(project)


def _item_text(decl: Declaration, indent: str) -> str:
    item: dict[str, Any] = {"name": decl.name}
    if decl.description:
        item["description"] = decl.description
    if decl.secret:
        item["secret"] = True
    if decl.required:
        item["required"] = True
    if decl.default is not None:
        item["default"] = decl.default
    dumped = yaml.safe_dump([item], sort_keys=False, allow_unicode=True, width=1000)
    return "".join(f"{indent}{line}\n" for line in dumped.splitlines())


@router.post("/env/declare", summary="Add a variable to the project's EnvConfig")
async def declare(decl: Declaration) -> dict[str, Any]:
    project = project_dir()
    if envconfig.load_manifest(project).get(decl.name) or decl.name in envconfig.ENGINE_CATALOG:
        raise HTTPException(409, f"{decl.name} is already declared")
    probe = {"kind": "EnvConfig", "spec": {"variables": [decl.model_dump(exclude_none=True)]}}
    try:
        envconfig.parse_env_config(probe)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    target = _manifest_path(project)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("kind: EnvConfig\nspec:\n  variables:\n" + _item_text(decl, "    "))
    else:
        text = target.read_text()
        match = _ITEM_INDENT.search(text)
        if match is None or "variables:" not in text:
            raise HTTPException(
                409,
                f"{target.relative_to(project)} lists its variables in a style Insight will not edit; "
                "add it by hand",
            )
        if not text.endswith("\n"):
            text += "\n"
        target.write_text(text + _item_text(decl, match.group(1)))
    return _payload(project)
