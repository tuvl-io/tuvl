"""`tuvl db generate-rls` / `tuvl db check-rls` — CLI wiring around the RLS module.

``_ensure_project_loaded`` is stubbed out since it drives real bootstrap /
registry globals (shared mutable state across the whole test process) that
are out of scope here; these tests pin the command's own argument handling
and exit-code contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tuvl.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_project_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl.cli.commands.db._ensure_project_loaded", lambda project_dir: None)


def test_generate_rls_reports_no_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl.core.datasources.rls.tenant_table_names", lambda: [])
    res = runner.invoke(app, ["generate", "rls", "-d", str(tmp_path)])
    assert res.exit_code == 1
    assert "No tenant-scoped tables found" in res.output


def test_generate_rls_prints_sql_to_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl.core.datasources.rls.tenant_table_names", lambda: ["candidates"])
    monkeypatch.setattr(
        "tuvl.core.datasources.rls.generate_rls_sql", lambda: "-- sql for candidates"
    )
    res = runner.invoke(app, ["generate", "rls", "-d", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "-- sql for candidates" in res.output


def test_generate_rls_writes_to_out_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl.core.datasources.rls.tenant_table_names", lambda: ["candidates"])
    monkeypatch.setattr(
        "tuvl.core.datasources.rls.generate_rls_sql", lambda: "-- sql for candidates"
    )
    out_file = tmp_path / "rls.sql"
    res = runner.invoke(app, ["generate", "rls", "-d", str(tmp_path), "--out", str(out_file)])
    assert res.exit_code == 0, res.output
    assert out_file.read_text() == "-- sql for candidates"
    assert "Wrote 1 table" in res.output


async def _no_bypass() -> None:
    return None


def test_check_rls_skips_when_single_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tuvl.core.bootstrap.LOCKED_DEPLOYMENT_MODE", "single_tenant")
    res = runner.invoke(app, ["verify", "rls", "-d", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "single_tenant" in res.output


def test_check_rls_passes_when_no_missing_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tuvl.core.bootstrap.LOCKED_DEPLOYMENT_MODE", "multi_tenant")

    async def _no_missing() -> list[str]:
        return []

    monkeypatch.setattr("tuvl.core.datasources.rls.find_tables_missing_rls_policy", _no_missing)
    monkeypatch.setattr("tuvl.core.datasources.rls.primary_role_bypass_reason", _no_bypass)
    res = runner.invoke(app, ["verify", "rls", "-d", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "All tenant-scoped tables" in res.output


def test_check_rls_fails_when_policies_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tuvl.core.bootstrap.LOCKED_DEPLOYMENT_MODE", "multi_tenant")

    async def _missing() -> list[str]:
        return ["candidates"]

    monkeypatch.setattr("tuvl.core.datasources.rls.find_tables_missing_rls_policy", _missing)
    monkeypatch.setattr("tuvl.core.datasources.rls.primary_role_bypass_reason", _no_bypass)
    res = runner.invoke(app, ["verify", "rls", "-d", str(tmp_path)])
    assert res.exit_code == 1
    assert "candidates" in res.output


def test_check_rls_fails_when_the_role_ignores_row_level_security(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tuvl.core.bootstrap.LOCKED_DEPLOYMENT_MODE", "multi_tenant")

    async def _no_missing() -> list[str]:
        return []

    async def _superuser() -> str:
        return "it is a superuser"

    monkeypatch.setattr("tuvl.core.datasources.rls.find_tables_missing_rls_policy", _no_missing)
    monkeypatch.setattr("tuvl.core.datasources.rls.primary_role_bypass_reason", _superuser)
    res = runner.invoke(app, ["verify", "rls", "-d", str(tmp_path)])
    assert res.exit_code == 1
    assert "superuser" in res.output and "All tenant-scoped tables" not in res.output
