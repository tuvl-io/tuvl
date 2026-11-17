"""Unit tests for the ``trigger.schedule`` cron scheduler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cronsim import CronSim

from tuvl.contract import registry
from tuvl.contract.schema import WorkflowDocument
from tuvl.core.engine import scheduler as sched

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


# ── collect_schedules ─────────────────────────────────────────────────────────


def _doc(name: str, trigger: dict[str, Any], enabled: bool = True) -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "enabled": enabled,
            "metadata": {"name": name},
            "spec": {
                "trigger": trigger,
                "agents": [
                    {
                        "id": "a",
                        "description": "d",
                        "engine": "pending",
                        "routes": {"default": "END"},
                    }
                ],
            },
        }
    )


def test_collect_schedules_picks_enabled_scheduled_workflows(monkeypatch):
    monkeypatch.setattr(
        registry,
        "WORKFLOWS",
        {
            "cron_wf": _doc("cron_wf", {"schedule": "*/5 * * * *"}),
            "http_wf": _doc("http_wf", {"http": {"path": "/api/x"}}),
            "off_wf": _doc("off_wf", {"schedule": "0 * * * *"}, enabled=False),
        },
    )
    assert sched.collect_schedules() == {"cron_wf": "*/5 * * * *"}


def test_invalid_cron_is_rejected_by_the_contract():
    with pytest.raises(ValueError, match="cron"):
        _doc("bad", {"schedule": "not a cron"})


# ── _lock_key ─────────────────────────────────────────────────────────────────


def test_lock_key_is_deterministic_and_distinct():
    a = sched._lock_key("wf", NOW)
    assert a == sched._lock_key("wf", NOW)
    assert a != sched._lock_key("wf2", NOW)
    assert a != sched._lock_key("wf", NOW + timedelta(minutes=1))
    # Must fit Postgres bigint (advisory lock key space).
    assert -(2**63) <= a < 2**63


# ── _next_after (skip-on-miss) ────────────────────────────────────────────────


def test_next_after_skips_missed_slots():
    it = CronSim("* * * * *", NOW)
    late = NOW + timedelta(minutes=10, seconds=30)
    upcoming = sched._next_after(it, late)
    assert upcoming == NOW + timedelta(minutes=11)


def test_next_after_returns_immediate_next_when_on_time():
    it = CronSim("* * * * *", NOW)
    assert sched._next_after(it, NOW) == NOW + timedelta(minutes=1)


# ── _run_scheduled commit / rollback semantics ────────────────────────────────


class _SessionFactory:
    """Stands in for ``async_sessionmaker`` — ``async with factory() as s``."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _patch_run_deps(monkeypatch, session, uow, engine_cls):
    from tuvl.core.api import manager
    from tuvl.core.datasources import postgres
    from tuvl.core.engine import runner

    monkeypatch.setattr(
        postgres, "get_session_factory", lambda ds, is_write: _SessionFactory(session)
    )
    monkeypatch.setattr(
        manager,
        "build_engine_context",
        lambda config, s, payload=None, *, is_write=None: {
            **(payload or {}),
            "_session": s,
            "_db": uow,
        },
    )
    monkeypatch.setattr(runner, "WorkflowEngine", engine_cls)


def _mock_uow():
    uow = MagicMock()
    uow.commit_secondary = AsyncMock()
    uow.rollback_secondary = AsyncMock()
    return uow


# ── _fire single-fire lock gate ───────────────────────────────────────────────


def _lock_session(acquired: bool) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = acquired
    session.execute.return_value = result
    return session


def _multi(monkeypatch, on: bool) -> None:
    from tuvl.core import bootstrap

    monkeypatch.setattr(
        bootstrap, "LOCKED_DEPLOYMENT_MODE", "multi_tenant" if on else "single_tenant"
    )


def test_multi_tenant_schedules_only_workflows_that_list_tenants(monkeypatch, caplog):
    """TD-40: a scheduled run has no request to take a tenant from, so the workflow names them."""
    monkeypatch.setattr(
        registry,
        "WORKFLOWS",
        {
            "nightly": _doc("nightly", {"schedule": "0 6 * * *", "tenants": ["acme", "globex"]}),
            "orphan": _doc("orphan", {"schedule": "0 7 * * *"}),
        },
    )
    _multi(monkeypatch, True)
    assert sched.schedulable() == {"nightly": "0 6 * * *"}
    assert sched.tenants_for("nightly") == ["acme", "globex"]
    assert sched.tenants_for("orphan") == []


def test_single_tenant_mode_ignores_the_tenant_list(monkeypatch):
    monkeypatch.setattr(
        registry,
        "WORKFLOWS",
        {"nightly": _doc("nightly", {"schedule": "0 6 * * *", "tenants": ["acme"]})},
    )
    _multi(monkeypatch, False)
    assert sched.schedulable() == {"nightly": "0 6 * * *"}
    assert sched.tenants_for("nightly") == [None]


