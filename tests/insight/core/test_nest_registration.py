"""Listing a project in a Nest: open registration, the one-time challenge tag, and the three labels.

A real git remote (a local bare repository standing in for the project's https URL) and a fake Nest that
answers like the real one, so the tag really is pushed and removed. Developer toolchain only.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tuvl.cli import app as cli_app
from tuvl.core.config import settings
from tuvl.core.project_info import (
    create_project,
    id_uuid,
    load_project,
    record_registration,
    record_verification,
    update_project,
)
from tuvl_insight import gitutil, registration
from tuvl_insight.api.dev_core_router import router as dev_router
from tuvl_insight.cli import nest as nest_mod

runner = CliRunner()
REPO = "https://git.example.com/acme/demo"
TAG = "tuvl-nest-verify-0123456789abcdef0123"
URL = "https://nest.test"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A project whose `origin` is REPO; pushes go to a local bare repository."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv(nest_mod.SECRET_ENV, raising=False)
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", REPO)
    _git(work, "config", f"url.{bare}.pushInsteadOf", REPO)
    create_project(work, name="demo", author="Jane", email="jane@example.com")
    update_project(work, {"repo": REPO})
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    return work


def _remote_tags(project: Path) -> list[str]:
    bare = project.parent / "remote.git"
    return _git(bare, "tag", "--list").split()


class FakeNest:
    """The registry's answers, without its internals: label by repository visibility."""

    def __init__(self, *, public: bool = True, existing: bool = False, refuse: int | None = None):
        self.public, self.existing, self.refuse = public, existing, refuse
        self.registered: list[dict] = []
        self.verified: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if self.refuse:
            return httpx.Response(self.refuse, json={"error": "refused"})
        if request.url.path == nest_mod.REGISTER_PATH:
            self.registered.append(body)
            claim = self.existing and not body.get("entry_secret")
            return httpx.Response(
                200,
                json={
                    "id": body["id"],
                    "created": not self.existing,
                    "claim": claim,
                    "label": "listed",
                    "entry_secret": "" if self.existing else "s3cret",
                    "challenge": {"token": "tok", "tag": TAG, "expires_at": "2030-01-01T00:00:00Z"},
                },
            )
        self.verified.append(body)
        label = "verified" if self.public else "push_confirmed"
        return httpx.Response(
            200,
            json={
                "id": body["id"],
                "label": label,
                "applied": True,
                "entry_secret": "newsecret" if self.existing else "",
            },
        )


@pytest.fixture
def fake_nest(monkeypatch):
    def install(**kw) -> FakeNest:
        fake = FakeNest(**kw)
        real = httpx.Client
        monkeypatch.setattr(
            nest_mod.httpx,
            "Client",
            lambda **k: real(transport=httpx.MockTransport(fake.handler), **k),
        )
        return fake

    return install


# ── git ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("remote", "repo", "same"),
    [
        ("git@github.com:Acme/App.git", "https://github.com/acme/app", True),
        ("ssh://git@gitlab.example.com:2222/g/s/p.git", "https://gitlab.example.com/g/s/p/", True),
        ("https://user:pw@github.com/acme/app.git", "https://GitHub.com/acme/app", True),
        ("https://github.com/acme/app", "https://github.com/acme/other", False),
        ("/local/path/repo.git", "https://github.com/acme/app", False),
        ("", "https://github.com/acme/app", False),
    ],
)
def test_remote_and_repository_urls_are_compared_in_one_form(remote, repo, same):
    assert gitutil.same_repo(remote, repo) is same


def test_the_tag_is_pushed_then_removed_locally_and_on_the_remote(project):
    assert gitutil.push_tag(project, "origin", TAG) == (True, "")
    assert TAG in _remote_tags(project)
    gitutil.delete_tag(project, "origin", TAG)
    assert _remote_tags(project) == []
    assert TAG not in _git(project, "tag", "--list")


@pytest.mark.parametrize("tag", ["--upload-pack=x", "v1.0.0", "tuvl-nest-verify-XYZ", "a b"])
def test_only_a_challenge_tag_name_is_ever_pushed(project, tag):
    ok, why = gitutil.push_tag(project, "origin", tag)
    assert not ok and "refused" in why
    assert _git(project, "tag", "--list") == ""


def test_a_remote_that_refuses_the_push_is_a_failure_and_leaves_no_local_tag(project):
    _git(project, "config", f"url.{project.parent / 'missing.git'}.pushInsteadOf", REPO)
    _git(project, "config", "--unset-all", f"url.{project.parent / 'remote.git'}.pushInsteadOf")
    ok, why = gitutil.push_tag(project, "origin", TAG)
    assert not ok and why
    assert _git(project, "tag", "--list") == ""


# ── the Nest client ─────────────────────────────────────────────────────────


def test_a_challenge_with_an_unexpected_tag_name_is_refused(fake_nest, project):
    fake = fake_nest()
    original = fake.handler

    def hostile(request):
        res = original(request)
        if request.url.path == nest_mod.REGISTER_PATH:
            data = json.loads(res.content)
            data["challenge"]["tag"] = "--delete-everything"
            return httpx.Response(200, json=data)
        return res

    fake.handler = hostile
    with pytest.raises(nest_mod.NestError, match="will not use"):
        nest_mod.register(URL, {"id": "x"}, secret=None)


