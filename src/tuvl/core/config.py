import os
from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

import yaml
from dotenv import load_dotenv
from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tuvl.core.logger import logger

# The project directory is set by the CLI via TUVL_PROJECT_DIR.
# Falls back to CWD so the engine still works when run directly.
_PROJECT_DIR = Path(os.environ.get("TUVL_PROJECT_DIR", ".")).resolve()

# Load .env from the project directory before Settings is instantiated.
load_dotenv(_PROJECT_DIR / ".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_DIR / ".env"),
        env_file_encoding="utf-8",
        secrets_dir="/run/secrets" if Path("/run/secrets").is_dir() else None,
        extra="ignore",
    )

    # ── General ───────────────────────────────────────────────────────────────
    app_name: str = "tuvl"
    debug: bool = False

    # ── Project directory (set by CLI) ────────────────────────────────────────
    tuvl_project_dir: str = str(_PROJECT_DIR)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def project_dir(self) -> Path:
        return Path(self.tuvl_project_dir).resolve()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def configs_dir(self) -> Path:
        return self.project_dir

    # ── Per-kind directory resolution (reads config.yaml if present) ──────────
    #   Default mapping (matches tuvl init scaffold):
    _DEFAULT_DIRS: dict[str, str] = {
        "models": "models",
        "workflows": "workflows",
        "datasources": "datasources",
        "llms": "llms",
        "nodes": "nodes",
        "federation": "federation",
        "storage": "storage",
        "artifacts": "artifacts",
        # embeddings.yaml and collections.yaml live inside the models dir
        "embeddings": "models",
        "collections": "models",
    }

    @cached_property
    def _project_config(self) -> dict:
        """Lazily parse config.yaml from the project root. Empty dict if absent."""
        cfg_path = self.project_dir / "config.yaml"
        if not cfg_path.exists():
            return {}
        try:
            with cfg_path.open() as f:
                data = yaml.safe_load(f) or {}
            if data.get("kind") != "ProjectConfig":
                logger.warning(
                    "config.yaml has unexpected kind, expected ProjectConfig, ignoring",
                    kind=data.get("kind"),
                )
                return {}
            logger.debug("Project config loaded", path=str(cfg_path))
            return data
        except Exception as exc:
            logger.warning("Could not parse config.yaml", error=exc)
            return {}

    def dir_for(self, kind: str) -> Path:
        """Return the absolute directory path for a config kind.

        Resolution order:
        1. ``dirs.<kind>`` in config.yaml (relative paths resolved against project_dir)
        2. Built-in default (``<project_dir>/<kind>``)
        """
        dirs = self._project_config.get("dirs", {})
        rel = dirs.get(kind) or self._DEFAULT_DIRS.get(kind, kind)
        return (self.project_dir / rel).resolve()

    @property
    def vector_datasource(self) -> str:
        """Name of the PostgreSQL datasource used for vector storage.

        Reads ``spec.vector_datasource`` from config.yaml.  Empty string means
        "use the primary datasource" (``metadata.primary: true``).
        """
        return str(self._project_config.get("spec", {}).get("vector_datasource", ""))

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "tuvl"
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"

    db_echo: bool = False
    db_pool_min: int = 5
    db_pool_max: int = 20

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pg_database_url_rw(self) -> str:
        pw = quote_plus(self.postgres_password)
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pg_database_url_ro(self) -> str:
        pw = quote_plus(self.postgres_password)
        host = self.postgres_ro_host or self.postgres_host
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{host}:{self.postgres_port}/{self.postgres_db}"
        )

    postgres_ro_host: str | None = None

    # ── Embeddings ────────────────────────────────────────────────────────────
    # Default embedding model used when a collection does not specify one.
    # A ``provider/model`` id of an OpenAI-compatible embeddings endpoint.
    tuvl_default_embedding: str = "openai/text-embedding-3-small"
    tuvl_default_embedding_dimensions: int = 1536

    # ── LLM ───────────────────────────────────────────────────────────────────
    llm_default_model: str = "ollama/llama3"
    ollama_base_url: str = "http://localhost:11434"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    # ── Token / Auth ──────────────────────────────────────────────────────────
    # Access-token lifetime in seconds.  Set to 0 to disable expiry (not
    # recommended for production).  Default: 24 hours.
    tuvl_token_ttl_seconds: int = 86400

    # First administrator for a fresh production database (see ``tuvl.core.auth.seed``).
    # Used once: ignored when the IAM tables already hold a user or a role.
    tuvl_bootstrap_admin_email: str | None = None
    tuvl_bootstrap_admin_password: str | None = None
    tuvl_bootstrap_admin_password_file: str | None = None

    # ── Multi-tenancy safety net ──────────────────────────────────────────
    # When the deployment mode is multi_tenant (locked via ``tuvl init
    # --multi-tenant``) and this flag is true, any request that reaches
    # ``get_session`` without ``current_tenant_id`` set raises HTTP 500.
    # This is the host application's protection against forgetting to wire
    # the tenant-extraction dependency on a new route.
    # Auto-defaults to ``true`` whenever multi_tenant mode is active; set
    # explicitly to ``false`` to opt out (not recommended).
    tuvl_require_tenant_context: bool = True

    # ── Testing / LLM-as-a-Judge ──────────────────────────────────────────
    # Default judge model used by ``tuvl run tests`` when a test case does not
    # specify its own ``judge_model``.  Override with TUVL_TEST_JUDGE env var.
    # Empty string means "no judge configured" — evaluations without a
    # per-evaluation judge_model will be skipped rather than calling an LLM.
    tuvl_test_judge: str = ""

    # ── Loop agents ───────────────────────────────────────────────────────
    # Timeout for one supervisor LLM-judge verdict. Without it the client's
    # default (600s) applies and a hung judge stalls supervision.
    tuvl_agent_judge_timeout_s: float = 30.0
    # Ceiling for a single loop tool call — a hung tool otherwise stalls the
    # loop, since control requests land between calls. 0 disables it.
    tuvl_agent_tool_timeout_s: float = 300.0

    # ── Telemetry (OpenTelemetry) ─────────────────────────────────────────
    # Set to true to enable OTLP span export in production mode.
    # Has no effect in dev mode (TUVL_DEV_MODE=true).
    tuvl_telemetry_enabled: bool = True

    # Runtime environment — controls log renderer:
    #   "development" → structlog ConsoleRenderer (coloured, human-readable)
    #   "production"  → structlog JSONRenderer (strict JSONL, one line per event)
    tuvl_env: str = "production"

    # Token revocation, OAuth state, and any other cross-worker shared state
    # require Redis in multi-worker deployments — without it, ``token_blacklist``
    # falls back to per-process memory and revocations don't propagate.  Set
    # this to ``true`` in production to fail the boot if a configured Redis
    # datasource is unreachable, instead of silently degrading.
    tuvl_require_redis: bool = False

    # In-process cron scheduler for workflows with ``trigger.schedule``.
    # Single-fire across workers is enforced via Postgres advisory locks.
    tuvl_scheduler_enabled: bool = True
    #: Seconds the worker waits for running runs at shutdown before interrupting them.
    tuvl_shutdown_drain_seconds: float = 20.0
    #: Seconds an unused MCP session of a ``tool`` agent (``use: mcp``) stays open for reuse;
    #: 0 opens a new session for every call.
    tuvl_mcp_idle_seconds: float = 300.0
    #: Deliver the notifications workflows declare in ``spec.webhooks`` (off unless turned on).
    tuvl_webhooks_enabled: bool = False
    #: What this process does with 2.0 runs: serve HTTP and execute (all),
    #: serve HTTP and run synchronous requests only (api), or poll the queue
    #: without HTTP triggers mattering (worker).
    tuvl_role: Literal["all", "api", "worker"] = "all"
    tuvl_worker_concurrency: int = 8

    # OTLP collector endpoint (Jaeger, Grafana Tempo, etc.). The default protocol
    # is OTLP over HTTP (port 4318); "grpc" needs the ``tuvl[otel-grpc]`` extra.
    # Accepts the OTel standard env vars OTEL_EXPORTER_OTLP_ENDPOINT and
    # OTEL_EXPORTER_OTLP_PROTOCOL *and* the TUVL_OTLP_* aliases.
    tuvl_otlp_endpoint: str = "http://localhost:4318"
    otel_exporter_otlp_endpoint: str = ""  # standard OTel env var alias
    tuvl_otlp_protocol: str = "http/protobuf"
    otel_exporter_otlp_protocol: str = ""  # standard OTel env var alias

    @computed_field  # type: ignore[prop-decorator]
    @property
    def otlp_endpoint(self) -> str:
        """Resolved OTLP endpoint — standard OTel env var takes precedence."""
        return self.otel_exporter_otlp_endpoint or self.tuvl_otlp_endpoint

    @computed_field  # type: ignore[prop-decorator]
    @property
    def otlp_protocol(self) -> str:
        """Resolved OTLP protocol — standard OTel env var takes precedence."""
        return self.otel_exporter_otlp_protocol or self.tuvl_otlp_protocol

    # Service name attached to every span.  Useful when multiple tuvl
    # instances report to the same collector.
    tuvl_service_name: str = "tuvl"

    # ── Dev mode (set only by tuvl CLI, never in .env) ────────────────────
    tuvl_dev_mode: bool = False
    tuvl_dev_api_key: str = ""
    tuvl_dev_port: int = 8885
    dev_session_file: str = ".tuvl/.dev-session"

    # ── Access control ────────────────────────────────────────────────────────
    # Overrides spec.api.expose_model_crud in .tuvl/system.yaml when set;
    # unset (None) defers to the YAML. Ops kill-switch for the /models/* CRUD API.
    tuvl_expose_model_crud: bool | None = None
    # Override spec.api.expose / spec.api.docs in .tuvl/system.yaml when set.
    # TUVL_API_EXPOSE is a comma-separated list of system route groups to mount
    # (auth, runs, admin, artifacts, system); TUVL_API_DOCS is off | workflows | all.
    tuvl_api_expose: str | None = None
    tuvl_api_docs: str | None = None
    # Browser origins allowed to call the API (comma-separated, e.g. "https://app.example.com").
    # Overrides spec.api.cors_origins; unset defers to the YAML, and none means no CORS headers.
    tuvl_cors_origins: str | None = None
    # When false the dev server mounts only what a production server would, instead of every
    # system route group. Overrides spec.api.dev_expose_all; unset defers to the YAML (default true).
    tuvl_dev_expose_all: bool | None = None
    # Comma-separated IPs / CIDR ranges permitted to access the engine remotely.
    # Localhost (127.0.0.0/8, ::1) is always allowed regardless of this value.
    # Example: "192.168.1.0/24,10.0.0.5"
    tuvl_allowed_hosts: str = ""
    # ── OAuth2 / Federation ─────────────────────────────────────────
    # Public base URL used to construct OAuth2 redirect_uri values.
    # Must match what you register in the provider's console.
    tuvl_oauth_base_url: str = "http://localhost:8885"

    # After a successful OAuth2 login the server will redirect the browser to
    # this URL with ?token=<jwt> appended.  When unset the callback
    # returns raw JSON (useful for server-side / CLI flows).
    tuvl_oauth_ui_redirect_url: str = ""

    # Google OAuth2
    tuvl_oauth_google_client_id: str = ""
    tuvl_oauth_google_client_secret: str = ""

    # GitHub OAuth2
    tuvl_oauth_github_client_id: str = ""
    tuvl_oauth_github_client_secret: str = ""

    # Microsoft / Azure AD OAuth2
    tuvl_oauth_microsoft_client_id: str = ""
    tuvl_oauth_microsoft_client_secret: str = ""
    # Tenant: "common" (any MS account), "organizations", or a specific tenant UUID.
    tuvl_oauth_microsoft_tenant_id: str = "common"


settings = Settings()


def use_project(project_dir: Path | str) -> None:
    """Point this process at *project_dir*: its ``.env``, the environment and
    the live ``settings`` (created at import, so setting the variable alone is
    too late)."""
    from dotenv import load_dotenv

    path = str(Path(project_dir).resolve())
    load_dotenv(Path(path) / ".env", override=False)  # the project's own variables; real env wins
    os.environ["TUVL_PROJECT_DIR"] = path
    settings.tuvl_project_dir = path
    settings.__dict__.pop("_project_config", None)
