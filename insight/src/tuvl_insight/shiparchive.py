"""``tuvl ship --format archive``: the runtime files as a project you can run with ``uv``.

The archive is reproducible: the same files give the same bytes (sorted entries, fixed times and
owners, no timestamp inside), so its sha256 identifies what was built.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

EPOCH = 315532800  # 1980-01-01: the earliest time a zip can hold
FORMATS = ("tar.gz", "zip")


@dataclass(frozen=True)
class Built:
    path: Path
    sha256: str
    files: int


def env_example(variables: list[str]) -> str:
    lines = [
        "# Copy to .env and fill it in. Never commit .env.",
        "TUVL_ENV=production",
        "TUVL_SIGNING_KEY=          # tuvl generate key",
        "POSTGRES_HOST=",
        "POSTGRES_PORT=5432",
        "POSTGRES_DB=",
        "POSTGRES_USER=",
        "POSTGRES_PASSWORD=",
    ]
    seen = {
        "TUVL_ENV",
        "TUVL_SIGNING_KEY",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    }
    lines += [f"{v}=" for v in variables if v not in seen]
    return "\n".join(lines) + "\n"


def run_script(*, tuvl_pin: str, locked: bool) -> str:
    sync = (
        "uv sync --frozen --no-dev --no-install-project"
        if locked
        else "uv sync --no-dev --no-install-project"
    )
    return f"""#!/bin/sh
# Runs this tuvl project. Needs uv: https://docs.astral.sh/uv/
set -eu
cd "$(dirname "$0")"
[ -f .env ] || {{ echo "Copy .env.example to .env and fill it in first." >&2; exit 1; }}
{sync}
uv pip install --python .venv/bin/python "{tuvl_pin}"
set -a; . ./.env; set +a
exec .venv/bin/tuvl serve app --project-dir . --host "${{HOST:-0.0.0.0}}" --port "${{PORT:-8885}}"
"""


def run_md(*, slug: str, version: str, tuvl_pin: str, runtime_hash: str, commit: str) -> str:
    return f"""# {slug} {version}

A tuvl project, packaged by `tuvl ship --format archive`. It contains only what runs; no specs, tests
or agent rules.

## Run

1. Install [uv](https://docs.astral.sh/uv/) (and Python 3.13 or 3.14, which uv can fetch).
2. `cp .env.example .env` and fill it in: a PostgreSQL database, a signing key
   (`uv run --with {tuvl_pin.split("[")[0].split("==")[0]} tuvl generate key`), and your models' variables.
3. `./run.sh` (it installs the locked dependencies and `{tuvl_pin}`, then serves on port 8885;
   set `HOST` / `PORT` to change that).

On Windows run the three commands of `run.sh` in PowerShell, or use WSL.

## What this is

- tuvl `{tuvl_pin.split("==")[-1]}`; runtime files hash `{runtime_hash}`; git commit `{commit or "unknown"}`
  (`BUILD-INFO.json`).
- Check the download with the `.sha256` file next to the archive.
- To keep it running, use your process manager (systemd, supervisor, …); `run.sh` is the command.
"""


def build(
    out_dir: Path,
    *,
    name: str,
    root: Path,
    files: list[str],
    generated: dict[str, str],
    fmt: str,
) -> Built:
    """Write ``<name>.<fmt>`` holding ``files`` (from *root*) and the ``generated`` texts, under ``<name>/``."""
    if fmt not in FORMATS:
        raise ValueError(f"archive format must be one of {', '.join(FORMATS)}")
    entries: dict[str, tuple[bytes, int]] = {}
    for rel in files:
        path = root / rel
        if path.is_file():
            entries[rel] = (path.read_bytes(), 0o755 if path.stat().st_mode & 0o111 else 0o644)
    for rel, text in generated.items():
        entries[rel] = (text.encode(), 0o755 if rel.endswith(".sh") else 0o644)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.{fmt}"
    buf = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in sorted(entries):
                data, mode = entries[rel]
                info = zipfile.ZipInfo(f"{name}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100000 | mode) << 16
                z.writestr(info, data)
    else:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for rel in sorted(entries):
                data, mode = entries[rel]
                member = tarfile.TarInfo(f"{name}/{rel}")
                member.size, member.mode, member.mtime = len(data), mode, EPOCH
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                tar.addfile(member, io.BytesIO(data))
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as gz:
            gz.write(raw.getvalue())
    target.write_bytes(buf.getvalue())
    digest = hashlib.sha256(buf.getvalue()).hexdigest()
    (out_dir / f"{name}.{fmt}.sha256").write_text(f"{digest}  {target.name}\n")
    return Built(target, digest, len(entries))


def build_info(**fields: object) -> str:
    return json.dumps(fields, indent=2, sort_keys=True) + "\n"
