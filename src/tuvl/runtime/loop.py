"""The ``loop`` engine (spec §11): a bounded tool-using agent.

Each iteration is one model turn. The model sees the agent's typed inputs,
its steering and skills, and the declared tools:

* ``agent: <id>``      — an off-spine agent of this workflow; its contract is
                         the tool schema and its routes are ignored;
* ``workflow: <name>`` — a **child run** with its own journal and gates; its
                         tokens count toward this loop, nesting is capped by
                         ``policy.max_depth``;
* ``mcp: artifact://…``— the server's ``tools/list``, filtered by ``allow``.

Arguments are validated before execution; invalid ones come back to the
model as an observation (the turn still counts). A tool whose ``approval``
matches suspends the run as ``waiting_approval`` with the full loop state in
the approval request; approve / edit / reject resumes it (edited arguments
are validated again, a rejection is an observation). The model ends by
calling ``finish`` with the declared outputs (plus ``outcome`` when several
normal routes exist); one repair attempt, then ``error`` (``parse_error``). Budgets:
``max_iterations``, ``max_tokens``, ``max_tool_calls`` (and per-tool
``max_calls``); ``timeout`` is the runner's.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import json
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession

from tuvl.contract.facts import field_types
from tuvl.contract.lockfile import check_live_mcp
from tuvl.contract.schema import (
    AgentToolRef,
    CollectionToolRef,
    HumanAgent,
    McpToolRef,
    WorkflowToolRef,
)
from tuvl.contract.signals import is_reserved
from tuvl.contract.tel import TELError, compile_expr
from tuvl.contract.types import format_type
from tuvl.core.config import settings
from tuvl.runtime import devtools, journal, queue
from tuvl.runtime.engines import (
    ENGINES,
    AgentResult,
    EngineContext,
    EngineError,
    contract_model,
    register_engine,
)
from tuvl.runtime.llm import (
    OUTCOME,
    UNTRUSTED_DATA_RULE,
    _classify,
    _guard,
    _input_block,
    _prose,
    fence,
    memory_owner,
    recall_into,
    remember_result,
    structured_schema,
)
from tuvl.runtime.models import SystemApproval
from tuvl.runtime.tool import mcp_session, mcp_value

logger = structlog.get_logger(__name__)

FINISH = "finish"
_APPROVALS = SystemApproval.__table__  # type: ignore[attr-defined]
_EFFECT_RANK = {"read": 0, "write": 1, "destructive": 2}
_OBSERVATION_CHARS = 20_000
#: One model turn; a stalled provider is retried once, then the loop times out.
_TURN_TIMEOUT_SECONDS = 60.0


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    effect: str
    kind: str  # agent | workflow | collection | mcp
    ref: Any
    validator: type[BaseModel] | None = None
    target: Any = None


@dataclass
class LoopState:
    """Everything needed to continue a loop on another worker."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    tokens: int = 0
    tool_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    effects: dict[str, int] = field(default_factory=dict)
    repairs: int = 0
    #: Tool calls of the last model turn still to run (the first may await approval).
    queue: list[dict[str, Any]] = field(default_factory=list)
    #: ``tool(args)`` → times called (supervisor ``tool_repeated``).
    signatures: dict[str, int] = field(default_factory=dict)
    #: Supervisor rules (by position) that already fired — each fires once, so
    #: a resumed pause or a steer does not repeat on the same condition.
    fired: list[int] = field(default_factory=list)


# ── tools ────────────────────────────────────────────────────────────────────


def _effect_of(agent: Any) -> str:
    from tuvl.contract.facts import derived_effect

    effect = derived_effect(agent)
    if effect is None:
        effect = str(getattr(agent, "effect", None) or "read")
    return effect


def _strongest(doc: Any) -> str:
    effects = [_effect_of(a) for a in doc.spec.agents]
    return max(effects, key=lambda e: _EFFECT_RANK.get(e, 0), default="read")


def _mcp_effect(tool: Any) -> str:
    hints = getattr(tool, "annotations", None)
    if hints is not None and getattr(hints, "destructiveHint", False):
        return "destructive"
    if hints is not None and getattr(hints, "readOnlyHint", False):
        return "read"
    return "write"


