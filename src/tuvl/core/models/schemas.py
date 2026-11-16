import datetime as _dt
import uuid as _uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "text": str,
    "varchar": str,
    "integer": int,
    "bigint": int,
    "smallint": int,
    "numeric": float,
    "float": float,
    "boolean": bool,
    "uuid": _uuid.UUID,
    "date": _dt.date,
    "timestamp": _dt.datetime,
    "timestamptz": _dt.datetime,
    "jsonb": JsonValue,
    "bytea": bytes,
    # Native enum — resolved to Literal[...] below when enum_values are present
    "enum": str,
}

# Mirror the loader's reserved-name list so Pydantic schema field names stay
# consistent with the SQLModel attribute names.  The alias ensures the JSON
# API surface still uses the original field name (e.g. "metadata").
_RESERVED_NAMES: frozenset[str] = frozenset({"metadata"})

#: per model: the ``create`` / ``read`` / ``update`` / ``expanded`` classes, the ``crud`` operations, ``access``
SCHEMA_REGISTRY: dict[str, dict[str, Any]] = {}


def _is_input_field(field_spec: dict[str, Any]) -> bool:
    if "input" in field_spec:
        return bool(field_spec["input"])
    return not field_spec.get("primary_key", False)


def _build_field(
    field_spec: dict[str, Any],
    *,
    force_optional: bool = False,
    alias: str | None = None,
) -> tuple[Any, Any]:  # (type-form incl. unions/Literals, pydantic Field)
    field_type = field_spec["type"]
    # For native enum fields, build a Literal type from the declared values so
    # Pydantic validates that the submitted value is one of the allowed choices.
    enum_vals: list[str] = field_spec.get("enum_values") or []
    if field_type == "enum" and enum_vals:
        py_type: Any = Literal[tuple(enum_vals)]
    else:
        py_type = _TYPE_MAP.get(field_type, str)
    is_required = field_spec.get("required", False)
    raw_default = field_spec.get("default")

    field_kwargs: dict[str, Any] = {
        "title": field_spec["name"].replace("_", " ").title(),
        "description": field_spec.get("description", ""),
    }
    if alias is not None:
        field_kwargs["alias"] = alias

    if force_optional:
        return py_type | None, Field(default=None, **field_kwargs)

    if raw_default == "uuid4":
        return py_type, Field(default_factory=_uuid.uuid4, **field_kwargs)

    # Auto-defaults for self-generating types (no explicit default needed)
    if "default" not in field_spec:
        if field_type == "uuid":
            return _uuid.UUID | None, Field(default_factory=_uuid.uuid4, **field_kwargs)
        if field_type == "date":
            return _dt.date | None, Field(default_factory=_dt.date.today, **field_kwargs)
        if field_type in ("timestamp", "timestamptz"):
            return _dt.datetime | None, Field(default_factory=_dt.datetime.now, **field_kwargs)

    if "default" in field_spec:
        return py_type | None, Field(default=raw_default, **field_kwargs)

    if is_required:
        return py_type, Field(**field_kwargs)

    return py_type | None, Field(default=None, **field_kwargs)


def _make_schema_class(
    class_name: str,
    fields: list[dict[str, Any]],
    *,
    input_only: bool = False,
    force_optional: bool = False,
    secure_scope: str | None = None,
) -> type[BaseModel]:
    """*secure_scope* marks a read schema: its ``secure`` fields are optional (the API leaves them out
    unless the caller holds that scope) and say so."""
    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {}

    for field_spec in fields:
        if input_only and not _is_input_field(field_spec):
            continue

        orig_name = field_spec["name"]
        fname = f"extra_{orig_name}" if orig_name in _RESERVED_NAMES else orig_name
        # When the Python attribute name differs from the JSON key, pass the
        # original name as an alias so `model_dump(by_alias=True)` emits the
        # expected key and JSON input is still accepted under that key.
        field_alias = orig_name if fname != orig_name else None

        is_secure_read = secure_scope is not None and bool(field_spec.get("secure"))
        spec = (
            {
                **field_spec,
                "description": (
                    f"{field_spec.get('description', '')} Omitted unless the caller holds "
                    f"`{secure_scope}`."
                ).strip(),
            }
            if is_secure_read
            else field_spec
        )
        annotation, field_info = _build_field(
            spec, force_optional=force_optional or is_secure_read, alias=field_alias
        )
        annotations[fname] = annotation
        namespace[fname] = field_info

    namespace["__annotations__"] = annotations
    namespace["model_config"] = ConfigDict(
        from_attributes=True,  # allow model_validate(orm_obj)
        populate_by_name=True,  # allow init by Python name even when alias is set
    )
    return type(class_name, (BaseModel,), namespace)


def generate_schemas(
    model_name: str,
    fields: list[dict[str, Any]],
    relations: list[dict[str, str]] | None = None,
) -> dict[str, type[BaseModel]]:
    schemas: dict[str, type[BaseModel]] = {
        "create": _make_schema_class(
            f"{model_name}Create", fields, input_only=True, force_optional=False
        ),
        "read": _make_schema_class(
            f"{model_name}Read",
            fields,
            input_only=False,
            force_optional=False,
            secure_scope=f"{model_name.lower()}:read_secure",
        ),
        "update": _make_schema_class(
            f"{model_name}Update", fields, input_only=True, force_optional=True
        ),
    }

    if relations:
        schemas["expanded"] = _make_expanded_schema(
            f"{model_name}Expanded",
            base_cls=schemas["read"],
            relations=relations,
        )

    return schemas


def _make_expanded_schema(
    class_name: str,
    base_cls: type[BaseModel],
    relations: list[dict[str, str]],
) -> type[BaseModel]:
    """Build a Read schema with optional nested relation fields.

    The nested types are resolved lazily (at first request) from SCHEMA_REGISTRY
    so that forward-references across models are never a problem — both models
    just need to be loaded before the first API call, which is guaranteed by
    load_all_models().

    Each relation field is typed as:
        many_to_one  → <RelModel>Read | None  (default None)
        one_to_many  → list[<RelModel>Read]   (default [])
    """
    from tuvl.core.models.schemas import (
        SCHEMA_REGISTRY,
    )  # local to avoid circular import at module load

    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {}

    # Inherit all base fields by copying model_fields into the new class.
    for fname, finfo in base_cls.model_fields.items():
        annotations[fname] = base_cls.__annotations__.get(fname, Any)
        namespace[fname] = finfo

    # Inject relation fields.
    for rel in relations:
        rel_name = rel["name"]
        rel_model_name = rel["model"]
        rel_type = rel["type"]

        # Resolve the Read schema for the related model; fall back to dict if
        # the related model hasn't declared schema: true.
        def _get_rel_cls(name: str = rel_model_name) -> type[BaseModel] | None:
            return SCHEMA_REGISTRY.get(name, {}).get("read")

        if rel_type == "many_to_one":
            rel_cls = _get_rel_cls()
            ann: Any = (rel_cls | None) if rel_cls else (dict | None)
            namespace[rel_name] = Field(default=None)
        else:  # one_to_many
            rel_cls = _get_rel_cls()
            ann = list[rel_cls] if rel_cls else list[dict]  # type: ignore[valid-type]
            namespace[rel_name] = Field(default_factory=list)

        annotations[rel_name] = ann

    namespace["__annotations__"] = annotations
    namespace["model_config"] = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
    )
    return type(class_name, (BaseModel,), namespace)
