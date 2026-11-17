"""What a provider sign-in keeps between its start and its end.

Two kinds of entry, both short-lived and single-use: a *state* (the CSRF token of an authorization-code
sign-in, with its PKCE verifier) and a *device* handle (a device-flow login in progress).

Where they live follows the rule of the session store (``tuvl.core.auth.blacklist``): in Redis when a
``type: redis`` datasource is deployed and reachable, otherwise in the ``tuvl_system_oauth_states`` table of
the primary database. Either way every worker and replica sees what another one started, so a callback may
land anywhere. Expired rows are removed whenever a new one is stored.

Usage::

    state = await oauth_state_store.new_state("github", {"verifier": v}, session=session)
    payload = await oauth_state_store.consume_state(state, "github", session=session)  # dict, or None
"""

from __future__ import annotations

import datetime
import json
import secrets
from typing import Any

from sqlalchemy import delete
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth.models import IAMOAuthState

_STATE_TTL: int = 600  # seconds (10 min)
_REDIS_KEY_PREFIX = "tuvl:oauth_state:"
_DEVICE_PREFIX = "tuvl:oauth_device:"
_STATE, _DEVICE = "state", "device"
_TABLE = IAMOAuthState.__table__  # type: ignore[attr-defined]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class OAuthStateStore:
    """Single-use sign-in state in Redis or Postgres."""

    @staticmethod
    def _use_redis() -> bool:
        from tuvl.core.infra.redis import is_redis_available

        return is_redis_available()

    @staticmethod
    def _redis() -> Any:
        from tuvl.core.infra.redis import get_redis_client

        return get_redis_client()

    @staticmethod
    def _need(session: AsyncSession | None) -> AsyncSession:
        if session is None:
            raise RuntimeError(
                "Without a Redis datasource the sign-in state is kept in the database."
            )
        return session

    # ── Redis ────────────────────────────────────────────────────────────────

    @staticmethod
    def _decode(raw: Any) -> tuple[str, dict[str, Any]]:
        """(provider, payload) of a stored value; a bare provider name is what 2.0 stored."""
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            entry = json.loads(text)
        except ValueError:
            return text, {}
        if isinstance(entry, dict) and "provider" in entry:
            return str(entry["provider"]), dict(entry.get("payload") or {})
        return text, {}

    # ── Postgres ─────────────────────────────────────────────────────────────

    async def _put(
        self,
        session: AsyncSession,
        handle: str,
        kind: str,
        provider: str,
        payload: dict[str, Any],
        ttl: int,
    ) -> None:
        now = _now()
        await session.exec(delete(_TABLE).where(_TABLE.c.expires_at <= now))
        session.add(
            IAMOAuthState(
                handle=handle,
                kind=kind,
                provider=provider,
                payload=payload,
                expires_at=now + datetime.timedelta(seconds=ttl),
            )
        )
        await session.commit()

    # ── Public API ───────────────────────────────────────────────────────────

    async def new_state(
        self,
        provider: str,
        payload: dict[str, Any] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> str:
        """Store a new CSRF state for *provider* (with *payload*, such as the PKCE verifier) and return it.

        The state goes in the authorization redirect and is checked, once, in the callback.
        """
        state = secrets.token_urlsafe(32)
        body = dict(payload or {})
        if self._use_redis():
            blob = json.dumps({"provider": provider, "payload": body})
            await self._redis().set(f"{_REDIS_KEY_PREFIX}{state}", blob, ex=_STATE_TTL)
        else:
            await self._put(self._need(session), state, _STATE, provider, body, _STATE_TTL)
        return state

    async def consume_state(
        self, state: str, provider: str, *, session: AsyncSession | None = None
    ) -> dict[str, Any] | None:
        """Use up *state*: its payload when it exists, is *provider*'s and has not expired, else ``None``.

        It is deleted in the same step that reads it (one ``GETDEL`` pipeline on Redis, one
        ``DELETE ... RETURNING`` on Postgres), so of any number of simultaneous callbacks only one wins, and
        a state tried for the wrong provider is spent too.
        """
        if self._use_redis():
            redis = self._redis()
            async with redis.pipeline(transaction=True) as pipe:
                pipe.get(f"{_REDIS_KEY_PREFIX}{state}")
                pipe.delete(f"{_REDIS_KEY_PREFIX}{state}")
                raw, _ = await pipe.execute()
            if raw is None:
                return None
            stored_provider, payload = self._decode(raw)
            return payload if stored_provider == provider else None
        db = self._need(session)
        row = (
            await db.exec(
                delete(_TABLE)
                .where(_TABLE.c.handle == state, _TABLE.c.kind == _STATE)
                .returning(_TABLE.c.provider, _TABLE.c.payload, _TABLE.c.expires_at)
            )
        ).first()
        await db.commit()
        if row is None:
            return None
        stored_provider, payload, expires_at = row
        if stored_provider != provider or expires_at <= _now():
            return None
        return dict(payload or {})

    async def new_device(
        self,
        provider: str,
        payload: dict[str, Any],
        *,
        ttl: int,
        session: AsyncSession | None = None,
    ) -> str:
        """Store a device-flow session and return its opaque handle."""
        handle = secrets.token_urlsafe(32)
        if self._use_redis():
            blob = json.dumps({"provider": provider, "payload": payload})
            await self._redis().set(f"{_DEVICE_PREFIX}{handle}", blob, ex=ttl)
        else:
            await self._put(self._need(session), handle, _DEVICE, provider, payload, ttl)
        return handle

    async def get_device(
        self, handle: str, provider: str, *, session: AsyncSession | None = None
    ) -> dict[str, Any] | None:
        """The stored payload for *handle*, or ``None`` if unknown, expired or another provider's."""
        if self._use_redis():
            raw = await self._redis().get(f"{_DEVICE_PREFIX}{handle}")
            if raw is None:
                return None
            stored_provider, payload = self._decode(raw)
            return payload if stored_provider == provider else None
        db = self._need(session)
        row = (
            await db.exec(
                select(_TABLE.c.provider, _TABLE.c.payload).where(
                    _TABLE.c.handle == handle,
                    _TABLE.c.kind == _DEVICE,
                    _TABLE.c.expires_at > _now(),
                )
            )
        ).first()
        if row is None or row[0] != provider:
            return None
        return dict(row[1] or {})

    async def drop_device(self, handle: str, *, session: AsyncSession | None = None) -> None:
        if self._use_redis():
            await self._redis().delete(f"{_DEVICE_PREFIX}{handle}")
            return
        db = self._need(session)
        await db.exec(delete(_TABLE).where(_TABLE.c.handle == handle, _TABLE.c.kind == _DEVICE))
        await db.commit()


# Single process-wide instance used by the federation router.
oauth_state_store = OAuthStateStore()
