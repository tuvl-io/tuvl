"""`tuvl deploy`: ship a project to a tuvl cloud with a client token.

A fake cloud (httpx's mock transport) answers like the real client gateway. The token must never be an argument, never be printed and never cross plain
http to another machine; the administrator password must be sealed before it is sent and shown once."""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight.cli import cloud
from tuvl_insight.cli.commands import deploy as deploy_mod

pytestmark = pytest.mark.requires_insight

runner = CliRunner()
TOKEN = "tvc_0123456789abcdef_" + "A" * 43
OTHER = "tvc_fedcba9876543210_" + "B" * 43
URL = "https://cloud.test"
REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for var in (cloud.TOKEN_ENV, cloud.URL_ENV, "TUVL_ADMIN_EMAIL"):
        monkeypatch.delenv(var, raising=False)


class FakeCloud:
    """The cloud's client API: records every request, answers from tables."""

    def __init__(self):
        self.requests, self.job_polls, self.token_ok = [], [], {TOKEN}
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_pem = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.me = {"client_id": "0123456789abcdef", "label": "ci on github", "customer": "Acme Trading", "wallet": 20000.0, "base_domain": "tuvl.app", "projects": [], "plans": []}
        self.job = {"id": "ab" * 16, "project": "shop", "state": "running", "step": "upload", "steps": [{"name": "upload", "detail": "shop", "at": 0.1}], "message": ""}
        self.finals = [
            {**self.job, "steps": [{"name": "upload", "detail": "shop", "at": 0.1}, {"name": "build", "detail": "", "at": 1}]},
            {**self.job, "state": "succeeded", "step": "done", "steps": [{"name": s, "detail": "", "at": 1} for s in ("upload", "build", "plan", "deploy", "done")],
             "result": {"project": "shop", "url": "http://shop.tuvl.app", "hostname": "shop.tuvl.app", "admin": "set", "plan": "hobby"}},
        ]
        self.start_status, self.start_body = 202, None
        self.check = {"hostname": "shop.tuvl.app", "base_domain": "tuvl.app", "availability": {"status": "available", "message": "that name is available"},
                      "dns": {"status": "ok", "message": "shop.tuvl.app resolves to this machine.", "resolved": ["93.184.216.34"], "expected": ["93.184.216.34"]}}
        self.rename_status, self.rename_body = 202, None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.token_ok:
            return httpx.Response(401, json={"detail": "that client token is not valid"})
        path = request.url.path
        if path == "/client/v1/me":
            return httpx.Response(200, json=self.me)
        if path == "/client/v1/seal-key":
            return httpx.Response(200, json={"key_id": "k" * 16, "algorithm": "RSA-OAEP-256+A256GCM", "public_key": self.public_pem})
        if path == "/client/v1/deployments" and request.method == "POST":
            if self.start_status != 202:
                return httpx.Response(self.start_status, json={"detail": self.start_body})
            return httpx.Response(202, json=self.job)
        if path == "/client/v1/hostnames/check":
            return httpx.Response(200, json={**self.check, "hostname": request.url.params.get("name") or f"{request.url.params.get('project')}.tuvl.app"})
        if path.startswith("/client/v1/projects/") and path.endswith("/hostname") and request.method == "POST":
            if self.rename_status != 202:
                return httpx.Response(self.rename_status, json={"detail": self.rename_body})
            return httpx.Response(202, json={**self.job, "id": "cd" * 16})
        if path.startswith("/client/v1/deployments/"):
            self.job_polls.append(path)
            return httpx.Response(200, json=self.finals[min(len(self.job_polls) - 1, len(self.finals) - 1)])
        return httpx.Response(404, json={"detail": "no"})

    def open(self, envelope: str, slug: str, name: str = "ADMIN_PASSWORD") -> str:
        e = json.loads(envelope)
        key = self.key.decrypt(base64.b64decode(e["wk"]), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        return AESGCM(key).decrypt(base64.b64decode(e["iv"]), base64.b64decode(e["ct"]), f"apphost-secret:v1:{slug}:{name}".encode()).decode()


@pytest.fixture
def fake(monkeypatch):
    f = FakeCloud()
    monkeypatch.setattr(cloud.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(f.handler), **kw))
    monkeypatch.setattr(cloud.time, "sleep", lambda s: None)
    return f


