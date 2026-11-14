"""Package a project's web UI (a folder of static files) for ``tuvl deploy``.

The cloud serves the files as they are, to anyone, so the archive is made carefully: only regular files, nothing hidden (so no ``.env`` or ``.git``), none of
the development helpers a UI folder usually holds (``serve.py``, ``tests``, ``specs``, ``node_modules``), the same bytes for the same files, and ``index.html``
at the top. The cloud checks the archive again; these checks only fail early, on your machine.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 5000
SKIP_NAMES = frozenset({"serve.py", "tests", "specs", "__pycache__", "node_modules", "Thumbs.db"})


class UiError(Exception):
    pass


def find_ui_dir(project_dir: Path, explicit: Path | None) -> Path | None:
    """The UI folder to deploy: the one named, or ``ui/`` in the project when it has an ``index.html``."""
    if explicit is not None:
        if not explicit.is_dir():
            raise UiError(f"{explicit} is not a folder")
        return explicit
    guess = project_dir / "ui"
    return guess if (guess / "index.html").is_file() else None


def collect(root: Path) -> tuple[list[Path], list[str]]:
    """(files to send relative to *root*, names left out)."""
    files: list[Path] = []
    skipped: set[str] = set()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            skipped.add(rel.parts[0] if rel.parts[0].startswith(".") else "hidden files")
            continue
        if any(part in SKIP_NAMES for part in rel.parts):
            skipped.add(next(part for part in rel.parts if part in SKIP_NAMES))
            continue
        if path.is_symlink():
            skipped.add("links")
            continue
        if path.is_file():
            files.append(rel)
    return files, sorted(skipped)


def package(root: Path) -> tuple[bytes, int, list[str]]:
    """``(gzipped tar, number of files, names left out)`` for a UI folder. The same files give the same bytes."""
    files, skipped = collect(root)
    if Path("index.html") not in files:
        tops = {f.parts[0] for f in files if len(f.parts) > 1}
        hint = (
            f" It has a folder {next(iter(tops))!r}: point --ui at that folder."
            if len(tops) == 1 and not any(len(f.parts) == 1 for f in files)
            else ""
        )
        raise UiError(f"{root} has no index.html at its top.{hint}")
    if len(files) > MAX_FILES:
        raise UiError(f"{len(files)} files: a web UI may have at most {MAX_FILES}")
    raw = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for rel in files:
            info = tarfile.TarInfo(rel.as_posix())
            data = (root / rel).read_bytes()
            info.size, info.mtime, info.mode, info.uid, info.gid, info.uname, info.gname = (
                len(data),
                0,
                0o644,
                0,
                0,
                "",
                "",
            )
            tar.addfile(info, io.BytesIO(data))
    blob = raw.getvalue()
    if len(blob) > MAX_BYTES:
        raise UiError(
            f"the web UI is {len(blob) / 1048576:.0f} MB packed; the limit is {MAX_BYTES // 1048576} MB"
        )
    return blob, len(files), skipped
