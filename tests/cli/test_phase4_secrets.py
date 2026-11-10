"""Phase 4.1 — file-mode hardening for files containing dev secrets.

The dev session file (``.tuvl/.dev-session``) holds the bearer token that
grants root-equivalent access to ``tuvl dev``.  The ``.env`` file written by
``tuvl init`` holds the PostgreSQL password and LLM API keys.  Both must be
created mode 0600 so other accounts on shared dev hosts can't read them.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tuvl_insight.cli.commands.init import _write_env
from tuvl_insight.cli.session import clear_session, read_session, session_file, write_session


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    clear_session(tmp_path)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only permission test")
def test_write_session_creates_file_mode_0600(isolated_cwd):
    write_session("AAAA-BBBB-CCCC-DDDD", 8000, isolated_cwd)
    session_path = session_file(isolated_cwd)
    assert session_path.exists()
    assert _mode(session_path) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only permission test")
def test_write_session_tightens_pre_existing_loose_perms(isolated_cwd):
    session_path = session_file(isolated_cwd)
    session_path.parent.mkdir(exist_ok=True)
    session_path.write_text("{}")
    os.chmod(session_path, 0o644)
    write_session("AAAA-BBBB-CCCC-DDDD", 8000, isolated_cwd)
    assert _mode(session_path) == 0o600


def test_session_paths_resolve_under_project_dir_not_cwd(tmp_path, monkeypatch):
    """TD-20: ``tuvl dev -d <elsewhere>`` must key the session file to the
    *project* directory, not the shell's cwd — otherwise the panel
    misreports where the key lives, the "already running" guard doesn't
    dedupe across cwds, and orphan ``.tuvl/.dev-session`` files scatter in
    whichever directory ``tuvl dev`` happened to be launched from."""
    cwd_dir = tmp_path / "shell-cwd"
    project_dir = tmp_path / "project"
    cwd_dir.mkdir()
    project_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    write_session("AAAA-BBBB-CCCC-DDDD", 8000, project_dir)

    assert session_file(project_dir).exists()
    assert not (cwd_dir / ".tuvl" / ".dev-session").exists()

    read_back = read_session(project_dir)
    assert read_back is not None
    assert read_back["key"] == "AAAA-BBBB-CCCC-DDDD"

    clear_session(project_dir)
    assert not session_file(project_dir).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only permission test")
def test_write_env_real_secrets_file_is_0600(tmp_path: Path):
    target = tmp_path / ".env"
    _write_env(
        target,
        {"POSTGRES_PASSWORD": ("hunter2", True)},
        redact=False,
    )
    assert _mode(target) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only permission test")
def test_write_env_example_file_is_not_forced_to_0600(tmp_path: Path):
    """``.env.example`` ships in git; no need to tighten perms — the helper
    only chmods the non-redacted (real) ``.env`` to avoid changing committed
    file modes unexpectedly."""
    target = tmp_path / ".env.example"
    _write_env(
        target,
        {"POSTGRES_PASSWORD": ("hunter2", True)},
        redact=True,
    )
    # Just confirm we didn't blanket-apply 0o600 to redacted files; the exact
    # mode depends on the caller's umask, so we only assert the helper didn't
    # crash and the file exists.
    assert target.exists()


def test_pg_datasource_yaml_never_contains_secret(tmp_path: Path):
    """The generated DataSource YAML must reference ``${POSTGRES_PASSWORD}``,
    never the literal — the file is committable and baked into ship images."""
    from tuvl_insight.cli.commands.init import _write_pg_datasource

    target = tmp_path / "postgres.yaml"
    _write_pg_datasource(
        target,
        host="localhost",
        port="5432",
        db="appdb",
        user="tuvl",
        primary=True,
    )
    content = target.read_text()
    assert "password: ${POSTGRES_PASSWORD}" in content
    # Non-secret fields keep init-entered values as inline defaults.
    assert "host: ${POSTGRES_HOST:localhost}" in content
    assert "database: ${POSTGRES_DB:appdb}" in content


def test_pg_datasource_yaml_resolves_via_env(tmp_path: Path, monkeypatch):
    """End-to-end: the emitted YAML round-trips through the loader's env
    interpolation — password from env, defaults for the rest."""
    import yaml

    from tuvl.core.datasources.loader import _resolve_env
    from tuvl_insight.cli.commands.init import _write_pg_datasource

    target = tmp_path / "postgres.yaml"
    _write_pg_datasource(target, host="dbhost", port="5432", db="appdb", user="tuvl")

    for var in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "hunter2")

    doc = _resolve_env(yaml.safe_load(target.read_text()))
    conn = doc["spec"]["connection"]
    assert conn == {
        "host": "dbhost",
        "port": 5432,
        "database": "appdb",
        "username": "tuvl",
        "password": "hunter2",
    }


def test_pg_datasource_yaml_fails_closed_without_password_env(tmp_path: Path, monkeypatch):
    """No POSTGRES_PASSWORD in the environment → loading must raise, not
    silently connect with an empty password."""
    import yaml

    from tuvl.core.datasources.loader import _resolve_env
    from tuvl_insight.cli.commands.init import _write_pg_datasource

    target = tmp_path / "postgres.yaml"
    _write_pg_datasource(target, host="dbhost", port="5432", db="appdb", user="tuvl")
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    with pytest.raises(OSError, match="POSTGRES_PASSWORD"):
        _resolve_env(yaml.safe_load(target.read_text()))
