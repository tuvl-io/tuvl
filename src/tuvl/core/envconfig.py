"""The environment a project needs: the engine's own variables, ``kind: EnvConfig`` and ``${VAR}`` references.

Three things say what a project reads from its environment, and until now nothing joined them:

* the engine's own variables (``TUVL_*``, ``POSTGRES_*``), listed in :data:`ENGINE_CATALOG`;
* the project's ``EnvConfig`` documents, which declare its own variables with a description and
  whether they are secret or required;
* the ``${VAR}`` references scattered through its YAML (and ``os.environ`` reads in its Python).

:func:`inventory` merges them. Insight shows it, ``tuvl ship`` writes ``.env.example`` and the Helm
values from it, and a production server refuses to start while a variable declared ``required`` is
missing.

A secret is declared, never given a value in YAML: it has a name and metadata only, and its value
lives in ``.env`` locally and in the Secret in the cluster. A non-secret may carry a ``default``.
The order of resolution stays the one the engine already uses: real environment, then ``.env``,
then the declared default.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tuvl.core.logger import logger

ENV_CONFIG_KIND = "EnvConfig"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:[^}]*)?\}")
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE)", re.IGNORECASE)
#: kinds whose documents are development material or describe the variables themselves
_SCAN_SKIP_KINDS = frozenset({ENV_CONFIG_KIND, "Test", "TaskPlan", "Idea", "IdeaIndex", "test"})
_PY_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "tests", "specs", "deploy", "dist"}
)


@dataclass(frozen=True)
class EnvVar:
    name: str
    description: str = ""
    secret: bool = False
    required: bool = False
    default: str | None = None
    example: str | None = None
    group: str = "project"
    #: read once, on the first boot of a database (the value does nothing afterwards)
    first_boot: bool = False


def _v(name: str, description: str, **kw: Any) -> tuple[str, EnvVar]:
    return name, EnvVar(name, description, group=kw.pop("group", "engine"), **kw)


#: Variables the engine itself reads. Dev-only ones (``TUVL_DEV_*``) are set by the CLI and left out.
ENGINE_CATALOG: dict[str, EnvVar] = dict(
    [
        _v(
            "TUVL_ENV",
            "`development` for readable logs, `production` for JSON logs and strict boot checks.",
            default="production",
        ),
        _v(
            "TUVL_SIGNING_KEY",
            "Ed25519 private key (64 hex characters) that signs access tokens. Make one with `tuvl generate key`.",
            secret=True,
            required=True,
        ),
        _v(
            "TUVL_TOKEN_TTL_SECONDS",
            "Access-token lifetime in seconds; 0 disables expiry (not recommended).",
            default="86400",
        ),
        _v(
            "TUVL_BOOTSTRAP_ADMIN_EMAIL",
            "Email of the first administrator, created on the first boot of an empty IAM database.",
            first_boot=True,
            group="iam",
        ),
        _v(
            "TUVL_BOOTSTRAP_ADMIN_PASSWORD",
            "Password of the first administrator. Prefer the `_FILE` variant with a mounted secret.",
            secret=True,
            first_boot=True,
            group="iam",
        ),
        _v(
            "TUVL_BOOTSTRAP_ADMIN_PASSWORD_FILE",
            "Path of a file holding the first administrator's password; wins over the plain variable.",
            first_boot=True,
            group="iam",
        ),
        _v("POSTGRES_HOST", "PostgreSQL host.", group="database", default="localhost"),
        _v("POSTGRES_PORT", "PostgreSQL port.", group="database", default="5432"),
        _v("POSTGRES_DB", "PostgreSQL database name.", group="database", default="tuvl"),
        _v("POSTGRES_USER", "PostgreSQL user.", group="database", default="postgres"),
        _v("POSTGRES_PASSWORD", "PostgreSQL password.", secret=True, group="database"),
        _v(
            "POSTGRES_RO_HOST",
            "Read-replica host; reads use the primary when unset.",
            group="database",
        ),
        _v(
            "TUVL_REQUIRE_REDIS",
            "Refuse to start when a configured Redis is unreachable, instead of degrading to per-process state.",
            default="false",
        ),
        _v(
            "TUVL_REQUIRE_TENANT_CONTEXT",
            "In multi-tenant mode, fail a request that reaches the database without a tenant.",
            default="true",
        ),
        _v(
            "TUVL_ROLE",
            "What this process does with runs: `all`, `api` (HTTP and synchronous runs only) or `worker` (queue only).",
            default="all",
        ),
        _v("TUVL_WORKER_CONCURRENCY", "Runs one worker executes at once.", default="8"),
        _v(
            "TUVL_SCHEDULER_ENABLED",
            "Run the cron scheduler for workflows with `trigger.schedule`.",
            default="true",
        ),
        _v(
            "TUVL_WEBHOOKS_ENABLED",
            "Deliver the notifications workflows declare in `spec.webhooks`.",
            default="false",
        ),
        _v(
            "TUVL_SHUTDOWN_DRAIN_SECONDS",
            "Seconds the worker waits for running runs at shutdown.",
            default="20.0",
        ),
        _v(
            "TUVL_MCP_IDLE_SECONDS",
            "Seconds an unused MCP session stays open for reuse; 0 opens one per call.",
            default="300.0",
        ),
        _v(
            "TUVL_ALLOWED_HOSTS",
            "Comma-separated IPs or CIDR ranges allowed to reach the engine remotely (localhost is always allowed).",
            example="10.0.0.0/8,192.168.1.5",
        ),
        _v(
            "TUVL_CORS_ORIGINS",
            "Comma-separated browser origins allowed to call the API, as `scheme://host[:port]`; none sends no CORS headers.",
            example="https://app.example.com",
        ),
        _v(
            "TUVL_API_EXPOSE",
            "Comma-separated system route groups to mount: auth, iam, admin, artifacts, system. Overrides `spec.api.expose`.",
        ),
        _v(
            "TUVL_API_DOCS",
            "What /docs shows: `off`, `workflows` or `all`. Overrides `spec.api.docs`.",
        ),
        _v(
            "TUVL_EXPOSE_MODEL_CRUD",
            "Mount the generated /models routes. Overrides `spec.api.expose_model_crud`.",
        ),
        _v("TUVL_TELEMETRY_ENABLED", "Export OpenTelemetry spans in production.", default="true"),
        _v("TUVL_OTLP_ENDPOINT", "OTLP collector endpoint.", default="http://localhost:4318"),
        _v(
            "TUVL_OTLP_PROTOCOL",
            "OTLP protocol: `http/protobuf` or `grpc` (needs the `otel-grpc` extra).",
            default="http/protobuf",
        ),
        _v(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "Standard OpenTelemetry endpoint; wins over TUVL_OTLP_ENDPOINT.",
        ),
        _v(
            "OTEL_EXPORTER_OTLP_PROTOCOL",
            "Standard OpenTelemetry protocol; wins over TUVL_OTLP_PROTOCOL.",
        ),
        _v("TUVL_SERVICE_NAME", "Service name attached to every span.", default="tuvl"),
        _v(
            "TUVL_DEFAULT_EMBEDDING",
            "Embedding model for a collection that names none, as `provider/model`.",
            default="openai/text-embedding-3-small",
            group="models",
        ),
        _v(
            "TUVL_DEFAULT_EMBEDDING_DIMENSIONS",
            "Vector size of that embedding model.",
            default="1536",
            group="models",
        ),
        _v(
            "LLM_DEFAULT_MODEL",
            "Model used when a workflow names none.",
            default="ollama/llama3",
            group="models",
        ),
        _v(
            "OLLAMA_BASE_URL",
            "Ollama server URL.",
            default="http://localhost:11434",
            group="models",
        ),
        _v("OPENAI_API_KEY", "OpenAI API key.", secret=True, group="models"),
        _v("ANTHROPIC_API_KEY", "Anthropic API key.", secret=True, group="models"),
        _v(
            "TUVL_AGENT_TOOL_TIMEOUT_S",
            "Ceiling in seconds for one loop tool call; 0 disables it.",
            default="300.0",
        ),
        _v(
            "TUVL_AGENT_JUDGE_TIMEOUT_S",
            "Timeout in seconds for one supervisor judge verdict.",
            default="30.0",
        ),
        _v(
            "TUVL_OAUTH_BASE_URL",
            "Public base URL used to build OAuth2 redirect URIs.",
            default="http://localhost:8885",
            group="federation",
        ),
        _v(
            "TUVL_OAUTH_UI_REDIRECT_URL",
            "Where the browser goes after an OAuth2 login, with `?token=` appended.",
            group="federation",
        ),
    ]
)


# ── the project's declarations ────────────────────────────────────────────────

#: variables declared by ``EnvConfig`` documents; cleared and refilled by ``load_all_configs``
MANIFEST: dict[str, EnvVar] = {}

_FIELDS = {"name", "description", "secret", "required", "default", "example"}


def parse_env_config(data: dict[str, Any]) -> list[EnvVar]:
    """The variables of one ``EnvConfig`` document; ``ValueError`` says what is wrong."""
    spec = data.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a mapping with a `variables` list")
    extra = set(spec) - {"variables"}
    if extra:
        raise ValueError(f"unknown spec field(s): {', '.join(sorted(extra))}")
    items = spec.get("variables")
    if not isinstance(items, list) or not items:
        raise ValueError("spec.variables must be a non-empty list")
    out: list[EnvVar] = []
    seen: set[str] = set()
    for i, raw in enumerate(items):
        where = f"spec.variables[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be a mapping with a `name`")
        unknown = set(raw) - _FIELDS
        if unknown:
            raise ValueError(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError(
                f"{where}.name must be an environment variable name such as MY_API_KEY"
            )
        if name in seen:
            raise ValueError(f"{where}: '{name}' is declared twice")
        seen.add(name)
        for flag in ("secret", "required"):
            if flag in raw and not isinstance(raw[flag], bool):
                raise ValueError(f"{where}.{flag} must be true or false")
        for text in ("description", "default", "example"):
            if raw.get(text) is not None and not isinstance(raw[text], (str, int, float, bool)):
                raise ValueError(f"{where}.{text} must be a scalar")
        default = None if raw.get("default") is None else str(raw["default"])
        if raw.get("secret") and default is not None:
            raise ValueError(
                f"{where}: '{name}' is secret and has a default; a secret is declared without a "
                "value (its value lives in .env or the cluster Secret)"
            )
        if raw.get("required") and default is not None:
            raise ValueError(f"{where}: '{name}' is required and has a default; pick one")
        example = raw.get("example")
        out.append(
            EnvVar(
                name=name,
                description=str(raw.get("description") or ""),
                secret=bool(raw.get("secret", False)),
                required=bool(raw.get("required", False)),
                default=default,
                example=None if example is None else str(example),
            )
        )
    return out


def register_env_config_from_dict(data: dict[str, Any], filename: str = "<unknown>") -> None:
    try:
        variables = parse_env_config(data)
    except ValueError as exc:
        raise ValueError(f"{filename}: {exc}") from exc
    for var in variables:
        if var.name in MANIFEST:
            raise ValueError(f"{filename}: '{var.name}' is already declared by another EnvConfig")
        MANIFEST[var.name] = var


def load_manifest(project_dir: Path) -> dict[str, EnvVar]:
    """The variables the project's ``EnvConfig`` files declare, read from disk now.

    Unlike :data:`MANIFEST` (filled once at boot) this follows edits made while the server runs;
    a malformed or duplicated declaration is left out here and reported by ``tuvl lint project``.
    """
    root = Path(project_dir).resolve()
    out: dict[str, EnvVar] = {}
    for path in _yaml_files(root):
        try:
            documents = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        for document in documents:
            if not isinstance(document, dict) or document.get("kind") != ENV_CONFIG_KIND:
                continue
            try:
                variables = parse_env_config(document)
            except ValueError:
                continue
            for var in variables:
                out.setdefault(var.name, var)
    return out


# ── what the project references ───────────────────────────────────────────────


@dataclass
class Reference:
    name: str
    #: every reference names a default (`${VAR:default}`), so the variable may stay unset
    all_have_default: bool = True
    files: list[str] = field(default_factory=list)

    def add(self, file: str, has_default: bool) -> None:
        self.all_have_default = self.all_have_default and has_default
        if file not in self.files:
            self.files.append(file)


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _yaml_files(root: Path) -> Iterator[Path]:
    from tuvl.core.config_loader import _walk_yaml_files

    for path in _walk_yaml_files(root):
        if "deploy" not in path.relative_to(root).parts[:-1]:
            yield path


def _python_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _PY_SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _env_reads(tree: ast.AST) -> Iterator[str]:
    """Names read with a literal key: ``os.environ["X"]``, ``os.environ.get("X")``, ``os.getenv("X")``."""
    for node in ast.walk(tree):
        key: ast.expr | None = None
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            key = node.slice
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("get", "getenv")
                and (
                    _is_environ(func.value)
                    or (func.attr == "getenv" and _is_name(func.value, "os"))
                )
            ):
                key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and _NAME.match(key.value):
            yield key.value


def _is_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" and _is_name(node.value, "os")


def scan_references(project_dir: Path) -> dict[str, Reference]:
    """Every environment variable the project's YAML and Python refer to, with where."""
    root = Path(project_dir).resolve()
    found: dict[str, Reference] = {}

    def note(name: str, rel: str, has_default: bool) -> None:
        found.setdefault(name, Reference(name)).add(rel, has_default)

    for path in _yaml_files(root):
        try:
            documents = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(root))
        for document in documents:
            if isinstance(document, dict) and document.get("kind") in _SCAN_SKIP_KINDS:
                continue
            for text in _strings(document):
                for match in _REF.finditer(text):
                    note(match.group(1), rel, match.group(2) is not None)
    for path in _python_files(root):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, OSError, UnicodeDecodeError, ValueError):
            continue
        rel = str(path.relative_to(root))
        for name in _env_reads(tree):
            # a literal read without a default is a requirement; with one it cannot be told apart here
            note(name, rel, True)
    return found


