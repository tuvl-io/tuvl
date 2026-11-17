"""Password guesses are slowed per account and per address, and an unknown login is slowed exactly like a known one."""

from __future__ import annotations

import datetime

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth import router as auth_router
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.iam_service import AuthenticationError, ThrottledError
from tuvl.core.auth.models import (
    IAMLoginAttempt,
    IAMRevokedToken,
    IAMRole,
    IAMRoleScope,
    IAMSessionEnd,
    IAMUser,
    IAMUserRole,
)
from tuvl.core.config import settings  # noqa: E402

pytestmark = pytest.mark.asyncio

GOOD = "correct-horse-1"
WRONG = "wrong-password-1"
ANN = "ann@example.com"
TABLES = [
    IAMUser,
    IAMRole,
    IAMUserRole,
    IAMRoleScope,
    IAMSessionEnd,
    IAMRevokedToken,
    IAMLoginAttempt,
]


@pytest.fixture(autouse=True)
def _clean():
    token_blacklist.reset_cache()
    iam_service.reset_login_throttle()
    yield
    iam_service.reset_login_throttle()
    token_blacklist.reset_cache()


@pytest.fixture
def clock(monkeypatch):
    """A frozen clock the tests move by hand."""
    now = [datetime.datetime(2026, 11, 16, 12, 0, 0)]
    monkeypatch.setattr(iam_service, "_now", lambda: now[0])

    def advance(seconds: float) -> None:
        now[0] += datetime.timedelta(seconds=seconds)

    return advance


@pytest.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, tables=[m.__table__ for m in TABLES])
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await iam_service.create_user(s, email=ANN, password=GOOD)
        yield s


async def wrong(session, login: str = ANN, *, ip: str | None = None) -> AuthenticationError:
    with pytest.raises(AuthenticationError) as raised:
        await iam_service.login(session, login_str=login, password=WRONG, client_ip=ip)
    return raised.value


async def waits(session, login: str = ANN, *, ip: str | None = None) -> int:
    """How long the next attempt must wait: it is refused with ``ThrottledError``."""
    with pytest.raises(ThrottledError) as raised:
        await iam_service.login(session, login_str=login, password=GOOD, client_ip=ip)
    return raised.value.retry_after


async def test_five_wrong_passwords_are_free_then_the_account_waits(session, clock) -> None:
    for _ in range(5):
        assert not isinstance(await wrong(session), ThrottledError)
    assert not isinstance(await wrong(session), ThrottledError)  # the sixth is still judged
    assert await waits(session) == 2  # ...and costs two seconds


async def test_the_right_password_is_refused_while_waiting(session, clock) -> None:
    for _ in range(6):
        await wrong(session)
    with pytest.raises(ThrottledError):
        await iam_service.login(session, login_str=ANN, password=GOOD)
    clock(3)
    assert await iam_service.login(session, login_str=ANN, password=GOOD)


async def test_the_wait_doubles_and_stops_at_fifteen_minutes(session, clock) -> None:
    for _ in range(6):
        await wrong(session)
    seen: list[int] = []
    for _ in range(11):
        wait = await waits(session)
        seen.append(wait)
        clock(wait)
        await wrong(session)
    assert seen == [2, 4, 8, 16, 32, 64, 128, 256, 512, 900, 900]


async def test_attempts_made_while_waiting_do_not_lengthen_it(session, clock) -> None:
    for _ in range(6):
        await wrong(session)
    for _ in range(20):
        await waits(session)
    assert await waits(session) == 2
    row = (await session.exec(select(IAMLoginAttempt))).one()
    assert row.failures == 6


async def test_an_unknown_login_is_slowed_exactly_like_a_known_one(session, clock) -> None:
    for _ in range(6):
        await wrong(session, "ghost@example.com")
    assert await waits(session, "ghost@example.com") == 2
    assert await iam_service.login(session, login_str=ANN, password=GOOD)  # not the ghost's doing


async def test_the_login_is_compared_without_case_or_spaces(session, clock) -> None:
    for spelling in ("ANN@example.com", " ann@example.com ", "Ann@Example.com") * 2:
        await wrong(session, spelling)
    assert await waits(session, "ann@EXAMPLE.com") == 2


async def test_a_success_forgets_the_failures(session, clock) -> None:
    for _ in range(4):
        await wrong(session)
    assert await iam_service.login(session, login_str=ANN, password=GOOD)
    assert (await session.exec(select(IAMLoginAttempt))).first() is None
    for _ in range(5):
        assert not isinstance(await wrong(session), ThrottledError)