async def build_tools(agent: Any, ctx: EngineContext, sessions: dict[str, Any]) -> dict[str, Tool]:
    from tuvl.contract.registry import WORKFLOWS

    by_id = {a.id: a for a in ctx.workflow.spec.agents}
    workflows = ctx.workflows if ctx.workflows is not None else WORKFLOWS
    tools: dict[str, Tool] = {}
    for ref in agent.loop.tools:
        if isinstance(ref, AgentToolRef):
            target = by_id.get(ref.agent)
            if target is None or isinstance(target, HumanAgent):
                raise EngineError(
                    "contract_violation", f"tool agent '{ref.agent}' cannot be a loop tool"
                )
            model = contract_model(
                target, f"{target.id}.args", field_types(target.inputs), ctx.resolver
            )
            tools[target.id] = Tool(
                target.id,
                target.description,
                structured_schema(model),
                _effect_of(target),
                "agent",
                ref,
                model,
                target,
            )
        elif isinstance(ref, WorkflowToolRef):
            doc = workflows.get(ref.workflow)
            if doc is None:
                raise EngineError(
                    "contract_violation", f"tool workflow '{ref.workflow}' is not loaded"
                )
            model = contract_model(
                doc, f"{ref.workflow}.args", field_types(doc.spec.trigger.input), ctx.resolver
            )
            tools[ref.workflow] = Tool(
                ref.workflow,
                doc.metadata.description or ref.workflow,
                structured_schema(model),
                _strongest(doc),
                "workflow",
                ref,
                model,
            )
        elif isinstance(ref, CollectionToolRef):
            name = ref.tool_name
            tools[name] = Tool(
                name,
                f"Search the '{ref.collection}' collection; returns the best-matching passages "
                f"with their metadata and score.",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "What to look for"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "read",
                "collection",
                ref,
            )
        elif isinstance(ref, McpToolRef):
            session = sessions[ref.mcp]
            listed = await session.list_tools()
            for item in listed.tools:
                if item.name not in ref.allow:
                    continue
                if settings.tuvl_env == "production":
                    check_live_mcp(settings.project_dir, ref.mcp, item.name, item.inputSchema)
                tools[item.name] = Tool(
                    item.name,
                    item.description or item.name,
                    item.inputSchema or {"type": "object"},
                    _mcp_effect(item),
                    "mcp",
                    ref,
                    target=item.name,
                )
    return tools


def _openai_tool(tool: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": f"{tool.description} (effect: {tool.effect})",
            "parameters": tool.schema,
        },
    }


def _finish_model(agent: Any, resolver: Any) -> type[BaseModel]:
    spec = {k: format_type(t) for k, t in field_types(agent.outputs).items()}
    signals = [s for s in agent.routes if not is_reserved(s)]
    if len(signals) > 1:
        spec[OUTCOME] = "enum[" + ", ".join(signals) + "]"
    return contract_model(agent, f"{agent.id}.finish", field_types(spec), resolver)


