"""Project identity — ``.tuvl/project.yaml`` (kind: Project).

Who built this project and what it is: a stable id minted by ``tuvl init``,
the author, a contact email, the organization, the project type and the app's own version.
Read fail-open (absent or unparseable → ``None``) so the engine boots where
``tuvl init`` predates this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

PROJECT_YAML_RELPATH = ".tuvl/project.yaml"

PROJECT_TYPES: tuple[str, ...] = ("poc", "dev", "production")

# Fields `tuvl set project` / Insight may write. ``id`` and ``registry`` are not
# among them: the id is immutable and the registry block belongs to `register`.
EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "author",
    "email",
    "organization",
    "organization_url",
    "type",
    "version",
    "description",
    "license",
    "repo",
)

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DESCRIPTION_MAX = 2000


class ProjectInfoError(ValueError):
    """A project field failed validation."""


_ID_PREFIX = "prj_"
_B32 = "0123456789abcdefghjkmnpqrstvwxyz"
_ID_RE = re.compile(
    r"^prj_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-([0-9a-z]{2})$"
)


def _uuid7() -> str:
    """A UUIDv7 (time-ordered): ``uuid.uuid7`` where available, else hand-built."""
    native = getattr(uuid, "uuid7", None)
    if native is not None:
        return str(native())
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (ms << 80) | (0x7 << 76) | (secrets.randbits(12) << 64)
    value |= (0b10 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))


def _check_chars(raw: str) -> str:
    """Two base-32 characters derived from the uuid. Catches typos, not forgery."""
    bits = int.from_bytes(hashlib.sha256(raw.encode()).digest()[:2], "big") & 0x3FF
    return _B32[bits >> 5] + _B32[bits & 0x1F]


def new_project_id() -> str:
    raw = _uuid7()
    return f"{_ID_PREFIX}{raw}-{_check_chars(raw)}"


def id_uuid(project_id: str) -> str | None:
    """The bare UUID inside a well-formed id, else ``None``."""
    match = _ID_RE.match(str(project_id))
    if match is None or _check_chars(match.group(1)) != match.group(2):
        return None
    return match.group(1)


def id_is_valid(project_id: str) -> bool:
    return id_uuid(project_id) is not None


def project_yaml_path(project_dir: Path | str) -> Path:
    return Path(project_dir) / PROJECT_YAML_RELPATH


def load_project(project_dir: Path | str) -> dict[str, Any] | None:
    """The flattened project record, or ``None`` when absent or unreadable."""
    path = project_yaml_path(project_dir)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or data.get("kind") != "Project":
        return None
    spec, meta = data.get("spec"), data.get("metadata")
    spec = spec if isinstance(spec, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    out: dict[str, Any] = {"name": meta.get("name", ""), **spec}
    return out


def validate_field(field: str, value: Any) -> Any:
    """Normalise *value* for *field* or raise :class:`ProjectInfoError`."""
    if field not in EDITABLE_FIELDS:
        raise ProjectInfoError(f"'{field}' is not editable (one of: {', '.join(EDITABLE_FIELDS)})")
    text = "" if value is None else str(value).strip()
    if field == "name" and not text:
        raise ProjectInfoError("name must not be empty")
    if field == "type" and text not in PROJECT_TYPES:
        raise ProjectInfoError(f"type must be one of: {', '.join(PROJECT_TYPES)}")
    if field == "version" and not _SEMVER.match(text):
        raise ProjectInfoError("version must be semver, e.g. 0.1.0")
    if field == "email" and text and not _EMAIL.match(text):
        raise ProjectInfoError("email is not a valid address")
    if field in ("repo", "organization_url") and text and not re.match(r"^https?://\S+$", text):
        raise ProjectInfoError(f"{field} must be an http(s) URL")
    if field == "description" and len(text) > _DESCRIPTION_MAX:
        raise ProjectInfoError(f"description is limited to {_DESCRIPTION_MAX} characters")
    return text


def _git_config(key: str, cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "config", "--get", key],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def default_identity(project_dir: Path | str) -> dict[str, str]:
    """Author and email suggestions from ``git config``, empty when unknown."""
    cwd = Path(project_dir)
    return {"author": _git_config("user.name", cwd), "email": _git_config("user.email", cwd)}


def write_project(project_dir: Path | str, record: dict[str, Any]) -> Path:
    """Persist *record* (flattened, as :func:`load_project` returns it)."""
    spec = {k: v for k, v in record.items() if k != "name"}
    doc = {
        "kind": "Project",
        "version": "v1",
        "metadata": {"name": record.get("name", "")},
        "spec": spec,
    }
    path = project_yaml_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.dump(doc, allow_unicode=True, sort_keys=False))
    os.replace(tmp, path)
    return path


def create_project(
    project_dir: Path | str,
    *,
    name: str,
    author: str = "",
    email: str = "",
    organization: str = "",
    project_type: str = "dev",
) -> dict[str, Any]:
    """Mint a project record with a new id. Never overwrites an existing one."""
    existing = load_project(project_dir)
    if existing is not None and existing.get("id"):
        return existing
    record: dict[str, Any] = {
        "name": name,
        "id": new_project_id(),
        "author": author,
        "email": email,
        "organization": organization,
        "organization_url": "",
        "type": project_type,
        "version": "0.1.0",
        "description": "",
        "license": "",
        "repo": "",
    }
    write_project(project_dir, record)
    return record


def update_project(project_dir: Path | str, changes: dict[str, Any]) -> dict[str, Any]:
    """Apply validated *changes*; the id and registry block are preserved."""
    record = load_project(project_dir)
    if record is None or not record.get("id"):
        raise ProjectInfoError("no project.yaml — run `tuvl upgrade project` first")
    if "id" in changes and str(changes["id"]) != str(record["id"]):
        raise ProjectInfoError("the project id is immutable")
    for field, value in changes.items():
        if field == "id":
            continue
        record[field] = validate_field(field, value)
    write_project(project_dir, record)
    return record


def regenerate_id(project_dir: Path | str) -> dict[str, Any]:
    """Replace the id (for a copied or hand-edited project) and drop its registration."""
    record = load_project(project_dir)
    if record is None:
        raise ProjectInfoError("no project.yaml — run `tuvl upgrade project` first")
    record["id"] = new_project_id()
    record.pop("registry", None)
    write_project(project_dir, record)
    return record


def registration_payload(record: dict[str, Any], *, tuvl_version: str = "") -> dict[str, Any]:
    """What the Nest receives. Its key is the bare UUID; the check suffix stays local.

    The author and email are sent so the catalogue owner can reach the developer; the Nest keeps
    them private and lists the name, version, organization (with its link), type, licence, description and repository link.
    """
    payload: dict[str, Any] = {
        "id": id_uuid(record.get("id", "")) or "",
        "name": record.get("name", ""),
        "author": record.get("author", ""),
        "organization": record.get("organization", ""),
        "organization_url": record.get("organization_url", ""),
        "type": record.get("type", "dev"),
        "version": record.get("version", ""),
        "description": record.get("description", ""),
        "license": record.get("license", ""),
        "repo": record.get("repo", ""),
        "tuvl_version": tuvl_version,
    }
    if record.get("email"):
        payload["contact_email"] = record["email"]
    return payload


def payload_digest(payload: dict[str, Any]) -> str:
    """Stable hash of the listed fields (engine version excluded) for drift detection."""
    stable = {k: v for k, v in payload.items() if k != "tuvl_version"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def registration_state(record: dict[str, Any]) -> str:
    """``invalid_id`` | ``unregistered`` | ``id_changed`` | ``outdated`` | ``current``."""
    if not id_is_valid(record.get("id", "")):
        return "invalid_id"
    reg = record.get("registry")
    if not isinstance(reg, dict) or not reg.get("registered_at"):
        return "unregistered"
    if reg.get("id") and reg["id"] != record["id"]:
        return "id_changed"
    current = payload_digest(registration_payload(record))
    return "current" if reg.get("digest") == current else "outdated"


#: How far the Nest checked a listing: self-declared, the developer's tool reported a push to the
#: remote (not checked), or the Nest itself saw the challenge tag on the public remote.
REGISTRY_LABELS: tuple[str, ...] = ("listed", "push_confirmed", "verified")


def record_registration(
    project_dir: Path | str,
    url: str,
    *,
    when: str,
    label: str = "listed",
    verified_at: str = "",
) -> dict[str, Any]:
    """Store the outcome of a successful registration, with the id, a digest of the listing and its label."""
    record = load_project(project_dir) or {}
    digest = payload_digest(registration_payload(record))
    registry: dict[str, Any] = {
        "url": url,
        "id": record.get("id", ""),
        "registered_at": when,
        "digest": digest,
        "label": label if label in REGISTRY_LABELS else "listed",
    }
    if verified_at:
        registry["verified_at"] = verified_at
    return set_registry(project_dir, registry)


def record_verification(
    project_dir: Path | str, label: str, *, verified_at: str = ""
) -> dict[str, Any]:
    """Update the label of the stored registration after the Nest answered a verification."""
    record = load_project(project_dir) or {}
    registry = dict(record.get("registry") or {})
    registry["label"] = label if label in REGISTRY_LABELS else "listed"
    if verified_at:
        registry["verified_at"] = verified_at
    else:
        registry.pop("verified_at", None)
    return set_registry(project_dir, registry)


def set_registry(project_dir: Path | str, registry: dict[str, Any]) -> dict[str, Any]:
    """Record the outcome of a Nest registration."""
    record = load_project(project_dir)
    if record is None or not record.get("id"):
        raise ProjectInfoError("no project.yaml — run `tuvl upgrade project` first")
    record["registry"] = registry
    write_project(project_dir, record)
    return record
