"""Insight's index.html is never cached: it names the build's hashed bundles."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _insight_from_source() -> ModuleType:
    """This repo's tuvl_insight (an installed wheel may be older)."""
    path = Path(__file__).resolve().parents[2] / "insight" / "src" / "tuvl_insight" / "__init__.py"
    spec = importlib.util.spec_from_file_location("tuvl_insight_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_index_html_is_served_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tuvl_insight = _insight_from_source()

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html><head></head><body>insight</body></html>")
    monkeypatch.setattr(tuvl_insight, "_STATIC_DIR", tmp_path)
    app = FastAPI()
    tuvl_insight.mount(app)
    client = TestClient(app)
    for path in ("/insight/", "/insight/workflows"):
        response = client.get(path)
        assert response.status_code == 200 and "insight" in response.text
        assert response.headers["cache-control"] == "no-cache"


DEV_KEY = "dev-key-for-the-test"
KEY_TAG = f'<meta name="tuvl-dev-key" content="{DEV_KEY}">'


def _auto_login_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from tuvl.core.config import settings

    tuvl_insight = _insight_from_source()
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html><head></head><body>insight</body></html>")
    monkeypatch.setattr(tuvl_insight, "_STATIC_DIR", tmp_path)
    monkeypatch.setattr(settings, "tuvl_dev_api_key", DEV_KEY)
    monkeypatch.setenv("TUVL_DEV_AUTO_LOGIN", "1")
    app = FastAPI()
    tuvl_insight.mount(app)
    return app


@pytest.mark.parametrize(
    ("client", "host", "gets_the_key"),
    [
        ("127.0.0.1", "localhost:8885", True),
        ("127.0.0.1", "127.0.0.1:8885", True),
        ("::1", "[::1]:8885", True),
        ("203.0.113.9", "localhost:8885", False),  # another machine
        ("203.0.113.9", "203.0.113.5:8885", False),
        ("127.0.0.1", "rebound.example:8885", False),  # a DNS name that resolved to loopback
    ],
)
@pytest.mark.parametrize("path", ["/insight/", "/insight/workflows"])
def test_auto_login_hands_the_key_only_to_this_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
    host: str,
    gets_the_key: bool,
    path: str,
) -> None:
    app = _auto_login_app(tmp_path, monkeypatch)
    response = TestClient(app, base_url=f"http://{host}", client=(client, 50000)).get(path)
    assert response.status_code == 200
    assert (KEY_TAG in response.text) is gets_the_key
    assert DEV_KEY not in response.text or gets_the_key


def test_the_key_is_not_injected_without_auto_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _auto_login_app(tmp_path, monkeypatch)
    monkeypatch.delenv("TUVL_DEV_AUTO_LOGIN")
    response = TestClient(app, base_url="http://localhost:8885", client=("127.0.0.1", 1)).get(
        "/insight/"
    )
    assert DEV_KEY not in response.text
