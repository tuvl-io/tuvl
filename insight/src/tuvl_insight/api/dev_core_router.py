import asyncio
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from tuvl.core.config import settings
from tuvl.core.logger import logger

router = APIRouter(prefix="/dev", tags=["dev"])

_ALLOWED_KINDS = {"models", "workflows", "datasources", "llms", "federation"}


@router.get("/files")
async def list_files(kind: str | None = None):
    kinds = [kind] if kind else list(_ALLOWED_KINDS)
    invalid = [k for k in kinds if k not in _ALLOWED_KINDS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown kind(s): {invalid}")
    return await asyncio.to_thread(_list_files, kinds, kind)


def _list_files(kinds: list[str], kind: str | None) -> dict[str, Any]:
    result: dict[str, list[str] | dict[str, list[str]]] = {}
    for k in kinds:
        dir_path = _config_dir(k)
        if dir_path.exists():
            result[k] = [f.name for f in sorted(dir_path.glob("*.yaml"))]
        else:
            result[k] = []
    # Build workflow_groups: map group name → list of workflow filenames
    if not kind or kind == "workflows":
        workflow_groups: dict[str, list[str]] = {}
        wf_dir = _config_dir("workflows")
        if wf_dir.exists():
            for wf_file in sorted(wf_dir.glob("*.yaml")):
                try:
                    raw = yaml.safe_load(wf_file.read_text()) or {}
                except yaml.YAMLError:
                    continue
                group = raw.get("metadata", {}).get("group") or raw.get("group")
                if group:
                    workflow_groups.setdefault(str(group), []).append(wf_file.name)
        result["workflow_groups"] = workflow_groups
    return result


@router.get("/files/{kind}/{name}")
async def get_file(kind: str, name: str):
    path = _safe_path(kind, name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        text = path.read_text()
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        content: object = docs[0] if len(docs) == 1 else docs
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"YAML parse error: {exc}") from exc
    return JSONResponse(content=content)


@router.put("/files/{kind}/{name}")
async def put_file(kind: str, name: str, body: dict | list = Body(...)):
    path = _safe_path(kind, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, list):
        path.write_text(
            yaml.dump_all(body, allow_unicode=True, sort_keys=False, explicit_start=True)
        )
    else:
        path.write_text(yaml.dump(body, allow_unicode=True, sort_keys=False))
    return {"saved": str(path)}


@router.delete("/files/{kind}/{name}")
async def delete_file(kind: str, name: str):
    path = _safe_path(kind, name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    path.unlink()
    return {"deleted": str(path)}


def _config_dir(kind: str) -> Path:
    return settings.dir_for(kind)


def _safe_path(kind: str, name: str) -> Path:
    if kind not in _ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {kind!r}")
    if not name.endswith(".yaml") or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid file name.")
    resolved = (_config_dir(kind) / name).resolve()
    allowed = _config_dir(kind).resolve()
    if not str(resolved).startswith(str(allowed)):
        raise HTTPException(status_code=400, detail="Path traversal detected.")
    return resolved


# ── Telemetry config (GET / PUT .tuvl/telemetry.yaml) ────────────────────────

_TELEMETRY_FILE_NAME = "telemetry.yaml"


def _telemetry_path() -> Path:
    return (settings.project_dir / ".tuvl" / _TELEMETRY_FILE_NAME).resolve()


@router.get("/telemetry-config")
async def get_telemetry_config():
    """Return the current telemetry config from .tuvl/telemetry.yaml.

    Returns a 404 when the file does not exist yet (first-run), letting the
    UI fall back to sensible defaults without an error state.
    """
    path = _telemetry_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Telemetry config not set.")
    try:
        content = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"YAML parse error: {exc}") from exc
    # Return flat spec fields whether the file uses the full envelope or flat format.
    spec = content.get("spec", content) if isinstance(content, dict) else content
    return JSONResponse(content=spec)


@router.put("/telemetry-config")
async def put_telemetry_config(body: dict):
    """Persist telemetry config to .tuvl/telemetry.yaml.

    Only the keys ``enabled``, ``otlp_endpoint``, ``otlp_protocol`` and ``service_name`` are
    written — all other keys in the request body are ignored so the endpoint
    cannot be used to write arbitrary data.
    """
    # UI may send flat spec keys or the full envelope; handle both.
    src = body.get("spec", body) if "spec" in body else body
    safe_spec = {
        "enabled": bool(src.get("enabled", True)),
        "otlp_endpoint": str(src.get("otlp_endpoint", "http://localhost:4318")),
        "otlp_protocol": str(src.get("otlp_protocol", "http/protobuf")),
        "service_name": str(src.get("service_name", "tuvl")),
    }
    structured = {
        "kind": "TelemetryConfig",
        "version": "v1",
        "metadata": {"name": "default"},
        "spec": safe_spec,
    }
    path = _telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(structured, allow_unicode=True, sort_keys=False))
    logger.info("dev.telemetry_config.saved", path=str(path))
    return {"saved": str(path)}


