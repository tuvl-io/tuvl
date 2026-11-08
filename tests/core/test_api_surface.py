"""Which system routes a server mounts and what its API documentation shows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tuvl.core.config import settings
from tuvl.core.system_config import api_docs_mode, exposed_api_groups, load_system_config


@pytest.fixture(autouse=True)
def _clear_cache():
    load_system_config.cache_clear()
    yield
    load_system_config.cache_clear()


def _project(monkeypatch, tmp_path: Path, api: str = "") -> None:
    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))
    (tmp_path / ".tuvl").mkdir(exist_ok=True)
    body = f"  api:\n{api}" if api else ""
    (tmp_path / ".tuvl" / "system.yaml").write_text(
        f"kind: SystemConfig\nspec:\n{body or '  {}'}\n"
    )


def test_the_dev_server_mounts_every_group(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path, "    expose: [auth]\n")
    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    assert exposed_api_groups() == {"auth", "runs", "iam", "admin", "artifacts", "system"}


def test_production_mounts_only_auth_and_runs_by_default(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    assert exposed_api_groups() == {"auth", "runs"}


def test_production_groups_come_from_system_yaml_and_the_environment_wins(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path, "    expose: [auth, runs, admin]\n")
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    assert exposed_api_groups() == {"auth", "runs", "admin"}
    monkeypatch.setattr(settings, "tuvl_api_expose", "auth, artifacts, nonsense")
    assert exposed_api_groups() == {"auth", "artifacts", "runs"}
    monkeypatch.setattr(settings, "tuvl_api_expose", "")
    assert exposed_api_groups() == {"runs"}


def test_the_documentation_defaults_to_workflows_in_dev_and_off_in_production(
    monkeypatch, tmp_path
):
    _project(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    assert api_docs_mode() == "workflows"
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    assert api_docs_mode() == "off"


def test_the_documentation_mode_comes_from_system_yaml_and_the_environment_wins(
    monkeypatch, tmp_path
):
    _project(monkeypatch, tmp_path, "    docs: all\n")
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    assert api_docs_mode() == "all"
    monkeypatch.setattr(settings, "tuvl_api_docs", "workflows")
    assert api_docs_mode() == "workflows"
    monkeypatch.setattr(settings, "tuvl_api_docs", "bogus")
    assert api_docs_mode() == "off"


_PROBE = """
import json
from fastapi.testclient import TestClient
from tuvl.main import app
c = TestClient(app, raise_server_exceptions=False)
paths = sorted(app.openapi()["paths"])
print(json.dumps({
    "docs": c.get("/docs").status_code,
    "openapi": c.get("/openapi.json").status_code,
    "auth": c.post("/auth/token", json={}).status_code,
    "admin": c.get("/admin/scopes").status_code,
    "bootstrap": c.post("/auth/bootstrap", json={}).status_code,
    "iam_users": c.get("/auth/admin/users").status_code,
    "system": c.get("/api/_system/models").status_code,
    "artifacts": c.get("/api/artifacts").status_code,
    "documented": paths,
}))
"""


def _probe(tmp_path: Path, **env: str) -> dict:
    full = {
        **os.environ,
        "TUVL_DEV_MODE": "false",
        "TUVL_ENV": "production",
        "TUVL_PROJECT_DIR": str(tmp_path),
        **env,
    }
    full.pop("TUVL_ALLOW_DEV_AUTH", None)
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_production_server_serves_no_documentation_and_no_admin_routes(tmp_path):
    seen = _probe(tmp_path)
    assert seen["docs"] == 404 and seen["openapi"] == 404
    assert seen["admin"] == 404 and seen["system"] == 404 and seen["artifacts"] == 404
    assert seen["auth"] != 404


def test_production_does_not_mount_iam_administration_or_bootstrap(tmp_path):
    seen = _probe(tmp_path)
    assert seen["bootstrap"] == 404 and seen["iam_users"] == 404
    assert seen["auth"] != 404  # login stays


def test_the_iam_group_can_be_exposed_on_purpose(tmp_path):
    seen = _probe(tmp_path, TUVL_API_EXPOSE="auth,iam")
    assert seen["bootstrap"] != 404 and seen["iam_users"] != 404


def test_the_documentation_lists_no_system_route_when_it_is_on(tmp_path):
    seen = _probe(tmp_path, TUVL_API_DOCS="workflows", TUVL_API_EXPOSE="auth,runs,admin")
    assert seen["docs"] == 200 and seen["openapi"] == 200
    assert seen["admin"] != 404  # mounted, only not documented
    assert not any(
        p.startswith(("/auth", "/admin", "/api/_system", "/health")) for p in seen["documented"]
    )


def test_all_shows_the_system_routes(tmp_path):
    seen = _probe(tmp_path, TUVL_API_DOCS="all", TUVL_API_EXPOSE="auth")
    assert "/auth/token" in seen["documented"]


def test_the_dev_server_can_mirror_production(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path, "    dev_expose_all: false\n")
    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    assert exposed_api_groups() == {"auth", "runs"}
    assert api_docs_mode() == "off"
    monkeypatch.setattr(settings, "tuvl_dev_expose_all", True)  # the environment wins
    assert exposed_api_groups() == {"auth", "runs", "iam", "admin", "artifacts", "system"}
    assert api_docs_mode() == "workflows"


def test_mirroring_production_keeps_what_was_exposed_on_purpose(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path, "    dev_expose_all: false\n    expose: [auth, runs, admin]\n")
    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    assert exposed_api_groups() == {"auth", "runs", "admin"}


def test_the_settings_endpoint_writes_only_what_it_is_given(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tuvl.core.dev.router import router as dev_router

    _project(monkeypatch, tmp_path, "    expose_model_crud: false\n")
    app = FastAPI()
    app.include_router(dev_router)
    client = TestClient(app)
    ok = client.put(
        "/dev/system-config",
        json={"api": {"dev_expose_all": False, "expose": ["auth", "runs", "admin"], "docs": "all"}},
    )
    assert ok.status_code == 200, ok.text
    import yaml

    saved = yaml.safe_load((tmp_path / ".tuvl" / "system.yaml").read_text())["spec"]["api"]
    assert saved == {
        "expose_model_crud": False,
        "dev_expose_all": False,
        "expose": ["auth", "admin"],
        "docs": "all",
    }
    client.put("/dev/system-config", json={"api": {"docs": None, "expose": None}})
    saved = yaml.safe_load((tmp_path / ".tuvl" / "system.yaml").read_text())["spec"]["api"]
    assert "docs" not in saved and "expose" not in saved and saved["expose_model_crud"] is False
    assert client.put("/dev/system-config", json={"api": {"expose": ["nope"]}}).status_code == 422
    assert client.put("/dev/system-config", json={"api": {"docs": "x"}}).status_code == 422


def test_runs_cannot_be_switched_off(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path, "    expose: [auth]\n")
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    assert "runs" in exposed_api_groups()
    monkeypatch.setattr(settings, "tuvl_api_expose", "admin")
    assert exposed_api_groups() == {"admin", "runs"}


def test_a_dev_process_can_answer_for_production(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    assert exposed_api_groups(production=True) == {"auth", "runs"}
    assert api_docs_mode(production=True) == "off"
