import datetime as _dt
import pathlib
import uuid as _uuid
from typing import Any

import sqlalchemy as _sa
import structlog
import yaml
from pydantic import JsonValue
from sqlalchemy.dialects.postgresql import JSONB as _JSONB
from sqlmodel import Field, SQLModel
from sqlmodel._compat import SQLModelConfig

from tuvl.core.models.schemas import SCHEMA_REGISTRY, generate_schemas

logger = structlog.get_logger("tuvl.models")

MODEL_REGISTRY: dict[str, type[SQLModel]] = {}

#: Operations a model may expose under /models/* via spec.api.crud, and the
#: access tier each one is guarded by.
CRUD_OPERATIONS: dict[str, str] = {
    "list": "read",
    "read": "read",
    "create": "write",
    "update": "write",
    "delete": "delete",
}

# Maps model name → {schema_version → raw config dict}.
# Populated by _register_model_from_dict for ALL versions (incl. disabled).
# Used by sync_model_versions() and admin endpoints.
MODEL_VERSION_REGISTRY: dict[str, dict[str, dict]] = {}

# Maps model name → EXPLICITLY declared spec.datasource name. Models without
# the key follow the resolved primary datasource (metadata.primary: true /
# first-postgres fallback) — consumers must use .get(name, primary), never
# assume presence. Read by WorkflowUoW, model_session, and init_db create_all.
MODEL_DATASOURCE_MAP: dict[str, str] = {}

# Relation metadata declared in YAML spec.relations blocks.
# Structure per entry:
#   {
#     "name":        str   — response key / Python attr (e.g. "candidate")
#     "model":       str   — MODEL_REGISTRY key of the related model
#     "foreign_key": str   — FK column name
#     "type":        str   — "many_to_one" | "one_to_many"
#   }
# many_to_one: FK lives on THIS model   (application.candidate_id → candidates.id)
# one_to_many: FK lives on RELATED model (assessments.application_id → applications.id)
MODEL_RELATIONS: dict[str, list[dict[str, str]]] = {}

# Field names marked ``secure: true`` in model YAMLs; consumed by the OTel hook
# in runner.py and every snapshot-masking site to redact PII.
# Rebuilt in place on hot reload — DO NOT MUTATE FROM OUTSIDE THE LOADER;
# callers wanting a stable view use ``secure_fields_view()`` below.
SECURE_FIELDS: set[str] = set()

#: The same fields, per model: what the CRUD API uses, so a field that only shares a *name* with a secure
#: one in another model is not hidden. ``SECURE_FIELDS`` stays the union of the names, for data that
#: carries no model (journal events, prompts, tool results).
SECURE_BY_MODEL: dict[str, frozenset[str]] = {}


def secure_fields_view() -> frozenset[str]:
    """Return an immutable snapshot of the secure-fields registry.

    Use this in code that needs to remain stable across a concurrent
    hot reload (``load_all_models()`` clears and re-adds to ``SECURE_FIELDS``
    atomically per add, but the overall replacement is not atomic).
    """
    return frozenset(SECURE_FIELDS)


TYPE_MAP = {
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
    # Native PostgreSQL enum — stored as str in Python, enforced by PG at DB level
    "enum": str,
}

# SQLAlchemy Declarative API reserves certain attribute names; using them as
# field names raises InvalidRequestError at class-creation time.
# For these names the Python attribute is prefixed with "extra_" while the
# database column retains the original name via an explicit sa_column.
_RESERVED_SA_NAMES: frozenset[str] = frozenset({"metadata"})

# Python type → lightweight SA type mapping, used only when a reserved-name
# non-jsonb field needs an explicit sa_column for column-name preservation.
_SA_TYPE_MAP: dict[type, Any] = {
    str: _sa.String,
    int: _sa.Integer,
    float: _sa.Numeric,
    bool: _sa.Boolean,
    _uuid.UUID: _sa.Uuid,
    _dt.date: _sa.Date,
    _dt.datetime: _sa.DateTime,
    bytes: _sa.LargeBinary,
}

