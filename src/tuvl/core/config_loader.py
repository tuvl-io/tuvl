"""Central configuration loader.

Scans the project directory tree recursively for ``*.yaml`` files and
dispatches each file to the correct registry based on its ``kind`` field.

No directory-to-kind mapping is required — every resource file declares its
own ``kind``.  This removes the dependency on ``config.yaml``'s ``dirs:``
section for startup registration.

Supported kinds
---------------
- ``Artifact``             → :mod:`tuvl.core.artifacts.loader`
- ``ModelDefinition``      → :mod:`tuvl.core.models.loader`
- ``EmbeddingRegistry``    → :mod:`tuvl.core.models.embeddings_loader`
- ``EmbeddingConfig``      → :mod:`tuvl.core.models.embeddings_loader`
- ``CollectionRegistry``   → :mod:`tuvl.core.models.collections_loader`
- ``CollectionConfig``     → :mod:`tuvl.core.models.collections_loader`
- ``DataSource``           → :mod:`tuvl.core.datasources.loader`
- ``RedisConfig``          → :mod:`tuvl.core.datasources.loader`
- ``FederationProvider``   → :mod:`tuvl.core.auth.federation_loader`
- ``AgentModel``           → :mod:`tuvl.core.models.agent_models`
- ``Workflow``             → :mod:`tuvl.contract.registry` when it declares
  ``version: tuvl/v2``, else the 1.x :mod:`tuvl.core.api.manager`
- ``Test`` / ``TaskPlan``  → :mod:`tuvl.contract.registry` (2.0 only)

Skipped kinds (handled elsewhere)
----------------------------------
- ``ProjectConfig``   — read by :class:`tuvl.core.config.Settings`
- ``TelemetryConfig`` — read by the telemetry init logic
- ``test``            — LLM-as-a-Judge test cases, read by ``tuvl run tests``
"""

from __future__ import annotations

import os
import pathlib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from typing import Any

import yaml

from tuvl.core.logger import logger

