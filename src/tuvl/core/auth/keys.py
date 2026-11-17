"""Key lifecycle for tuvl's access tokens (Ed25519-signed JWTs).

Production mode
---------------
Set ``TUVL_SIGNING_KEY`` to the hex-encoded Ed25519 private key (64 hex
characters / 32 bytes). The key is loaded once and reused for the lifetime of
the process, so tokens survive restarts.

Rotation
--------
Set ``TUVL_SIGNING_KEY_PREVIOUS`` to one or more retired private keys (comma-separated, same format).
They sign nothing; their public keys verify tokens already issued and are published in the JWKS, so a
rotation does not sign everyone out. Remove them once the longest token lifetime has passed.

Development mode
----------------
Without a key, a fresh one is generated in memory every time the process
starts. Tokens signed with an ephemeral key are **invalid after restart** — a
warning is emitted at startup to make this visible.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from tuvl.core.logger import logger

ENV_VAR = "TUVL_SIGNING_KEY"
PREVIOUS_ENV_VAR = "TUVL_SIGNING_KEY_PREVIOUS"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _private_key(raw_hex: str, var: str) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw_hex))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{var} is set but is not a 32-byte Ed25519 private key "
            f"(expected 64 hex characters). Details: {exc}"
        ) from exc


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _kid(raw_public: bytes) -> str:
    return _b64url(hashlib.sha256(raw_public).digest()[:12])


def _jwk(raw_public: bytes, kid: str) -> dict[str, str]:
    """A public key as a JWK (RFC 8037)."""
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _b64url(raw_public),
        "kid": kid,
        "alg": "EdDSA",
        "use": "sig",
    }


def generate_private_key_hex() -> str:
    """A fresh 32-byte Ed25519 private key as 64 hex characters."""
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        .hex()
    )


class TuvlKeyManager:
    """The Ed25519 key pair that signs and verifies tuvl's access tokens.

    - ``public_key``  — verifies tokens (safe to share; see ``jwk``).
    - ``private_key`` — signs tokens (never exposed).
    - ``kid``         — key id, stamped in every token header.
    - ``is_ephemeral`` — ``True`` when the key was generated for this process.
    - previous keys   — public halves of ``TUVL_SIGNING_KEY_PREVIOUS``, verify only.
    """

    def __init__(self) -> None:
        raw_hex = os.environ.get(ENV_VAR, "").strip()
        if raw_hex:
            self._private = _private_key(raw_hex, ENV_VAR)
            self.is_ephemeral = False
            logger.info("auth.key.loaded", env_var=ENV_VAR, mode="production", ephemeral=False)
        else:
            from tuvl.core.config import settings as _settings

            if not _settings.tuvl_dev_mode:
                raise RuntimeError(
                    f"{ENV_VAR} must be set when running in production mode. "
                    "Generate a persistent key with: tuvl generate key\n"
                    "Then add it to your .env:\n"
                    f"  {ENV_VAR}=<64-hex-char Ed25519 private key>"
                )
            self._private = Ed25519PrivateKey.generate()
            self.is_ephemeral = True
            logger.warning(
                "auth.key.ephemeral",
                env_var=ENV_VAR,
                reason="no signing key set; tokens will be invalid after process restart",
                hint=f"set {ENV_VAR} in your .env for persistent sessions",
            )
        raw_public = _public_bytes(self.public_key)
        self._raw_public = raw_public
        self.kid = _kid(raw_public)
        self._previous: dict[str, tuple[Ed25519PublicKey, dict[str, str]]] = {}
        for raw in os.environ.get(PREVIOUS_ENV_VAR, "").split(","):
            if not raw.strip():
                continue
            retired = _private_key(raw.strip(), PREVIOUS_ENV_VAR).public_key()
            retired_public = _public_bytes(retired)
            retired_kid = _kid(retired_public)
            if retired_kid != self.kid:  # the current key listed again adds nothing
                self._previous[retired_kid] = (retired, _jwk(retired_public, retired_kid))
        if self._previous:
            logger.info(
                "auth.key.previous_loaded", env_var=PREVIOUS_ENV_VAR, count=len(self._previous)
            )

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private.public_key()

    @property
    def private_key(self) -> Ed25519PrivateKey:
        """Signs tokens. Never log it or send it anywhere."""
        return self._private

    @property
    def jwk(self) -> dict[str, str]:
        """The public key as a JWK (RFC 8037)."""
        return _jwk(self._raw_public, self.kid)

    @property
    def jwks(self) -> list[dict[str, str]]:
        """Every key that verifies a token, the current one first, for ``/auth/jwks.json``."""
        return [self.jwk, *(jwk for _, jwk in self._previous.values())]

    def verification_key(self, kid: object) -> Ed25519PublicKey:
        """The public key a token with key id *kid* is checked against: a retired key when *kid* names
        one, otherwise the current key (so an unknown or missing id fails as a bad signature)."""
        if isinstance(kid, str) and kid in self._previous:
            return self._previous[kid][0]
        return self.public_key


# ── Lazy singleton ────────────────────────────────────────────────────────────
# Constructed on first use rather than at import, so unrelated modules (the CLI,
# the validator) can be imported without a signing key in production mode.
_key_manager_singleton: TuvlKeyManager | None = None


def get_key_manager() -> TuvlKeyManager:
    """The process-wide ``TuvlKeyManager``, constructed on first call."""
    global _key_manager_singleton
    if _key_manager_singleton is None:
        _key_manager_singleton = TuvlKeyManager()
    return _key_manager_singleton


def __getattr__(name: str) -> TuvlKeyManager:
    if name == "key_manager":
        return get_key_manager()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
