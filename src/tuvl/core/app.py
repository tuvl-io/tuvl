import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import version as pkg_version
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from tuvl.api.runs import router as runs_router
from tuvl.core.api.admin_router import router as admin_router
from tuvl.core.api.artifacts_router import router as artifacts_router
from tuvl.core.api.crud_router import build_crud_routers
from tuvl.core.api.system_router import require_dev_mode
from tuvl.core.api.system_router import router as system_router
from tuvl.core.auth.router import router as auth_router
from tuvl.core.bootstrap import run_pre_flight
from tuvl.core.config import settings
from tuvl.core.config_loader import load_all_configs
from tuvl.core.cors import install_cors
from tuvl.core.datasources.loader import DATASOURCE_REGISTRY
from tuvl.core.datasources.postgres import PostgresStartupError, close_db, init_db
from tuvl.core.engine.telemetry import init_telemetry
from tuvl.core.infra.redis import close_redis, init_redis
from tuvl.core.logger import configure_tenant_logging, logger, reconfigure_third_party_logging
from tuvl.core.models.collections_loader import COLLECTION_REGISTRY
from tuvl.core.models.embeddings_loader import EMBEDDING_REGISTRY
from tuvl.core.models.loader import MODEL_REGISTRY
from tuvl.core.models.schemas import SCHEMA_REGISTRY
from tuvl.core.system_config import api_docs_mode, exposed_api_groups

if settings.tuvl_dev_mode:
    from tuvl.core.dev.artifacts import router as dev_artifacts_router
    from tuvl.core.dev.extensions import mount_dev_extensions
    from tuvl.core.dev.middleware import DevMiddleware

if not settings.tuvl_dev_mode and settings.tuvl_allowed_hosts:
    from tuvl.core.access_control import AllowedHostsMiddleware


_RUNTIME_STACK: AsyncExitStack | None = None


async def _start_runtime(app: FastAPI) -> None:
    """Start the 2.0 runtime: code agents, trigger routes and the worker."""
    if settings.tuvl_dev_mode:
        from tuvl.runtime import devtools

        devtools.load(settings.project_dir)
    from tuvl.api.triggers import mount_triggers
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.datasources.postgres import _engines_rw, primary_ds_name
    from tuvl.runtime.code import load_agents
    from tuvl.runtime.host import RuntimeHost, set_host

    engine = _engines_rw.get(primary_ds_name())
    if engine is None:
        logger.warning("runtime.disabled", reason="no primary datasource")
        return
    from tuvl.runtime.decision import load_entry_point_providers

    agents = load_agents(settings.dir_for("agents"))
    load_entry_point_providers()
    host = RuntimeHost(
        engine, role=settings.tuvl_role, concurrency=settings.tuvl_worker_concurrency
    )
    set_host(host)
    mounted = mount_triggers(app, dict(WORKFLOWS))
    global _RUNTIME_STACK
    _RUNTIME_STACK = AsyncExitStack()
    tools = 0
    if any(
        doc.enabled and doc.spec.trigger.mcp is not None and doc.spec.trigger.mcp.enabled
        for doc in host.registry().values()
    ):
        from tuvl.api.mcp_export import mount_mcp

        tools = await mount_mcp(app, host, _RUNTIME_STACK)
    host.start()
    for name, reasons in host.unavailable().items():
        logger.error("runtime.workflow.unavailable", workflow=name, reasons=reasons)
    logger.info(
        "runtime.started",
        role=settings.tuvl_role,
        agents=agents,
        triggers=mounted,
        mcp_tools=tools,
        workflows=len(WORKFLOWS),
    )