def make_archive(tmp_path: Path, project="shop", version="0.1.0") -> Path:
    path = tmp_path / f"{project}-{version}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        data = json.dumps({"project": project, "version": version}).encode()
        info = tarfile.TarInfo(f"{project}-{version}/BUILD-INFO.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return path


def run(*args, env=None, input=None):
    return runner.invoke(app, ["deploy", *args], env=env, input=input)


# ---- addresses and tokens

@pytest.mark.parametrize("given, expected", [
    ("https://cloud.tuvl.io", "https://cloud.tuvl.io"), ("https://cloud.tuvl.io/", "https://cloud.tuvl.io"), ("http://localhost:8951", "http://localhost:8951"),
    ("http://127.0.0.1:8951/", "http://127.0.0.1:8951"), ("https://cloud.example.com:8443", "https://cloud.example.com:8443"), (None, "https://cloud.tuvl.io"),
])
def test_the_cloud_address(given, expected):
    assert cloud.cloud_url(given) == expected


@pytest.mark.parametrize("bad", ["http://cloud.example.com", "http://203.0.113.9", "ftp://cloud.example.com", "cloud.tuvl.io", "https://", "https://user:pw@cloud.example.com",
                                 "https://cloud.example.com/path", "https://cloud.example.com?x=1", "https://cloud.example.com#frag", "javascript:alert(1)"])
def test_an_address_that_could_leak_the_token_or_is_not_one_is_refused(bad):
    with pytest.raises(cloud.CloudError):
        cloud.cloud_url(bad)


def test_the_address_comes_from_the_environment_when_not_given(monkeypatch):
    monkeypatch.setenv(cloud.URL_ENV, "https://cloud.other.example")
    assert cloud.cloud_url(None) == "https://cloud.other.example" and cloud.cloud_url("https://x.example") == "https://x.example"


@pytest.mark.parametrize("bad", ["", "nope", TOKEN + "x", TOKEN[:-1], "tvc_0123456789ABCDEF_" + "A" * 43, "tvc_0123456789abcdef_" + "A" * 42 + "!", " ", "tvc_" + "0" * 16 + "_" + "A" * 44])
def test_a_token_that_is_not_one_is_refused(bad):
    with pytest.raises(cloud.CloudError):
        cloud.check_token(bad)
    assert cloud.check_token(f"  {TOKEN}\n") == TOKEN


def test_the_token_is_taken_from_standard_input_then_the_environment_then_the_saved_login(monkeypatch):
    assert cloud.resolve_token(URL) is None
    cloud.store_token(URL, OTHER)
    assert cloud.resolve_token(URL) == (OTHER, "the saved login")
    monkeypatch.setenv(cloud.TOKEN_ENV, TOKEN)
    assert cloud.resolve_token(URL) == (TOKEN, cloud.TOKEN_ENV)
    assert cloud.resolve_token(URL, stdin_token=OTHER) == (OTHER, "standard input")
    assert cloud.resolve_token("https://other.example") == (TOKEN, cloud.TOKEN_ENV), "the environment applies to every cloud; a saved login is per cloud"


def test_the_saved_login_is_private_one_per_cloud_and_a_damaged_file_is_ignored():
    cloud.store_token(URL, TOKEN, label="ci")
    cloud.store_token("https://other.example", OTHER)
    path = cloud._cache_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and cloud.stored_token(URL) == TOKEN and cloud.stored_token("https://other.example") == OTHER
    assert cloud.forget_token(URL) is True and cloud.forget_token(URL) is False and cloud.stored_token(URL) is None and cloud.stored_token("https://other.example") == OTHER
    path.write_text("not json")
    assert cloud.stored_token(URL) is None
    path.write_text(json.dumps({URL: {"token": "bogus"}}))
    assert cloud.stored_token(URL) is None, "a saved value that is not a token is not used"


def test_there_is_no_option_that_takes_the_token_as_an_argument():
    res = run("--token", TOKEN)
    assert res.exit_code == 2 and TOKEN not in res.output


# ---- sealing the administrator's password

def test_a_password_is_random_without_look_alikes_and_always_mixed():
    seen = {cloud.generate_password() for _ in range(50)}
    assert len(seen) == 50
    for pw in seen:
        assert len(pw) == 20 and not set(pw) & set("0O1lI") and any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


def test_a_sealed_password_opens_for_its_own_project_and_purpose_only():
    f = FakeCloud()
    envelope = cloud.seal(f.public_pem, "k" * 16, "Correct-Horse-9-battery", slug="shop")
    assert "Correct-Horse" not in envelope and f.open(envelope, "shop") == "Correct-Horse-9-battery"
    with pytest.raises(Exception):
        f.open(envelope, "other")
    with pytest.raises(Exception):
        f.open(envelope, "shop", name="SMTP_PASSWORD")
    assert cloud.seal(f.public_pem, "k" * 16, "x", slug="shop") != cloud.seal(f.public_pem, "k" * 16, "x", slug="shop"), "every envelope is new"


# ---- the cloud's answers

@pytest.mark.parametrize("status, body, fragment", [
    (401, {"detail": "x"}, "expired or been revoked"), (402, {"detail": "the wallet holds 100 credits and the hobby plan needs 5000"}, "wallet holds 100"),
    (409, {"detail": "that project name is not available"}, "not available"), (413, {"detail": "the archive is larger than 64 MB"}, "64 MB"),
    (422, {"detail": "the archive was rejected by the platform's checks: x"}, "rejected"), (429, {"detail": "slow"}, "Too many failed attempts"),
    (500, {"detail": "boom with internals"}, "HTTP 500"), (502, None, "HTTP 502"),
])
def test_what_the_cloud_says_is_shown_plainly_and_internals_are_not(fake, status, body, fragment):
    fake.start_status, fake.start_body = status, (body or {}).get("detail") if body else None
    fake.token_ok = {TOKEN} if status != 401 else set()
    with pytest.raises(cloud.CloudError) as exc:
        cloud.Cloud(URL, TOKEN).start(make_archive(Path(os.environ["XDG_CONFIG_HOME"]).parent), plan="hobby") if status != 401 else cloud.Cloud(URL, TOKEN).me()
    assert fragment in str(exc.value) and "internals" not in str(exc.value) and TOKEN not in str(exc.value)


def test_a_cloud_that_cannot_be_reached_is_one_plain_sentence(monkeypatch):
    def down(request):
        raise httpx.ConnectError("secret internals")
    monkeypatch.setattr(cloud.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(down), **kw))
    with pytest.raises(cloud.CloudError) as exc:
        cloud.Cloud(URL, TOKEN).me()
    assert "could not reach https://cloud.test" in str(exc.value) and "secret" not in str(exc.value)


