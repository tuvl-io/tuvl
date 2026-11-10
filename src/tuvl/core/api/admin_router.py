"""Admin state-management endpoints (``iam:admin``).

``GET  /admin/workflows``
    The loaded 2.0 workflows: trigger, models, scope/group and agent count.

``GET  /admin/models`` · ``PATCH /admin/models/{name}/{version}/toggle`` ·
``POST /admin/models/{name}/{version}/fork``
    Model versions and their operational state.

``GET  /admin/scopes``
    Every scope the CRUD router and workflow triggers enforce, so IAM admins
    can compose roles without reading the YAML.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth.access import require_scope
from tuvl.core.datasources.postgres import get_session, primary_ds_name
from tuvl.core.models.model_version import ModelVersion

router = APIRouter(tags=["admin"])

# All /admin/* endpoints require ``iam:admin``; they live on a dedicated
# sub-router so the guard can never be forgotten on an individual route.
admin_router = APIRouter(dependencies=[Depends(require_scope("iam:admin"))])


class ToggleResponse(BaseModel):
    name: str
    schema_version: str
    enabled: bool


class ForkRequest(BaseModel):
    new_version: str


class ForkResponse(BaseModel):
    name: str
    source_version: str
    new_version: str
    file: str


@admin_router.get("/admin/workflows", summary="List the loaded workflows")
async def admin_list_workflows() -> list[dict[str, Any]]:
    from tuvl.contract.registry import WORKFLOWS

    out = []
    for name, doc in sorted(WORKFLOWS.items()):
        http = doc.spec.trigger.http
        out.append(
            {
                "name": name,
                "description": doc.metadata.description,
                "enabled": doc.enabled,
                "trigger_path": http.path if http else None,
                "trigger_method": http.method if http else None,
                "api_enabled": bool(http and http.enabled),
                "schedule": doc.spec.trigger.schedule.cron if doc.spec.trigger.schedule else None,
                "schedule_enabled": doc.spec.trigger.active_schedule is not None,
                "models": list(doc.spec.models),
                "required_scope": doc.metadata.required_scope,
                "required_group": doc.metadata.required_group,
                "agents": len(doc.spec.agents),
            }
        )
    return out


@admin_router.get(
    "/admin/models",
    summary="List all model versions with status",
)
async def admin_list_models(
    session: AsyncSession = Depends(get_session),
) -> dict[str, list[dict[str, Any]]]:
    """Return all model versions grouped by name."""
    result = await session.exec(select(ModelVersion))
    rows: list[ModelVersion] = list(result.all())

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        config = row.get_config()
        fields = [
            {"name": f.get("name", ""), "type": f.get("type", "")}
            for f in config.get("spec", {}).get("fields", [])
            if isinstance(f, dict)
        ]
        grouped.setdefault(row.name, []).append(
            {
                "schema_version": row.schema_version,
                "enabled": row.enabled,
                "fields": fields,
                "tablename": config.get("spec", {}).get("tablename", ""),
                "datasource": config.get("spec", {}).get("datasource") or primary_ds_name(),
                "description": config.get("metadata", {}).get("description"),
            }
        )

    return grouped


@admin_router.patch(
    "/admin/models/{name}/{version}/toggle",
    response_model=ToggleResponse,
    summary="Toggle a model version enabled/disabled",
)
async def admin_toggle_model(
    name: str,
    version: str,
    session: AsyncSession = Depends(get_session),
) -> ToggleResponse:
    """Flip the ``enabled`` flag for model ``(name, version)``.

    The flag is applied to the live registries at the next boot
    (``apply_db_enabled_overrides`` in ``init_db``); it overrides the YAML
    ``enabled`` value without requiring a YAML edit.
    """
    result = await session.exec(
        select(ModelVersion).where(
            ModelVersion.name == name,
            ModelVersion.schema_version == version,
        )
    )
    row: ModelVersion | None = result.first()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{name}' version '{version}' not found.",
        )

    row.enabled = not row.enabled
    session.add(row)
    await session.commit()
    await session.refresh(row)

    return ToggleResponse(
        name=row.name,
        schema_version=row.schema_version,
        enabled=row.enabled,
    )


@admin_router.post(
    "/admin/models/{name}/{version}/fork",
    response_model=ForkResponse,
    summary="Fork a model version to a new version",
)
async def fork_model_version(
    name: str,
    version: str,
    body: ForkRequest,
    session: AsyncSession = Depends(get_session),
) -> ForkResponse:
    """Copy model *name*@*version* config to a new YAML file with
    ``metadata.schema_version`` set to ``body.new_version``.
    """
    from tuvl.core.config import settings
    from tuvl.core.models.loader import MODEL_VERSION_REGISTRY

    versions = MODEL_VERSION_REGISTRY.get(name, {})
    if version not in versions:
        result = await session.exec(
            select(ModelVersion).where(
                ModelVersion.name == name,
                ModelVersion.schema_version == version,
            )
        )
        row = result.first()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{name}' version '{version}' not found.",
            )
        config = row.get_config()
    else:
        config = dict(versions[version])

    new_version = body.new_version.strip()
    if not new_version:
        raise HTTPException(status_code=422, detail="new_version must not be empty.")

    import copy

    forked = copy.deepcopy(config)
    forked.setdefault("metadata", {})["schema_version"] = new_version

    import yaml as _yaml

    models_dir = settings.dir_for("models")
    safe_name = new_version.replace("/", "_").replace(" ", "_")
    filename = f"{name.lower()}_{safe_name}.yaml"
    dest = models_dir / filename
    from fastapi.concurrency import run_in_threadpool

    await run_in_threadpool(
        dest.write_text, _yaml.dump(forked, allow_unicode=True, sort_keys=False)
    )

    return ForkResponse(
        name=name,
        source_version=version,
        new_version=new_version,
        file=filename,
    )


# ── Scope catalogue ───────────────────────────────────────────────────────────


@admin_router.get(
    "/admin/scopes",
    summary="List every scope enforced by CRUD routes and workflow triggers",
)
async def admin_list_scopes() -> dict[str, Any]:
    """Return all scope strings that IAM roles can be granted.

    The response groups scopes by source so the IAM admin knows which
    resource each scope guards:

    Response shape::

        {
          "crud": {
            "Candidate": ["candidate:read", "candidate:write", "candidate:delete"],
            ...
          },
          "workflows": {
            "screen_candidate": ["requisition:write"],
            ...
          },
          "system": ["iam:admin"]
        }

    Convention for CRUD scopes: ``{modelname.lower()}:{read|write|delete}``
    unless overridden in the model YAML ``spec.access`` block.
    Workflow scopes are taken from ``metadata.required_scope`` in the
    workflow YAML; workflows without a ``required_scope`` are not listed.
    """
    from tuvl.core.auth.scopes import scope_catalogue

    return scope_catalogue()


# Mount the iam:admin-guarded admin endpoints onto the public router so they are
# exposed wherever ``router`` is included, but always behind the scope guard.
router.include_router(admin_router)
