import contextlib
import os
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any
from urllib.parse import quote_plus

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.exc import IllegalStateChangeError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.context import current_tenant_id
from tuvl.core.datasources.loader import DATASOURCE_REGISTRY
from tuvl.core.logger import logger
from tuvl.core.models.tables import table_of

# Strict allowlist for tenant identifiers used inside ``SET LOCAL ...``.
# Postgres does not support bind parameters in SET statements, so the value
# must be interpolated.  Enforcing a narrow character class makes injection
# structurally impossible — a rogue tenant id is rejected before it ever
# reaches the database driver.
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

# Pool defaults applied to every engine — keeps long-idle connections from
# being silently severed by cloud Postgres idle-timeout policies.
_POOL_RECYCLE_SECONDS = int(os.environ.get("TUVL_DB_POOL_RECYCLE", "1800"))
_POOL_TIMEOUT_SECONDS = int(os.environ.get("TUVL_DB_POOL_TIMEOUT", "30"))


class PostgresStartupError(Exception):
    """Raised when PostgreSQL is unreachable at startup."""


# ---------------------------------------------------------------------------
# Per-datasource engine / session-factory registries
# ---------------------------------------------------------------------------

_engines_rw: dict[str, AsyncEngine] = {}
_engines_ro: dict[str, AsyncEngine] = {}
_sessions_rw: dict[str, async_sessionmaker] = {}
_sessions_ro: dict[str, async_sessionmaker] = {}

# Primary datasource name (used by the FastAPI get_session dependency for
# backward-compatible single-DB requests).
# Resolved dynamically in _build_engines() from loader.PRIMARY_DS so it
# honours the ``metadata.primary: true`` flag or the first-postgres fallback.
_PRIMARY_DS: str = ""

# Set to True by init_db() once the pgvector extension + SystemVectorStore table
# are confirmed ready.  Checked by DataIngest / DataSearch nodes at call time.
_VECTOR_STORE_READY: bool = False

# The datasource that actually hosts the vector store.  Determined at init_db()
# time from settings.vector_datasource (falls back to _PRIMARY_DS).
_VECTOR_DS: str = ""


def _get_dsn(conn: dict, driver: str) -> str:
    password = quote_plus(str(conn["password"]))
    return (
        f"postgresql+{driver}://{conn['username']}:{password}"
        f"@{conn['host']}:{conn['port']}/{conn['database']}"
    )


# ── Transaction-scoped tenant isolation (Phase 0) ────────────────────────────


class MissingTenantContextError(RuntimeError):
    """Raised when a database session is opened in multi-tenant mode without
    the host having set ``current_tenant_id``.

    Failing closed at the session boundary prevents queries from accidentally
    running with no tenant predicate — which, on a misconfigured RLS policy,
    could expose cross-tenant rows.  The host application is expected to
    populate the tenant ContextVar (via :func:`tuvl.tenancy.set_tenant_context`
    or :func:`tuvl.tenancy.tenant_scope`) before any TUVL handler executes.
    """


def _bind_tenant_isolation(session: AsyncSession) -> None:
    """Install a per-transaction ``SET LOCAL app.current_tenant_id`` hook.

    SQLAlchemy 2.0 auto-begins a new transaction on the first statement after
    each ``commit()`` / ``rollback()``.  This helper hooks the
    ``after_begin`` session event so the tenant id is rebound to **every**
    transaction the session enters during its lifetime — not only the first.

    In single-tenant mode this function is a deliberate no-op so that
    deployments without tenancy incur zero overhead and never raise the
    safety-net error.
    """
    # Local import: ``bootstrap`` is the single source of truth for the locked
    # deployment mode and must not be evaluated at module import time
    # (postgres.py is imported during bootstrap itself).
    import tuvl.core.bootstrap as _bootstrap

    if _bootstrap.LOCKED_DEPLOYMENT_MODE != "multi_tenant":
        return

    from tuvl.core.config import settings

    require_tenant = getattr(settings, "tuvl_require_tenant_context", True)

    def _after_begin(_session, _transaction, connection) -> None:  # noqa: ANN001
        tid = current_tenant_id.get("")
        if not tid or not _TENANT_ID_RE.fullmatch(tid):
            if require_tenant:
                raise MissingTenantContextError(
                    "Multi-tenant mode is active but `current_tenant_id` is "
                    "missing or malformed for this transaction. The host "
                    "application must call `tuvl.tenancy.set_tenant_context()` "
                    "before opening a TUVL database session."
                )
            return
        # Validated via the strict regex above — interpolation is safe.
        connection.exec_driver_sql(f"SET LOCAL app.current_tenant_id = '{tid}'")

    event.listen(session.sync_session, "after_begin", _after_begin)


