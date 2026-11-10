"""A spec's lifecycle: draft → approved (→ superseded), recorded in its front matter.

Approval pins the spec body with a hash. Any later edit of the body makes the approval *stale*:
the spec is a draft again until someone approves it. The front matter is not part of the hash, so
recording the approval, or an owner change, never invalidates it. The people who may approve are
whoever the project's review process allows; this module only records who did.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tuvl_insight.spec import ids
from tuvl_insight.spec.parse import _FRONT, content_hash

STATUSES = ("draft", "approved", "superseded")
_APPROVAL_KEYS = ("approved_by", "approved_at", "approved_hash")
_REVIEW_KEYS = ("review_note", "reviewed_by", "reviewed_at")


class LifecycleError(ValueError):
    pass


def split_front(text: str) -> tuple[dict[str, Any], str]:
    """``(front matter, body)``; an empty mapping when there is none."""
    match = _FRONT.match(text)
    if not match:
        return {}, text
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise LifecycleError(f"front matter is not YAML: {exc}") from exc
    if not isinstance(front, dict):
        raise LifecycleError("front matter must be a mapping")
    return front, text[match.end() :]


def with_front(text: str, updates: dict[str, Any], remove: tuple[str, ...] = ()) -> str:
    front, body = split_front(text)
    for key in remove:
        front.pop(key, None)
    front.update(updates)
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{dumped}---\n{body}"


def body_hash(text: str) -> str:
    return content_hash(split_front(text)[1])


@dataclass
class State:
    status: str = "draft"
    id: str = ""
    ideas: list[str] = field(default_factory=list)
    approved_by: str = ""
    approved_at: str = ""
    approved_hash: str = ""
    current_hash: str = ""

    @property
    def stale(self) -> bool:
        return self.status == "approved" and self.approved_hash != self.current_hash

    @property
    def effective(self) -> str:
        """``approved`` only while the body still matches what was approved."""
        return "draft" if self.stale else self.status


def state_of(text: str) -> State:
    front, _ = split_front(text)
    status = str(front.get("status") or "draft")
    ideas = front.get("ideas")
    return State(
        status=status if status in STATUSES else "draft",
        id=str(front.get("id") or ""),
        ideas=[str(i) for i in ideas] if isinstance(ideas, list) else [],
        approved_by=str(front.get("approved_by") or ""),
        approved_at=str(front.get("approved_at") or ""),
        approved_hash=str(front.get("approved_hash") or ""),
        current_hash=body_hash(text),
    )


def now_stamp(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now(_dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def approve(text: str, *, by: str, now: _dt.datetime | None = None) -> str:
    state = state_of(text)
    if state.status == "approved" and not state.stale:
        raise LifecycleError("already approved and unchanged since")
    if state.status == "superseded":
        raise LifecycleError("this spec is superseded")
    return with_front(
        text,
        {
            "status": "approved",
            "approved_by": by,
            "approved_at": now_stamp(now),
            "approved_hash": state.current_hash,
        },
        remove=_REVIEW_KEYS,
    )


def reject(text: str, *, by: str, reason: str, now: _dt.datetime | None = None) -> str:
    if not reason.strip():
        raise LifecycleError("a rejection needs a reason")
    return with_front(
        text,
        {
            "status": "draft",
            "review_note": reason.strip(),
            "reviewed_by": by,
            "reviewed_at": now_stamp(now),
        },
        remove=_APPROVAL_KEYS,
    )


def reopen(text: str) -> str:
    state = state_of(text)
    if state.status != "approved":
        raise LifecycleError("only an approved spec can be reopened")
    return with_front(text, {"status": "draft"}, remove=_APPROVAL_KEYS)


# ── files ────────────────────────────────────────────────────────────────────


def resolve_spec(project: Path, ref: str) -> Path:
    """A spec given as a path, a spec id (``SPEC-SK-0004-01``), a file stem or its ``name``."""
    candidate = Path(ref)
    for path in (candidate, project / candidate):
        if path.is_file():
            return path.resolve()
    specs = project / ids.SPECS_DIR
    matches: list[Path] = []
    for path in sorted(specs.glob("*.md")) if specs.is_dir() else []:
        front, _ = split_front(path.read_text())
        if ref in (path.stem, str(front.get("id") or ""), str(front.get("name") or "")):
            matches.append(path)
    if not matches:
        raise LifecycleError(f"no spec '{ref}' in {specs}")
    if len(matches) > 1:
        raise LifecycleError(
            f"'{ref}' names more than one spec: {', '.join(p.name for p in matches)}"
        )
    return matches[0]


_SPEC_HASH = re.compile(r"^(\s*spec_hash:\s*)\S+", re.M)


def refresh_plan_hash(project: Path, spec_file: Path) -> bool:
    """Re-record the whole-spec hash in the spec's task list after a front-matter-only edit."""
    from tuvl_insight.spec.parse import parse_text
    from tuvl_insight.spec.tasks import plan_for

    text = spec_file.read_text()
    shown = (
        str(spec_file.relative_to(project)) if spec_file.is_relative_to(project) else str(spec_file)
    )
    spec = parse_text(text, path=shown, default_name=spec_file.stem)
    plan = plan_for(project, spec)
    if plan is None:
        return False
    old = plan.read_text()
    new = _SPEC_HASH.sub(lambda m: f"{m.group(1)}{spec.hash}", old, count=1)
    if new != old:
        plan.write_text(new)
    return True


