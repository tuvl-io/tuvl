"""Rotating the signing key without signing everyone out: tokens made with the old key keep verifying
(and its public key stays published) until they would have expired anyway."""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tuvl.core.auth import keys as keys_module
from tuvl.core.auth.access import decode_token
from tuvl.core.auth.keys import (
    ENV_VAR,
    PREVIOUS_ENV_VAR,
    TuvlKeyManager,
    generate_private_key_hex,
)
from tuvl.core.auth.token import mint_token


def install(
    monkeypatch: pytest.MonkeyPatch, current: str, previous: str | None = None
) -> TuvlKeyManager:
    """Make a manager with these keys the process's key manager."""
    monkeypatch.setenv(ENV_VAR, current)
    if previous is None:
        monkeypatch.delenv(PREVIOUS_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(PREVIOUS_ENV_VAR, previous)
    manager = TuvlKeyManager()
    monkeypatch.setattr(keys_module, "_key_manager_singleton", manager)
    return manager


def _claims() -> dict:
    return {"iss": "tuvl", "sub": str(uuid.uuid4()), "groups": [], "scopes": [], "iat": time.time()}


def test_a_token_signed_before_the_rotation_still_verifies(monkeypatch) -> None:
    old, new = generate_private_key_hex(), generate_private_key_hex()
    before = install(monkeypatch, old)
    old_token = mint_token("u1", [], ["a:b"], ttl_seconds=600)

    after = install(monkeypatch, new, previous=old)
    assert decode_token(old_token)["sub"] == "u1"
    fresh = mint_token("u2", [], [], ttl_seconds=600)
    assert jwt.get_unverified_header(fresh)["kid"] == after.kid != before.kid
    assert decode_token(fresh)["sub"] == "u2"


def test_without_the_old_key_the_old_token_is_refused(monkeypatch) -> None:
    old, new = generate_private_key_hex(), generate_private_key_hex()
    install(monkeypatch, old)
    old_token = mint_token("u1", [], [], ttl_seconds=600)
    install(monkeypatch, new)
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(old_token)


def test_the_key_id_picks_the_key_but_the_signature_must_match_it(monkeypatch) -> None:
    old, new = generate_private_key_hex(), generate_private_key_hex()
    old_kid = install(monkeypatch, old).kid
    install(monkeypatch, new, previous=old)
    forged = jwt.encode(
        _claims(), Ed25519PrivateKey.generate(), algorithm="EdDSA", headers={"kid": old_kid}
    )
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(forged)


def test_an_unknown_key_id_is_a_bad_token_not_a_server_error(monkeypatch) -> None:
    install(monkeypatch, generate_private_key_hex(), previous=generate_private_key_hex())
    stranger = jwt.encode(
        _claims(), Ed25519PrivateKey.generate(), algorithm="EdDSA", headers={"kid": "nope"}
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(stranger)


def test_a_token_without_a_key_id_is_checked_against_the_current_key(monkeypatch) -> None:
    manager = install(monkeypatch, generate_private_key_hex(), previous=generate_private_key_hex())
    token = jwt.encode(_claims(), manager.private_key, algorithm="EdDSA")
    assert decode_token(token)["iss"] == "tuvl"


def test_several_previous_keys_are_accepted_trimmed_and_deduplicated(monkeypatch) -> None:
    current, older, oldest = (generate_private_key_hex() for _ in range(3))
    manager = install(monkeypatch, current, previous=f" {older} , {oldest},, {current} ")
    assert len(manager.jwks) == 3  # the current key listed again adds nothing
    assert manager.jwks[0]["kid"] == manager.kid  # the current key comes first


def test_a_malformed_previous_key_fails_loudly(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, generate_private_key_hex())
    monkeypatch.setenv(PREVIOUS_ENV_VAR, "not-hex")
    with pytest.raises(RuntimeError, match=PREVIOUS_ENV_VAR):
        TuvlKeyManager()


async def test_the_jwks_publishes_the_previous_key_too(monkeypatch) -> None:
    from tuvl.core.auth.router import router

    old, new = generate_private_key_hex(), generate_private_key_hex()
    install(monkeypatch, old)
    old_token = mint_token("u1", [], ["s:r"], ttl_seconds=600)
    manager = install(monkeypatch, new, previous=old)
    new_token = mint_token("u2", [], ["s:r"], ttl_seconds=600)

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        keys = (await client.get("/auth/jwks.json")).json()["keys"]
    assert len(keys) == 2 and keys[0]["kid"] == manager.kid and all("d" not in k for k in keys)
    by_kid = {k["kid"]: jwt.PyJWK(k).key for k in keys}
    for token in (old_token, new_token):
        kid = jwt.get_unverified_header(token)["kid"]
        assert jwt.decode(token, by_kid[kid], algorithms=["EdDSA"], issuer="tuvl")["scopes"] == [
            "s:r"
        ]