# ── System config (GET / PUT .tuvl/system.yaml) ──────────────────────────────


def _system_config_path() -> Path:
    from tuvl.core.system_config import SYSTEM_YAML_RELPATH

    return (settings.project_dir / SYSTEM_YAML_RELPATH).resolve()


@router.get("/system-config")
async def get_system_config():
    """Return the spec of .tuvl/system.yaml.

    404 when the file does not exist (project without ``tuvl init``), letting
    the UI fall back to defaults without an error state.
    """
    path = _system_config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="System config not set.")
    try:
        content = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"YAML parse error: {exc}") from exc
    spec = content.get("spec", {}) if isinstance(content, dict) else {}
    return JSONResponse(content=spec)


def _apply_api_settings(api_cfg: dict, api_src: dict) -> None:
    from tuvl.core.system_config import API_DOCS_MODES, API_GROUPS

    for key in ("expose_model_crud", "dev_expose_all"):
        if key in api_src:
            if api_src[key] is None:
                api_cfg.pop(key, None)
            else:
                api_cfg[key] = bool(api_src[key])
    if "expose" in api_src:
        groups = api_src["expose"]
        if groups is None:
            api_cfg.pop("expose", None)
        elif isinstance(groups, list) and all(g in (*API_GROUPS, "runs") for g in groups):
            api_cfg["expose"] = [g for g in dict.fromkeys(groups) if g != "runs"]
        else:
            raise HTTPException(
                status_code=422, detail=f"'expose' must be a list of {list(API_GROUPS)}"
            )
    if "docs" in api_src:
        docs = api_src["docs"]
        if docs is None:
            api_cfg.pop("docs", None)
        elif docs in API_DOCS_MODES:
            api_cfg["docs"] = docs
        else:
            raise HTTPException(
                status_code=422, detail=f"'docs' must be one of {list(API_DOCS_MODES)}"
            )


@router.put("/system-config")
async def put_system_config(body: dict):
    """Persist API-surface settings to .tuvl/system.yaml.

    Read-modify-write: only the ``spec.api`` keys ``expose_model_crud``, ``dev_expose_all``,
    ``expose`` and ``docs`` are writable. A key that is absent is left alone and a key set
    to ``null`` is removed; ``deployment_mode`` (and any other existing spec key) is
    preserved and extra keys in the request body are ignored. Takes effect on restart.
    """
    src = body.get("spec", body) if "spec" in body else body
    api_src = _v if isinstance(_v := src.get("api"), dict) else src

    path = _system_config_path()
    existing: dict = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except yaml.YAMLError as exc:
            raise HTTPException(
                status_code=422, detail=f"Existing system.yaml is unreadable: {exc}"
            ) from exc

    spec = _v if isinstance(_v := existing.get("spec"), dict) else {}
    api_cfg = _v if isinstance(_v := spec.get("api"), dict) else {}
    _apply_api_settings(api_cfg, api_src)
    spec["api"] = api_cfg

    structured = {
        "kind": existing.get("kind", "SystemConfig"),
        "version": existing.get("version", "v1"),
        "metadata": existing.get("metadata", {"name": "system"}),
        "spec": spec,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(structured, allow_unicode=True, sort_keys=False))
    logger.info("dev.system_config.saved", path=str(path))
    return {"saved": str(path), "restart_required": True}


# ── Project identity (GET / PUT .tuvl/project.yaml) ──────────────────────────


def _project_view(record: dict) -> dict:
    from tuvl.core.project_info import registration_state

    return {**record, "registration_state": registration_state(record)}


def _engine_version() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version("tuvl")
    except importlib.metadata.PackageNotFoundError:
        return ""


@router.get("/project")
async def get_project():
    """The project record; 404 for a project that predates ``tuvl upgrade project``."""
    from tuvl.core.project_info import load_project

    record = load_project(settings.project_dir)
    if record is None or not record.get("id"):
        raise HTTPException(status_code=404, detail="Project info not set.")
    return JSONResponse(content=_project_view(record))


