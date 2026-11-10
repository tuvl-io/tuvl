"""What a workflow may reference: models, artifacts, AgentModels, workflows.

``tuvl lint project`` runs without booting the runtime, so model types come
straight from ``ModelDefinition`` YAML: each model compiles to lightweight
``read``/``create``/``update``/``expanded`` Pydantic variants that the type
checker (§6) resolves against.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from tuvl.contract.schema import WorkflowDocument
from tuvl.contract.types import TypeRef, build_model
from tuvl.storage.spec import StoreSpec

# ModelDefinition column types → type-language scalars.
_COLUMN_TYPES = {
    "string": "str",
    "text": "str",
    "varchar": "str",
    "bytea": "str",
    "integer": "int",
    "bigint": "int",
    "smallint": "int",
    "numeric": "float",
    "float": "float",
    "boolean": "bool",
    "uuid": "uuid",
    "date": "date",
    "timestamp": "datetime",
    "timestamptz": "datetime",
    "jsonb": "json",
}


def model_variants(definition: Mapping[str, Any]) -> dict[str, type[BaseModel]]:
    """Compile a ``ModelDefinition`` document into its four schema variants."""
    name = str((definition.get("metadata") or {}).get("name"))
    spec = definition.get("spec") or {}
    read: dict[str, TypeRef] = {}
    create: dict[str, TypeRef] = {}
    update: dict[str, TypeRef] = {}
    for f in spec.get("fields") or []:
        if not isinstance(f, Mapping) or not f.get("name"):
            continue
        base = _column_type(f)
        required = bool(f.get("required") or f.get("primary_key"))
        read[f["name"]] = _with_optional(base, not required)
        if f.get("input", not f.get("primary_key")) is not False:
            has_default = "default" in f
            create[f["name"]] = _with_optional(base, not required or has_default)
            update[f["name"]] = _with_optional(base, True)
    expanded = dict(read)
    for rel in spec.get("relations") or []:
        if isinstance(rel, Mapping) and rel.get("name"):
            expanded.setdefault(rel["name"], TypeRef("scalar", name="json", optional=True))
    return {
        "read": build_model(name, read),
        "create": build_model(f"{name}.create", create),
        "update": build_model(f"{name}.update", update),
        "expanded": build_model(f"{name}.expanded", expanded),
    }


def _column_type(f: Mapping[str, Any]) -> TypeRef:
    if f.get("type") == "enum" and f.get("enum_values"):
        return TypeRef("enum", values=tuple(str(v) for v in f["enum_values"]))
    return TypeRef("scalar", name=_COLUMN_TYPES.get(str(f.get("type")), "str"))


def _with_optional(ref: TypeRef, optional: bool) -> TypeRef:
    return TypeRef(ref.kind, ref.name, ref.variant, ref.values, ref.item, optional)


@dataclass
class Catalog:
    #: model name → variant → Pydantic class
    models: dict[str, dict[str, type[BaseModel]]] = field(default_factory=dict)
    #: artifact name → (type, versions)
    artifacts: dict[str, tuple[str, set[int]]] = field(default_factory=dict)
    #: AgentModel name → spec.type ("llm" | "decision")
    agent_models: dict[str, str] = field(default_factory=dict)
    #: 2.0 workflows by name (loop ``workflow:`` tools, test targets)
    workflows: dict[str, WorkflowDocument] = field(default_factory=dict)
    #: model name → its ``secure: true`` fields (V020, spec §16.4).
    secure_fields: dict[str, set[str]] = field(default_factory=dict)
    #: ``type: decision`` AgentModels → (provider, model).
    decision_models: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: ``@agent`` names found in ``agents/``; ``None`` when not scanned.
    code_agents: set[str] | None = None
    #: Collection names; ``None`` when not scanned (the reference checks are then skipped).
    collections: set[str] | None = None
    #: ``Storage`` stores by name; ``None`` when not scanned (V030 is then skipped).
    stores: dict[str, StoreSpec] | None = None

    def resolve(self, name: str, variant: str) -> type[BaseModel] | None:
        return self.models.get(name, {}).get(variant)

    def add_model(self, definition: Mapping[str, Any]) -> None:
        name = (definition.get("metadata") or {}).get("name")
        if name and definition.get("enabled", True) is not False:
            self.models[str(name)] = model_variants(definition)
            spec = definition.get("spec") or {}
            secure = {
                str(f.get("name"))
                for f in spec.get("fields") or []
                if isinstance(f, dict) and f.get("secure")
            }
            if secure:
                self.secure_fields[str(name)] = secure

    @classmethod
    def from_registries(cls) -> Catalog:
        """A catalog backed by the live runtime registries (after load_all_configs)."""
        from tuvl.contract import registry
        from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY
        from tuvl.core.models.agent_models import AGENT_MODEL_REGISTRY
        from tuvl.core.models.collections_loader import COLLECTION_REGISTRY
        from tuvl.core.models.schemas import SCHEMA_REGISTRY
        from tuvl.storage.spec import STORE_REGISTRY

        return cls(
            models={k: dict(v) for k, v in SCHEMA_REGISTRY.items()},
            artifacts={n: (e.type, set(e.versions)) for n, e in ARTIFACT_REGISTRY.items()},
            agent_models={n: str(m.get("type") or "llm") for n, m in AGENT_MODEL_REGISTRY.items()},
            workflows=dict(registry.WORKFLOWS),
            collections=set(COLLECTION_REGISTRY),
            stores=dict(STORE_REGISTRY),
        )
