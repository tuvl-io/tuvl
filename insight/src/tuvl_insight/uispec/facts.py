"""What a UI needs to know about a backend, read from the loaded project — no model involved."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from tuvl_insight.spec.lifecycle import split_front

#: Task states that count as implemented when tests were not run.
_IMPLEMENTED = {"implemented", "tested", "done"}
_JSON: dict[str, Any] = {"sort_keys": True, "ensure_ascii": False, "default": str}


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, **_JSON).encode()).hexdigest()[:12]


def describe(schema: dict[str, Any] | None) -> str:
    """A JSON schema as a short type: ``string``, ``enum: a | b``, ``uuid | null``, ``item[]``."""
    if not schema:
        return "any"
    if "enum" in schema:
        return "enum: " + " | ".join(str(v) for v in schema["enum"])
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        parts = [describe(o) for o in options]
        return " | ".join(dict.fromkeys(parts))
    kind = schema.get("type")
    fmt = schema.get("format")
    if kind == "array":
        return f"{describe(schema.get('items'))}[]"
    if fmt in ("uuid", "date-time", "date", "email"):
        return str({"date-time": "timestamp"}.get(fmt, fmt))
    return str(kind or "object")


def fields_of(schema: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The top-level properties of an object schema as rows (name, type, required)."""
    schema = schema or {}
    required = set(schema.get("required", []))
    return [
        {"name": name, "type": describe(prop), "required": name in required}
        for name, prop in (schema.get("properties") or {}).items()
    ]


