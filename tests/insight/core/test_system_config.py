"""``.tuvl/system.yaml`` resolution — ``load_system_config`` / ``expose_model_crud``,
and the dev-mode GET/PUT ``/dev/system-config`` endpoints that edit it."""

from __future__ import annotations

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tuvl.core.config import settings
from tuvl.core.system_config import expose_model_crud, load_system_config
from tuvl_insight.api.dev_core_router import router as dev_router


@pytest.fixture(autouse=True)
def _clear_cache():
    load_system_config.cache_clear()
    yield
    load_system_config.cache_clear()


def _use_project_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))


def _write_system_yaml(tmp_path, text: str) -> None:
    path = tmp_path / ".tuvl" / "system.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ── load_system_config / expose_model_crud ───────────────────────────────────


def test_expose_model_crud_true_when_file_absent(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    assert expose_model_crud() is True


def test_expose_model_crud_false_from_yaml(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    _write_system_yaml(
        tmp_path, "kind: SystemConfig\nspec:\n  api:\n    expose_model_crud: false\n"
    )
    assert expose_model_crud() is False


def test_expose_model_crud_true_from_yaml(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    _write_system_yaml(tmp_path, "kind: SystemConfig\nspec:\n  api:\n    expose_model_crud: true\n")
    assert expose_model_crud() is True


def test_malformed_yaml_fails_open(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    _write_system_yaml(tmp_path, "kind: [unterminated\n")
    assert expose_model_crud() is True


def test_env_override_beats_yaml_false(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    _write_system_yaml(tmp_path, "spec:\n  api:\n    expose_model_crud: false\n")
    monkeypatch.setattr(settings, "tuvl_expose_model_crud", True)
    assert expose_model_crud() is True


def test_env_override_beats_yaml_true(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    _write_system_yaml(tmp_path, "spec:\n  api:\n    expose_model_crud: true\n")
    monkeypatch.setattr(settings, "tuvl_expose_model_crud", False)
    assert expose_model_crud() is False


# ── /dev/system-config GET/PUT ────────────────────────────────────────────────


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(dev_router)
    return TestClient(app)


def test_get_system_config_404_when_absent(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    resp = _client().get("/dev/system-config")
    assert resp.status_code == 404


def test_get_system_config_returns_spec(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    _write_system_yaml(
        tmp_path,
        "kind: SystemConfig\nspec:\n  deployment_mode: single_tenant\n  api:\n    expose_model_crud: true\n",
    )
    resp = _client().get("/dev/system-config")
    assert resp.status_code == 200
    assert resp.json() == {"deployment_mode": "single_tenant", "api": {"expose_model_crud": True}}


def test_put_system_config_preserves_deployment_mode_and_sets_flag(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    _write_system_yaml(tmp_path, "kind: SystemConfig\nspec:\n  deployment_mode: single_tenant\n")
    resp = _client().put("/dev/system-config", json={"api": {"expose_model_crud": False}})
    assert resp.status_code == 200
    assert resp.json()["restart_required"] is True

    on_disk = yaml.safe_load((tmp_path / ".tuvl" / "system.yaml").read_text())
    assert on_disk["spec"]["deployment_mode"] == "single_tenant"
    assert on_disk["spec"]["api"]["expose_model_crud"] is False


def test_put_system_config_ignores_extra_keys(monkeypatch, tmp_path):
    _use_project_dir(monkeypatch, tmp_path)
    _write_system_yaml(tmp_path, "kind: SystemConfig\nspec:\n  deployment_mode: single_tenant\n")
    resp = _client().put(
        "/dev/system-config",
        json={"api": {"expose_model_crud": False, "unexpected": 1}, "root_extra": 2},
    )
    assert resp.status_code == 200

    on_disk = yaml.safe_load((tmp_path / ".tuvl" / "system.yaml").read_text())
    assert "unexpected" not in on_disk["spec"]["api"]
    assert "root_extra" not in on_disk["spec"]
