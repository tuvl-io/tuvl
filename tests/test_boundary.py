"""The line between the engine and the developer tooling, held while the tooling is being split out.

`scripts/split_report.py` reads every import in `src/tuvl` and sorts the modules into the engine ("core")
and the tooling. Two rules, both about edges that cross the line:

- the engine imports the tooling only at the listed seams;
- the tooling does not start depending on engine modules it did not use before (P3 turns the snapshot into
  the short list in the plan: every entry with a reason).

Regenerate the snapshot with `UPDATE_SNAPSHOTS=1 pytest tests/test_boundary.py` and say why in the commit.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(__file__).parent / "snapshots" / "tooling_uses_core.json"


def _report() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "split_report", ROOT / "scripts" / "split_report.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_engine_imports_the_tooling_only_at_the_seams() -> None:
    report = _report()
    bad = report.seam_violations()
    assert not bad, (
        "these engine modules import developer tooling; move the shared code into the contract/core, "
        "or (if it is a real seam) list it in scripts/split_report.py SEAMS with a reason:\n"
        + "\n".join(f"  {module} -> {target}" for module, target in bad)
    )


def test_the_tooling_does_not_grow_new_dependencies_on_the_engine() -> None:
    current = _report().core_used_by_tooling()
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(json.dumps(current, indent=1) + "\n")
    allowed = set(json.loads(SNAPSHOT.read_text()))
    new = sorted(set(current) - allowed)
    assert not new, (
        "the tooling now imports engine modules it did not before: "
        f"{new}. Prefer a module already on the list, or add it to {SNAPSHOT.name} with a reviewer's eyes."
    )
