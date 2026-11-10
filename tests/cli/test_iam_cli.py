"""`tuvl iam`: the administrator signs in, then manages roles, scopes and users in the database.

The database is a SQLite file: the command opens its own engine on every call, as it does against
PostgreSQL, so each invocation is a separate process-like session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl.cli.commands import iam as iam_cli
from tuvl.core.auth import iam_service
from tuvl.core.auth.blacklist import token_blacklist
from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole
from tuvl.core.auth.seed import parse_role_seed
from tuvl.core.config import settings

ADMIN = "root@example.com"
ADMIN_PW = "admin-pw-1234"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean():
    token_blacklist._memory.clear()
    yield
    token_blacklist._memory.clear()


@pytest.fixture
def db(monkeypatch, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'iam.db'}"

    async def create() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(
                SQLModel.metadata.create_all,
                tables=[
                    IAMUser.__table__,
                    IAMRole.__table__,
                    IAMUserRole.__table__,
                    IAMRoleScope.__table__,
                ],
            )
        await engine.dispose()

    asyncio.run(create())

    async def session_gen():
        engine = create_async_engine(url)
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await engine.dispose()

    from tuvl.core.datasources import postgres

    monkeypatch.setattr(postgres, "_PRIMARY_DS", "main")
    monkeypatch.setitem(postgres._sessions_rw, "main", object())
    monkeypatch.setattr(postgres, "get_primary_rw_session", session_gen)

    async def no_close() -> None:
        return None

    monkeypatch.setattr(postgres, "close_db", no_close)
    monkeypatch.setattr(iam_cli, "_load_project", lambda _dir: None)
    monkeypatch.setattr(iam_cli, "_load_config", lambda _dir: None)

    class Db:
        def run(self, fn):
            async def go():
                gen = session_gen()
                session = await gen.__anext__()
                try:
                    return await fn(session)
                finally:
                    await gen.aclose()

            return asyncio.run(go())

    handle = Db()
    handle.run(lambda s: iam_service.bootstrap(s, email=ADMIN, password=ADMIN_PW))
    return handle


def iam(*args, password=ADMIN_PW, email=ADMIN, **env):
    environ = {"TUVL_IAM_EMAIL": email, "TUVL_IAM_PASSWORD": password, **env}
    return runner.invoke(app, ["iam", *args], env=environ)


def test_the_wrong_password_is_refused(db):
    result = iam("roles", "list", password="nope")
    assert result.exit_code == 1
    assert "Incorrect email or password" in result.output


def test_a_user_without_iam_admin_is_refused(db):
    db.run(lambda s: iam_service.create_user(s, email="user@example.com", password="user-pw-1234"))
    result = iam("roles", "list", email="user@example.com", password="user-pw-1234")
    assert result.exit_code == 1 and "does not hold iam:admin" in result.output


def test_roles_are_created_granted_disabled_and_deleted(db):
    assert (
        iam("roles", "create", "recruiter", "--description", "Reads", "-s", "a:read").exit_code == 0
    )
    assert iam("roles", "grant", "recruiter", "b:read").exit_code == 0
    listing = iam("roles", "list").output
    assert "recruiter" in listing and "a:read" in listing and "b:read" in listing

    assert iam("roles", "revoke", "recruiter", "a:read").exit_code == 0
    assert iam("roles", "revoke", "recruiter", "a:read").exit_code == 1  # no longer granted

    assert iam("roles", "disable", "recruiter").exit_code == 0
    assert "disabled" in iam("roles", "list").output
    assert iam("roles", "enable", "recruiter").exit_code == 0

    assert iam("roles", "delete", "recruiter", "--yes").exit_code == 0
    assert "recruiter" not in iam("roles", "list").output


def test_a_duplicate_role_and_a_missing_role_say_so(db):
    assert iam("roles", "create", "ops").exit_code == 0
    assert "already exists" in iam("roles", "create", "ops").output
    result = iam("roles", "grant", "ghost", "x:read")
    assert result.exit_code == 1 and "No role named 'ghost'" in result.output


def test_the_last_administrator_cannot_be_locked_out(db):
    for args in (
        ("roles", "disable", "superadmin"),
        ("roles", "revoke", "superadmin", "iam:admin"),
        ("roles", "remove-user", "superadmin", ADMIN),
        ("users", "disable", ADMIN),
    ):
        result = iam(*args)
        assert result.exit_code == 1, args
        assert "no active administrator" in result.output
    assert iam("roles", "delete", "superadmin", "--force", "--yes").exit_code == 1
    assert iam("roles", "list").exit_code == 0  # still signed in


def test_membership_and_users(db):
    assert iam("roles", "create", "ops", "-s", "runs:read").exit_code == 0
    created = iam(
        "users", "create", "ann@example.com", "--first-name", "Ann", "-r", "ops",
        TUVL_IAM_NEW_PASSWORD="ann-pw-12345",
    )  # fmt: skip
    assert created.exit_code == 0, created.output
    assert "ann@example.com" in iam("users", "list").output

    # the new user can sign in but is not an administrator
    assert iam("roles", "list", email="ann@example.com", password="ann-pw-12345").exit_code == 1

    removed = iam("roles", "remove-user", "ops", "ann@example.com")
    assert removed.exit_code == 0
    assert iam("roles", "add-user", "ops", "ann@example.com").exit_code == 0
    assert iam("users", "disable", "ann@example.com").exit_code == 0
    assert "disabled" in iam("users", "list").output
    assert iam("users", "enable", "ann@example.com").exit_code == 0


def test_a_short_password_is_refused(db):
    result = iam("users", "create", "ann@example.com", TUVL_IAM_NEW_PASSWORD="short")
    assert result.exit_code == 1 and "at least" in result.output


def test_a_role_with_members_needs_force(db):
    iam("roles", "create", "ops")
    iam("users", "create", "ann@example.com", "-r", "ops", TUVL_IAM_NEW_PASSWORD="ann-pw-12345")
    refused = iam("roles", "delete", "ops", "--yes")
    assert refused.exit_code == 1 and "still has 1 member" in refused.output
    assert iam("roles", "delete", "ops", "--yes", "--force").exit_code == 0


def test_deleting_asks_to_confirm(db):
    iam("roles", "create", "ops")
    declined = runner.invoke(
        app,
        ["iam", "roles", "delete", "ops"],
        input="n\n",
        env={"TUVL_IAM_EMAIL": ADMIN, "TUVL_IAM_PASSWORD": ADMIN_PW},
    )
    assert declined.exit_code == 1
    assert "ops" in iam("roles", "list").output


def test_the_password_can_come_from_stdin(db):
    result = runner.invoke(
        app, ["iam", "--email", ADMIN, "--password-stdin", "roles", "list"], input=f"{ADMIN_PW}\n"
    )
    assert result.exit_code == 0, result.output


def test_reset_admin_recovers_without_a_login(db):
    # lock everyone out underneath the guard
    async def lock(session):
        from sqlalchemy import update

        await session.exec(update(IAMUser).values(is_active=False))
        await session.commit()

    db.run(lock)
    assert iam("roles", "list").exit_code == 1

    result = runner.invoke(
        app,
        ["iam", "reset-admin", ADMIN],
        env={"TUVL_IAM_NEW_PASSWORD": "fresh-pw-12345"},
    )
    assert result.exit_code == 0, result.output
    assert iam("roles", "list", password="fresh-pw-12345").exit_code == 0


def test_check_names_scopes_no_active_role_grants(db, monkeypatch):
    monkeypatch.setattr(
        iam_cli,
        "_project_scopes",
        lambda: {"iam:admin": ["IAM administration"], "ticket:write": ["workflow triage"]},
    )
    failed = iam("check")
    assert failed.exit_code == 1 and "ticket:write" in failed.output
    iam("roles", "create", "agents", "-s", "ticket:write")
    assert iam("check").exit_code == 0
    iam("roles", "disable", "agents")
    assert iam("check").exit_code == 1  # a disabled role grants nothing


@pytest.mark.requires_insight
def test_export_writes_a_role_seed_the_loader_accepts(db, monkeypatch, tmp_path):
    iam("roles", "create", "recruiter", "--description", "Reads", "-s", "b:read", "-s", "a:read")
    iam("roles", "create", "dormant", "-s", "z:read")
    iam("roles", "disable", "dormant")
    monkeypatch.setattr(settings, "tuvl_env", "development")
    target = tmp_path / "roles.yaml"
    # a developer's database needs no login to export
    result = runner.invoke(app, ["iam", "export", "--out", str(target)])
    assert result.exit_code == 0, result.output
    document = yaml.safe_load(target.read_text())
    entries = {e.name: e for e in parse_role_seed(document)}
    assert set(entries) == {"recruiter", "superadmin"}
    assert entries["recruiter"].scopes == ("a:read", "b:read")
    assert "password" not in target.read_text().lower()


@pytest.mark.requires_insight
def test_export_in_production_asks_for_the_login(db, monkeypatch):
    monkeypatch.setattr(settings, "tuvl_env", "production")
    assert (
        runner.invoke(app, ["iam", "export"], env={"TUVL_IAM_EMAIL": ADMIN}, input="\n").exit_code
        == 1
    )
    assert iam("export").exit_code == 0


def test_no_primary_datasource_is_explained(monkeypatch):
    from tuvl.core.datasources import postgres

    monkeypatch.setattr(iam_cli, "_load_project", lambda _dir: None)
    monkeypatch.setattr(postgres, "_PRIMARY_DS", "")
    result = runner.invoke(
        app,
        ["iam", "roles", "list"],
        env={"TUVL_IAM_EMAIL": ADMIN, "TUVL_IAM_PASSWORD": ADMIN_PW},
    )
    assert result.exit_code == 1 and "No primary PostgreSQL datasource" in result.output


def test_iam_is_in_the_core_cli_not_the_toolchain():
    from tuvl.cli import TOOLCHAIN_COMMANDS

    assert "iam" not in TOOLCHAIN_COMMANDS
    assert Path(iam_cli.__file__).name == "iam.py"