def _validate_args(tool: Tool, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object")
    if tool.validator is not None:
        return tool.validator.model_validate(args).model_dump(mode="json", by_alias=True)
    required = tool.schema.get("required") or []
    missing = [k for k in required if k not in args]
    if missing:
        raise ValueError(f"missing required arguments {missing}")
    return args


def _needs_approval(ref: Any, args: dict[str, Any]) -> bool:
    approval = ref.approval
    if approval is None:
        return False
    if approval == "required":
        return True
    try:
        return compile_expr(approval.when).evaluate({"args": args}) is True
    except TELError:
        return True  # an unevaluable condition fails closed


def _observation(value: Any) -> str:
    text = json.dumps(to_jsonable_python(value), default=str)
    return text if len(text) <= _OBSERVATION_CHARS else text[:_OBSERVATION_CHARS] + "…(truncated)"


async def _execute(
    tool: Tool,
    args: dict[str, Any],
    ctx: EngineContext,
    sessions: dict[str, Any],
    state: LoopState,
    policy_depth: int,
) -> tuple[bool, Any]:
    """Run one tool call. Returns ``(ok, observation value)``."""
    if tool.kind == "agent":
        engine = ENGINES.get(tool.target.engine)
        if engine is None:
            return False, f"no '{tool.target.engine}' engine"
        sub = EngineContext(
            run_id=ctx.run_id,
            workflow=ctx.workflow,
            session=ctx.session,
            principal=ctx.principal,
            idempotency_key=f"{ctx.idempotency_key}:{state.tool_calls}",
            resolver=ctx.resolver,
            emit=ctx.emit,
            run_child=ctx.run_child,
            depth=ctx.depth,
            workflows=ctx.workflows,
        )
        try:
            result = await engine(tool.target, args, sub)
        except EngineError as exc:
            return False, {"error_type": exc.error_type, "message": str(exc)}
        except Exception as exc:
            return False, {"error_type": "tool_error", "message": f"{type(exc).__name__}: {exc}"}
        if result.suspend:
            return False, "this tool waits for a person and cannot run inside a loop"
        if is_reserved(result.signal):
            return False, {
                "signal": result.signal,
                "error_type": result.error_type,
                "message": result.message,
            }
        return True, {"signal": result.signal, "outputs": result.outputs}
    if tool.kind == "workflow":
        if ctx.run_child is None:
            return False, "child runs are not available"
        if ctx.depth + 1 > policy_depth:
            return False, f"nesting depth {ctx.depth + 1} exceeds policy.max_depth {policy_depth}"
        try:
            child = await ctx.run_child(ctx.run_id, tool.name, args, ctx.principal)
        except ValueError as exc:
            return False, str(exc)
        state.tokens += child.tokens_used
        body = (child.output or {}).get("body")
        ok = child.status in ("completed",)
        return ok, {
            "run_id": str(child.id),
            "status": child.status,
            "end": child.end_name,
            "output": body,
            "error": child.error,
        }
    if tool.kind == "collection":
        from tuvl.runtime.builtins import search_collection

        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return False, "query must be a non-empty string"
        ref = tool.ref
        try:
            passages = await search_collection(
                ctx.session,
                ctx.run_id,
                collection=ref.collection,
                query=query,
                top_k=ref.top_k,
                metadata_filter=ref.filter,
                scope=ref.scope,
            )
        except EngineError as exc:
            return False, {"error_type": exc.error_type, "message": str(exc)}
        return True, {"passages": passages}
    session = sessions[tool.ref.mcp]
    try:
        result = await session.call_tool(tool.target, arguments=args)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return mcp_value(result)


# ── approvals ────────────────────────────────────────────────────────────────


async def _pending_approval(ctx: EngineContext, agent_id: str) -> SystemApproval | None:
    stmt = (
        sa.select(SystemApproval)
        .where(
            _APPROVALS.c.run_id == ctx.run_id,
            _APPROVALS.c.agent_id == agent_id,
            _APPROVALS.c.kind.in_(("tool", "pause")),
        )
        .order_by(_APPROVALS.c.created_at.desc())
        .limit(1)
    )
    found: SystemApproval | None = (await queue._execute(ctx.session, stmt)).scalars().first()
    if found is not None and found.payload.get("step") == ctx.idempotency_key:
        return found
    return None


async def _request_approval(
    agent: Any,
    ctx: EngineContext,
    tool: Tool,
    call: dict[str, Any],
    args: dict[str, Any],
    state: LoopState,
) -> AgentResult:
    ref = tool.ref
    group = ref.approval.required_group if not isinstance(ref.approval, str) else None
    approval = SystemApproval(
        run_id=ctx.run_id,
        agent_id=agent.id,
        kind="tool",
        payload=journal.to_json(
            {
                "step": ctx.idempotency_key,
                "title": f"Approve {tool.name}",
                "tool": tool.name,
                "effect": tool.effect,
                "args": args,
                "args_schema": tool.schema,
                "call_id": call["id"],
                "loop_state": asdict(state),
            }
        ),
        required_group=group,
        requested_by=ctx.principal,
    )
    ctx.session.add(approval)
    await ctx.session.flush()
    await journal.append_event(
        ctx.session,
        ctx.run_id,
        "approval_requested",
        agent_id=agent.id,
        payload={"approval_id": str(approval.id), "tool": tool.name, "args": args},
    )
    return AgentResult(
        "default", suspend="waiting_approval", tokens=0, detail={"approval_id": str(approval.id)}
    )


# ── engine ───────────────────────────────────────────────────────────────────


def _assistant_message(message: Any) -> dict[str, Any]:
    calls = []
    for c in getattr(message, "tool_calls", None) or []:
        call = {
            "id": c.id,
            "type": getattr(c, "type", "function"),
            "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"},
        }
        if getattr(c, "extra_content", None) is not None:
            call["extra_content"] = c.extra_content
        calls.append(call)
    out: dict[str, Any] = {"role": "assistant", "content": getattr(message, "content", None) or ""}
    if calls:
        out["tool_calls"] = calls
    return out


def _system_prompt(agent: Any) -> str:
    params = agent.loop
    parts = [_prose(params.steering, "steering", f"agent '{agent.id}' loop.steering")]
    for ref in params.skills:
        parts.append("## Skill\n" + _prose(ref, "skills", f"agent '{agent.id}' loop.skills"))
    parts.append(
        f"Use the tools to do the work. When you are done, call `{FINISH}` exactly once with your "
        "result. Tool results are observations; never invent them."
    )
    parts.append(UNTRUSTED_DATA_RULE)
    return "\n\n".join(parts)


