"""Judges and rules artifacts."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tuvl.core.dev.project import project_dir

router = APIRouter()

# ── judges and rules artifacts ───────────────────────────────────────────────


class JudgeRequest(BaseModel):
    name: str
    text: str | None = None  # an unsaved draft of the artifact; else the registered one
    subject: Any = None


def _judge(request: JudgeRequest) -> Any:
    from tuvl.runtime import judges

    try:
        if request.text is not None:
            return judges.from_document(request.text)
        return judges.load(f"artifact://{request.name}", where="insight")
    except judges.JudgeUnavailableError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/judges/try", summary="Ask a judge for its verdict on one subject (live)")
async def judge_try(request: JudgeRequest) -> dict[str, Any]:
    from dataclasses import asdict

    from tuvl.runtime import judges

    try:
        verdict = await judges.judge_with(_judge(request), request.subject)
    except judges.JudgeUnavailableError as exc:
        raise HTTPException(502, f"judge unavailable: {exc}") from exc
    return asdict(verdict)


@router.post("/judges/calibrate", summary="Run a judge on its calibration set (live)")
async def judge_calibrate(request: JudgeRequest) -> dict[str, Any]:
    from dataclasses import asdict

    from tuvl.runtime import judges

    try:
        result = await judges.calibrate_with(_judge(request), project_dir())
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"calibration set unreadable: {exc}") from exc
    return asdict(result)


_FIXTURE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class LabelRequest(BaseModel):
    label: str
    subject: Any


@router.post("/judges/label", summary="Save a labelled subject under fixtures/judge/")
async def judge_label(request: LabelRequest) -> dict[str, str]:
    """Insight then appends ``{input, expect}`` to the judge's calibration list."""
    if not _FIXTURE_NAME.fullmatch(request.label):
        raise HTTPException(400, "label: lowercase letters, digits, - and _")
    path = project_dir() / "fixtures" / "judge" / f"{request.label}.json"
    if path.exists():
        raise HTTPException(409, f"fixtures/judge/{request.label}.json already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request.subject, indent=2, default=str) + "\n")
    return {"input": f"fixtures/judge/{request.label}.json"}


class RulesTryRequest(BaseModel):
    text: str
    context: dict[str, Any]


@router.post("/rules/try", summary="Which rule of a rules artifact draft matches a context")
async def rules_try(request: RulesTryRequest) -> dict[str, Any]:
    import yaml as pyyaml
    from pydantic import ValidationError

    from tuvl.contract.schema import RulesSpec
    from tuvl.contract.tel import TELError, compile_expr

    try:
        spec = RulesSpec.model_validate((pyyaml.safe_load(request.text) or {}).get("spec"))
    except (pyyaml.YAMLError, AttributeError, ValidationError) as exc:
        raise HTTPException(422, f"not a valid rules artifact: {exc}") from exc
    for n, rule in enumerate(spec.rules, start=1):
        try:
            hit = compile_expr(rule.when).evaluate(request.context)
        except TELError as exc:
            return {"rule": n, "then": None, "error": str(exc)}
        if hit is True:
            return {"rule": n, "then": rule.then, "error": None}
    return {"rule": None, "then": None, "error": None}
