"""The engine interface the runner dispatches to (spec §9).

An engine receives the agent, the *view* (exactly its declared inputs) and an
:class:`EngineContext`, and returns an :class:`AgentResult`. Everything that
is the same for every engine — contracts, timeouts, retries, routing,
checkpoints, compensation — lives in the runner, not here.
"""

from __future__ import annotations

import datetime as _dt
import uuid
import weakref
from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tuvl.contract.schema import WorkflowDocument
from tuvl.contract.signals import is_reserved
from tuvl.contract.types import ModelResolver, TypeRef, build_model, parse_field


@dataclass
class AgentResult:
    """What an engine reports back.

    ``signal`` is a normal signal (with ``outputs``) or a reserved one (with
    ``error_type``/``message`` for ``error``). An engine that must wait for a
    human or an approval returns ``suspend`` instead of finishing.
    """

    signal: str
    outputs: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0
    error_type: str | None = None
    message: str | None = None
    suspend: Literal["waiting_human", "waiting_approval", "paused"] | None = None
    #: Engine-specific detail to journal (decision source, http status, …).
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineContext:
    run_id: uuid.UUID
    workflow: WorkflowDocument
    #: The primary-datasource session of this agent's transaction: writes made
    #: here commit together with the agent's checkpoint (``per_agent``).
    session: AsyncSession
    principal: str | None
    #: Stable across retries and crash re-execution of the same agent step.
    idempotency_key: str
    resolver: ModelResolver
    #: Journal an event for this agent (``llm_turn``, ``tool_call`` …).
    emit: Callable[..., Awaitable[int]]
    #: Runs a child workflow to its next stopping point (``workflow:`` loop
    #: tools); set by the runner.
    run_child: Callable[..., Awaitable[Any]] | None = None
    #: Nesting depth of this run (0 for a top-level run).
    depth: int = 0
    #: The loaded workflows (loop ``workflow:`` tools); ``None`` = the registry.
    workflows: Mapping[str, WorkflowDocument] | None = None
    #: Where an engine writes the draft of its answer while it is still being produced
    #: (a ``deltas.DeltaStream``); the run's event stream shows it live.
    stream: Any = None


Engine = Callable[[Any, dict[str, Any], EngineContext], Awaitable[AgentResult]]


def render_params(value: Any, view: Mapping[str, Any]) -> Any:
    """Render ``{{ }}`` templates in a parameter tree (``{{ x }}`` alone keeps
    its type)."""
    from tuvl.contract.tel import compile_template

    if isinstance(value, str):
        return compile_template(value).render(view) if "{{" in value else value
    if isinstance(value, dict):
        return {k: render_params(v, view) for k, v in value.items()}
    if isinstance(value, list):
        return [render_params(v, view) for v in value]
    return value


def expand_env_params(value: Any) -> Any:
    """Expand ``${VAR}`` / ``${VAR:default}`` in the strings of a parameter tree.

    Run it on the static document text *before* :func:`render_params`: a value that arrives later through a
    ``{{ }}`` template is data and is never read as an environment reference.
    """
    from tuvl.core.engine.templating import expand_env

    if isinstance(value, str):
        return expand_env(value, source="code.with") if "${" in value else value
    if isinstance(value, dict):
        return {k: expand_env_params(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_params(v) for v in value]
    return value


ENGINES: dict[str, Engine] = {}

_DERIVED: dict[int, tuple[weakref.ref[Any], dict[Hashable, Any]]] = {}


def derived[T](owner: Any, key: Hashable, build: Callable[[], T]) -> T:
    """``build()`` once per *owner* object and *key*.

    Keyed on the owner's identity, not its content: a reloaded project has new agent, workflow
    and model objects, so nothing built against the previous registries is ever reused. The
    entries go when the owner is collected. *key* must capture everything else ``build`` reads.
    """
    oid = id(owner)
    entry = _DERIVED.get(oid)
    if entry is None or entry[0]() is not owner:

        def forget(ref: weakref.ref[Any], oid: int = oid) -> None:
            if (current := _DERIVED.get(oid)) is not None and current[0] is ref:
                del _DERIVED[oid]

        entry = (weakref.ref(owner, forget), {})
        _DERIVED[oid] = entry
    cache = entry[1]
    if key not in cache:
        cache[key] = build()
    return cache[key]  # type: ignore[no-any-return]


def contract_model(
    owner: Any, name: str, fields: Mapping[str, TypeRef], resolver: ModelResolver
) -> type[BaseModel]:
    """``build_model(name, fields, resolver)``, built once per *owner* and field set."""
    frozen = tuple(fields.items())
    return derived(
        owner,
        ("model", name, frozen, resolver),
        lambda: build_model(name, dict(frozen), resolver),
    )


def register_engine(name: str, engine: Engine) -> None:
    ENGINES[name] = engine


class EngineError(RuntimeError):
    """An engine failed in a way it classified (``error_type``)."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


# ── pending: typed stubs (spec §9.1) ─────────────────────────────────────────

_SCALAR_STUBS: dict[str, Any] = {
    "str": "",
    "int": 0,
    "float": 0.0,
    "bool": False,
    "date": _dt.date(1970, 1, 1),
    "datetime": _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC),
    "uuid": uuid.UUID(int=0),
    "json": None,
    "file": {
        "store": "stub",
        "key": "stub",
        "name": "stub.bin",
        "size": 0,
        "content_type": "application/octet-stream",
        "sha256": "0" * 64,
    },
}


def stub_value(ref: TypeRef, resolver: ModelResolver) -> Any:
    """A schema-valid placeholder for *ref*."""
    if ref.optional:
        return None
    if ref.kind == "scalar":
        return _SCALAR_STUBS[ref.name]
    if ref.kind == "enum":
        return ref.values[0]
    if ref.kind == "list":
        return []
    cls = resolver(ref.name, ref.variant)
    return _model_stub(cls, resolver) if cls is not None else {}


def _model_stub(cls: type[BaseModel], resolver: ModelResolver) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, info in cls.model_fields.items():
        if not info.is_required():
            continue
        out[info.alias or name] = _annotation_stub(info.annotation, resolver)
    return out


def _annotation_stub(ann: Any, resolver: ModelResolver) -> Any:
    import typing

    origin = typing.get_origin(ann)
    if origin is Literal:
        return typing.get_args(ann)[0]
    if origin in (list, tuple):
        return []
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return _model_stub(ann, resolver)
    for scalar, python_type in (("bool", bool), ("int", int), ("float", float), ("str", str)):
        if ann is python_type:
            return _SCALAR_STUBS[scalar]
    if ann in (_dt.datetime, _dt.date, uuid.UUID):
        return {
            _dt.datetime: _SCALAR_STUBS["datetime"],
            _dt.date: _SCALAR_STUBS["date"],
            uuid.UUID: _SCALAR_STUBS["uuid"],
        }[ann]
    return None


async def pending_engine(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    signal = next((s for s in agent.routes if not is_reserved(s)), "default")
    outputs = {
        name: stub_value(
            parse_field(
                spec.model_dump(exclude_unset=True) if isinstance(spec, BaseModel) else spec
            ).type,
            ctx.resolver,
        )
        for name, spec in agent.outputs_for(signal).items()
    }
    return AgentResult(signal=signal, outputs=outputs, detail={"stub": True})


register_engine("pending", pending_engine)
