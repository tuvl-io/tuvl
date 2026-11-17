"""AllowedHostsMiddleware boot-time allowlist parsing (TD-12).

Before the fix, ``__init__`` built ``self._allowlist`` but ``dispatch`` ignored
it and re-read + re-parsed ``settings.tuvl_allowed_hosts`` on every request —
so a malformed CIDR written to settings after boot raised a 500 at request
time instead of failing fast at startup, and a settings change after boot
silently changed a running worker's allowlist.
"""

from __future__ import annotations

import pytest

from tuvl.core.access_control import (
    AllowedHostsMiddleware,
    InvalidAllowedHostError,
    host_is_permitted,
    parse_cidr_list,
)


class _DummyApp:
    async def __call__(self, scope, receive, send):  # pragma: no cover - unused
        pass


def _make_request(host: str):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": (host, 12345),
    }
    return Request(scope)


def test_parse_cidr_list_strict_raises_on_malformed():
    with pytest.raises(InvalidAllowedHostError):
        parse_cidr_list("10.0.0.0/24, not-an-ip")


def test_host_is_permitted_explicit_allowlist_ignores_settings(monkeypatch):
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "tuvl_allowed_hosts", "203.0.113.0/24")
    allowlist = parse_cidr_list("10.0.0.0/24")
    assert host_is_permitted("10.0.0.5", allowlist) is True
    assert host_is_permitted("203.0.113.5", allowlist) is False  # not in the passed-in list


def test_host_is_permitted_without_allowlist_falls_back_to_settings(monkeypatch):
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "tuvl_allowed_hosts", "198.51.100.0/24")
    assert host_is_permitted("198.51.100.7") is True
    assert host_is_permitted("10.0.0.5") is False


def test_host_is_permitted_localhost_always_allowed():
    assert host_is_permitted("127.0.0.1", []) is True
    assert host_is_permitted("::1", []) is True


def test_middleware_init_fails_fast_on_malformed_cidr(monkeypatch):
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "tuvl_allowed_hosts", "not-a-cidr")
    with pytest.raises(InvalidAllowedHostError):
        AllowedHostsMiddleware(_DummyApp())


@pytest.mark.asyncio
async def test_dispatch_uses_boot_time_allowlist_not_live_settings(monkeypatch):
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "tuvl_allowed_hosts", "10.0.0.0/24")
    mw = AllowedHostsMiddleware(_DummyApp())

    async def call_next(request):
        return "ok"

    resp = await mw.dispatch(_make_request("10.0.0.5"), call_next)
    assert resp == "ok"

    # Settings mutated after boot must not affect this already-constructed
    # middleware instance's allowlist.
    monkeypatch.setattr(settings, "tuvl_allowed_hosts", "192.0.2.0/24")
    resp2 = await mw.dispatch(_make_request("10.0.0.5"), call_next)
    assert resp2 == "ok"

    # A malformed CIDR written to settings post-boot must not blow up
    # dispatch — it would have, had dispatch still re-parsed per request.
    monkeypatch.setattr(settings, "tuvl_allowed_hosts", "not-a-cidr")
    resp3 = await mw.dispatch(_make_request("10.0.0.5"), call_next)
    assert resp3 == "ok"


@pytest.mark.asyncio
async def test_dispatch_denies_host_outside_boot_time_allowlist(monkeypatch):
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "tuvl_allowed_hosts", "10.0.0.0/24")
    mw = AllowedHostsMiddleware(_DummyApp())

    async def call_next(request):  # pragma: no cover - must not be reached
        raise AssertionError("call_next should not run for a denied host")

    resp = await mw.dispatch(_make_request("8.8.8.8"), call_next)
    assert resp.status_code == 403


_ROUTES_PROBE = """
import json
from fastapi.testclient import TestClient
from tuvl.main import app

def status(client_ip, path):
    return TestClient(app, client=(client_ip, 50000)).get(path).status_code

print(json.dumps({
    "outsider": status("198.51.100.9", "/health"),
    "outsider_insight": status("198.51.100.9", "/insight/"),
    "listed": status("203.0.113.5", "/health"),
    "loopback": status("127.0.0.1", "/health"),
}))
"""


def _statuses(mode: dict[str, str], tmp_path):
    import json
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("TUVL_")}
    env.update(mode)
    env["TUVL_TOOLCHAIN"] = "1"
    done = subprocess.run(
        [sys.executable, "-c", _ROUTES_PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout.strip().splitlines()[-1])


_DEV = {
    "TUVL_ENV": "development",
    "TUVL_DEV_MODE": "true",
    "TUVL_ALLOW_DEV_AUTH": "true",
    "TUVL_DEV_API_KEY": "allowlist-test-key",
}


def test_dev_mode_applies_the_allowlist_to_every_route(tmp_path):
    seen = _statuses({**_DEV, "TUVL_ALLOWED_HOSTS": "203.0.113.0/24"}, tmp_path)
    assert seen["outsider"] == 403 and seen["outsider_insight"] == 403
    assert seen["listed"] == 200 and seen["loopback"] == 200


def test_dev_mode_without_an_allowlist_answers_everyone(tmp_path):
    seen = _statuses(_DEV, tmp_path)
    assert seen["outsider"] == 200