def primary_ds_name() -> str:
    """Return the resolved primary datasource name (honours metadata.primary)."""
    return _PRIMARY_DS or "main_postgres"


def _build_engines() -> None:
    """Build RW + RO engine pairs for every postgres datasource in the registry."""
    global _PRIMARY_DS

    from tuvl.core.datasources.loader import PRIMARY_DS

    _PRIMARY_DS = PRIMARY_DS

    if not DATASOURCE_REGISTRY:
        logger.info("postgres.setup.skipped", reason="no datasources registered")
        return

    pid = os.getpid()
    built: list[str] = []

    for ds_name, ds_config in DATASOURCE_REGISTRY.items():
        spec = ds_config.get("spec", {})
        ds_type = spec.get("type", "postgres")
        if ds_type not in ("postgres", "postgresql"):
            logger.debug("Skipping non-postgres datasource", datasource=ds_name, type=ds_type)
            continue

        conn = spec["connection"]
        pool = spec.get("pooling", {})
        driver = spec.get("driver", "asyncpg")

        pool_size = int(pool.get("min_size", 5))
        max_overflow = max(0, int(pool.get("max_size", 20)) - pool_size)
        connect_args = dict(pool.get("connect_args", {}))

        # Convenience for common asyncpg timeouts since it's the default driver
        if driver == "asyncpg":
            if "command_timeout" in pool and "command_timeout" not in connect_args:
                connect_args["command_timeout"] = float(pool["command_timeout"])
            if "server_settings" in pool and "server_settings" not in connect_args:
                connect_args["server_settings"] = pool["server_settings"]

        logger.info(
            "postgres.engine.building",
            pid=pid,
            datasource=ds_name,
            host=conn["host"],
            port=conn["port"],
            database=conn["database"],
        )

        rw_dsn = _get_dsn(conn, driver)
        ro_conn = {**conn, "host": conn.get("ro_host", conn["host"])}
        ro_dsn = _get_dsn(ro_conn, driver)

        # Allow skipping the QueuePool completely (e.g. for PgBouncer / serverless)
        poolclass_name = pool.get("poolclass", "QueuePool")
        if poolclass_name == "NullPool":
            from sqlalchemy.pool import NullPool

            pool_kwargs: dict[str, Any] = {"poolclass": NullPool}
            pool_desc = "NullPool (external pooling)"
        else:
            pool_kwargs = {
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "pool_recycle": _POOL_RECYCLE_SECONDS,
                "pool_timeout": _POOL_TIMEOUT_SECONDS,
            }
            pool_desc = f"{pool_size}…{pool_size + max_overflow}"

        _engines_rw[ds_name] = create_async_engine(
            rw_dsn,
            echo=False,
            pool_pre_ping=True,
            connect_args=connect_args,
            **pool_kwargs,
        )
        _engines_ro[ds_name] = create_async_engine(
            ro_dsn,
            echo=False,
            pool_pre_ping=True,
            connect_args=connect_args,
            **pool_kwargs,
        )
        _sessions_rw[ds_name] = async_sessionmaker(
            _engines_rw[ds_name], class_=AsyncSession, expire_on_commit=False
        )
        _sessions_ro[ds_name] = async_sessionmaker(
            _engines_ro[ds_name], class_=AsyncSession, expire_on_commit=False
        )

        logger.info(
            "postgres.engine.ready",
            pid=pid,
            datasource=ds_name,
            pool=pool_desc,
        )
        built.append(ds_name)

        # Isolation is applied per-transaction via the ``after_begin`` session
        # event (see ``_bind_tenant_isolation``).
        import tuvl.core.bootstrap as _bootstrap

        if _bootstrap.LOCKED_DEPLOYMENT_MODE == "multi_tenant":
            logger.info(
                "postgres.multi_tenant.active",
                pid=pid,
                datasource=ds_name,
                detail="sessions will issue SET LOCAL app.current_tenant_id per request",
            )

    if not built:
        logger.info("postgres.setup.skipped", reason="no postgres-type datasources found")


