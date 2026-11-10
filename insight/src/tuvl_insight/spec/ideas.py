"""Ideas: the immutable, numbered input a spec is generated from.

``tuvl import idea`` copies files (and snapshots of URLs) into ``idea/<IDEA-id>/source/``, writes the text of PDFs and
Word files to ``extracted/`` next to them, and records everything in ``IDEA.yaml`` and
``idea/INDEX.yaml``. An idea is never edited afterwards; a changed brief is a new idea that
``supersedes`` the old one.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tuvl_insight.spec import ids

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
ACCEPTED_SUFFIXES = TEXT_SUFFIXES | IMAGE_SUFFIXES | {".pdf", ".docx"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".tuvl"}
_SECRET_NAMES = re.compile(
    r"^(\.env.*|id_(rsa|dsa|ecdsa|ed25519).*|.*\.(pem|key|p12|pfx|jks|keystore)|\.netrc|credentials(\.json)?)$",
    re.I,
)
DEFAULT_MAX_FILE = 10 * 1024 * 1024
DEFAULT_MAX_TOTAL = 50 * 1024 * 1024
_DOCX_XML_CAP = 20 * 1024 * 1024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class IdeaError(ValueError):
    pass


@dataclass
class Planned:
    source: Path
    relative: str
    bytes: int
    origin: dict | None = None


@dataclass
class ImportPlan:
    files: list[Planned] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(f.bytes for f in self.files)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_import(
    paths: list[Path],
    *,
    max_file: int = DEFAULT_MAX_FILE,
    max_total: int = DEFAULT_MAX_TOTAL,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> ImportPlan:
    """Which files an import would copy, and what it skips and why. Reads nothing but metadata."""
    plan = ImportPlan()
    seen: set[str] = set()

    def consider(file: Path, relative: str) -> None:
        name = file.name
        if relative in seen:
            plan.skipped.append((relative, "duplicate name from another path"))
        elif file.is_symlink():
            plan.skipped.append((relative, "symbolic link"))
        elif _SECRET_NAMES.match(name):
            plan.skipped.append((relative, "looks like a secret or key file"))
        elif name.startswith("."):
            plan.skipped.append((relative, "hidden file"))
        elif file.suffix.lower() not in ACCEPTED_SUFFIXES:
            plan.skipped.append((relative, f"unsupported type '{file.suffix or 'none'}'"))
        elif include and not any(file.match(g) or Path(relative).match(g) for g in include):
            plan.skipped.append((relative, "not matched by --include"))
        elif exclude and any(file.match(g) or Path(relative).match(g) for g in exclude):
            plan.skipped.append((relative, "matched by --exclude"))
        else:
            size = file.stat().st_size
            if size > max_file:
                plan.skipped.append((relative, f"larger than {max_file // (1024 * 1024)} MB"))
            else:
                seen.add(relative)
                plan.files.append(Planned(file, relative, size))

    for root in paths:
        if not root.exists():
            raise IdeaError(f"{root} does not exist")
        if root.is_file():
            consider(root, root.name)
            continue
        for file in sorted(p for p in root.rglob("*") if p.is_file()):
            parts = file.relative_to(root).parts
            if any(part in SKIP_DIRS or part.startswith(".") for part in parts[:-1]):
                continue
            consider(file, "/".join(parts))
    if plan.total > max_total:
        raise IdeaError(
            f"{plan.total // (1024 * 1024)} MB to import is over the {max_total // (1024 * 1024)} MB "
            "limit; narrow it with --include / --exclude or raise --max-total"
        )
    if not plan.files:
        raise IdeaError(
            "nothing to import: "
            + ("; ".join(f"{n} ({why})" for n, why in plan.skipped) or "no files")
        )
    return plan


# ── text extraction ──────────────────────────────────────────────────────────


def extract_text(path: Path) -> tuple[str | None, str | None]:
    """``(text, warning)``: plain text of a file, or why none could be made. Images give neither."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return None, None
    if suffix in TEXT_SUFFIXES:
        return path.read_text(errors="replace"), None
    if suffix == ".pdf":
        return _pdf_text(path)
    if suffix == ".docx":
        return _docx_text(path)
    return None, None