def test_following_a_deployment_reports_each_step_once_and_gives_up_after_the_limit(fake):
    steps = []
    job = cloud.Cloud(URL, TOKEN).follow("ab" * 16, lambda n, d: steps.append(n), interval=0)
    assert job["state"] == "succeeded" and steps == ["upload", "build", "plan", "deploy", "done"]
    fake.job_polls.clear(); fake.finals[:] = [{**fake.job, "state": "running", "step": "build"}]
    t = [0.0]
    with pytest.raises(cloud.CloudError, match="still running after 10 seconds"):
        cloud.Cloud(URL, TOKEN).follow("ab" * 16, lambda n, d: None, interval=1, timeout=10, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])


# ---- the command

def test_with_no_token_it_says_how_to_get_one_and_uploads_nothing(fake, tmp_path):
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL)
    assert res.exit_code == 1 and "Deploy tokens" in res.output and "--login" in res.output and fake.requests == []


def test_a_plain_http_address_to_another_machine_is_refused_before_anything_is_sent(fake, tmp_path):
    res = run("--archive", str(make_archive(tmp_path)), "--url", "http://cloud.example.com", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 2 and "refusing to send a token over plain http" in res.output and fake.requests == []


def test_a_deployment_uploads_the_archive_follows_it_and_prints_the_address_but_never_the_token(fake, tmp_path):
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--plan", "standard", "--hostname", "shop.tuvl.app", "--no-admin", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0, res.output
    assert TOKEN not in res.output and "A" * 43 not in res.output
    assert "http://shop.tuvl.app" in res.output and "Deployed" in res.output and "20,000 credits" in res.output and "ci on github" in res.output
    for step in ("upload", "build", "plan", "deploy"):
        assert step in res.output
    post = next(r for r in fake.requests if r.method == "POST")
    assert post.headers["authorization"] == f"Bearer {TOKEN}" and post.headers["content-type"].startswith("multipart/form-data")
    body = post.content
    assert b'name="plan"' in body and b"standard" in body and b'name="hostname"' in body and b"shop.tuvl.app" in body and b'filename="shop-0.1.0.tar.gz"' in body and b"admin_email" not in body
    assert not any(r.url.path == "/client/v1/seal-key" for r in fake.requests), "no administrator, no sealing"


def test_an_administrator_is_asked_for_sealed_and_shown_once_at_the_end(fake, tmp_path):
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--admin-email", "owner@acme.example", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0, res.output
    post = next(r for r in fake.requests if r.method == "POST")
    body = post.content.decode("latin-1")
    assert "owner@acme.example" in body and "admin_envelope" in body
    envelope = body.split('name="admin_envelope"')[1].split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0]
    password = fake.open(envelope, "shop")
    assert len(password) == 20 and password not in body, "the cloud can open it; nobody on the way can read it"
    assert res.output.count(password) == 1 and "Administrator created" in res.output and "Save the password now" in res.output


