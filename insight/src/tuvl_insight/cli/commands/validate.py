from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import warnings
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from tuvl.core.artifacts.kinds import (
    ARTIFACT_TYPES,
    STRUCTURED_TYPES,
)
from tuvl.core.auth.schemas import SCOPE_NAME_RE
from tuvl.core.bootstrap import VALID_DEPLOYMENT_MODES
from tuvl.core.config_loader import _PROCESS_ORDER as _RUNTIME_KINDS
from tuvl.core.config_loader import _SKIP_KINDS as _RUNTIME_SKIP_KINDS
from tuvl.core.config_loader import _walk_yaml_files
from tuvl.core.engine.guardrails import GUARDRAIL_CHECKS
from tuvl.core.models.loader import TYPE_MAP
from tuvl.core.system_config import (
    API_DOCS_MODES,
    API_GROUPS,
    SYSTEM_API_KEYS,
    SYSTEM_YAML_RELPATH,
    invalid_cors_origin,
)

console = Console()

# Sourced from the model loader's canonical map so validator and runtime can
# never drift (the loader is the authority on supported column types, incl. enum).
_FIELD_TYPES = set(TYPE_MAP)

_WORKFLOW_KIND = "Workflow"
_MODEL_KIND = "ModelDefinition"
_DS_KINDS = ("DataSource", "RedisConfig")
_LLM_KIND = "AgentModel"
_EMBEDDING_KINDS = ("EmbeddingRegistry", "EmbeddingConfig")
_COLLECTION_KINDS = ("CollectionRegistry", "CollectionConfig")
_FEDERATION_KIND = "FederationProvider"
_STORAGE_KIND = "Storage"
_V1_TEST_KIND = "test"

# Closed set of resource kinds the runtime dispatches on — imported from the
# central config loader so the validator can never recognize a kind the
# runtime wouldn't (or vice versa). TD-14: this is also what makes file
# *discovery* below identical to the runtime's.
_KNOWN_KINDS: frozenset[str] = frozenset(_RUNTIME_KINDS) | frozenset(_RUNTIME_SKIP_KINDS)

# tuvl-ship-generated output (Dockerfile + Helm chart under deploy/). Helm
# templates carry literal Kubernetes `kind:` fields (Deployment, Service) that
# collide with tuvl's own kind namespace, and use Go template `{{ }}` syntax
# that yaml.safe_load cannot parse. deploy/ is already excluded from the built
# image via .dockerignore, so it is not part of the project's loaded config
# surface — validate must not treat it as one either (a re-run of `tuvl ship`
# against an already-shipped project would otherwise flag its own output).
_EXTRA_EXCLUDE_DIRS: frozenset[str] = frozenset({"deploy"})


def _walk_project_yaml(project_dir: Path) -> Iterator[Path]:
    """The runtime's recursive YAML walk, minus tuvl-ship's own output dir."""
    for path in _walk_yaml_files(project_dir):
        rel_parts = path.relative_to(project_dir).parts[:-1]
        if _EXTRA_EXCLUDE_DIRS.isdisjoint(rel_parts):
            yield path


def _label(project_dir: Path, path: Path) -> str:
    """Render *path* relative to the project root for report display."""
    try:
        return str(path.relative_to(project_dir))
    except ValueError:
        return str(path)