async def test_unlock_lets_the_account_try_again(session, clock) -> None:
    for _ in range(8):
        await wrong(session)
    await waits(session)
    assert await iam_service.unlock_login(session, "Ann@Example.com") is True
    assert await iam_service.login(session, login_str=ANN, password=GOOD)
    assert await iam_service.unlock_login(session, ANN) is False  # nothing left to clear


async def test_reset_admin_clears_the_lock_it_would_otherwise_sit_behind(session, clock) -> None:
    for _ in range(8):
        await wrong(session)
    await iam_service.reset_admin(session, email=ANN, password="fresh-pass-123")
    assert await iam_service.login(session, login_str=ANN, password="fresh-pass-123")


async def test_one_address_is_limited_across_every_login(session, clock) -> None:
    ip = "203.0.113.9"
    for n in range(settings.tuvl_login_ip_failures):
        await wrong(session, f"user{n}@example.com", ip=ip)
    with pytest.raises(ThrottledError) as raised:
        await iam_service.login(session, login_str=ANN, password=GOOD, client_ip=ip)
    assert 0 < raised.value.retry_after <= iam_service.LOGIN_IP_WINDOW_SECONDS
    assert await iam_service.login(session, login_str=ANN, password=GOOD, client_ip="203.0.113.10")
    clock(iam_service.LOGIN_IP_WINDOW_SECONDS + 1)
    assert await iam_service.login(session, login_str=ANN, password=GOOD, client_ip=ip)


async def test_a_success_from_an_address_does_not_clear_its_failures(session, clock) -> None:
    ip = "203.0.113.9"
    for n in range(settings.tuvl_login_ip_failures):
        await wrong(session, f"user{n}@example.com", ip=ip)
        if n % 5 == 0:
            await iam_service.login(session, login_str=ANN, password=GOOD, client_ip=ip)
    with pytest.raises(ThrottledError):
        await iam_service.login(session, login_str=ANN, password=GOOD, client_ip=ip)


async def test_a_failure_is_audited_without_the_login_or_the_password(session, clock) -> None:
    with structlog.testing.capture_logs() as logs:
        await wrong(session, "Ann@Example.com", ip="203.0.113.9")
        await wrong(session, "ghost@example.com")
    failed = [e for e in logs if e["event"] == "auth.login.failed"]
    assert [(e["known"], e["failures"]) for e in failed] == [(True, 1), (False, 1)]
    assert failed[0]["ip"] == "203.0.113.9" and len(failed[0]["login_hash"]) == 16
    assert failed[0]["login_hash"] != failed[1]["login_hash"]
    text = repr(logs)
    assert "ann@example.com" not in text.lower() and "ghost" not in text and WRONG not in text


async def test_old_failures_are_forgotten(session, clock) -> None:
    for _ in range(3):
        await wrong(session)
    clock(2 * 24 * 3600)
    await wrong(session, "someone-else@example.com")  # any failure sweeps the old rows
    rows = (await session.exec(select(IAMLoginAttempt))).all()
    assert len(rows) == 1 and rows[0].failures == 1


async def test_rest_answers_429_with_retry_after(engine, session, monkeypatch, clock) -> None:
    async def _session():
        s = AsyncSession(engine, expire_on_commit=False)
        try:
            yield s
        finally:
            await s.close()

    import tuvl.core.datasources.postgres as pg

    monkeypatch.setattr(pg, "get_primary_rw_session", _session)
    app = FastAPI()
    app.include_router(auth_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:

        async def attempt(user: str, password: str):
            return await client.post("/auth/token", data={"username": user, "password": password})

        for _ in range(6):
            assert (await attempt(ANN, WRONG)).status_code == 401
        blocked = await attempt(ANN, GOOD)
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "2"
        assert "Too many" in blocked.json()["detail"]
        assert (
            await attempt("other@example.com", WRONG)
        ).status_code == 401  # not the address's doing
        clock(3)
        assert (await attempt(ANN, GOOD)).status_code == 200


async def test_the_table_is_keyed_by_a_hash_not_the_login(session, clock) -> None:
    await wrong(session, "ghost@example.com")
    row = (await session.exec(select(IAMLoginAttempt))).one()
    assert "ghost" not in row.key and len(row.key) == 64


async def test_the_address_limit_can_be_turned_off(session, clock, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tuvl_login_ip_failures", 0)
    for n in range(40):
        await wrong(session, f"user{n}@example.com", ip="203.0.113.9")
    assert await iam_service.login(session, login_str=ANN, password=GOOD, client_ip="203.0.113.9")
