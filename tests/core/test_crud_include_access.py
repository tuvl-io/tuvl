"""`?include=` embeds the records of a related model, so it needs the access that model asks of a reader."""

from __future__ import annotations

import pytest

from tests.core.test_secure_fields_crud import make_client, seed  # noqa: F401 - fixture and helper

pytestmark = pytest.mark.asyncio


async def test_an_include_needs_read_access_to_the_related_model(make_client) -> None:  # noqa: F811
    person_id, _ = await seed(make_client)
    reader = make_client("person:read")
    plain = await reader.get(f"/models/person/{person_id}")
    assert plain.status_code == 200 and "pets" not in plain.json()
    refused = await reader.get(f"/models/person/{person_id}?include=pets")
    assert refused.status_code == 403 and "pet:read" in refused.text
    assert (await reader.get("/models/person/?include=pets")).status_code == 403


async def test_with_access_to_both_models_the_include_answers(make_client) -> None:  # noqa: F811
    person_id, _ = await seed(make_client)
    both = make_client("person:read", "pet:read")
    body = (await both.get(f"/models/person/{person_id}?include=pets")).json()
    assert body["pets"][0]["name"] == "Rex" and "microchip" not in body["pets"][0]
    listed = (await both.get("/models/person/?include=pets")).json()
    assert listed[0]["pets"][0]["name"] == "Rex"


async def test_an_administrator_may_include_anything(make_client) -> None:  # noqa: F811
    person_id, _ = await seed(make_client)
    body = (await make_client("iam:admin").get(f"/models/person/{person_id}?include=pets")).json()
    assert body["pets"][0]["microchip"] is not None


async def test_the_many_to_one_direction_is_checked_too(make_client) -> None:  # noqa: F811
    await seed(make_client)
    pet_reader = make_client("pet:read")
    assert (await pet_reader.get("/models/pet/?include=owner")).status_code == 403
    both = make_client("pet:read", "person:read")
    assert (await both.get("/models/pet/?include=owner")).json()[0]["owner"]["name"] == "Ann"
