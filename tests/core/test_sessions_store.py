"""Revocation and session end: Redis where a Redis datasource is deployed, Postgres otherwise."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import event, func
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth.blacklist import SessionStoreUnavailableError, token_blacklist
from tuvl.core.auth.models import IAMRevokedToken, IAMSessionEnd
from tuvl.core.config import settings
from tuvl.core.datasources import postgres
from tuvl.core.infra import redis as redis_infra

pytestmark = pytest.mark.asyncio

ENDED = "Session ended — sign in again."
REVOKED = "Token has been revoked."


@pytest.fixture
async def engine(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(
            SQLModel.metadata.create_all,
            tables=[IAMSessionEnd.__table__, IAMRevokedToken.__table__],  # type: ignore[attr-defined]
        )

    async def sessions() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(eng, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(postgres, "get_primary_rw_session", sessions)
    monkeypatch.setattr(settings, "tuvl_session_cache_seconds", 0.0)
    token_blacklist.reset_cache()
    yield eng
    await eng.dispose()


def claims(
    sub: str | None = None,
    *,
    iat: float | None = None,
    jti: str | None = None,
    exp: float | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "iss": "tuvl",
        "sub": sub or str(uuid.uuid4()),
        "iat": time.time() if iat is None else iat,
        "jti": jti or uuid.uuid4().hex,
    }
    if exp is not None:
        out["exp"] = exp
    return out


# ── Postgres: the store when no Redis datasource is deployed ──────────────────


async def test_a_session_end_refuses_tokens_issued_before_it_and_only_those(
    engine: AsyncEngine,
) -> None:
    sub = str(uuid.uuid4())
    before = claims(sub, iat=time.time() - 1)
    async with AsyncSession(engine) as s:
        await token_blacklist.end_sessions(sub, None, session=s)
        await s.commit()
    assert await token_blacklist.refusal("t", before) == ENDED
    assert await token_blacklist.refusal("t", claims(sub, iat=time.time() + 1)) is None
    assert await token_blacklist.refusal("t", claims()) is None  # another user


async def test_a_revoked_token_is_refused_by_its_jti(engine: AsyncEngine) -> None:
    c = claims(exp=time.time() + 600)
    assert await token_blacklist.refusal("t", c) is None
    await token_blacklist.revoke("t", claims=c)
    assert await token_blacklist.refusal("t", c) == REVOKED
    assert await token_blacklist.refusal("t", claims(exp=time.time() + 600)) is None


async def test_a_token_with_no_expiry_is_revoked_for_good(engine: AsyncEngine) -> None:
    c = claims()
    await token_blacklist.revoke("t", claims=c)
    async with AsyncSession(engine) as s:
        row = (await s.exec(select(IAMRevokedToken))).one()
    assert row.jti == c["jti"] and row.expires_at_ms is None


async def test_a_revocation_that_has_run_out_is_not_kept(engine: AsyncEngine) -> None:
    await token_blacklist.revoke("a", claims=claims(exp=time.time() - 5))
    live = claims(exp=time.time() + 600)
    await token_blacklist.revoke("b", claims=live)
    async with AsyncSession(engine) as s:
        assert list((await s.exec(select(IAMRevokedToken.jti))).all()) == [live["jti"]]


async def test_a_session_end_is_undone_with_the_transaction_it_belongs_to(
    engine: AsyncEngine,
) -> None:
    sub = str(uuid.uuid4())
    async with AsyncSession(engine) as s:
        await token_blacklist.end_sessions(sub, None, session=s)
        await s.rollback()
    assert await token_blacklist.sessions_ended_at(sub) is None


async def test_a_verdict_is_cached_and_a_committed_write_clears_it(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "tuvl_session_cache_seconds", 60.0)
    statements: list[str] = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, *rest: statements.append(statement),
    )
    sub = str(uuid.uuid4())
    c = claims(sub, iat=time.time() - 1)
    assert await token_blacklist.refusal("t", c) is None
    assert await token_blacklist.refusal("t", c) is None
    assert len(statements) == 1  # the second answer came from the cache
    async with AsyncSession(engine) as s:
        await token_blacklist.end_sessions(sub, None, session=s)
        await s.commit()
    assert await token_blacklist.refusal("t", c) == ENDED


async def test_another_process_sees_a_session_end_at_once_without_a_cache(
    engine: AsyncEngine,
) -> None:
    from tuvl.core.auth.blacklist import TokenBlacklist

    sub = str(uuid.uuid4())
    c = claims(sub, iat=time.time() - 1)
    assert await token_blacklist.refusal("t", c) is None
    async with AsyncSession(engine) as s:  # what `tuvl iam` does, in a process of its own
        await TokenBlacklist().end_sessions(sub, None, session=s)
        await s.commit()
    assert await token_blacklist.refusal("t", c) == ENDED


async def test_a_store_that_does_not_answer_refuses_instead_of_letting_the_token_in(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken() -> AsyncIterator[AsyncSession]:
        raise OSError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(postgres, "get_primary_rw_session", broken)
    with pytest.raises(SessionStoreUnavailableError):
        await token_blacklist.refusal("t", claims())


async def test_no_primary_datasource_means_nothing_to_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing() -> AsyncIterator[AsyncSession]:
        raise postgres.DatasourceNotInitialisedError("Primary datasource 'x' is not initialised.")
        yield  # pragma: no cover

    monkeypatch.setattr(postgres, "get_primary_rw_session", missing)
    monkeypatch.setattr(settings, "tuvl_session_cache_seconds", 0.0)
    for dev_mode in (True, False):
        monkeypatch.setattr(settings, "tuvl_dev_mode", dev_mode)
        assert await token_blacklist.refusal("t", claims()) is None


# ── Redis: the store when a Redis datasource is deployed ──────────────────────


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, tuple[str, int | None]] = {}
        self.mget_calls: list[list[str]] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = (value, ex)

    async def mget(self, keys: list[str]) -> list[str | None]:
        self.mget_calls.append(list(keys))
        return [self.data[k][0] if k in self.data else None for k in keys]


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(redis_infra, "_redis_client", fake)
    monkeypatch.setattr(redis_infra, "_redis_available", True)
    return fake


async def test_with_redis_deployed_revocations_and_session_ends_go_there(
    engine: AsyncEngine, redis: FakeRedis
) -> None:
    c = claims(exp=time.time() + 600)
    await token_blacklist.revoke("raw-token", claims=c)
    key = "tuvl:blacklist:" + hashlib.sha256(b"raw-token").hexdigest()
    assert key in redis.data and 0 < (redis.data[key][1] or 0) <= 600
    assert await token_blacklist.refusal("raw-token", c) == REVOKED

    sub = str(uuid.uuid4())
    await token_blacklist.end_sessions(sub, 300)
    assert redis.data[f"tuvl:sessions-ended:{sub}"][1] == 300
    assert await token_blacklist.refusal("x", claims(sub, iat=time.time() - 1)) == ENDED

    async with AsyncSession(engine) as s:  # nothing went to Postgres
        assert (await s.exec(select(func.count()).select_from(IAMRevokedToken))).one() == 0
        assert (await s.exec(select(func.count()).select_from(IAMSessionEnd))).one() == 0


async def test_a_token_with_no_expiry_is_revoked_in_redis_without_one(redis: FakeRedis) -> None:
    await token_blacklist.revoke("raw", claims=claims())
    assert next(iter(redis.data.values()))[1] is None


async def test_a_check_is_one_round_trip_to_redis(redis: FakeRedis) -> None:
    await token_blacklist.refusal("raw", claims())
    assert len(redis.mget_calls) == 1 and len(redis.mget_calls[0]) == 2