# Directories to skip when walking the project tree
_SCAN_EXCLUDE: frozenset[str] = frozenset(
    {
        ".tuvl",
        ".git",
        ".svn",
        "__pycache__",
        "node_modules",
        "site-packages",
        ".venv",
        "venv",
        "env",
        ".env",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

# Development-only kinds a production server never loads.
_DEV_KINDS: frozenset[str] = frozenset({"Test", "TaskPlan"})

# Kinds that are intentionally NOT handled by the central loader
SKIP_KINDS: frozenset[str] = frozenset(
    {"ProjectConfig", "TelemetryConfig", "SystemConfig", "test", "Idea", "IdeaIndex"}
)

# Processing order — earlier kinds must be registered before later ones
# (e.g. collections reference embeddings, workflows reference collections)
PROCESS_ORDER: list[str] = [
    "Artifact",
    "AgentModel",
    "ModelDefinition",
    "EmbeddingRegistry",
    "EmbeddingConfig",
    "CollectionRegistry",
    "CollectionConfig",
    "DataSource",
    "RedisConfig",
    "FederationProvider",
    "Storage",
    "Workflow",
    "Test",
    "TaskPlan",
]


def walk_yaml_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    """Yield every ``*.yaml`` path under *root*, skipping excluded dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs in-place so os.walk does not descend into them
        dirnames[:] = sorted(
            d for d in dirnames if d not in _SCAN_EXCLUDE and not d.startswith(".")
        )
        for fname in sorted(filenames):
            if fname.endswith(".yaml") or fname.endswith(".yml"):
                yield pathlib.Path(dirpath) / fname


def _forget_model_tables(classes: Iterable[type]) -> None:
    """Drop previously loaded model tables and classes from SQLModel's metadata
    and class registry, so loading the project again in the same process
    (tests, ``tuvl init --sample`` then another command) redefines them cleanly."""
    from sqlalchemy import Table
    from sqlmodel import SQLModel

    registry = SQLModel._sa_registry
    for cls in list(classes):
        table = getattr(cls, "__table__", None)
        if isinstance(table, Table) and SQLModel.metadata.tables.get(table.key) is table:
            SQLModel.metadata.remove(table)
        registry._dispose_cls(cls)


def load_all_configs(
    project_dir: pathlib.Path | str | None = None, *, include_dev_documents: bool = True
) -> None:
    """Scan *project_dir* recursively and populate all registries by kind.

    Parameters
    ----------
    project_dir:
        Root directory to scan.  Defaults to ``settings.project_dir`` when
        ``None``.
    include_dev_documents:
        ``False`` skips ``Test`` and ``TaskPlan`` documents — a production
        server never needs them, even if one reaches the image.
    """
    if project_dir is None:
        from tuvl.core.config import settings

        project_dir = settings.project_dir

    root = pathlib.Path(project_dir)
    if not root.exists():
        logger.warning("config.project_dir.missing", project_dir=str(root.resolve()))
        return

    # ── 1. Clear all registries ────────────────────────────────────────────
    from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY
    from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY
    from tuvl.core.datasources.loader import (
        DATASOURCE_REGISTRY,
        _reset_primary_state,
    )
    from tuvl.core.models.agent_models import AGENT_MODEL_REGISTRY
    from tuvl.core.models.collections_loader import COLLECTION_REGISTRY
    from tuvl.core.models.embeddings_loader import EMBEDDING_REGISTRY
    from tuvl.core.models.loader import (
        MODEL_DATASOURCE_MAP,
        MODEL_REGISTRY,
        MODEL_RELATIONS,
        MODEL_VERSION_REGISTRY,
        SECURE_FIELDS,
    )
    from tuvl.core.models.schemas import SCHEMA_REGISTRY
    from tuvl.storage.spec import STORE_REGISTRY

    ARTIFACT_REGISTRY.clear()
    STORE_REGISTRY.clear()
    AGENT_MODEL_REGISTRY.clear()
    _forget_model_tables(MODEL_REGISTRY.values())
    MODEL_REGISTRY.clear()
    MODEL_VERSION_REGISTRY.clear()
    MODEL_DATASOURCE_MAP.clear()
    MODEL_RELATIONS.clear()
    SECURE_FIELDS.clear()
    SCHEMA_REGISTRY.clear()
    EMBEDDING_REGISTRY.clear()
    COLLECTION_REGISTRY.clear()
    DATASOURCE_REGISTRY.clear()
    _reset_primary_state()
    FEDERATION_REGISTRY.clear()

    from tuvl.contract import registry as contract_registry

    contract_registry.clear()

    # Prose artifacts (front-matter .md under artifacts/) load alongside the
    # YAML walk; structured artifacts dispatch by kind like every other doc.
    from tuvl.core.artifacts.loader import load_file_artifacts

    load_file_artifacts(root)

    # ── 2. Walk tree and parse all YAML files ──────────────────────────────
    by_kind: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    unknown_kinds: set[str] = set()

    for yaml_path in walk_yaml_files(root):
        try:
            with open(yaml_path) as f:
                docs = [d for d in yaml.safe_load_all(f) if d is not None]
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f" (line {mark.line + 1})" if mark else ""
            from tuvl.contract.registry import yaml_error_hint

            logger.error(
                "config.yaml.parse_error",
                file=yaml_path.name,
                location=location.strip(" ()") or None,
                error=str(exc),
                hint=yaml_error_hint(exc),
            )
            continue
        except OSError as exc:
            logger.error("config.yaml.read_error", file=str(yaml_path), error=str(exc))
            continue

        for data in docs:
            if not isinstance(data, dict) or "kind" not in data:
                continue

            kind = data["kind"]
            if kind in SKIP_KINDS:
                continue

            if kind not in PROCESS_ORDER:
                unknown_kinds.add(kind)
                continue

            by_kind[kind].append((data, yaml_path.name))

    if unknown_kinds:
        logger.debug("config.yaml.unhandled_kinds", kinds=sorted(unknown_kinds))

    # ── 3. Dispatch in dependency order ───────────────────────────────────
    from tuvl.core.artifacts.loader import _register_artifact_from_dict
    from tuvl.core.auth.federation_loader import _register_federation_from_dict
    from tuvl.core.datasources.loader import (
        _finalize_primary_ds,
        _register_datasource_from_dict,
    )
    from tuvl.core.models.agent_models import _register_agent_model_from_dict
    from tuvl.core.models.collections_loader import _register_collection_from_dict
    from tuvl.core.models.embeddings_loader import _register_embedding_from_dict
    from tuvl.core.models.loader import _register_model_from_dict
    from tuvl.storage.spec import register_store_from_dict

    _handlers: dict[str, Any] = {
        "Artifact": _register_artifact_from_dict,
        "AgentModel": _register_agent_model_from_dict,
        "ModelDefinition": _register_model_from_dict,
        "EmbeddingRegistry": _register_embedding_from_dict,
        "EmbeddingConfig": _register_embedding_from_dict,
        "CollectionRegistry": _register_collection_from_dict,
        "CollectionConfig": _register_collection_from_dict,
        "DataSource": _register_datasource_from_dict,
        "RedisConfig": _register_datasource_from_dict,
        "FederationProvider": _register_federation_from_dict,
        "Storage": register_store_from_dict,
        "Workflow": _register_workflow,
        "Test": contract_registry.register_document,
        # The task list is the coding agent's, checked by `tuvl lint spec` (P001–P005).
        "TaskPlan": lambda data, filename="<unknown>": None,
    }

    for kind in PROCESS_ORDER:
        entries = by_kind.get(kind, [])
        if not entries:
            continue
        if not include_dev_documents and kind in _DEV_KINDS:
            logger.info("config.dev_documents.skipped", kind=kind, count=len(entries))
            continue
        handler = _handlers[kind]
        for data, filename in entries:
            try:
                handler(data, filename)
            except Exception as exc:
                logger.error("config.register.failed", kind=kind, file=filename, error=str(exc))
                raise

    # Resolve PRIMARY_DS after all DataSource entries have been registered.
    _finalize_primary_ds()

    logger.info(
        "config.loaded",
        artifacts=len(ARTIFACT_REGISTRY),
        agent_models=len(AGENT_MODEL_REGISTRY),
        models=len(MODEL_REGISTRY),
        embeddings=len(EMBEDDING_REGISTRY),
        collections=len(COLLECTION_REGISTRY),
        datasources=len(DATASOURCE_REGISTRY),
        federation=len(FEDERATION_REGISTRY),
        storage=len(STORE_REGISTRY),
        workflows=len(contract_registry.WORKFLOWS),
        v2_tests=len(contract_registry.TESTS),
    )


def _register_workflow(data: dict[str, Any], filename: str) -> None:
    """Register a 2.0 ``Workflow``; a 1.x workflow is a load error with the
    migration hint."""
    from tuvl.contract import registry as contract_registry

    if contract_registry.is_v2(data):
        contract_registry.register_document(data, filename)
    else:
        contract_registry.reject_v1_workflow(data, filename)