async def _stop_runtime() -> None:
    from tuvl.runtime.host import _HOST, set_host

    global _RUNTIME_STACK
    if _RUNTIME_STACK is not None:
        await _RUNTIME_STACK.aclose()
        _RUNTIME_STACK = None
    if _HOST is not None:
        await _HOST.stop()
        set_host(None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("tuvl starting up")
    logger.info(
        "Settings loaded",
        app=settings.app_name,
        project=settings.project_dir,
        db=f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}",
    )
    init_telemetry()
    reconfigure_third_party_logging()

    # Fail fast on a missing token signing key. Construction is lazy (the
    # CLI must import auth modules keylessly), so without this a misconfigured
    # production deploy would boot green and 500 on the first verify.
    from tuvl.core.auth.keys import get_key_manager

    get_key_manager()

    logger.info("Loading all configurations")
    load_all_configs(include_dev_documents=settings.tuvl_env != "production")

    # Integrity-pinned external artifact sources (config.yaml artifact_sources).
    from tuvl.core.artifacts.external import load_external_artifacts

    await load_external_artifacts()
    logger.info("Model registry loaded", models=list(MODEL_REGISTRY.keys()))
    logger.info("Schema registry loaded", schemas=list(SCHEMA_REGISTRY.keys()))
    logger.info("Embedding registry loaded", embeddings=list(EMBEDDING_REGISTRY.keys()))
    logger.info("Collection registry loaded", collections=list(COLLECTION_REGISTRY.keys()))
    logger.info("DataSource registry loaded", datasources=list(DATASOURCE_REGISTRY.keys()))
    from tuvl.contract.registry import WORKFLOWS

    logger.info("Workflow registry loaded", workflows=sorted(WORKFLOWS))

    # Fail fast on models the workflows use but the environment can't serve.
    from tuvl.core.models.requirements import check_at_boot

    check_at_boot(production=settings.tuvl_env == "production")

    logger.info("Running deployment mode pre-flight check")
    await run_pre_flight()

    # Conditionally append the tenant log processor to the structlog pipeline.
    # Must run after run_pre_flight() has set LOCKED_DEPLOYMENT_MODE.
    configure_tenant_logging()

    build_crud_routers(app)

    logger.info("Initialising Redis (if configured)")
    await init_redis()

    import asyncio as _asyncio

    try:
        await init_db()
    except PostgresStartupError:
        logger.error(
            "Startup aborted — could not connect to PostgreSQL. "
            "Fix the connection issue and restart tuvl."
        )
        await close_db()
        # SIGTERM the reloader/supervisor (our parent) so it exits cleanly
        # rather than hanging idle waiting for file-change events.
        import signal as _signal

        parent = os.getppid()
        if parent and parent != 1:
            os.kill(parent, _signal.SIGTERM)
        os._exit(1)

    logger.info("Syncing model versions to DB")
    from tuvl.core.models.model_version import sync_model_versions

    await sync_model_versions()

    # DB-source artifact rows merge below file-source precedence.
    from tuvl.core.artifacts.db import merge_db_artifacts

    await merge_db_artifacts()

    # Every artifact:// ref in every workflow must resolve and type-check.
    # Production: unresolved ref = startup failure (same posture as
    # PostgresStartupError); dev: warning, the step errors at runtime.
    from tuvl.core.artifacts.loader import check_artifact_refs_at_boot

    check_artifact_refs_at_boot()

    await _start_runtime(app)

    # Cron scheduler for workflows declaring trigger.schedule.
    if settings.tuvl_scheduler_enabled:
        from tuvl.core.engine.scheduler import scheduler_loop

        app.state.workflow_scheduler = _asyncio.create_task(scheduler_loop())

    # Webhook notifications declared in spec.webhooks (off unless enabled).
    if settings.tuvl_webhooks_enabled:
        from tuvl.runtime.host import get_host
        from tuvl.webhooks.dispatcher import dispatcher_loop

        host = get_host()
        if host.role in ("all", "worker"):
            app.state.webhook_dispatcher = _asyncio.create_task(dispatcher_loop(host.engine))

    logger.info("tuvl is ready")
    yield

    logger.info("tuvl shutting down")
    await _stop_runtime()
    for _task_attr in ("workflow_scheduler", "webhook_dispatcher"):
        _bg = getattr(app.state, _task_attr, None)
        if _bg is not None and not _bg.done():
            _bg.cancel()

    from tuvl import llm as _llm
    from tuvl.core.infra.http_client import aclose_all as _aclose_http_clients
    from tuvl.runtime.mcp_pool import aclose_all as _aclose_mcp_sessions
    from tuvl.webhooks import dispatcher as _webhook_dispatcher

    await _aclose_mcp_sessions()
    await _aclose_http_clients()
    await _llm.aclose()
    await _webhook_dispatcher.aclose()
    await close_redis()
    await close_db()
    logger.info("Shutdown complete.")