# Column-name convention for audit timestamps.  A date/timestamp field is only
# auto-populated to "now" when the author declares no default AND the column is
# one of these audit names.  Every other default-less date/timestamp field is
# business data and honours its nullability (NULL when omitted) — "today"/"now"
# is never a safe implicit guess for e.g. dob, exit_date, due_date.  Authors who
# want a generated value on any other field opt in explicitly via
# ``default: now`` / ``default: today`` (recognised alongside ``default: uuid4``).
_AUDIT_TIMESTAMP_NAMES: frozenset[str] = frozenset({"created_at", "updated_at"})


def create_sqlmodel_class(schema: dict[str, Any]) -> type[SQLModel]:
    model_name = schema["metadata"]["name"]
    table_name = schema["spec"].get("tablename", model_name.lower())

    annotations: dict[str, Any] = {}
    field_definitions: dict[str, Any] = {}

    for field_spec in schema["spec"]["fields"]:
        col_name = field_spec["name"]
        # SQLAlchemy reserves some attribute names (e.g. "metadata").
        # Rename the Python attribute while keeping the DB column name intact.
        py_name = f"extra_{col_name}" if col_name in _RESERVED_SA_NAMES else col_name

        if field_spec["type"] not in TYPE_MAP:
            raise ValueError(
                f"Model '{model_name}' field '{col_name}': unknown type "
                f"'{field_spec['type']}'. Valid types: {sorted(TYPE_MAP)}."
            )
        py_type: Any = TYPE_MAP[field_spec["type"]]
        is_required = field_spec.get("required", False)
        is_pk = field_spec.get("primary_key", False)
        nullable = not is_required and not is_pk
        raw_default = field_spec.get("default")
        field_type = field_spec["type"]

        # ── ENUM ──────────────────────────────────────────────────────────────
        # Native PostgreSQL enum type.  Values are declared in the YAML via
        # ``enum_values: [val1, val2, ...]``.  SQLAlchemy creates the PG ENUM
        # type object named ``{table}_{col}_enum`` to avoid collisions.
        if field_type == "enum":
            enum_values: list[str] = field_spec.get("enum_values") or []
            enum_type_name = f"{table_name}_{col_name}_enum"
            sa_col = _sa.Column(
                col_name,
                _sa.Enum(*enum_values, name=enum_type_name),
                nullable=nullable,
                index=field_spec.get("index", False),
                unique=field_spec.get("unique", False),
            )
            if raw_default is not None:
                fld = Field(default=raw_default, sa_column=sa_col)
            elif nullable:
                fld = Field(default=None, sa_column=sa_col)
            else:
                fld = Field(sa_column=sa_col)
            annotations[py_name] = str | None if nullable else str
            field_definitions[py_name] = fld
            continue

        # ── JSONB ──────────────────────────────────────────────────────────────
        # SQLAlchemy cannot infer a column type from plain `dict`.
        # Always use an explicit sa_column backed by PostgreSQL JSONB.
        # col_name is passed to Column() so the DB column keeps its original name
        # even when the Python attribute has been renamed (e.g. extra_metadata).
        if field_type == "jsonb":
            sa_col = _sa.Column(col_name, _JSONB, nullable=nullable)
            if raw_default is not None:
                fld = Field(default=raw_default, sa_column=sa_col)
            elif nullable:
                fld = Field(default=None, sa_column=sa_col)
            else:
                fld = Field(sa_column=sa_col)
            # Any JSON value — objects, lists and scalars all live in jsonb.
            annotations[py_name] = JsonValue | None if nullable else JsonValue
            field_definitions[py_name] = fld
            continue

        # ── TIMESTAMPTZ ────────────────────────────────────────────────────────
        # The advertised ``timestamptz`` type must map to a timezone-AWARE
        # column (``TIMESTAMP WITH TIME ZONE``) with a UTC default, not the
        # tz-naive ``TIMESTAMP`` that the bare datetime mapping would produce.
        if field_type == "timestamptz":
            sa_col = _sa.Column(
                col_name,
                _sa.DateTime(timezone=True),
                primary_key=is_pk,
                index=field_spec.get("index", False),
                unique=field_spec.get("unique", False),
                nullable=nullable,
            )
            # Auto-populate to now() only for the audit-column convention or an
            # explicit ``default: now``; other default-less timestamptz fields
            # are business data and honour their nullability.
            auto_now = raw_default in ("now", "utcnow") or (
                "default" not in field_spec and py_name in _AUDIT_TIMESTAMP_NAMES
            )
            if auto_now:
                fld = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC), sa_column=sa_col)
                annotations[py_name] = _dt.datetime | None
            elif "default" in field_spec:
                fld = Field(default=raw_default, sa_column=sa_col)
                annotations[py_name] = _dt.datetime | None
            elif nullable:
                fld = Field(default=None, sa_column=sa_col)
                annotations[py_name] = _dt.datetime | None
            else:
                fld = Field(sa_column=sa_col)
                annotations[py_name] = _dt.datetime
            field_definitions[py_name] = fld
            continue

        field_kwargs: dict[str, Any] = {
            "primary_key": is_pk,
            "index": field_spec.get("index", False),
            "unique": field_spec.get("unique", False),
            "nullable": nullable,
        }

        if raw_default == "uuid4":
            field_kwargs["default_factory"] = _uuid.uuid4
            annotations[py_name] = _uuid.UUID

        elif raw_default in ("now", "utcnow") and field_type == "timestamp":
            # Explicit opt-in: auto-populate to the current datetime.
            field_kwargs["default_factory"] = _dt.datetime.now
            annotations[py_name] = _dt.datetime | None

        elif raw_default == "today" and field_type == "date":
            # Explicit opt-in: auto-populate to the current date.
            field_kwargs["default_factory"] = _dt.date.today
            annotations[py_name] = _dt.date | None

        elif "default" not in field_spec and field_type == "uuid" and is_pk:
            # UUID primary key auto-generates — optional to supply in requests.
            # A non-PK uuid field (e.g. a nullable FK) is NOT auto-generated;
            # it falls through to the standard nullable/required handling so an
            # omitted value persists as NULL instead of a dangling random UUID.
            field_kwargs["default_factory"] = _uuid.uuid4
            annotations[py_name] = _uuid.UUID

        elif (
            "default" not in field_spec
            and field_type == "timestamp"
            and py_name in _AUDIT_TIMESTAMP_NAMES
        ):
            # Audit columns (created_at/updated_at) auto-populate to now.
            # Other default-less timestamp fields are business data → honour NULL.
            field_kwargs["default_factory"] = _dt.datetime.now
            annotations[py_name] = _dt.datetime | None

        elif "default" in field_spec:
            field_kwargs["default"] = raw_default
            annotations[py_name] = py_type | None

        elif is_required or is_pk:
            annotations[py_name] = py_type

        else:
            field_kwargs["default"] = None
            annotations[py_name] = py_type | None

        # When the Python attribute name differs from the column name we must
        # tell SQLAlchemy the DB column name via an explicit sa_column.
        if py_name != col_name:
            sa_type = _SA_TYPE_MAP.get(py_type, _sa.String)
            sa_col = _sa.Column(
                col_name,
                sa_type,
                primary_key=field_kwargs.pop("primary_key", False),
                index=field_kwargs.pop("index", False),
                unique=field_kwargs.pop("unique", False),
                nullable=field_kwargs.pop("nullable", True),
            )
            remaining: dict[str, Any] = {}
            if "default" in field_kwargs:
                remaining["default"] = field_kwargs["default"]
            elif "default_factory" in field_kwargs:
                remaining["default_factory"] = field_kwargs["default_factory"]
            field_definitions[py_name] = Field(sa_column=sa_col, **remaining)
        else:
            field_definitions[py_name] = Field(**field_kwargs)

    namespace = {
        "__annotations__": annotations,
        "__tablename__": table_name,
        "model_config": SQLModelConfig(table=True),
        **field_definitions,
    }

    stale = SQLModel.metadata.tables.get(table_name)
    if stale is not None and not table_name.startswith("tuvl_system_"):
        # A reload (or a version toggle that dropped the class) left the old
        # definition behind; the new one replaces it.
        SQLModel.metadata.remove(stale)
    cls = type(model_name, (SQLModel,), namespace)
    # Stamp the schema_version onto the generated class so the WorkflowUoW
    # can verify that a workflow's ``context.models[].version`` pin matches
    # the version that is actually live in MODEL_REGISTRY.
    cls.__schema_version__ = schema.get("metadata", {}).get("schema_version", "v1")  # type: ignore[attr-defined]
    return cls