def get_session_factory(ds_name: str, is_write: bool) -> async_sessionmaker:
    """
    Return the session factory for *ds_name*.

    Parameters
    ----------
    ds_name:
        Name matching a key in ``DATASOURCE_REGISTRY``.
    is_write:
        ``True``  → use the read-write engine (primary).
        ``False`` → use the read-only engine (replica, falls back to primary
                    when ``ro_host`` is not configured).

    Raises
    ------
    RuntimeError
        When *ds_name* has not been initialised (engine not built).
    """
    pool = _sessions_rw if is_write else _sessions_ro
    if ds_name not in pool:
        registered = list(_sessions_rw.keys()) or ["(none)"]
        raise RuntimeError(
            f"Datasource '{ds_name}' is not initialised. "
            f"Registered datasources: {registered}. "
            f"Check that a datasource YAML with metadata.name='{ds_name}' exists."
        )
    return pool[ds_name]


async def _close_session_safely(session: AsyncSession) -> None:
    """Close an async session, surviving mid-state-transition cancellations.

    SQLAlchemy 2.0+ raises ``IllegalStateChangeError`` if ``close()`` is
    invoked while the session is mid ``PROVISIONING_CONNECTION`` (very common
    when a FastAPI request is cancelled by uvicorn's hot-reload, by a
    client disconnect mid-stream, or by a browser tab close in the middle
    of a SQL round-trip).  We catch that, attempt a rollback to release
    the connection back to the pool, and swallow further errors so the
    cleanup doesn't shadow whatever cancellation triggered it.

    Any leaked connections still get reclaimed by SQLAlchemy's
    ``AsyncAdaptedQueuePool`` on the next GC pass — but explicit best-effort
    cleanup keeps the noisy "trying to clean up non-checked-in connection"
    warnings out of the logs in the common case.
    """
    try:
        await session.close()
    except IllegalStateChangeError:
        with contextlib.suppress(Exception):
            await session.rollback()
    except Exception:
        # Best-effort: anything else (network error during close, etc.)
        # is suppressed so this finalizer can't mask the original error.
        pass


class SessionRouter:
    """FastAPI dependency that yields a session for the primary datasource."""

    async def __call__(self, request: Request) -> AsyncGenerator[AsyncSession]:
        if _PRIMARY_DS not in _sessions_rw:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=503,
                detail=(
                    f"Primary datasource '{_PRIMARY_DS}' is not initialised. "
                    "Ensure a datasource YAML with that name exists and the engine was built."
                ),
            )

        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            factory, target = _sessions_rw[_PRIMARY_DS], f"{_PRIMARY_DS} (RW)"
        else:
            factory, target = _sessions_ro[_PRIMARY_DS], f"{_PRIMARY_DS} (RO)"

        # Manual session lifecycle (no `async with`) so cancellation mid-yield
        # can't trip the AsyncSession.__aexit__ → close() → IllegalStateChangeError
        # chain.  Cleanup runs in `finally` via _close_session_safely.
        session = factory()
        try:
            _bind_tenant_isolation(session)
            logger.debug("DB session opened", target=target)
            yield session
        except OSError as exc:
            logger.error("Network error on DB session", target=target, error=str(exc))
            raise
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error on session", target=target, error=str(exc))
            raise
        except Exception:
            # Not a DB error — e.g. a pydantic ValidationError surfacing as a
            # client-side 422. Re-raise as-is so it isn't mislabeled as a DB
            # failure in the logs.
            raise
        finally:
            await _close_session_safely(session)


get_session = SessionRouter()


def model_session(model_name: str) -> Callable[..., Any]:
    """
    Return a FastAPI dependency that yields a session routed to the datasource
    declared in the model's own config (``spec.datasource``).

    This is the single entry-point for the CRUD routers — no caller needs to
    know which database a model lives on.

    Parameters
    ----------
    model_name:
        The canonical model name (must exist in ``MODEL_DATASOURCE_MAP``).
    """

    async def _dep(request: Request) -> AsyncGenerator[AsyncSession]:
        from tuvl.core.models.loader import MODEL_DATASOURCE_MAP

        ds_name = MODEL_DATASOURCE_MAP.get(model_name, _PRIMARY_DS)
        is_write = request.method in ("POST", "PUT", "PATCH", "DELETE")
        factory = get_session_factory(ds_name, is_write)
        session = factory()
        try:
            _bind_tenant_isolation(session)
            logger.debug("DB session opened", datasource=ds_name, model=model_name)
            yield session
        except OSError as exc:
            logger.error("Network error on DB session", datasource=ds_name, error=str(exc))
            raise
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error on session", datasource=ds_name, error=str(exc))
            raise
        except Exception:
            # Not a DB error — e.g. a pydantic ValidationError surfacing as a
            # client-side 422. Re-raise as-is so it isn't mislabeled as a DB
            # failure in the logs.
            raise
        finally:
            await _close_session_safely(session)

    return _dep


