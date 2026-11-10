"""Project checks: validate, draft validation, schemas, codegen and tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tuvl.core.dev.project import project_dir
from tuvl_insight.api.dev.files import _area_path

router = APIRouter()

# ── checks ───────────────────────────────────────────────────────────────────


@router.post("/validate", summary="Validate the project (tuvl lint project --json)")
async def validate() -> dict[str, Any]:
    from tuvl_insight.cli.commands.validate import run_validation

    result, *_ = run_validation(project_dir(), echo=False)
    return {
        "valid": not result.errors,
        "errors": [i.to_json() for i in result.errors],
        "warnings": [i.to_json() for i in result.warnings],
        "profiles": result.profiles,
    }


class DraftRequest(BaseModel):
    area: str = "workflows"
    name: str
    text: str


@router.post(
    "/validate/draft",
    summary="Validate the project as if a file held this text (nothing is written)",
)
async def validate_draft(request: DraftRequest) -> dict[str, Any]:
    import shutil
    import tempfile

    from tuvl.core.config import use_project
    from tuvl_insight.cli.commands.validate import run_validation

    project = project_dir()
    target = _area_path(request.area, request.name).relative_to(project.resolve())
    ignore = shutil.ignore_patterns(
        ".git", ".venv", "node_modules", "__pycache__", ".tuvl", "deploy"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="tuvl-draft-") as tmp:
            copy = Path(tmp) / project.name
            shutil.copytree(project, copy, ignore=ignore)
            (copy / target).parent.mkdir(parents=True, exist_ok=True)
            (copy / target).write_text(request.text)
            result, *_ = run_validation(copy, echo=False)
    finally:
        use_project(project)  # validation may point the process at the copy
    return {
        "valid": not result.errors,
        "errors": [i.to_json() for i in result.errors],
        "warnings": [i.to_json() for i in result.warnings],
        "profiles": result.profiles,
    }


@router.get(
    "/schema/{kind}", summary="JSON Schema of a 2.0 document kind (or 'cli' for --json shapes)"
)
async def schema(kind: str) -> dict[str, Any]:
    from tuvl.contract.schema import DOCUMENT_MODELS, workflow_json_schema

    if kind == "cli":
        from tuvl_insight.cli.json_output import json_schemas

        return json_schemas()
    if kind == "Workflow":
        return workflow_json_schema()
    if kind in DOCUMENT_MODELS:
        return DOCUMENT_MODELS[kind].model_json_schema()
    raise HTTPException(404, f"unknown kind {kind!r}")


class CodegenRequest(BaseModel):
    write: bool = False


@router.post("/codegen", summary="tuvl generate code (dry run unless write)")
async def codegen(request: CodegenRequest) -> dict[str, Any]:
    from tuvl_insight import codegen as generator

    return generator.run(project_dir(), write=request.write)


class TestRequest(BaseModel):
    __test__ = False

    only: str | None = None
    live: bool = False


@router.post("/tests/run", summary="tuvl run tests --json")
async def run_tests(request: TestRequest) -> dict[str, Any]:
    from tuvl_insight.cli.commands.test import _run

    results, warnings = await _run(
        project_dir(), live=request.live, allow_uncertain=False, only=request.only, strict=False
    )
    return {
        "passed": all(r.status == "passed" for r in results),
        "stubbed": [r.name for r in results if r.status == "stubbed"],
        "tests": [r.to_json() for r in results],
        "warnings": warnings,
    }