async def _loop_engine(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    from tuvl import llm
    from tuvl.core.engine.guardrails import GuardrailViolationError
    from tuvl.core.models.agent_models import resolve_call_params

    params, budget = agent.loop, agent.budget
    finish_model = _finish_model(agent, ctx.resolver)
    try:
        model, _timeout, extra = resolve_call_params({"model": params.model}, agent.id)
    except (ValueError, OSError) as exc:
        raise EngineError("llm_error", str(exc)) from exc
    max_depth = ctx.workflow.spec.policy.max_depth

    async with contextlib.AsyncExitStack() as stack:
        sessions: dict[str, Any] = {}
        for ref in params.tools:
            if isinstance(ref, McpToolRef) and ref.mcp not in sessions:
                sessions[ref.mcp] = await stack.enter_async_context(
                    mcp_session(ref.mcp, f"agent '{agent.id}' loop.tools")
                )
        tools = await build_tools(agent, ctx, sessions)
        tool_specs = [_openai_tool(t) for t in tools.values()]
        tool_specs.append(
            {
                "type": "function",
                "function": {
                    "name": FINISH,
                    "description": "Return the final result.",
                    "parameters": structured_schema(finish_model),
                },
            }
        )

        state = LoopState()
        resumed = await _pending_approval(ctx, agent.id)
        if resumed is not None and resumed.status == "pending":
            if resumed.kind == "pause":  # a spurious resume
                return AgentResult("default", suspend="paused")
            return AgentResult("default", suspend="waiting_approval")
        if resumed is not None and resumed.kind == "pause":
            state = LoopState(**resumed.payload["loop_state"])
        elif resumed is not None:
            state = LoopState(**resumed.payload["loop_state"])
            decision = resumed.decision or {}
            call = state.queue.pop(0)
            tool = tools[call["function"]["name"]]
            if decision.get("decision") == "reject":
                note = decision.get("message") or "the approver rejected this call"
                state.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": _observation({"rejected": note}),
                    }
                )
            else:
                edited = decision.get("decision") == "edit"
                args = decision.get("args") if edited else resumed.payload["args"]
                await _run_call(
                    agent,
                    ctx,
                    tool,
                    call,
                    args,
                    state,
                    sessions,
                    max_depth,
                    approved=True,
                    edited_from=resumed.payload["args"] if edited else None,
                )
        else:
            user = _input_block(agent, view) if agent.inputs else "Begin."
            try:
                user = await _guard(agent.guardrails.input, user, "input", agent.id)
            except GuardrailViolationError as exc:
                return AgentResult("guardrail_violation", message=str(exc))
            state.messages = [
                {"role": "system", "content": _system_prompt(agent)},
                {"role": "user", "content": user},
            ]
            await recall_into(agent, view, ctx, [state.messages[0], state.messages[1]])

        supervisor = params.supervisor or ctx.workflow.spec.supervisor
        while True:
            control = await _take_control(ctx)
            if control is not None:
                action = control.get("action")
                await ctx.emit("supervisor", payload={"source": "operator", "action": action})
                if action == "abort":
                    return AgentResult(
                        "aborted", tokens=state.tokens, message="aborted by an operator"
                    )
                if action == "pause":
                    return await _pause(agent, ctx, state, "paused by an operator")
                if action == "steer":
                    state.messages.append(
                        {"role": "user", "content": f"[operator] {control.get('message')}"}
                    )
            if (
                supervisor is not None
                and state.iterations
                and state.iterations % supervisor.every_n_iterations == 0
            ):
                verdict = await _supervise(agent, ctx, supervisor, state)
                if verdict is not None:
                    return verdict
            # Remaining calls from the last turn first (one of them may need approval).
            while state.queue:
                batch = _concurrent_batch(agent, ctx, tools, state)
                if len(batch) > 1:
                    outcome = await _run_batch(agent, ctx, batch, state, sessions, max_depth)
                    if outcome is not None:
                        return outcome
                    continue
                call = state.queue[0]
                outcome = await _dispatch(
                    agent, ctx, tools, call, state, sessions, max_depth, finish_model
                )
                if outcome is not None:
                    return outcome
            if budget.max_iterations and state.iterations >= budget.max_iterations:
                return AgentResult(
                    "max_iterations",
                    tokens=state.tokens,
                    message=f"no result after {state.iterations} iterations",
                )
            response = None
            for attempt in (1, 2):  # one transport retry per turn; tools are not re-run
                try:
                    response = await asyncio.wait_for(
                        llm.acompletion(
                            model=model,
                            messages=state.messages,
                            tools=tool_specs,
                            tool_choice="auto",
                            **extra,
                        ),
                        _TURN_TIMEOUT_SECONDS,
                    )
                    break
                except TimeoutError:
                    if attempt == 2:
                        return AgentResult(
                            "timeout",
                            tokens=state.tokens,
                            message=f"model turn {state.iterations + 1} took over {_TURN_TIMEOUT_SECONDS}s twice",
                        )
                except Exception as exc:
                    classified = _classify(exc)
                    if attempt == 2 or classified.error_type not in (
                        "transient",
                        "rate_limit",
                        "timeout",
                    ):
                        raise classified from exc
            assert response is not None
            usage = getattr(response, "usage", None)
            turn_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            state.tokens += turn_tokens
            state.iterations += 1
            message = response.choices[0].message
            assistant = _assistant_message(message)
            state.messages.append(assistant)
            await ctx.emit(
                "llm_turn",
                payload={
                    "model": model,
                    "iteration": state.iterations,
                    "total_tokens": turn_tokens,
                    "tool_calls": [c["function"]["name"] for c in assistant.get("tool_calls", [])],
                },
            )
            if budget.max_tokens and state.tokens > budget.max_tokens:
                return AgentResult(
                    "budget_exceeded",
                    tokens=state.tokens,
                    message=f"used {state.tokens} tokens of {budget.max_tokens}",
                )
            if not assistant.get("tool_calls"):
                if state.repairs >= 1:
                    return AgentResult(
                        "error",
                        error_type="parse_error",
                        tokens=state.tokens,
                        message="the model never called finish",
                    )
                state.repairs += 1
                state.messages.append(
                    {"role": "user", "content": f"Call `{FINISH}` with your result now."}
                )
                continue
            state.queue = list(assistant["tool_calls"])


