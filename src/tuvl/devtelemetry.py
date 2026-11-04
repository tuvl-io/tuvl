"""Developer telemetry: what developers do on a project, shared through a collector the team runs.

Optional and off unless ``.tuvl/dev-telemetry.yaml`` says otherwise. Every audit event (init, idea, spec,
approval, task, agent attempt, branch, commit, gate, ship …) and one ``cli.command`` event per command is
queued in ``.tuvl/audit/outbox.queue`` and sent in batches, so several developers' work lands in one place, and the
same server lets each developer read the team's events back. The local audit ledger stays the record; this never
blocks or fails a command, and a server that is down only delays delivery. It is development-time only:
``tuvl ship`` refuses the config file and the outbox.

Each developer registers once at the collector (``tuvl register developer``) and gets a token of their own; the
token both sends and reads. The server speaks (all under ``<server>/v1``):

* ``POST /register``     ``{name, email, team_code}`` → ``{developer, token}``
* ``GET  /me``           who the token belongs to
* ``POST /dev-events``   ``{"schema": "tuvl.dev-events/1", "sender": {...}, "events": [...]}``; events carry a
  unique ``id`` so delivery can be at-least-once
* ``GET  /team/events``  the events of the teams the developer belongs to (``project``, ``actor``, ``entity``,
  ``kind``, ``since``, ``before``, ``limit``)

with ``Authorization: Bearer <token>`` on all but ``/register``. The collector decides who a token is; what an
event claims about its actor is not trusted.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

CONFIG = ".tuvl/dev-telemetry.yaml"
OUTBOX = ".tuvl/audit/outbox.queue"
STATE = ".tuvl/audit/telemetry-state.json"
SCHEMA = "tuvl.dev-events/1"
DEFAULT_TOKEN_ENV = "TUVL_DEV_TELEMETRY_TOKEN"
IDENTITIES = ("full", "initials")
BATCH = 100
MAX_QUEUE = 5000
_LOCAL = {"localhost", "127.0.0.1", "::1"}


if os.name == "nt":
    import msvcrt

    def _lock(fh: Any, *, wait: bool) -> None:
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not wait:
                    raise
                time.sleep(0.05)

    def _unlock(fh: Any) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fh: Any, *, wait: bool) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))

    def _unlock(fh: Any) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)


class TelemetryError(ValueError):
    pass


@dataclass
class Config:
    enabled: bool = False
    server: str = ""
    token_env: str = DEFAULT_TOKEN_ENV
    identity: str = "initials"
    commands: bool = True

    def to_yaml(self) -> str:
        head = (
            "# Developer telemetry: your team's collector (tuvl show dev-telemetry). Commit this file.\n"
            "# Each developer registers once (tuvl register developer); the token is kept in the user's\n"
            "# own config folder, or in the environment variable named by token_env, never here.\n"
            "# Development only: `tuvl ship` keeps this file and the outbox out of the image.\n"
        )
        body = {
            "kind": "DevTelemetry",
            "version": "tuvl/v2",
            "telemetry": {
                "enabled": self.enabled,
                "server": self.server,
                "token_env": self.token_env,
                "identity": self.identity,
                "commands": self.commands,
            },
        }
        return head + str(yaml.safe_dump(body, sort_keys=False))


def check_server(url: str) -> str:
    """https only (plain http only for this machine, to try a collector); no credentials in the URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise TelemetryError("the server must be an https:// URL")
    if parsed.username or parsed.password:
        raise TelemetryError("the server must not contain credentials; each developer has a token")
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL:
        raise TelemetryError("the server must be https (plain http is accepted only for localhost)")
    return url.rstrip("/")


def load(project: Path) -> Config | None:
    try:
        doc = yaml.safe_load((project / CONFIG).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    raw = doc.get("telemetry") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        return None
    identity = str(raw.get("identity", "initials"))
    return Config(
        enabled=bool(raw.get("enabled", False)),
        server=str(raw.get("server", "")),
        token_env=str(raw.get("token_env", DEFAULT_TOKEN_ENV)),
        identity=identity if identity in IDENTITIES else "initials",
        commands=bool(raw.get("commands", True)),
    )


def save(project: Path, config: Config) -> Path:
    if config.server:
        config.server = check_server(config.server)
    if config.enabled and not config.server:
        raise TelemetryError("give the collector's address")
    if config.identity not in IDENTITIES:
        raise TelemetryError(f"identity must be one of {', '.join(IDENTITIES)}")
    path = project / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.to_yaml())
    return path