_docs_mode = api_docs_mode()
#: system routes appear in the API documentation only when it is asked to show everything
_system_in_docs = _docs_mode == "all"
_exposed = exposed_api_groups()

app = FastAPI(
    title=settings.app_name,
    description=(
        "Spec-driven, deterministic agent workflows: typed YAML contracts run by "
        "a durable, journaled runtime, served as a REST/SSE API."
    ),
    version=pkg_version("tuvl"),
    lifespan=lifespan,
    docs_url="/docs" if _docs_mode != "off" else None,
    redoc_url="/redoc" if _docs_mode != "off" else None,
    openapi_url="/openapi.json" if _docs_mode != "off" else None,
)

if settings.tuvl_dev_mode:
    app.add_middleware(DevMiddleware)
    mount_dev_extensions(app, include_in_schema=_system_in_docs)
    app.include_router(dev_artifacts_router, include_in_schema=_system_in_docs)
    logger.info("Dev mode enabled — /dev/* routes active, insight loaded via tuvl.main.")

# ── FastAPI OpenTelemetry instrumentation ─────────────────────────────────────
# Intercepts every inbound HTTP request, reads the W3C ``traceparent`` header
# (if present), and starts the root span.  In dev mode telemetry is disabled
# so this call is a lightweight no-op (the SDK returns NonRecordingSpan).
if not settings.tuvl_dev_mode and settings.tuvl_telemetry_enabled:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    logger.info("FastAPI OpenTelemetry instrumentation active")

for _group, _router in (
    ("system", system_router),
    ("auth", auth_router),
    ("admin", admin_router),
    ("artifacts", artifacts_router),
    ("runs", runs_router),
):
    if _group in _exposed:
        app.include_router(_router, include_in_schema=_system_in_docs)
logger.info("API groups mounted", groups=sorted(_exposed), docs=_docs_mode)


if not settings.tuvl_dev_mode and settings.tuvl_allowed_hosts:
    app.add_middleware(AllowedHostsMiddleware)
    logger.info("IP allowlist active", allowed_hosts=settings.tuvl_allowed_hosts)

install_cors(app)  # last: outermost, so preflights and refusals carry the CORS headers


@app.get(
    "/",
    tags=["meta"],
    summary="Root redirect",
    response_model=None,
    include_in_schema=_system_in_docs,
)
async def root() -> RedirectResponse | dict[str, str]:
    # Insight is mounted by ``tuvl.main`` (dev mode only) once the
    # ``tuvl_insight`` package is importable — mirror that same gate here
    # rather than relying on an env var, since app.py never mounts it itself.
    import importlib.util as _iutil

    if settings.tuvl_dev_mode and _iutil.find_spec("tuvl_insight") is not None:
        return RedirectResponse(url="/insight/")
    return {"status": "ok", "service": settings.app_name}


@app.get("/health", tags=["meta"], summary="Liveness probe", include_in_schema=_system_in_docs)
async def health() -> dict[str, Any]:
    from tuvl.core.models.requirements import MODEL_STATUS
    from tuvl.runtime.decision import health as decision_health

    report: dict[str, Any] = {"status": "healthy", "version": pkg_version("tuvl")}
    models = decision_health()
    if models:
        report["decision_models"] = models
    if MODEL_STATUS:
        # Unauthenticated: production reports readiness only, not variable names.
        report["models"] = {
            name: (info if settings.tuvl_dev_mode else {"configured": info["configured"]})
            for name, info in MODEL_STATUS.items()
        }
    return report


@app.get(
    "/registry",
    tags=["meta"],
    summary="List all loaded registries",
    dependencies=[Depends(require_dev_mode)],
    include_in_schema=_system_in_docs,
)
async def registry() -> JSONResponse:
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.runtime.code import AGENT_REGISTRY

    return JSONResponse(
        content={
            "models": list(MODEL_REGISTRY.keys()),
            "schemas": {name: list(variants.keys()) for name, variants in SCHEMA_REGISTRY.items()},
            "datasources": list(DATASOURCE_REGISTRY.keys()),
            "agents": sorted(AGENT_REGISTRY),
            "workflows": sorted(WORKFLOWS),
        }
    )
