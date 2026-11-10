"""``.tuvl/project.yaml`` — id minting, validation, immutability, dev endpoints, CLI."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tuvl.cli import app as cli_app
from tuvl.core.config import settings
from tuvl.core.project_info import (
    ProjectInfoError,
    create_project,
    id_is_valid,
    id_uuid,
    load_project,
    new_project_id,
    record_registration,
    regenerate_id,
    registration_state,
    set_registry,
    update_project,
    validate_field,
)
from tuvl_insight.api.dev_core_router import router as dev_router

runner = CliRunner()


def test_project_id_is_uuid7_and_time_ordered():
    a, b = new_project_id(), new_project_id()
    assert a.startswith("prj_")
    assert uuid.UUID(id_uuid(a)).version == 7
    assert a != b


def test_id_check_catches_edits_and_typos():
    good = new_project_id()
    assert id_is_valid(good)
    head, check = good.rsplit("-", 1)
    flipped = head[:-1] + ("0" if head[-1] != "0" else "1") + "-" + check
    assert not id_is_valid(flipped)
    assert not id_is_valid(head + "-zz" if check != "zz" else head + "-00")
    assert not id_is_valid(str(uuid.uuid4()))
    assert not id_is_valid("")


def test_create_is_idempotent_and_never_changes_the_id(tmp_path: Path):
    first = create_project(tmp_path, name="demo", author="A", email="a@example.com")
    again = create_project(tmp_path, name="other", author="B")
    assert again["id"] == first["id"]
    assert load_project(tmp_path)["name"] == "demo"


def test_update_validates_and_keeps_registry(tmp_path: Path):
    create_project(tmp_path, name="demo")
    set_registry(tmp_path, {"url": "https://nest.tuvl.io", "registered_at": "2026-10-15T00:00:00Z"})
    rec = update_project(
        tmp_path, {"type": "production", "version": "1.2.3", "organization": "Acme"}
    )
    assert rec["organization"] == "Acme"
    rec = update_project(tmp_path, {"organization_url": "https://acme.example"})
    assert rec["organization_url"] == "https://acme.example"
    with pytest.raises(ProjectInfoError):
        update_project(tmp_path, {"organization_url": "acme.example"})
    assert rec["type"] == "production"
    assert rec["registry"]["url"] == "https://nest.tuvl.io"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "staging"),
        ("version", "one"),
        ("email", "not-an-email"),
        ("repo", "ftp://x"),
        ("name", "  "),
        ("description", "x" * 2001),
        ("id", "anything"),
    ],
)
def test_rejects_invalid_values(field: str, value: str):
    with pytest.raises(ProjectInfoError):
        validate_field(field, value)


def test_id_cannot_be_changed(tmp_path: Path):
    rec = create_project(tmp_path, name="demo")
    with pytest.raises(ProjectInfoError):
        update_project(tmp_path, {"id": str(uuid.uuid4())})
    assert load_project(tmp_path)["id"] == rec["id"]


def test_unreadable_file_loads_as_none(tmp_path: Path):
    path = tmp_path / ".tuvl" / "project.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("kind: [broken\n")
    assert load_project(tmp_path) is None


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))
    api = FastAPI()
    api.include_router(dev_router)
    return TestClient(api)


def test_dev_get_404_then_put_creates_with_id(monkeypatch, tmp_path: Path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/dev/project").status_code == 404
    res = client.put("/dev/project", json={"author": "Jane", "type": "poc", "version": "0.2.0"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["author"] == "Jane"
    assert id_is_valid(body["id"])
    assert body["registration_state"] == "unregistered"
    assert client.get("/dev/project").json()["id"] == body["id"]


def test_dev_put_ignores_id_and_registry(monkeypatch, tmp_path: Path):
    client = _client(monkeypatch, tmp_path)
    created = client.put("/dev/project", json={"author": "Jane"}).json()
    res = client.put("/dev/project", json={"id": "forged", "registry": {"url": "x"}, "type": "dev"})
    assert res.status_code == 200
    assert res.json()["id"] == created["id"]
    assert "registry" not in res.json()


def test_dev_put_rejects_bad_type(monkeypatch, tmp_path: Path):
    client = _client(monkeypatch, tmp_path)
    assert client.put("/dev/project", json={"type": "staging"}).status_code == 422


def test_cli_init_set_info(tmp_path: Path):
    d = str(tmp_path)
    res = runner.invoke(cli_app, ["upgrade", "project", "-d", d, "--name", "demo"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(cli_app, ["set", "project", "type", "production", "-d", d])
    assert res.exit_code == 0, res.output
    res = runner.invoke(cli_app, ["set", "project", "type", "bogus", "-d", d])
    assert res.exit_code == 2
    res = runner.invoke(cli_app, ["show", "project", "--json", "-d", d])
    data = yaml.safe_load(res.output)
    assert data["type"] == "production"
    assert data["name"] == "demo"


def test_cli_info_without_project_fails(tmp_path: Path):
    res = runner.invoke(cli_app, ["show", "project", "-d", str(tmp_path)])
    assert res.exit_code == 1


def test_cli_register_sends_the_shown_payload_and_records_it(monkeypatch, tmp_path: Path):
    import httpx

    from tuvl_insight.cli import nest as nest_mod

    create_project(tmp_path, name="demo", author="Jane", email="jane@example.com")
    sent: dict = {}

    class _Res:
        status_code = 200
        content = b"{}"

        def json(self):
            return {}

    def _post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, json=json, auth=headers["Authorization"])
        return _Res()

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(nest_mod, "ensure_token", lambda url, relogin=False: "tok")
    res = runner.invoke(
        cli_app,
        ["register", "project", "-d", str(tmp_path), "--nest", "https://nest.test", "--yes"],
    )
    assert res.exit_code == 0, res.output
    assert sent["url"] == "https://nest.test/api/register-project"
    assert sent["auth"] == "Bearer tok"
    assert (
        sent["json"]["contact_email"] == "jane@example.com"
    )  # kept private by the Nest, not by the client
    assert sent["json"]["author"] == "Jane"
    assert sent["json"]["id"] == id_uuid(load_project(tmp_path)["id"])
    assert load_project(tmp_path)["registry"]["url"] == "https://nest.test"


def test_registration_state_tracks_drift_and_id_change(tmp_path: Path):
    create_project(tmp_path, name="demo", author="Jane")
    assert registration_state(load_project(tmp_path)) == "unregistered"
    record_registration(tmp_path, "https://nest.test", when="2026-10-15T00:00:00Z")
    assert registration_state(load_project(tmp_path)) == "current"
    update_project(tmp_path, {"description": "now with words"})
    assert registration_state(load_project(tmp_path)) == "outdated"
    record_registration(tmp_path, "https://nest.test", when="2026-10-15T00:00:00Z")
    rec = load_project(tmp_path)
    rec["id"] = new_project_id()
    assert registration_state(rec) == "id_changed"
    rec["id"] = "prj_tampered"
    assert registration_state(rec) == "invalid_id"


def test_commands_refuse_a_tampered_id_and_new_id_recovers(tmp_path: Path):
    create_project(tmp_path, name="demo")
    path = tmp_path / ".tuvl" / "project.yaml"
    path.write_text(path.read_text().replace("prj_", "prj_0", 1))
    res = runner.invoke(cli_app, ["show", "project", "-d", str(tmp_path)])
    assert res.exit_code == 1
    assert "edited by hand" in res.output
    fresh = regenerate_id(tmp_path)
    assert id_is_valid(fresh["id"])
    assert runner.invoke(cli_app, ["show", "project", "-d", str(tmp_path)]).exit_code == 0


def _fake_nest(monkeypatch, *, pending_polls: int = 0, cached: str | None = None):
    """Route the dev server's outbound httpx calls to an in-memory nest."""
    import httpx

    from tuvl_insight.cli import nest as nest_mod

    seen: dict = {"register": [], "polls": 0}
    monkeypatch.setattr(nest_mod, "cached_token", lambda url: cached)
    monkeypatch.setattr(nest_mod, "store_token", lambda url, t: seen.setdefault("stored", t))
    monkeypatch.setattr(nest_mod, "forget_token", lambda url: seen.setdefault("forgot", True))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/device/start"):
            return httpx.Response(
                200,
                json={
                    "handle": "h1",
                    "user_code": "AB-12",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 1,
                    "expires_in": 600,
                },
            )
        if path.endswith("/device/poll"):
            seen["polls"] += 1
            if seen["polls"] <= pending_polls:
                return httpx.Response(428, json={"detail": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "tok"})
        seen["register"].append(request.read())
        return httpx.Response(200, json={"project_id": "x"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


def test_dev_register_logs_in_polls_then_registers(monkeypatch, tmp_path: Path):
    client = _client(monkeypatch, tmp_path)
    client.put("/dev/project", json={"author": "Jane"})
    seen = _fake_nest(monkeypatch, pending_polls=1)

    started = client.post("/dev/project/register", json={"nest": "https://nest.test"}).json()
    assert started["status"] == "login_required"
    assert started["user_code"] == "AB-12"
    assert "token" not in started

    poll = {"nest": "https://nest.test", "handle": started["handle"]}
    assert client.post("/dev/project/register/poll", json=poll).json()["status"] == "pending"
    done = client.post("/dev/project/register/poll", json=poll).json()
    assert done["status"] == "registered"
    assert done["project"]["registration_state"] == "current"
    assert seen["stored"] == "tok"
    assert len(seen["register"]) == 1
    assert b"organization" in seen["register"][0]


def test_dev_register_uses_cached_token_and_preview_matches(monkeypatch, tmp_path: Path):
    client = _client(monkeypatch, tmp_path)
    client.put("/dev/project", json={"author": "Jane", "email": "jane@example.com"})
    seen = _fake_nest(monkeypatch, cached="tok")

    preview = client.post(
        "/dev/project/register/preview", json={"nest": "https://nest.test"}
    ).json()
    assert preview["payload"]["contact_email"] == "jane@example.com"

    res = client.post("/dev/project/register", json={"nest": "https://nest.test"}).json()
    assert res["status"] == "registered"
    assert len(seen["register"]) == 1


def test_cli_description_markdown_from_file_and_stdin(tmp_path: Path):
    create_project(tmp_path, name="demo")
    md = tmp_path / "about.md"
    md.write_text("# Demo\n\n- fast\n- **safe**\n")
    res = runner.invoke(cli_app, ["set", "project", "description", f"@{md}", "-d", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert load_project(tmp_path)["description"].startswith("# Demo")
    res = runner.invoke(
        cli_app, ["set", "project", "description", "-", "-d", str(tmp_path)], input="from *stdin*\n"
    )
    assert res.exit_code == 0, res.output
    assert load_project(tmp_path)["description"] == "from *stdin*"
    info = runner.invoke(cli_app, ["show", "project", "-d", str(tmp_path)])
    assert "from stdin" in info.output
    missing = runner.invoke(
        cli_app, ["set", "project", "description", "@nope.md", "-d", str(tmp_path)]
    )
    assert missing.exit_code == 2
