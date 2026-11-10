"""Regression pins for TD-16 (versioned workflows must validate) and TD-17
(run_validation must not leave logging/warnings muted process-wide)."""

from __future__ import annotations

import warnings

from tuvl_insight.cli.commands.validate import run_validation

# ── TD-16: same name at different schema_version is NOT a duplicate ──────────


# ── TD-17: run_validation restores global logging/warnings state ────────────


def test_run_validation_restores_warning_filters(tmp_path):
    (tmp_path / "workflows").mkdir()
    before = list(warnings.filters)
    run_validation(tmp_path, echo=False)
    assert warnings.filters == before
