"""Specs, ideas and tasks: lifecycle, review, build and drift."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from tuvl.api.dev.jobs import _cli_argv, _cli_env, _start_job
from tuvl.core.dev.project import project_dir

router = APIRouter()

# ── specs ────────────────────────────────────────────────────────────────────


@router.get("/specs/lifecycle", summary="Each spec's id, approval state and ideas (read-only)")
async def spec_lifecycle() -> dict[str, Any]:
    from tuvl.spec.views import lifecycle_view

    return {"specs": lifecycle_view(project_dir())}


@router.get("/ideas", summary="The project's ideas and the specs drawn from them, and who you are")
async def ideas() -> dict[str, Any]:
    from tuvl.coding_agents import read_settings
    from tuvl.spec.views import ideas_view

    saved = read_settings(project_dir())
    return {
        "ideas": ideas_view(project_dir()),
        # what an upload and "generate spec" need: your initials (they number the ideas), your coding agent, and whether
        # you already agreed that the project's files may be sent to it
        "me": {
            "initials": saved.initials if saved else "",
            "agent": saved.agent if saved and saved.agent else "none",
            "consent": bool(saved and saved.consent),
        },
    }


@router.get("/ideas/{idea_id}", summary="One idea: record, file previews, linked specs (read-only)")
async def idea(idea_id: str) -> dict[str, Any]:
    from tuvl.spec import ids
    from tuvl.spec.views import idea_view

    if not ids.IDEA_ID.match(idea_id):
        raise HTTPException(400, "not an idea id")
    found = idea_view(project_dir(), idea_id)
    if found is None:
        raise HTTPException(404, f"{idea_id} is not an idea in this project")
    return found


_IDEA_MAX_FILES = 200
_IDEA_MAX_URLS = 10
_IDEA_MAX_BYTES = 50 * 1024 * 1024


def _upload_path(name: str) -> str | None:
    """The folder-relative path an uploaded file is kept under, or None when the name cannot be trusted."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0]:
        return None
    return "/".join(parts)


