"""`secure: true` fields in the CRUD API: left out of a response unless the caller holds `<model>:read_secure`
(or `iam:admin`), here and in the records `?include=` embeds, and keyed by model rather than by field name."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.api import crud_router
from tuvl.core.auth.access import verify_token
from tuvl.core.models import loader
from tuvl.core.models.loader import (
    MODEL_DATASOURCE_MAP,
    MODEL_REGISTRY,
    MODEL_RELATIONS,
    MODEL_VERSION_REGISTRY,
    SCHEMA_REGISTRY,
    SECURE_FIELDS,
    _register_model_from_dict,
)

pytestmark = pytest.mark.asyncio

_REGISTRIES = (MODEL_REGISTRY, MODEL_VERSION_REGISTRY, SCHEMA_REGISTRY, MODEL_RELATIONS)
CRUD = ["list", "read", "create", "update", "delete"]
SSN = "123-45-6789"
CHIP = "9851-1000-2222"


def model(
    name: str,
    table: str,
    fields: list[dict[str, Any]],
    relations: list[dict[str, Any]] | None = None,
):
    return {
        "kind": "ModelDefinition",
        "metadata": {"name": name},
        "spec": {
            "tablename": table,
            "fields": [{"name": "id", "type": "uuid", "primary_key": True}, *fields],
            "relations": relations or [],
            "api": {"crud": CRUD},
        },
    }


PERSON = model(
    "Person",
    "people",
    [
        {"name": "name", "type": "string", "required": True},
        {"name": "ssn", "type": "string", "secure": True, "required": True},
    ],
    [{"name": "pets", "model": "Pet", "foreign_key": "owner_id", "type": "one_to_many"}],
)
PET = model(
    "Pet",
    "pets",
    [
        {"name": "owner_id", "type": "uuid"},
        {"name": "name", "type": "string"},
        {"name": "microchip", "type": "string", "secure": True},
    ],
    [{"name": "owner", "model": "Person", "foreign_key": "owner_id", "type": "many_to_one"}],
)
NOTE = model("Note", "notes", [{"name": "ssn", "type": "string"}])  # same name, not secure


@pytest.fixture
async def make_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Callable[..., AsyncClient]]:
    saved = [dict(r) for r in _REGISTRIES]
    saved_ds = dict(MODEL_DATASOURCE_MAP)
    saved_secure = set(SECURE_FIELDS)
    for registry in _REGISTRIES:
        registry.clear()
    MODEL_DATASOURCE_MAP.clear()
    SECURE_FIELDS.clear()
    getattr(loader, "SECURE_BY_MODEL", {}).clear()
    for document in (PERSON, PET, NOTE):
        _register_model_from_dict(document)
    engine: AsyncEngine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        for name in ("Person", "Pet", "Note"):
            await conn.run_sync(MODEL_REGISTRY[name].__table__.create)  # type: ignore[attr-defined]

    async def session() -> AsyncIterator[AsyncSession]:
        s = AsyncSession(engine, expire_on_commit=False)
        try:
            yield s
        finally:
            await s.close()

    monkeypatch.setattr(crud_router, "model_session", lambda _name: session)
    clients: list[AsyncClient] = []
    app = FastAPI()
    for name in ("Person", "Pet", "Note"):
        app.include_router(crud_router._make_crud_router(name))
    holder: dict[str, list[str]] = {"scopes": []}

    def claims() -> dict[str, Any]:
        return {
            "iss": "tuvl",
            "sub": "caller",
            "groups": [],
            "scopes": list(holder["scopes"]),
            "iat": time.time(),
        }

    app.dependency_overrides[verify_token] = claims

    def client(*scopes: str) -> AsyncClient:
        """A client whose token carries *scopes* (the next request uses the latest call's scopes)."""
        holder["scopes"] = list(scopes)
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    client.app = app  # type: ignore[attr-defined]
    client.use = lambda *scopes: holder.__setitem__("scopes", list(scopes))  # type: ignore[attr-defined]
    yield client
    for c in clients:
        await c.aclose()
    await engine.dispose()
    for registry, before in zip(_REGISTRIES, saved, strict=True):
        registry.clear()
        registry.update(before)
    MODEL_DATASOURCE_MAP.clear()
    MODEL_DATASOURCE_MAP.update(saved_ds)
    SECURE_FIELDS.clear()
    SECURE_FIELDS.update(saved_secure)
    getattr(loader, "SECURE_BY_MODEL", {}).clear()


async def seed(make_client: Callable[..., AsyncClient]) -> tuple[str, str]:
    """An owner with a secure SSN and a pet with a secure microchip, written by an administrator."""
    admin = make_client("iam:admin")
    person = (await admin.post("/models/person/", json={"name": "Ann", "ssn": SSN})).json()
    pet = (
        await admin.post(
            "/models/pet/", json={"owner_id": person["id"], "name": "Rex", "microchip": CHIP}
        )
    ).json()
    return person["id"], pet["id"]


async def test_a_reader_without_the_secure_scope_gets_no_secure_field(make_client) -> None:
    person_id, _ = await seed(make_client)
    reader = make_client("person:read")
    one = (await reader.get(f"/models/person/{person_id}")).json()
    assert one["name"] == "Ann" and "ssn" not in one
    listed = (await reader.get("/models/person/")).json()
    assert [p["name"] for p in listed] == ["Ann"] and all("ssn" not in p for p in listed)
    assert SSN not in str(one) + str(listed)


async def test_the_secure_scope_or_an_administrator_gets_it(make_client) -> None:
    person_id, _ = await seed(make_client)
    for scopes in (("person:read", "person:read_secure"), ("iam:admin",)):
        caller = make_client(*scopes)
        assert (await caller.get(f"/models/person/{person_id}")).json()["ssn"] == SSN
        assert (await caller.get("/models/person/")).json()[0]["ssn"] == SSN


async def test_the_secure_scope_alone_does_not_let_a_caller_read(make_client) -> None:
    person_id, _ = await seed(make_client)
    caller = make_client("person:read_secure")
    assert (await caller.get(f"/models/person/{person_id}")).status_code == 403


async def test_a_writer_is_not_handed_back_the_secure_values(make_client) -> None:
    writer = make_client("person:write")
    created = await writer.post("/models/person/", json={"name": "Bob", "ssn": "987-65-4321"})
    assert created.status_code == 201 and "ssn" not in created.json()
    person_id = created.json()["id"]
    patched = await writer.patch(f"/models/person/{person_id}", json={"name": "Bobby"})
    assert patched.status_code == 200 and patched.json()["name"] == "Bobby"
    assert "ssn" not in patched.json() and "987-65-4321" not in patched.text
    reader = make_client("person:read", "person:read_secure")  # it was stored
    assert (await reader.get(f"/models/person/{person_id}")).json()["ssn"] == "987-65-4321"


async def test_embedded_records_follow_their_own_models_scope(make_client) -> None:
    person_id, _ = await seed(make_client)
    plain = (
        await make_client("person:read", "pet:read").get(f"/models/person/{person_id}?include=pets")
    ).json()
    assert "ssn" not in plain and plain["pets"][0]["name"] == "Rex"
    assert "microchip" not in plain["pets"][0]

    own_only = make_client("person:read", "person:read_secure", "pet:read")
    mixed = (await own_only.get(f"/models/person/{person_id}?include=pets")).json()
    assert mixed["ssn"] == SSN and "microchip" not in mixed["pets"][0]

    pets_only = make_client("person:read", "pet:read", "pet:read_secure")
    other = (await pets_only.get(f"/models/person/{person_id}?include=pets")).json()
    assert "ssn" not in other and other["pets"][0]["microchip"] == CHIP

    listed = (await make_client("pet:read", "person:read").get("/models/pet/?include=owner")).json()
    assert "microchip" not in listed[0] and "ssn" not in listed[0]["owner"]
    assert (await make_client("iam:admin").get("/models/pet/?include=owner")).json()[0]["owner"][
        "ssn"
    ] == SSN


async def test_filtering_on_a_secure_field_stays_refused(make_client) -> None:
    await seed(make_client)
    caller = make_client("person:read", "person:read_secure")
    refused = await caller.get(f"/models/person/?filter[ssn]={SSN}")
    assert refused.status_code == 422 and "ssn" in refused.text


async def test_the_read_schema_marks_secure_fields_optional(make_client) -> None:
    schema = make_client.app.openapi()["components"]["schemas"]
    person = schema["PersonRead"]
    assert "ssn" in person["properties"] and "ssn" not in person.get("required", [])
    assert "name" in person["required"]  # a field that is not secure stays as the model declares it
    assert "omitted" in person["properties"]["ssn"].get("description", "").lower()


async def test_a_field_of_another_model_with_the_same_name_is_not_hidden(make_client) -> None:
    await seed(make_client)
    writer = make_client("note:write", "note:read")
    made = await writer.post("/models/note/", json={"ssn": "not secret here"})
    assert made.json()["ssn"] == "not secret here"
    assert (await writer.get("/models/note/")).json()[0]["ssn"] == "not secret here"


async def test_the_registry_is_keyed_by_model_and_field(make_client) -> None:
    by_model = loader.SECURE_BY_MODEL  # type: ignore[attr-defined]
    assert by_model["Person"] == frozenset({"ssn"}) and by_model["Pet"] == frozenset({"microchip"})
    assert "Note" not in by_model
    assert {
        "ssn",
        "microchip",
    } == SECURE_FIELDS  # the union of names, for data that carries no model


async def test_dev_mode_callers_see_everything(make_client) -> None:
    person_id, _ = await seed(make_client)
    from tuvl.core.auth.access import dev_claims

    make_client.app.dependency_overrides[verify_token] = dev_claims
    c = make_client()
    assert (await c.get(f"/models/person/{person_id}")).json()["ssn"] == SSN