async def check_connection() -> bool:
    """Ping every initialised RW engine."""
    if not _engines_rw:
        raise RuntimeError("No database engines initialised.")
    pid = os.getpid()
    for ds_name, engine in _engines_rw.items():
        logger.info("Pinging datasource", datasource=ds_name, pid=pid)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except OperationalError as exc:
            cause = exc.orig if exc.orig else exc
            logger.error(
                "Datasource connection failed",
                datasource=ds_name,
                pid=pid,
                error=cause,
            )
            raise ConnectionError(f"Cannot reach '{ds_name}': {cause}") from exc
        except OSError as exc:
            logger.error(
                "Network error connecting to datasource",
                datasource=ds_name,
                pid=pid,
                error=exc,
            )
            raise ConnectionError(f"Network error reaching '{ds_name}': {exc}") from exc
        except SQLAlchemyError as exc:
            logger.error(
                "Unexpected database error pinging datasource",
                datasource=ds_name,
                pid=pid,
                error=exc,
            )
            raise
        logger.info("Datasource ping successful", datasource=ds_name, pid=pid)
    return True


def _tables_for_datasource(ds_name: str) -> list[sa.Table]:
    """Tables to create on *ds_name* — models with no explicit spec.datasource
    belong to the resolved primary datasource."""
    from tuvl.core.models.loader import MODEL_DATASOURCE_MAP, MODEL_REGISTRY

    return [
        table_of(cls)
        for name, cls in MODEL_REGISTRY.items()
        if MODEL_DATASOURCE_MAP.get(name, _PRIMARY_DS) == ds_name
    ]


#: Indexes on system tables that can already be large when an upgrade adds the index. They are
#: built with ``CREATE INDEX CONCURRENTLY`` outside any transaction, so writes go on meanwhile.
#: A runtime index is also declared on its model, so a fresh database gets it from ``create_all``.
RUNTIME_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_tuvl_run_deltas_created", "ON tuvl_system_run_deltas (created_at)"),
)
VECTOR_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_svs_created_at", "ON tuvl_system_vector_store (created_at)"),
    (
        "ix_svs_memory_key",
        "ON tuvl_system_vector_store (collection, (metadata->>'memory_key'), created_at) "
        "WHERE metadata ? 'memory_key'",
    ),
    (
        "ix_svs_memory_step",
        "ON tuvl_system_vector_store (collection, (metadata->>'step')) WHERE metadata ? 'step'",
    ),
    (
        "ix_svs_embedding_hnsw",
        "ON tuvl_system_vector_store USING hnsw (embedding vector_l2_ops) "
        "WITH (m = 16, ef_construction = 64)",
    ),
)
_INDEX_LOCK = 0x7475766C_69647800


async def ensure_indexes(engine: AsyncEngine, indexes: tuple[tuple[str, str], ...]) -> None:
    """Create the missing *indexes* concurrently; rebuild one left invalid by a build that died.

    One process at a time, on an advisory lock: another would see an index still being built (it
    exists, not yet valid) and drop it. A process that finds the lock taken skips the work rather
    than wait: a session blocked in ``pg_advisory_lock`` holds a snapshot that the concurrent build
    must outwait, which deadlocks. A failure is logged, never fatal.
    """
    async with engine.connect() as base:
        conn = await base.execution_options(isolation_level="AUTOCOMMIT")
        locked = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _INDEX_LOCK})
        ).scalar_one()
        if not locked:
            logger.info("db.index.skipped", reason="another process is building indexes")
            return
        try:
            for name, body in indexes:
                try:
                    valid = (
                        await conn.execute(
                            text(
                                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)"
                            ),
                            {"n": name},
                        )
                    ).scalar_one_or_none()
                    if valid is True:
                        continue
                    if valid is False:
                        await conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
                    logger.info("db.index.building", index=name)
                    await conn.execute(
                        text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {body}")
                    )
                except SQLAlchemyError as exc:
                    logger.warning("db.index.failed", index=name, error=str(exc))
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _INDEX_LOCK})


