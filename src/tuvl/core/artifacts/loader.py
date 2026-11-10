"""Artifact loading — file source (``artifacts/``) and the ``kind: Artifact``
envelope — plus the boot-time reference-resolution pass.

Prose artifacts are markdown with YAML front-matter under the project's
``artifacts/`` directory::

    ---
    name: support-triage-system
    type: prompt
    version: 3
    description: System prompt for the ticket-triage agent.
    ---
    You are a support-ticket triage specialist…

Structured artifacts (guardrail / hook / mcp) use the standard document
envelope (``kind: Artifact`` + ``spec``) and are dispatched by the central
config loader like every other kind.
"""

from __future__ import annotations

import pathlib
from typing import Any

import structlog
import yaml

from tuvl.core.artifacts.kinds import (
    ARTIFACT_TYPES,
    MAX_ARTIFACT_BYTES,
    PROSE_TYPES,
    STRUCTURED_TYPES,
)
from tuvl.core.artifacts.registry import (
    ARTIFACT_REGISTRY,
    ArtifactResolutionError,
    register_artifact,
    resolve_prose,
    resolve_structured,
)

logger = structlog.get_logger(__name__)


def read_prose_artifact(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    """Parse one front-matter ``.md`` file → ``(meta, body)``. Raises ValueError."""
    raw = path.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact file exceeds the {MAX_ARTIFACT_BYTES // 1024} KB content cap")
    if not raw.startswith("---"):
        raise ValueError("missing YAML front-matter (file must start with '---')")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError("unterminated YAML front-matter (missing closing '---')")
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"front-matter YAML parse error: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("front-matter must be a YAML mapping")
    name = meta.get("name")
    type_ = meta.get("type")
    if not name:
        raise ValueError("front-matter 'name' is required")
    if type_ not in PROSE_TYPES:
        raise ValueError(
            f"front-matter 'type' must be one of {sorted(PROSE_TYPES)} for .md "
            f"artifacts, got '{type_}'"
        )
    version = meta.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise ValueError(f"front-matter 'version' must be a positive integer, got {version!r}")
    return meta, parts[2].lstrip("\n")


def load_file_artifacts(project_dir: pathlib.Path | str | None = None) -> None:
    """Walk the project's ``artifacts/`` directory and register every prose
    ``.md`` artifact. Structured YAML artifacts arrive via the central config
    loader (``kind: Artifact``); this function only handles the markdown side.
    """
    from tuvl.core.config import settings

    artifacts_dir = (
        pathlib.Path(project_dir) / "artifacts"
        if project_dir is not None
        else settings.dir_for("artifacts")
    )
    if not artifacts_dir.is_dir():
        return

    for path in sorted(artifacts_dir.rglob("*.md")):
        try:
            meta, body = read_prose_artifact(path)
        except ValueError as exc:
            logger.error("artifact.load_failed", file=str(path), error=str(exc))
            continue
        register_artifact(
            name=str(meta["name"]),
            type_=str(meta["type"]),
            version=int(meta.get("version", 1)),
            content=body,
            source="file",
            description=str(meta.get("description") or ""),
            enabled=meta.get("enabled", True) is not False,
            path=path,
            mtime=path.stat().st_mtime,
        )
        logger.info(
            "Artifact loaded",
            name=meta["name"],
            type=meta["type"],
            version=meta.get("version", 1),
            source="file",
        )


def _register_artifact_from_dict(
    data: dict[str, Any], filename: str = "<unknown>", *, source: str = "file"
) -> None:
    """Register a ``kind: Artifact`` document (structured types only)."""
    if data.get("kind") != "Artifact":
        return
    spec = data.get("spec")
    if not isinstance(spec, dict):
        raise ValueError(f"Artifact in '{filename}' has no 'spec' block")
    name = (data.get("metadata") or {}).get("name")
    if not name:
        raise ValueError(f"Artifact in '{filename}' is missing metadata.name")
    type_ = spec.get("type")
    if type_ not in ARTIFACT_TYPES:
        raise ValueError(
            f"Artifact '{name}' in '{filename}': spec.type '{type_}' is invalid — "
            f"one of {sorted(ARTIFACT_TYPES)}"
        )
    if type_ not in STRUCTURED_TYPES:
        raise ValueError(
            f"Artifact '{name}' in '{filename}': prose type '{type_}' is authored "
            f"as a front-matter .md file under artifacts/, not a YAML envelope"
        )
    if len(yaml.safe_dump(spec).encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError(
            f"Artifact '{name}' exceeds the {MAX_ARTIFACT_BYTES // 1024} KB content cap"
        )
    _validate_structured_spec(str(name), filename, spec)
    content = {k: v for k, v in spec.items() if k != "type"}
    version = (data.get("metadata") or {}).get("version", 1)
    register_artifact(
        name=str(name),
        type_=str(type_),
        version=int(version) if isinstance(version, int) else 1,
        content=content,
        source=source,
        description=str((data.get("metadata") or {}).get("description") or ""),
        enabled=data.get("enabled", True) is not False,
    )
    logger.info("Artifact loaded", name=name, type=type_, source=source)


def _validate_structured_spec(name: str, filename: str, spec: dict[str, Any]) -> None:
    """Shape-check the 2.0 structured types (judge, rules) at load time."""
    from pydantic import BaseModel, ValidationError

    from tuvl.contract.schema import JudgeSpec, RulesSpec

    shapes: dict[str, type[BaseModel]] = {"judge": JudgeSpec, "rules": RulesSpec}
    model = shapes.get(str(spec.get("type")))
    if model is None:
        return
    try:
        model.model_validate(spec)
    except ValidationError as exc:
        raise ValueError(f"Artifact '{name}' in '{filename}': {exc}") from exc


_STRUCTURED_SITES = ("guardrails", "hooks", "mcp.server", "judge", "decide.rules")


def resolve_artifact_refs() -> list[str]:
    """Boot pass: resolve + type-check every artifact ref in every loaded
    workflow. Returns the problem list; the caller decides the posture
    (production: startup failure, dev: warn and error at runtime).
    """
    from tuvl.contract.facts import document_artifact_refs
    from tuvl.contract.registry import WORKFLOWS

    problems: list[str] = []
    for doc in WORKFLOWS.values():
        for where, site, ref in document_artifact_refs(doc):
            try:
                if site in _STRUCTURED_SITES:
                    resolve_structured(ref, site=site, where=where)
                else:
                    resolve_prose(ref, site=site, where=where)
            except ArtifactResolutionError as exc:
                problems.append(str(exc))
    return problems


def check_artifact_refs_at_boot() -> None:
    """Enforce the boot posture: unresolved refs fail startup in production and
    warn in dev (the step errors at runtime with a precise message)."""
    from tuvl.core.config import settings

    problems = resolve_artifact_refs()
    if not problems:
        if ARTIFACT_REGISTRY:
            logger.info("Artifact refs resolved", artifacts=len(ARTIFACT_REGISTRY))
        return
    for p in problems:
        logger.error("artifact.ref.unresolved", detail=p)
    if not settings.tuvl_dev_mode:
        raise RuntimeError(
            f"{len(problems)} unresolved artifact reference(s) — fix the artifacts/ "
            f"directory or the workflow refs (details above)"
        )