def sync_idea_links(project: Path) -> None:
    """Make ``IDEA.yaml`` and the index list the specs whose front matter names the idea."""
    specs_by_idea: dict[str, list[str]] = {}
    specs = project / ids.SPECS_DIR
    for path in sorted(specs.glob("*.md")) if specs.is_dir() else []:
        front, _ = split_front(path.read_text())
        spec_id = str(front.get("id") or "")
        if not ids.SPEC_ID.match(spec_id):
            continue
        for idea in front.get("ideas") or []:
            specs_by_idea.setdefault(str(idea), []).append(spec_id)
    rows = ids.read_index(project)
    for row in rows:
        row["specs"] = sorted(specs_by_idea.get(str(row.get("id")), []))
    if rows:
        ids.write_index(project, rows)
    for row in rows:
        record = ids.idea_dir(project) / str(row.get("id")) / "IDEA.yaml"
        if record.is_file():
            data = yaml.safe_load(record.read_text()) or {}
            data["specs"] = row["specs"]
            record.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


# ── checks ───────────────────────────────────────────────────────────────────


def lifecycle_findings(project: Path, spec_file: Path) -> list[tuple[str, str, str]]:
    """``(code, severity, message)`` for the spec's lifecycle, id and idea links (S017–S021)."""
    import hashlib

    text = spec_file.read_text()
    front, _ = split_front(text)
    out: list[tuple[str, str, str]] = []
    state = state_of(text)
    if state.stale:
        out.append(
            ("S017", "error", "approved, but the body changed since: approve it again or reopen it")
        )
    spec_id = str(front.get("id") or "")
    if spec_id and not ids.SPEC_ID.match(spec_id):
        out.append(("S019", "warning", f"id '{spec_id}' is not SPEC-<INITIALS>-<idea>-<n>"))
    elif ids.SPEC_ID.match(spec_file.stem) and spec_id != spec_file.stem:
        out.append(("S019", "warning", f"file name {spec_file.stem} but id is '{spec_id}'"))
    idea_list = [str(i) for i in front.get("ideas") or []]
    known = ids.idea_ids(project)
    for idea in idea_list:
        if idea not in known:
            out.append(("S018", "error", f"{idea} is not an idea in this project"))
    if ids.SPEC_ID.match(spec_id) and ids.idea_of(spec_id) not in idea_list:
        out.append(
            (
                "S019",
                "warning",
                f"{spec_id} derives from {ids.idea_of(spec_id)}, which `ideas:` lacks",
            )
        )
    superseded_by = {
        str(r.get("supersedes")): str(r.get("id"))
        for r in ids.read_index(project)
        if r.get("supersedes")
    }
    for idea in idea_list:
        if idea in superseded_by and idea in known:
            out.append(("S020", "warning", f"{idea} was superseded by {superseded_by[idea]}"))
        record = ids.idea_dir(project) / idea / "IDEA.yaml"
        if record.is_file():
            data = yaml.safe_load(record.read_text()) or {}
            for f in data.get("files") or []:
                path = ids.idea_dir(project) / idea / str(f.get("path", ""))
                if not path.is_file():
                    out.append(("S021", "warning", f"{idea}: {f.get('path')} is missing"))
                elif hashlib.sha256(path.read_bytes()).hexdigest() != f.get("sha256"):
                    out.append(
                        (
                            "S021",
                            "warning",
                            f"{idea}: {f.get('path')} changed since it was imported",
                        )
                    )
    return out
