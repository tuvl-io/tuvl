"""CRUD API semantics: PATCH can clear a field, a list pages in a stable order, a constraint answers 4xx."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.api import crud_router
from tuvl.core.auth.access import dev_claims, verify_token
from tuvl.core.models.loader import (
    MODEL_DATASOURCE_MAP,
    MODEL_REGISTRY,
    MODEL_RELATIONS,
    MODEL_VERSION_REGISTRY,
    SCHEMA_REGISTRY,
    _register_model_from_dict,
)

pytestmark = pytest.mark.asyncio

_REGISTRIES = (MODEL_REGISTRY, MODEL_VERSION_REGISTRY, SCHEMA_REGISTRY, MODEL_RELATIONS)


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    saved = [dict(r) for r in _REGISTRIES]
    saved_ds = dict(MODEL_DATASOURCE_MAP)
    for registry in _REGISTRIES:
        registry.clear()
    MODEL_DATASOURCE_MAP.clear()
    _register_model_from_dict(
        {
            "kind": "ModelDefinition",
            "metadata": {"name": "CrudThing"},
            "spec": {
                "tablename": "crud_things",
                "fields": [
                    {"name": "id", "type": "uuid", "primary_key": True},
                    {"name": "name", "type": "string", "required": True},
                    {"name": "note", "type": "string"},
                    {"name": "code", "type": "string", "unique": True},
                ],
                "api": {"crud": ["list", "read", "create", "update", "delete"]},
            },
        }
    )
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(MODEL_REGISTRY["CrudThing"].__table__.create)  # type: ignore[attr-defined]

    async def session() -> AsyncIterator[AsyncSession]:
        s = AsyncSession(engine, expire_on_commit=False)
        try:
            yield s
        finally:
            await s.close()

    monkeypatch.setattr(crud_router, "model_session", lambda _name: session)
    app = FastAPI()
    app.include_router(crud_router._make_crud_router("CrudThing"))
    app.dependency_overrides[verify_token] = dev_claims
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await engine.dispose()
    for registry, before in zip(_REGISTRIES, saved, strict=True):
        registry.clear()
        registry.update(before)
    MODEL_DATASOURCE_MAP.clear()
    MODEL_DATASOURCE_MAP.update(saved_ds)


async def _create(client: AsyncClient, **body: object) -> dict:
    response = await client.post("/models/crudthing/", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_patch_with_null_clears_a_nullable_field(client: AsyncClient) -> None:
    row = await _create(client, name="a", note="hello")
    response = await client.patch(f"/models/crudthing/{row['id']}", json={"note": None})
    assert response.status_code == 200 and response.json()["note"] is None
    assert (await client.get(f"/models/crudthing/{row['id']}")).json()["note"] is None


async def test_patch_leaves_a_field_it_does_not_name(client: AsyncClient) -> None:
    row = await _create(client, name="a", note="hello")
    response = await client.patch(f"/models/crudthing/{row['id']}", json={"name": "b"})
    assert response.json()["name"] == "b" and response.json()["note"] == "hello"


async def test_patch_with_null_on_a_required_field_is_refused(client: AsyncClient) -> None:
    row = await _create(client, name="a")
    response = await client.patch(f"/models/crudthing/{row['id']}", json={"name": None})
    assert response.status_code == 422 and "name" in response.text


async def test_a_list_pages_in_a_stable_order(client: AsyncClient) -> None:
    made = [(await _create(client, name=f"n{i}"))["id"] for i in range(7)]
    pages: list[str] = []
    for offset in (0, 3, 6):
        page = (
            await client.get("/models/crudthing/", params={"limit": 3, "offset": offset})
        ).json()
        pages += [r["id"] for r in page]
    assert sorted(made) == pages  # ordered by primary key: no repeats, none skipped


async def test_a_duplicate_on_create_is_409_not_500(client: AsyncClient) -> None:
    await _create(client, name="a", code="x")
    response = await client.post("/models/crudthing/", json={"name": "b", "code": "x"})
    assert response.status_code == 409
    assert "x" not in response.text.replace("exists", "")  # the value is not echoed back


async def test_a_duplicate_on_update_is_409(client: AsyncClient) -> None:
    await _create(client, name="a", code="x")
    other = await _create(client, name="b", code="y")
    response = await client.patch(f"/models/crudthing/{other['id']}", json={"code": "x"})
    assert response.status_code == 409


async def test_an_unknown_record_is_still_404(client: AsyncClient) -> None:
    response = await client.patch(f"/models/crudthing/{uuid.uuid4()}", json={"note": None})
    assert response.status_code == 404