async def _dispatch(
    agent: Any,
    ctx: EngineContext,
    tools: dict[str, Tool],
    call: dict[str, Any],
    state: LoopState,
    sessions: dict[str, Any],
    max_depth: int,
    finish_model: type[BaseModel],
) -> AgentResult | None:
    """Handle the first queued call. Returns a result when the loop ends or waits."""
    from tuvl.core.engine.guardrails import GuardrailViolationError

    name = call["function"]["name"]
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except ValueError:
        args = None
    if name == FINISH:
        state.queue.pop(0)
        try:
            data = finish_model.model_validate(args if isinstance(args, dict) else {}).model_dump(
                by_alias=True
            )
        except ValidationError as exc:
            if state.repairs >= 1:
                return AgentResult(
                    "error",
                    error_type="parse_error",
                    tokens=state.tokens,
                    message=f"finish: {str(exc)[:400]}",
                )
            state.repairs += 1
            state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": _observation({"error": f"invalid result: {str(exc)[:400]}"}),
                }
            )
            state.queue.clear()
            return None
        signals = [s for s in agent.routes if not is_reserved(s)]
        signal = data.pop(OUTCOME) if len(signals) > 1 else (signals[0] if signals else "default")
        try:
            await _guard(agent.guardrails.output, json.dumps(data, default=str), "output", agent.id)
        except GuardrailViolationError as exc:
            return AgentResult("guardrail_violation", tokens=state.tokens, message=str(exc))
        return AgentResult(
            signal,
            outputs=data,
            tokens=state.tokens,
            detail={"iterations": state.iterations, "tool_calls": state.tool_calls},
        )
    tool = tools.get(name)
    if tool is None:
        state.queue.pop(0)
        _observe(
            state, call, False, f"unknown tool '{name}' — use one of {sorted(tools)} or {FINISH}"
        )
        return None
    try:
        clean = _validate_args(tool, args)
    except (ValueError, ValidationError) as exc:
        state.queue.pop(0)
        await ctx.emit(
            "tool_result", payload={"tool": name, "ok": False, "error": "invalid arguments"}
        )
        _observe(state, call, False, f"invalid arguments: {str(exc)[:400]}")
        return None
    budget = agent.budget
    if budget.max_tool_calls and state.tool_calls >= budget.max_tool_calls:
        return AgentResult(
            "budget_exceeded",
            tokens=state.tokens,
            message=f"reached budget.max_tool_calls ({budget.max_tool_calls})",
        )
    if tool.ref.max_calls and state.calls_by_tool.get(name, 0) >= tool.ref.max_calls:
        state.queue.pop(0)
        _observe(state, call, False, f"'{name}' has reached its max_calls ({tool.ref.max_calls})")
        return None
    if _needs_approval(tool.ref, clean) or devtools.approve_tools(ctx.run_id):
        return await _request_approval(agent, ctx, tool, call, clean, state)
    state.queue.pop(0)
    return await _run_call(agent, ctx, tool, call, clean, state, sessions, max_depth)


