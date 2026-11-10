"""`/dev/ping` and the artifact file routes: the dev-mode routes that act on the engine's own registries.

The rest of the dev API (project files, specs, jobs, settings, ...) is developer tooling and is mounted by
`tuvl.core.dev.extensions`. These stay with the engine because they read and edit `ARTIFACT_REGISTRY`.
"""

import re
from pathlib import Path

import yaml
from fastapi import APIRouter, Body, HTTPException

from tuvl.core.config import settings
from tuvl.core.logger import logger

router = APIRouter(prefix="/dev", tags=["dev"])


@router.get("/ping")
async def ping():
    return {"status": "ok", "dev": True}


# ── Artifacts (file-source CRUD for the Insight artifacts UI) ─────────────────
# Prose artifacts are front-matter .md files under artifacts/; structured
# artifacts are `kind: Artifact` YAML. Dev-mode edits write straight to the
# project's artifacts/ directory (git-visible); production uploads go to the
# DB-backed /api/artifacts surface instead.

_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _artifacts_dir() -> Path:
    return settings.dir_for("artifacts")


def _safe_artifact_path(name: str) -> Path:
    if not _ARTIFACT_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid artifact name.")
    resolved = (_artifacts_dir() / f"{name}.md").resolve()
    if _artifacts_dir().resolve() not in resolved.parents:
        raise HTTPException(status_code=400, detail="Path traversal detected.")
    return resolved


@router.get("/artifacts")
async def list_artifacts(type: str | None = None):
    """List registered artifacts (all sources), with the reverse index of
    workflow reference sites computed from the live workflow registry."""
    from tuvl.contract.facts import document_artifact_refs
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY, parse_ref

    referenced: dict[str, list[str]] = {}
    for doc in WORKFLOWS.values():
        for where, _site, ref in document_artifact_refs(doc):
            try:
                ref_name, _v = parse_ref(ref)
            except ValueError:
                continue
            referenced.setdefault(ref_name, []).append(where)

    artifacts = [
        {
            "name": e.name,
            "type": e.type,
            "description": e.description,
            "source": e.source,
            "versions": sorted(e.versions),
            "latest": e.latest,
            "referenced_by": referenced.get(e.name, []),
        }
        for e in ARTIFACT_REGISTRY.values()
        if type is None or e.type == type
    ]
    return {"artifacts": sorted(artifacts, key=lambda a: a["name"])}


@router.get("/artifacts/{name}")
async def get_artifact(name: str, version: int | None = None):
    """Return one artifact version's content (prose text or structured spec)."""
    from tuvl.core.artifacts.registry import (
        ARTIFACT_REGISTRY,
        ArtifactResolutionError,
        get_version,
    )

    entry = ARTIFACT_REGISTRY.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    try:
        av = get_version(name, version, where="dev.artifacts")
    except ArtifactResolutionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "name": entry.name,
        "type": entry.type,
        "description": entry.description,
        "source": av.source,
        "version": version if version is not None else entry.latest,
        "content": av.content,
        "content_hash": av.content_hash,
    }


@router.post("/artifacts")
async def create_artifact(body: dict = Body(...)):
    """Create a prose artifact file under ``artifacts/`` and register it.

    Body: ``{name, type (prompt|steering|skill), content, description?, version?}``.
    """
    from tuvl.core.artifacts.kinds import MAX_ARTIFACT_BYTES, PROSE_TYPES
    from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY, register_artifact

    name = str(body.get("name") or "")
    type_ = str(body.get("type") or "")
    content = body.get("content")
    if not _ARTIFACT_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid artifact name.")
    if type_ not in PROSE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"type must be one of {sorted(PROSE_TYPES)} (structured artifacts are YAML).",
        )
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content (string) is required.")
    if len(content.encode()) > MAX_ARTIFACT_BYTES:
        raise HTTPException(
            status_code=413, detail=f"content exceeds {MAX_ARTIFACT_BYTES // 1024} KB cap."
        )
    entry = ARTIFACT_REGISTRY.get(name)
    version = int(body.get("version") or ((max(entry.versions) + 1) if entry else 1))
    description = str(body.get("description") or "")

    dest = _safe_artifact_path(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    front = yaml.dump(
        {"name": name, "type": type_, "version": version, "description": description},
        sort_keys=False,
    )
    dest.write_text(f"---\n{front}---\n\n{content}")
    register_artifact(
        name=name,
        type_=type_,
        version=version,
        content=content,
        source="file",
        description=description,
        path=dest,
        mtime=dest.stat().st_mtime,
    )
    logger.info("Artifact saved", name=name, type=type_, version=version, path=str(dest))
    return {"name": name, "version": version, "path": str(dest)}


@router.delete("/artifacts/{name}")
async def delete_artifact(name: str):
    """Delete a file-source artifact (file + registry entry)."""
    from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY

    entry = ARTIFACT_REGISTRY.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    if entry.source != "file":
        raise HTTPException(
            status_code=400, detail="Only file-source artifacts can be deleted here."
        )
    removed = []
    for av in entry.versions.values():
        if av.path is not None and av.path.is_file():
            av.path.unlink()
            removed.append(str(av.path))
    path = _safe_artifact_path(name)
    if path.is_file():
        path.unlink()
        removed.append(str(path))
    ARTIFACT_REGISTRY.pop(name, None)
    return {"deleted": name, "files": sorted(set(removed))}
