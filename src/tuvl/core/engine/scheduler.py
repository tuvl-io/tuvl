"""In-process cron scheduler for workflows declaring ``trigger.schedule``.

A workflow may carry a 5-field cron expression (UTC) alongside — or instead
of — its HTTP trigger::

    trigger:
      schedule: "*/5 * * * *"

or, with the input every run starts with and an off switch::

    trigger:
      schedule:
        cron: "*/5 * * * *"
        enabled: true
        input: {report: daily}

One asyncio task per process sleeps until the nearest fire time. A fire
enqueues a run (no principal, the schedule's ``input``) whose trigger is
``schedule:<fire time>``; the workers execute it. Every process wakes, and
the check-and-enqueue runs under a Postgres advisory lock keyed on
(workflow, fire time), so a fire is enqueued once even when another process
takes the lock after the first released it. Fires missed while the process
is down are skipped — there is no catch-up.

Multi-tenant mode: a scheduled run has no request to take a tenant from, so the
workflow lists its tenants::

    trigger:
      schedule: "0 6 * * *"
      tenants: [acme, globex]

Each fire enqueues one run per tenant (trigger ``schedule:<fire time>:<tenant>``),
created under that tenant's scope. A scheduled workflow with no ``tenants`` is not
scheduled in multi-tenant mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from datetime import UTC, datetime

import structlog
from cronsim import CronSim, CronSimError
from sqlalchemy import text

from tuvl.tenancy import tenant_scope

logger = structlog.get_logger(__name__)

# Strong refs to in-flight fire tasks (create_task results are weakly held).
_FIRE_TASKS: set[asyncio.Task] = set()


def collect_schedules() -> dict[str, str]:
    """Return {workflow_name: cron_expr} for every scheduled, valid workflow."""
    from tuvl.contract.registry import WORKFLOWS

    schedules: dict[str, str] = {}
    for name, doc in WORKFLOWS.items():
        schedule = doc.spec.trigger.active_schedule
        if not doc.enabled or schedule is None:
            continue
        expr = schedule.cron
        try:
            CronSim(expr, datetime.now(UTC))
        except CronSimError as exc:
            logger.error("workflow.schedule_invalid", workflow=name, schedule=expr, error=str(exc))
            continue
        schedules[name] = expr
    return schedules


def _multi_tenant() -> bool:
    from tuvl.core import bootstrap

    return bootstrap.LOCKED_DEPLOYMENT_MODE == "multi_tenant"


def tenants_for(name: str) -> list[str | None]:
    """Who a fire of *name* runs for: ``[None]`` (single-tenant), or its listed tenants."""
    from tuvl.contract.registry import WORKFLOWS

    doc = WORKFLOWS.get(name)
    listed = list(doc.spec.trigger.tenants) if doc else []
    if not _multi_tenant():
        return [None]
    return list(listed)


def upcoming(expr: str, count: int, after: datetime | None = None) -> list[datetime]:
    """The next *count* fire times of cron *expr* (UTC). Raises ``CronSimError`` if invalid."""
    iterator = CronSim(expr, after or datetime.now(UTC))
    return [next(iterator) for _ in range(count)]


def _lock_key(workflow: str, fire_at: datetime, tenant: str | None = None) -> int:
    suffix = f":{tenant}" if tenant else ""
    digest = hashlib.sha256(
        f"tuvl:sched:{workflow}:{fire_at.isoformat()}{suffix}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _next_after(iterator: CronSim, now: datetime) -> datetime:
    """Advance the cron iterator past every slot <= *now* — skip, no catch-up."""
    upcoming = next(iterator)
    while upcoming <= now:
        upcoming = next(iterator)
    return upcoming


def schedule_trigger(fire_at: datetime, tenant: str | None = None) -> str:
    return f"schedule:{fire_at.isoformat()}" + (f":{tenant}" if tenant else "")


async def enqueue_scheduled(name: str, fire_at: datetime, tenant: str | None = None) -> bool:
    """Enqueue the 2.0 run for this fire (and tenant) unless one exists. True when enqueued."""
    import sqlalchemy as sa
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    from tuvl.runtime.host import InputError, WorkflowUnavailableError, get_host
    from tuvl.runtime.models import SystemRun

    host = get_host()
    doc = host.registry().get(name)
    if doc is None:
        return False
    trigger = schedule_trigger(fire_at, tenant)
    schedule = doc.spec.trigger.schedule
    payload = dict(schedule.input) if schedule else {}
    key = _lock_key(name, fire_at, tenant)
    async with AsyncSession(host.engine) as lock_session:
        got = (
            await lock_session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        ).scalar()
        if not got:
            return False
        try:
            existing = await lock_session.execute(
                sa.select(sa.func.count())
                .select_from(SystemRun)
                .where(
                    SystemRun.workflow == name,  # type: ignore[arg-type]
                    SystemRun.trigger == trigger,  # type: ignore[arg-type]
                    SystemRun.trigger.like("schedule:%"),  # type: ignore[attr-defined]  # the index's predicate
                )
            )
            if existing.scalar_one():
                return False
            try:
                with tenant_scope(tenant) if tenant else contextlib.nullcontext():
                    run_id = await host.enqueue(doc, payload, principal=None, trigger=trigger)
            except IntegrityError:
                return (
                    False  # the unique index on (workflow, trigger): another process fired it first
                )
            except InputError as exc:
                logger.error("workflow.scheduled_input_invalid", workflow=name, errors=exc.errors)
                return False
            except WorkflowUnavailableError as exc:
                logger.error("workflow.scheduled_unavailable", workflow=name, reasons=exc.reasons)
                return False
            logger.info(
                "workflow.scheduled_enqueued",
                workflow=name,
                tenant=tenant,
                fire_at=str(fire_at),
                run_id=str(run_id),
            )
            return True
        finally:
            await lock_session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            await lock_session.commit()


async def _fire(name: str, fire_at: datetime) -> None:
    for tenant in tenants_for(name):
        try:
            await enqueue_scheduled(name, fire_at, tenant)
        except Exception:  # one tenant's failure must not skip the others
            logger.exception("workflow.scheduled_fire_failed", workflow=name, tenant=tenant)


def schedulable() -> dict[str, str]:
    """The schedules this process will fire: ``collect_schedules`` minus what the mode rules out.

    Multi-tenant mode has no request to take a tenant from, so a workflow that lists no
    ``trigger.tenants`` is not scheduled. In single-tenant mode ``tenants`` has no meaning.
    """
    from tuvl.contract.registry import WORKFLOWS

    schedules = collect_schedules()
    for name in list(schedules):
        listed = WORKFLOWS[name].spec.trigger.tenants
        if _multi_tenant() and not listed:
            logger.error(
                "workflow.schedule_needs_tenants",
                workflow=name,
                reason="multi_tenant mode: list the tenants to run for in trigger.tenants",
            )
            del schedules[name]
        elif not _multi_tenant() and listed:
            logger.warning(
                "workflow.schedule_tenants_ignored",
                workflow=name,
                reason="trigger.tenants only applies in multi_tenant mode",
            )
    return schedules


async def scheduler_loop() -> None:
    """Lifespan background task — see module docstring."""
    schedules = schedulable()
    if not schedules:
        logger.info("scheduler.no_scheduled_workflows")
        return
    logger.info("scheduler.started", schedules=schedules)

    now = datetime.now(UTC)
    iterators: dict[str, CronSim] = {}
    next_fire: dict[str, datetime] = {}
    for name, expr in schedules.items():
        try:
            iterators[name] = CronSim(expr, now)
            next_fire[name] = next(iterators[name])
        except Exception:  # an expression that never matches must not stop the other schedules
            logger.exception("scheduler.schedule_dropped", workflow=name, cron=expr)

    while next_fire:
        try:
            await _wait_and_fire(iterators, next_fire)
        except Exception:
            logger.exception("scheduler.tick_failed")
            await asyncio.sleep(5)
    logger.error("scheduler.no_schedules_left")


async def _wait_and_fire(iterators: dict[str, CronSim], next_fire: dict[str, datetime]) -> None:
    soonest = min(next_fire.values())
    delay = (soonest - datetime.now(UTC)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)

    now = datetime.now(UTC)
    for name, fire_at in list(next_fire.items()):
        if fire_at > now:
            continue
        task = asyncio.create_task(_fire(name, fire_at))
        _FIRE_TASKS.add(task)
        task.add_done_callback(_FIRE_TASKS.discard)
        try:
            next_fire[name] = _next_after(iterators[name], now)
        except Exception:
            logger.exception("scheduler.schedule_dropped", workflow=name)
            del next_fire[name]