async def _run_call(
    agent: Any,
    ctx: EngineContext,
    tool: Tool,
    call: dict[str, Any],
    args: Any,
    state: LoopState,
    sessions: dict[str, Any],
    max_depth: int,
    approved: bool = False,
    edited_from: dict[str, Any] | None = None,
) -> AgentResult | None:

    if approved:  # edited arguments are validated again
        try:
            args = _validate_args(tool, args)
        except (ValueError, ValidationError) as exc:
            _observe(state, call, False, f"invalid arguments: {str(exc)[:400]}")
            return None
    await _begin_call(ctx, tool, args, state, approved)
    ok, value = await _timed_execute(tool, args, ctx, sessions, state, max_depth)
    return await _finish_call(agent, ctx, tool, call, args, state, ok, value, edited_from)


async def _begin_call(
    ctx: EngineContext, tool: Tool, args: Any, state: LoopState, approved: bool
) -> None:
    state.tool_calls += 1
    state.calls_by_tool[tool.name] = state.calls_by_tool.get(tool.name, 0) + 1
    state.effects[tool.effect] = state.effects.get(tool.effect, 0) + 1
    await ctx.emit(
        "tool_call",
        payload={
            "tool": tool.name,
            "kind": tool.kind,
            "effect": tool.effect,
            "args": to_jsonable_python(args),
            "approved": approved,
        },
    )
    signature = f"{tool.name}({json.dumps(to_jsonable_python(args), sort_keys=True, default=str)})"
    state.signatures[signature] = state.signatures.get(signature, 0) + 1


async def _timed_execute(
    tool: Tool,
    args: Any,
    ctx: EngineContext,
    sessions: dict[str, Any],
    state: LoopState,
    max_depth: int,
) -> tuple[bool, Any]:
    from tuvl.core.config import settings

    limit = settings.tuvl_agent_tool_timeout_s or None
    try:
        return await asyncio.wait_for(_execute(tool, args, ctx, sessions, state, max_depth), limit)
    except TimeoutError:
        return False, f"'{tool.name}' did not answer within {limit}s"


async def _finish_call(
    agent: Any,
    ctx: EngineContext,
    tool: Tool,
    call: dict[str, Any],
    args: Any,
    state: LoopState,
    ok: bool,
    value: Any,
    edited_from: dict[str, Any] | None = None,
) -> AgentResult | None:
    from tuvl.core.engine.guardrails import GuardrailViolationError

    await ctx.emit("tool_result", payload={"tool": tool.name, "ok": ok})
    if edited_from is not None:
        # The model must know what actually ran, not what it asked for.
        value = {
            "approver_changed_arguments": {"requested": edited_from, "executed": args},
            "result": value,
        }
    text = _observation(value)
    try:
        text = await _guard(agent.guardrails.tools, text, "tools", agent.id)
    except GuardrailViolationError as exc:
        return AgentResult("guardrail_violation", tokens=state.tokens, message=str(exc))
    state.messages.append(
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": fence(text, tool=tool.name) if ok else _observation({"error": value}),
        }
    )
    return None


def _concurrent_batch(
    agent: Any, ctx: EngineContext, tools: dict[str, Tool], state: LoopState
) -> list[tuple[dict[str, Any], Tool, Any]]:
    """The leading queued calls that may run at once: read-only MCP tools that need no person, in
    order. Other kinds share the run's database session, so they stay one at a time."""
    batch: list[tuple[dict[str, Any], Tool, Any]] = []
    used: dict[str, int] = {}
    for call in state.queue:
        tool = tools.get(call["function"]["name"])
        if tool is None or tool.kind != "mcp" or tool.effect != "read":
            break
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            clean = _validate_args(tool, args)
        except (ValueError, ValidationError):
            break
        budget = agent.budget
        if budget.max_tool_calls and state.tool_calls + len(batch) >= budget.max_tool_calls:
            break
        n = used.get(tool.name, 0) + state.calls_by_tool.get(tool.name, 0)
        if tool.ref.max_calls and n >= tool.ref.max_calls:
            break
        if _needs_approval(tool.ref, clean) or devtools.approve_tools(ctx.run_id):
            break
        used[tool.name] = used.get(tool.name, 0) + 1
        batch.append((call, tool, clean))
    return batch