@router.post("/ideas", summary="tuvl import idea: files or text uploaded from Insight")
async def upload_idea(
    files: list[UploadFile] = File(default_factory=list),
    text: str = Form(""),
    urls: list[str] = Form(default_factory=list),
    allow_private: bool = Form(False),
    title: str = Form(""),
    supersedes: str = Form(""),
    initials: str = Form(""),
    commit: bool = Form(False),
) -> dict[str, Any]:
    """Save what was uploaded in a temporary folder and run `tuvl import idea` on it.

    The CLI does the importing, so the numbering, the hashes, the audit entry and the person recorded are the CLI's.
    """
    import tempfile

    from tuvl.spec import ids
    from tuvl.spec.views import idea_view

    project = project_dir()
    links = [u.strip() for u in urls if u.strip()]
    if not files and not text.strip() and not links:
        raise HTTPException(422, "Add at least one file or link, or write the idea.")
    if len(links) > _IDEA_MAX_URLS:
        raise HTTPException(413, f"At most {_IDEA_MAX_URLS} links in one idea.")
    if any(not re.match(r"https?://\S+$", u) for u in links):
        raise HTTPException(422, "A link must start with https:// (or http://) and hold no spaces.")
    if len(files) > _IDEA_MAX_FILES:
        raise HTTPException(413, f"At most {_IDEA_MAX_FILES} files in one idea.")
    if supersedes and not ids.IDEA_ID.match(supersedes):
        raise HTTPException(422, "supersedes must be an idea id such as IDEA-AB-0001.")
    before = set(ids.idea_ids(project))
    with tempfile.TemporaryDirectory(prefix="tuvl-idea-") as tmp:
        root = Path(tmp) / "idea"
        root.mkdir()
        total = 0
        for upload in files:
            rel = _upload_path(upload.filename or "")
            dest = (root / rel).resolve() if rel else None
            if rel is None or dest is None or not dest.is_relative_to(root.resolve()):
                raise HTTPException(422, f"{upload.filename!r} is not a usable file name.")
            data = await upload.read()
            total += len(data)
            if total > _IDEA_MAX_BYTES:
                raise HTTPException(
                    413, f"An idea may hold at most {_IDEA_MAX_BYTES // (1024 * 1024)} MB."
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        if text.strip():
            name = "idea.md" if not (root / "idea.md").exists() else "idea-text.md"
            (root / name).write_text(text if text.endswith("\n") else text + "\n")
        args = ["import", "idea", str(root)] if any(root.iterdir()) else ["import", "idea"]
        for link in links:
            args += ["--url", link]
        if allow_private:
            args.append("--allow-private")
        for flag, value in (
            ("--title", title.strip()),
            ("--supersedes", supersedes),
            ("--initials", initials.strip()),
        ):
            if value:
                args += [flag, value]
        if commit:
            args.append("--commit")
        done = await _spec_command(*args)
    created = sorted(set(ids.idea_ids(project)) - before)
    if not created:
        raise HTTPException(422, done["output"] or "Nothing was imported.")
    return {"id": created[0], "idea": idea_view(project, created[0]), "output": done["output"]}


class GenerateSpec(BaseModel):
    consent: bool = False
    where: str = "here"
    title: str = ""


@router.post("/ideas/{idea_id}/spec", summary="tuvl generate spec --idea (your coding agent)")
async def generate_spec_from_idea(idea_id: str, request: GenerateSpec) -> dict[str, str]:
    from tuvl.coding_agents import read_settings
    from tuvl.spec import ids

    project = project_dir()
    if not ids.IDEA_ID.match(idea_id) or idea_id not in ids.idea_ids(project):
        raise HTTPException(404, f"{idea_id} is not an idea in this project")
    if request.where not in ("here", "branch"):
        raise HTTPException(422, "where is 'here' or 'branch'.")
    settings = read_settings(project)
    if not settings or settings.agent in ("", "none"):
        raise HTTPException(
            409,
            "Choose your coding agent first: `tuvl use agent <agent>` (`tuvl list agents` shows what is installed).",
        )
    # `--here` writes the draft into this folder, uncommitted, so the Specs page shows it at once; `--no-pr` puts it on
    # a spec/<id> branch and pushes nothing. Either way the agent may change only the spec file.
    args = [
        "generate",
        "spec",
        "--idea",
        idea_id,
        "--yes",
        "--here" if request.where == "here" else "--no-pr",
    ]
    if request.title.strip():
        args += ["--title", request.title.strip()]
    if not settings.consent:
        if not request.consent:
            raise HTTPException(
                status_code=409,
                detail="Say that the idea's files may be sent to your coding agent.",
            )
        args.append("--consent")
    job = await _start_job(f"spec from {idea_id}", args)
    return {"job": job.id}


@router.get("/audit", summary="The local audit ledger (read-only) and whether its chain holds")
async def audit(
    entity: str | None = None,
    kind: str | None = None,
    actor: str | None = None,
    limit: int = Query(200, ge=0, le=2000),
    after: str | None = Query(None, pattern=r"^[0-9A-Z]{26}$"),
) -> dict[str, Any]:
    from tuvl.spec.views import audit_view

    return audit_view(
        project_dir(), entity=entity, kind=kind, actor=actor, limit=limit, after=after
    )


@router.get("/specs/check", summary="tuvl lint spec --json")
async def spec_check(spec: str | None = None) -> dict[str, Any]:
    from tuvl.spec.check import check_report
    from tuvl.spec.parse import SpecError

    try:
        return check_report(project_dir(), spec)
    except SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/specs/verify", summary="tuvl verify spec --json")
async def spec_verify(
    spec: str | None = None, run_tests: bool = Query(True, alias="tests")
) -> dict[str, Any]:
    from tuvl.spec.parse import SpecError
    from tuvl.spec.verify import verify_report

    try:
        return await verify_report(project_dir(), spec, run_tests=run_tests)
    except SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/specs/status", summary="tuvl list tasks --json")
async def spec_status(
    spec: str | None = None, run_tests: bool = Query(True, alias="tests")
) -> dict[str, Any]:
    from tuvl.spec.tasks import TaskPlanError, status_report

    try:
        return await status_report(project_dir(), spec, run_tests=run_tests)
    except TaskPlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/specs/branches", summary="tuvl list branches --json")
async def spec_branches(spec: str | None = None) -> dict[str, Any]:
    from tuvl.spec.branches import report

    return await asyncio.to_thread(report, project_dir(), Path(spec) if spec else None)


@router.get("/specs/{name}/tasks/{task_id}", summary="One task and the spec section it comes from")
async def spec_task(name: str, task_id: str) -> dict[str, Any]:
    from tuvl.spec.tasks import task_detail

    found = task_detail(project_dir(), task_id, Path(_spec_arg(name)))
    if found is None:
        raise HTTPException(status_code=404, detail=f"no task {task_id} in {name}")
    return found


@router.get("/specs/diff", summary="tuvl show drift --json")
async def spec_diff(spec: str | None = None) -> dict[str, Any]:
    from tuvl.spec.tasks import TaskPlanError, diff_report

    try:
        return diff_report(project_dir(), spec)
    except TaskPlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class BuildTask(BaseModel):
    consent: bool = False
    scaffold: bool = False


@router.post(
    "/specs/{name}/tasks/{task_id}/build", summary="tuvl generate code --task (your coding agent)"
)
async def build_task(name: str, task_id: str, request: BuildTask) -> dict[str, str]:
    from tuvl.coding_agents import read_settings
    from tuvl.spec.tasks import TaskPlanError, status_report

    try:
        report = await status_report(project_dir(), _spec_arg(name), run_tests=False)
    except TaskPlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    task = next((t for p in report["plans"] for t in p["tasks"] if t["id"] == task_id), None)
    if task is not None and task["type"] != "agent" and not request.scaffold:
        raise HTTPException(
            status_code=409,
            detail=f"A {task['type']} task is not built by the coding agent: models and workflows come "
            "from the scaffold (`tuvl generate code --no-agent`), tests from the spec's examples.",
        )
    if task is not None and task["blocked"] and not request.scaffold:
        raise HTTPException(
            status_code=409, detail="; ".join(task["reasons"]) or "The task is blocked."
        )
    settings = read_settings(project_dir())
    args = ["generate", "code", "--spec", _spec_arg(name)]
    args += ["--no-agent"] if request.scaffold else ["--task", task_id]
    args.append("--no-pr")
    if not request.scaffold and not (settings and settings.consent):
        if not request.consent:
            raise HTTPException(
                status_code=409, detail="Say that your files may be sent to your coding agent."
            )
        args.append("--consent")
    job = await _start_job(f"build {task_id}", args)
    return {"job": job.id}


async def _spec_command(*args: str) -> dict[str, Any]:
    """Run ``tuvl <args> -d <project>`` so the gates, the audit entry and the person recorded are the CLI's."""
    proc = await asyncio.create_subprocess_exec(
        *_cli_argv(list(args)),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_cli_env(),
    )
    out, _ = await proc.communicate()
    lines = [ln for ln in out.decode(errors="replace").splitlines() if not ln.startswith("{")]
    text = "\n".join(lines).strip()
    if proc.returncode != 0:
        raise HTTPException(status_code=422, detail=text or "the command failed")
    return {"ok": True, "output": text}


def _spec_arg(name: str) -> str:
    if "/" in name or "\\" in name or not name.endswith(".md"):
        raise HTTPException(status_code=422, detail="Name a spec file in specs/.")
    return f"specs/{name}"


class SpecReview(BaseModel):
    reason: str = ""


@router.post("/specs/{name}/approve", summary="tuvl approve spec")
async def approve_spec(name: str) -> dict[str, Any]:
    return await _spec_command("approve", "spec", _spec_arg(name))


@router.post("/specs/{name}/reject", summary="tuvl reject spec")
async def reject_spec(name: str, request: SpecReview) -> dict[str, Any]:
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="Say why it goes back to draft.")
    return await _spec_command(
        "reject", "spec", _spec_arg(name), "--reason", request.reason.strip()
    )


@router.post("/specs/{name}/reopen", summary="tuvl reopen spec")
async def reopen_spec(name: str) -> dict[str, Any]:
    return await _spec_command("reopen", "spec", _spec_arg(name))


@router.post("/specs/{name}/tasks", summary="tuvl generate tasks")
async def derive_tasks(name: str) -> dict[str, Any]:
    return await _spec_command("generate", "tasks", _spec_arg(name))


class NewSpec(BaseModel):
    name: str
    owner: str = ""
    workflow: str | None = None


@router.post("/specs/new", summary="tuvl create spec")
async def new_spec(request: NewSpec) -> dict[str, str]:
    from tuvl.cli.commands.spec import SPEC_TEMPLATE
    from tuvl.spec.parse import slug

    stem = slug(request.name)
    target = project_dir() / "specs" / f"{stem}.md"
    if target.exists():
        raise HTTPException(409, f"specs/{stem}.md already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    flow = request.workflow or stem.replace("-", "_")
    target.write_text(
        SPEC_TEMPLATE.format(name=stem, owner=request.owner or "unassigned", workflow=flow)
    )
    return {"created": f"specs/{stem}.md"}
