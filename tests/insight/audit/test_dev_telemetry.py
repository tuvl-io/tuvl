"""Developer telemetry: off unless configured, queued durably, sent with the developer's own token, never in the way."""

import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight import devtelemetry as dt
from tuvl_insight.audit import ledger as lg
from tuvl_insight.coding_agents import Settings, write_settings

runner = CliRunner()
TOKEN = "tdev_s3cret"
ROTATED = "tdev_rotated"


class Collector:
    """A stand-in for the team's collector: records requests, answers like it."""

    def __init__(self) -> None:
        self.posts: list[tuple[dict, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.status = 200
        self.token = TOKEN
        self.feed: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _auth(self) -> bool:
                return self.headers.get("authorization") == f"Bearer {outer.token}"

            def _send(self, code: int, body: dict | None = None) -> None:
                self.send_response(code)
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers["content-length"]))
                body = json.loads(raw)
                headers = {k.lower(): v for k, v in self.headers.items()}
                if self.path == "/v1/register":
                    outer.posts.append((headers, body))
                    if body["team_code"] != "join_ok":
                        return self._send(403, {"detail": "that join code is not valid"})
                    return self._send(
                        201,
                        {
                            "token": outer.token,
                            "developer": {
                                "name": body["name"],
                                "email": body["email"],
                                "team": "payments",
                            },
                        },
                    )
                if not self._auth():
                    return self._send(401, {"detail": "unknown token"})
                if self.path == "/v1/token/rotate":
                    outer.token = ROTATED
                    return self._send(200, {"token": ROTATED})
                outer.posts.append((headers, body))
                self._send(outer.status, {"accepted": len(body["events"])})

            def do_GET(self) -> None:  # noqa: N802
                url = urlparse(self.path)
                if not self._auth():
                    return self._send(401, {"detail": "unknown token"})
                outer.gets.append((url.path, {k: v[0] for k, v in parse_qs(url.query).items()}))
                if url.path == "/v1/me":
                    return self._send(
                        200, {"name": "Ada", "email": "ada@example.org", "team": "payments"}
                    )
                self._send(200, {"events": outer.feed})

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "proj"
    (p / ".tuvl").mkdir(parents=True)
    write_settings(p, Settings("SK", "none"))
    monkeypatch.setattr(
        "tuvl_insight.gitutil.config_get",
        lambda _p, key: {"user.name": "Ada", "user.email": "ada@example.org"}.get(key, ""),
    )
    monkeypatch.setenv("TUVL_HOME", str(tmp_path / "home"))  # the developer's own config folder
    monkeypatch.delenv(dt.DEFAULT_TOKEN_ENV, raising=False)
    monkeypatch.setattr(dt, "_start_thread", lambda: None)  # deliveries are driven by the tests
    return p


def turn_on(project: Path, url: str, **kw) -> None:
    dt.save(project, dt.Config(enabled=True, server=url, **kw))


def event(project: Path) -> None:
    lg.append(
        project, "idea.imported", command="import idea", entities=["IDEA-SK-0001"], detail="Refunds"
    )


def test_nothing_is_queued_unless_telemetry_is_on(project: Path) -> None:
    event(project)
    assert dt.queued(project) == 0 and not (project / dt.OUTBOX).exists()
    dt.save(project, dt.Config(enabled=False, server="https://t.example.com"))
    event(project)
    assert dt.queued(project) == 0


def test_an_event_is_delivered_with_the_developers_token_and_leaves_the_queue(
    project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dt.DEFAULT_TOKEN_ENV, TOKEN)
    turn_on(project, collector.url)
    event(project)
    assert dt.queued(project) == 1
    assert dt.flush(project) == {"sent": 1, "queued": 0, "error": ""}
    headers, body = collector.posts[0]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert body["schema"] == dt.SCHEMA and body["sender"]["tuvl"] and "team" not in body["sender"]
    sent = body["events"][0]
    assert sent["kind"] == "idea.imported" and sent["entities"] == ["IDEA-SK-0001"] and sent["id"]
    assert sent["actor"] == {"initials": "SK"}  # the default does not send the name or email
    assert dt.status(project)["sent"] == 1 and dt.status(project)["last_error"] == ""