async def _run_batch(
    agent: Any,
    ctx: EngineContext,
    batch: list[tuple[dict[str, Any], Tool, Any]],
    state: LoopState,
    sessions: dict[str, Any],
    max_depth: int,
) -> AgentResult | None:
    """Run the calls together. Events and the transcript keep the order the model asked in."""
    del state.queue[: len(batch)]
    for _, tool, args in batch:
        await _begin_call(ctx, tool, args, state, False)
    results = await asyncio.gather(
        *(_timed_execute(tool, args, ctx, sessions, state, max_depth) for _, tool, args in batch)
    )
    for (call, tool, args), (ok, value) in zip(batch, results, strict=True):
        outcome = await _finish_call(agent, ctx, tool, call, args, state, ok, value)
        if outcome is not None:
            return outcome
    return None


def _observe(state: LoopState, call: dict[str, Any], ok: bool, value: Any) -> None:
    state.messages.append(
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": _observation(value if ok else {"error": value}),
        }
    )


# ── supervision and live control (spec §11.6) ────────────────────────────────


async def _take_control(ctx: EngineContext) -> dict[str, Any] | None:
    """Consume an operator request (pause / abort / steer) left on the run."""
    from tuvl.runtime.models import SystemRun

    runs = SystemRun.__table__  # type: ignore[attr-defined]
    async with AsyncSession(ctx.session.bind) as s:
        row = (
            await queue._execute(
                s, sa.select(runs.c.control).where(runs.c.id == ctx.run_id).with_for_update()
            )
        ).first()
        control = row[0] if row else None
        if control:
            await queue._execute(
                s, sa.update(runs).where(runs.c.id == ctx.run_id).values(control=None)
            )
        await s.commit()
    return dict(control) if control else None


async def _pause(agent: Any, ctx: EngineContext, state: LoopState, reason: str) -> AgentResult:
    """Suspend the loop with its state; ``POST /api/runs/{id}/resume`` continues it."""
    record = SystemApproval(
        run_id=ctx.run_id,
        agent_id=agent.id,
        kind="pause",
        payload=journal.to_json(
            {
                "step": ctx.idempotency_key,
                "title": f"{agent.id} paused",
                "reason": reason,
                "loop_state": asdict(state),
            }
        ),
        requested_by=ctx.principal,
    )
    ctx.session.add(record)
    await ctx.session.flush()
    await journal.append_event(
        ctx.session, ctx.run_id, "paused", agent_id=agent.id, payload={"reason": reason}
    )
    return AgentResult("default", suspend="paused")


def _violation(rule: Any, state: LoopState, budget: Any) -> str | None:
    if rule.when == "iteration_reached" and state.iterations >= rule.gte:
        return f"iteration {state.iterations} reached {rule.gte}"
    if (
        rule.when == "budget_fraction"
        and budget.max_tokens
        and state.tokens / budget.max_tokens > rule.gt
    ):
        return f"used {state.tokens / budget.max_tokens:.0%} of max_tokens (> {rule.gt:.0%})"
    if rule.when == "tool_repeated":
        for sig, count in state.signatures.items():
            if count >= rule.count and (rule.tool is None or sig.startswith(f"{rule.tool}(")):
                return f"{sig.split('(')[0]} called {count}× with the same arguments"
    if rule.when == "effect_count":
        rank = _EFFECT_RANK[rule.effect]
        n = sum(c for effect, c in state.effects.items() if _EFFECT_RANK.get(effect, 0) >= rank)
        if n >= rule.count:
            return f"{n} {rule.effect} calls (limit {rule.count})"
    return None


async def _supervise(
    agent: Any, ctx: EngineContext, supervisor: Any, state: LoopState
) -> AgentResult | None:
    for n, rule in enumerate(supervisor.rules):
        if n in state.fired:
            continue
        why = _violation(rule, state, agent.budget)
        if why is None:
            continue
        state.fired.append(n)
        await ctx.emit(
            "supervisor",
            payload={"source": "rule", "rule": rule.when, "action": rule.then, "why": why},
        )
        if rule.then == "abort":
            return AgentResult("aborted", tokens=state.tokens, message=f"supervisor: {why}")
        if rule.then == "pause":
            return await _pause(agent, ctx, state, f"supervisor: {why}")
        message = supervisor.steer_message or f"Supervisor: {why}. Change approach or finish."
        state.messages.append({"role": "user", "content": message})
        return None
    if supervisor.judge is not None:
        return await _judge_trajectory(agent, ctx, supervisor, state)
    return None