def _register_model_from_dict(data: dict[str, Any], filename: str = "<unknown>") -> None:
    """Register a single ``ModelDefinition`` dict into all model registries."""
    if data.get("kind") != "ModelDefinition":
        return

    model_name = data.get("metadata", {}).get("name") or data.get("name", "")
    if not model_name:
        raise RuntimeError(f"ModelDefinition in {filename} is missing metadata.name.")
    # The ``tuvl_system_`` prefix is reserved for engine-internal tables; a user
    # model claiming it (via spec.tablename or name.lower()) would collide with /
    # corrupt a system table, so reject it at load time.
    _tablename = (
        data.get("spec", {}).get("tablename")
        or data.get("spec", {}).get("table")
        or model_name.lower()
    )
    if _tablename.startswith("tuvl_system_"):
        raise RuntimeError(
            f"Model '{model_name}' ({filename}) targets table '{_tablename}', but the "
            f"'tuvl_system_' prefix is reserved for engine-internal tables. Rename it."
        )
    schema_version: str = data.get("metadata", {}).get("schema_version", "v1")

    # Always populate the version registry (even disabled versions) so the
    # admin API can list them and toggle their state.
    existing_versions = MODEL_VERSION_REGISTRY.setdefault(model_name, {})

    # Cross-version collision detection: two enabled versions of the same
    # model targeting the same physical table with different field shapes
    # would silently corrupt each other under concurrent workflow execution
    # (the second-registered class overwrites the first in MODEL_REGISTRY).
    # Refuse to register the new version when the on-disk shapes diverge.
    if data.get("enabled", True) is not False:
        new_table = data.get("spec", {}).get("tablename") or data.get("spec", {}).get("table")
        new_fields = data.get("spec", {}).get("fields", [])
        new_field_sig = sorted(
            (
                f.get("name"),
                f.get("type"),
                bool(f.get("required", False)),
                bool(f.get("primary_key", False)),
                bool(f.get("unique", False)),
            )
            for f in new_fields
            if isinstance(f, dict) and f.get("name")
        )
        for prior_version, prior_data in existing_versions.items():
            if prior_version == schema_version or prior_data.get("enabled", True) is False:
                continue
            prior_table = prior_data.get("spec", {}).get("tablename") or prior_data.get(
                "spec", {}
            ).get("table")
            prior_fields = prior_data.get("spec", {}).get("fields", [])
            prior_field_sig = sorted(
                (
                    f.get("name"),
                    f.get("type"),
                    bool(f.get("required", False)),
                    bool(f.get("primary_key", False)),
                    bool(f.get("unique", False)),
                )
                for f in prior_fields
                if isinstance(f, dict) and f.get("name")
            )
            if new_table == prior_table and new_field_sig != prior_field_sig:
                raise RuntimeError(
                    f"Cross-version model collision: '{model_name}' versions "
                    f"{prior_version!r} and {schema_version!r} share physical "
                    f"table {new_table!r} but declare different field shapes. "
                    f"Concurrent workflow steps targeting different versions "
                    f"would corrupt rows. Resolve by (a) giving each version "
                    f"its own ``spec.tablename``, (b) disabling the older "
                    f"version with ``enabled: false``, or (c) aligning the "
                    f"field declarations. Offending file: {filename}."
                )

    existing_versions[schema_version] = data

    if data.get("enabled", True) is False:
        logger.info("Skipped model (enabled: false)", filename=filename)
        return

    model_class = create_sqlmodel_class(data)
    MODEL_REGISTRY[model_class.__name__] = model_class

    ds_name = data["spec"].get("datasource")
    if ds_name:
        MODEL_DATASOURCE_MAP[model_class.__name__] = ds_name

    logger.info(
        "Loaded model",
        model=model_class.__name__,
        table=model_class.__tablename__,
        datasource=ds_name or "(primary)",
    )

    _secure = frozenset(
        spec["name"] for spec in data["spec"].get("fields", []) if spec.get("secure")
    )
    if _secure:
        SECURE_BY_MODEL[model_class.__name__] = _secure
        SECURE_FIELDS.update(_secure)

    relations = data["spec"].get("relations", [])
    if relations:
        validated: list[dict[str, str]] = []
        for r in relations:
            rel_type = r.get("type", "many_to_one")
            if rel_type not in ("many_to_one", "one_to_many"):
                logger.warning(
                    "Unknown relation type — skipped",
                    rel_type=rel_type,
                    filename=filename,
                )
                continue
            validated.append(
                {
                    "name": r["name"],
                    "model": r["model"],
                    "foreign_key": r["foreign_key"],
                    "type": rel_type,
                }
            )
        MODEL_RELATIONS[model_class.__name__] = validated
        rel_names = ", ".join(r["name"] for r in validated)
        logger.info("Relations registered", model=model_class.__name__, relations=rel_names)

    schemas = generate_schemas(
        model_class.__name__,
        data["spec"]["fields"],
        relations=MODEL_RELATIONS.get(model_class.__name__, []),
    )
    # Optional per-model CRUD scope overrides declared in the YAML:
    #   spec:
    #     access:
    #       read_scope:   candidate:read
    #       write_scope:  candidate:write
    #       delete_scope: candidate:delete
    # When absent the CRUD router derives scopes by convention:
    # <modelnamelower>:read / :write / :delete
    access_cfg = data["spec"].get("access")
    if access_cfg:
        schemas["access"] = access_cfg
    # spec.api.crud lists the /models/* operations this model exposes;
    # none by default.
    api_cfg = data["spec"].get("api") or {}
    crud_ops = api_cfg.get("crud") if isinstance(api_cfg, dict) else None
    schemas["crud"] = frozenset(  # type: ignore[assignment]
        op for op in (crud_ops or []) if op in CRUD_OPERATIONS
    )
    SCHEMA_REGISTRY[model_class.__name__] = schemas
    names = ", ".join(f"{model_class.__name__}{v.title()}" for v in schemas)
    logger.info("Schemas generated", model=model_class.__name__, schemas=names)


