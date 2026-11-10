"""The process-wide runtime: one engine, one runner, one worker.

The API serves the synchronous path through the worker (``run_inline``); a
worker role also polls the queue in the background. ``tuvl serve app --role``
chooses which of the two a process does (spec §14.3).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tuvl.contract.facts import field_types
from tuvl.contract.schema import WorkflowDocument, duration_seconds
from tuvl.contract.types import build_model
from tuvl.runtime import queue
from tuvl.runtime.models import SystemRun
from tuvl.runtime.runner import Runner, workflow_hash

Role = Literal["all", "api", "worker"]


class InputError(ValueError):
    """The trigger input does not match ``trigger.input``."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("invalid input")
        self.errors = errors


class WorkflowUnavailableError(RuntimeError):
    """The workflow names a model that is not loaded, so its first ``db`` call would fail."""

    def __init__(self, workflow: str, reasons: list[str]) -> None:
        self.workflow = workflow
        self.reasons = reasons
        super().__init__(f"workflow '{workflow}' is unavailable: {'; '.join(reasons)}")


def unavailable_reasons(doc: WorkflowDocument) -> list[str]:
    """Why *doc* cannot run: each ``spec.models`` entry that is not in the model registry."""
    from tuvl.core.models.loader import MODEL_REGISTRY, MODEL_VERSION_REGISTRY

    reasons: list[str] = []
    for pin in doc.spec.models:
        name = pin.partition("@")[0]
        if name in MODEL_REGISTRY:
            continue
        if name in MODEL_VERSION_REGISTRY:
            reasons.append(
                f"model '{name}' is disabled (a database override set in Insight or the admin "
                "API) — re-enable it, or remove it from spec.models"
            )
        else:
            reasons.append(f"model '{name}' is not defined in this project")
    return reasons


@dataclass
class RuntimeHost:
    engine: AsyncEngine
    workflows: Mapping[str, WorkflowDocument] | None = None
    role: Role = "all"
    concurrency: int = 8
    runner: Runner = field(init=False)
    worker: queue.Worker = field(init=False)
    _input_models: dict[str, type[BaseModel]] = field(default_factory=dict)
    _poller: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        queue.bind_engine_tenant(self.engine)
        self.runner = Runner(self.engine, self.workflows)
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.worker = queue.Worker(
            self.engine, self.runner, owner=owner, concurrency=self.concurrency
        )

    def registry(self) -> Mapping[str, WorkflowDocument]:
        return self.runner.registry()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.role in ("all", "worker") and self._poller is None:
            self._poller = asyncio.create_task(self.worker.run_forever())

    async def stop(self) -> None:
        await self.worker.stop()
        if self._poller is not None:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
            self._poller = None
        from tuvl.core.config import settings

        await self.worker.drain(settings.tuvl_shutdown_drain_seconds)

    # ── runs ─────────────────────────────────────────────────────────────────

    def validate_input(
        self, doc: WorkflowDocument, payload: Mapping[str, Any], key: str | None = None
    ) -> dict[str, Any]:
        key = key or workflow_hash(doc)
        if key not in self._input_models:
            self._input_models[key] = build_model(
                f"{doc.metadata.name}.input",
                field_types(doc.spec.trigger.input),
                self.runner.resolver,
            )
        try:
            model = self._input_models[key].model_validate(dict(payload))
        except ValidationError as exc:
            raise InputError(
                [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors(include_url=False)]
            ) from exc
        return model.model_dump(mode="json", by_alias=True)

    def unavailable(self) -> dict[str, list[str]]:
        """Workflows that cannot run now, with the reasons."""
        out: dict[str, list[str]] = {}
        for name, doc in self.registry().items():
            reasons = unavailable_reasons(doc) if doc.enabled else []
            if reasons:
                out[name] = reasons
        return out

    async def enqueue(
        self,
        doc: WorkflowDocument,
        payload: Mapping[str, Any],
        *,
        principal: str | None,
        trigger: str,
    ) -> uuid.UUID:
        reasons = unavailable_reasons(doc)
        if reasons:
            raise WorkflowUnavailableError(doc.metadata.name, reasons)
        digest = workflow_hash(doc)
        clean = self.validate_input(doc, payload, digest)
        async with AsyncSession(self.engine) as s:
            run_id = await queue.enqueue(
                s,
                workflow=doc.metadata.name,
                workflow_hash=digest,
                trigger=trigger,
                input=clean,
                principal=principal,
            )
            await s.commit()
        return run_id

    async def run_sync(self, doc: WorkflowDocument, run_id: uuid.UUID) -> SystemRun:
        """Execute *run_id* inline up to ``trigger.http.sync_timeout``; the
        returned run is finished, waiting, or still running in the background."""
        http = doc.spec.trigger.http
        timeout = duration_seconds(http.sync_timeout) if http else 30.0
        if self.role != "worker":
            outcome = await queue.run_inline(self.worker, run_id, timeout_seconds=timeout)
            if outcome == "not_claimed":  # a background worker got there first: wait for it
                await queue.wait_settled(self.engine, run_id, timeout)
        return await self.get(run_id)

    async def get(self, run_id: uuid.UUID) -> SystemRun:
        async with AsyncSession(self.engine, expire_on_commit=False) as s:
            run = await s.get(SystemRun, run_id)
        assert run is not None
        return run


_HOST: RuntimeHost | None = None


def set_host(host: RuntimeHost | None) -> None:
    global _HOST
    _HOST = host


def get_host() -> RuntimeHost:
    if _HOST is None:
        raise RuntimeError("the 2.0 runtime is not started")
    return _HOST