def test_a_failed_administrator_is_said_without_failing_the_deployment(fake, tmp_path):
    fake.finals[-1] = {**fake.finals[-1], "result": {**fake.finals[-1]["result"], "admin": "failed: database unreachable"}}
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--admin-email", "owner@acme.example", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0 and "could not be created" in res.output and "database unreachable" in res.output and "Password:" not in res.output


def test_a_failed_deployment_exits_non_zero_with_the_reason(fake, tmp_path):
    fake.finals[-1] = {**fake.job, "state": "failed", "step": "failed", "message": "the build was rejected: the image did not start healthy", "steps": [{"name": "upload", "detail": "", "at": 0}, {"name": "failed", "detail": "", "at": 1}]}
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--no-admin", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 1 and "The deployment failed" in res.output and "did not start healthy" in res.output


def test_a_refusal_when_starting_is_shown_and_exits_non_zero(fake, tmp_path):
    fake.start_status, fake.start_body = 402, "the wallet holds 100 credits and the hobby plan needs 5000; ask the platform to add credit"
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--no-admin", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 1 and "wallet holds 100" in res.output and TOKEN not in res.output


def test_no_follow_starts_it_and_returns(fake, tmp_path):
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--no-admin", "--no-follow", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0 and "started" in res.output and fake.job_polls == []


def test_an_administrator_and_no_admin_together_are_a_usage_error(fake, tmp_path):
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--admin-email", "a@b.example", "--no-admin", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 2 and fake.requests == []


def test_login_checks_the_token_with_the_cloud_before_saving_it(fake):
    res = run("--login", "--token-stdin", "--url", URL, input=TOKEN + "\n")
    assert res.exit_code == 0 and "ci on github" in res.output and TOKEN not in res.output and cloud.stored_token(URL) == TOKEN
    bad = run("--login", "--token-stdin", "--url", URL, input=OTHER + "\n")
    assert bad.exit_code == 1 and "expired or been revoked" in bad.output and cloud.stored_token(URL) == TOKEN, "a token the cloud refuses is not saved, and the old one stays"
    junk = run("--login", "--token-stdin", "--url", URL, input="not a token\n")
    assert junk.exit_code == 1 and "not a client token" in junk.output


def test_a_saved_login_is_used_and_logout_forgets_it(fake, tmp_path):
    cloud.store_token(URL, TOKEN)
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--no-admin")
    assert res.exit_code == 0 and "the saved login" in " ".join(res.output.split())
    out = run("--logout", "--url", URL)
    assert out.exit_code == 0 and "Forgot" in out.output and cloud.stored_token(URL) is None
    assert "There was no saved token" in " ".join(run("--logout", "--url", URL).output.split())