async def init_db() -> None:
    _build_engines()
    if not _engines_rw:
        return  # no postgres datasources registered — nothing to do
    try:
        await check_connection()
    except (ConnectionError, SQLAlchemyError) as exc:
        raise PostgresStartupError(str(exc)) from exc
    # Admin-toggled `enabled` flags win over YAML; must run before create_all
    # so a toggled-on version gets its table created this boot.
    from tuvl.core.models.model_version import apply_db_enabled_overrides

    await apply_db_enabled_overrides()
    pid = os.getpid()
    # Create only the tables that belong to each datasource.
    # MODEL_DATASOURCE_MAP is populated by load_all_models() before init_db() runs.
    for ds_name, engine in _engines_rw.items():
        tables = _tables_for_datasource(ds_name)
        if not tables:
            logger.info(
                "No models assigned to datasource, skipping create_all", datasource=ds_name, pid=pid
            )
            continue
        table_names = ", ".join(t.name for t in tables)
        logger.info(
            "Creating tables on datasource", datasource=ds_name, pid=pid, tables=table_names
        )
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all, tables=tables)
        logger.info("Schema ready", datasource=ds_name, pid=pid)

    # Always create IAM tables on the primary datasource when it exists.
    if _PRIMARY_DS in _engines_rw:
        from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole

        iam_tables = [
            table_of(IAMUser),
            table_of(IAMRole),
            table_of(IAMUserRole),
            table_of(IAMRoleScope),
        ]
        async with _engines_rw[_PRIMARY_DS].begin() as conn:
            await conn.run_sync(
                lambda sync_conn: SQLModel.metadata.create_all(
                    sync_conn, tables=iam_tables, checkfirst=True
                )
            )
            # A database first booted before roles could be disabled has no such column.
            await conn.execute(
                text(
                    f'ALTER TABLE "{table_of(IAMRole).name}" ADD COLUMN IF NOT EXISTS '
                    "is_active BOOLEAN NOT NULL DEFAULT true"
                )
            )
        logger.info("IAM tables ready", datasource=_PRIMARY_DS, pid=pid)

    # Create tuvl_system_model_versions tracking table on the primary datasource.
    if _PRIMARY_DS in _engines_rw:
        from tuvl.core.models.model_version import ModelVersion

        mv_tables = [table_of(ModelVersion)]
        async with _engines_rw[_PRIMARY_DS].begin() as conn:
            await conn.run_sync(
                lambda sync_conn: SQLModel.metadata.create_all(
                    sync_conn, tables=mv_tables, checkfirst=True
                )
            )
        logger.info("Model version tracking table ready", datasource=_PRIMARY_DS, pid=pid)

    # Create tuvl_system_artifacts (DB-source artifact uploads) on the primary.
    if _PRIMARY_DS in _engines_rw:
        from tuvl.core.artifacts.models import SystemArtifact

        artifact_tables = [table_of(SystemArtifact)]
        async with _engines_rw[_PRIMARY_DS].begin() as conn:
            await conn.run_sync(
                lambda sync_conn: SQLModel.metadata.create_all(
                    sync_conn, tables=artifact_tables, checkfirst=True
                )
            )
        logger.info("Artifact table ready", datasource=_PRIMARY_DS, pid=pid)

    # 2.0 durable runtime: runs, journal, checkpoints, approvals (spec §14.2).
    if _PRIMARY_DS in _engines_rw:
        from tuvl.runtime.models import RUNTIME_TABLES

        runtime_tables = [table_of(m) for m in RUNTIME_TABLES]
        async with _engines_rw[_PRIMARY_DS].begin() as conn:
            await conn.run_sync(
                lambda sync_conn: SQLModel.metadata.create_all(
                    sync_conn, tables=runtime_tables, checkfirst=True
                )
            )
        await ensure_indexes(_engines_rw[_PRIMARY_DS], RUNTIME_INDEXES)
        logger.info("Runtime tables ready", datasource=_PRIMARY_DS, pid=pid)

    # Create RAG vector store table on the selected (or primary) datasource.
    # Also enables the pgvector extension and creates the HNSW index.
    # Both the pgvector Python package AND the PostgreSQL server extension are
    # required.  Either may be absent, so we degrade gracefully and track
    # readiness in the module-level _VECTOR_STORE_READY flag.
    global _VECTOR_STORE_READY, _VECTOR_DS
    from tuvl.core.config import settings as _cfg

    _preferred = _cfg.vector_datasource or _PRIMARY_DS
    # Fall back to _PRIMARY_DS if the preferred DS was not actually built.
    _VECTOR_DS = _preferred if _preferred in _engines_rw else _PRIMARY_DS
    if _VECTOR_DS in _engines_rw:
        from tuvl.core.models.system import _PGVECTOR_AVAILABLE, SystemVectorStore

        if not _PGVECTOR_AVAILABLE:
            logger.info(
                "rag.pgvector.python_package_missing",
                pid=pid,
                consequence="RAG vector features disabled",
                hint="pip install 'tuvl[vector]'",
            )
        else:
            try:
                async with _engines_rw[_VECTOR_DS].begin() as conn:
                    # Enable pgvector extension (requires pg_vector server extension)
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    await conn.run_sync(
                        lambda sync_conn: SQLModel.metadata.create_all(
                            sync_conn,
                            tables=[table_of(SystemVectorStore)],
                            checkfirst=True,
                        )
                    )
                    # A table made before memory existed has no timestamp: add it without
                    # touching the rows (its indexes follow, built concurrently).
                    await conn.execute(
                        text(
                            "ALTER TABLE tuvl_system_vector_store ADD COLUMN IF NOT EXISTS "
                            "created_at timestamptz NOT NULL DEFAULT now()"
                        )
                    )
                    _dim = _cfg.tuvl_default_embedding_dimensions
                await ensure_indexes(_engines_rw[_VECTOR_DS], VECTOR_INDEXES)
                _VECTOR_STORE_READY = True
                logger.info(
                    "rag.vector_store.ready",
                    pid=pid,
                    datasource=_VECTOR_DS,
                    dimensions=_dim,
                )
            except Exception as _vec_err:
                _VECTOR_STORE_READY = False
                logger.warning(
                    "rag.pgvector.server_extension_missing",
                    pid=pid,
                    consequence="RAG features DataIngest and DataSearch disabled",
                    hint="install pgvector server extension: https://github.com/pgvector/pgvector#installation",
                    error=str(_vec_err),
                )

    await ensure_tenant_columns()


