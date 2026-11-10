"""Test 2.0 runner (spec §18): run ``kind: Test`` documents through the real
runner, offline by default.

Each test runs in a throwaway in-memory database (runtime tables plus the
project's model tables), so code agents and ``ctx.db`` work without touching
a real datasource. An agent can be **mocked** (its outputs are still checked
against the contract, so a stale mock fails) or **replayed** from a pinned
journal. Unmocked ``llm``, ``loop``, ``http``/``mcp`` tool and model-backed
``decide`` agents need ``--live`` (``db`` tools run against the test database); offline they fail with a pointer. An unmocked
``human`` agent suspends the run, which ``expect.wait`` asserts.

``expect`` is evaluated in order: structure (``end``, ``path``,
``decisions``) → ``output`` (partial match) → ``assertions`` (TEL) →
``judge`` (cached verdicts; uncertain fails unless allowed).
"""

from __future__ import annotations

import asyncio
import json
import weakref
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from tuvl.contract.schema import TestDocument, WorkflowDocument
from tuvl.contract.signals import is_reserved
from tuvl.contract.tel import TELError, compile_expr, compile_template
from tuvl.runtime import decision, journal, queue
from tuvl.runtime.engines import AgentResult
from tuvl.runtime.models import RUNTIME_TABLES, SystemRun
from tuvl.runtime.runner import Runner, RunState, workflow_hash

OFFLINE = "offline"
_NEEDS_LIVE = ("llm", "loop", "tool")


# The test database is SQLite; render Postgres-only column types portably.
@compiles(JSONB, "sqlite")
def _jsonb(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


@compiles(TSVECTOR, "sqlite")
def _tsvector(type_: Any, compiler: Any, **kw: Any) -> str:
    return "TEXT"


@compiles(PG_UUID, "sqlite")
def _uuid(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(36)"


@dataclass
class TestResult:
    __test__ = False

    name: str
    workflow: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    end: str | None = None
    path: list[str] = field(default_factory=list)
    tokens: int = 0
    judge_calls: int = 0
    waiting_at: str | None = None  # the agent a suspended run stopped at
    #: ``pending`` agents the run went through without a mock: placeholders, not logic.
    stubbed: list[str] = field(default_factory=list)
    #: T001: deterministic agents the test mocks instead of exercising.
    warnings: list[str] = field(default_factory=list)
    #: compensators that ran (and succeeded) on a rollback end
    compensated: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """``failed``, ``stubbed`` (expectations met, but through placeholders) or ``passed``."""
        if not self.passed:
            return "failed"
        return "stubbed" if self.stubbed else "passed"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "workflow": self.workflow,
            "status": self.status,
            "passed": self.passed,
            "failures": self.failures,
            "end": self.end,
            "path": self.path,
            "tokens": self.tokens,
            "stubbed": self.stubbed,
            "warnings": self.warnings,
            "compensated": self.compensated,
        }


@dataclass
class Canned:
    """A prepared result for one agent invocation (mock or replay)."""

    signal: str
    outputs: dict[str, Any]


class TestRunner(Runner):
    """A :class:`Runner` that serves mocks/replays and blocks live calls offline."""

    __test__ = False

    def __init__(
        self,
        engine: AsyncEngine,
        workflows: Mapping[str, WorkflowDocument],
        canned: dict[str, list[Canned]],
        *,
        live: bool,
    ) -> None:
        super().__init__(engine, workflows)
        self.canned = canned
        self.live = live

    async def dev_pause(self, state: RunState, lease: queue.Lease) -> bool:
        return False  # Insight breakpoints and step mode never apply to tests or replays

    async def call_engine(
        self, state: RunState, agent: Any, view: dict[str, Any], key: str
    ) -> tuple[AgentResult, AsyncSession]:
        queue_ = self.canned.get(agent.id)
        if queue_:
            served = queue_.pop(0) if len(queue_) > 1 else queue_[0]
            return AgentResult(
                served.signal, outputs=dict(served.outputs), detail={"mocked": True}
            ), AsyncSession(self.engine, expire_on_commit=False)
        if agent.engine == "parallel" and not self.live:
            return self._parallel_from_mocks(agent), AsyncSession(
                self.engine, expire_on_commit=False
            )
        local_db = agent.engine == "tool" and agent.tool.use == "db"  # the test's own database
        if not self.live and agent.engine in _NEEDS_LIVE and not local_db:
            return self._offline(agent), AsyncSession(self.engine, expire_on_commit=False)
        if agent.engine == "human":  # nobody answers in a test: the run stops here
            return AgentResult("default", suspend="waiting_human"), AsyncSession(self.engine)
        return await super().call_engine(state, agent, view, key)

    def _parallel_from_mocks(self, agent: Any) -> AgentResult:
        """Offline, a parallel step runs only when every branch is mocked; the branches' mocks are joined."""
        missing = [b.id for b in agent.parallel.branches if not self.canned.get(b.id)]
        if missing:
            return AgentResult(
                "error",
                error_type=OFFLINE,
                message=f"'{agent.id}' (parallel) has branches that are not mocked: add mocks.{missing[0]}"
                + (f" (and {', '.join(missing[1:])})" if missing[1:] else "")
                + ", or run with --live",
            )
        outputs: dict[str, Any] = {}
        for branch in agent.parallel.branches:
            queue_ = self.canned[branch.id]
            served = queue_.pop(0) if len(queue_) > 1 else queue_[0]
            if served.signal != "default":
                return AgentResult(
                    "error",
                    error_type="parallel_branch_failed",
                    message=f"branch '{branch.id}' is mocked to end on '{served.signal}'",
                    detail={"mocked": True},
                )
            outputs.update(served.outputs)
        return AgentResult("default", outputs=outputs, detail={"mocked": True})

    @staticmethod
    def _offline(agent: Any) -> AgentResult:
        return AgentResult(
            "error",
            error_type=OFFLINE,
            message=f"'{agent.id}' ({agent.engine}) is not mocked — add mocks.{agent.id}, a replay:, or run with --live",
        )


def _default_signal(agent: Any) -> str:
    normal = [s for s in agent.routes if not is_reserved(s)]
    return "default" if "default" in normal or not normal else normal[0]


def _reachable_agents(
    doc: WorkflowDocument, workflows: Mapping[str, WorkflowDocument] | None
) -> dict[str, Any]:
    """The agents of *doc*, then those of the workflows it maps over or calls as loop tools (a
    mock can target any of them). An id in *doc* wins over the same id further down."""
    from tuvl.contract.schema import LoopAgent, MapAgent, ParallelAgent, WorkflowToolRef

    found: dict[str, Any] = {}
    seen: set[str] = set()
    queue_ = [doc]
    while queue_:
        current = queue_.pop(0)
        if current.metadata.name in seen:
            continue
        seen.add(current.metadata.name)
        for agent in current.spec.agents:
            found.setdefault(agent.id, agent)
            if isinstance(agent, ParallelAgent):
                for branch in agent.parallel.branches:
                    found.setdefault(branch.id, branch)
            names: list[str] = []
            if isinstance(agent, MapAgent):
                names.append(agent.map.workflow)
            elif isinstance(agent, LoopAgent):
                names += [t.workflow for t in agent.loop.tools if isinstance(t, WorkflowToolRef)]
            queue_ += [workflows[n] for n in names if workflows and n in workflows]
    return found


def canned_results(
    doc: WorkflowDocument,
    test: TestDocument,
    base: Path,
    workflows: Mapping[str, WorkflowDocument] | None = None,
) -> dict[str, list[Canned]]:
    """Mocks first; agents not mocked take their results from the replay."""
    by_id = _reachable_agents(doc, workflows)
    canned: dict[str, list[Canned]] = defaultdict(list)
    if test.spec.replay:
        data = json.loads((base / test.spec.replay).read_text())
        for cp in data.get("checkpoints", []):
            if cp.get("signal") == "suspend":
                continue
            context = cp.get("context") or {}
            agent = by_id.get(cp["agent_id"])
            if agent is None:
                continue
            declared = agent.outputs_for(cp["signal"]) if not is_reserved(cp["signal"]) else {}
            outputs = {k: context[k] for k in declared if k in context}
            canned[agent.id].append(Canned(cp["signal"], outputs))
    for agent_id, mock in test.spec.mocks.items():
        agent = by_id.get(agent_id)
        if agent is None:
            continue
        canned[agent_id] = [Canned(mock.signal or _default_signal(agent), dict(mock.outputs or {}))]
    return dict(canned)


def _matches(expected: Any, actual: Any, where: str = "output") -> list[str]:
    """Partial match: every key in *expected* must match; extra keys are fine."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{where}: expected an object, got {actual!r}"]
        problems: list[str] = []
        for key, value in expected.items():
            if key not in actual:
                problems.append(f"{where}.{key}: missing")
            else:
                problems += _matches(value, actual[key], f"{where}.{key}")
        return problems
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [f"{where}: expected {expected!r}, got {actual!r}"]
        return [
            p
            for i, (e, a) in enumerate(zip(expected, actual, strict=True))
            for p in _matches(e, a, f"{where}[{i}]")
        ]
    if expected != actual and not (
        isinstance(expected, int | float)
        and isinstance(actual, int | float)
        and float(expected) == float(actual)
    ):
        return [f"{where}: expected {expected!r}, got {actual!r}"]
    return []


def _no_savepoint(connection: Any, name: Any) -> None:
    return None


async def _database(models: Mapping[str, type[SQLModel]]) -> AsyncEngine:
    """One in-memory connection that every session shares, in autocommit.

    Concurrent sessions (map items, parallel branches, batched loop tools) cannot each get a
    transaction of their own: SQLite has one writer, and the runtime keeps an agent's session open
    while other sessions journal, so per-connection transactions deadlock. On a shared connection a
    transaction would be everyone's, and one session's rollback would undo another's half-written
    journal entry. Autocommit makes every statement final on its own; rollbacks and savepoints
    become no-ops, so an agent's writes are kept even when prod would discard them.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        isolation_level="AUTOCOMMIT",
    )
    # SAVEPOINT outside a transaction would open one on the shared connection.
    dialect = engine.sync_engine.dialect
    dialect.do_savepoint = _no_savepoint  # type: ignore[method-assign]
    dialect.do_release_savepoint = _no_savepoint  # type: ignore[method-assign]
    dialect.do_rollback_to_savepoint = _no_savepoint  # type: ignore[method-assign]
    classes: list[Any] = [*RUNTIME_TABLES, *models.values()]
    tables = [cls.__table__ for cls in classes]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=tables))
    return engine


def _fault_only(mock: Any) -> bool:
    """A mock that only injects a failure (fault injection), replacing no logic."""
    return mock.signal == "error" and not mock.outputs


def _deterministic(agent: Any) -> bool:
    """Business logic a test must exercise rather than mock."""
    if agent.engine == "code":
        return True
    if agent.engine == "tool":
        return bool(agent.tool.use == "db")
    if agent.engine == "decide":
        return agent.decide.model is None
    return False


async def _seed(
    engine: AsyncEngine,
    given: Mapping[str, list[dict[str, Any]]],
    tables: Mapping[str, type[SQLModel]],
) -> list[str]:
    """Insert the test's ``given:`` rows; the failures, if any."""
    problems: list[str] = []
    async with AsyncSession(engine) as s:
        for model, rows in given.items():
            cls = tables.get(model)
            if cls is None:
                problems.append(f"given.{model}: model is not loaded")
                continue
            for i, row in enumerate(rows):
                try:
                    s.add(cls.model_validate(row))
                except Exception as exc:
                    problems.append(f"given.{model}[{i}]: {' '.join(str(exc).split())[:200]}")
        if not problems:
            await s.commit()
    return problems


_RUN_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)
_HOLDING: ContextVar[bool] = ContextVar("tuvl_testing_exclusive", default=False)


@asynccontextmanager
async def exclusive() -> AsyncIterator[None]:
    """One test run or project reload at a time per event loop (re-entrant).

    A test run swaps process-wide state (decision providers) and reads the model and workflow
    registries, which a project reload clears — the dev server does both on concurrent requests.
    """
    if _HOLDING.get():
        yield
        return
    loop = asyncio.get_running_loop()
    lock = _RUN_LOCKS.get(loop)
    if lock is None:
        lock = _RUN_LOCKS[loop] = asyncio.Lock()
    async with lock:
        token = _HOLDING.set(True)
        try:
            yield
        finally:
            _HOLDING.reset(token)


async def run_test(
    test: TestDocument,
    workflows: Mapping[str, WorkflowDocument],
    *,
    base: Path,
    live: bool = False,
    allow_uncertain: bool = False,
    models: Mapping[str, type[SQLModel]] | None = None,
    live_agents: frozenset[str] = frozenset(),
) -> TestResult:
    """Run one test document and evaluate its expectations. Agents in
    *live_agents* are never served from mocks or the replay: they execute
    (an engine change is checked this way against a pinned run)."""
    async with exclusive():
        return await _run_test(
            test,
            workflows,
            base=base,
            live=live,
            allow_uncertain=allow_uncertain,
            models=models,
            live_agents=live_agents,
        )


async def _run_test(
    test: TestDocument,
    workflows: Mapping[str, WorkflowDocument],
    *,
    base: Path,
    live: bool = False,
    allow_uncertain: bool = False,
    models: Mapping[str, type[SQLModel]] | None = None,
    live_agents: frozenset[str] = frozenset(),
) -> TestResult:
    from tuvl.core.models.loader import MODEL_REGISTRY

    spec = test.spec
    result = TestResult(test.metadata.name, spec.workflow, passed=False)
    doc = workflows.get(spec.workflow)
    if doc is None:
        result.failures.append(f"workflow '{spec.workflow}' is not loaded")
        return result
    try:
        canned = {
            k: v
            for k, v in canned_results(doc, test, base, workflows).items()
            if k not in live_agents
        }
    except (OSError, ValueError, KeyError) as exc:
        result.failures.append(f"replay {spec.replay}: {exc}")
        return result

    tables = models if models is not None else MODEL_REGISTRY
    by_id = {a.id: a for a in doc.spec.agents}
    result.warnings = [
        f"T001 mocks deterministic agent '{agent_id}' ({by_id[agent_id].engine}) — "
        "seed rows with given: and the caller with as: instead"
        for agent_id, mock in spec.mocks.items()
        if agent_id in by_id and _deterministic(by_id[agent_id]) and not _fault_only(mock)
    ]
    engine = await _database(tables)
    seeded = await _seed(engine, spec.given, tables)
    if seeded:
        result.failures += seeded
        await engine.dispose()
        return result
    saved = dict(decision.PROVIDERS)
    if not live:

        async def offline(request: decision.DecisionRequest) -> decision.Decision:
            raise decision.ProviderUnavailableError(
                f"{OFFLINE}: decision model '{request.model_ref}' — mock the decide agent or run with --live"
            )

        for name in list(decision.PROVIDERS):
            decision.PROVIDERS[name] = offline
    try:
        async with AsyncSession(engine) as s:
            run_id = await queue.enqueue(
                s,
                workflow=doc.metadata.name,
                workflow_hash=workflow_hash(doc),
                trigger="test",
                input=dict(spec.input),
                principal=spec.caller.user if spec.caller else None,
            )
            await s.commit()
        worker = queue.Worker(
            engine, TestRunner(engine, workflows, canned, live=live), owner="test"
        )
        while await worker.claim_some():
            await worker.drain()
        async with AsyncSession(engine) as s:
            run = await s.get(SystemRun, run_id)
            events = await journal.events(s, run_id)
            last = await journal.latest_checkpoint(s, run_id)
    finally:
        decision.PROVIDERS.clear()
        decision.PROVIDERS.update(saved)
        await engine.dispose()
    assert run is not None
    result.end, result.tokens = run.end_name, run.tokens_used
    result.path = [e.agent_id for e in events if e.type == "agent_completed" and e.agent_id]
    result.compensated = [
        e.agent_id
        for e in events
        if e.type == "compensation_completed" and e.agent_id and (e.payload or {}).get("ok")
    ]
    result.stubbed = list(
        dict.fromkeys(
            a
            for a in result.path
            if a in by_id and by_id[a].engine == "pending" and a not in canned
        )
    )
    if run.status in ("waiting_human", "waiting_approval"):
        result.waiting_at = run.current_agent
    offline_hits = [
        e.payload.get("message")
        for e in events
        if e.type == "agent_completed"
        and (
            e.payload.get("error_type") == OFFLINE or OFFLINE + ":" in str(e.payload.get("message"))
        )
    ]
    if offline_hits:
        result.failures += [str(m) for m in offline_hits]
        return result
    await _evaluate(
        result, test, run, events, (last.context if last else {}) or {}, allow_uncertain
    )
    cause = _failure_cause(events)
    if cause:
        result.failures = [
            f"{f} — {cause}" if f.startswith("end: expected") else f for f in result.failures
        ]
    result.passed = not result.failures
    return result


def _failure_cause(events: list[Any]) -> str | None:
    """The agent whose reserved signal (error, timeout, …) ended the run, and why."""
    for e in reversed(events):
        payload = e.payload or {}
        signal = payload.get("signal")
        if e.type == "agent_completed" and signal and signal != "default" and is_reserved(signal):
            why = payload.get("message") or payload.get("error_type")
            return f"`{e.agent_id}` emitted {signal}" + (f": {str(why)[:200]}" if why else "")
    return None


async def _evaluate(
    result: TestResult,
    test: TestDocument,
    run: SystemRun,
    events: list[Any],
    context: dict[str, Any],
    allow_uncertain: bool,
) -> None:
    from tuvl.runtime import judges

    expect = test.spec.expect
    failures = result.failures
    waiting = {"human": ("waiting_human",), "approval": ("waiting_human", "waiting_approval")}
    if expect.wait is not None:
        if run.status not in waiting[expect.wait]:
            failures.append(f"wait: expected the run to stop for {expect.wait}, got {run.status}")
    elif run.status in ("waiting_human", "waiting_approval"):
        failures.append(
            f"run stopped {run.status} at {run.current_agent} — mock it, or expect wait:"
        )
    elif run.status not in ("completed", "compensated"):
        failures.append(f"run ended {run.status}: {run.error}")
    if expect.end is not None and run.end_name != expect.end:
        failures.append(f"end: expected {expect.end!r}, got {run.end_name!r}")
    if expect.path is not None and result.path != list(expect.path):
        failures.append(f"path: expected {list(expect.path)}, got {result.path}")
    missing = [a for a in expect.path_includes if a not in result.path]
    if missing:
        failures.append(f"path_includes: {missing} never ran (path {result.path})")
    decided = {e.agent_id: e.payload for e in events if e.type == "decision"}
    for agent_id, want in expect.decisions.items():
        got = decided.get(agent_id)
        if got is None:
            failures.append(f"decisions.{agent_id}: no decision was made")
            continue
        if want.route is not None and got.get("decision") != want.route:
            failures.append(
                f"decisions.{agent_id}.route: expected {want.route!r}, got {got.get('decision')!r}"
            )
        if want.source is not None and got.get("source") != want.source:
            failures.append(
                f"decisions.{agent_id}.source: expected {want.source!r}, got {got.get('source')!r}"
            )
    if failures:
        return  # structure first: later checks would only repeat the same story
    output = run.output or {}
    if expect.output is not None:
        failures += _matches(expect.output, output)
    data = {"output": output, "end": run.end_name, **context}
    for text in expect.assertions:
        try:
            if compile_expr(text).evaluate(data) is not True:
                failures.append(f"assertion failed: {text}")
        except TELError as exc:
            failures.append(f"assertion {text!r}: {exc}")
    if failures:
        return
    for item in expect.judge:
        target = compile_template(item.target).render(data) if "{{" in item.target else item.target
        try:
            verdict = await judges.judge(
                item.judge, target, where=f"test '{test.metadata.name}' judge"
            )
        except judges.JudgeUnavailableError as exc:
            failures.append(f"judge {item.judge}: unavailable — {exc}")
            continue
        result.judge_calls += 0 if verdict.cached else 1
        if verdict.verdict == "fail" or (verdict.verdict == "uncertain" and not allow_uncertain):
            failures.append(f"judge {item.judge}: {verdict.verdict} — {'; '.join(verdict.reasons)}")
