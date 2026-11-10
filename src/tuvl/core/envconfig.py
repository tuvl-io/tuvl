"""The environment a project declares: the engine's own variables and ``kind: EnvConfig``.

Two things say what the engine reads from its environment:

* the engine's own variables (``TUVL_*``, ``POSTGRES_*``), listed in :data:`ENGINE_CATALOG`;
* the project's ``EnvConfig`` documents, which declare its own variables with a description and
  whether they are secret or required.

A production server refuses to start while a variable declared ``required`` is missing
(:func:`check_at_boot`). Scanning a project for ``${VAR}`` references, the inventory that Insight shows, the
``.env.example`` that ``tuvl ship`` writes and writing ``.env`` are tooling: see
:mod:`tuvl_insight.envconfig_tools`.

A secret is declared, never given a value in YAML: it has a name and metadata only, and its value
lives in ``.env`` locally and in the Secret in the cluster. A non-secret may carry a ``default``.
The order of resolution stays the one the engine already uses: real environment, then ``.env``,
then the declared default.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tuvl.core.logger import logger

ENV_CONFIG_KIND = "EnvConfig"


VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        if not isinstance(name, str) or not VARIABLE_NAME.match(name):
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


def yaml_files(root: Path) -> Iterator[Path]:
    """The project's YAML documents, without the `deploy/` directory that `tuvl ship` writes."""
    from tuvl.core.config_loader import _walk_yaml_files

    for path in _walk_yaml_files(root):
        if "deploy" not in path.relative_to(root).parts[:-1]:
            yield path


def load_manifest(project_dir: Path) -> dict[str, EnvVar]:
    """The variables the project's ``EnvConfig`` files declare, read from disk now.

    Unlike :data:`MANIFEST` (filled once at boot) this follows edits made while the server runs;
    a malformed or duplicated declaration is left out here and reported by ``tuvl lint project``.
    """
    root = Path(project_dir).resolve()
    out: dict[str, EnvVar] = {}
    for path in yaml_files(root):
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


def dotenv_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".env"


def read_dotenv(project_dir: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    path = dotenv_path(project_dir)
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


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
