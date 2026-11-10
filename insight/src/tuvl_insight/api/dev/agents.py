"""The Python behind a code agent's code.run, and the code assistant."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tuvl.core.config import settings
from tuvl.core.dev.project import project_dir

router = APIRouter()

# ── code agents: the Python behind code.run ────────────────────────────────


def _agent_function(directory: Path, run: str) -> tuple[Path, int, str] | None:
    """The ``@agent("<run>")`` function in *directory*: (file, first line, source incl. decorators).

    Read from the files (not the import cache), so an edit on disk shows at once.
    """
    import ast

    for path in sorted(directory.rglob("*.py")) if directory.is_dir() else []:
        if "_generated" in path.parts:
            continue
        try:
            text = path.read_text()
            tree = ast.parse(text)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and (getattr(deco.func, "id", None) or getattr(deco.func, "attr", None))
                    == "agent"
                    and deco.args
                    and isinstance(deco.args[0], ast.Constant)
                    and deco.args[0].value == run
                ):
                    start = min(d.lineno for d in node.decorator_list)
                    lines = text.splitlines()[start - 1 : node.end_lineno]
                    return path, start, "\n".join(lines) + "\n"
    return None


@router.get("/agents/{run}/source", summary="The Python function behind a code agent's code.run")
async def agent_source(run: str, description: str = "") -> dict[str, Any]:
    from tuvl_insight.codegen.stubs import function_name

    if not re.fullmatch(r"[A-Za-z_][\w.]*", run):
        raise HTTPException(400, "invalid code.run name")
    if run.startswith("tuvl."):
        return {
            "run": run,
            "builtin": True,
            "file": None,
            "line": None,
            "source": None,
            "file_text": None,
            "exists": True,
        }
    project = project_dir()
    found = _agent_function(settings.dir_for("agents"), run)
    if found is None:
        # a new code agent (e.g. just added on the canvas): start from codegen's stub
        from tuvl_insight.api.code_assist import stub_for

        target = settings.dir_for("agents") / f"{function_name(run)}.py"
        return {
            "run": run,
            "builtin": False,
            "exists": False,
            "file": str(target.relative_to(project))
            if target.is_relative_to(project)
            else str(target),
            "line": None,
            "source": None,
            "file_text": stub_for(run, description),
        }
    path, line, source = found
    return {
        "run": run,
        "builtin": False,
        "exists": True,
        "file": str(path.relative_to(project)) if path.is_relative_to(project) else str(path),
        "line": line,
        "source": source,
        "file_text": path.read_text(),
    }


class SourceWrite(BaseModel):
    text: str


@router.put(
    "/agents/{run}/source",
    summary="Save a code agent's Python (normalized by codegen, then reloaded)",
)
async def save_agent_source(run: str, request: SourceWrite) -> dict[str, Any]:
    import ast

    from tuvl.runtime.code import load_agents
    from tuvl_insight.api.code_assist import normalize
    from tuvl_insight.codegen.stubs import function_name

    if not re.fullmatch(r"[A-Za-z_]\w*", run):
        raise HTTPException(400, "invalid code.run name (built-ins have no Python)")
    try:
        ast.parse(request.text)
    except SyntaxError as exc:
        raise HTTPException(422, {"error": f"syntax error: {exc.msg}", "line": exc.lineno}) from exc
    decorator = r"""@(?:\w+\.)?agent\(\s*["']""" + re.escape(run) + r"""["']"""
    if not re.search(decorator, request.text):
        raise HTTPException(422, {"error": f'keep the @agent("{run}") decorator', "line": None})
    project = project_dir()
    agents_dir = settings.dir_for("agents")
    found = _agent_function(agents_dir, run)
    note: str | None
    path = found[0] if found else agents_dir / f"{function_name(run)}.py"
    try:
        text = normalize(run, request.text)
    except Exception as exc:
        text, note = request.text, f"not normalized by codegen: {exc}"
    else:
        note = (
            None
            if text == request.text
            else "codegen restored the decorator, signature or docstring"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    load_error: str | None = None
    try:
        load_agents(agents_dir)
    except Exception as exc:
        load_error = f"{type(exc).__name__}: {exc}"
    saved = _agent_function(agents_dir, run)
    return {
        "run": run,
        "file": str(path.relative_to(project)) if path.is_relative_to(project) else str(path),
        "line": saved[1] if saved else None,
        "source": saved[2] if saved else None,
        "file_text": text,
        "note": note,
        "load_error": load_error,
    }


@router.get("/agents/assist/models", summary="LLM AgentModels the code assistant can use")
async def code_assist_models() -> list[dict[str, Any]]:
    from tuvl_insight.api.code_assist import assist_models

    return assist_models()


class AssistRequest(BaseModel):
    prompt: str
    code: str | None = None
    model: str | None = None
    messages: list[dict[str, Any]] = []
    #: the agent as the canvas has it (an unsaved draft has no saved contract yet)
    agent: dict[str, Any] | None = None


@router.post(
    "/agents/{run}/assist", summary="AI help writing a code agent's body (nothing is saved)"
)
async def code_assist(run: str, request: AssistRequest) -> dict[str, Any]:
    from tuvl_insight.api.code_assist import assist, assist_models
    from tuvl_insight.skills import skill_text

    if not request.prompt.strip():
        raise HTTPException(422, "empty prompt")
    choices = {m["name"]: m for m in assist_models()}
    name = request.model or ("default" if "default" in choices else None)
    if name in choices and not choices[name]["configured"]:
        missing = ", ".join(choices[name]["missing"]) or "its configuration"
        raise HTTPException(
            422, f"model '{name}' is not configured — it needs {missing} (see `tuvl check system`)"
        )
    return await assist(
        run,
        prompt=request.prompt,
        code=request.code,
        model=name,
        messages=request.messages,
        skill=skill_text("agent-code"),
        draft=request.agent,
    )