async def ensure_tenant_columns() -> list[str]:
    """Add ``tenant_id`` to system tables created before the column was injected.

    ``create_all`` never alters an existing table, so a database first booted in multi-tenant mode
    on an earlier build has none. Idempotent; a no-op in single-tenant mode.
    """
    import tuvl.core.bootstrap as _bootstrap
    from tuvl.core.models.base import DEFAULT_TENANT, tenant_tables

    if _bootstrap.LOCKED_DEPLOYMENT_MODE != "multi_tenant" or _PRIMARY_DS not in _engines_rw:
        return []
    names = sorted(t.name for t in tenant_tables() if "tenant_id" in t.c)
    async with _engines_rw[_PRIMARY_DS].begin() as conn:
        existing = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
                )
            )
        }
        for name in (n for n in names if n in existing):
            await conn.execute(
                text(
                    f'ALTER TABLE "{name}" ADD COLUMN IF NOT EXISTS tenant_id VARCHAR '
                    f"NOT NULL DEFAULT '{DEFAULT_TENANT}'"
                )
            )
            await conn.execute(
                text(f'CREATE INDEX IF NOT EXISTS "ix_{name}_tenant_id" ON "{name}" (tenant_id)')
            )
    return names


async def get_primary_rw_session() -> AsyncGenerator[AsyncSession]:
    """Yield an always-RW ``AsyncSession`` on the primary datasource.

    Used by IAM routes that must write regardless of the HTTP method.

    Uses the manual try/finally lifecycle (not ``async with``) so the
    session close survives a mid-yield cancellation — see
    ``_close_session_safely`` for the rationale.
    """
    if _PRIMARY_DS not in _sessions_rw:
        raise RuntimeError(
            f"Primary datasource '{_PRIMARY_DS}' is not initialised. "
            "Ensure a datasource YAML with that name exists."
        )
    session = _sessions_rw[_PRIMARY_DS]()
    _bind_tenant_isolation(session)  # no-op in single-tenant mode
    try:
        yield session
    finally:
        await _close_session_safely(session)


async def close_db() -> None:
    pid = os.getpid()
    logger.info("Disposing PostgreSQL connection pools", pid=pid)
    for engine in list(_engines_rw.values()) + list(_engines_ro.values()):
        await engine.dispose()
    _engines_rw.clear()
    _engines_ro.clear()
    _sessions_rw.clear()
    _sessions_ro.clear()
    logger.info("Connection pools disposed", pid=pid)
