"""Token revocation and session end: what makes a still-valid token stop working.

Where it lives depends on the deployment:

* a ``type: redis`` datasource is deployed and reachable: **Redis** (``SET`` with an expiry);
* otherwise: **Postgres**, on the primary datasource (``tuvl_system_iam_session_ends`` and
  ``tuvl_system_iam_revoked_tokens``).

Either way what one worker, or ``tuvl iam`` in a process of its own, records is seen by every other worker
and survives a restart. There is no per-process fallback: it forgot a logout, and a session ended from the
CLI, as soon as the process that held it was gone.

A token is refused when its ``jti`` was revoked (logout, refresh), or when it was issued before its user's
sessions were last ended (deactivation, deletion, a role or scope taken away, a password change). In
Postgres a write is made in the caller's transaction, so it commits or rolls back with the change that
caused it. A worker keeps its own answer for ``TUVL_SESSION_CACHE_SECONDS`` (5) to spare the database a
read per request; a write in this process, once committed, clears it. Redis answers are not cached.

When the store cannot be reached the token is refused (``SessionStoreUnavailableError``, HTTP 503): never
let in. The one exception is a process with no primary datasource at all, which has nothing to ask: the
check is skipped and a warning says so once (``auth.session_store.none``).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from tuvl.core.config import settings
from tuvl.core.logger import logger

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

_REDIS_KEY_PREFIX = "tuvl:blacklist:"
_SESSIONS_KEY_PREFIX = "tuvl:sessions-ended:"
_CACHE_LIMIT = 10_000
#: what a read or write answers when a dev-mode process has no primary datasource to ask
_SKIPPED: Any = object()

ENDED = "Session ended — sign in again."
REVOKED = "Token has been revoked."


class SessionStoreUnavailableError(RuntimeError):
    """The store that says whether a token is still good cannot be reached."""


async def _execute(session: AsyncSession, statement: Any) -> Any:
    from sqlalchemy.ext.asyncio import AsyncSession as SqlAlchemySession

    return await SqlAlchemySession.execute(session, statement)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _refusal(revoked: bool, ended_at_ms: int | None, issued_ms: int) -> str | None:
    if revoked:
        return REVOKED
    if ended_at_ms is not None and issued_ms < ended_at_ms:
        return ENDED
    return None


def _identity(token: str, claims: Mapping[str, Any] | None) -> tuple[str, float | None]:
    """``(jti, exp)`` of a token the caller has already verified; the hash of the token when it has no ``jti``."""
    if claims is None:
        import jwt

        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            claims = {}
    exp = claims.get("exp")
    return str(claims.get("jti") or _hash_token(token)), (float(exp) if exp is not None else None)


def _upsert(
    session: AsyncSession, table: Any, values: dict[str, Any], key: str, update: bool
) -> Any:
    from sqlalchemy.dialects import postgresql, sqlite

    # SQLite: development databases and the tests
    insert: Any = (
        postgresql.insert if session.get_bind().dialect.name == "postgresql" else sqlite.insert
    )
    stmt = insert(table).values(**values)
    if update:
        return stmt.on_conflict_do_update(
            index_elements=[key], set_={k: v for k, v in values.items() if k != key}
        )
    return stmt.on_conflict_do_nothing(index_elements=[key])


class TokenBlacklist:
    """Async store of revocations and session ends: Redis when deployed, Postgres otherwise."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[float, str | None]] = {}
        self._warned_no_store = False

    # ── which store ───────────────────────────────────────────────────────────

    @staticmethod
    def _use_redis() -> bool:
        from tuvl.core.infra.redis import is_redis_available

        return is_redis_available()

    @staticmethod
    def _redis() -> Any:
        from tuvl.core.infra.redis import get_redis_client

        return get_redis_client()

    def reset_cache(self) -> None:
        self._cache.clear()

    # ── Postgres plumbing ─────────────────────────────────────────────────────

    async def _with_session(
        self, work: Callable[[AsyncSession], Awaitable[Any]], *, commit: bool
    ) -> Any:
        """Run *work* in a session of its own on the primary datasource; ``_SKIPPED``, without running it,
        when a dev-mode process has no primary datasource to ask."""
        from tuvl.core.datasources import postgres

        generator = postgres.get_primary_rw_session()
        try:
            try:
                session = await generator.__anext__()
            except postgres.DatasourceNotInitialisedError:
                self._warn_no_store()
                return _SKIPPED
            result = await work(session)
            if commit:
                await session.commit()
                self._cache.clear()
            return result
        except SessionStoreUnavailableError:
            raise
        except Exception as exc:
            raise SessionStoreUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            await generator.aclose()

    def _warn_no_store(self) -> None:
        if not self._warned_no_store:
            self._warned_no_store = True
            logger.warning(
                "auth.session_store.none",
                detail="no primary datasource and no Redis: a logout or a session end cannot be recorded, "
                "so neither is enforced",
            )

    def _clear_cache_after_commit(self, session: AsyncSession) -> None:
        from sqlalchemy import event

        event.listen(
            session.sync_session, "after_commit", lambda _session: self._cache.clear(), once=True
        )

    # ── writes ────────────────────────────────────────────────────────────────

    async def revoke(
        self,
        token: str,
        ttl_seconds: int | None = None,
        *,
        claims: Mapping[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Refuse *token* from now on, until it would have expired anyway; for good when it has no expiry.

        With *session* (Postgres) the write is part of the caller's transaction, which the caller commits."""
        ident, exp = _identity(token, claims)
        if self._use_redis():
            ttl = max(1, int(exp - time.time())) if exp is not None else ttl_seconds
            await self._redis().set(f"{_REDIS_KEY_PREFIX}{_hash_token(token)}", "1", ex=ttl)
            return

        async def write(s: AsyncSession) -> None:
            from sqlalchemy import delete

            from tuvl.core.auth.models import IAMRevokedToken

            table = IAMRevokedToken.__table__  # type: ignore[attr-defined]
            await _execute(
                s,
                _upsert(
                    s,
                    table,
                    {"jti": ident, "expires_at_ms": int(exp * 1000) if exp is not None else None},
                    "jti",
                    update=False,
                ),
            )
            # whatever has run out is not kept: the table holds only what a token could still use
            now_ms = int(time.time() * 1000)
            await _execute(
                s,
                delete(table).where(
                    table.c.expires_at_ms.is_not(None), table.c.expires_at_ms < now_ms
                ),
            )

        await self._write(session, write)

    async def end_sessions(
        self, user_id: str, ttl_seconds: int | None, *, session: AsyncSession | None = None
    ) -> None:
        """Refuse every token *user_id* was issued before now (deactivation, role changes, deletion). The
        mark lives as long as a token can (*ttl_seconds*; ``None``: tokens never expire, so neither does it)."""
        now_ms = int(time.time() * 1000)
        if self._use_redis():
            await self._redis().set(f"{_SESSIONS_KEY_PREFIX}{user_id}", str(now_ms), ex=ttl_seconds)
            return

        async def write(s: AsyncSession) -> None:
            from tuvl.core.auth.models import IAMSessionEnd

            await _execute(
                s,
                _upsert(
                    s,
                    IAMSessionEnd.__table__,  # type: ignore[attr-defined]
                    {"user_id": user_id, "ended_at_ms": now_ms},
                    "user_id",
                    update=True,
                ),
            )

        await self._write(session, write)

    async def _write(
        self, session: AsyncSession | None, work: Callable[[AsyncSession], Awaitable[None]]
    ) -> None:
        if session is None:
            await self._with_session(work, commit=True)
            return
        try:
            await work(session)
        except Exception as exc:
            raise SessionStoreUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        self._clear_cache_after_commit(session)

    # ── reads ─────────────────────────────────────────────────────────────────

    async def refusal(self, token: str, claims: Mapping[str, Any]) -> str | None:
        """Why *token* may no longer be used (revoked, or issued before its user's sessions ended), or ``None``."""
        sub = str(claims.get("sub", ""))
        issued_ms = int(float(claims.get("iat", 0)) * 1000)
        if self._use_redis():
            revoked, ended = await self._redis().mget(
                [f"{_REDIS_KEY_PREFIX}{_hash_token(token)}", f"{_SESSIONS_KEY_PREFIX}{sub}"]
            )
            return _refusal(
                revoked is not None, int(ended) if ended is not None else None, issued_ms
            )

        ident = str(claims.get("jti") or _hash_token(token))
        keep = settings.tuvl_session_cache_seconds
        if keep > 0:
            hit = self._cache.get((sub, ident))
            if hit is not None and hit[0] > time.monotonic():
                return hit[1]

        async def read(s: AsyncSession) -> str | None:
            import sqlalchemy as sa

            from tuvl.core.auth.models import IAMRevokedToken, IAMSessionEnd

            ends: Any = IAMSessionEnd.__table__  # type: ignore[attr-defined]
            revoked: Any = IAMRevokedToken.__table__  # type: ignore[attr-defined]
            row = (
                await _execute(
                    s,
                    sa.select(
                        sa.select(ends.c.ended_at_ms)
                        .where(ends.c.user_id == sub)
                        .scalar_subquery(),
                        sa.exists().where(revoked.c.jti == ident),
                    ),
                )
            ).one()
            return _refusal(bool(row[1]), row[0], issued_ms)

        reason = await self._with_session(read, commit=False)
        if reason is _SKIPPED:
            return None
        if keep > 0:
            if len(self._cache) >= _CACHE_LIMIT:
                self._cache.clear()
            self._cache[(sub, ident)] = (time.monotonic() + keep, reason)
        return reason  # type: ignore[no-any-return]

    async def is_revoked(self, token: str, claims: Mapping[str, Any] | None = None) -> bool:
        ident, _ = _identity(token, claims)
        if self._use_redis():
            return await self._redis().get(f"{_REDIS_KEY_PREFIX}{_hash_token(token)}") is not None

        async def read(s: AsyncSession) -> bool:
            import sqlalchemy as sa

            from tuvl.core.auth.models import IAMRevokedToken

            revoked: Any = IAMRevokedToken.__table__  # type: ignore[attr-defined]
            found = await _execute(s, sa.select(sa.exists().where(revoked.c.jti == ident)))
            return bool(found.scalar())

        found = await self._with_session(read, commit=False)
        return found is not _SKIPPED and bool(found)

    async def sessions_ended_at(self, user_id: str) -> int | None:
        """When *user_id*'s sessions were last ended (epoch ms), or ``None``."""
        if self._use_redis():
            value = await self._redis().get(f"{_SESSIONS_KEY_PREFIX}{user_id}")
            return int(value) if value is not None else None

        async def read(s: AsyncSession) -> int | None:
            from tuvl.core.auth.models import IAMSessionEnd

            row = await s.get(IAMSessionEnd, user_id)
            return int(row.ended_at_ms) if row is not None else None

        found = await self._with_session(read, commit=False)
        return None if found is _SKIPPED or found is None else int(found)


# Process-wide singleton used by verify_token and the IAM service.
token_blacklist = TokenBlacklist()
