"""GitHub login for headless clients (device flow), the allowed_users gate and the
fragment-borne browser redirect."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import federation
from tuvl.core.auth import router as auth_router
from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY
from tuvl.core.auth.models import IAMOAuthState, IAMRole, IAMRoleScope, IAMUser, IAMUserRole
from tuvl.core.config import settings

_TABLES = [t.__table__ for t in (IAMUser, IAMRole, IAMUserRole, IAMRoleScope, IAMOAuthState)]


class _Resp:
    def __init__(self, data: Any, status: int = 200) -> None:
        self._data, self.status_code = data, status

    def json(self) -> Any:
        return self._data


class _Github:
    """Stands in for github.com / api.github.com."""

    def __init__(self) -> None:
        self.token_replies: list[dict[str, Any]] = []
        self.login = "octocat"
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, data: dict[str, Any] | None = None, **_: Any) -> _Resp:
        self.posts.append((url, data or {}))
        if url.endswith("/login/device/code"):
            return _Resp(
                {
                    "device_code": "dev-code-secret",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 5,
                    "expires_in": 900,
                }
            )
        return _Resp(self.token_replies.pop(0))

    async def get(self, url: str, **_: Any) -> _Resp:
        if url.endswith("/user"):
            return _Resp(
                {"id": 4242, "login": self.login, "name": "Mona Lisa", "email": "mona@example.com"}
            )
        return _Resp([{"email": "mona@example.com", "primary": True, "verified": True}])


@pytest.fixture
async def api(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=_TABLES)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _session():
        async with maker() as s:
            yield s

    gh = _Github()
    monkeypatch.setattr(federation, "get_http_client", lambda timeout=30.0: gh)
    monkeypatch.setattr(settings, "tuvl_oauth_base_url", "https://nest.test")
    FEDERATION_REGISTRY.clear()
    FEDERATION_REGISTRY["github"] = {
        "spec": {"provider": "github", "client_id": "cid", "client_secret": "csecret"}
    }

    app = FastAPI()
    app.include_router(federation.router)
    app.dependency_overrides[auth_router._get_iam_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as client:
        yield client, gh
    FEDERATION_REGISTRY.clear()


async def test_device_flow_end_to_end(api):
    client, gh = api
    start = await client.post("/auth/oauth/github/device/start")
    assert start.status_code == 200
    body = start.json()
    assert body["user_code"] == "ABCD-1234"
    assert "dev-code-secret" not in start.text  # the device code stays server-side
    handle = body["handle"]

    gh.token_replies = [{"error": "authorization_pending"}, {"access_token": "gho_x"}]
    pending = await client.post("/auth/oauth/github/device/poll", json={"handle": handle})
    assert pending.status_code == 428

    done = await client.post("/auth/oauth/github/device/poll", json={"handle": handle})
    assert done.status_code == 200
    assert done.json()["access_token"]

    again = await client.post("/auth/oauth/github/device/poll", json={"handle": handle})
    assert again.status_code == 400  # a handle is single-use


async def test_poll_with_unknown_handle_is_rejected(api):
    client, _ = api
    res = await client.post("/auth/oauth/github/device/poll", json={"handle": "x" * 32})
    assert res.status_code == 400


async def test_denied_login_drops_the_handle(api):
    client, gh = api
    handle = (await client.post("/auth/oauth/github/device/start")).json()["handle"]
    gh.token_replies = [{"error": "access_denied"}]
    res = await client.post("/auth/oauth/github/device/poll", json={"handle": handle})
    assert res.status_code == 400
    gh.token_replies = [{"access_token": "gho_x"}]
    res = await client.post("/auth/oauth/github/device/poll", json={"handle": handle})
    assert res.status_code == 400


async def test_device_flow_unsupported_for_other_providers(api):
    client, _ = api
    FEDERATION_REGISTRY["google"] = {
        "spec": {"provider": "google", "client_id": "a", "client_secret": "b"}
    }
    res = await client.post("/auth/oauth/google/device/start")
    assert res.status_code == 404


async def test_allowed_users_gate(api):
    client, gh = api
    FEDERATION_REGISTRY["github"]["spec"]["allowed_users"] = ["someone-else"]
    handle = (await client.post("/auth/oauth/github/device/start")).json()["handle"]
    gh.token_replies = [{"access_token": "gho_x"}]
    res = await client.post("/auth/oauth/github/device/poll", json={"handle": handle})
    assert res.status_code == 403

    FEDERATION_REGISTRY["github"]["spec"]["allowed_users"] = ["OctoCat"]
    handle = (await client.post("/auth/oauth/github/device/start")).json()["handle"]
    gh.token_replies = [{"access_token": "gho_x"}]
    res = await client.post("/auth/oauth/github/device/poll", json={"handle": handle})
    assert res.status_code == 200


async def test_browser_redirect_carries_the_token_in_the_fragment(api, monkeypatch):
    client, gh = api
    monkeypatch.setattr(settings, "tuvl_oauth_ui_redirect_url", "https://nest.test/login")
    start = await client.get("/auth/oauth/github/start", follow_redirects=False)
    state = start.headers["location"].split("state=")[1].split("&")[0]
    gh.token_replies = [{"access_token": "gho_x"}]
    res = await client.get(
        "/auth/oauth/github/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("https://nest.test/login#token=")
    assert "?" not in location