def test_the_token_can_be_piped_in_for_ci(fake, tmp_path):
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--no-admin", "--token-stdin", input=OTHER + "\n")
    assert res.exit_code == 1 and "expired or been revoked" in res.output, "the piped token is the one used, and the cloud refused it"
    ok = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--no-admin", "--token-stdin", input=TOKEN + "\n")
    assert ok.exit_code == 0 and "standard input" in ok.output


def test_the_project_is_packaged_with_tuvl_ship_and_a_failed_ship_uploads_nothing(fake, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    calls = []

    class Done:
        def __init__(self, code, out=""):
            self.returncode, self.stdout, self.stderr = code, out, ""

    def good(cmd, **kw):
        calls.append(cmd)
        out = Path(cmd[cmd.index("--out") + 1])
        make_archive(out)
        return Done(0)

    monkeypatch.setattr(deploy_mod.subprocess, "run", good)
    res = run("-d", str(project), "--url", URL, "--no-admin", "--allow-dirty", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0, res.output
    cmd = calls[0]
    assert cmd[1:4] == ["-c", "from tuvl.cli.main import main; main()", "ship"] and cmd[4:6] == ["--format", "archive"] and "--allow-dirty" in cmd and "--strict" not in cmd and cmd[cmd.index("-d") + 1] == str(project)
    fake.requests.clear()
    monkeypatch.setattr(deploy_mod.subprocess, "run", lambda cmd, **kw: Done(1, "validation failed: something"))
    res = run("-d", str(project), "--url", URL, "--no-admin", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 1 and "nothing was uploaded" in res.output and not any(r.method == "POST" for r in fake.requests)


def test_the_project_name_is_read_from_an_archive_and_an_unreadable_one_gives_nothing(tmp_path):
    assert deploy_mod._slug_of(make_archive(tmp_path, "shop")) == "shop"
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tarball")
    assert deploy_mod._slug_of(bad) == "" and deploy_mod._slug_of(tmp_path / "missing.tar.gz") == ""


def test_deploy_is_listed_in_the_help_screen():
    assert "deploy" in runner.invoke(app, ["help"]).output


# ---- the name: --check-dns and --set-hostname

def project_dir(tmp_path, name="shop"):
    d = tmp_path / "proj"
    d.mkdir(exist_ok=True)
    (d / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    return d


def never_ship(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("nothing may be packaged")
    monkeypatch.setattr(deploy_mod.subprocess, "run", boom)


def test_check_dns_says_a_name_is_available_and_points_at_the_cloud_without_packaging_or_uploading(fake, tmp_path, monkeypatch):
    never_ship(monkeypatch)
    res = run("--check-dns", "--hostname", "cafe.tuvl.app", "-d", str(project_dir(tmp_path)), "--url", URL, env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "cafe.tuvl.app: that name is available" in flat and "DNS: ok" in flat and "resolves to this machine" in flat
    assert [r.method for r in fake.requests if "hostnames" not in r.url.path and "/me" not in r.url.path] == []
    q = next(r for r in fake.requests if "hostnames" in r.url.path).url.params
    assert q["name"] == "cafe.tuvl.app" and q["project"] == "shop", "the project comes from pyproject.toml"


def test_check_dns_with_no_name_checks_the_projects_default_name_and_a_project_can_be_named(fake, tmp_path, monkeypatch):
    never_ship(monkeypatch)
    res = run("--check-dns", "--project", "blog", "--url", URL, env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0 and "blog.tuvl.app" in res.output
    q = next(r for r in fake.requests if "hostnames" in r.url.path).url.params
    assert q["name"] == "" and q["project"] == "blog"


def test_a_taken_or_unusable_name_exits_non_zero_and_a_name_that_does_not_resolve_yet_is_only_a_warning(fake, tmp_path):
    fake.check["availability"] = {"status": "taken", "message": "that name is already used by another project; choose another"}
    fake.check["dns"] = None
    res = run("--check-dns", "--hostname", "blog.tuvl.app", "-d", str(project_dir(tmp_path)), "--url", URL, env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 1 and "already used by another project" in " ".join(res.output.split()) and "DNS" not in res.output
    fake.check["availability"] = {"status": "available", "message": "that name is available"}
    fake.check["dns"] = {"status": "missing", "message": "shop.tuvl.app does not resolve yet. Create the record, then wait"}
    res = run("--check-dns", "--hostname", "shop.tuvl.app", "-d", str(project_dir(tmp_path)), "--url", URL, env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0 and "DNS: missing" in " ".join(res.output.split()), "the name is usable; its record is the operator's to create"
    fake.check["availability"] = {"status": "yours", "message": "your project already serves that name"}
    assert run("--check-dns", "--hostname", "shop.tuvl.app", "-d", str(project_dir(tmp_path)), "--url", URL, env={cloud.TOKEN_ENV: TOKEN}).exit_code == 0


def test_set_hostname_renames_the_deployed_app_and_nothing_else(fake, tmp_path, monkeypatch):
    never_ship(monkeypatch)
    fake.finals[-1] = {**fake.job, "id": "cd" * 16, "state": "succeeded", "step": "done", "steps": [{"name": "restart", "detail": "cafe.tuvl.app", "at": 1}, {"name": "done", "detail": "", "at": 2}],
                       "result": {"project": "shop", "url": "http://cafe.tuvl.app", "hostname": "cafe.tuvl.app", "admin": "", "plan": "hobby"}}
    fake.finals[:] = fake.finals[-1:]
    res = run("--set-hostname", "Cafe.Tuvl.App", "-d", str(project_dir(tmp_path)), "--url", URL, env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "Renamed" in flat and "http://cafe.tuvl.app" in flat and "old name no longer works" in flat and TOKEN not in res.output
    post = next(r for r in fake.requests if r.method == "POST")
    assert post.url.path == "/client/v1/projects/shop/hostname" and json.loads(post.content) == {"hostname": "cafe.tuvl.app"}
    assert not any(r.url.path == "/client/v1/deployments" and r.method == "POST" for r in fake.requests)


def test_a_refused_rename_is_shown_and_exits_non_zero(fake, tmp_path):
    for status, text in ((409, "that name is already used by another project; choose another"), (422, "a name must be under tuvl.app, for example shop.tuvl.app"), (404, "you have no project by that name")):
        fake.rename_status, fake.rename_body = status, text
        res = run("--set-hostname", "x.tuvl.app", "-d", str(project_dir(tmp_path)), "--url", URL, env={cloud.TOKEN_ENV: TOKEN})
        assert res.exit_code == 1 and text in " ".join(res.output.split())


def test_a_rename_that_fails_on_the_cloud_exits_non_zero_with_the_reason(fake, tmp_path):
    fake.finals[:] = [{**fake.job, "id": "cd" * 16, "state": "failed", "step": "failed", "message": "the name could not be changed: the platform refused it", "steps": [{"name": "failed", "detail": "", "at": 1}]}]
    res = run("--set-hostname", "x.tuvl.app", "-d", str(project_dir(tmp_path)), "--url", URL, env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 1 and "The rename failed" in res.output and "could not be changed" in " ".join(res.output.split())


@pytest.mark.parametrize("args", [["--set-hostname", "a.tuvl.app", "--hostname", "b.tuvl.app"], ["--set-hostname", "a.tuvl.app", "--admin-email", "a@b.example"], ["--check-dns", "--admin-email", "a@b.example"]])
def test_options_that_do_not_go_together_are_a_usage_error_before_anything_is_sent(fake, tmp_path, args):
    res = run(*args, "-d", str(project_dir(tmp_path)), "--url", URL, env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 2 and fake.requests == []
    archive = make_archive(tmp_path)
    assert run("--set-hostname", "a.tuvl.app", "--archive", str(archive), "--url", URL, env={cloud.TOKEN_ENV: TOKEN}).exit_code == 2
    assert run("--check-dns", "--archive", str(archive), "--url", URL, env={cloud.TOKEN_ENV: TOKEN}).exit_code == 2


def test_the_project_name_for_a_check_or_rename_is_read_the_way_ship_reads_it(tmp_path):
    assert deploy_mod._project_slug(project_dir(tmp_path, "My Shop_App")) == "my-shop-app"


def test_a_finished_deployment_points_at_the_name_check(fake, tmp_path):
    res = run("--archive", str(make_archive(tmp_path)), "--url", URL, "--no-admin", env={cloud.TOKEN_ENV: TOKEN})
    assert res.exit_code == 0 and "tuvl deploy --check-dns" in res.output
