"""Browser sign-in through a provider: the callback is bound to the browser that started it, PKCE is used
where the provider takes it, Microsoft gives an email, and what is kept between start and callback lives
in Redis or Postgres rather than in one process."""

from __future__ import annotations

import base64
import datetime
import hashlib
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import federation, state_store
from tuvl.core.auth import router as auth_router
from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY, _register_federation_from_dict
from tuvl.core.auth.models import (
    IAMOAuthState,
    IAMRole,
    IAMRoleScope,
    IAMUser,
    IAMUserRole,
)
from tuvl.core.auth.state_store import oauth_state_store
from tuvl.core.config import settings
from tuvl.core.infra import redis as redis_infra

pytestmark = pytest.mark.asyncio

TABLES = [IAMUser, IAMRole, IAMUserRole, IAMRoleScope, IAMOAuthState]
COOKIE = "tuvl_oauth_state"


class _Resp:
    def __init__(self, data: Any, status: int = 200) -> None:
        self._data, self.status_code = data, status

    def json(self) -> Any:
        return self._data


class Provider:
    """Stands in for a provider's token and userinfo endpoints."""

    def __init__(self) -> None:
        self.profile: dict[str, Any] = {
            "sub": "1",
            "email": "mona@example.com",
            "email_verified": True,
            "given_name": "Mona",
        }
        self.emails: list[dict[str, Any]] = []
        self.token_posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []

    async def post(self, url: str, data: dict[str, Any] | None = None, **_: Any) -> _Resp:
        self.token_posts.append((url, dict(data or {})))
        return _Resp({"access_token": "tok"})

    async def get(self, url: str, **_: Any) -> _Resp:
        self.gets.append(url)
        return _Resp(self.emails if url.endswith("/user/emails") else self.profile)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=[m.__table__ for m in TABLES])  # type: ignore[attr-defined]
    yield eng
    await eng.dispose()


@pytest.fixture
async def api(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
    async def _session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s

    provider = Provider()
    monkeypatch.setattr(federation, "get_http_client", lambda timeout=30.0: provider)
    monkeypatch.setattr(settings, "tuvl_oauth_base_url", "https://nest.test")
    monkeypatch.setattr(settings, "tuvl_oauth_ui_redirect_url", "")
    monkeypatch.setattr(settings, "tuvl_oauth_state_cookie", True)
    snapshot = dict(FEDERATION_REGISTRY)
    FEDERATION_REGISTRY.clear()
    for name in ("google", "microsoft", "github"):
        FEDERATION_REGISTRY[name] = {
            "spec": {"provider": name, "client_id": "cid", "client_secret": "csecret"}
        }
    app = FastAPI()
    app.include_router(federation.router)
    app.dependency_overrides[auth_router._get_iam_session] = _session

    def browser() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="https://t")

    async with browser() as client:
        yield client, provider, browser
    FEDERATION_REGISTRY.clear()
    FEDERATION_REGISTRY.update(snapshot)


def query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


async def start(client: AsyncClient, provider: str = "google"):
    res = await client.get(f"/auth/oauth/{provider}/start", follow_redirects=False)
    assert res.status_code == 302
    return res, query(res.headers["location"])


# ── the callback belongs to the browser that started ──────────────────────────


async def test_start_sets_a_cookie_bound_to_the_state(api) -> None:
    client, _, _ = api
    res, params = await start(client)
    cookie = res.headers["set-cookie"]
    assert f"{COOKIE}={hashlib.sha256(params['state'].encode()).hexdigest()}" in cookie
    lowered = cookie.lower()
    for flag in ("httponly", "samesite=lax", "secure", "path=/auth/oauth", "max-age=600"):
        assert flag in lowered, flag


async def test_the_cookie_is_not_secure_when_the_app_is_served_over_http(api, monkeypatch) -> None:
    client, _, _ = api
    monkeypatch.setattr(settings, "tuvl_oauth_base_url", "http://localhost:8000")
    res, _ = await start(client)
    assert "secure" not in res.headers["set-cookie"].lower()


async def test_the_matching_cookie_signs_in_and_is_cleared(api) -> None:
    client, _, _ = api
    _, params = await start(client)
    done = await client.get(
        "/auth/oauth/google/callback", params={"code": "c", "state": params["state"]}
    )
    assert done.status_code == 200 and done.json()["access_token"]
    assert "max-age=0" in done.headers["set-cookie"].lower()


async def test_another_browser_cannot_finish_a_flow_it_did_not_start(api, engine) -> None:
    client, _, browser = api
    _, params = await start(client)
    async with browser() as victim:  # no cookie: someone made it follow the attacker's link
        refused = await victim.get(
            "/auth/oauth/google/callback", params={"code": "c", "state": params["state"]}
        )
    assert refused.status_code == 400
    async with AsyncSession(engine) as s:
        assert (await s.exec(select(IAMUser))).first() is None
    # and the state is spent: the browser that did start cannot use it afterwards either
    again = await client.get(
        "/auth/oauth/google/callback", params={"code": "c", "state": params["state"]}
    )
    assert again.status_code == 400


