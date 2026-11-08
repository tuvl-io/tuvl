"""The once-only IAM seed: `kind: RoleSeed` roles plus the TUVL_BOOTSTRAP_ADMIN_* administrator."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tuvl.core.auth import iam_service
from tuvl.core.auth.models import IAMRole, IAMRoleScope, IAMUser, IAMUserRole
from tuvl.core.auth.seed import (
    ROLE_SEED_REGISTRY,
    RoleSeedEntry,
    parse_role_seed,
    read_admin_from_env,
    register_role_seed_from_dict,
    seed_iam,
)
from tuvl.core.bootstrap import SystemRegistry

pytestmark = pytest.mark.asyncio

ROLES = [
    RoleSeedEntry("recruiter", "Reads candidates", ("candidate:read", "interview:read")),
    RoleSeedEntry("viewer", None, ()),
]


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            SQLModel.metadata.create_all,
            tables=[
                IAMUser.__table__,
                IAMRole.__table__,
                IAMUserRole.__table__,
                IAMRoleScope.__table__,
                SystemRegistry.__table__,
            ],
        )
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


async def _names(session):
    return sorted(r.name for r in (await session.exec(select(IAMRole))).all())


async def test_a_fresh_database_gets_the_roles_and_the_administrator(session):
    result = await seed_iam(
        session, roles=ROLES, admin_email="root@example.com", admin_password="pw-1234567"
    )
    await session.commit()
    assert result.status == "seeded" and result.admin_email == "root@example.com"
    assert await _names(session) == ["recruiter", "superadmin", "viewer"]
    token = await iam_service.login(session, login_str="root@example.com", password="pw-1234567")
    assert token
    assert await iam_service.active_admin_count(session) == 1


async def test_seeded_roles_carry_their_scopes(session):
    await seed_iam(session, roles=ROLES, admin_email=None, admin_password=None)
    await session.commit()
    role = await iam_service.get_role_by_name(session, "recruiter")
    assert role.description == "Reads candidates"
    assert sorted(await iam_service.load_role_scopes(session, role.id)) == [
        "candidate:read",
        "interview:read",
    ]


async def test_it_runs_once(session):
    await seed_iam(session, roles=ROLES, admin_email="a@example.com", admin_password="pw-1234567")
    await session.commit()
    again = await seed_iam(session, roles=ROLES, admin_email="a@example.com", admin_password="x")
    assert again.status == "already_seeded"
    assert await _names(session) == ["recruiter", "superadmin", "viewer"]


async def test_deleting_everything_later_does_not_seed_again(session):
    await seed_iam(session, roles=ROLES, admin_email="a@example.com", admin_password="pw-1234567")
    await session.commit()
    for model in (IAMUserRole, IAMRoleScope, IAMUser, IAMRole):
        for row in (await session.exec(select(model))).all():
            await session.delete(row)
    await session.commit()
    result = await seed_iam(session, roles=ROLES, admin_email="a@example.com", admin_password="pw")
    assert result.status == "already_seeded"
    assert await _names(session) == []


async def test_a_database_that_already_has_iam_data_is_left_alone(session):
    await iam_service.create_role(session, name="legacy", scopes=["x:read"])
    result = await seed_iam(
        session, roles=ROLES, admin_email="a@example.com", admin_password="pw-1234567"
    )
    await session.commit()
    assert result.status == "populated"
    assert await _names(session) == ["legacy"]
    assert (await session.exec(select(IAMUser))).first() is None
    # and the marker means a later boot does not retry once the legacy data is gone
    assert (await session.get(SystemRegistry, "iam_seeded")) is not None


async def test_nothing_to_seed_writes_no_marker(session):
    result = await seed_iam(session, roles=[], admin_email=None, admin_password=None)
    await session.commit()
    assert result.status == "nothing_to_seed"
    assert await session.get(SystemRegistry, "iam_seeded") is None
    # so a later boot that does carry a seed still applies
    later = await seed_iam(session, roles=ROLES, admin_email=None, admin_password=None)
    assert later.status == "seeded"


async def test_half_an_administrator_is_reported_and_skipped(session):
    result = await seed_iam(session, roles=ROLES, admin_email="a@example.com", admin_password=None)
    await session.commit()
    assert result.status == "seeded" and result.admin_email is None
    assert any("must both be set" in n for n in result.notes)
    assert (await session.exec(select(IAMUser))).first() is None


async def test_a_seeded_superadmin_role_is_reused_for_the_administrator(session):
    roles = [RoleSeedEntry("superadmin", "Everything", ("iam:admin", "runs:read"))]
    await seed_iam(session, roles=roles, admin_email="a@example.com", admin_password="pw-1234567")
    await session.commit()
    supers = [r for r in (await session.exec(select(IAMRole))).all() if r.name == "superadmin"]
    assert len(supers) == 1
    assert supers[0].description == "Everything"
    assert await iam_service.active_admin_count(session) == 1


async def test_no_administrator_and_no_admin_role_is_called_out(session):
    result = await seed_iam(session, roles=ROLES, admin_email=None, admin_password=None)
    assert any("reset-admin" in n for n in result.notes)


# ── the document ──────────────────────────────────────────────────────────────


def _doc(roles):
    return {"kind": "RoleSeed", "spec": {"roles": roles}}


def test_a_role_seed_parses():
    entries = parse_role_seed(
        _doc([{"name": "ops", "description": "d", "scopes": ["runs:read", "runs:read"]}])
    )
    assert entries == [RoleSeedEntry("ops", "d", ("runs:read",))]


@pytest.mark.parametrize(
    "doc, message",
    [
        ({"kind": "RoleSeed"}, "spec must be"),
        (_doc([]), "non-empty"),
        (_doc([{"description": "x"}]), "name"),
        (_doc([{"name": "bad name"}]), "name"),
        (_doc([{"name": "a"}, {"name": "a"}]), "twice"),
        (_doc([{"name": "a", "scopes": "runs:read"}]), "scopes"),
        (_doc([{"name": "a", "scopes": ["has space"]}]), "scopes"),
        (_doc([{"name": "a", "extra": 1}]), "unknown field"),
    ],
)
def test_a_malformed_role_seed_says_what_is_wrong(doc, message):
    with pytest.raises(ValueError, match=message):
        parse_role_seed(doc)


def test_a_role_defined_in_two_seed_files_is_refused():
    ROLE_SEED_REGISTRY.clear()
    try:
        register_role_seed_from_dict(_doc([{"name": "ops"}]), "a.yaml")
        with pytest.raises(ValueError, match="already defined"):
            register_role_seed_from_dict(_doc([{"name": "ops"}]), "b.yaml")
    finally:
        ROLE_SEED_REGISTRY.clear()


def test_the_administrator_comes_from_the_environment_and_a_file_wins(monkeypatch, tmp_path):
    from tuvl.core.config import settings

    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    monkeypatch.setattr(settings, "tuvl_bootstrap_admin_email", " root@example.com ")
    monkeypatch.setattr(settings, "tuvl_bootstrap_admin_password", "from-env")
    monkeypatch.setattr(settings, "tuvl_bootstrap_admin_password_file", None)
    assert read_admin_from_env() == ("root@example.com", "from-env")
    monkeypatch.setattr(settings, "tuvl_bootstrap_admin_password_file", str(secret))
    assert read_admin_from_env() == ("root@example.com", "from-file")
    monkeypatch.setattr(settings, "tuvl_bootstrap_admin_password_file", str(tmp_path / "gone"))
    with pytest.raises(RuntimeError, match="cannot be read"):
        read_admin_from_env()


def test_validate_reports_a_bad_or_duplicated_role_seed(tmp_path):
    from tuvl.cli.commands.validate import ValidateResult, _validate_role_seeds

    (tmp_path / "a.yaml").write_text("kind: RoleSeed\nspec:\n  roles: [{name: ops}]\n")
    (tmp_path / "b.yaml").write_text("kind: RoleSeed\nspec:\n  roles: [{name: ops}]\n")
    (tmp_path / "c.yaml").write_text("kind: RoleSeed\nspec:\n  roles: []\n")
    result = ValidateResult()
    _validate_role_seeds(
        [tmp_path / "a.yaml", tmp_path / "b.yaml", tmp_path / "c.yaml"], tmp_path, result
    )
    messages = [i.message for i in result.issues]
    assert any("also defined in a.yaml" in m for m in messages)
    assert any("non-empty" in m for m in messages)
    assert all(i.code == "V044" for i in result.issues)