def load_all_models(config_dir: pathlib.Path | str | None = None) -> None:
    from tuvl.core.config_loader import _forget_model_tables

    _forget_model_tables(MODEL_REGISTRY.values())  # reloading must not redefine live tables
    MODEL_REGISTRY.clear()
    MODEL_VERSION_REGISTRY.clear()
    MODEL_DATASOURCE_MAP.clear()
    MODEL_RELATIONS.clear()
    SECURE_FIELDS.clear()
    SECURE_BY_MODEL.clear()

    if config_dir is None:
        from tuvl.core.config import settings

        config_dir = settings.dir_for("models")

    base_path = pathlib.Path(config_dir)

    if not base_path.exists():
        logger.warning("Config directory not found", path=str(base_path.resolve()))
        return

    yaml_files = list(base_path.glob("*.yaml"))
    if not yaml_files:
        logger.warning("No YAML files found", path=str(base_path.resolve()))
        return

    for yaml_file in yaml_files:
        with open(yaml_file) as f:
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                mark = getattr(exc, "problem_mark", None)
                location = f" (line {mark.line + 1}, col {mark.column + 1})" if mark else ""
                logger.error(
                    "YAML parse error",
                    filename=yaml_file.name,
                    location=location,
                    error=str(exc),
                )
                raise

        _register_model_from_dict(data, yaml_file.name)
