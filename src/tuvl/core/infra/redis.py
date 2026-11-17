"""Redis connection manager for tuvl.

Reads ``type: redis`` datasource entries from the ``DATASOURCE_REGISTRY``
populated by :func:`~tuvl.core.datasources.loader.load_all_datasources`.

A Redis datasource YAML file looks like::

    kind: DataSource
    metadata:
      name: redis-primary
    spec:
      type: redis
      connection:
        host: ${REDIS_HOST:localhost}
        port: ${REDIS_PORT:6379}
        db: 0
        password: ${REDIS_PASSWORD:}   # empty string → no auth

The first redis datasource found (sorted by name) is used as the primary
client.  If no datasource is configured or the connection fails, the manager
is silently unavailable — callers should check :func:`is_redis_available`
before using the client.

Note on scaling
---------------
Redis is **required** for multi-worker / multi-process deployments.
Without it, OAuth state and token revocation lists are stored only in the
memory of a single process, meaning any other worker will not share the state.
For production, always configure a ``type: redis`` datasource.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable
from typing import Any, cast

import structlog

logger = structlog.get_logger("tuvl")

_redis_client: Any | None = None  # redis.asyncio.Redis or None
_redis_available: bool = False


class RedisRequiredButUnavailableError(RuntimeError):
    """Raised at startup when Redis is configured + required but unreachable.

    Fail-closed is the safe production default — without Redis, token
    revocation and OAuth state degrade to per-process memory which silently
    breaks logout / refresh / login flows across workers.
    """


def _redis_entries() -> list[tuple[str, dict[str, Any]]]:
    """The ``type: redis`` datasources, by name; the first one is used."""
    from tuvl.core.datasources.loader import DATASOURCE_REGISTRY

    return sorted(
        (
            (name, entry)
            for name, entry in DATASOURCE_REGISTRY.items()
            if entry.get("spec", {}).get("type") in ("redis",)
        ),
        key=lambda t: t[0],
    )


def redis_configured() -> bool:
    """Whether the project declares a ``type: redis`` datasource (reachable or not)."""
    return bool(_redis_entries())


async def init_redis() -> None:
    """Attempt to connect to the first configured Redis datasource.

    Call this during application startup (after datasources are loaded).

    Behaviour
    ---------
    - No Redis datasource configured → log info, continue with in-memory fallback.
    - Redis configured + reachable    → connect, ready.
    - Redis configured + unreachable + ``settings.tuvl_require_redis`` is True
      → raise ``RedisRequiredButUnavailableError`` so the process refuses to boot.
    - Redis configured + unreachable + ``tuvl_require_redis`` is False
      → log warning and fall back to Postgres (revocation and session end; OAuth state stays in memory).
    """
    global _redis_client, _redis_available

    from tuvl.core.config import settings

    redis_entries = _redis_entries()
    if not redis_entries:
        if settings.tuvl_require_redis:
            raise RedisRequiredButUnavailableError(
                "TUVL_REQUIRE_REDIS=true but no Redis datasource is configured. "
                "Add a 'kind: DataSource, spec.type: redis' YAML to your datasources/ "
                "directory before boot."
            )
        logger.info(
            "redis.not_configured",
            reason="no DataSource with spec.type=redis was registered",
            consequence="revocation and session end use Postgres; OAuth state uses in-process memory",
            multi_worker_safe=False,
        )
        return

    name, entry = redis_entries[0]
    conn = entry.get("spec", {}).get("connection", {})

    host = str(conn.get("host", "localhost"))
    port = int(conn.get("port", 6379))
    db = int(conn.get("db", 0))
    password = conn.get("password") or None  # treat empty string as None

    try:
        import redis.asyncio as aioredis
    except ImportError as exc:
        if settings.tuvl_require_redis:
            raise RedisRequiredButUnavailableError(
                "TUVL_REQUIRE_REDIS=true but the redis package is not installed. "
                "Run `pip install redis[asyncio]`."
            ) from exc
        logger.warning(
            "redis.package.missing",
            hint="pip install redis[asyncio]",
        )
        return

    try:
        client = aioredis.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        await cast(
            "Awaitable[bool]", client.ping()
        )  # redis-py types ping() as bool | Awaitable[bool]
        _redis_client = client
        _redis_available = True
        logger.info(
            "redis.connected",
            datasource=name,
            host=host,
            port=port,
            db=db,
        )
    except Exception as exc:
        if settings.tuvl_require_redis:
            raise RedisRequiredButUnavailableError(
                f"TUVL_REQUIRE_REDIS=true but Redis datasource {name!r} "
                f"({host}:{port}) is unreachable: {exc}. Fix connectivity or "
                "unset TUVL_REQUIRE_REDIS to fall back to Postgres."
            ) from exc
        logger.warning(
            "redis.unreachable.fallback_postgres",
            datasource=name,
            host=host,
            port=port,
            error=str(exc),
            consequence="revocation and session end use Postgres here; workers connected to Redis do not see them",
        )


def is_redis_available() -> bool:
    """Return ``True`` if Redis is connected and ready."""
    return _redis_available


def get_redis_client() -> Any:
    """Return the active Redis client, or raise ``RuntimeError`` if unavailable."""
    if _redis_client is None:
        raise RuntimeError("Redis is not available. Configure a 'type: redis' datasource.")
    return _redis_client


async def close_redis() -> None:
    """Close the Redis connection gracefully."""
    global _redis_client, _redis_available
    if _redis_client is not None:
        with contextlib.suppress(Exception):
            await _redis_client.aclose()
        _redis_client = None
        _redis_available = False
        logger.info("redis.connection.closed")