def _pdf_text(path: Path) -> tuple[str | None, str | None]:
    from tuvl.extras import available

    if not available("pdf"):
        return None, "PDF text needs the 'pdf' extra: pip install 'tuvl[pdf]'"
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return None, "encrypted PDF: no text extracted"
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except (PyPdfError, OSError, ValueError) as exc:
        return None, f"PDF could not be read: {exc}"
    text = "\n\n".join(f"<!-- page {n} -->\n{t}" for n, t in enumerate(pages, 1) if t)
    if not text:
        return None, "no text layer (a scanned PDF?): nothing extracted, OCR is not supported"
    return text, None


def _docx_text(path: Path) -> tuple[str | None, str | None]:
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > _DOCX_XML_CAP:
                return None, "Word document too large to read safely"
            raw = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        return None, f"Word file could not be read: {exc}"
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        return None, "Word document refused: it declares XML entities"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return None, f"Word file is not valid XML: {exc}"
    lines: list[str] = []
    for para in root.iter(f"{_W}p"):
        text = "".join(
            (t.text or "") if t.tag == f"{_W}t" else ("\t" if t.tag == f"{_W}tab" else "")
            for t in para.iter()
            if t.tag in (f"{_W}t", f"{_W}tab")
        ).strip()
        if not text:
            continue
        style = para.find(f"{_W}pPr/{_W}pStyle")
        name = style.get(f"{_W}val", "") if style is not None else ""
        level = re.fullmatch(r"Heading(\d)", name)
        lines.append(f"{'#' * int(level.group(1))} {text}" if level else text)
    if not lines:
        return None, "no text found in the Word document"
    return "\n\n".join(lines), None


# ── import ───────────────────────────────────────────────────────────────────


def first_title(text: str | None, fallback: str) -> str:
    if text:
        for line in text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped and not stripped.startswith("<!--"):
                return stripped[:120]
    return fallback


@dataclass
class Imported:
    id: str
    folder: Path
    title: str
    files: list[dict]
    warnings: list[str]


def import_idea(
    project: Path,
    plan: ImportPlan,
    *,
    initials: str,
    title: str | None,
    supersedes: str | None,
    imported_by: str,
    now: _dt.datetime | None = None,
) -> Imported:
    if not ids.IDEA_ID.match(f"IDEA-{initials}-0001"):
        raise IdeaError(f"'{initials}' is not valid developer initials")
    index = ids.read_index(project)
    if supersedes is not None and not any(r.get("id") == supersedes for r in index):
        raise IdeaError(f"{supersedes} is not an idea in this project")
    new_id = ids.next_idea_id(project, initials)
    folder = ids.idea_dir(project) / new_id
    if folder.exists():
        raise IdeaError(f"{folder} already exists")
    stamp = (now or _dt.datetime.now(_dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    files: list[dict] = []
    warnings: list[str] = []
    texts: list[str | None] = []
    try:
        for item in plan.files:
            dest = folder / "source" / item.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item.source, dest)
            text, warning = extract_text(dest)
            texts.append(text)
            entry: dict = {
                "path": f"source/{item.relative}",
                "sha256": _sha256(dest),
                "bytes": item.bytes,
                "extracted": None,
            }
            if text is not None and dest.suffix.lower() in {".pdf", ".docx"}:
                out = folder / "extracted" / (item.relative + ".md")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text.rstrip() + "\n")
                entry["extracted"] = f"extracted/{item.relative}.md"
            if item.origin:
                entry["origin"] = item.origin
            if warning:
                entry["warning"] = warning
                warnings.append(f"{item.relative}: {warning}")
            files.append(entry)
        chosen = title or first_title(
            next((t for t in texts if t), None), Path(plan.files[0].relative).stem
        )
        record = {
            "kind": "Idea",
            "version": "tuvl/v2",
            "id": new_id,
            "title": chosen,
            "imported_at": stamp,
            "imported_by": imported_by,
            "supersedes": supersedes,
            "files": files,
            "specs": [],
        }
        (folder / "IDEA.yaml").write_text(
            yaml.safe_dump(record, sort_keys=False, allow_unicode=True)
        )
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    index.append(
        {
            "id": new_id,
            "title": chosen,
            "imported_at": stamp,
            "supersedes": supersedes,
            "specs": [],
        }
    )
    ids.write_index(project, index)
    return Imported(new_id, folder, chosen, files, warnings)


def read_idea(project: Path, idea: str) -> dict:
    path = ids.idea_dir(project) / idea / "IDEA.yaml"
    if not path.is_file():
        raise IdeaError(f"{idea} is not an idea in this project")
    return yaml.safe_load(path.read_text()) or {}
