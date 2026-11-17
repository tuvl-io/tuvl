"""tuvl structured logger.

Configures structlog once at import time.  The rendered format is toggled by
``TUVL_ENV`` (read directly from the environment so that this module can be
imported before pydantic-settings parses the full config):

* ``development`` — compact logfmt-style coloured output via ``_dev_renderer``
* anything else   — strict single-line JSON via ``JSONRenderer`` (default)

Dev format (single line per record)::

    2026-06-18T05:01:13Z INFO  tuvl.core.bootstrap  brownfield boot deployment_mode=single_tenant pid=6206

Design choices:

* level is uppercase, padded to 5 chars (``INFO ``, ``WARN ``, ``ERROR``)
* logger name precedes the event (matches uvicorn / SLF4J / zerolog conventions)
* the event message is **not** padded — KV pairs follow with a single space
* ``trace_id`` / ``span_id`` remain in the line for span correlation
* ANSI colour is auto-detected from ``sys.stdout.isatty()``; force off with
  ``NO_COLOR=1`` or ``TUVL_LOG_COLOR=no``, force on with ``TUVL_LOG_COLOR=yes``

Every log event automatically gains ``trace_id`` and ``span_id`` fields when
an active OpenTelemetry span exists in the current execution context.

All structlog loggers flow through the stdlib ``logging`` pipeline
(``LoggerFactory()``) so that any attached stdlib handlers continue to receive
records correctly.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import opentelemetry.trace as _otel_trace
import structlog

# ── OTel context injector ─────────────────────────────────────────────────────


def inject_otel_context(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Inject ``trace_id`` / ``span_id`` from the active OTel span, if any."""
    span = _otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _inject_user_context(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Append ``user_id`` to every log record when an authenticated
    principal is bound to the current async task.

    Registered in all modes (user identity is orthogonal to tenancy);
    omitted when no user is bound.
    """
    try:
        from tuvl.core.context import current_user_id as _ctx

        uid = _ctx.get("")
        if uid:
            event_dict["user_id"] = uid
    except ImportError:
        pass
    return event_dict


# ── Dev console renderer (compact logfmt) ────────────────────────────────────


def _colors_enabled() -> bool:
    """Decide whether to emit ANSI colour codes for dev-console output.

    Order of precedence: ``TUVL_LOG_COLOR`` → ``NO_COLOR`` → stdout tty check.
    """
    flag = os.environ.get("TUVL_LOG_COLOR", "").lower()
    if flag in ("1", "yes", "true", "on"):
        return True
    if flag in ("0", "no", "false", "off"):
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_USE_COLOR = _colors_enabled()

_RESET = "\033[0m" if _USE_COLOR else ""
_DIM = "\033[2m" if _USE_COLOR else ""
_BOLD = "\033[1m" if _USE_COLOR else ""

_LEVEL_STYLE: dict[str, str] = (
    {
        "debug": "\033[2m",  # dim grey
        "info": "\033[36m",  # cyan
        "warning": "\033[33m",  # yellow
        "warn": "\033[33m",
        "error": "\033[31m",  # red
        "critical": "\033[1;31m",  # bold red
    }
    if _USE_COLOR
    else dict.fromkeys(("debug", "info", "warning", "warn", "error", "critical"), "")
)

_LEVEL_LABEL: dict[str, str] = {
    "debug": "DEBUG",
    "info": "INFO ",
    "warning": "WARN ",
    "warn": "WARN ",
    "error": "ERROR",
    "critical": "CRIT ",
}

_HEADER_KEYS = frozenset({"timestamp", "level", "logger", "logger_name", "event"})


def _format_kv_value(value: Any) -> str:
    """Render a structured value for the dev console.

    Strings without whitespace or ``=`` pass through bare; otherwise we
    JSON-quote so spaces don't bleed into adjacent fields.  Paths use
    ``str()`` so ``project=/foo/bar`` reads cleanly instead of
    ``project=PosixPath('/foo/bar')``.
    """
    if isinstance(value, str):
        if value == "" or any(c in value for c in ' "\t='):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, os.PathLike):
        return str(value)
    if isinstance(value, (list, tuple, dict, set)):
        return repr(value)
    text = str(value)
    if any(c in text for c in ' "\t='):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _dev_renderer(_logger: Any, _method: str, event_dict: dict[str, Any]) -> str:
    """Compact logfmt-style renderer for development.

    Format: ``<ts> <LEVEL> <logger>  <event> key=value key=value``
    """
    ts_raw = event_dict.pop("timestamp", "")
    if isinstance(ts_raw, str) and "." in ts_raw and ts_raw.endswith("Z"):
        ts = ts_raw.split(".", 1)[0] + "Z"
    else:
        ts = str(ts_raw)

    level = str(event_dict.pop("level", "info")).lower()
    level_label = _LEVEL_LABEL.get(level, level.upper().ljust(5)[:5])
    level_style = _LEVEL_STYLE.get(level, "")

    logger_name = str(event_dict.pop("logger", event_dict.pop("logger_name", "")) or "")
    event = str(event_dict.pop("event", ""))

    trace_kvs: list[str] = []
    other_kvs: list[str] = []
    for k, v in event_dict.items():
        if k in _HEADER_KEYS:
            continue
        rendered = f"{k}={_format_kv_value(v)}"
        if k in ("trace_id", "span_id"):
            trace_kvs.append(rendered)
        else:
            other_kvs.append(rendered)

    parts: list[str] = []
    if ts:
        parts.append(f"{_DIM}{ts}{_RESET}" if _USE_COLOR else ts)
    parts.append(f"{level_style}{level_label}{_RESET}" if level_style else level_label)
    if logger_name:
        parts.append(f"{_BOLD}{logger_name}{_RESET}" if _USE_COLOR else logger_name)
    if event:
        parts.append(event)

    line = " ".join(parts)
    tail = " ".join(other_kvs + trace_kvs)
    if tail:
        line = f"{line}  {_DIM}{tail}{_RESET}" if _USE_COLOR else f"{line}  {tail}"
    return line


# ── Renderer selection ────────────────────────────────────────────────────────

_tuvl_env: str = os.environ.get("TUVL_ENV", "production").strip().lower()

_renderer: Any = (
    _dev_renderer if _tuvl_env == "development" else structlog.processors.JSONRenderer()
)

# ── Shared processor chain ────────────────────────────────────────────────────
# Applied to both structlog-native loggers and foreign stdlib log records.

_shared_processors: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
    inject_otel_context,
    _inject_user_context,
]

# ── Wire structlog → stdlib ───────────────────────────────────────────────────
# Using LoggerFactory() routes structlog records through the stdlib pipeline so
# any attached handlers keep receiving them correctly.

structlog.configure(
    processors=_shared_processors
    + [
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    cache_logger_on_first_use=True,
)

# ── Configure the stdlib root logger ─────────────────────────────────────────
# Route all stdlib log records (including from third-party libraries) through
# the structlog ProcessorFormatter so they also get OTel correlation and the
# correct renderer.

# Diagnostics go to stderr so stdout stays clean for actual command output
# (e.g. `tuvl --version`, `tuvl follow run`); container runtimes capture both.
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(
    structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _renderer,
        ],
    )
)

_root = logging.getLogger()
_root.handlers.clear()
_root.addHandler(_handler)
_root.setLevel(logging.INFO)

# ── Public API ────────────────────────────────────────────────────────────────


def get_logger(name: str = "tuvl") -> structlog.stdlib.BoundLogger:
    """Return a structlog ``BoundLogger`` scoped to *name*."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def reconfigure_third_party_logging() -> None:
    """Strip uvicorn / watchfiles handlers so their records propagate to root.

    Uvicorn calls ``logging.config.dictConfig`` after the app module is
    imported, which adds its own ``DefaultFormatter`` / ``AccessFormatter``
    handlers to ``uvicorn`` and ``uvicorn.access`` **with propagate=False**.
    That means those records never reach the root logger and bypass the
    structlog pipeline entirely.

    This function must be called from the FastAPI lifespan (after uvicorn has
    run its dictConfig) to reset those loggers so everything routes through
    the single structlog handler on the root logger.
    """
    _third_party = [
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "watchfiles",
        "watchfiles.main",
        "sqlalchemy.engine",
        "httpx",
    ]
    for name in _third_party:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


# Default module-level logger — re-exported for legacy
# ``from tuvl.core.logger import logger`` imports.
logger = get_logger("tuvl")


# ── Multi-tenant log processor ────────────────────────────────────────────────


def _inject_tenant_context(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Append ``tenant_id`` to every log record in multi-tenant deployments.

    Reads ``current_tenant_id`` from the contextvar set by the active
    async task.  If no tenant is set (e.g. system-level log lines) the
    key is omitted.

    Appended to the structlog pipeline only when
    ``LOCKED_DEPLOYMENT_MODE == "multi_tenant"`` (see
    :func:`configure_tenant_logging`).
    """
    try:
        from tuvl.core.context import current_tenant_id as _ctx

        tid = _ctx.get("")
        if tid:
            event_dict["tenant_id"] = tid
    except ImportError:
        pass
    return event_dict


def configure_tenant_logging() -> None:
    """Conditionally append the tenant processor to the structlog pipeline.

    Must be called from the FastAPI lifespan *after*
    :func:`tuvl.core.bootstrap.run_pre_flight` has set
    ``LOCKED_DEPLOYMENT_MODE``.

    In single-tenant mode this function is a complete no-op — the logger
    pipeline is not modified and no structlog cache invalidation occurs.
    In multi-tenant mode the ``_inject_tenant_context`` processor is
    inserted immediately before the ``wrap_for_formatter`` step so that
    ``tenant_id`` appears in every structured log record.
    """
    # Lazy import to prevent a circular dependency at module load time.
    try:
        import tuvl.core.bootstrap as _bootstrap
    except ImportError:
        return

    if _bootstrap.LOCKED_DEPLOYMENT_MODE != "multi_tenant":
        return

    cfg = structlog.get_config()
    processors: list[Any] = list(cfg["processors"])

    # Idempotent: bail out if the processor is already present.
    if _inject_tenant_context in processors:
        return

    # Insert just before the wrap_for_formatter step so the tenant_id
    # field is available to the stdlib ProcessorFormatter renderer.
    insertion_point = next(
        (
            i
            for i, p in enumerate(processors)
            if p is structlog.stdlib.ProcessorFormatter.wrap_for_formatter
        ),
        len(processors) - 1,
    )
    processors.insert(insertion_point, _inject_tenant_context)

    structlog.configure(
        processors=processors,
        logger_factory=cfg.get("logger_factory", structlog.stdlib.LoggerFactory()),
        wrapper_class=cfg.get(
            "wrapper_class",
            structlog.make_filtering_bound_logger(logging.INFO),
        ),
        cache_logger_on_first_use=True,
    )

    # Foreign (stdlib / 3rd-party) records flow through the root handler's
    # ProcessorFormatter.foreign_pre_chain, NOT the structlog pipeline above, so
    # tenant_id must be injected there too — otherwise foreign records carry
    # user_id (it's in _shared_processors) but never tenant_id, an inconsistency.
    for handler in logging.getLogger().handlers:
        fmt = getattr(handler, "formatter", None)
        pre = getattr(fmt, "foreign_pre_chain", None)
        if fmt is not None and pre is not None and _inject_tenant_context not in pre:
            fmt.foreign_pre_chain = list(pre) + [_inject_tenant_context]

    logger.info("bootstrap: tenant log processor enabled")
