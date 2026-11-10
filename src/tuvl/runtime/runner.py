"""The 2.0 runner (spec §8, §14): walks a workflow graph one agent at a time.

For each agent the runner builds the *view* (exactly the declared inputs,
contract-checked), calls the engine under the agent's timeout and retry
budget, checks the result against the outputs declared for the emitted
signal, routes, and checkpoints. Under ``per_agent`` transactions an agent's
database writes, its checkpoint and an ownership check on the lease commit
together — a worker that lost its lease cannot commit anything.

A run stops at a named end (rendering its response), when an engine suspends
(human input, approval), on an operator pause/abort, or on an unrouted error.
Rollback ends, unhandled errors and aborts run compensation in reverse
completion order.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tuvl.contract.facts import determinism_of, field_types
from tuvl.contract.schema import WorkflowDocument, duration_seconds
from tuvl.contract.signals import is_reserved
from tuvl.contract.tel import compile_template
from tuvl.contract.types import ModelResolver, build_model, registry_resolver
from tuvl.core.engine.hooks import emit_hooks
from tuvl.runtime import code as _code  # noqa: F401 — registers the code engine
from tuvl.runtime import decide as _decide  # noqa: F401 — registers the decide engine
from tuvl.runtime import deltas, journal, queue
from tuvl.runtime import human as _human  # noqa: F401 — registers the human engine
from tuvl.runtime import llm as _llm  # noqa: F401 — registers the llm engine
from tuvl.runtime import loop as _loop  # noqa: F401 — registers the loop engine
from tuvl.runtime import map as _map  # noqa: F401 — registers the map engine
from tuvl.runtime import parallel as _parallel  # noqa: F401 — registers the parallel engine
from tuvl.runtime import tool as _tool  # noqa: F401 — registers the tool engine
from tuvl.runtime.engines import ENGINES, AgentResult, EngineContext, EngineError, contract_model
from tuvl.runtime.models import SystemRun, SystemRunCheckpoint
from tuvl.runtime.queue import Lease

logger = structlog.get_logger(__name__)
_tracer = trace.get_tracer("tuvl.runtime")

_RUNS = SystemRun.__table__  # type: ignore[attr-defined]
_CHECKPOINTS = SystemRunCheckpoint.__table__  # type: ignore[attr-defined]
#: Checkpoint signal of a run suspended inside an agent (human, approval).
SUSPEND = "suspend"
#: Retry-policy error kinds → the result that matches them.
_RETRYABLE = ("timeout", "rate_limit", "parse_error", "transient")
_BACKOFF_BASE_SECONDS = 0.5


def workflow_hash(doc: WorkflowDocument) -> str:
    canonical = json.dumps(doc.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class _State:
    run: SystemRun
    doc: WorkflowDocument
    context: dict[str, Any]
    completed: list[str]
    next: str
    #: Agent steps finished so far (suspensions excluded) — part of the
    #: idempotency key, so it is stable across retries, crashes and resume.
    step: int
    #: ``workflow_hash(doc)`` as checked at load; keys the compiled contract models.
    workflow_hash: str
    #: Parent runs above this one (workflow-as-tool nesting); fixed for the run.
    depth: int
    tokens: int = 0
    #: The last agent's final result and attempt count (spans, hooks).
    last: AgentResult | None = None
    attempts: int = 0
    #: ``transaction: per_run`` — the run's single data session, and the
    #: savepoint of the agent step in progress.
    data: AsyncSession | None = None
    savepoint: Any = None


@dataclass
class Runner:
    """A :data:`queue.Executor`: ``await runner(run_id, lease)``."""

    engine: AsyncEngine
    workflows: Mapping[str, WorkflowDocument] | None = None
    resolver: ModelResolver = registry_resolver
    _models: dict[tuple[str, str, str], type[BaseModel]] = field(default_factory=dict)

    async def __call__(self, run_id: uuid.UUID, lease: Lease) -> None:
        with _tracer.start_as_current_span("tuvl.run") as span:
            span.set_attribute("tuvl.run_id", str(run_id))
            state = await self.load(run_id, lease)
            if state is None:
                return
            span.set_attribute("tuvl.workflow", state.doc.metadata.name)
            if state.doc.spec.transaction == "per_run":
                # One transaction for the whole run; nothing is checkpointed, so a
                # crash simply reruns from the start (spec §14.6).
                state.data = AsyncSession(self.engine, expire_on_commit=False)
            try:
                await self.walk(state, lease)
            finally:
                if state.data is not None:
                    await state.data.close()

    async def walk(self, state: _State, lease: Lease) -> None:
        by_id = {a.id: a for a in state.doc.spec.agents}
        while True:
            if lease.lost.is_set():
                return
            if state.next == "END" or state.next.startswith("END."):
                await self.finish(
                    state, lease, state.next[4:] if state.next.startswith("END.") else "default"
                )
                return
            if self.dev_pause_possible(state):
                if await self.honour_control(state, lease):
                    return
                if await self.dev_pause(state, lease):
                    return
            agent = by_id[state.next]
            outcome = await self.step(state, lease, agent)
            if outcome == "control":
                if await self.honour_control(state, lease):
                    return
                continue
            if outcome != "continue":
                return

    # ── loading ──────────────────────────────────────────────────────────────

    def registry(self) -> Mapping[str, WorkflowDocument]:
        if self.workflows is not None:
            return self.workflows
        from tuvl.contract.registry import WORKFLOWS

        return WORKFLOWS

    async def load(self, run_id: uuid.UUID, lease: Lease) -> _State | None:
        async with AsyncSession(self.engine, expire_on_commit=False) as s:
            run = await s.get(SystemRun, run_id)
            if run is None:
                return None
            cp = await journal.latest_checkpoint(s, run_id)
            doc = self.registry().get(run.workflow)
            problem = None
            if doc is None:
                problem = ("workflow_not_found", f"workflow '{run.workflow}' is not loaded")
            elif workflow_hash(doc) != run.workflow_hash:
                problem = (
                    "workflow_changed",
                    f"workflow '{run.workflow}' changed since this run started",
                )
            if problem is not None:
                await self.fail(s, run_id, lease, problem[0], problem[1])
                await s.commit()
                return None
            assert doc is not None
            fresh = cp is None and run.claims <= 1
            steps = await queue._execute(
                s,
                sa.select(sa.func.count())
                .select_from(_CHECKPOINTS)
                .where(_CHECKPOINTS.c.run_id == run_id, _CHECKPOINTS.c.signal != SUSPEND),
            )
            step = int(steps.scalar_one())
            depth = await self.depth_of(run, s)
            await journal.append_event(
                s, run_id, "run_started" if fresh else "resumed", payload={"claims": run.claims}
            )
            await s.commit()
        if cp is None:
            return _State(
                run, doc, dict(run.input), [], doc.spec.entry, 0, run.workflow_hash, depth
            )
        return _State(
            run,
            doc,
            dict(cp.context),
            list(cp.completed),
            cp.next,
            step,
            run.workflow_hash,
            depth,
            run.tokens_used,
        )

    # ── one agent ────────────────────────────────────────────────────────────

    def model(self, state: _State, agent: Any, which: str) -> type[BaseModel]:
        key = (state.workflow_hash, agent.id, which)
        if key not in self._models:
            fields = agent.inputs if which == "inputs" else agent.outputs_for(which)
            self._models[key] = build_model(
                f"{agent.id}.{which}", field_types(fields), self.resolver
            )
        return self._models[key]

    async def step(self, state: _State, lease: Lease, agent: Any) -> str:
        """One agent invocation, in its own span and between its hooks. ``"control"``
        means an operator request is pending and nothing ran."""
        if not await self.start_agent(state, agent):
            return "control"
        workflow = state.doc.metadata.name
        hooks = [*agent.hooks, *state.doc.spec.hooks]
        await emit_hooks(hooks, "before_step", workflow=workflow, step_id=agent.id)
        with _tracer.start_as_current_span(f"tuvl.agent {agent.id}") as span:
            span.set_attributes(
                {
                    "tuvl.workflow": workflow,
                    "tuvl.agent": agent.id,
                    "tuvl.engine": str(agent.engine),
                    "tuvl.determinism": determinism_of(agent),
                }
            )
            outcome = await self.run_agent(state, lease, agent)
            last = state.last
            if last is not None:
                span.set_attribute("tuvl.signal", last.signal)
                span.set_attribute("tuvl.tokens", last.tokens)
                span.set_attribute("tuvl.attempt", state.attempts)
                if last.error_type:
                    span.set_attribute("tuvl.error_type", last.error_type)
                    span.set_status(Status(StatusCode.ERROR, last.error_type))
        if last is not None:
            detail = {"signal": last.signal, "error_type": last.error_type}
            await emit_hooks(
                hooks, "after_step", workflow=workflow, step_id=agent.id, detail=detail
            )
            if last.signal == "error" or last.error_type:
                await emit_hooks(
                    hooks, "on_error", workflow=workflow, step_id=agent.id, detail=detail
                )
        return outcome

    async def start_agent(self, state: _State, agent: Any) -> bool:
        """Journal ``agent_started`` unless an operator request (pause, abort …) is
        pending; the ``control`` read and the event share one statement and transaction."""
        run_id = state.run.id
        async with AsyncSession(self.engine) as s:
            allocated = await journal.allocate_seqs(s, run_id, 1, returning=(_RUNS.c.control,))
            if allocated is None:
                raise journal.JournalError(f"run {run_id} does not exist")
            seq, (control,) = allocated
            if control and control.get("action") != "steer":
                await s.rollback()
                return False
            await journal.append_event(s, run_id, "agent_started", agent_id=agent.id, seq=seq)
            await s.commit()
        return True

    async def run_agent(self, state: _State, lease: Lease, agent: Any) -> str:
        state.last, state.attempts = None, 0
        started = time.monotonic()
        raw = {k: state.context[k] for k in agent.inputs if k in state.context}
        try:
            view = self.model(state, agent, "inputs").model_validate(raw).model_dump(by_alias=True)
        except ValidationError as exc:
            result = AgentResult(
                "error", error_type="contract_violation", message=f"inputs: {_brief(exc)}"
            )
            session = state.data or AsyncSession(self.engine, expire_on_commit=False)
            if state.data is not None:
                state.savepoint = await session.begin_nested()
            return await self.complete(state, lease, agent, result, session, started)

        retry = agent.budget.retry
        attempt = 0
        while True:
            attempt += 1
            state.attempts = attempt
            result, session = await self.call_engine(state, agent, view, str(state.step))
            kind = _retry_kind(result)
            if retry is not None and kind in retry.errors and attempt < retry.attempts:
                await self.discard(state, session)
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * retry.backoff ** (attempt - 1))
                continue
            return await self.complete(state, lease, agent, result, session, started)

    async def discard(self, state: _State, session: AsyncSession) -> None:
        """Throw away one attempt's writes before a retry."""
        if state.data is not None:
            await state.savepoint.rollback()
        else:
            await session.rollback()
            await session.close()

    async def complete_per_run(
        self, state: _State, lease: Lease, agent: Any, result: AgentResult, started: float
    ) -> str:
        """``per_run``: keep or drop this step's savepoint; journal and the run
        row are written outside the run's transaction (observability)."""
        run_id = state.run.id
        latency = round((time.monotonic() - started) * 1000, 2)
        if result.suspend:
            await state.savepoint.rollback()
            state.last = result
            await self.end_with_error(
                state,
                lease,
                "not_supported",
                f"'{agent.id}' waits for a human, which transaction: per_run cannot do",
                agent_id=agent.id,
            )
            return "stop"
        outputs: dict[str, Any] = {}
        normal = not is_reserved(result.signal)
        if normal:
            try:
                outputs = (
                    self.model(state, agent, result.signal)
                    .model_validate(result.outputs)
                    .model_dump(by_alias=True)
                )
            except ValidationError as exc:
                result = AgentResult(
                    "error",
                    error_type="contract_violation",
                    message=f"outputs for '{result.signal}': {_brief(exc)}",
                    tokens=result.tokens,
                )
                normal = False
        if normal:
            await state.savepoint.commit()
        else:
            await state.savepoint.rollback()
        state.last = result
        target = agent.routes.get(result.signal)
        completion = {
            "signal": result.signal,
            "error_type": result.error_type,
            "message": result.message,
        }
        tokens = state.tokens + result.tokens
        async with AsyncSession(self.engine) as s:
            allocated = await self.claim_seqs(s, state, lease, 1, tokens, target)
            if allocated is None:
                await s.rollback()
                lease.lost.set()
                return "stop"
            await journal.append_event(
                s,
                run_id,
                "agent_completed",
                agent_id=agent.id,
                payload=completion,
                tokens=result.tokens,
                latency_ms=latency,
                seq=allocated,
                workflow=state.run.workflow,
            )
            await s.commit()
        if target is None:
            await self.end_with_error(
                state,
                lease,
                result.error_type or "unrouted_signal",
                result.message or f"'{agent.id}' emitted '{result.signal}', which is not routed",
                agent_id=agent.id,
            )
            return "stop"
        if normal:
            state.context = {**state.context, **outputs}
            state.completed = [*state.completed, agent.id]
        state.next, state.step, state.tokens = target, state.step + 1, tokens
        limit = state.doc.spec.policy.max_tokens_per_run
        if limit is not None and tokens > limit:
            await self.end_with_error(
                state,
                lease,
                "policy_violation",
                f"used {tokens} tokens, over policy.max_tokens_per_run {limit}",
                agent_id=agent.id,
            )
            return "stop"
        return "continue"

    async def complete(
        self,
        state: _State,
        lease: Lease,
        agent: Any,
        result: AgentResult,
        session: AsyncSession,
        started: float,
    ) -> str:
        if state.data is not None:
            return await self.complete_per_run(state, lease, agent, result, started)
        run_id = state.run.id
        latency = round((time.monotonic() - started) * 1000, 2)
        state.last = result
        async with session:
            if result.suspend:
                # Keep the engine's writes (the approval request) with the checkpoint.
                await journal.write_checkpoint(
                    session,
                    run_id,
                    agent_id=agent.id,
                    signal=SUSPEND,
                    next=agent.id,
                    context=state.context,
                    completed=state.completed,
                )
                if not await queue.release(
                    session, run_id, lease.owner, result.suspend, current_agent=agent.id
                ):
                    await session.rollback()
                    lease.lost.set()
                    return "stop"
                await session.commit()
                return "stop"

            normal = not is_reserved(result.signal)
            outputs: dict[str, Any] = {}
            if normal:
                try:
                    outputs = (
                        self.model(state, agent, result.signal)
                        .model_validate(result.outputs)
                        .model_dump(by_alias=True)
                    )
                except ValidationError as exc:
                    result = AgentResult(
                        "error",
                        error_type="contract_violation",
                        message=f"outputs for '{result.signal}': {_brief(exc)}",
                        tokens=result.tokens,
                    )
                    normal = False
            if not normal:
                await session.rollback()  # an agent's writes commit only with a normal outcome

            state.last = result
            target = agent.routes.get(result.signal)
            completion = {
                "signal": result.signal,
                "error_type": result.error_type,
                "message": result.message,
                **({"detail": result.detail} if result.detail else {}),
            }
            if target is None:
                await session.rollback()
                async with AsyncSession(self.engine) as s:
                    await journal.append_event(
                        s,
                        run_id,
                        "agent_completed",
                        agent_id=agent.id,
                        payload=completion,
                        tokens=result.tokens,
                        latency_ms=latency,
                        workflow=state.run.workflow,
                    )
                    await s.commit()
                error_type = result.error_type or "unrouted_signal"
                message = (
                    result.message or f"'{agent.id}' emitted '{result.signal}', which is not routed"
                )
                await self.end_with_error(state, lease, error_type, message, agent_id=agent.id)
                return "stop"

            tokens = state.tokens + result.tokens
            context = {**state.context, **outputs} if normal else state.context
            completed = [*state.completed, agent.id] if normal else state.completed
            # The lease fence, the run-row update and both sequence numbers in one statement.
            first = await self.claim_seqs(session, state, lease, 2, tokens, target)
            if first is None:
                await session.rollback()
                lease.lost.set()
                return "stop"
            await journal.append_event(
                session,
                run_id,
                "agent_completed",
                agent_id=agent.id,
                payload=completion,
                tokens=result.tokens,
                latency_ms=latency,
                seq=first,
                workflow=state.run.workflow,
            )
            await journal.write_checkpoint(
                session,
                run_id,
                agent_id=agent.id,
                signal=result.signal,
                next=target,
                context=context,
                completed=completed,
                seq=first + 1,
            )
            await session.commit()

        state.context, state.completed, state.next, state.step, state.tokens = (
            context,
            completed,
            target,
            state.step + 1,
            tokens,
        )
        limit = state.doc.spec.policy.max_tokens_per_run
        if limit is not None and tokens > limit:
            await self.end_with_error(
                state,
                lease,
                "policy_violation",
                f"used {tokens} tokens, over policy.max_tokens_per_run {limit}",
                agent_id=agent.id,
            )
            return "stop"
        return "continue"

    async def claim_seqs(
        self,
        session: AsyncSession,
        state: _State,
        lease: Lease,
        count: int,
        tokens: int,
        target: str | None,
    ) -> int | None:
        """Take *count* sequence numbers and record the step on the run row, only
        while *lease* still owns the running run. ``None`` means the lease is lost."""
        allocated = await journal.allocate_seqs(
            session,
            state.run.id,
            count,
            where=(_RUNS.c.lease_owner == lease.owner, _RUNS.c.status == "running"),
            values={"tokens_used": tokens, "current_agent": target},
        )
        return None if allocated is None else allocated[0]

    # ── control, ends, failure ───────────────────────────────────────────────

    def dev_pause_possible(self, state: _State) -> bool:
        """Whether :meth:`dev_pause` could pause before the next agent. When it cannot,
        the operator-control check is folded into :meth:`start_agent`."""
        from tuvl.runtime import devtools

        return state.data is None and devtools.enabled()

    async def honour_control(self, state: _State, lease: Lease) -> bool:
        async with AsyncSession(self.engine) as s:
            row = (
                await queue._execute(
                    s, sa.select(_RUNS.c.control).where(_RUNS.c.id == state.run.id)
                )
            ).first()
            control = row[0] if row else None
            if not control:
                return False
            action = control.get("action")
            if action == "steer":
                return False  # for the next loop agent to pick up
            await queue._execute(
                s, sa.update(_RUNS).where(_RUNS.c.id == state.run.id).values(control=None)
            )
            if action == "pause" and state.data is not None:
                # A per_run transaction cannot outlive this worker; it runs on.
                logger.warning("runtime.pause_ignored", run_id=str(state.run.id), reason="per_run")
                await s.commit()
                return False
            if action == "pause":
                if await queue.release(
                    s, state.run.id, lease.owner, "paused", current_agent=state.next
                ):
                    await journal.append_event(s, state.run.id, "paused", agent_id=state.next)
                await s.commit()
                return True
            await s.commit()
        if action == "abort":
            await self.end_with_error(
                state, lease, "aborted", "aborted by an operator", status="aborted"
            )
            return True
        return False

    async def dev_pause(self, state: _State, lease: Lease) -> bool:
        """Dev-mode breakpoints and step mode: pause before the next agent."""
        from tuvl.runtime import devtools

        if state.data is not None:  # a per_run transaction cannot pause
            return False
        reason = devtools.pause_reason(
            state.run.id, state.doc.metadata.name, state.next, state.step
        )
        if reason is None:
            return False
        async with AsyncSession(self.engine) as s:
            if await queue.release(
                s, state.run.id, lease.owner, "paused", current_agent=state.next
            ):
                await journal.append_event(
                    s, state.run.id, "paused", agent_id=state.next, payload={"reason": reason}
                )
            await s.commit()
        return True

    async def finish(self, state: _State, lease: Lease, end: str) -> None:
        spec = state.doc.spec.outputs.get(end)
        status_code, body = (spec.status, render(spec.body, state.context)) if spec else (200, None)
        output = {"status": status_code, "body": body}
        status = "completed"
        rollback = spec is not None and spec.rollback
        if state.data is not None:
            if rollback:
                await state.data.rollback()
                status = "compensated"
            else:
                # The run's writes, its completion and run_completed commit together.
                if await queue.release(
                    state.data,
                    state.run.id,
                    lease.owner,
                    status,
                    output=output,
                    end_name=end,
                    current_agent=None,
                ):
                    await journal.append_event(
                        state.data,
                        state.run.id,
                        "run_completed",
                        payload={"end": end, "status": status_code},
                        workflow=state.run.workflow,
                    )
                    await state.data.commit()
                else:
                    await state.data.rollback()
                    lease.lost.set()
                return
        elif rollback:
            status = await self.compensate(state, lease) or "completed"
        async with AsyncSession(self.engine) as s:
            if await queue.release(
                s,
                state.run.id,
                lease.owner,
                status,
                output=output,
                end_name=end,
                current_agent=None,
            ):
                await journal.append_event(
                    s,
                    state.run.id,
                    "run_completed",
                    payload={"end": end, "status": status_code},
                    workflow=state.run.workflow,
                )
            await s.commit()

    async def end_with_error(
        self,
        state: _State,
        lease: Lease,
        error_type: str,
        message: str,
        *,
        agent_id: str | None = None,
        status: str = "failed",
    ) -> None:
        """End the run on an error or abort, compensating completed agents.
        A failed run whose compensation succeeded ends ``compensated``."""
        error: dict[str, Any] = {"error_type": error_type, "message": message, "agent_id": agent_id}
        if state.data is not None:
            await state.data.rollback()  # per_run: undoing is discarding the transaction
            outcome = None
        else:
            outcome = await self.compensate(state, lease)
        final = status
        if outcome == "failed":
            final, error = "failed", {**error, "compensation": "failed"}
        elif outcome == "compensated" and status == "failed":
            final = "compensated"
        async with AsyncSession(self.engine) as s:
            if await queue.release(
                s, state.run.id, lease.owner, final, error=error, current_agent=agent_id
            ):
                kind = "run_aborted" if status == "aborted" else "run_failed"
                await journal.append_event(
                    s,
                    state.run.id,
                    kind,
                    agent_id=agent_id,
                    payload=error,
                    workflow=state.run.workflow,
                )
            await s.commit()

    async def fail(
        self, s: AsyncSession, run_id: uuid.UUID, lease: Lease, error_type: str, message: str
    ) -> None:
        error = {"error_type": error_type, "message": message, "agent_id": None}
        if await queue.release(s, run_id, lease.owner, "failed", error=error):
            await journal.append_event(s, run_id, "run_failed", payload=error)

    # ── compensation (spec §14.7) ────────────────────────────────────────────

    async def compensate(self, state: _State, lease: Lease) -> str | None:
        """Run the compensators of completed agents, newest first. Returns
        ``None`` when nothing needed compensating."""
        by_id = {a.id: a for a in state.doc.spec.agents}
        steps = []
        for agent_id in reversed(state.completed):
            compensator = by_id[agent_id].compensate
            if compensator is not None:
                steps.append((agent_id, by_id[compensator]))
        if not steps:
            return None
        async with AsyncSession(self.engine) as s:
            await queue._execute(
                s,
                sa.update(_RUNS)
                .where(_RUNS.c.id == state.run.id, _RUNS.c.lease_owner == lease.owner)
                .values(status="compensating"),
            )
            await s.commit()
        ok = True
        for original, comp in steps:
            async with AsyncSession(self.engine, expire_on_commit=False) as s:
                await journal.append_event(
                    s,
                    state.run.id,
                    "compensation_started",
                    agent_id=comp.id,
                    payload={"for": original},
                )
                await s.commit()
            view = {k: state.context.get(k) for k in comp.inputs}
            result, session = await self.call_engine(state, comp, view, f"compensate:{original}")
            succeeded = not is_reserved(result.signal)
            async with session:
                if succeeded:
                    await session.commit()
                else:
                    await session.rollback()
                    ok = False
            async with AsyncSession(self.engine) as s:
                await journal.append_event(
                    s,
                    state.run.id,
                    "compensation_completed",
                    agent_id=comp.id,
                    payload={
                        "for": original,
                        "ok": succeeded,
                        "error_type": result.error_type,
                        "message": result.message,
                    },
                )
                await queue._execute(
                    s,
                    sa.update(_RUNS)
                    .where(_RUNS.c.id == state.run.id, _RUNS.c.lease_owner == lease.owner)
                    .values(status="running"),
                )
                await s.commit()
        return "compensated" if ok else "failed"

    async def depth_of(self, run: SystemRun, s: AsyncSession) -> int:
        """How many parent runs *run* has (workflow-as-tool nesting)."""
        depth, parent = 0, run.parent_run_id
        while parent is not None and depth < 10:
            row = await s.get(SystemRun, parent)
            depth, parent = depth + 1, (row.parent_run_id if row else None)
        return depth

    async def run_child(
        self,
        parent: uuid.UUID,
        workflow: str,
        payload: dict[str, Any],
        principal: str | None,
        *,
        key: str | None = None,
    ) -> SystemRun:
        """Run *workflow* as a child of *parent*, inline, to its next stopping
        point (an end, or a wait for a person). Raises ``ValueError`` for an
        unknown workflow or invalid input.

        With a *key* the child is idempotent: a parent step that runs again (it was
        reclaimed after a crash) finds the child it already started under that key and
        follows it, instead of starting a second one."""
        doc = self.registry().get(workflow)
        if doc is None or not doc.enabled:
            raise ValueError(f"workflow '{workflow}' is not loaded")
        from tuvl.runtime.host import unavailable_reasons

        if reasons := unavailable_reasons(doc):
            raise ValueError(f"workflow '{workflow}' is unavailable: {'; '.join(reasons)}")
        model = contract_model(
            doc, f"{workflow}.input", field_types(doc.spec.trigger.input), self.resolver
        )
        try:
            clean = model.model_validate(payload).model_dump(mode="json", by_alias=True)
        except ValidationError as exc:
            raise ValueError(_brief(exc)) from exc
        trigger = f"child:{key}" if key else "tool"
        async with AsyncSession(self.engine) as s:
            existing = None
            if key:
                existing = (
                    await s.execute(
                        sa.select(SystemRun.id).where(  # type: ignore[call-overload]
                            SystemRun.parent_run_id == parent, SystemRun.trigger == trigger
                        )
                    )
                ).scalar_one_or_none()
            child = existing or await queue.enqueue(
                s,
                workflow=workflow,
                workflow_hash=workflow_hash(doc),
                trigger=trigger,
                input=clean,
                principal=principal,
                parent_run_id=parent,
            )
            await s.commit()
        worker = queue.Worker(
            self.engine, self, owner=f"child:{uuid.uuid4().hex[:8]}", concurrency=1
        )
        await queue.run_inline(worker, child, timeout_seconds=24 * 3600)
        return await self._settled(child)

    async def _settled(self, run_id: uuid.UUID, poll: float = 0.25) -> SystemRun:
        """*run_id* once it has ended or is waiting for a person. A run another worker still holds
        (a re-run parent found its child mid-flight) is followed until it gets there."""
        deadline = asyncio.get_running_loop().time() + 24 * 3600
        while True:
            async with AsyncSession(self.engine, expire_on_commit=False) as s:
                run = await s.get(SystemRun, run_id)
            assert run is not None
            if (
                run.status in queue.TERMINAL_STATUSES
                or run.status.startswith("waiting_")
                or run.status == "paused"
                or asyncio.get_running_loop().time() > deadline
            ):
                return run
            await asyncio.sleep(poll)

    async def call_engine(
        self, state: _State, agent: Any, view: dict[str, Any], key: str
    ) -> tuple[AgentResult, AsyncSession]:
        """Run *agent*'s engine once in a fresh session (returned uncommitted),
        or — under ``per_run`` — in a savepoint of the run's session."""
        if state.data is not None:
            session = state.data
            state.savepoint = await session.begin_nested()
        else:
            session = AsyncSession(self.engine, expire_on_commit=False)
        run_id = state.run.id

        async def emit(type: str, **kw: Any) -> int:
            async with AsyncSession(self.engine) as s:
                seq = await journal.append_event(s, run_id, type, agent_id=agent.id, **kw)
                await s.commit()
                return seq

        ctx = EngineContext(
            run_id=run_id,
            workflow=state.doc,
            session=session,
            principal=state.run.principal,
            idempotency_key=f"{run_id}:{agent.id}:{key}",
            resolver=self.resolver,
            emit=emit,
            run_child=self.run_child,
            depth=state.depth,
            workflows=self.registry(),
            stream=deltas.DeltaStream(self.engine, run_id, agent.id),
        )
        from tuvl.runtime.judges import JUDGE_TOKENS

        judge_tokens = [0]
        JUDGE_TOKENS.set(judge_tokens)
        engine_fn = ENGINES.get(agent.engine)
        if engine_fn is None:
            return AgentResult(
                "error",
                error_type="engine_unavailable",
                message=f"no '{agent.engine}' engine is installed",
            ), session
        # decide bounds its own model call (a slow model is decision_model_unavailable).
        timeout = (
            duration_seconds(agent.budget.timeout)
            if agent.budget.timeout and agent.engine != "decide"
            else None
        )
        try:
            result = await asyncio.wait_for(engine_fn(agent, view, ctx), timeout)
        except TimeoutError:
            result = AgentResult(
                "timeout" if "timeout" in agent.routes else "error",
                error_type="timeout",
                message=f"no result within {agent.budget.timeout}",
            )
        except EngineError as exc:
            result = AgentResult("error", error_type=exc.error_type, message=str(exc))
        except Exception as exc:
            logger.exception("runtime.engine.raised", agent=agent.id, engine=agent.engine)
            kind = "code_exception" if agent.engine == "code" else f"{agent.engine}_error"
            result = AgentResult("error", error_type=kind, message=f"{type(exc).__name__}: {exc}")
        finally:
            if ctx.stream is not None:
                await ctx.stream.close()
        if judge_tokens[0]:
            result.tokens += judge_tokens[0]
        return result, session


def _retry_kind(result: AgentResult) -> str | None:
    if result.signal in ("timeout", "parse_error"):
        return result.signal
    return result.error_type if result.error_type in _RETRYABLE else None


def _brief(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
        for e in exc.errors(include_url=False)[:5]
    )


def render(body: Any, context: Mapping[str, Any]) -> Any:
    """Render ``{{ }}`` templates in an end's body against the run context."""
    if isinstance(body, str):
        return compile_template(body).render(context) if "{{" in body else body
    if isinstance(body, dict):
        return {k: render(v, context) for k, v in body.items()}
    if isinstance(body, list):
        return [render(v, context) for v in body]
    return body
