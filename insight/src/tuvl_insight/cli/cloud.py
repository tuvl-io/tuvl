"""Talking to a tuvl cloud (cloud.tuvl.io) from the developer toolchain: deploy a project with a client token.

A *client token* (``tvc_<client id>_<secret>``) is made for one customer in the cloud's console. It is the only credential this needs. It is never a
command-line argument (a command line shows in ``ps`` and shell history): it comes from ``TUVL_CLIENT_TOKEN`` (CI), from standard input, or from the
cache ``~/.config/tuvl/cloud.json`` (mode 0600, one entry per cloud address) that ``tuvl deploy --login`` fills. It is sent only over https, or over
plain http to this machine.

The app's administrator password, when one is asked for, is made here, sealed for the cloud's own key (so only the cloud can read it), sent, and shown to
you once at the end. The cloud keeps no copy.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CLOUD_DEFAULT = "https://cloud.tuvl.io"
URL_ENV = "TUVL_CLOUD_URL"
TOKEN_ENV = "TUVL_CLIENT_TOKEN"
TOKEN_RE = re.compile(r"^tvc_[0-9a-f]{16}_[A-Za-z0-9_-]{43}$")
ADMIN_SEAL_NAME = "ADMIN_PASSWORD"
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_"


class CloudError(RuntimeError):
    """The cloud refused, or could not be reached. The message is fit to show."""


def cloud_url(explicit: str | None = None) -> str:
    """The cloud to talk to. Plain http is accepted only for this machine, so a token cannot be sent unencrypted across a network."""
    url = (explicit or os.environ.get(URL_ENV) or CLOUD_DEFAULT).strip().rstrip("/")
    parts = urlparse(url)
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.username
        or parts.query
        or parts.fragment
    ):
        raise CloudError(
            f"{url!r} is not a cloud address: use https://host, for example {CLOUD_DEFAULT}"
        )
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise CloudError(
            f"refusing to send a token over plain http to {parts.hostname}: use https (http is accepted only for localhost)"
        )
    return url


def check_token(token: str) -> str:
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        raise CloudError(
            "that is not a client token: it looks like tvc_0123456789abcdef_ followed by 43 characters. Make one in the cloud's console (Customers, Deploy tokens)."
        )
    return token


# ---- the cache of tokens, one per cloud address


def _cache_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tuvl" / "cloud.json"


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(cache: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cache, fh)
    os.replace(tmp, path)


def stored_token(url: str) -> str | None:
    entry = _read_cache().get(url)
    token = entry.get("token") if isinstance(entry, dict) else None
    return token if isinstance(token, str) and TOKEN_RE.match(token) else None


def store_token(url: str, token: str, *, label: str = "") -> None:
    cache = _read_cache()
    cache[url] = {"token": check_token(token), "label": label[:80], "saved_at": int(time.time())}
    _write_cache(cache)


def forget_token(url: str) -> bool:
    cache = _read_cache()
    had = cache.pop(url, None) is not None
    if had:
        _write_cache(cache)
    return had


def resolve_token(url: str, *, stdin_token: str | None = None) -> tuple[str, str] | None:
    """(token, where it came from): standard input, the environment, then the cache; None when there is none."""
    if stdin_token:
        return check_token(stdin_token), "standard input"
    if os.environ.get(TOKEN_ENV):
        return check_token(os.environ[TOKEN_ENV]), TOKEN_ENV
    cached = stored_token(url)
    return (cached, "the saved login") if cached else None


# ---- sealing the app's administrator password for the cloud


def generate_password(length: int = 20) -> str:
    """A random password without look-alike characters, with at least one lower-case letter, one capital and one digit."""
    while True:
        pw = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
        ):
            return pw


def seal(
    public_pem: str, key_id: str, value: str, *, slug: str, name: str = ADMIN_SEAL_NAME
) -> str:
    """Encrypt *value* so only the cloud can read it: AES-256-GCM for the value, the key wrapped with the cloud's RSA key (OAEP, SHA-256).
    The associated data names the project and the purpose, so the envelope cannot be replayed for another."""
    public = serialization.load_pem_public_key(public_pem.encode())
    if not isinstance(public, rsa.RSAPublicKey):
        raise CloudError("the cloud sent a key this tool cannot use")
    aes_key, iv = AESGCM.generate_key(bit_length=256), os.urandom(12)
    ct = AESGCM(aes_key).encrypt(iv, value.encode(), f"apphost-secret:v1:{slug}:{name}".encode())
    wrapped = public.encrypt(
        aes_key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return json.dumps({"v": 1, "kid": key_id, "wk": b64(wrapped), "iv": b64(iv), "ct": b64(ct)})


# ---- the cloud's client API

_STATUS_TEXT = {
    401: "The cloud did not accept the client token: it may have expired or been revoked. Make a new one in the cloud's console.",
    429: "Too many failed attempts from this address. Wait a few minutes and try again.",
}


class Cloud:
    def __init__(
        self, url: str, token: str, *, http: httpx.Client | None = None, timeout: float = 60.0
    ) -> None:
        self.url = cloud_url(url)
        self._token = check_token(token)
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=False)

    def _call(self, method: str, path: str, **kw: Any) -> dict:
        try:
            r = self._http.request(
                method,
                f"{self.url}/client/v1{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                **kw,
            )
        except httpx.HTTPError:
            raise CloudError(
                f"could not reach {self.url}; check the address and your network"
            ) from None
        if r.status_code in (200, 202):
            try:
                body: dict[str, Any] = r.json()
                return body
            except ValueError:
                raise CloudError(
                    "the cloud answered with something that is not a deployment answer"
                ) from None
        if r.status_code in _STATUS_TEXT:
            raise CloudError(_STATUS_TEXT[r.status_code])
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, str) and r.status_code < 500:
            raise CloudError(detail)
        raise CloudError(f"the cloud had a problem (HTTP {r.status_code}); try again in a moment")

    def me(self) -> dict:
        return self._call("GET", "/me")

    def seal_key(self) -> dict:
        return self._call("GET", "/seal-key")

    def start(
        self,
        archive: Path,
        *,
        plan: str,
        hostname: str = "",
        admin_email: str = "",
        admin_envelope: str = "",
    ) -> dict:
        with open(archive, "rb") as handle:
            data = {
                "plan": plan,
                **({"hostname": hostname} if hostname else {}),
                **(
                    {"admin_email": admin_email, "admin_envelope": admin_envelope}
                    if admin_email
                    else {}
                ),
            }
            return self._call(
                "POST",
                "/deployments",
                data=data,
                files={"archive": (archive.name, handle, "application/gzip")},
                timeout=600.0,
            )

    def check_hostname(self, name: str = "", project: str = "") -> dict:
        """Whether *name* (default: the project's own default name) can be used, and whether it points at the cloud."""
        return self._call("GET", "/hostnames/check", params={"name": name, "project": project})

    def set_hostname(self, project: str, hostname: str) -> dict:
        """Serve a running project under a new name. A deployment job, followed like any other."""
        return self._call("POST", f"/projects/{project}/hostname", json={"hostname": hostname})

    def job(self, job_id: str) -> dict:
        return self._call("GET", f"/deployments/{job_id}")

    def follow(
        self,
        job_id: str,
        on_step: Callable[[str, str], None],
        *,
        interval: float = 2.0,
        timeout: float = 1800.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict:
        """Wait for a deployment to end, calling *on_step(name, detail)* once for each step it reaches. Returns its last state."""
        seen, deadline = 0, clock() + timeout
        while True:
            job = self.job(job_id)
            for step in job.get("steps", [])[seen:]:
                on_step(step.get("name", ""), step.get("detail", ""))
            seen = len(job.get("steps", []))
            if job.get("state") != "running" and job.get("step") in ("done", "failed"):
                return job
            if clock() + interval > deadline:
                raise CloudError(
                    f"the deployment is still running after {int(timeout)} seconds; it continues on the cloud, check it in the console"
                )
            sleep(interval)
