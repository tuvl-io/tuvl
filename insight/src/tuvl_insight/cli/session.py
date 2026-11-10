import contextlib
import json
import os
import secrets
import string
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SESSION_DIRNAME = ".tuvl"
SESSION_FILENAME = ".dev-session"

_KEY_CHARS = string.ascii_uppercase + string.digits


def session_dir(project_dir: Path) -> Path:
    """Return the ``.tuvl`` directory for ``project_dir``.

    Session state must live under the *project* being served, not the
    invoking shell's cwd — ``tuvl dev -d <elsewhere>`` targets a project
    directory that may differ from where the command was run.
    """
    return Path(project_dir) / SESSION_DIRNAME


def session_file(project_dir: Path) -> Path:
    """Return the dev session file path for ``project_dir``."""
    return session_dir(project_dir) / SESSION_FILENAME


def generate_key() -> str:
    """Generate a XXXX-XXXX-XXXX-XXXX security key (capital letters + digits, ~83 bits entropy)."""

    def segment() -> str:
        return "".join(secrets.choice(_KEY_CHARS) for _ in range(4))

    return f"{segment()}-{segment()}-{segment()}-{segment()}"


def write_session(key: str, port: int, project_dir: Path) -> None:
    directory = session_dir(project_dir)
    directory.mkdir(exist_ok=True)
    session_path = session_file(project_dir)
    # ``os.open`` with mode=0o600 ensures the file is created owner-only.
    # ``Path.write_text`` would honour the user's umask (typically 022),
    # leaving the dev key world-readable on shared dev hosts.
    payload = json.dumps(
        {
            "key": key,
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
            "port": port,
        },
        indent=2,
    )
    fd = os.open(
        session_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
    finally:
        # If the file already existed with looser perms, tighten them.
        with contextlib.suppress(OSError):
            os.chmod(session_path, 0o600)


def read_session(project_dir: Path) -> dict[str, Any] | None:
    session_path = session_file(project_dir)
    if not session_path.exists():
        return None
    try:
        data = json.loads(session_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    pid = data.get("pid")
    if pid and not _pid_alive(pid):
        session_path.unlink(missing_ok=True)
        return None

    return data


def clear_session(project_dir: Path) -> None:
    session_file(project_dir).unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
