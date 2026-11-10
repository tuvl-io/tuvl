"""Talking to a tuvl nest from the CLI: GitHub device login and a small token cache."""

from __future__ import annotations

import contextlib
import json
import os
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

NEST_DEFAULT = "https://nest.tuvl.io"
PROVIDER = "github"

console = Console()


class NestError(RuntimeError):
    """The Nest refused or could not be reached."""


def nest_url(explicit: str | None = None) -> str:
    return (explicit or os.environ.get("TUVL_NEST_URL") or NEST_DEFAULT).rstrip("/")


def _cache_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tuvl" / "nest.json"


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cached_token(url: str) -> str | None:
    entry = _read_cache().get(url)
    return entry.get("token") if isinstance(entry, dict) else None


def store_token(url: str, token: str) -> None:
    cache = _read_cache()
    cache[url] = {"token": token, "saved_at": int(time.time())}
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cache, fh)


def forget_token(url: str) -> None:
    cache = _read_cache()
    if cache.pop(url, None) is not None:
        path = _cache_path()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cache, fh)


def device_start(url: str, http: httpx.Client) -> dict[str, Any]:
    res = http.post(f"{url}/auth/oauth/{PROVIDER}/device/start")
    if res.status_code != 200:
        raise NestError(f"the Nest refused the login request ({res.status_code})")
    body: dict[str, Any] = res.json()
    return body


def device_login(url: str, *, client: httpx.Client | None = None, open_browser: bool = True) -> str:
    """Sign in to the Nest with GitHub's device flow and return its access token."""
    own = client is None
    http = client or httpx.Client(timeout=20)
    try:
        start = device_start(url, http)
        console.print(
            f"\nOpen [bold cyan]{start['verification_uri']}[/bold cyan] and enter "
            f"[bold]{start['user_code']}[/bold]\n"
        )
        if open_browser:
            with contextlib.suppress(Exception):  # headless: the URL above is enough
                webbrowser.open(start["verification_uri"])
        deadline = time.monotonic() + int(start["expires_in"])
        interval = max(1, int(start["interval"]))
        while time.monotonic() < deadline:
            time.sleep(interval)
            poll = http.post(
                f"{url}/auth/oauth/{PROVIDER}/device/poll", json={"handle": start["handle"]}
            )
            if poll.status_code == 200:
                return str(poll.json()["access_token"])
            if poll.status_code == 428:
                if poll.json().get("detail") == "slow_down":
                    interval += 5
                continue
            detail = poll.json().get("detail", poll.text) if poll.content else poll.status_code
            raise NestError(f"login failed: {detail}")
        raise NestError("login timed out; run the command again")
    except httpx.HTTPError as exc:
        raise NestError(f"could not reach {url}: {exc}") from exc
    finally:
        if own:
            http.close()


def ensure_token(url: str, *, relogin: bool = False) -> str:
    token = None if relogin else cached_token(url)
    if token:
        return token
    token = device_login(url)
    store_token(url, token)
    return token