def test_the_entry_secret_is_kept_per_listing_with_owner_only_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(nest_mod.SECRET_ENV, raising=False)
    assert nest_mod.entry_secret(URL, "p1") is None
    nest_mod.store_entry_secret(URL, "p1", "aaa")
    nest_mod.store_entry_secret(URL, "p2", "bbb")
    assert (nest_mod.entry_secret(URL, "p1"), nest_mod.entry_secret(URL, "p2")) == ("aaa", "bbb")
    assert stat.S_IMODE(os.stat(nest_mod._cache_path()).st_mode) == 0o600
    monkeypatch.setenv(nest_mod.SECRET_ENV, "from-ci")
    assert nest_mod.entry_secret(URL, "p1") == "from-ci"
    monkeypatch.delenv(nest_mod.SECRET_ENV)
    nest_mod.forget_entry_secret(URL, "p1")
    assert nest_mod.entry_secret(URL, "p1") is None and nest_mod.entry_secret(URL, "p2") == "bbb"


@pytest.mark.parametrize("status", [403, 409, 422, 429, 500])
def test_a_refusal_from_the_nest_is_an_error_with_its_reason(fake_nest, status):
    fake_nest(refuse=status)
    with pytest.raises(nest_mod.NestError, match=f"{status}.*refused"):
        nest_mod.register(URL, {"id": "x"}, secret=None)


# ── the flow ────────────────────────────────────────────────────────────────


def test_a_public_repository_is_listed_pushed_and_verified(project, fake_nest):
    fake = fake_nest(public=True)
    out = registration.register_project(project, load_project(project), URL)
    assert (out.label, out.pushed, out.applied) == ("verified", True, True)
    assert fake.registered[0]["id"] == id_uuid(load_project(project)["id"])
    assert fake.registered[0]["contact_email"] == "jane@example.com"
    assert fake.verified[0] == {"id": fake.registered[0]["id"], "token": "tok", "pushed": True}
    reg = load_project(project)["registry"]
    assert (reg["label"], bool(reg["verified_at"])) == ("verified", True)
    assert _remote_tags(project) == [] and _git(project, "tag", "--list") == ""
    assert nest_mod.entry_secret(URL, fake.registered[0]["id"]) == "s3cret"


def test_a_private_repository_is_push_confirmed_not_verified(project, fake_nest):
    fake_nest(public=False)
    out = registration.register_project(project, load_project(project), URL)
    assert out.label == "push_confirmed"
    reg = load_project(project)["registry"]
    assert reg["label"] == "push_confirmed" and "verified_at" not in reg


def test_keep_tag_leaves_it_on_the_remote(project, fake_nest):
    fake_nest()
    registration.register_project(project, load_project(project), URL, keep_tag=True)
    assert _remote_tags(project) == [TAG]


def test_no_verify_lists_without_touching_the_repository(project, fake_nest):
    fake = fake_nest()
    out = registration.register_project(project, load_project(project), URL, verify=False)
    assert out.label == "listed" and out.pushed is None and fake.verified == []
    assert _git(project, "tag", "--list") == "" and _remote_tags(project) == []
    assert load_project(project)["registry"]["label"] == "listed"


def test_declining_the_prompt_does_not_push(project, fake_nest):
    fake = fake_nest()
    out = registration.register_project(
        project, load_project(project), URL, confirm_push=lambda *_: False
    )
    assert out.pushed is None and fake.verified == [] and _remote_tags(project) == []
    assert any("not pushed" in n for n in out.notes)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda p: update_project(p, {"repo": ""}), "no repository URL"),
        (lambda p: update_project(p, {"repo": "http://git.example.com/acme/demo"}), "https"),
        (lambda p: _git(p, "remote", "remove", "origin"), "no git remote"),
        (
            lambda p: _git(p, "remote", "set-url", "origin", "https://git.example.com/other/x"),
            "not the project's repository",
        ),
    ],
)
def test_verification_is_skipped_with_the_reason_when_it_cannot_run(
    project, fake_nest, setup, reason
):
    fake = fake_nest()
    setup(project)
    out = registration.register_project(project, load_project(project), URL)
    assert out.pushed is None and fake.verified == [] and out.label == "listed"
    assert any(reason in n for n in out.notes), out.notes


def test_a_failed_push_leaves_the_listing_unverified(project, fake_nest):
    fake = fake_nest()
    _git(project, "config", "--unset-all", f"url.{project.parent / 'remote.git'}.pushInsteadOf")
    out = registration.register_project(project, load_project(project), URL)
    assert out.pushed is False and fake.verified == []
    assert any("Could not push" in n for n in out.notes)
    assert load_project(project)["registry"]["label"] == "listed"


def test_a_registration_for_a_listed_id_without_the_secret_is_a_claim(project, fake_nest):
    fake = fake_nest(public=True, existing=True)
    out = registration.register_project(project, load_project(project), URL)
    assert out.claim and out.applied and out.label == "verified"
    assert load_project(project)["registry"]["label"] == "verified"
    assert nest_mod.entry_secret(URL, fake.registered[0]["id"]) == "newsecret"


