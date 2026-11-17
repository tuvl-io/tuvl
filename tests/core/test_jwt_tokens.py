"""Access tokens are Ed25519-signed JWTs: the classic JWT attacks are refused, claims are
shape-checked, and the public key is published as a JWKS that verifies real tokens."""

from __future__ import annotations

import base64
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from tuvl.core.auth import keys as keys_module
from tuvl.core.auth.access import decode_token, user_from_claims, verify_bearer_token
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.keys import (
    TuvlKeyManager,
    generate_private_key_hex,
    get_key_manager,
)
from tuvl.core.auth.token import mint_token


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _claims(**over: object) -> dict:
    base = {"iss": "tuvl", "sub": str(uuid.uuid4()), "groups": [], "scopes": [], "iat": time.time()}
    return {**base, **over}


@pytest.fixture(autouse=True)
def _clean_store():
    token_blacklist.reset_cache()


def test_a_minted_token_is_an_eddsa_jwt_with_the_key_id() -> None:
    token = mint_token("u1", ["reviewers"], ["approvals:decide"], ttl_seconds=60)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "EdDSA" and header["kid"] == get_key_manager().kid
    claims = decode_token(token)
    assert claims["iss"] == "tuvl" and claims["sub"] == "u1"
    assert claims["groups"] == ["reviewers"] and claims["scopes"] == ["approvals:decide"]
    # iat is rounded to milliseconds and exp is not, so they can straddle a second boundary
    assert abs(claims["exp"] - claims["iat"] - 60) < 1 and len(claims["jti"]) == 32


def test_alg_none_is_refused() -> None:
    unsigned = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_claims(scopes=['iam:admin']))}."
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(unsigned)


def test_hmac_signed_with_the_public_key_is_refused() -> None:
    """The algorithm-confusion attack: an HS256 token keyed with the (public) verification key."""
    public = get_key_manager().public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    forged = jwt.api_jws.PyJWS().encode(
        json.dumps(_claims(scopes=["iam:admin"])).encode(), public, algorithm="HS256"
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(forged)


def test_a_tampered_payload_is_refused() -> None:
    head, _payload, sig = mint_token("u1", [], [], ttl_seconds=60).split(".")
    escalated = _b64(_claims(sub="u1", scopes=["iam:admin"]))
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(f"{head}.{escalated}.{sig}")


def test_a_token_from_another_key_is_refused() -> None:
    other = jwt.encode(_claims(), Ed25519PrivateKey.generate(), algorithm="EdDSA")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(other)


@pytest.mark.parametrize(
    ("claims", "error"),
    [
        (_claims(iss="someone-else"), jwt.InvalidIssuerError),
        ({k: v for k, v in _claims().items() if k != "sub"}, jwt.MissingRequiredClaimError),
        (_claims(exp=int(time.time()) - 5), jwt.ExpiredSignatureError),
    ],
)
def test_issuer_subject_and_expiry_are_enforced(claims: dict, error: type[Exception]) -> None:
    token = jwt.encode(claims, get_key_manager().private_key, algorithm="EdDSA")
    with pytest.raises(error):
        decode_token(token)


@pytest.mark.parametrize("bad", [{"groups": "admins"}, {"scopes": [1, 2]}, {"tenant": "x; DROP"}])
def test_claims_of_the_wrong_shape_are_refused(bad: dict) -> None:
    with pytest.raises(HTTPException) as exc:
        user_from_claims(_claims(**bad))
    assert exc.value.status_code == 401


async def test_verify_maps_every_rejection_to_401() -> None:
    for token in (
        "garbage",
        jwt.encode(_claims(), Ed25519PrivateKey.generate(), algorithm="EdDSA"),
    ):
        with pytest.raises(HTTPException) as exc:
            await verify_bearer_token(token)
        assert exc.value.status_code == 401


async def test_the_jwks_endpoint_publishes_a_key_that_verifies_tokens() -> None:
    from tuvl.core.auth.router import router

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        jwks = (await client.get("/auth/jwks.json")).json()
    (key,) = jwks["keys"]
    assert key["kty"] == "OKP" and key["crv"] == "Ed25519" and "d" not in key  # public only
    token = mint_token("u1", [], ["s:r"], ttl_seconds=60)
    claims = jwt.decode(token, jwt.PyJWK(key).key, algorithms=["EdDSA"], issuer="tuvl")
    assert claims["scopes"] == ["s:r"]


def test_the_1x_variable_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(keys_module.ENV_VAR, raising=False)
    monkeypatch.setenv("TUVL_BISCUIT_PRIVATE_KEY", generate_private_key_hex())
    monkeypatch.setattr("tuvl.core.config.settings.tuvl_dev_mode", True)
    assert TuvlKeyManager().is_ephemeral  # only TUVL_SIGNING_KEY sets the key
    monkeypatch.setattr("tuvl.core.config.settings.tuvl_dev_mode", False)
    with pytest.raises(RuntimeError, match="TUVL_SIGNING_KEY must be set"):
        TuvlKeyManager()


def test_a_malformed_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(keys_module.ENV_VAR, "not-hex")
    with pytest.raises(RuntimeError, match="TUVL_SIGNING_KEY"):
        TuvlKeyManager()