@router.put("/project")
async def put_project(body: dict):
    """Update the editable fields. The id and registry block cannot be written here."""
    from tuvl.core.project_info import (
        EDITABLE_FIELDS,
        ProjectInfoError,
        create_project,
        default_identity,
        load_project,
        update_project,
    )

    if load_project(settings.project_dir) is None:
        who = default_identity(settings.project_dir)
        create_project(
            settings.project_dir,
            name=settings.project_dir.name,
            author=who["author"],
            email=who["email"],
        )
    changes = {k: v for k, v in body.items() if k in EDITABLE_FIELDS}
    try:
        record = update_project(settings.project_dir, changes)
    except ProjectInfoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(content=_project_view(record))


# ── Catalogue registration (device login + register, run by the dev server) ───
# The Nest token never reaches the browser; it goes to the same per-machine cache
# the CLI uses, so `tuvl register project` afterwards needs no second login.


def _registrable_record() -> dict:
    from tuvl.core.project_info import id_is_valid, load_project

    record = load_project(settings.project_dir)
    if record is None or not record.get("id"):
        raise HTTPException(status_code=404, detail="Project info not set.")
    if not id_is_valid(record["id"]):
        raise HTTPException(status_code=422, detail="The project id is malformed.")
    return record


@router.post("/project/register/preview")
async def register_preview(body: dict):
    """The exact payload a registration would send."""
    from tuvl.core.project_info import registration_payload
    from tuvl_insight.cli.nest import nest_url

    record = _registrable_record()
    payload = registration_payload(record, tuvl_version=_engine_version())
    return {"nest": nest_url(body.get("nest")), "payload": payload}


async def _post_registration(url: str, token: str, body: dict):
    import httpx

    from tuvl.core.project_info import record_registration, registration_payload

    record = _registrable_record()
    payload = registration_payload(record, tuvl_version=_engine_version())
    async with httpx.AsyncClient(timeout=30) as http:
        res = await http.post(
            f"{url}/api/register-project",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    if res.status_code == 401:
        return None
    if res.status_code >= 400:
        detail = res.json().get("detail", res.text) if res.content else res.status_code
        raise HTTPException(status_code=502, detail=f"The Nest rejected the registration: {detail}")
    from datetime import UTC, datetime

    updated = record_registration(
        settings.project_dir,
        url,
        when=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return _project_view(updated)


async def _device_start(url: str) -> dict:
    import httpx

    from tuvl_insight.cli.nest import PROVIDER

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            res = await http.post(f"{url}/auth/oauth/{PROVIDER}/device/start")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach {url}: {exc}") from exc
    if res.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"The Nest refused the login ({res.status_code})."
        )
    body: dict[str, Any] = res.json()
    return body


@router.post("/project/register")
async def register_project(body: dict):
    """Register or update the listing. Starts a GitHub device login when none is cached."""
    from tuvl_insight.cli.nest import cached_token, forget_token, nest_url

    url = nest_url(body.get("nest"))
    token = cached_token(url)
    if token:
        done = await _post_registration(url, token, body)
        if done is not None:
            return {"status": "registered", "project": done}
        forget_token(url)
    start = await _device_start(url)
    return {"status": "login_required", "nest": url, **start}


@router.post("/project/register/poll")
async def register_poll(body: dict):
    """Poll the device login; on approval, complete the registration."""
    import httpx

    from tuvl_insight.cli.nest import PROVIDER, nest_url, store_token

    url = nest_url(body.get("nest"))
    handle = str(body.get("handle", ""))
    if not handle:
        raise HTTPException(status_code=422, detail="handle is required")
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            res = await http.post(
                f"{url}/auth/oauth/{PROVIDER}/device/poll", json={"handle": handle}
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach {url}: {exc}") from exc
    if res.status_code == 428:
        slow = res.json().get("detail") == "slow_down"
        return {"status": "pending", "slow_down": slow}
    if res.status_code != 200:
        detail = res.json().get("detail", res.text) if res.content else res.status_code
        raise HTTPException(status_code=502, detail=f"Login failed: {detail}")
    token = str(res.json()["access_token"])
    store_token(url, token)
    done = await _post_registration(url, token, body)
    if done is None:
        raise HTTPException(status_code=502, detail="The Nest did not accept the new login.")
    return {"status": "registered", "project": done}
