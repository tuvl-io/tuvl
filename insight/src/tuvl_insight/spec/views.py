"""Read-only views of ideas, spec lifecycle and the audit ledger, for Insight (and anything else that reads them).

Everything here reads files; nothing writes. The repository stays the source of truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tuvl_insight.audit import ledger
from tuvl_insight.spec import ids, lifecycle

PREVIEW_CHARS = 6000


def lifecycle_view(project: Path) -> list[dict[str, Any]]:
    """Every spec's id, status (as it stands: ``approved`` only while unchanged), approver and ideas."""
    out: list[dict[str, Any]] = []
    specs = project / ids.SPECS_DIR
    for path in sorted(specs.glob("*.md")) if specs.is_dir() else []:
        text = path.read_text()
        state = lifecycle.state_of(text)
        front, _ = lifecycle.split_front(text)
        out.append(
            {
                "path": f"{ids.SPECS_DIR}/{path.name}",
                "name": path.name,
                "id": state.id,
                "title": str(front.get("title") or front.get("name") or path.stem),
                "managed": "status" in front,
                "status": state.status,
                "stale": state.stale,
                "effective": state.effective,
                "approved_by": state.approved_by,
                "approved_at": state.approved_at,
                "ideas": state.ideas,
                "generated_by": str(front.get("generated_by") or ""),
                "review_note": str(front.get("review_note") or ""),
            }
        )
    return out


def ideas_view(project: Path) -> list[dict[str, Any]]:
    """The index joined with each idea's record, the specs that name it and what superseded it."""
    rows = ids.read_index(project)
    superseded_by = {
        str(r.get("supersedes")): str(r.get("id")) for r in rows if r.get("supersedes")
    }
    specs_by_idea: dict[str, list[str]] = {}
    for spec in lifecycle_view(project):
        for idea in spec["ideas"]:
            specs_by_idea.setdefault(idea, []).append(spec["id"] or spec["path"])
    out = []
    for row in rows:
        idea = str(row.get("id", ""))
        record = _record(project, idea)
        out.append(
            {
                "id": idea,
                "title": str(row.get("title") or record.get("title") or idea),
                "imported_at": str(row.get("imported_at") or record.get("imported_at") or ""),
                "imported_by": str(record.get("imported_by") or ""),
                "supersedes": row.get("supersedes"),
                "superseded_by": superseded_by.get(idea),
                "files": len(record.get("files") or []),
                "specs": sorted(specs_by_idea.get(idea, [])),
            }
        )
    return out


def _record(project: Path, idea: str) -> dict[str, Any]:
    if not ids.IDEA_ID.match(idea):
        return {}
    path = ids.idea_dir(project) / idea / "IDEA.yaml"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def idea_view(project: Path, idea: str) -> dict[str, Any] | None:
    """One idea: its record, a preview of each file's text, and the specs drawn from it."""
    record = _record(project, idea)
    if not record:
        return None
    folder = ids.idea_dir(project) / idea
    files = []
    for entry in record.get("files") or []:
        shown = str(entry.get("extracted") or entry.get("path") or "")
        path = (folder / shown).resolve()
        text = ""
        if (
            path.is_file()
            and path.is_relative_to(folder.resolve())
            and path.suffix.lower()
            in {".md", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml", ".markdown"}
        ):
            text = path.read_text(errors="replace")
        files.append(
            {
                "path": entry.get("path"),
                "sha256": entry.get("sha256"),
                "bytes": entry.get("bytes"),
                "extracted": entry.get("extracted"),
                "warning": entry.get("warning"),
                "origin": entry.get("origin"),
                "preview": text[:PREVIEW_CHARS],
                "truncated": len(text) > PREVIEW_CHARS,
            }
        )
    overview = next((i for i in ideas_view(project) if i["id"] == idea), {})
    return {
        **overview,
        "files_detail": files,
        "specs_detail": [s for s in lifecycle_view(project) if idea in s["ideas"]],
    }


def audit_view(
    project: Path,
    *,
    entity: str | None = None,
    kind: str | None = None,
    actor: str | None = None,
    limit: int = 200,
    after: str | None = None,
) -> dict[str, Any]:
    """Events (newest last), and whether the chain holds. ``after`` returns only events past that id."""
    events = ledger.select(ledger.read_events(project), entity=entity, actor_match=actor, kind=kind)
    if after:
        events = [e for e in events if str(e.get("id", "")) > after]
    verified = ledger.verify(project)
    return {
        "events": events[-limit:] if limit else events,
        "chain": {
            "ok": verified.ok,
            "events": verified.events,
            "problem": verified.problem,
            "where": verified.where,
        },
        "kinds": sorted(ledger.KINDS),
    }