def sample(schema: dict[str, Any] | None) -> Any:
    """A plausible value for a schema, for an illustration when no real example exists."""
    schema = schema or {}
    if "enum" in schema:
        return schema["enum"][0]
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        non_null = [o for o in options if o.get("type") != "null"]
        return sample(non_null[0] if non_null else options[0])
    kind, fmt = schema.get("type"), schema.get("format")
    if kind == "object" or "properties" in schema:
        return {k: sample(v) for k, v in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [sample(schema.get("items"))]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.5
    if kind == "boolean":
        return True
    return {
        "uuid": "00000000-0000-4000-8000-000000000000",
        "date-time": "2026-01-01T09:00:00Z",
        "date": "2026-01-01",
        "email": "person@example.com",
    }.get(str(fmt), "string")


def _task_state(project: Path, run_tests: bool) -> dict[str, list[tuple[str, str]]]:
    """Workflow → [(task id, status)] of the non-obsolete tasks that target it."""
    from tuvl_insight.spec.tasks import derived_plans, project_state

    async def go() -> dict[str, list[tuple[str, str]]]:
        state = await project_state(project, run_tests=run_tests)
        out: dict[str, list[tuple[str, str]]] = {}
        for _, tasks in derived_plans(project, state):
            for t in tasks:
                if t.obsolete:
                    continue
                wf = t.target.get("workflow")
                if wf:
                    out.setdefault(wf, []).append((t.id, t.status))
        return out

    return asyncio.run(go())


def _examples(project: Path, workflow: str) -> list[dict[str, Any]]:
    """The acceptance examples of *workflow* (generated tests): real inputs and the end they reach."""
    out: list[dict[str, Any]] = []
    directory = project / "tests"
    for path in sorted(directory.rglob("*.y*ml")) if directory.is_dir() else []:
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        spec = (doc or {}).get("spec") if isinstance(doc, dict) else None
        if not (isinstance(doc, dict) and doc.get("kind") == "Test" and isinstance(spec, dict)):
            continue
        if spec.get("workflow") != workflow or "input" not in spec:
            continue
        out.append(
            {
                "name": (doc.get("metadata") or {}).get("name", path.stem),
                "input": spec["input"],
                "end": (spec.get("expect") or {}).get("end"),
            }
        )
    return out[:4]


def _human_steps(doc: Any) -> list[dict[str, Any]]:
    steps = []
    for a in doc.spec.agents:
        if a.engine != "human":
            continue
        h = a.human
        steps.append(
            {
                "agent": a.id,
                "title": h.title,
                "instruction": h.instruction,
                "group": h.required_group,
                "expires": h.expires,
                "route_on": h.route_on,
                "form": [
                    {"name": n, "widget": f.widget or "text", "label": f.label or n, "help": f.help}
                    for n, f in h.form.items()
                ],
                "show": list(h.show),
            }
        )
    return steps


def _approvals(doc: Any) -> list[str]:
    """Agents whose tool calls wait for approval (``tools[].approval``), by agent id."""
    out = []
    for a in doc.spec.agents:
        tools = getattr(getattr(a, "loop", None), "tools", None) or []
        if any(getattr(t, "approval", None) for t in tools):
            out.append(a.id)
    return out


def _workflow_facts(
    project: Path, name: str, doc: Any, tasks: dict[str, list[tuple[str, str]]], run_tests: bool
) -> dict[str, Any]:
    from tuvl.api.triggers import trigger_openapi
    from tuvl.api.uploads import file_inputs
    from tuvl.contract.surface import input_schema, workflow_effect

    http = doc.spec.trigger.active_http
    pending = [a.id for a in doc.spec.agents if a.engine == "pending"]
    open_tasks = [
        t
        for t, s in tasks.get(name, [])
        if not (s == "done" or (not run_tests and s in _IMPLEMENTED))
    ]
    operation = trigger_openapi(doc) if http else {}
    responses = []
    for code, body in sorted((operation.get("responses") or {}).items(), key=lambda kv: int(kv[0])):
        if code == "202":
            continue
        schema = ((body.get("content") or {}).get("application/json") or {}).get("schema")
        responses.append(
            {
                "status": int(code),
                "ends": [e.strip() for e in str(body.get("description", "")).split(",")],
                "fields": fields_of(schema),
                "example": sample(schema) if schema else None,
            }
        )
    schema = input_schema(doc)
    files = file_inputs(doc)
    upload = http.upload if http else None
    in_path = re.findall(r"{([A-Za-z_][A-Za-z0-9_]*)}", http.path) if http else []
    return {
        "name": name,
        "description": doc.metadata.description,
        "ready": not pending and not open_tasks,
        "missing": {"pending_agents": pending, "open_tasks": open_tasks},
        "http": {"method": http.method, "path": http.path, "sync_timeout": http.sync_timeout}
        if http
        else None,
        "auth": {
            "public": bool(http and http.public),
            "scope": doc.metadata.required_scope,
            "group": doc.metadata.required_group,
        },
        "effect": workflow_effect(doc),
        "input": {
            "in_path": in_path,
            "fields": [
                {**fld, "type": ("list[file]" if files[fld["name"]] else "file")}
                if fld["name"] in files
                else fld
                for fld in fields_of(schema)
            ],
            "example": sample(schema),
        },
        "upload": {
            "files": sorted(files),
            "max_bytes": upload.max_bytes,
            "max_files": upload.max_files,
            "accept": list(upload.accept),
        }
        if upload
        else None,
        "responses": responses,
        "human": _human_steps(doc),
        "tool_approvals": _approvals(doc),
        "examples": _examples(project, name),
        "ends": {end: {"status": spec.status} for end, spec in sorted(doc.spec.outputs.items())},
    }


def _model_facts(project: Path) -> list[dict[str, Any]]:
    from tuvl.core.system_config import expose_model_crud
    from tuvl_insight.spec.verify import model_definitions

    if not expose_model_crud():
        return []
    out = []
    for name, spec in sorted(model_definitions(project).items()):
        crud = list((spec.get("api") or {}).get("crud") or [])
        if not crud:
            continue
        access = spec.get("access") or {}
        low = name.lower()
        fields = spec.get("fields") or []
        if isinstance(fields, dict):
            fields = [
                {"name": k, **(v if isinstance(v, dict) else {"type": v})}
                for k, v in fields.items()
            ]
        out.append(
            {
                "name": name,
                "path": f"/models/{low}/",
                "operations": crud,
                "scopes": {
                    "read": access.get("read_scope") or f"{low}:read",
                    "write": access.get("write_scope") or f"{low}:write",
                    "delete": access.get("delete_scope") or f"{low}:delete",
                },
                "groups": {
                    k: list(access.get(f"{k}_groups") or []) for k in ("read", "write", "delete")
                },
                "fields": [
                    {
                        "name": f.get("name"),
                        "type": f.get("type"),
                        "required": bool(f.get("required")),
                        "primary_key": bool(f.get("primary_key")),
                        "in_create": f.get("input", not f.get("primary_key")) is not False,
                        "secure": bool(f.get("secure")),
                    }
                    for f in fields
                    if isinstance(f, dict) and f.get("name")
                ],
            }
        )
    return out


def collect(project: Path, *, run_tests: bool = True) -> dict[str, Any]:
    """The UI-facing facts of *project*: its callable workflows and CRUD models."""
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.config import use_project
    from tuvl.core.config_loader import load_all_configs

    project = project.resolve()
    use_project(str(project))
    from tuvl.core.system_config import load_system_config

    load_system_config.cache_clear()  # the settings of this project, not of the last one read
    load_all_configs(project)
    tasks = _task_state(project, run_tests)
    workflows = [
        _workflow_facts(project, n, d, tasks, run_tests)
        for n, d in sorted(WORKFLOWS.items())
        if d.spec.trigger.http is not None
    ]
    other = sorted(n for n, d in WORKFLOWS.items() if d.spec.trigger.http is None)
    spec_ids = (
        sorted(
            {
                str(split_front(p.read_text())[0].get("id"))
                for p in (project / "specs").glob("*.md")
                if split_front(p.read_text())[0].get("id")
            }
        )
        if (project / "specs").is_dir()
        else []
    )
    from tuvl.core.system_config import api_docs_mode, cors_origins, exposed_api_groups

    return {
        "project": project.name,
        "surface": {
            "groups": sorted(exposed_api_groups(production=True)),
            "docs": api_docs_mode(production=True),
            "cors": list(cors_origins()),
        },
        "specs": spec_ids,
        "workflows": workflows,
        "not_callable": other,
        "models": _model_facts(project),
    }
