"""A session ended in another process (what ``tuvl iam`` does) is seen here, with no Redis."""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.resilience.conftest import PG_URL
from tuvl.core.auth.blacklist import ENDED, REVOKED, TokenBlacklist, token_blacklist
from tuvl.core.auth.models import IAMRevokedToken, IAMSessionEnd
from tuvl.core.config import settings
from tuvl.core.datasources import postgres

pytestmark = pytest.mark.asyncio

CHILD = """
import asyncio
import sys
import uuid

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service


async def main(url: str, user_id: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    async with AsyncSession(engine) as session:
        await iam_service.end_sessions(session, [uuid.UUID(user_id)])
        await session.commit()
    await engine.dispose()


asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


@pytest.fixture
async def store(pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    tables = [IAMSessionEnd.__table__, IAMRevokedToken.__table__]  # type: ignore[attr-defined]
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=tables))
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=tables))

    async def sessions() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(pg_engine, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(postgres, "get_primary_rw_session", sessions)
    monkeypatch.setattr(settings, "tuvl_session_cache_seconds", 0.0)
    token_blacklist.reset_cache()
    yield
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=tables))


def claims(sub: str) -> dict[str, object]:
    return {"iss": "tuvl", "sub": sub, "iat": time.time() - 1, "jti": uuid.uuid4().hex}


async def test_a_session_ended_in_another_process_is_refused_here(store: None) -> None:
    sub = str(uuid.uuid4())
    c = claims(sub)
    assert await token_blacklist.refusal("t", c) is None
    done = subprocess.run(
        [sys.executable, "-c", CHILD, PG_URL, sub],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert await token_blacklist.refusal("t", c) == ENDED
    assert await TokenBlacklist().refusal("t", c) == ENDED  # a restarted server too


async def test_a_second_session_end_moves_the_mark_forward(store: None) -> None:
    sub = str(uuid.uuid4())
    await token_blacklist.end_sessions(sub, None)
    first = await token_blacklist.sessions_ended_at(sub)
    time.sleep(0.01)
    await token_blacklist.end_sessions(sub, None)
    second = await token_blacklist.sessions_ended_at(sub)
    assert first is not None and second is not None and second > first


async def test_revocation_and_its_purge_work_on_postgres(store: None) -> None:
    gone = claims(str(uuid.uuid4())) | {"exp": time.time() - 5}
    live = claims(str(uuid.uuid4())) | {"exp": time.time() + 600}
    await token_blacklist.revoke("a", claims=gone)
    await token_blacklist.revoke("b", claims=live)
    await token_blacklist.revoke("b", claims=live)  # twice is the same as once
    assert await token_blacklist.refusal("b", live) == REVOKED
    assert await token_blacklist.refusal("a", gone) is None  # run out, so not kept
