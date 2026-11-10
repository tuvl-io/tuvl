"""The ``decide`` engine (spec §10): ordered rules, then an optional model.

Normative semantics (§10.2):

* a rule matches → its ``then``; the model is **not** called (``rule#n``);
* no rule, no model → ``error / no_rule_matched``;
* no rule, model confident (≥ ``min_confidence``) → its value (``model``);
* no rule, model below the threshold → ``low_confidence`` (proposal journaled);
* no rule, model unavailable → ``error / decision_model_unavailable`` — never
  a fallback to rules, defaults or another model;
* ``shadow: true`` → rules decide; the model runs every time and is only
  journaled — its failures are logged, not errors.

The model call is retried per ``budget.retry`` and bounded by
``budget.timeout`` here (the runner does not wrap ``decide`` in its own
timeout), so a slow provider becomes ``decision_model_unavailable``.
Every invocation journals a ``decision`` record.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from tuvl.contract.facts import enum_outputs
from tuvl.contract.schema import duration_seconds
from tuvl.contract.tel import TELError, compile_expr
from tuvl.runtime import decision as providers
from tuvl.runtime.engines import AgentResult, EngineContext, EngineError, register_engine

logger = structlog.get_logger(__name__)


def _rules(agent: Any) -> list[tuple[str, str]]:
    rules = agent.decide.rules
    if isinstance(rules, str):
        from tuvl.core.artifacts.registry import ArtifactResolutionError, resolve_structured

        try:
            spec = resolve_structured(
                rules, site="decide.rules", where=f"agent '{agent.id}' decide.rules"
            )
        except ArtifactResolutionError as exc:
            raise EngineError("contract_violation", str(exc)) from exc
        return [(str(r["when"]), str(r["then"])) for r in spec.get("rules") or []]
    return [(r.when, r.then) for r in rules]


def _output(agent: Any) -> tuple[str, list[str]]:
    outputs = enum_outputs(agent)
    if len(outputs) != 1:
        raise EngineError("contract_violation", "a decide agent has exactly one enum output")
    key, ref = next(iter(outputs.items()))
    return key, list(ref.values)


async def _ask_model(agent: Any, view: dict[str, Any], choices: list[str]) -> providers.Decision:
    model = agent.decide.model
    config = providers.resolve_model(model.ref)
    timeout = duration_seconds(agent.budget.timeout) if agent.budget.timeout else None
    request = providers.DecisionRequest(
        model_ref=model.ref,
        config=config,
        inputs=view,
        choices=choices,
        description=agent.description,
        timeout=timeout,
    )
    retry = agent.budget.retry
    attempts = retry.attempts if retry is not None else 1
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(providers.decide(request), timeout)
        except (providers.ProviderUnavailableError, TimeoutError) as exc:
            last = exc
            if attempt < attempts:
                await asyncio.sleep(0.5 * (retry.backoff if retry else 1.5) ** (attempt - 1))
    message = f"timed out after {timeout}s" if isinstance(last, TimeoutError) else str(last)
    raise providers.ProviderUnavailableError(message)


async def decide_engine(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    key, choices = _output(agent)
    params = agent.decide
    started = time.monotonic()
    record: dict[str, Any] = {}

    matched: str | None = None
    for n, (when, then) in enumerate(_rules(agent), start=1):
        try:
            hit = compile_expr(when).evaluate(view)
        except TELError as exc:
            raise EngineError("contract_violation", f"rule #{n}: {exc}") from exc
        if hit is True:
            matched = then
            record = {"decision": then, "source": f"rule#{n}"}
            break

    if params.model is not None and (params.shadow or matched is None):
        try:
            answer = await _ask_model(agent, view, choices)
        except providers.ProviderUnavailableError as exc:
            if params.shadow:
                logger.warning("decide.shadow_model_failed", agent=agent.id, error=str(exc))
                record.setdefault("shadow", {"error": str(exc)})
            else:
                await _journal(
                    ctx,
                    {
                        "decision": None,
                        "source": "model",
                        "model_ref": params.model.ref,
                        "error": str(exc),
                    },
                    started,
                )
                return AgentResult(
                    "error", error_type="decision_model_unavailable", message=str(exc)
                )
        else:
            proposal = {
                "value": answer.value,
                "confidence": round(answer.confidence, 4),
                "model_ref": params.model.ref,
                **({"probabilities": answer.probabilities} if answer.probabilities else {}),
            }
            if params.shadow:
                record["shadow"] = proposal
            elif answer.confidence >= params.model.min_confidence:
                matched = answer.value
                record = {"decision": answer.value, "source": "model", **proposal}
            else:
                await _journal(ctx, {"decision": None, "source": "model", **proposal}, started)
                return AgentResult(
                    "low_confidence",
                    message=f"{params.model.ref} proposed {answer.value!r} at {answer.confidence:.2f} "
                    f"(< {params.model.min_confidence})",
                    detail={"proposal": proposal},
                )

    if matched is None:
        await _journal(ctx, {"decision": None, "source": "rules", **record}, started)
        return AgentResult("error", error_type="no_rule_matched", message="no rule matched")
    await _journal(ctx, record, started)
    return AgentResult(matched, outputs={key: matched}, detail={"source": record["source"]})


async def _journal(ctx: EngineContext, record: dict[str, Any], started: float) -> None:
    record["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
    await ctx.emit("decision", payload=record)


register_engine("decide", decide_engine)
