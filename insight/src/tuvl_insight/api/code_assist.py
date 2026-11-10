"""Insight's code-agent editor: save a code agent's Python, and an AI assistant that writes or
changes it against the agent's typed contract (dev mode only).

The assistant follows the same rules a coding agent does — the ``agent-code`` skill — and
returns the full file; nothing is written until the developer saves. Saving normalizes the file
through codegen (the decorator, signature and docstring come from the contract; the body is kept)
and reloads the agent in the running server.
"""

from __future__ import annotations

import re
from typing import Any

from tuvl.core.logger import logger

_FENCE = re.compile(r"```(?:python3?|py)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)

_RULES = """You are helping a developer write the Python body of one tuvl 2.0 `code` agent.

Rules (from the project's `agent-code` skill):
{skill}

The contract of this agent (generated from the workflow YAML — never change it):
{contract}

The models this agent may read and write through `ctx.db` (fields and types). `ctx.db.get/list/
create/update` return plain dicts (`row["field"]`, never attributes); timestamps are timezone-aware
UTC datetimes (`datetime.now(UTC)`), ids are uuid.UUID:
{models}

Answer in two parts: a short explanation (1-3 sentences), then the COMPLETE file in one
```python fenced block. Keep the imports, decorator, signature and docstring exactly as they are
in the current file — tuvl generate code owns them; change only the function body (and add imports or
small helper functions when needed). Return every declared signal the routes expect, with the
outputs the contract declares for it. If the request needs no code change, omit the code block."""


def contract_of(run: str) -> list[dict[str, Any]]:
    """Every workflow agent served by ``@agent(run)``: what it reads, returns and signals."""
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.contract.signals import is_reserved

    found = []
    for wf_name, doc in sorted(WORKFLOWS.items()):
        for agent in doc.spec.all_agents():
            if agent.engine != "code" or agent.code.run != run:
                continue
            outputs = (
                {s: dict(f) for s, f in agent.signal_outputs.items()}
                if agent.signal_outputs is not None
                else {"(every signal)": dict(agent.outputs)}
            )
            found.append(
                {
                    "workflow": wf_name,
                    "agent": agent.id,
                    "description": agent.description,
                    "effect": getattr(agent, "effect", None),
                    "params": dict(agent.code.with_),
                    "inputs": {k: str(v) for k, v in dict(agent.inputs).items()},
                    "outputs": {s: {k: str(v) for k, v in f.items()} for s, f in outputs.items()},
                    "signals": sorted(s for s in agent.routes if not is_reserved(s)),
                    "models": [str(m) for m in doc.spec.models],
                }
            )
    return found


def stub_for(run: str, description: str) -> str:
    """codegen's stub for *run*: typed when one saved workflow agent uses it, else untyped."""
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.models.schemas import SCHEMA_REGISTRY
    from tuvl_insight.codegen.schemas import render_schemas
    from tuvl_insight.codegen.stubs import new_stub

    users = [
        (wf, a)
        for wf, doc in WORKFLOWS.items()
        for a in doc.spec.all_agents()
        if a.engine == "code" and a.code.run == run
    ]
    names = None
    if len(users) == 1:
        _, all_names = render_schemas(dict(WORKFLOWS), SCHEMA_REGISTRY)
        names = all_names.get((users[0][0], users[0][1].id))
    fallback = users[0][1].description if users else "A code agent"
    return new_stub(run, names, description or fallback)


def draft_contract(agent: dict[str, Any]) -> dict[str, Any]:
    """The contract of an agent as the canvas has it (not saved yet)."""
    return {
        "workflow": "(unsaved draft)",
        "agent": agent.get("id"),
        "description": agent.get("description"),
        "effect": agent.get("effect"),
        "params": (agent.get("code") or {}).get("with") or {},
        "inputs": agent.get("inputs") or {},
        "outputs": agent.get("outputs") or {},
        "signals": sorted(str(s) for s in (agent.get("routes") or {}) if str(s) != "error"),
        "models": [],
    }


def model_fields(names: set[str]) -> dict[str, dict[str, str]]:
    """Each model's columns and their Python types, as the assistant needs them."""
    from tuvl.core.models.loader import MODEL_REGISTRY

    out: dict[str, dict[str, str]] = {}
    for name in sorted(names):
        cls = MODEL_REGISTRY.get(name)
        if cls is not None:
            out[name] = {
                field: str(info.annotation).replace("typing.", "")
                for field, info in cls.model_fields.items()
            }
    return out


def assist_models() -> list[dict[str, Any]]:
    """The project's LLM AgentModels the assistant may use, and whether each is configured."""
    from tuvl.core.models.agent_models import AGENT_MODEL_REGISTRY
    from tuvl.core.models.requirements import need_for

    out = []
    for name, spec in sorted(AGENT_MODEL_REGISTRY.items()):
        if spec.get("type") == "decision" or spec.get("enabled", True) is False:
            continue
        need = need_for(name, spec)
        info = need.to_json()
        out.append(
            {
                "name": name,
                "model": need.model,
                "configured": info["configured"],
                "missing": info["missing"],
            }
        )
    return out


def normalize(run: str, text: str) -> str:
    """The file as codegen would keep it: decorator, signature and docstring from the contract."""
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.models.schemas import SCHEMA_REGISTRY
    from tuvl_insight.codegen.schemas import render_schemas
    from tuvl_insight.codegen.stubs import merge_stub

    users = [
        (wf, a)
        for wf, doc in WORKFLOWS.items()
        for a in doc.spec.all_agents()
        if a.engine == "code" and a.code.run == run
    ]
    if not users:
        return text
    _, names = render_schemas(dict(WORKFLOWS), SCHEMA_REGISTRY)
    wf, first = users[0]
    keys = {
        (repr(a.inputs), repr(a.outputs), repr(a.signal_outputs), tuple(sorted(a.routes)))
        for _, a in users
    }
    signature = names.get((wf, first.id)) if len(keys) == 1 else None
    return merge_stub(text, run, signature, first.description)


async def assist(
    run: str,
    *,
    prompt: str,
    code: str | None,
    model: str | None,
    messages: list[dict[str, Any]],
    skill: str,
    draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One assistant turn: ``{reply, code, model, tokens}``. Model failures come back as a reply."""
    import json

    from tuvl import llm
    from tuvl.core.models.agent_models import AGENT_MODEL_REGISTRY, resolve_call_params

    name = model or ("default" if "default" in AGENT_MODEL_REGISTRY else "")
    if not name:
        return {
            "reply": "No model chosen and no `default` AgentModel in llms/.",
            "code": None,
            "model": None,
            "tokens": 0,
        }
    model_id, timeout, extra = resolve_call_params({"model": name}, f"assist:{run}")
    contract = contract_of(run) or ([draft_contract(draft)] if draft else [])
    system = _RULES.format(
        skill=skill.strip(),
        contract=json.dumps(contract, indent=2),
        models=json.dumps(model_fields({m for c in contract for m in c["models"]}), indent=2),
    )
    chat: list[dict[str, str]] = [{"role": "system", "content": system}]
    for m in messages[-12:]:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            chat.append({"role": m["role"], "content": m["content"]})
    chat.append(
        {
            "role": "user",
            "content": f"Current file:\n\n```python\n{(code or '').strip()}\n```\n\nRequest: {prompt}",
        }
    )
    try:
        response = await llm.acompletion(
            model=model_id, messages=chat, timeout=max(timeout, 60), **extra
        )
    except Exception as exc:
        logger.warning("insight.code_assist.failed", run=run, error=type(exc).__name__)
        return {
            "reply": f"The model call failed ({type(exc).__name__}): {exc}",
            "code": None,
            "model": name,
            "tokens": 0,
        }
    text = (response.choices[0].message.content or "").strip()
    match = _FENCE.search(text)
    reply = (text[: match.start()] + text[match.end() :]).strip() if match else text
    usage = getattr(response, "usage", None)
    return {
        "reply": reply or "Updated the code.",
        "code": match.group(1).strip() + "\n" if match else None,
        "model": name,
        "tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }
