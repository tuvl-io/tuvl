"""The ``code`` engine (spec §9.2): Python functions registered with ``@agent``.

.. code-block:: python

    # agents/issue_refund.py — one @agent per file, file name == run name
    from tuvl import agent, Ctx

    @agent("issue_refund")
    async def issue_refund(inp: IssueRefundIn, ctx: Ctx) -> IssueRefundOut:
        refund = await ctx.db.create("Refund", order_id=inp.order.id, amount=inp.order.total)
        return IssueRefundOut(refund=refund)

The function receives its inputs as one typed object — an instance of the
class its first parameter is annotated with, or a model compiled from the
contract — and returns its outputs (a model or a dict), a ``(outputs,
signal)`` pair, or a bare signal. ``ctx.db`` reaches only the workflow's
``spec.models`` and writes inside the agent's transaction, so the writes
commit with its checkpoint; with ``effect: read`` it refuses writes.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import typing
from collections.abc import Awaitable, Callable
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic import BaseModel
from sqlmodel import SQLModel

from tuvl.contract.facts import field_types
from tuvl.contract.types import build_model
from tuvl.runtime.engines import (
    AgentResult,
    EngineContext,
    EngineError,
    expand_env_params,
    register_engine,
    render_params,
)

logger = structlog.get_logger(__name__)

AgentFn = Callable[[Any, "Ctx"], Awaitable[Any]]
AGENT_REGISTRY: dict[str, AgentFn] = {}


def agent(name: str) -> Callable[[AgentFn], AgentFn]:
    """Register an async function as the ``code`` agent runner *name*."""

    def decorate(fn: AgentFn) -> AgentFn:
        if name.startswith("tuvl."):
            raise ValueError(f"@agent('{name}'): the 'tuvl.' prefix is reserved for built-ins")
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"@agent('{name}') must decorate an async function")
        existing = AGENT_REGISTRY.get(name)
        if existing is not None and existing.__qualname__ != fn.__qualname__:
            raise ValueError(f"@agent('{name}') is already registered by {existing.__module__}")
        AGENT_REGISTRY[name] = fn
        return fn

    return decorate


def load_agents(agents_dir: pathlib.Path | str) -> list[str]:
    """Import every ``agents/*.py`` (skipping ``_``-prefixed files and
    directories such as ``_generated``). The project root goes on
    ``sys.path`` so ``from agents._generated.schemas import …`` resolves."""
    directory = pathlib.Path(agents_dir)
    if not directory.is_dir():
        return []
    root = str(directory.parent.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    loaded: list[str] = []
    for py_file in sorted(directory.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        before = set(AGENT_REGISTRY)
        module_name = f"{directory.name}.{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        added = set(AGENT_REGISTRY) - before
        if added and py_file.stem not in added:
            logger.warning(
                "runtime.agent.file_name_mismatch", file=py_file.name, agents=sorted(added)
            )
        loaded += sorted(added)
    return loaded


# ── ctx ──────────────────────────────────────────────────────────────────────


class Db:
    """Model access for a code agent, inside its transaction."""

    def __init__(self, ctx: EngineContext, allowed: set[str], read_only: bool) -> None:
        self._session = ctx.session
        self._allowed = allowed
        self._read_only = read_only

    def _model(self, name: str, *, write: bool = False) -> type[SQLModel]:
        from tuvl.core.models.loader import MODEL_REGISTRY

        if name not in self._allowed:
            raise EngineError("permission_denied", f"model '{name}' is not in spec.models")
        if write and self._read_only:
            raise EngineError(
                "read_only", f"this agent declares effect: read and cannot write '{name}'"
            )
        cls = MODEL_REGISTRY.get(name)
        if cls is None:
            raise EngineError("db_error", f"model '{name}' is not loaded")
        return cls

    @staticmethod
    def _row(obj: SQLModel) -> dict[str, Any]:
        return obj.model_dump()

    async def get(self, model: str, id: Any) -> dict[str, Any] | None:
        obj = await self._session.get(self._model(model), id)
        return self._row(obj) if obj is not None else None

    async def list(self, model: str, *, limit: int = 100, **where: Any) -> list[dict[str, Any]]:
        cls = self._model(model)
        stmt = sa.select(cls).filter_by(**where).limit(limit)
        result = await self._session.execute(stmt)
        return [self._row(o) for o in result.scalars().all()]

    async def create(self, model: str, **values: Any) -> dict[str, Any]:
        obj = self._model(model, write=True)(**values)
        self._session.add(obj)
        await self._session.flush()
        await self._session.refresh(obj)
        return self._row(obj)

    async def update(self, model: str, id: Any, **values: Any) -> dict[str, Any] | None:
        obj = await self._session.get(self._model(model, write=True), id)
        if obj is None:
            return None
        for key, value in values.items():
            setattr(obj, key, value)
        await self._session.flush()
        return self._row(obj)

    async def delete(self, model: str, id: Any) -> bool:
        obj = await self._session.get(self._model(model, write=True), id)
        if obj is None:
            return False
        await self._session.delete(obj)
        await self._session.flush()
        return True


class Ctx:
    """The second argument of every ``@agent`` function."""

    def __init__(
        self, engine_ctx: EngineContext, agent_spec: Any, params: dict[str, Any] | None = None
    ) -> None:
        #: ``code.with``, rendered against the agent's inputs.
        self.params: dict[str, Any] = params or {}
        self.agent_id: str = agent_spec.id
        self._engine_ctx = engine_ctx
        allowed = {pin.partition("@")[0] for pin in engine_ctx.workflow.spec.models}
        self._read_only = agent_spec.effect == "read"
        self.db = Db(engine_ctx, allowed, read_only=self._read_only)
        self.run_id = engine_ctx.run_id
        self.principal = engine_ctx.principal
        self.idempotency_key = engine_ctx.idempotency_key
        self.log = logger.bind(run_id=str(engine_ctx.run_id), agent=agent_spec.id)
        self._emit = engine_ctx.emit

    @property
    def http(self) -> Any:
        from tuvl.core.infra.http_client import get_http_client

        return get_http_client()

    def storage(self, name: str) -> Any:
        """The named ``kind: Storage`` store; read-only inside a ``read`` agent."""
        from tuvl.storage import Store, get_store

        store = get_store(name)
        if self._read_only and not store.spec.readonly:
            return Store(
                store.name, store.spec.model_copy(update={"readonly": True}), store.backend
            )
        return store

    async def read_file(self, ref: Any, max_bytes: int | None = None) -> bytes:
        """The bytes of a ``file`` input (or any ``{store, key}`` record)."""
        from tuvl.contract.files import FileRef
        from tuvl.storage import get_store
        from tuvl.storage.errors import StorageError

        file = ref if isinstance(ref, FileRef) else FileRef.model_validate(ref)
        try:
            data, _ = await get_store(file.store).get(file.key, max_bytes)
        except StorageError as exc:
            raise EngineError("storage_error", str(exc)) from exc
        return data

    def artifact(self, ref: str) -> str:
        """Prose content of ``artifact://name[@version]``."""
        from tuvl.core.artifacts.registry import get_version, parse_ref

        name, version = parse_ref(ref)
        content = get_version(name, version, where=f"agent '{self.run_id}'").content
        return content if isinstance(content, str) else str(content)


# ── engine ───────────────────────────────────────────────────────────────────

_INPUT_MODELS: dict[tuple[str, str, str], type[BaseModel]] = {}


def _input_class(fn: AgentFn, agent_spec: Any, ctx: EngineContext) -> type[BaseModel]:
    params = list(inspect.signature(fn).parameters.values())
    if params:
        hints = typing.get_type_hints(fn)
        ann = hints.get(params[0].name)
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return ann
    # the contract is part of the key: an edited workflow must not reuse a stale input model
    key = (ctx.workflow.metadata.name, agent_spec.id, repr(agent_spec.inputs))
    if key not in _INPUT_MODELS:
        _INPUT_MODELS[key] = build_model(
            f"{agent_spec.id}.inputs", field_types(agent_spec.inputs), ctx.resolver
        )
    return _INPUT_MODELS[key]


def _as_outputs(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True)
    if isinstance(value, dict):
        return value
    raise EngineError(
        "contract_violation", f"a code agent returned {type(value).__name__}, not its outputs"
    )


async def code_engine(agent_spec: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    fn = AGENT_REGISTRY.get(agent_spec.code.run)
    if fn is None:
        raise EngineError("engine_unavailable", f"no @agent('{agent_spec.code.run}') is registered")
    inp = _input_class(fn, agent_spec, ctx).model_validate(view)
    params = render_params(expand_env_params(agent_spec.code.with_), view)
    returned = await fn(inp, Ctx(ctx, agent_spec, params))
    if isinstance(returned, str):
        return AgentResult(returned)
    if isinstance(returned, tuple):
        outputs, signal = returned
        return AgentResult(str(signal), outputs=_as_outputs(outputs))
    return AgentResult("default", outputs=_as_outputs(returned))


register_engine("code", code_engine)

from tuvl.runtime import builtins as _builtins  # noqa: E402,F401 — registers tuvl.* agents
