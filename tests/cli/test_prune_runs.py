"""`tuvl prune runs`: remove finished runs older than an age, with a dry run first."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl.cli.commands import db as db_cli
from tuvl.core.datasources import postgres
from tuvl.runtime.models import RUNTIME_TABLES, SystemRun

runner = CliRunner()


class FreshEngines(dict):
    """The command runs in its own event loop, so it gets its own engine on every access."""

    def __init__(self, url: str) -> None:
        super().__init__(main=None)
        self._url = url

    def __getitem__(self, key: str):  # type: ignore[no-untyped-def]
        return create_async_engine(self._url)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}"

    async def create() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(
                SQLModel.metadata.create_all, tables=[m.__table__ for m in RUNTIME_TABLES]
            )
        async with AsyncSession(engine, expire_on_commit=False) as s:
            now = dt.datetime.now(dt.UTC)
            for status, days in (
                ("completed", 60),
                ("failed", 60),
                ("completed", 2),
                ("running", 90),
            ):
                when = now - dt.timedelta(days=days)
                s.add(
                    SystemRun(
                        workflow="w",
                        workflow_hash="h",
                        status=status,
                        trigger="http",
                        created_at=when,
                        updated_at=when,
                        finished_at=when if status != "running" else None,
                    )
                )
            await s.commit()
        await engine.dispose()

    asyncio.run(create())
    monkeypatch.setattr(postgres, "_PRIMARY_DS", "main")
    monkeypatch.setattr(postgres, "_engines_rw", FreshEngines(url))

    async def no_close() -> None:
        return None

    monkeypatch.setattr(postgres, "close_db", no_close)
    monkeypatch.setattr(db_cli, "_ensure_project_loaded", lambda _dir: None)
    return url


def count(url: str) -> dict[str, int]:
    async def go() -> dict[str, int]:
        engine = create_async_engine(url)
        out: dict[str, int] = {}
        async with AsyncSession(engine) as s:
            from sqlalchemy import func, select

            for status in ("completed", "failed", "running"):
                out[status] = (
                    await s.execute(
                        select(func.count())
                        .select_from(SystemRun)
                        .where(SystemRun.status == status)  # type: ignore[arg-type]
                    )
                ).scalar_one()
        await engine.dispose()
        return out

    return asyncio.run(go())


def test_a_dry_run_says_what_it_would_remove_and_removes_nothing(db: str) -> None:
    result = runner.invoke(app, ["prune", "runs", "--older-than", "30d", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would remove 2 runs" in result.output
    assert count(db) == {"completed": 2, "failed": 1, "running": 1}


def test_it_removes_old_finished_runs_and_nothing_else(db: str) -> None:
    result = runner.invoke(app, ["prune", "runs", "--older-than", "30d"])
    assert result.exit_code == 0, result.output
    assert "Removed 2 runs" in result.output
    assert count(db) == {"completed": 1, "failed": 0, "running": 1}


def test_a_status_filter_narrows_it(db: str) -> None:
    result = runner.invoke(app, ["prune", "runs", "--older-than", "30d", "--status", "failed"])
    assert result.exit_code == 0, result.output
    assert count(db) == {"completed": 2, "failed": 0, "running": 1}


@pytest.mark.parametrize(
    "args",
    [
        ["--older-than", "30d", "--status", "running"],
        ["--older-than", "soon"],
        ["--status", "failed"],
    ],
    ids=["a status that can still change", "an age that is not one", "no age"],
)
def test_bad_arguments_are_refused_before_anything_is_touched(db: str, args: list[str]) -> None:
    result = runner.invoke(app, ["prune", "runs", *args])
    assert result.exit_code == 2, result.output
    assert count(db) == {"completed": 2, "failed": 1, "running": 1}
