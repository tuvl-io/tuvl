"""Project-level system configuration — ``.tuvl/system.yaml`` (kind: SystemConfig).

Boot-time settings that shape the mounted API surface. The file is written by
``tuvl init`` and edited via the Insight Settings page; changes take effect on
restart. ``spec.deployment_mode`` in the same file is read separately by
bootstrap (pre-DB, write-once locking semantics) — this module never
interprets it.

Resolution order for each knob: env var > ``.tuvl/system.yaml`` > default.
The file is read fail-open (absent or unparseable → defaults) so the engine
stays boot-safe where ``tuvl init`` was never run; the env override is the
fail-closed path for ops.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import yaml

from tuvl.core.config import settings
from tuvl.core.logger import logger

SYSTEM_YAML_RELPATH = ".tuvl/system.yaml"

# Known keys under spec.api — the validator warns on anything else.
SYSTEM_API_KEYS: frozenset[str] = frozenset(
    {"expose_model_crud", "expose", "docs", "dev_expose_all", "cors_origins"}
)

#: the system route groups ``spec.api.expose`` chooses between
API_GROUPS: tuple[str, ...] = ("auth", "iam", "roles", "admin", "artifacts", "system")
#: always mounted: starting, following and approving runs, which every client and trigger depends on
ALWAYS_MOUNTED: frozenset[str] = frozenset({"runs"})
#: what a production server mounts, besides ``runs``, unless told otherwise
PRODUCTION_GROUPS: frozenset[str] = frozenset({"auth"})
API_DOCS_MODES: tuple[str, ...] = ("off", "workflows", "all")


@lru_cache(maxsize=1)
def load_system_config() -> dict[str, Any]:
    """Return the parsed ``spec`` of ``.tuvl/system.yaml`` (``{}`` when absent/invalid)."""
    path = settings.project_dir / SYSTEM_YAML_RELPATH
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as exc:
        logger.warning("system_config.unreadable", path=str(path), error=str(exc))
        return {}
    if not isinstance(data, dict):
        return {}
    spec = data.get("spec", {})
    return spec if isinstance(spec, dict) else {}


def expose_model_crud() -> bool:
    """Whether the auto-generated ``/models/*`` CRUD routers are mounted.

    ``TUVL_EXPOSE_MODEL_CRUD`` (when set) overrides ``spec.api.expose_model_crud``.
    """
    if settings.tuvl_expose_model_crud is not None:
        return settings.tuvl_expose_model_crud
    api_cfg = load_system_config().get("api")
    if isinstance(api_cfg, dict):
        return bool(api_cfg.get("expose_model_crud", True))
    return True


def _api_cfg() -> dict[str, Any]:
    cfg = load_system_config().get("api")
    return cfg if isinstance(cfg, dict) else {}


def dev_exposes_all() -> bool:
    """Whether the dev server mounts every system route group (Insight needs them).

    Turn it off to see exactly what a production server would mount.
    ``TUVL_DEV_EXPOSE_ALL`` overrides ``spec.api.dev_expose_all``.
    """
    if settings.tuvl_dev_expose_all is not None:
        return settings.tuvl_dev_expose_all
    return bool(_api_cfg().get("dev_expose_all", True))


def _production(production: bool | None) -> bool:
    if production is not None:
        return production
    return not (settings.tuvl_dev_mode and dev_exposes_all())


def exposed_api_groups(production: bool | None = None) -> frozenset[str]:
    """The system route groups to mount: ``runs`` always, plus those of ``auth``, ``admin``,
    ``artifacts`` and ``system`` that are exposed.

    The dev server mounts all of them (Insight needs them) unless ``spec.api.dev_expose_all`` is
    false, when it follows the production rules. A production server mounts only ``auth`` unless
    ``TUVL_API_EXPOSE`` (comma-separated) or ``spec.api.expose`` (a list) says otherwise, so IAM
    administration (``iam``: bootstrap, users, roles, federation), artifact storage and the
    introspection API are not reachable where nobody asked for them. Production IAM is run with
    ``tuvl iam`` inside the cluster. ``production=True`` answers for a production server whatever
    mode this process runs in.
    """
    if not _production(production):
        return frozenset(API_GROUPS) | ALWAYS_MOUNTED
    if settings.tuvl_api_expose is not None:
        asked: Any = settings.tuvl_api_expose.split(",")
    else:
        asked = _api_cfg().get("expose")
    if not isinstance(asked, list):
        return PRODUCTION_GROUPS | ALWAYS_MOUNTED
    names = {str(n).strip().lower() for n in asked} - ALWAYS_MOUNTED
    for unknown in sorted(names - set(API_GROUPS)):
        logger.warning("system_config.unknown_api_group", group=unknown, known=list(API_GROUPS))
    return frozenset(names & set(API_GROUPS)) | ALWAYS_MOUNTED


def api_docs_mode(production: bool | None = None) -> str:
    """What the generated API documentation (``/docs``, ``/redoc``, ``/openapi.json``) shows.

    ``off`` serves none of it, ``workflows`` shows only the project's own workflow and model
    routes, ``all`` also shows the system routes. The dev server defaults to ``workflows``
    (``off`` when it is set to mirror production), production to ``off``. ``TUVL_API_DOCS`` overrides ``spec.api.docs``.
    """
    default = "off" if _production(production) else "workflows"
    asked = settings.tuvl_api_docs
    if asked is None:
        asked = _api_cfg().get("docs")
    if asked is False:
        return "off"
    mode = str(asked).strip().lower() if asked is not None else default
    return mode if mode in API_DOCS_MODES else default


def invalid_cors_origin(origin: str) -> str | None:
    """Why *origin* cannot be an allowed browser origin, or ``None`` when it can.

    An origin is ``scheme://host[:port]`` exactly as a browser sends it, or ``*``. A trailing slash
    or a path never matches anything, so it is refused instead of silently allowing nothing.
    """
    if origin == "*":
        return None
    try:
        parts = urlsplit(origin)
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return "is not a valid origin"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "must start with http:// or https:// and name a host"
    if parts.path or parts.query or parts.fragment or origin.endswith("/"):
        return "must be scheme://host[:port] only, without a path or a trailing slash"
    return None


def cors_origins() -> tuple[str, ...]:
    """The browser origins allowed to call the API; empty means no CORS headers are sent.

    ``TUVL_CORS_ORIGINS`` (comma-separated, when set, even to an empty string) overrides
    ``spec.api.cors_origins`` (a list). An origin that is not ``scheme://host[:port]`` is logged
    and left out: a mistake narrows access, it never widens it.
    """
    if settings.tuvl_cors_origins is not None:
        asked: Any = settings.tuvl_cors_origins.split(",")
    else:
        asked = _api_cfg().get("cors_origins")
    if not isinstance(asked, list):
        return ()
    origins: list[str] = []
    for item in asked:
        origin = str(item).strip()
        if not origin or origin in origins:
            continue
        problem = invalid_cors_origin(origin)
        if problem:
            logger.warning("system_config.invalid_cors_origin", origin=origin, problem=problem)
            continue
        origins.append(origin)
    return tuple(origins)