def _discover_by_kind(
    project_dir: Path,
) -> tuple[dict[str, list[Path]], list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Walk *project_dir* exactly like ``config_loader.load_all_configs`` and
    group every YAML file by its declared ``kind`` (TD-14).

    Location- and extension-agnostic: a ``*.yaml``/``*.yml`` file anywhere in
    the tree (skip-dirs aside) is discovered regardless of which directory it
    lives in — matching the runtime, which dispatches purely by ``kind``.
    Returns ``(by_kind, unrecognized, unparsable)``: ``unrecognized`` lists
    ``(path, kind)`` pairs whose ``kind`` the runtime does not handle (e.g. a
    typo); ``unparsable`` lists ``(path, error)`` for files that are not valid
    YAML — their kind is unknowable, so no per-kind validator would see them.
    """
    by_kind: dict[str, list[Path]] = defaultdict(list)
    unrecognized: list[tuple[Path, str]] = []
    unparsable: list[tuple[Path, str]] = []
    for yaml_path in _walk_project_yaml(project_dir):
        data, err = _load_yaml(yaml_path)
        if err:
            unparsable.append((yaml_path, err))
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("kind")
        if not kind:
            continue
        if kind not in _KNOWN_KINDS:
            unrecognized.append((yaml_path, str(kind)))
            continue
        by_kind[kind].append(yaml_path)
    for paths in by_kind.values():
        paths.sort()
    return dict(by_kind), unrecognized, unparsable


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    file: str
    message: str
    code: str = ""
    line: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "code": self.code or None,
            "message": self.message,
        }


@dataclass
class ValidateResult:
    issues: list[Issue] = field(default_factory=list)
    #: 2.0 workflow name → determinism/cost profile (spec §13).
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Models the workflows use and the environment variables they need (V028/V029).
    models: dict[str, dict[str, Any]] = field(default_factory=dict)

    def error(self, file: str, msg: str) -> None:
        self.issues.append(Issue("error", file, msg))

    def warn(self, file: str, msg: str) -> None:
        self.issues.append(Issue("warning", file, msg))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]


def _load_yaml(path: Path) -> tuple[dict | None, str | None]:
    """Return (parsed_dict, error_string). One of them will be None."""
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
        return data, None
    except yaml.YAMLError as exc:
        from tuvl.contract.registry import yaml_error_hint

        hint = yaml_error_hint(exc)
        return None, f"YAML parse error: {exc}" + (f"\n  hint: {hint}" if hint else "")
    except OSError as exc:
        return None, f"Cannot read file: {exc}"


def _require_spec(data: dict, label: str, result: ValidateResult) -> dict | None:
    """Return the spec-flattened document, or ``None`` (recording an error)
    when the document has no ``spec`` block. The spec-wrapped envelope is the
    only accepted form — mirrors ``api.manager._normalize_workflow``."""
    spec = data.get("spec")
    if not isinstance(spec, dict):
        result.error(
            label,
            "No 'spec' block — the root-level (flat) document form is not "
            "accepted. Wrap the document body under 'spec:'.",
        )
        return None
    return {**data, **spec}


def _resolve_dirs(project_dir: Path) -> dict[str, Path]:
    """Read config.yaml and return absolute directory paths for each kind."""
    defaults = {
        "models": "models",
        "workflows": "workflows",
        "datasources": "datasources",
        "llms": "llms",
        "agents": "agents",
    }
    cfg_path = project_dir / "config.yaml"
    if cfg_path.exists():
        data, err = _load_yaml(cfg_path)
        if data and data.get("kind") == "ProjectConfig":
            cfg_dirs = data.get("dirs", {})
            for k in defaults:
                if k in cfg_dirs:
                    defaults[k] = cfg_dirs[k]
    return {k: (project_dir / v).resolve() for k, v in defaults.items()}


def _validate_project_config(project_dir: Path, result: ValidateResult) -> None:
    cfg_path = project_dir / "config.yaml"
    if not cfg_path.exists():
        # Optional: discovery is kind-based, so the default layout needs no config.
        return
    data, err = _load_yaml(cfg_path)
    if err:
        result.error("config.yaml", err)
        return
    if data is None:
        result.warn("config.yaml", "File is empty")
        return
    if data.get("kind") != "ProjectConfig":
        result.error("config.yaml", f"Expected kind='ProjectConfig', got '{data.get('kind')}'")


def _validate_system_config(project_dir: Path, result: ValidateResult) -> None:
    """Validate ``.tuvl/system.yaml`` (kind: SystemConfig) when present.

    The runtime reads this file fail-open, so validation is the only place
    a typo (e.g. a root-level ``api:`` block, or a non-bool value) surfaces.
    """
    path = project_dir / SYSTEM_YAML_RELPATH
    label = SYSTEM_YAML_RELPATH
    if not path.exists():
        return
    data, err = _load_yaml(path)
    if err:
        result.error(label, err)
        return
    if not isinstance(data, dict):
        result.warn(label, "File is empty")
        return
    if data.get("kind") != "SystemConfig":
        result.error(label, f"Expected kind='SystemConfig', got '{data.get('kind')}'")
    merged = _require_spec(data, label, result)
    if merged is None:
        return

    mode = merged.get("deployment_mode")
    if mode is not None and str(mode).strip().lower() not in VALID_DEPLOYMENT_MODES:
        result.error(
            label,
            f"Invalid deployment_mode '{mode}' — must be one of {sorted(VALID_DEPLOYMENT_MODES)}",
        )

    api_cfg = merged.get("api")
    if api_cfg is None:
        return
    if not isinstance(api_cfg, dict):
        result.error(label, "'spec.api' must be a mapping")
        return
    for key in set(api_cfg) - SYSTEM_API_KEYS:
        result.warn(label, f"Unknown key 'spec.api.{key}' — will be ignored by the engine")
    if "expose_model_crud" in api_cfg and not isinstance(api_cfg["expose_model_crud"], bool):
        result.error(
            label,
            "'spec.api.expose_model_crud' must be a boolean "
            f"(got {type(api_cfg['expose_model_crud']).__name__})",
        )
    if "dev_expose_all" in api_cfg and not isinstance(api_cfg["dev_expose_all"], bool):
        result.error(label, "'spec.api.dev_expose_all' must be a boolean")
    if "expose" in api_cfg:
        groups = api_cfg["expose"]
        if not isinstance(groups, list):
            result.error(label, f"'spec.api.expose' must be a list of {list(API_GROUPS)}")
        else:
            for group in groups:
                if str(group).strip().lower() not in (*API_GROUPS, "runs"):
                    result.error(
                        label,
                        f"Unknown group '{group}' in 'spec.api.expose' — must be one of {list(API_GROUPS)} (runs is always mounted)",
                    )
    if "cors_origins" in api_cfg:
        origins = api_cfg["cors_origins"]
        if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
            result.error(label, "'spec.api.cors_origins' must be a list of origins (strings)")
        else:
            for origin in origins:
                problem = invalid_cors_origin(origin)
                if problem:
                    result.error(label, f"'spec.api.cors_origins' entry {origin!r} {problem}")
            if "*" in origins:
                result.warn(
                    label,
                    "'spec.api.cors_origins' contains '*': any website can call this API with a token it holds; list the origins instead",
                )
    docs = api_cfg.get("docs")
    if "docs" in api_cfg and docs is not False and str(docs).strip().lower() not in API_DOCS_MODES:
        result.error(label, f"'spec.api.docs' must be one of {list(API_DOCS_MODES)} (got {docs!r})")


def _validate_models(files: list[Path], project_dir: Path, result: ValidateResult) -> set[str]:
    """Validate ``ModelDefinition`` YAMLs discovered anywhere in the project.
    Returns set of known model names."""
    known_models: set[str] = set()

    if not files:
        result.warn("models/", "No ModelDefinition YAML files found anywhere in the project")
        return known_models

    for yf in files:
        label = _label(project_dir, yf)
        data, err = _load_yaml(yf)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict):
            result.error(label, "File does not parse to a YAML mapping")
            continue

        kind = data.get("kind")
        if kind != _MODEL_KIND:
            result.warn(label, f"Unexpected kind '{kind}' — expected '{_MODEL_KIND}'")
            continue

        meta = data.get("metadata") or {}
        name = meta.get("name")
        if not name:
            result.error(label, "metadata.name is required")
            continue
        known_models.add(name)

        spec = data.get("spec") or {}
        if not spec.get("tablename"):
            result.warn(label, f"[{name}] spec.tablename not set — will use lowercase model name")

        fields = spec.get("fields", [])
        if not fields:
            result.error(label, f"[{name}] spec.fields is empty")
            continue

        for i, fld in enumerate(fields):
            fname = fld.get("name", f"#field_{i}")
            ftype = fld.get("type", "")
            if not ftype:
                result.error(label, f"[{name}] field '{fname}': type is required")
            elif ftype not in _FIELD_TYPES:
                result.error(label, f"[{name}] field '{fname}': unknown type '{ftype}'")

        for rel in spec.get("relations", []):
            rel_model = rel.get("model", "")
            rel_name = rel.get("name", "")
            if not rel_model:
                result.error(label, f"[{name}] relation '{rel_name}': model is required")
            if rel.get("type") not in ("many_to_one", "one_to_many", None):
                result.warn(
                    label, f"[{name}] relation '{rel_name}': unexpected type '{rel.get('type')}'"
                )

        _validate_model_access(spec.get("access"), name, label, result)
        _validate_model_api(spec.get("api"), name, label, result)
        if "schema" in spec:
            result.warn(
                label,
                f"[{name}] spec.schema is gone in 2.0 — schemas are always generated; "
                f"expose /models/* routes with spec.api.crud instead",
            )

    return known_models


def _validate_model_api(api: Any, name: str, label: str, result: ValidateResult) -> None:
    """spec.api.crud: the /models/* operations the model exposes (none by default)."""
    from tuvl.core.models.loader import CRUD_OPERATIONS

    if api is None:
        return
    if not isinstance(api, dict):
        result.error(label, f"[{name}] spec.api must be a mapping")
        return
    for key in set(api) - {"crud"}:
        result.warn(label, f"[{name}] unknown key 'spec.api.{key}' — will be ignored")
    crud = api.get("crud")
    if crud is None:
        return
    if not isinstance(crud, list) or not all(isinstance(op, str) for op in crud):
        result.error(label, f"[{name}] spec.api.crud must be a list of operations")
        return
    for op in crud:
        if op not in CRUD_OPERATIONS:
            result.error(
                label,
                f"[{name}] spec.api.crud: unknown operation '{op}' — one of {sorted(CRUD_OPERATIONS)}",
            )


_ACCESS_KEYS = frozenset(
    {"read_scope", "write_scope", "delete_scope", "read_groups", "write_groups", "delete_groups"}
)


def _validate_model_access(access: Any, name: str, label: str, result: ValidateResult) -> None:
    """Validate a model's spec.access block (scope overrides + group ACLs)."""
    if access is None:
        return
    if not isinstance(access, dict):
        result.error(label, f"[{name}] spec.access must be a mapping")
        return
    for key in set(access) - _ACCESS_KEYS:
        result.warn(label, f"[{name}] unknown key 'spec.access.{key}' — will be ignored")
    for key in ("read_scope", "write_scope", "delete_scope"):
        val = access.get(key)
        if val is None:
            continue
        if not isinstance(val, str) or not SCOPE_NAME_RE.match(val):
            result.error(
                label,
                f"[{name}] spec.access.{key} '{val}' is not a valid scope name "
                "(word chars, ':', '.', '-', '/' only)",
            )
    for key in ("read_groups", "write_groups", "delete_groups"):
        val = access.get(key)
        if val is None:
            continue
        groups = [val] if isinstance(val, str) else val
        if not isinstance(groups, list):
            result.error(label, f"[{name}] spec.access.{key} must be a group name or list")
            continue
        for g in groups:
            if not isinstance(g, str) or not SCOPE_NAME_RE.match(g):
                result.error(
                    label,
                    f"[{name}] spec.access.{key} entry '{g}' is not a valid group name "
                    "(word chars, ':', '.', '-', '/' only)",
                )


def _validate_datasources(files: list[Path], project_dir: Path, result: ValidateResult) -> set[str]:
    """Validate ``DataSource`` and ``RedisConfig`` YAMLs — both register into
    the runtime's single ``DATASOURCE_REGISTRY``, so both share one known-name
    set. Returns set of known datasource names."""
    known: set[str] = set()

    if not files:
        # No warning here — mirrors the pre-TD-14 behavior, where an existing
        # but empty datasources/ dir was silent (only a missing dir warned,
        # and that check was subsumed by an empty glob either way).
        return known

    for yf in sorted(files):
        label = _label(project_dir, yf)
        data, err = _load_yaml(yf)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict):
            result.error(label, "File does not parse to a YAML mapping")
            continue

        kind = data.get("kind")
        if kind not in _DS_KINDS:
            result.warn(label, f"Unexpected kind '{kind}' — expected one of {_DS_KINDS}")
            continue

        meta = data.get("metadata") or {}
        name = meta.get("name")
        if not name:
            result.error(label, "metadata.name is required")
        else:
            known.add(name)

        spec = data.get("spec") or {}
        ds_type = spec.get("type")
        if not ds_type:
            result.error(label, f"[{name}] spec.type is required (e.g. 'postgres')")

    return known


def _validate_llms(files: list[Path], project_dir: Path, result: ValidateResult) -> set[str]:
    """Validate ``AgentModel`` YAMLs discovered anywhere in the project.
    Returns set of known LLM preset names."""
    known: set[str] = set()

    if not files:
        # No warning here — mirrors the pre-TD-14 behavior, where an existing
        # but empty llms/ dir was silent (no AgentModel presets is a valid
        # project state, e.g. Router/Functional-only workflows).
        return known

    for yf in sorted(files):
        label = _label(project_dir, yf)
        data, err = _load_yaml(yf)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict):
            result.error(label, "File does not parse to a YAML mapping")
            continue

        kind = data.get("kind")
        if kind != _LLM_KIND:
            result.warn(label, f"Unexpected kind '{kind}' — expected '{_LLM_KIND}'")
            continue

        merged = _require_spec(data, label, result)
        if merged is None:
            continue
        data = merged
        meta = data.get("metadata") or {}
        name = meta.get("name")
        if not name:
            result.error(label, "metadata.name is required")
        else:
            known.add(name)

        model = data.get("model")
        if not model:
            result.error(label, f"[{name}] 'model' field is required (provider/model id)")

    return known


def _validate_embeddings(files: list[Path], project_dir: Path, result: ValidateResult) -> set[str]:
    """Kind-recognition + shape check for ``EmbeddingRegistry``/``EmbeddingConfig``
    YAMLs (TD-14 — previously unvalidated). Returns known embedding names."""
    known: set[str] = set()
    for yf in sorted(files):
        label = _label(project_dir, yf)
        data, err = _load_yaml(yf)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict):
            result.error(label, "File does not parse to a YAML mapping")
            continue

        kind = data.get("kind")
        if kind not in _EMBEDDING_KINDS:
            result.warn(label, f"Unexpected kind '{kind}' — expected one of {_EMBEDDING_KINDS}")
            continue

        if kind == "EmbeddingConfig":
            name = (data.get("metadata") or {}).get("name")
            if not name:
                result.error(label, "metadata.name is required")
                continue
            known.add(name)
            spec = _v if isinstance(_v := data.get("spec"), dict) else data
            if not spec.get("model"):
                result.warn(label, f"[{name}] spec.model not set")
            continue

        # EmbeddingRegistry: multi-entry format
        block = data.get("spec")
        if not isinstance(block, dict):
            result.error(label, "No 'spec' block")
            continue
        entries = block.get("embeddings") or []
        if not entries:
            result.warn(label, "spec.embeddings is empty")
            continue
        for i, entry in enumerate(entries):
            name = (entry or {}).get("name") if isinstance(entry, dict) else None
            if not name:
                result.error(label, f"spec.embeddings[{i}]: name is required")
                continue
            known.add(name)
            if not entry.get("model"):
                result.warn(label, f"[{name}] spec.embeddings entry has no 'model'")

    return known


def _validate_collections(
    files: list[Path], project_dir: Path, known_embeddings: set[str], result: ValidateResult
) -> set[str]:
    """Kind-recognition + shape check for ``CollectionRegistry``/``CollectionConfig``
    YAMLs (TD-14 — previously unvalidated). Returns known collection names."""
    known: set[str] = set()

    def _check_embedding_ref(embedding: Any, where: str) -> None:
        if embedding and known_embeddings and embedding not in known_embeddings:
            result.warn(
                label, f"{where}: embedding '{embedding}' matches no known EmbeddingRegistry entry"
            )

    for yf in sorted(files):
        label = _label(project_dir, yf)
        data, err = _load_yaml(yf)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict):
            result.error(label, "File does not parse to a YAML mapping")
            continue

        kind = data.get("kind")
        if kind not in _COLLECTION_KINDS:
            result.warn(label, f"Unexpected kind '{kind}' — expected one of {_COLLECTION_KINDS}")
            continue

        if kind == "CollectionConfig":
            name = (data.get("metadata") or {}).get("name")
            if not name:
                result.error(label, "metadata.name is required")
                continue
            known.add(name)
            spec = _v if isinstance(_v := data.get("spec"), dict) else data
            _check_embedding_ref(spec.get("embedding"), f"[{name}]")
            continue

        # CollectionRegistry: multi-entry format
        block = data.get("spec")
        if not isinstance(block, dict):
            result.error(label, "No 'spec' block")
            continue
        entries = block.get("collections") or []
        if not entries:
            result.warn(label, "spec.collections is empty")
            continue
        for i, entry in enumerate(entries):
            name = (entry or {}).get("name") if isinstance(entry, dict) else None
            if not name:
                result.error(label, f"spec.collections[{i}]: name is required")
                continue
            known.add(name)
            _check_embedding_ref(entry.get("embedding"), f"[{name}]")

    return known


def _validate_federation(files: list[Path], project_dir: Path, result: ValidateResult) -> set[str]:
    """Kind-recognition + shape check for ``FederationProvider`` YAMLs
    (TD-14 — previously unvalidated). Returns known provider names."""
    known: set[str] = set()
    for yf in sorted(files):
        label = _label(project_dir, yf)
        data, err = _load_yaml(yf)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict):
            result.error(label, "File does not parse to a YAML mapping")
            continue

        if data.get("kind") != _FEDERATION_KIND:
            result.warn(
                label, f"Unexpected kind '{data.get('kind')}' — expected '{_FEDERATION_KIND}'"
            )
            continue

        meta = data.get("metadata") or {}
        name = meta.get("name")
        if not name:
            result.error(label, "metadata.name is required")
            continue
        known.add(name)

        spec = data.get("spec") or {}
        if not spec.get("provider"):
            result.warn(label, f"[{name}] spec.provider not set")

    return known


def _validate_storage(
    files: list[Path], project_dir: Path, result: ValidateResult
) -> dict[str, Any]:
    """V034: every ``Storage`` file parses into a valid store. Returns the valid stores by name."""
    from tuvl.storage import ConfigError, parse_store

    stores: dict[str, Any] = {}
    for yf in sorted(files):
        label = _label(project_dir, yf)
        data, err = _load_yaml(yf)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict):
            result.error(label, "File does not parse to a YAML mapping")
            continue
        try:
            name, spec = parse_store(data)
        except ConfigError as exc:
            result.issues.append(Issue("error", label, f"[V034] {exc}", "V034"))
            continue
        if name in stores:
            result.error(label, f"duplicate Storage name '{name}'")
            continue
        stores[name] = spec
    return stores


def _agent_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "agent"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                names.append(str(dec.args[0].value))
    return names


def _validate_agents(dirs: dict[str, Path], result: ValidateResult) -> set[str]:
    """Syntax- and import-check ``agents/*.py``. Returns the ``@agent`` names."""
    registered: set[str] = set()
    agents_dir = dirs["agents"]
    if not agents_dir.exists():
        return registered
    for py_file in sorted(agents_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        label = f"agents/{py_file.name}"
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError as exc:
            result.error(label, f"Syntax error at line {exc.lineno}: {exc.msg}")
            continue
        names = _agent_names(tree)
        registered.update(names)
        for name in names:
            if name.startswith("tuvl."):
                result.error(
                    label, f"@agent('{name}'): the 'tuvl.' prefix is reserved for built-ins"
                )
        if len(names) > 1:
            result.warn(
                label,
                f"{len(names)} @agent() decorators in one file ({', '.join(names)}) — keep one "
                f"agent per file so editors and codegen can map files to agents",
            )
        elif names and names[0] != py_file.stem:
            result.warn(
                label,
                f'@agent("{names[0]}") does not match the file name — rename it to {names[0]}.py',
            )

        # Import to catch runtime errors (missing imports, bad code). The
        # project root goes on sys.path so agents._generated imports resolve.
        root = str(agents_dir.parent.resolve())
        added = root not in sys.path
        if added:
            sys.path.insert(0, root)
        module_name = f"_tuvl_validate_agents.{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            from tuvl.runtime.code import AGENT_REGISTRY

            before = dict(AGENT_REGISTRY)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    saved = logging.root.manager.disable
                    logging.disable(logging.WARNING)
                    try:
                        spec.loader.exec_module(module)
                    finally:
                        logging.disable(saved)
            except Exception as exc:
                result.error(label, f"Import error: {exc}")
            finally:
                sys.modules.pop(module_name, None)
                AGENT_REGISTRY.clear()
                AGENT_REGISTRY.update(before)
                if added:
                    sys.path.remove(root)
    return registered


def _validate_artifacts(
    project_dir: Path, result: ValidateResult
) -> dict[str, tuple[str, set[int]]]:
    """Validate the ``artifacts/`` directory. Returns ``{name: (type, versions)}``.

    Prose artifacts are front-matter ``.md``; structured artifacts are
    ``kind: Artifact`` YAML envelopes. Both live under ``artifacts/`` by
    convention (the runtime also accepts structured YAML anywhere, dispatched
    by ``kind:``).
    """
    from tuvl.core.artifacts.loader import read_prose_artifact

    known: dict[str, tuple[str, set[int]]] = {}
    artifacts_dir = project_dir / "artifacts"

    def _record(label: str, name: str, type_: str, version: int) -> None:
        if name in known and known[name][0] != type_:
            result.error(
                label,
                f"artifact '{name}' redeclared with type '{type_}' (was "
                f"'{known[name][0]}') — one type per name",
            )
            return
        known.setdefault(name, (type_, set()))[1].add(version)

    for path in sorted(artifacts_dir.rglob("*.md")):
        label = f"artifacts/{path.relative_to(artifacts_dir)}"
        try:
            meta, _body = read_prose_artifact(path)
        except ValueError as exc:
            result.error(label, str(exc))
            continue
        _record(label, str(meta["name"]), str(meta["type"]), int(meta.get("version", 1)))

    for path in sorted(artifacts_dir.rglob("*.yaml")):
        label = f"artifacts/{path.relative_to(artifacts_dir)}"
        data, err = _load_yaml(path)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict) or data.get("kind") != "Artifact":
            result.warn(label, "Expected kind='Artifact' in artifacts/")
            continue
        spec = data.get("spec")
        if not isinstance(spec, dict):
            result.error(label, "No 'spec' block")
            continue
        name = (data.get("metadata") or {}).get("name")
        type_ = spec.get("type")
        if not name:
            result.error(label, "metadata.name is required")
            continue
        if type_ not in ARTIFACT_TYPES:
            result.error(
                label, f"[{name}] spec.type '{type_}' invalid — one of {sorted(ARTIFACT_TYPES)}"
            )
            continue
        if type_ not in STRUCTURED_TYPES:
            result.error(
                label,
                f"[{name}] prose type '{type_}' is authored as a front-matter .md "
                f"file, not a YAML envelope",
            )
            continue
        if type_ == "hook":
            from tuvl.core.engine.hooks import HOOK_ACTIONS, HOOK_EVENTS

            if spec.get("on") not in HOOK_EVENTS:
                result.error(
                    label,
                    f"[{name}] spec.on '{spec.get('on')}' invalid — one of {sorted(HOOK_EVENTS)}",
                )
            action = spec.get("action", "log")
            if action not in HOOK_ACTIONS:
                result.error(
                    label,
                    f"[{name}] spec.action '{action}' invalid — one of {sorted(HOOK_ACTIONS)}",
                )
            elif action == "notify" and not spec.get("target"):
                result.error(
                    label,
                    f"[{name}] action 'notify' requires 'target' (a declared APICall "
                    f"step id in the attaching workflow)",
                )
        if type_ == "guardrail":
            checks = spec.get("checks") or []
            if not checks:
                result.warn(label, f"[{name}] guardrail has no checks")
            for i, check in enumerate(checks):
                ck = (check or {}).get("check") if isinstance(check, dict) else None
                if ck == "llm_judge":
                    result.error(
                        label,
                        f"[{name}] checks[{i}]: llm_judge is gone in 2.0 — write a "
                        f"'type: judge' artifact and list it under guardrails instead",
                    )
                elif ck not in GUARDRAIL_CHECKS:
                    result.error(
                        label,
                        f"[{name}] checks[{i}].check '{ck}' invalid — one of "
                        f"{sorted(GUARDRAIL_CHECKS)}",
                    )
                elif ck == "regex_deny" and not check.get("patterns"):
                    result.error(label, f"[{name}] checks[{i}]: regex_deny requires 'patterns'")
                elif ck == "max_chars" and not check.get("limit"):
                    result.error(label, f"[{name}] checks[{i}]: max_chars requires 'limit'")
                elif ck == "json_schema" and not isinstance(check.get("schema"), dict):
                    result.error(label, f"[{name}] checks[{i}]: json_schema requires 'schema'")
        version = (data.get("metadata") or {}).get("version", 1)
        _record(label, str(name), str(type_), version if isinstance(version, int) else 1)

    # External sources: sha256 must be pinned; validate stays OFFLINE — a warm
    # cache dir is scanned, a cold one is reported as not-fetched.
    cfg_path = project_dir / "config.yaml"
    cfg_data, _cfg_err = _load_yaml(cfg_path) if cfg_path.exists() else (None, None)
    sources = ((cfg_data or {}).get("spec") or {}).get("artifact_sources") or []
    for i, source in enumerate(sources):
        if not isinstance(source, dict):
            result.error("config.yaml", f"artifact_sources[{i}] must be a mapping")
            continue
        src_name = str(source.get("name") or source.get("url") or f"#{i}")
        if not source.get("url"):
            result.error("config.yaml", f"artifact_sources '{src_name}': url is required")
        sha = str(source.get("sha256") or "").strip().lower()
        if not sha:
            result.error(
                "config.yaml",
                f"artifact_sources '{src_name}': sha256 is REQUIRED — unpinned "
                f"sources are refused at boot",
            )
            continue
        cache_dir = project_dir / ".tuvl" / "artifacts" / "cache" / sha
        if not cache_dir.is_dir():
            result.warn(
                "config.yaml",
                f"artifact_sources '{src_name}': not fetched (cache cold) — refs "
                f"into this source cannot be verified offline",
            )
            continue
        for path in sorted(cache_dir.rglob("*.md")):
            try:
                meta, _body = read_prose_artifact(path)
            except ValueError as exc:
                result.error(f"cache:{src_name}/{path.name}", str(exc))
                continue
            _record(
                f"cache:{src_name}/{path.name}",
                str(meta["name"]),
                str(meta["type"]),
                int(meta.get("version", 1)),
            )
        for path in sorted(cache_dir.rglob("*.yaml")):
            data, err = _load_yaml(path)
            if err or not isinstance(data, dict) or data.get("kind") != "Artifact":
                continue
            spec = data.get("spec") or {}
            a_name = (data.get("metadata") or {}).get("name")
            a_ver = (data.get("metadata") or {}).get("version", 1)
            if a_name and spec.get("type") in STRUCTURED_TYPES:
                _record(
                    f"cache:{src_name}/{path.name}",
                    str(a_name),
                    str(spec["type"]),
                    a_ver if isinstance(a_ver, int) else 1,
                )

    return known


def _validate_v2_documents(
    files: list[Path],
    project_dir: Path,
    result: ValidateResult,
    *,
    model_files: list[Path] | None = None,
    llm_files: list[Path] | None = None,
    stores: dict[str, Any] | None = None,
    known_artifacts: dict[str, tuple[str, set[int]]] | None = None,
    used_artifacts: set[str] | None = None,
    code_agents: set[str] | None = None,
    collections: set[str] | None = None,
) -> None:
    """Validate 2.0 documents: schema shape, then the Appendix B rules
    (graph, signals, data flow, types, expressions, references) for each
    workflow, recording its determinism/cost profile."""
    from tuvl.contract.registry import DocumentLoadError, parse_document
    from tuvl.contract.schema import WorkflowDocument
    from tuvl_insight.validate import Catalog, validate_workflow

    parsed: list[tuple[str, str, object]] = []
    names: dict[tuple[str, str], str] = {}
    for path in sorted(files):
        label = _label(project_dir, path)
        data, err = _load_yaml(path)
        if err:
            result.error(label, err)
            continue
        if not isinstance(data, dict):
            continue
        if data.get("kind") == "Test" and data.get("version") != "tuvl/v2":
            result.error(label, f"{data.get('kind')} documents need 'version: tuvl/v2'")
            continue
        try:
            doc = parse_document(data, label)
        except DocumentLoadError as exc:
            for problem in exc.errors:
                result.issues.append(Issue("error", label, f"[V001] {problem}", "V001"))
            continue
        key = (str(data["kind"]), doc.metadata.name)  # type: ignore[attr-defined]
        if key in names:
            result.error(
                label, f"duplicate {key[0]} name '{key[1]}' — already defined in {names[key]}"
            )
            continue
        names[key] = label
        parsed.append((label, path.read_text(), doc))

    catalog = Catalog(
        artifacts=dict(known_artifacts or {}),
        code_agents=code_agents,
        stores=stores,
        collections=collections,
    )
    model_specs: dict[str, dict[str, Any]] = {}
    for path in model_files or []:
        data, _err = _load_yaml(path)
        if isinstance(data, dict):
            try:
                catalog.add_model(data)
            except Exception:  # malformed models are reported by _validate_models
                continue
    for path in llm_files or []:
        data, _err = _load_yaml(path)
        if isinstance(data, dict) and (data.get("metadata") or {}).get("name"):
            spec = _v if isinstance(_v := data.get("spec"), dict) else {}
            name = str(data["metadata"]["name"])
            catalog.agent_models[name] = str(spec.get("type") or "llm")
            model_specs[name] = dict(spec)
            if spec.get("type") == "decision":
                catalog.decision_models[name] = (str(spec.get("provider")), str(spec.get("model")))
    for *_, parsed_doc in parsed:
        if isinstance(parsed_doc, WorkflowDocument):
            catalog.workflows[parsed_doc.metadata.name] = parsed_doc

    for label, text, item in parsed:
        if not isinstance(item, WorkflowDocument):
            continue
        report = validate_workflow(item, catalog, text=text)
        if used_artifacts is not None:
            used_artifacts.update(_artifact_names(item.model_dump(mode="json")))
        for d in report.diagnostics:
            where = f"{label}:{d.line}" if d.line else label
            message = f"[{d.code}] {d.path}: {d.message}" if d.path else f"[{d.code}] {d.message}"
            result.issues.append(Issue(d.severity, where, message, d.code, d.line))
        result.profiles[item.metadata.name] = {
            "file": label,
            "summary": report.profile.summary(),
            **report.profile.to_json(),
        }
    _check_model_needs(parsed, model_specs, result)


def _check_model_needs(
    parsed: list[tuple[str, str, object]],
    model_specs: dict[str, dict[str, Any]],
    result: ValidateResult,
) -> None:
    """V028: model references resolve; V029: their credentials are declared."""
    from tuvl.contract.schema import WorkflowDocument
    from tuvl.core.models.requirements import project_needs

    docs = {
        doc.metadata.name: (label, doc)
        for label, _, doc in parsed
        if isinstance(doc, WorkflowDocument)
    }
    needs, unknown = project_needs([d for _, d in docs.values()], model_specs)
    for ref, users in unknown:
        for user in users:
            label = docs[user.split(".", 1)[0]][0]
            result.issues.append(
                Issue(
                    "error",
                    label,
                    f"[V028] {user}: unknown AgentModel '{ref}' — add llms/{ref}.yaml or use a "
                    "provider/model id",
                    "V028",
                )
            )
    for need in needs:
        result.models[need.name] = need.to_json(environ={})
        for problem in need.problems:
            result.issues.append(
                Issue("warning", f"model {need.name}", f"[V029] {need.name}: {problem}", "V029")
            )


def _models_line(models: dict[str, dict[str, Any]]) -> str:
    if not models:
        return "none — no LLM or decision model required"
    parts = []
    for name, info in sorted(models.items()):
        env = info.get("env") or []
        parts.append(f"{name} ({info['provider']}" + (f" · {', '.join(env)})" if env else ")"))
    return ", ".join(parts)


def _artifact_names(value: object) -> set[str]:
    """Every artifact name referenced (``artifact://name[@v]``) anywhere in *value*."""
    if isinstance(value, str):
        if value.startswith("artifact://"):
            return {value[len("artifact://") :].partition("@")[0]}
        return set()
    if isinstance(value, dict):
        return set().union(*(_artifact_names(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_artifact_names(v) for v in value)) if value else set()
    return set()


def _validate_cross_references(
    model_files: list[Path],
    project_dir: Path,
    known_models: set[str],
    known_datasources: set[str],
    result: ValidateResult,
) -> None:
    """Check that relation.model and spec.datasource reference known names."""
    for yf in sorted(model_files):
        label = _label(project_dir, yf)
        data, err = _load_yaml(yf)
        if err or not isinstance(data, dict):
            continue
        if data.get("kind") != _MODEL_KIND:
            continue

        meta = data.get("metadata") or {}
        name = meta.get("name", yf.stem)
        spec = data.get("spec") or {}

        for rel in spec.get("relations", []):
            rel_model = rel.get("model", "")
            if rel_model and rel_model not in known_models:
                result.error(
                    label,
                    f"[{name}] relation '{rel.get('name', '?')}' references unknown model '{rel_model}'",
                )

        # Models without spec.datasource follow the primary DataSource; an
        # explicit name that matches nothing means no table is ever created.
        ds_name = spec.get("datasource")
        if ds_name and known_datasources and ds_name not in known_datasources:
            result.warn(
                label,
                f"[{name}] spec.datasource '{ds_name}' matches no DataSource "
                "metadata.name — the model will not be bound and its table "
                "will not be created",
            )


def run_validation(
    project_dir: Path, *, echo: bool = True
) -> tuple[ValidateResult, set[str], set[str], set[str]]:
    """Run every validation pass over a project directory.

    Programmatic entry point shared by ``tuvl lint project`` and ``tuvl ship``.
    Returns the result plus the discovered model/node/llm names so callers
    can render the same report via ``_print_report``.
    """

    def _step(msg: str) -> None:
        if echo:
            console.print(msg)

    # Suppress noisy pydantic_settings / tuvl settings INFO logs that fire on
    # import during validation — but restore global state on the way out, since
    # run_validation is a programmatic API (tuvl ship, embedders) that must not
    # leave warnings/logging muted for the rest of the process.
    _saved_disable = logging.root.manager.disable
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        logging.disable(logging.INFO)
        try:
            result = ValidateResult()

            # TD-14: discover every YAML/YML file in the project the same way
            # config_loader.load_all_configs does — one recursive walk,
            # grouped by kind, independent of directory layout — instead of
            # non-recursively globbing config.yaml's fixed `dirs:`.
            _step("[dim]• Discovering project files…[/dim]")
            by_kind, unrecognized_kinds, unparsable = _discover_by_kind(project_dir)

            # agents/ is Python, not YAML-by-kind — it stays directory-scoped
            # (config.yaml's `dirs.agents` override still applies here).
            dirs = _resolve_dirs(project_dir)

            _step("[dim]• Checking config.yaml…[/dim]")
            _validate_project_config(project_dir, result)
            _validate_system_config(project_dir, result)

            for path, err in unparsable:
                result.error(_label(project_dir, path), err)

            for path, kind in unrecognized_kinds:
                result.error(
                    _label(project_dir, path),
                    f"Unknown kind '{kind}' — not a recognized tuvl resource kind "
                    f"(typo? the runtime will silently skip this file)",
                )

            _step("[dim]• Checking models…[/dim]")
            known_models = _validate_models(by_kind.get(_MODEL_KIND, []), project_dir, result)

            _step("[dim]• Checking datasources…[/dim]")
            known_datasources = _validate_datasources(
                [p for kind in _DS_KINDS for p in by_kind.get(kind, [])], project_dir, result
            )

            _step("[dim]• Checking AgentModel presets (llms)…[/dim]")
            known_llms = _validate_llms(by_kind.get(_LLM_KIND, []), project_dir, result)

            _step("[dim]• Checking embeddings…[/dim]")
            known_embeddings = _validate_embeddings(
                [p for kind in _EMBEDDING_KINDS for p in by_kind.get(kind, [])],
                project_dir,
                result,
            )

            _step("[dim]• Checking collections…[/dim]")
            known_collections = _validate_collections(
                [p for kind in _COLLECTION_KINDS for p in by_kind.get(kind, [])],
                project_dir,
                known_embeddings,
                result,
            )

            _step("[dim]• Checking federation providers…[/dim]")
            _validate_federation(by_kind.get(_FEDERATION_KIND, []), project_dir, result)

            _step("[dim]• Checking storage…[/dim]")
            stores = _validate_storage(by_kind.get(_STORAGE_KIND, []), project_dir, result)

            _step("[dim]• Checking code agents…[/dim]")
            known_agents = _validate_agents(dirs, result)

            _step("[dim]• Checking artifacts…[/dim]")
            known_artifacts = _validate_artifacts(project_dir, result)

            _step("[dim]• Checking model cross-references…[/dim]")
            _validate_cross_references(
                by_kind.get(_MODEL_KIND, []), project_dir, known_models, known_datasources, result
            )

            _step("[dim]• Checking workflows…[/dim]")
            from tuvl.contract.registry import V1_WORKFLOW_MESSAGE, is_v2

            v2_documents: list[Path] = []
            for path in by_kind.get(_WORKFLOW_KIND, []):
                data, _err = _load_yaml(path)
                if isinstance(data, dict) and not is_v2(data):
                    result.error(_label(project_dir, path), f"this file {V1_WORKFLOW_MESSAGE}")
                else:
                    v2_documents.append(path)
            for path in by_kind.get(_V1_TEST_KIND, []):
                result.error(
                    _label(project_dir, path),
                    "1.x 'kind: test' files are not supported in tuvl 2.0 — write a 'kind: Test' "
                    "document (version: tuvl/v2) instead",
                )
            v2_documents += by_kind.get("Test", [])  # task lists: `tuvl lint spec`
            used_artifacts: set[str] = set()
            _validate_v2_documents(
                v2_documents,
                project_dir,
                result,
                model_files=by_kind.get(_MODEL_KIND, []),
                llm_files=by_kind.get(_LLM_KIND, []),
                stores=stores,
                known_artifacts=known_artifacts,
                used_artifacts=used_artifacts,
                code_agents=known_agents,
                collections=known_collections,
            )

            # Dead prose is debt — surface artifacts nothing references.
            for name in sorted(set(known_artifacts) - used_artifacts):
                result.warn(
                    "artifacts/",
                    f"artifact '{name}' is not referenced by any workflow",
                )
        finally:
            logging.disable(_saved_disable)

    return result, known_models, known_agents, known_llms


def validate_cmd(
    project_dir: Path = typer.Option(
        Path("."),
        "--project-dir",
        "-d",
        help="Path to the tuvl project directory (contains config.yaml, models/, workflows/, etc.).",
        exists=True,
        file_okay=False,
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Treat warnings as errors (non-zero exit if any warnings).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print a machine-readable report (for CI and coding agents).",
    ),
) -> None:
    """Validate a tuvl project — YAMLs, code agents, workflow contracts, and more."""
    project_dir = project_dir.resolve()
    if as_json:
        import json

        result, *_ = run_validation(project_dir, echo=False)
        failed = bool(result.errors) or (strict and bool(result.warnings))
        typer.echo(
            json.dumps(
                {
                    "valid": not failed,
                    "errors": [i.to_json() for i in result.errors],
                    "warnings": [i.to_json() for i in result.warnings],
                    "profiles": result.profiles,
                    "models": result.models,
                },
                indent=2,
            )
        )
        if failed:
            raise typer.Exit(1)
        return

    console.print(f"\n[bold]Validating project:[/bold] {project_dir}\n")

    result, known_models, known_agents, known_llms = run_validation(project_dir)

    console.print()

    _print_report(result, project_dir, known_models, known_agents, known_llms)

    has_errors = bool(result.errors)
    has_warnings = bool(result.warnings)

    if has_errors or (strict and has_warnings):
        raise typer.Exit(1)


def _print_report(
    result: ValidateResult,
    project_dir: Path,
    known_models: set[str],
    known_agents: set[str],
    known_llms: set[str],
) -> None:
    errors = result.errors
    warnings = result.warnings

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row(
        "Models detected:",
        f"[cyan]{len(known_models)}[/cyan]  " + ", ".join(sorted(known_models))
        if known_models
        else "[dim]none[/dim]",
    )
    summary.add_row(
        "Code agents:",
        f"[cyan]{len(known_agents)}[/cyan]  " + ", ".join(sorted(known_agents))
        if known_agents
        else "[dim]none[/dim]",
    )
    summary.add_row(
        "LLM presets:",
        f"[cyan]{len(known_llms)}[/cyan]  " + ", ".join(sorted(known_llms))
        if known_llms
        else "[dim]none[/dim]",
    )
    summary.add_row(
        "Errors:", f"[red bold]{len(errors)}[/red bold]" if errors else "[green]0[/green]"
    )
    summary.add_row(
        "Warnings:", f"[yellow]{len(warnings)}[/yellow]" if warnings else "[green]0[/green]"
    )
    console.print(summary)
    console.print()

    if result.profiles:
        for name, profile in sorted(result.profiles.items()):
            console.print(f"  [bold]{name}[/bold]  [dim]{profile['summary']}[/dim]")
        console.print()
    console.print(f"  [bold]needs models:[/bold] {_models_line(result.models)}")
    console.print()

    if errors or warnings:
        table = Table(
            show_header=True,
            header_style="bold",
            border_style="dim",
            show_lines=True,
            expand=True,
        )
        table.add_column("Sev", width=5, no_wrap=True)
        table.add_column("File", min_width=20, no_wrap=True)
        table.add_column("Issue")

        for issue in result.issues:
            if issue.severity == "error":
                sev = Text("ERR", style="bold red")
            else:
                sev = Text("WARN", style="yellow")
            table.add_row(sev, Text(issue.file), Text(issue.message))

        console.print(table)
        console.print()

    console.print(Rule())
    if errors:
        console.print(
            f"[red bold]✗ Validation FAILED:[/red bold] {len(errors)} error(s), {len(warnings)} warning(s)"
        )
    elif warnings:
        console.print(f"[yellow]⚠ Validation passed with {len(warnings)} warning(s)[/yellow]")
    else:
        console.print("[green bold]✓ Validation passed — project looks good![/green bold]")
    console.print()