def test_with_initials_identity_the_approvers_name_is_taken_out_of_the_text_too(
    project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dt.DEFAULT_TOKEN_ENV, TOKEN)
    turn_on(project, collector.url)
    lg.append(
        project,
        "spec.approved",
        command="approve spec",
        entities=["SPEC-SK-0001-01"],
        detail="Ada <ada@example.org> (body abc)",
        refs={"by": "ada@example.org"},
    )
    dt.flush(project)
    sent = collector.posts[0][1]["events"][0]
    assert "Ada" not in json.dumps(sent) and "ada@example.org" not in json.dumps(sent)
    assert sent["detail"] == "SK (body abc)" and sent["actor"] == {"initials": "SK"}
    assert "Ada" in json.dumps(lg.read_events(project))  # the local ledger keeps it


def test_full_identity_sends_the_git_name(
    project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dt.DEFAULT_TOKEN_ENV, TOKEN)
    turn_on(project, collector.url, identity="full")
    event(project)
    dt.flush(project)
    assert collector.posts[0][1]["events"][0]["actor"]["git"] == "Ada <ada@example.org>"


def test_a_server_that_is_down_only_delays_delivery(
    project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dt.DEFAULT_TOKEN_ENV, TOKEN)
    turn_on(project, collector.url)
    collector.status = 503
    event(project)
    event(project)
    failed = dt.flush(project)
    assert failed["sent"] == 0 and failed["queued"] == 2 and "503" in failed["error"]
    assert "503" in dt.status(project)["last_error"]
    collector.status = 200
    assert dt.flush(project)["sent"] == 2 and dt.queued(project) == 0