async def test_a_cookie_from_another_flow_is_refused(api) -> None:
    client, _, browser = api
    _, first = await start(client)
    async with browser() as other:
        _, second = await start(other)
        crossed = await other.get(
            "/auth/oauth/google/callback", params={"code": "c", "state": first["state"]}
        )
    assert crossed.status_code == 400 and second["state"] != first["state"]


async def test_the_cookie_check_can_be_turned_off(api, monkeypatch) -> None:
    client, _, browser = api
    monkeypatch.setattr(settings, "tuvl_oauth_state_cookie", False)
    res, params = await start(client)
    assert "set-cookie" not in res.headers
    async with browser() as other:
        done = await other.get(
            "/auth/oauth/google/callback", params={"code": "c", "state": params["state"]}
        )
    assert done.status_code == 200


# ── PKCE ───────────────────────────────────────────────────────────────────────


def challenge_of(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.mark.parametrize("name", ["google", "microsoft"])
async def test_pkce_is_used_for_google_and_microsoft(api, name) -> None:
    client, provider, _ = api
    _, params = await start(client, name)
    assert params["code_challenge_method"] == "S256"
    done = await client.get(
        f"/auth/oauth/{name}/callback", params={"code": "c", "state": params["state"]}
    )
    assert done.status_code == 200
    _, sent = provider.token_posts[-1]
    assert challenge_of(sent["code_verifier"]) == params["code_challenge"]


async def test_github_has_no_pkce_unless_the_file_says_so(api) -> None:
    client, provider, _ = api
    _, plain = await start(client, "github")
    assert "code_challenge" not in plain
    FEDERATION_REGISTRY["github"]["spec"]["pkce"] = True
    _, asked = await start(client, "github")
    assert asked["code_challenge_method"] == "S256"
    await client.get("/auth/oauth/github/callback", params={"code": "c", "state": asked["state"]})
    assert challenge_of(provider.token_posts[-1][1]["code_verifier"]) == asked["code_challenge"]


async def test_pkce_can_be_turned_off_for_a_provider(api) -> None:
    client, provider, _ = api
    FEDERATION_REGISTRY["google"]["spec"]["pkce"] = False
    _, params = await start(client, "google")
    assert "code_challenge" not in params
    await client.get("/auth/oauth/google/callback", params={"code": "c", "state": params["state"]})
    assert "code_verifier" not in provider.token_posts[-1][1]


# ── Microsoft, and a provider whose name is not its type ──────────────────────


async def sign_in(client: AsyncClient, name: str):
    _, params = await start(client, name)
    return await client.get(
        f"/auth/oauth/{name}/callback", params={"code": "c", "state": params["state"]}
    )


async def test_microsoft_reads_the_oidc_userinfo_endpoint(api) -> None:
    client, provider, _ = api
    provider.profile = {"sub": "m1", "email": "pat@contoso.com", "given_name": "Pat"}
    assert (await sign_in(client, "microsoft")).status_code == 200
    assert provider.gets[-1] == "https://graph.microsoft.com/oidc/userinfo"


@pytest.mark.parametrize(
    "profile",
    [
        {"id": "g1", "mail": "pat@contoso.com", "givenName": "Pat", "surname": "S"},
        {"id": "g1", "mail": None, "userPrincipalName": "pat@contoso.onmicrosoft.com"},
    ],
    ids=["mail", "userPrincipalName"],
)
async def test_microsoft_graph_me_shapes_still_give_an_email(api, profile) -> None:
    client, provider, _ = api
    FEDERATION_REGISTRY["microsoft"]["spec"]["userinfo_url"] = "https://graph.microsoft.com/v1.0/me"
    provider.profile = profile
    assert (await sign_in(client, "microsoft")).status_code == 200


async def test_a_principal_name_that_is_not_an_address_is_not_an_email(api) -> None:
    client, provider, _ = api
    provider.profile = {"id": "g1", "userPrincipalName": "pat"}
    assert (await sign_in(client, "microsoft")).status_code == 400


async def test_a_github_provider_with_another_name_still_finds_the_private_email(api) -> None:
    client, provider, _ = api
    FEDERATION_REGISTRY["gh-main"] = {
        "spec": {"provider": "github", "client_id": "cid", "client_secret": "csecret"}
    }
    provider.profile = {"id": 7, "login": "octocat", "email": None}
    provider.emails = [{"email": "octo@example.com", "primary": True, "verified": True}]
    assert (await sign_in(client, "gh-main")).status_code == 200
    assert provider.gets[-1].endswith("/user/emails")


# ── what is configured and what is logged ─────────────────────────────────────


def entry(provider: str, **spec: Any) -> dict[str, Any]:
    return {
        "kind": "FederationProvider",
        "metadata": {"name": "p"},
        "spec": {"provider": provider, "client_id": "a", "client_secret": "b", **spec},
    }


async def test_allowed_users_is_refused_where_it_cannot_work() -> None:
    FEDERATION_REGISTRY.pop("p", None)
    with structlog.testing.capture_logs() as logs:
        _register_federation_from_dict(entry("google", allowed_users=["someone"]), "p.yaml")
    assert "p" not in FEDERATION_REGISTRY
    assert [e["event"] for e in logs if e["log_level"] == "error"] == [
        "auth.federation.provider_refused"
    ]
    _register_federation_from_dict(entry("github", allowed_users=["octocat"]), "p.yaml")
    assert "p" in FEDERATION_REGISTRY
    FEDERATION_REGISTRY.pop("p", None)


async def test_the_log_names_the_user_not_the_email(api) -> None:
    client, _, _ = api
    with structlog.testing.capture_logs() as logs:
        assert (await sign_in(client, "google")).status_code == 200
    created = [e for e in logs if e["event"] == "auth.federation.user_created"]
    assert len(created) == 1 and created[0]["user_id"]
    assert "mona@example.com" not in repr(logs)


# ── where the state lives ──────────────────────────────────────────────────────


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s


async def test_a_state_is_single_use_and_carries_its_payload(session) -> None:
    state = await oauth_state_store.new_state("google", {"verifier": "v"}, session=session)
    assert await oauth_state_store.consume_state(state, "google", session=session) == {
        "verifier": "v"
    }
    assert await oauth_state_store.consume_state(state, "google", session=session) is None


async def test_a_state_of_another_provider_is_refused_and_spent(session) -> None:
    state = await oauth_state_store.new_state("google", None, session=session)
    assert await oauth_state_store.consume_state(state, "github", session=session) is None
    assert await oauth_state_store.consume_state(state, "google", session=session) is None


async def test_an_expired_state_is_refused(session, monkeypatch) -> None:
    now = [datetime.datetime(2026, 11, 16, 12, 0, 0)]
    monkeypatch.setattr(state_store, "_now", lambda: now[0])
    state = await oauth_state_store.new_state("google", None, session=session)
    now[0] += datetime.timedelta(seconds=state_store._STATE_TTL + 1)
    assert await oauth_state_store.consume_state(state, "google", session=session) is None


async def test_expired_rows_are_removed_when_a_new_one_is_stored(session, monkeypatch) -> None:
    now = [datetime.datetime(2026, 11, 16, 12, 0, 0)]
    monkeypatch.setattr(state_store, "_now", lambda: now[0])
    for _ in range(5):
        await oauth_state_store.new_state("google", None, session=session)
    now[0] += datetime.timedelta(seconds=state_store._STATE_TTL + 1)
    await oauth_state_store.new_state("google", None, session=session)
    assert len((await session.exec(select(IAMOAuthState))).all()) == 1


async def test_a_state_started_by_one_worker_is_found_by_another(engine, session) -> None:
    state = await oauth_state_store.new_state("google", {"verifier": "v"}, session=session)
    async with AsyncSession(engine, expire_on_commit=False) as elsewhere:
        found = await oauth_state_store.consume_state(state, "google", session=elsewhere)
    assert found == {"verifier": "v"}


async def test_device_handles_live_in_the_same_place(session) -> None:
    handle = await oauth_state_store.new_device(
        "github", {"device_code": "d", "interval": 5}, ttl=900, session=session
    )
    assert await oauth_state_store.get_device(handle, "github", session=session) == {
        "device_code": "d",
        "interval": 5,
    }
    assert await oauth_state_store.get_device(handle, "google", session=session) is None
    await oauth_state_store.drop_device(handle, session=session)
    assert await oauth_state_store.get_device(handle, "github", session=session) is None


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, tuple[str, int | None]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = (value, ex)

    async def get(self, key: str) -> str | None:
        return self.data[key][0] if key in self.data else None

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def pipeline(self, transaction: bool = True) -> FakeRedis._Pipe:
        return FakeRedis._Pipe(self)

    class _Pipe:
        def __init__(self, redis: FakeRedis) -> None:
            self.redis, self.ops = redis, []

        async def __aenter__(self) -> FakeRedis._Pipe:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def get(self, key: str) -> None:
            self.ops.append(("get", key))

        def delete(self, key: str) -> None:
            self.ops.append(("delete", key))

        async def execute(self) -> list[Any]:
            out: list[Any] = []
            for op, key in self.ops:
                out.append(await (self.redis.get(key) if op == "get" else self.redis.delete(key)))
            return out


async def test_with_redis_deployed_the_state_goes_there_and_not_to_postgres(
    session, monkeypatch
) -> None:
    fake = FakeRedis()
    monkeypatch.setattr(redis_infra, "_redis_client", fake)
    monkeypatch.setattr(redis_infra, "_redis_available", True)
    state = await oauth_state_store.new_state("google", {"verifier": "v"}, session=session)
    assert any(key.endswith(state) for key in fake.data)
    assert (await session.exec(select(IAMOAuthState))).first() is None
    assert await oauth_state_store.consume_state(state, "google", session=session) == {
        "verifier": "v"
    }
    assert await oauth_state_store.consume_state(state, "google", session=session) is None
