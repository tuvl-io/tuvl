"""Dev API: the Environment page's endpoints — inventory, write-only secrets, declaring a variable."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI

from tuvl.api.dev import env as env_api
from tuvl.api.dev import router
from tuvl.core.config import use_project


@pytest.fixture
async def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    (tmp_path / "llms").mkdir()
    (tmp_path / "llms" / "m.yaml").write_text(
        "kind: AgentModel\nmetadata: {name: m}\nspec:\n  api_key: ${GEMINI_API_KEY}\n"
        "  api_base: ${GEMINI_BASE:https://x.example}\n"
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "env.yaml").write_text(
        "# the project's variables\nkind: EnvConfig\nspec:\n  variables:\n"
        "    - name: GEMINI_API_KEY\n      description: Gemini key\n      secret: true\n"
        "      required: true\n"
    )
    (tmp_path / ".gitignore").write_text(".env\n")
    for name in ("GEMINI_API_KEY", "GEMINI_BASE", "CRM_TOKEN", "TUVL_API_DOCS"):
        monkeypatch.delenv(name, raising=False)
    env_api._CHANGED.clear()
    use_project(tmp_path)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://dev"
    ) as c:
        yield c


def _by_name(payload: dict) -> dict:
    return {v["name"]: v for v in payload["variables"]}


async def test_the_inventory_lists_what_the_project_needs(client: httpx.AsyncClient) -> None:
    payload = (await client.get("/dev/env")).json()
    rows = _by_name(payload)
    key = rows["GEMINI_API_KEY"]
    assert key["secret"] and key["required"] and key["declared"] and not key["is_set"]
    assert key["referenced_by"] == ["llms/m.yaml"] and key["source"] == "project"
    assert rows["GEMINI_BASE"]["declared"] is False and rows["GEMINI_BASE"]["needed"] is False
    assert payload["dotenv"] == {"path": ".env", "exists": False, "gitignored": True}
    assert payload["manifest_file"] == "config/env.yaml"


async def test_a_secret_is_written_but_never_returned(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    result = await client.put("/dev/env", json={"set": {"GEMINI_API_KEY": "s3cret-value"}})
    assert result.status_code == 200
    assert "s3cret-value" not in result.text
    row = _by_name(result.json())["GEMINI_API_KEY"]
    assert row["is_set"] and row["in_dotenv"] and row["value"] is None and row["restart_needed"]
    from tuvl.core import envconfig

    assert envconfig.read_dotenv(tmp_path)["GEMINI_API_KEY"] == "s3cret-value"
    assert "s3cret-value" not in (await client.get("/dev/env")).text
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


async def test_a_plain_value_is_shown(client: httpx.AsyncClient) -> None:
    await client.put("/dev/env", json={"set": {"GEMINI_BASE": "https://other.example"}})
    row = _by_name((await client.get("/dev/env")).json())["GEMINI_BASE"]
    assert row["value"] == "https://other.example"


async def test_unset_removes_a_variable(client: httpx.AsyncClient, tmp_path: Path) -> None:
    await client.put("/dev/env", json={"set": {"GEMINI_BASE": "x"}})
    await client.put("/dev/env", json={"unset": ["GEMINI_BASE"]})
    assert "GEMINI_BASE" not in (tmp_path / ".env").read_text()


@pytest.mark.parametrize(
    "body, message",
    [
        ({}, "nothing to change"),
        ({"set": {"A": "1"}, "unset": ["A"]}, "both set and unset"),
        ({"set": {"bad name": "1"}}, "not an environment variable name"),
        ({"set": {"A": "line\nbreak"}}, "one line"),
        ({"set": {"TUVL_DEV_API_KEY": "x"}}, "set by `tuvl dev`"),
    ],
)
async def test_bad_updates_are_refused(client: httpx.AsyncClient, body: dict, message: str) -> None:
    result = await client.put("/dev/env", json=body)
    assert result.status_code == 422 and message in result.text


async def test_declaring_a_variable_appends_to_the_manifest_and_keeps_its_comments(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    result = await client.post(
        "/dev/env/declare",
        json={
            "name": "GEMINI_BASE",
            "description": "Where Gemini lives",
            "default": "https://x.example",
        },
    )
    assert result.status_code == 200, result.text
    assert _by_name(result.json())["GEMINI_BASE"]["declared"] is True
    text = (tmp_path / "config" / "env.yaml").read_text()
    assert text.startswith("# the project's variables")
    declared = {v["name"]: v for v in yaml.safe_load(text)["spec"]["variables"]}
    assert declared["GEMINI_BASE"]["default"] == "https://x.example"


async def test_declaring_creates_the_manifest_when_there_is_none(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    (tmp_path / "config" / "env.yaml").unlink()
    result = await client.post("/dev/env/declare", json={"name": "CRM_TOKEN", "secret": True})
    assert result.status_code == 200
    document = yaml.safe_load((tmp_path / "config" / "env.yaml").read_text())
    assert document["kind"] == "EnvConfig"
    assert document["spec"]["variables"] == [{"name": "CRM_TOKEN", "secret": True}]


async def test_declaring_twice_or_declaring_an_engine_variable_is_a_conflict(
    client: httpx.AsyncClient,
) -> None:
    assert (
        await client.post("/dev/env/declare", json={"name": "GEMINI_API_KEY"})
    ).status_code == 409
    assert (await client.post("/dev/env/declare", json={"name": "TUVL_ENV"})).status_code == 409


async def test_a_secret_cannot_be_declared_with_a_value(client: httpx.AsyncClient) -> None:
    result = await client.post(
        "/dev/env/declare", json={"name": "X_TOKEN", "secret": True, "default": "hunter2"}
    )
    assert result.status_code == 422 and "secret and has a default" in result.text
    assert "hunter2" not in (await client.get("/dev/env")).text
