"""Browser access: ``spec.api.cors_origins`` / ``TUVL_CORS_ORIGINS`` and the middleware they turn on."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tuvl.core.config import settings
from tuvl.core.cors import install_cors
from tuvl.core.system_config import cors_origins, invalid_cors_origin, load_system_config
from tuvl_insight.cli.commands.validate import ValidateResult, _validate_system_config

APP = "https://app.example.com"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    load_system_config.cache_clear()
    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tuvl_cors_origins", None)
    yield
    load_system_config.cache_clear()


def _yaml(tmp_path: Path, origins) -> None:
    (tmp_path / ".tuvl").mkdir(exist_ok=True)
    (tmp_path / ".tuvl" / "system.yaml").write_text(
        yaml.safe_dump({"kind": "SystemConfig", "spec": {"api": {"cors_origins": origins}}})
    )
    load_system_config.cache_clear()


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/api/workflows/w/runs")
    def start() -> JSONResponse:
        return JSONResponse({"ok": True}, headers={"X-Tuvl-Run-Id": "r1"})

    @app.get("/api/runs")
    def runs() -> None:
        raise HTTPException(401, "Not authenticated")

    install_cors(app)
    return app


@pytest.mark.parametrize(
    "origin",
    ["*", "https://app.example.com", "http://localhost:5173", "https://[::1]:8443"],
)
def test_a_browser_origin_is_accepted(origin):
    assert invalid_cors_origin(origin) is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.com/",
        "https://app.example.com/app",
        "app.example.com",
        "ftp://app.example.com",
        "https://",
        "https://app.example.com:notaport",
    ],
)
def test_anything_else_is_refused(origin):
    assert invalid_cors_origin(origin)


def test_no_origins_by_default(tmp_path):
    assert cors_origins() == ()
    _yaml(tmp_path, [])
    assert cors_origins() == ()


def test_origins_come_from_system_yaml_and_the_environment_wins(monkeypatch, tmp_path):
    _yaml(tmp_path, [APP, "http://localhost:5173", APP])
    assert cors_origins() == (APP, "http://localhost:5173")
    monkeypatch.setattr(settings, "tuvl_cors_origins", "https://other.example.com, ")
    assert cors_origins() == ("https://other.example.com",)
    monkeypatch.setattr(settings, "tuvl_cors_origins", "")
    assert cors_origins() == ()


def test_a_bad_entry_is_left_out_not_widened(tmp_path):
    _yaml(tmp_path, [f"{APP}/", "https://ok.example.com", 5])
    assert cors_origins() == ("https://ok.example.com",)
    _yaml(tmp_path, APP)  # a string, not a list
    assert cors_origins() == ()


def test_without_origins_no_cors_headers_are_sent():
    client = TestClient(_app())
    response = client.post("/api/workflows/w/runs", headers={"Origin": APP})
    assert "access-control-allow-origin" not in response.headers
    assert (
        client.options(
            "/api/runs", headers={"Origin": APP, "Access-Control-Request-Method": "GET"}
        ).status_code
        == 405
    )


def test_a_preflight_from_an_allowed_origin_is_answered(tmp_path):
    _yaml(tmp_path, [APP])
    response = TestClient(_app()).options(
        "/api/workflows/w/runs",
        headers={
            "Origin": APP,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == APP
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed and "content-type" in allowed
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in response.headers


def test_another_origin_gets_no_permission(tmp_path):
    _yaml(tmp_path, [APP])
    client = TestClient(_app())
    preflight = client.options(
        "/api/workflows/w/runs",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 400 and "access-control-allow-origin" not in preflight.headers
    actual = client.post("/api/workflows/w/runs", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in actual.headers


def test_the_run_id_header_is_readable_by_the_browser(tmp_path):
    _yaml(tmp_path, [APP])
    response = TestClient(_app()).post("/api/workflows/w/runs", headers={"Origin": APP})
    assert response.headers["access-control-allow-origin"] == APP
    assert "x-tuvl-run-id" in response.headers["access-control-expose-headers"].lower()


def test_a_refusal_still_carries_the_headers(tmp_path):
    _yaml(tmp_path, [APP])
    response = TestClient(_app()).get("/api/runs", headers={"Origin": APP})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == APP


def test_the_validator_checks_the_setting(tmp_path):
    def run(origins) -> ValidateResult:
        _yaml(tmp_path, origins)
        result = ValidateResult()
        _validate_system_config(tmp_path, result)
        return result

    assert run([APP]).errors == []
    assert any("trailing slash" in i.message for i in run([f"{APP}/"]).errors)
    assert any("list of origins" in i.message for i in run(APP).errors)
    wildcard = run(["*"])
    assert wildcard.errors == [] and any("any website" in i.message for i in wildcard.warnings)
