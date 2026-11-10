"""Which ``/models/*`` CRUD routes exist: none by default, each operation
enabled per model with ``spec.api.crud``, every route guarded by its
access tier; ``expose_model_crud`` is the deployment-wide master off."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException

from tuvl.core.api.admin_router import admin_list_scopes
from tuvl.core.api.crud_router import build_crud_routers
from tuvl.core.auth.access import Claims, decode_token
from tuvl.core.auth.token import mint_token
from tuvl.core.config import settings
from tuvl.core.models.loader import (
    MODEL_DATASOURCE_MAP,
    MODEL_REGISTRY,
    MODEL_VERSION_REGISTRY,
    SCHEMA_REGISTRY,
    _register_model_from_dict,
)
from tuvl_insight.cli.commands.validate import ValidateResult, _validate_model_api


def _model_dict(name: str, crud: list[str] | None = None, access: dict | None = None) -> dict:
    spec: dict = {
        "tablename": f"{name.lower()}_v1",
        "fields": [
            {"name": "id", "type": "uuid", "primary_key": True},
            {"name": "label", "type": "string"},
        ],
    }
    if crud is not None:
        spec["api"] = {"crud": crud}
    if access:
        spec["access"] = access
    return {"kind": "ModelDefinition", "metadata": {"name": name}, "spec": spec}


@pytest.fixture(autouse=True)
def _clean_registries(monkeypatch):
    monkeypatch.setattr(settings, "tuvl_expose_model_crud", None)
    for registry in (MODEL_REGISTRY, MODEL_VERSION_REGISTRY, SCHEMA_REGISTRY, MODEL_DATASOURCE_MAP):
        registry.clear()
    yield
    for registry in (MODEL_REGISTRY, MODEL_VERSION_REGISTRY, SCHEMA_REGISTRY, MODEL_DATASOURCE_MAP):
        registry.clear()


def _routes(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (method, r.path)
        for r in app.routes
        if getattr(r, "path", "").startswith("/models/")
        for method in getattr(r, "methods", set())
    }


def _mounted(*models: dict) -> FastAPI:
    for model in models:
        _register_model_from_dict(model)
    app = FastAPI()
    build_crud_routers(app)
    return app


def _claims(scopes: list[str]) -> Claims:
    return decode_token(mint_token("u1", [], scopes, ttl_seconds=60))


def test_models_expose_nothing_by_default() -> None:
    assert _routes(_mounted(_model_dict("Quiet"), _model_dict("Empty", crud=[]))) == set()


def test_each_operation_is_enabled_on_its_own() -> None:
    app = _mounted(
        _model_dict("Ticket", crud=["list", "read"]), _model_dict("Note", crud=["create", "delete"])
    )
    assert _routes(app) == {
        ("GET", "/models/ticket/"),
        ("GET", "/models/ticket/{record_id}"),
        ("POST", "/models/note/"),
        ("DELETE", "/models/note/{record_id}"),
    }


def test_every_mounted_route_is_guarded_by_its_tier() -> None:
    app = _mounted(_model_dict("Order", crud=["list", "read", "create", "update", "delete"]))
    tiers = {"GET": "read", "POST": "write", "PATCH": "write", "DELETE": "delete"}
    routes = [r for r in app.routes if getattr(r, "path", "").startswith("/models/")]
    assert len(routes) == 5
    for route in routes:
        [method] = route.methods
        guard = route.dependencies[-1].dependency
        guard(_claims([f"order:{tiers[method]}"]))
        with pytest.raises(HTTPException) as exc:
            guard(_claims(["order:other"]))
        assert exc.value.status_code == 403


def test_master_switch_turns_everything_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tuvl_expose_model_crud", False)
    assert _routes(_mounted(_model_dict("Loud", crud=["list", "read"]))) == set()


@pytest.mark.asyncio
async def test_scope_listing_shows_only_exposed_tiers() -> None:
    _register_model_from_dict(_model_dict("Hidden"))
    _register_model_from_dict(_model_dict("Reader", crud=["list"]))
    _register_model_from_dict(
        _model_dict("Writer", crud=["read", "create"], access={"write_scope": "writer:edit"})
    )
    result = await admin_list_scopes()
    assert result["crud_api_enabled"] is True
    assert result["crud"] == {"Reader": ["reader:read"], "Writer": ["writer:read", "writer:edit"]}


@pytest.mark.asyncio
async def test_scope_listing_empty_when_master_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tuvl_expose_model_crud", False)
    _register_model_from_dict(_model_dict("OffReader", crud=["list"]))
    result = await admin_list_scopes()
    assert result["crud_api_enabled"] is False and result["crud"] == {}


@pytest.mark.parametrize(
    ("api", "problem"),
    [
        ({"crud": ["read", "explode"]}, "unknown operation 'explode'"),
        ({"crud": "read"}, "must be a list"),
        ("all", "must be a mapping"),
    ],
)
def test_validate_rejects_bad_api_blocks(api: object, problem: str) -> None:
    result = ValidateResult()
    _validate_model_api(api, "Thing", "models/thing.yaml", result)
    assert any(problem in str(issue) for issue in result.errors)


def test_validate_accepts_a_good_api_block() -> None:
    result = ValidateResult()
    _validate_model_api({"crud": ["list", "read"]}, "Thing", "models/thing.yaml", result)
    assert not result.errors and not result.warnings
