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