def test_the_audit_ledger_still_works_when_the_collector_cannot_be_reached(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(dt.DEFAULT_TOKEN_ENV, TOKEN)
    turn_on(project, "http://127.0.0.1:9")  # nothing listens there
    event(project)
    assert len(lg.read_events(project)) == 1
    assert dt.flush(project, timeout=1.0)["error"]


def test_without_a_token_nothing_is_sent_and_it_says_how_to_get_one(
    project: Path, collector: Collector
) -> None:
    turn_on(project, collector.url)
    event(project)
    result = dt.flush(project)
    assert (
        result["sent"] == 0
        and "tuvl register developer" in result["error"]
        and dt.queued(project) == 1
    )
    assert collector.posts == []


def test_the_server_must_be_https_without_credentials(project: Path) -> None:
    for bad in (
        "http://t.example.com",
        "https://u:p@t.example.com",
        "ftp://t.example.com",
        "nonsense",
    ):
        with pytest.raises(dt.TelemetryError):
            dt.save(project, dt.Config(enabled=True, server=bad))
    dt.save(project, dt.Config(enabled=True, server="https://t.example.com/"))
    assert dt.load(project).server == "https://t.example.com"  # type: ignore[union-attr]
    dt.save(project, dt.Config(enabled=True, server="http://localhost:9000"))


def test_registering_keeps_the_token_in_the_users_own_folder_and_the_project_never_has_it(
    project: Path, collector: Collector
) -> None:
    turn_on(project, collector.url)
    developer = dt.register(collector.url, name="Ada", email="ada@example.org", team_code="join_ok")
    assert developer["team"] == "payments"
    path = dt.credentials_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and TOKEN in path.read_text()
    assert TOKEN not in (project / dt.CONFIG).read_text()
    config = dt.load(project)
    assert dt.token_for(config) == (TOKEN, "registered")  # type: ignore[arg-type]
    with pytest.raises(dt.TelemetryError, match="join code"):
        dt.register(collector.url, name="Eve", email="e@x.y", team_code="wrong")
    dt.forget(collector.url)
    assert dt.token_for(config) == ("", "none")  # type: ignore[arg-type]


def test_rotating_replaces_the_registered_token_and_only_that_one(
    project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_on(project, collector.url)
    with pytest.raises(dt.TelemetryError, match="register developer"):
        dt.rotate(project)  # nothing registered yet
    dt.register(collector.url, name="Ada", email="ada@example.org", team_code="join_ok")
    dt.rotate(project)
    assert dt.token_for(dt.load(project)) == (ROTATED, "registered")  # type: ignore[arg-type]
    assert (
        ROTATED in dt.credentials_path().read_text()
        and TOKEN not in dt.credentials_path().read_text()
    )
    assert ROTATED not in (project / dt.CONFIG).read_text()
    monkeypatch.setenv(dt.DEFAULT_TOKEN_ENV, "tdev_from_ci")
    with pytest.raises(dt.TelemetryError, match="registered with"):
        dt.rotate(project)  # a CI token is not ours to replace


def test_a_company_certificate_authority_is_trusted_for_every_request(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    seen: list[object] = []

    class Spy:
        def __init__(self, **kw: object) -> None:
            seen.append(kw["verify"])

        def __enter__(self) -> "Spy":
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def request(self, *a: object, **k: object):
            raise httpx.ConnectError("stop here")

    monkeypatch.setattr(httpx, "Client", Spy)
    monkeypatch.delenv("TUVL_DEV_TELEMETRY_CA", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    for _ in range(3):
        with pytest.raises(dt.TelemetryError):
            dt.register("https://t.example.com", name="A", email="a@b.c", team_code="x")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/company-ca.pem")
    with pytest.raises(dt.TelemetryError):
        dt.register("https://t.example.com", name="A", email="a@b.c", team_code="x")
    monkeypatch.setenv("TUVL_DEV_TELEMETRY_CA", "/etc/team-ca.pem")
    with pytest.raises(dt.TelemetryError):
        dt.register("https://t.example.com", name="A", email="a@b.c", team_code="x")
    assert seen[0] is True and seen[-2] == "/etc/company-ca.pem" and seen[-1] == "/etc/team-ca.pem"


def test_an_environment_token_wins_over_the_registered_one(
    project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_on(project, collector.url)
    dt.register(collector.url, name="Ada", email="ada@example.org", team_code="join_ok")
    monkeypatch.setenv(dt.DEFAULT_TOKEN_ENV, "tdev_from_ci")
    assert dt.token_for(dt.load(project)) == ("tdev_from_ci", "env")  # type: ignore[arg-type]


def test_one_event_per_command_without_its_arguments(
    project: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl.cli import _command_words

    assert _command_words(["generate", "code", "--spec", "SPEC-SK-0001-01"]) == "generate code"
    assert _command_words(["approve", "spec", "SPEC-SK-0001-01"]) == "approve spec"
    assert _command_words(["init", "refunds"]) == "init"  # a project name is not a command
    assert _command_words(["list", "tasks", "specs/x.md"]) == "list tasks"
    assert _command_words(["--version"]) == ""
    monkeypatch.setenv(dt.DEFAULT_TOKEN_ENV, TOKEN)
    turn_on(project, collector.url)
    dt.record_command(project, "lint spec", exit_code=1, seconds=0.4)
    dt.flush(project)
    sent = collector.posts[0][1]["events"][0]
    assert (
        sent["kind"] == "cli.command"
        and sent["command"] == "lint spec"
        and sent["result"] == "failed"
    )
    assert sent["refs"] == {"exit_code": 1, "seconds": 0.4}
    turn_on(project, collector.url, commands=False)
    dt.record_command(project, "lint spec", exit_code=0, seconds=0.1)
    assert dt.queued(project) == 0


def test_the_commands_set_register_show_check_and_read_the_team(
    project: Path, collector: Collector
) -> None:
    shown = runner.invoke(app, ["show", "dev-telemetry", "-d", str(project)])
    assert shown.exit_code == 0 and "not configured" in shown.output
    bad = runner.invoke(app, ["set", "dev-telemetry", "http://example.com", "-d", str(project)])
    assert bad.exit_code == 2 and "https" in bad.output
    done = runner.invoke(app, ["set", "dev-telemetry", collector.url, "-d", str(project)])
    assert done.exit_code == 0 and "tuvl register developer" in " ".join(done.output.split())

    refused = runner.invoke(
        app,
        [
            "register",
            "developer",
            "--name",
            "Ada",
            "--email",
            "ada@example.org",
            "--code",
            "wrong",
            "-d",
            str(project),
        ],
    )
    assert refused.exit_code == 1 and "join code" in refused.output
    ok = runner.invoke(
        app,
        [
            "register",
            "developer",
            "--name",
            "Ada",
            "--email",
            "ada@example.org",
            "--code",
            "join_ok",
            "-d",
            str(project),
        ],
    )
    assert ok.exit_code == 0 and "Registered" in ok.output and "payments" in ok.output

    rotated = runner.invoke(app, ["set", "dev-telemetry", "--rotate-token", "-d", str(project)])
    assert rotated.exit_code == 0 and "New token kept" in rotated.output
    assert dt.token_for(dt.load(project)) == (ROTATED, "registered")  # type: ignore[arg-type]
    collector.token = ROTATED

    event(project)
    shown = runner.invoke(app, ["show", "dev-telemetry", "-d", str(project)])
    flat = " ".join(shown.output.split())
    assert "Ada" in flat and "payments" in flat and "1 event(s) waiting" in flat
    assert (
        "from git: Ada <ada@example.org>" in flat and "events say: SK" in flat
    )  # what the machine says about you
    assert dt.status(project)["local"] == {
        "initials": "SK",
        "name": "Ada",
        "email": "ada@example.org",
    }
    checked = runner.invoke(app, ["check", "dev-telemetry", "--flush", "-d", str(project)])
    assert checked.exit_code == 0 and "knows you" in checked.output and dt.queued(project) == 0

    collector.feed = [
        {
            "id": "02",
            "ts": "2026-10-22T09:00:00.000Z",
            "kind": "task.failed",
            "result": "failed",
            "entities": ["TASK-SK-0001-01-003"],
            "detail": "attempt 2",
            "developer": {"name": "Bob"},
            "actor": {"initials": "BO"},
        }
    ]
    feed = runner.invoke(app, ["list", "team-activity", "--actor", "bob", "-d", str(project)])
    assert feed.exit_code == 0 and "Bob" in feed.output and "task.failed" in feed.output
    assert collector.gets[-1][1]["actor"] == "bob" and collector.gets[-1][1]["limit"] == "30"

    collector.token = "rotated"
    assert (
        runner.invoke(app, ["check", "dev-telemetry", "-d", str(project)]).exit_code == 1
    )  # the collector refuses it
    forgot = runner.invoke(app, ["set", "dev-telemetry", "--forget", "-d", str(project)])
    assert forgot.exit_code == 0 and dt.credentials_for(collector.url) is None
    off = runner.invoke(app, ["set", "dev-telemetry", "--off", "-d", str(project)])
    assert off.exit_code == 0 and dt.active(project) is None


def test_init_can_turn_it_on_by_flag_or_prompt_and_skips_it_by_default(tmp_path: Path) -> None:
    none = tmp_path / "a"
    assert runner.invoke(app, ["init", str(none), "--preset", "ollama", "-y"]).exit_code == 0
    assert not (none / dt.CONFIG).exists()

    flagged = tmp_path / "b"
    done = runner.invoke(
        app,
        [
            "init",
            str(flagged),
            "--preset",
            "ollama",
            "-y",
            "--dev-telemetry",
            "https://t.example.com/",
        ],
    )
    assert done.exit_code == 0, done.output
    cfg = dt.load(flagged)
    assert (
        cfg is not None
        and cfg.enabled
        and cfg.server == "https://t.example.com"
        and cfg.identity == "initials"
    )
    assert "tuvl register developer" in " ".join(done.output.split())

    bad = runner.invoke(
        app, ["init", str(tmp_path / "c"), "-y", "--dev-telemetry", "http://t.example.com"]
    )
    assert bad.exit_code == 2 and not (tmp_path / "c").exists()