def test_a_claim_the_registry_cannot_confirm_changes_nothing_locally(project, fake_nest):
    fake = fake_nest(public=True, existing=True)
    original = fake.handler

    def not_applied(request):
        res = original(request)
        if request.url.path == nest_mod.VERIFY_PATH:
            return httpx.Response(
                200, json={"id": "x", "label": "listed", "applied": False, "entry_secret": ""}
            )
        return res

    fake.handler = not_applied
    out = registration.register_project(project, load_project(project), URL)
    assert out.claim and not out.applied
    assert load_project(project).get("registry") is None
    assert any("claim was not applied" in n for n in out.notes)


def test_the_stored_secret_is_sent_on_the_next_registration(project, fake_nest):
    fake = fake_nest()
    registration.register_project(project, load_project(project), URL, verify=False)
    registration.register_project(project, load_project(project), URL, verify=False)
    assert "entry_secret" not in fake.registered[0]
    assert fake.registered[1]["entry_secret"] == "s3cret"


def test_record_verification_updates_only_the_label(tmp_path):
    create_project(tmp_path, name="demo")
    record_registration(tmp_path, URL, when="2026-11-10T00:00:00Z")
    record_verification(tmp_path, "verified", verified_at="2026-11-11T00:00:00Z")
    reg = load_project(tmp_path)["registry"]
    assert (reg["label"], reg["verified_at"], reg["registered_at"]) == (
        "verified",
        "2026-11-11T00:00:00Z",
        "2026-11-10T00:00:00Z",
    )
    record_verification(tmp_path, "bogus")
    assert load_project(tmp_path)["registry"]["label"] == "listed"


# ── the command ─────────────────────────────────────────────────────────────


def test_the_command_shows_the_payload_asks_twice_and_reports_the_label(project, fake_nest):
    fake_nest(public=True)
    res = runner.invoke(
        cli_app,
        ["register", "project", "-d", str(project), "--nest", URL],
        input="y\ny\n",
    )
    assert res.exit_code == 0, res.output
    assert "no account needed" in res.output and "jane@example.com" in res.output
    assert f"git tag {TAG}" in res.output
    assert "Verified" in res.output
    assert _remote_tags(project) == []


def test_the_command_with_yes_and_no_verify_never_pushes(project, fake_nest):
    fake_nest()
    res = runner.invoke(
        cli_app, ["register", "project", "-d", str(project), "--nest", URL, "--yes", "--no-verify"]
    )
    assert res.exit_code == 0, res.output
    assert "Listed" in res.output and _git(project, "tag", "--list") == ""


def test_the_command_fails_with_the_nests_reason(project, fake_nest):
    fake_nest(refuse=429)
    res = runner.invoke(
        cli_app, ["register", "project", "-d", str(project), "--nest", URL, "--yes"]
    )
    assert res.exit_code == 1 and "429" in res.output


# ── Insight (dev server) ────────────────────────────────────────────────────


@pytest.fixture
def client(project, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "tuvl_project_dir", str(project))
    api = FastAPI()
    api.include_router(dev_router)
    return TestClient(api)


def test_the_preview_shows_the_payload_and_what_verification_would_do(client, project):
    body = client.post("/dev/project/register/preview", json={"nest": URL}).json()
    assert body["payload"]["contact_email"] == "jane@example.com"
    assert body["verification"] == {
        "remote": "origin",
        "remote_url": REPO,
        "repo": REPO,
        "matches_repo": True,
        "can_verify": True,
        "reason": "",
    }


def test_the_preview_says_why_verification_cannot_run(client, project):
    _git(project, "remote", "remove", "origin")
    body = client.post("/dev/project/register/preview", json={"nest": URL}).json()
    assert (
        body["verification"]["can_verify"] is False
        and "no git remote" in body["verification"]["reason"]
    )


def test_dev_register_without_consent_lists_but_never_pushes(client, project, fake_nest):
    fake = fake_nest()
    res = client.post("/dev/project/register", json={"nest": URL}).json()
    assert res["status"] == "registered" and res["label"] == "listed" and res["pushed"] is None
    assert fake.verified == [] and _git(project, "tag", "--list") == ""
    assert res["project"]["registration_state"] == "current"


def test_dev_register_with_consent_pushes_verifies_and_returns_the_label(
    client, project, fake_nest
):
    fake_nest(public=True)
    res = client.post("/dev/project/register", json={"nest": URL, "verify": True}).json()
    assert (res["label"], res["pushed"], res["applied"]) == ("verified", True, True)
    assert res["project"]["registry"]["label"] == "verified"
    assert _remote_tags(project) == []


def test_dev_register_reports_a_refusal_as_a_gateway_error(client, fake_nest):
    fake_nest(refuse=403)
    res = client.post("/dev/project/register", json={"nest": URL})
    assert res.status_code == 502 and "403" in res.json()["detail"]


def test_the_device_login_routes_are_gone(client):
    assert client.post("/dev/project/register/poll", json={"handle": "h"}).status_code in (404, 405)
