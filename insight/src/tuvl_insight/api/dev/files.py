"""Project files as text, project helpers and the project summary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import PlainTextResponse

from tuvl.core.config import settings
from tuvl.core.dev.project import project_dir

router = APIRouter()

#: Project areas Insight edits as text: area → (directory, allowed suffixes).
TEXT_AREAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "workflows": ("workflows", (".yaml", ".yml")),
    "models": ("models", (".yaml", ".yml")),
    "specs": ("specs", (".md", ".yaml")),
    "tests": ("tests", (".yaml", ".yml", ".json")),
    "artifacts": ("artifacts", (".md", ".yaml", ".yml", ".json")),
    "llms": ("llms", (".yaml", ".yml")),
    "datasources": ("datasources", (".yaml", ".yml")),
    "storage": ("storage", (".yaml", ".yml")),
}


def _area_path(area: str, name: str) -> Path:
    if area not in TEXT_AREAS:
        raise HTTPException(400, f"unknown area {area!r} — one of {sorted(TEXT_AREAS)}")
    directory, suffixes = TEXT_AREAS[area]
    base = (
        settings.dir_for(directory)
        if area != "specs" and area != "tests"
        else project_dir() / directory
    ).resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or name.startswith(".") or "\\" in name:
        raise HTTPException(400, "invalid file name")
    if target.suffix not in suffixes:
        raise HTTPException(400, f"{area} files end with {', '.join(suffixes)}")
    return target


@router.get(
    "/project/summary", summary="What the project holds: workflows, models, specs, tests, lock"
)
async def project_summary() -> dict[str, Any]:
    from tuvl.contract.registry import TESTS, WORKFLOWS
    from tuvl.core.models.loader import MODEL_REGISTRY
    from tuvl.runtime.host import unavailable_reasons
    from tuvl_insight import lock
    from tuvl_insight.spec import load_specs

    project = project_dir()
    specs = []
    for spec in load_specs(project):
        tasks = project / Path(spec.path).with_suffix("").with_suffix(".tasks.yaml")
        specs.append({"path": spec.path, "name": spec.name, "has_plan": tasks.is_file()})
    return {
        "project": project.name,
        "workflows": [
            {
                "name": name,
                "description": doc.metadata.description,
                "agents": len(doc.spec.agents),
                "pending": sum(1 for a in doc.spec.agents if a.engine == "pending"),
                "unavailable": unavailable_reasons(doc) if doc.enabled else [],
            }
            for name, doc in sorted(WORKFLOWS.items())
        ],
        "models": sorted(MODEL_REGISTRY),
        "specs": specs,
        "tests": sorted(TESTS),
        "lock": (project / lock.LOCK_FILE).is_file(),
    }


@router.get("/text/{area}", summary="Files in a project area")
async def list_text(area: str) -> list[str]:
    if area not in TEXT_AREAS:
        raise HTTPException(400, f"unknown area {area!r}")
    base = _area_path(area, "x" + TEXT_AREAS[area][1][0]).parent
    if not base.is_dir():
        return []
    suffixes = TEXT_AREAS[area][1]
    return sorted(
        str(p.relative_to(base))
        for p in base.rglob("*")
        if p.is_file()
        and p.suffix in suffixes
        and not any(part.startswith((".", "_")) for part in p.relative_to(base).parts)
    )


@router.get("/text/{area}/{name:path}", response_class=PlainTextResponse, summary="A file's text")
async def read_text(area: str, name: str) -> str:
    path = _area_path(area, name)
    if not path.is_file():
        raise HTTPException(404, f"{area}/{name} not found")
    return path.read_text()


@router.put("/text/{area}/{name:path}", summary="Write a file's text (YAML must parse)")
async def write_text(
    area: str, name: str, text: str = Body(..., media_type="text/plain")
) -> dict[str, Any]:
    path = _area_path(area, name)
    if path.suffix in (".yaml", ".yml"):
        try:
            list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            raise HTTPException(422, f"YAML parse error: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return {"saved": str(path.relative_to(project_dir()))}


@router.delete("/text/{area}/{name:path}", summary="Delete a file")
async def delete_text(area: str, name: str) -> dict[str, str]:
    path = _area_path(area, name)
    if not path.is_file():
        raise HTTPException(404, f"{area}/{name} not found")
    path.unlink()
    return {"deleted": str(path.relative_to(project_dir()))}