@pytest.mark.asyncio
async def test_a_fire_enqueues_one_run_per_tenant_and_survives_a_failure(monkeypatch):
    monkeypatch.setattr(
        registry,
        "WORKFLOWS",
        {
            "nightly": _doc(
                "nightly", {"schedule": "0 6 * * *", "tenants": ["acme", "bad", "globex"]}
            )
        },
    )
    _multi(monkeypatch, True)
    seen: list[str | None] = []

    async def fake(name: str, fire_at: datetime, tenant: str | None = None) -> bool:
        seen.append(tenant)
        if tenant == "bad":
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(sched, "enqueue_scheduled", fake)
    await sched._fire("nightly", NOW)
    assert seen == ["acme", "bad", "globex"]  # the failure did not skip globex


def test_triggers_and_lock_keys_are_distinct_per_tenant():
    assert sched.schedule_trigger(NOW) == f"schedule:{NOW.isoformat()}"
    assert sched.schedule_trigger(NOW, "acme") == f"schedule:{NOW.isoformat()}:acme"
    keys = {sched._lock_key("w", NOW, t) for t in (None, "acme", "globex")}
    assert len(keys) == 3


def test_trigger_tenants_are_validated():
    ok = _doc("w", {"schedule": "0 6 * * *", "tenants": ["acme", "globex-2"]})
    assert ok.spec.trigger.tenants == ["acme", "globex-2"]
    with pytest.raises(ValueError, match="must match"):
        _doc("w", {"schedule": "0 6 * * *", "tenants": ["has space"]})
    with pytest.raises(ValueError, match="must not repeat"):
        _doc("w", {"schedule": "0 6 * * *", "tenants": ["a", "a"]})
    with pytest.raises(ValueError, match="only applies to a scheduled trigger"):
        _doc("w", {"http": {"path": "/api/x"}, "tenants": ["acme"]})


def test_the_schema_tenant_pattern_matches_the_public_one():
    from tuvl.contract.schema import _TENANT_ID
    from tuvl.tenancy import TENANT_ID_PATTERN

    assert _TENANT_ID.pattern == TENANT_ID_PATTERN.pattern


# ── schedule object: enabled flag and input ───────────────────────────────────


def test_the_object_form_and_the_cron_shorthand_are_the_same_schedule():
    short = _doc("w", {"schedule": "0 9 * * 1-5"}).spec.trigger.schedule
    full = _doc("w", {"schedule": {"cron": "0 9 * * 1-5"}}).spec.trigger.schedule
    assert short == full and short.enabled and short.input == {}


def test_a_disabled_schedule_is_not_collected(monkeypatch):
    monkeypatch.setattr(
        registry,
        "WORKFLOWS",
        {
            "on": _doc("on", {"schedule": {"cron": "0 * * * *", "enabled": True}}),
            "off": _doc("off", {"schedule": {"cron": "0 * * * *", "enabled": False}}),
        },
    )
    assert sched.collect_schedules() == {"on": "0 * * * *"}


def test_a_disabled_http_trigger_is_not_active():
    doc = _doc("w", {"http": {"path": "/w", "enabled": False}, "schedule": "0 * * * *"})
    assert doc.spec.trigger.http is not None and doc.spec.trigger.active_http is None
    assert doc.spec.trigger.active_schedule is not None


@pytest.mark.asyncio
async def test_a_fire_starts_the_run_with_the_schedules_input(monkeypatch):
    doc = _doc("w", {"schedule": {"cron": "0 * * * *", "input": {"report": "daily"}}})
    host = MagicMock()
    host.registry.return_value = {"w": doc}
    host.enqueue = AsyncMock(return_value="run-1")
    host.engine = MagicMock()
    monkeypatch.setattr("tuvl.runtime.host.get_host", lambda: host)

    class _Session:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_a: Any) -> None: ...

        async def execute(self, stmt: Any, params: Any = None) -> Any:
            result = MagicMock()
            result.scalar.return_value = True
            result.scalar_one.return_value = 0
            return result

        async def commit(self) -> None: ...

    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession", _Session)
    assert await sched.enqueue_scheduled("w", NOW) is True
    assert host.enqueue.await_args.args[1] == {"report": "daily"}


def test_upcoming_lists_the_next_fires():
    fires = sched.upcoming("0 9 * * *", 3, after=NOW)
    assert fires == [
        datetime(2026, 7, 26, 9, tzinfo=UTC),
        datetime(2026, 7, 27, 9, tzinfo=UTC),
        datetime(2026, 7, 28, 9, tzinfo=UTC),
    ]


async def test_a_schedule_that_cannot_advance_is_dropped_and_the_others_keep_firing(monkeypatch):
    import asyncio

    fired: list[str] = []
    monkeypatch.setattr(sched, "schedulable", lambda: {"good": "good", "bad": "bad"})

    async def fake_fire(name, fire_at):
        fired.append(name)

    class FakeSim:
        def __init__(self, expr, now):
            self.expr, self.calls = expr, 0

        def __iter__(self):
            return self

        def __next__(self):
            self.calls += 1
            if self.expr == "bad" and self.calls > 1:
                raise RuntimeError("no such day")
            return datetime.now(UTC) + timedelta(seconds=0.05)

    monkeypatch.setattr(sched, "_fire", fake_fire)
    monkeypatch.setattr(sched, "CronSim", FakeSim)
    loop = asyncio.create_task(sched.scheduler_loop())
    await asyncio.sleep(0.6)
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    assert fired.count("bad") == 1
    assert fired.count("good") >= 3