# ── the inventory ─────────────────────────────────────────────────────────────


@dataclass
class EnvEntry:
    var: EnvVar
    #: "engine" (the catalog), "project" (an EnvConfig), or "referenced" (found in the project only)
    source: str
    referenced_by: list[str]
    is_set: bool
    #: the value of a non-secret variable when set; never a secret's
    value: str | None = None
    #: a `${VAR}` with no default points at it, so the project cannot run without it
    needed: bool = False


def dotenv_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".env"


def read_dotenv(project_dir: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    path = dotenv_path(project_dir)
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def inventory(
    project_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    manifest: Mapping[str, EnvVar] | None = None,
) -> list[EnvEntry]:
    """Engine catalog, project declarations and project references, merged by variable name.

    The declarations are read from the project's files unless *manifest* is given.
    """
    root = Path(project_dir).resolve()
    manifest = load_manifest(root) if manifest is None else manifest
    environ = os.environ if environ is None else environ
    dotenv = read_dotenv(root)
    refs = scan_references(root)

    entries: list[EnvEntry] = []
    names = list(
        dict.fromkeys(
            [*manifest, *(n for n in refs if n not in manifest and n not in ENGINE_CATALOG)]
        )
    )
    # engine variables appear when the project refers to one or has set one
    names += [
        n for n in ENGINE_CATALOG if n not in names and (n in refs or n in environ or n in dotenv)
    ]
    for name in names:
        if name in manifest:
            var, source = manifest[name], "project"
        elif name in ENGINE_CATALOG:
            var, source = ENGINE_CATALOG[name], "engine"
        else:
            var = EnvVar(name, secret=bool(_SECRET_NAME.search(name)))
            source = "referenced"
        raw = environ.get(name, dotenv.get(name))
        ref = refs.get(name)
        entries.append(
            EnvEntry(
                var=var,
                source=source,
                referenced_by=ref.files if ref else [],
                is_set=raw is not None and raw != "",
                value=None if var.secret or not raw else raw,
                needed=bool(ref and not ref.all_have_default),
            )
        )
    return entries


def undeclared_references(project_dir: Path) -> dict[str, Reference]:
    """References to variables neither an ``EnvConfig`` nor the engine catalog declares."""
    manifest = load_manifest(project_dir)
    return {
        name: ref
        for name, ref in scan_references(project_dir).items()
        if name not in manifest and name not in ENGINE_CATALOG
    }


def unused_declarations(project_dir: Path) -> list[str]:
    """Manifest variables nothing in the project refers to (the engine reads its own)."""
    refs = scan_references(project_dir)
    return [
        name
        for name in load_manifest(project_dir)
        if name not in refs and name not in ENGINE_CATALOG
    ]


# ── boot and ship ─────────────────────────────────────────────────────────────


class EnvConfigError(RuntimeError):
    """A production server was started without a variable the project declared required."""


def missing_required(environ: Mapping[str, str] | None = None) -> list[str]:
    environ = os.environ if environ is None else environ
    return [
        name
        for name, var in MANIFEST.items()
        if var.required and var.default is None and not environ.get(name)
    ]


def check_at_boot(*, production: bool, environ: Mapping[str, str] | None = None) -> None:
    """Production refuses to start without a variable the project declared `required`; development warns."""
    missing = missing_required(environ)
    if not missing:
        return
    if production:
        raise EnvConfigError(
            "refusing to start — required environment variable(s) not set: "
            + ", ".join(missing)
            + " (declared by the project's EnvConfig)"
        )
    for name in missing:
        logger.warning("env.required_missing", variable=name)


def ship_variables(project_dir: Path) -> tuple[list[EnvEntry], list[EnvEntry]]:
    """``(secrets, plain)`` entries the deployment must supply, for the Secret and the Helm values.

    Secrets are the declared secrets and the referenced variables that look like one, plus the
    engine's signing key and database password. Plain ones are those with a declared default or an
    example; they go in the chart values.
    """
    root = Path(project_dir)
    entries = {e.var.name: e for e in inventory(root, environ={})}
    for name in ("TUVL_SIGNING_KEY", "POSTGRES_PASSWORD"):
        entries.setdefault(name, EnvEntry(ENGINE_CATALOG[name], "engine", [], False))
    secrets = [e for e in entries.values() if e.var.secret]
    plain = [
        e
        for e in entries.values()
        if not e.var.secret and e.source == "project" and e.var.default is not None
    ]
    return secrets, plain


# ── writing .env (Insight, development) ───────────────────────────────────────


def gitignored(project_dir: Path) -> bool:
    """Whether the project's `.gitignore` lists `.env` (a plain line check, not git itself)."""
    path = Path(project_dir) / ".gitignore"
    try:
        lines = {ln.strip() for ln in path.read_text().splitlines()}
    except OSError:
        return False
    return bool(lines & {".env", "/.env", ".env*", "*.env", ".env.*"}) or any(
        ln.startswith(".env") and not ln.startswith(".env.") for ln in lines
    )


def write_dotenv(project_dir: Path, updates: Mapping[str, str | None]) -> None:
    """Set (a string) or remove (``None``) variables in the project's `.env`, keeping the rest.

    The file is created owner-readable only; the project's other lines and comments are untouched.
    """
    from dotenv import set_key, unset_key

    path = dotenv_path(project_dir)
    if not path.exists():
        path.touch(mode=0o600)
    for name, value in updates.items():
        if not _NAME.match(name):
            raise ValueError(f"'{name}' is not an environment variable name")
        if value is None:
            unset_key(path, name, quote_mode="never")
        else:
            if "\n" in value or "\r" in value:
                raise ValueError(f"the value of {name} must be one line")
            set_key(path, name, value, quote_mode="auto")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