def active(project: Path) -> Config | None:
    config = load(project)
    return config if config is not None and config.enabled and config.server else None


# ── the developer's own token ────────────────────────────────────────────────


def credentials_path() -> Path:
    home = os.environ.get("TUVL_HOME")
    return (Path(home) if home else Path.home() / ".config" / "tuvl") / "dev-telemetry.json"


def _credentials() -> dict[str, Any]:
    try:
        data = json.loads(credentials_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def credentials_for(server: str) -> dict[str, Any] | None:
    found = _credentials().get(server.rstrip("/"))
    return found if isinstance(found, dict) and found.get("token") else None


def _store(server: str, entry: dict[str, Any] | None) -> None:
    data = _credentials()
    if entry is None:
        data.pop(server.rstrip("/"), None)
    else:
        data[server.rstrip("/")] = entry
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )  # only this user may read the token
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def forget(server: str) -> None:
    _store(server, None)


def token_for(config: Config) -> tuple[str, str]:
    """The token and where it came from: the environment variable first, else this developer's registration."""
    env = os.environ.get(config.token_env, "")
    if env:
        return env, "env"
    found = credentials_for(config.server)
    return (str(found["token"]), "registered") if found else ("", "none")


# ── talking to the collector ─────────────────────────────────────────────────


def _http(
    method: str,
    url: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> Any:
    import httpx

    # a company's own certificate authority: TUVL_DEV_TELEMETRY_CA (or SSL_CERT_FILE) names its bundle
    verify: Any = os.environ.get("TUVL_DEV_TELEMETRY_CA") or os.environ.get("SSL_CERT_FILE") or True
    headers = {"user-agent": "tuvl-dev-telemetry"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False, verify=verify
        ) as client:
            response = client.request(method, url, headers=headers, json=body, params=params)
    except httpx.HTTPError as exc:
        raise TelemetryError(str(exc) or exc.__class__.__name__) from exc
    if not 200 <= response.status_code < 300:
        detail = ""
        with contextlib.suppress(ValueError):
            detail = str(response.json().get("detail", ""))
        raise TelemetryError(
            f"the server answered {response.status_code}" + (f": {detail}" if detail else "")
        )
    return response.json() if response.content else {}


def register(
    server: str,
    *,
    name: str,
    email: str,
    team_code: str,
    token_env: str = DEFAULT_TOKEN_ENV,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Register this developer at the collector and keep the token in the user's own config folder."""
    server = check_server(server)
    answer = _http(
        "POST",
        f"{server}/v1/register",
        body={"name": name, "email": email, "team_code": team_code},
        timeout=timeout,
    )
    token = str(answer.get("token", ""))
    if not token:
        raise TelemetryError("the server did not return a token")
    developer = dict(answer.get("developer") or {})
    _store(server, {"token": token, "developer": developer, "registered_at": time.time()})
    return developer


def rotate(project: Path, *, timeout: float = 10.0) -> None:
    """Ask the collector for a new token and keep it; the old one stops working at once."""
    config = load(project)
    if config is None or not config.server:
        raise TelemetryError("no collector is configured")
    found = credentials_for(config.server)
    token, source = token_for(config)
    if not found or source != "registered":
        raise TelemetryError(
            "rotate the token you registered with: run `tuvl register developer` first"
        )
    answer = _http(
        "POST", f"{config.server}/v1/token/rotate", token=token, body={}, timeout=timeout
    )
    new = str(answer.get("token", ""))
    if not new:
        raise TelemetryError("the server did not return a token")
    _store(config.server, {**found, "token": new, "rotated_at": time.time()})


def whoami(project: Path, *, timeout: float = 5.0) -> dict[str, Any]:
    config = load(project)
    if config is None or not config.server:
        raise TelemetryError("no collector is configured")
    token, _ = token_for(config)
    if not token:
        raise TelemetryError(f"no token: run `tuvl register developer`, or set {config.token_env}")
    return dict(_http("GET", f"{config.server}/v1/me", token=token, timeout=timeout))


def team_events(root: Path, *, timeout: float = 8.0, **filters: Any) -> list[dict[str, Any]]:
    """The team's events, newest first: ``project``, ``actor``, ``entity``, ``kind``, ``before``, ``limit``."""
    config = load(root)
    if config is None or not config.server:
        raise TelemetryError("no collector is configured")
    token, _ = token_for(config)
    if not token:
        raise TelemetryError(f"no token: run `tuvl register developer`, or set {config.token_env}")
    params = {k: v for k, v in filters.items() if v not in (None, "")}
    answer = _http(
        "GET", f"{config.server}/v1/team/events", token=token, params=params, timeout=timeout
    )
    return list(answer.get("events", []))


# ── the outbox ───────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _locked(project: Path) -> Iterator[None]:
    lock = project / ".tuvl" / "audit" / "outbox.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as fh:
        _lock(fh, wait=True)
        try:
            yield
        finally:
            _unlock(fh)


def queued(project: Path) -> int:
    try:
        return sum(1 for ln in (project / OUTBOX).read_text().splitlines() if ln.strip())
    except OSError:
        return 0


def _shape(config: Config, event: dict[str, Any]) -> dict[str, Any]:
    """The event as it leaves this machine: with ``initials`` identity the git name and email are removed from the
    actor and from the text (an approval's detail names the approver)."""
    out = dict(event)
    actor = dict(out.get("actor") or {})
    if config.identity == "initials":
        git = str(actor.pop("git", ""))
        initials = str(actor.get("initials", "")) or "developer"
        name, _, rest = git.partition(" <")
        email = rest.rstrip(">")
        for secret in (git, name, email):
            if len(secret) >= 3:
                out["detail"] = str(out.get("detail", "")).replace(secret, initials)
                out["refs"] = json.loads(
                    json.dumps(out.get("refs") or {}).replace(secret, initials)
                )
    out["actor"] = actor
    return out


_touched: set[Path] = set()
_wake = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def enqueue(project: Path, event: dict[str, Any]) -> None:
    """Queue one event for delivery; a no-op unless telemetry is on. Never raises."""
    with contextlib.suppress(Exception):
        config = active(project)
        if config is None:
            return
        line = json.dumps(_shape(config, event), sort_keys=True, separators=(",", ":"))
        with _locked(project):
            path = project / OUTBOX
            lines = path.read_text().splitlines() if path.exists() else []
            lines.append(line)
            path.write_text("\n".join(lines[-MAX_QUEUE:]) + "\n")
        _touched.add(project.resolve())
        _start_thread()
        _wake.set()


def record_command(project: Path, command: str, *, exit_code: int, seconds: float) -> None:
    """A ``cli.command`` event: which command ran, how it ended and how long it took (no arguments)."""
    with contextlib.suppress(Exception):
        config = active(project)
        if config is None or not config.commands or not command:
            return
        from tuvl.audit import ledger

        enqueue(
            project,
            {
                "id": ledger._ulid(time.time(), ""),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "kind": "cli.command",
                "command": command,
                "actor": ledger.actor(project),
                "entities": [],
                "result": "ok" if exit_code == 0 else "failed",
                "detail": "",
                "refs": {"exit_code": exit_code, "seconds": round(seconds, 2)},
            },
        )


# ── delivery ─────────────────────────────────────────────────────────────────


def _sender(project: Path) -> dict[str, Any]:
    from tuvl import __version__, gitflow
    from tuvl.core.project_info import load_project

    record = load_project(project) or {}
    return {
        "tuvl": __version__,
        "project": str(record.get("id", "")),
        "name": str(record.get("name", project.name)),
        "host": hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12],
        "branch": gitflow.current_branch(project),
    }


def _post(project: Path, config: Config, events: list[dict[str, Any]], timeout: float) -> None:
    token, _ = token_for(config)
    if not token:
        raise TelemetryError(f"no token: run `tuvl register developer`, or set {config.token_env}")
    _http(
        "POST",
        f"{config.server}/v1/dev-events",
        token=token,
        body={"schema": SCHEMA, "sender": _sender(project), "events": events},
        timeout=timeout,
    )


def _state(project: Path, **fields: Any) -> dict[str, Any]:
    path = project / STATE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if fields:
        data.update(fields)
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
    return data  # type: ignore[no-any-return]


def local_identity(project: Path) -> dict[str, str]:
    """Who this machine says you are: the initials from ``tuvl init`` and the name and email from ``git config``."""
    from tuvl import gitutil
    from tuvl.coding_agents import read_settings

    settings = read_settings(project)
    return {
        "initials": settings.initials if settings else "",
        "name": gitutil.config_get(project, "user.name"),
        "email": gitutil.config_get(project, "user.email"),
    }


def status(project: Path) -> dict[str, Any]:
    config = load(project)
    state = _state(project)
    token, source = token_for(config) if config and config.server else ("", "none")
    found = credentials_for(config.server) if config and config.server else None
    return {
        "configured": config is not None,
        "enabled": bool(config and config.enabled),
        "server": config.server if config else "",
        "token_env": config.token_env if config else DEFAULT_TOKEN_ENV,
        "token_set": bool(token),
        "token_source": source,
        "developer": (found or {}).get("developer") or {},
        "local": local_identity(project),
        "identity": config.identity if config else "initials",
        "commands": config.commands if config else True,
        "queued": queued(project),
        "last_ok": state.get("last_ok"),
        "last_error": state.get("last_error", ""),
        "sent": state.get("sent", 0),
    }


def flush(project: Path, *, timeout: float = 3.0) -> dict[str, Any]:
    """Send what is queued, oldest first, in batches; what the server did not accept stays queued.

    One sender at a time per project (another process or thread already sending means this one has nothing to do).
    """
    config = active(project)
    if config is None:
        return {"sent": 0, "queued": queued(project), "error": ""}
    guard = (project / ".tuvl" / "audit" / "outbox.send").open("w")
    try:
        _lock(guard, wait=False)
    except OSError:
        guard.close()
        return {"sent": 0, "queued": queued(project), "error": "", "busy": True}
    try:
        return _flush_locked(project, config, timeout)
    finally:
        _unlock(guard)
        guard.close()


def _flush_locked(project: Path, config: Config, timeout: float) -> dict[str, Any]:
    sent, error = 0, ""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            with _locked(project):
                path = project / OUTBOX
                lines = [
                    ln
                    for ln in (path.read_text().splitlines() if path.exists() else [])
                    if ln.strip()
                ]
            if not lines:
                break
            batch = lines[:BATCH]
            _post(
                project,
                config,
                [json.loads(ln) for ln in batch],
                max(0.5, deadline - time.monotonic()),
            )
            with _locked(project):  # drop what was sent; keep anything queued meanwhile
                current = [
                    ln
                    for ln in (path.read_text().splitlines() if path.exists() else [])
                    if ln.strip()
                ]
                path.write_text("".join(f"{ln}\n" for ln in current[len(batch) :]))
            sent += len(batch)
    except Exception as exc:  # the server is down or refused: the events stay queued
        error = str(exc)[:200] or exc.__class__.__name__
    state = _state(project)
    _state(
        project,
        sent=state.get("sent", 0) + sent,
        **({"last_ok": time.time(), "last_error": ""} if not error else {"last_error": error}),
    )
    return {"sent": sent, "queued": queued(project), "error": error}


def ping(project: Path, *, timeout: float = 5.0) -> str:
    """Whether the collector knows this developer's token: ``""`` when it does, else why not."""
    try:
        whoami(project, timeout=timeout)
    except Exception as exc:
        return str(exc)[:200] or exc.__class__.__name__
    return ""


def _worker() -> None:
    while True:
        _wake.wait()
        time.sleep(1.0)  # let a burst of events gather into one request
        _wake.clear()
        for project in list(_touched):
            with contextlib.suppress(Exception):
                flush(project, timeout=5.0)


def _start_thread() -> None:
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="tuvl-dev-telemetry", daemon=True)
            _thread.start()


def shutdown(timeout: float = 3.0) -> None:
    """At the end of a command: send what this process queued, within *timeout* seconds in all."""
    deadline = time.monotonic() + timeout
    for project in list(_touched):
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            break
        with contextlib.suppress(Exception):
            flush(project, timeout=remaining)