async def _judge_trajectory(
    agent: Any, ctx: EngineContext, supervisor: Any, state: LoopState
) -> AgentResult | None:
    """The supervisor's judge reads the recent trajectory (spec §11.6, §12.2)."""
    from tuvl.runtime import judges

    recent = [m for m in state.messages if m["role"] != "system"][-12:]
    subject = {
        "agent": agent.description,
        "iterations": state.iterations,
        "tool_calls": state.tool_calls,
        "effects": state.effects,
        "recent": recent,
    }
    try:
        verdict = await judges.judge(
            supervisor.judge, subject, where=f"agent '{agent.id}' supervisor.judge"
        )
    except judges.JudgeUnavailableError as exc:
        await ctx.emit("judge_verdict", payload={"judge": supervisor.judge, "error": str(exc)})
        return await _act(
            agent, ctx, state, supervisor.on_judge_error, f"supervisor judge unavailable: {exc}"
        )
    await ctx.emit(
        "judge_verdict",
        payload={
            "judge": supervisor.judge,
            "verdict": verdict.verdict,
            "score": verdict.score,
            "reasons": verdict.reasons,
            "cached": verdict.cached,
        },
    )
    why = "; ".join(verdict.reasons) or verdict.verdict
    if verdict.verdict == "fail":
        return await _act(
            agent,
            ctx,
            state,
            supervisor.on_violation,
            f"supervisor judge: {why}",
            steer=supervisor.steer_message,
        )
    if verdict.verdict != "pass":
        return await _act(
            agent, ctx, state, supervisor.on_judge_error, f"supervisor judge uncertain: {why}"
        )
    return None


async def _act(
    agent: Any,
    ctx: EngineContext,
    state: LoopState,
    action: str,
    why: str,
    steer: str | None = None,
) -> AgentResult | None:
    await ctx.emit("supervisor", payload={"source": "judge", "action": action, "why": why})
    if action == "abort":
        return AgentResult("aborted", tokens=state.tokens, message=why)
    if action == "pause":
        return await _pause(agent, ctx, state, why)
    if action == "steer":
        state.messages.append({"role": "user", "content": steer or f"Supervisor: {why}"})
    return None


async def resume_paused(session: Any, run_id: Any) -> int:
    """Close a run's pending pause records so its loop continues on resume."""
    result = await queue._execute(
        session,
        sa.update(_APPROVALS)
        .where(
            _APPROVALS.c.run_id == run_id,
            _APPROVALS.c.kind == "pause",
            _APPROVALS.c.status == "pending",
        )
        .values(status="decided"),
    )
    return int(result.rowcount or 0)


# ── decisions on tool approvals ──────────────────────────────────────────────


async def decide_tool(
    session: Any,
    approval_id: Any,
    decision: str,
    reviewer: Any,
    *,
    args: dict[str, Any] | None = None,
    message: str | None = None,
) -> SystemApproval:
    """Approve, edit or reject a pending in-loop tool call, and re-queue the run."""
    from tuvl.runtime import human

    if decision not in ("approve", "edit", "reject"):
        raise human.ApprovalInvalidError("decision must be approve, edit or reject")
    if decision == "edit" and not isinstance(args, dict):
        raise human.ApprovalInvalidError("an edit needs 'edited_args' (an object)")
    stmt = sa.select(SystemApproval).where(_APPROVALS.c.id == approval_id).with_for_update()
    approval: SystemApproval | None = (await queue._execute(session, stmt)).scalars().first()
    if approval is None or approval.kind != "tool":
        raise human.ApprovalNotFoundError(f"no tool approval {approval_id}")
    if approval.status != "pending":
        raise human.ApprovalClosedError(f"this request is already {approval.status}")
    human.authorise(approval, reviewer)
    approval.status = "decided"
    approval.decision = {"decision": decision, "args": args, "message": message}
    approval.decided_by = reviewer.user_id
    approval.decided_at = _dt.datetime.now(_dt.UTC)
    await journal.append_event(
        session,
        approval.run_id,
        "approval_decided",
        agent_id=approval.agent_id,
        payload={
            "approval_id": str(approval.id),
            "decision": decision,
            "decided_by": reviewer.user_id,
        },
    )
    await queue.requeue(session, approval.run_id)
    return approval


async def loop_engine(agent: Any, view: dict[str, Any], ctx: EngineContext) -> AgentResult:
    result = await _loop_engine(agent, view, ctx)
    if agent.loop.memory is not None and result.suspend is None and not is_reserved(result.signal):
        owner = memory_owner(agent, view, ctx)
        await remember_result(agent, view, ctx, result.outputs, owner)
    return result


register_engine("loop", loop_engine)
