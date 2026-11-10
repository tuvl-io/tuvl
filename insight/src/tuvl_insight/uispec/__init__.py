"""``tuvl generate ui-spec``: guidelines for building a UI on the implemented backend, with no model involved."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tuvl_insight.uispec.facts import collect
from tuvl_insight.uispec.render import DEFAULT_BASE, NOTES, render

NOTES_MARK = NOTES.split(" — ")[0]

__all__ = ["DEFAULT_BASE", "Outcome", "check", "collect", "generate", "render"]

SPECS_DIR = Path("ui") / "specs"


@dataclass
class Outcome:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.stale or self.missing or self.extra)


def _with_notes(text: str, existing: Path) -> str:
    """Keep what a person wrote below the notes marker of the file already on disk."""
    if not existing.is_file():
        return text
    old, new = existing.read_text(), text
    if NOTES_MARK not in old or NOTES_MARK not in new:
        return text
    mine = old.split(NOTES_MARK, 1)[1].split("\n", 1)[1:]
    return new.split(NOTES_MARK, 1)[0] + NOTES + "\n" + (mine[0] if mine else "")


def _expected(project: Path, facts: dict, base_url: str, include_partial: bool) -> dict[str, str]:
    out = Path(project) / SPECS_DIR
    files = render(facts, base_url=base_url, include_partial=include_partial)
    return {n: (_with_notes(t, out / n) if n.endswith(".md") else t) for n, t in files.items()}


def generate(
    project: Path,
    *,
    only: list[str] | None = None,
    include_partial: bool = False,
    run_tests: bool = True,
    base_url: str = DEFAULT_BASE,
    facts: dict | None = None,
) -> Outcome:
    """Write ``ui/specs/``: the index, the ready workflows (all with *include_partial*), the models."""
    project = project.resolve()
    facts = facts or collect(project, run_tests=run_tests)
    if only:
        facts = {**facts, "workflows": [w for w in facts["workflows"] if w["name"] in only]}
    out = project / SPECS_DIR
    out.mkdir(parents=True, exist_ok=True)
    result = Outcome()
    for name, text in _expected(project, facts, base_url, include_partial).items():
        path = out / name
        if path.is_file() and path.read_text() == text:
            result.unchanged.append(name)
            continue
        path.write_text(text)
        result.written.append(name)
    return result


def check(project: Path, *, run_tests: bool = True, base_url: str = DEFAULT_BASE) -> Outcome:
    """Is ``ui/specs/`` what ``generate`` would write? Stale, missing and extra files are reported."""
    project = project.resolve()
    facts = collect(project, run_tests=run_tests)
    out = project / SPECS_DIR
    result = Outcome()
    expected = _expected(project, facts, base_url, False)
    for name, text in expected.items():
        path = out / name
        if not path.is_file():
            result.missing.append(name)
        elif path.read_text() != text:
            result.stale.append(name)
        else:
            result.unchanged.append(name)
    if out.is_dir():
        known = {w["name"] for w in facts["workflows"]}
        for path in sorted(out.glob("*.md")):
            if path.name not in expected and path.stem in known | {"models"}:
                result.extra.append(path.name)
    return result
