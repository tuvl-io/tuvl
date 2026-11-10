"""Talking to a tuvl Nest from the developer toolchain: register a project and finish its verification.

Registration is open: there is no sign-in. The Nest returns an *entry secret* on the first registration
(shown once); it is what lets this machine update the listing later, and it is kept in
``~/.config/tuvl/nest.json`` (mode 0600), never in the project. ``TUVL_NEST_ENTRY_SECRET`` supplies one
for CI. Trust in a listing comes from a one-time tag pushed to the project's git remote (see
:mod:`tuvl_insight.gitutil`); the Nest looks for it, or takes this tool's report when it cannot.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

NEST_DEFAULT = "https://nest.tuvl.io"
SECRET_ENV = "TUVL_NEST_ENTRY_SECRET"
REGISTER_PATH = "/api/register-project"
VERIFY_PATH = "/api/verify-project"
#: the only tag names this tool will create or push; the Nest's answer is not trusted to name anything else
TAG_RE = re.compile(r"^tuvl-nest-verify-[0-9a-f]{20}$")

LABEL_TEXT: dict[str, str] = {
    "listed": "Listed: self-declared; nothing about it was checked.",
    "push_confirmed": (
        "Push confirmed: this tool pushed to your remote, but the registry could not see it "
        "(private repository), so it is not independently checked."
    ),
    "verified": "Verified: the registry saw the one-time tag on your public repository.",
}


class NestError(RuntimeError):
    """The Nest refused or could not be reached."""


@dataclass(frozen=True)
class Challenge:
    token: str
    tag: str
    expires_at: str


@dataclass(frozen=True)
class Registration:
    id: str
    created: bool
    claim: bool
    label: str
    entry_secret: str
    challenge: Challenge | None


@dataclass(frozen=True)
class Verification:
    label: str
    applied: bool
    entry_secret: str


def nest_url(explicit: str | None = None) -> str:
    return (explicit or os.environ.get("TUVL_NEST_URL") or NEST_DEFAULT).rstrip("/")


# ── the entry secret ─────────────────────────────────────────────────────────


def _cache_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tuvl" / "nest.json"


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(cache: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cache, fh)


def entry_secret(url: str, project_id: str) -> str | None:
    """The secret for this listing: the environment first (CI), then the local cache."""
    env = os.environ.get(SECRET_ENV, "").strip()
    if env:
        return env
    entry = _read_cache().get(url)
    secret = entry.get(project_id, {}).get("entry_secret") if isinstance(entry, dict) else None
    return str(secret) if secret else None


def store_entry_secret(url: str, project_id: str, secret: str) -> None:
    cache = _read_cache()
    current = cache.get(url)
    entry: dict[str, Any] = current if isinstance(current, dict) else {}
    entry[project_id] = {"entry_secret": secret, "saved_at": int(time.time())}
    cache[url] = entry
    _write_cache(cache)


def forget_entry_secret(url: str, project_id: str) -> None:
    cache = _read_cache()
    entry = cache.get(url)
    if isinstance(entry, dict) and entry.pop(project_id, None) is not None:
        _write_cache(cache)


# ── calls ────────────────────────────────────────────────────────────────────


def _error(res: httpx.Response, what: str) -> NestError:
    detail: Any = res.text
    try:
        body = res.json()
        detail = body.get("error") or body.get("detail") or detail
    except ValueError:
        pass
    return NestError(f"the Nest rejected the {what} ({res.status_code}): {detail}")


def _challenge(raw: Any) -> Challenge | None:
    if not isinstance(raw, dict) or not raw.get("token"):
        return None
    tag = str(raw.get("tag", ""))
    if not TAG_RE.match(tag):
        raise NestError("the Nest sent a challenge tag this tool will not use")
    return Challenge(token=str(raw["token"]), tag=tag, expires_at=str(raw.get("expires_at", "")))


def _parse_registration(body: dict[str, Any]) -> Registration:
    return Registration(
        id=str(body.get("id", "")),
        created=bool(body.get("created")),
        claim=bool(body.get("claim")),
        label=str(body.get("label", "listed")),
        entry_secret=str(body.get("entry_secret", "")),
        challenge=_challenge(body.get("challenge")),
    )


def register(
    url: str, payload: dict[str, Any], *, secret: str | None, client: httpx.Client | None = None
) -> Registration:
    """Register or update the listing. Without the entry secret an id that is already listed becomes a claim."""
    body = dict(payload)
    if secret:
        body["entry_secret"] = secret
    http = client or httpx.Client(timeout=30)
    try:
        res = http.post(f"{url}{REGISTER_PATH}", json=body)
    except httpx.HTTPError as exc:
        raise NestError(f"could not reach {url}: {exc}") from exc
    finally:
        if client is None:
            http.close()
    if res.status_code != 200:
        raise _error(res, "registration")
    return _parse_registration(res.json())


def _parse_verification(body: dict[str, Any]) -> Verification:
    return Verification(
        label=str(body.get("label", "listed")),
        applied=bool(body.get("applied")),
        entry_secret=str(body.get("entry_secret", "")),
    )


def verify(
    url: str,
    project_id: str,
    token: str,
    *,
    pushed: bool,
    client: httpx.Client | None = None,
) -> Verification:
    """Tell the Nest whether the tag was pushed; it looks for the tag itself and answers with the label."""
    http = client or httpx.Client(timeout=40)
    try:
        res = http.post(
            f"{url}{VERIFY_PATH}", json={"id": project_id, "token": token, "pushed": pushed}
        )
    except httpx.HTTPError as exc:
        raise NestError(f"could not reach {url}: {exc}") from exc
    finally:
        if client is None:
            http.close()
    if res.status_code != 200:
        raise _error(res, "verification")
    return _parse_verification(res.json())
