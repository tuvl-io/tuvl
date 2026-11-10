"""``tuvl serve mcp`` (spec §20.2): the development MCP server coding agents use to
read and change a tuvl project.

Every tool reloads the project, so answers reflect the files as they are now,
and returns the same JSON as the CLI's documented ``--json`` output
(:mod:`tuvl.cli.json_output`). Read tools are always on;
``codegen_write``, ``run_workflow`` and ``pin_fixture`` exist only with
``--allow-write``. Runs are read from a running server (``TUVL_URL`` /
``TUVL_TOKEN``). Development only: the server refuses ``TUVL_ENV=production``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tuvl.core.config import use_project
from tuvl.extras import require

require("mcp")

from mcp.server.fastmcp import FastMCP  # noqa: E402

INSTRUCTIONS = (
    "Tools for developing a tuvl project. Read the project state with spec_check, spec_status, "
    "validate and list_agents instead of inferring it from files. A spec must pass spec_check "
    "before you build from it. The core loop: spec_status → pick the next unblocked task → "
    "get_contract → implement → validate → test → spec_status."
)


def _load(project: Path) -> None:
    from tuvl.core.config_loader import load_all_configs

    use_project(str(project))
    load_all_configs(project)


def _http(method: str, path: str, **kwargs: Any) -> Any:
    import httpx

    url = os.environ.get("TUVL_URL", "http://localhost:8885").rstrip("/")
    token = os.environ.get("TUVL_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.request(method, f"{url}{path}", headers=headers, timeout=60, **kwargs)
    if response.status_code >= 400:
        return {"error": response.status_code, "detail": response.text}
    return response.json()


def build_server(project: Path, *, allow_write: bool = False) -> FastMCP:
    from dotenv import load_dotenv

    project = project.resolve()
    use_project(str(project))
    load_dotenv(project / ".env", override=False)  # datasources and AgentModels reference it
    server = FastMCP("tuvl", instructions=INSTRUCTIONS)

    @server.tool()
    def validate() -> dict[str, Any]:
        """Validate the project: errors, warnings (with V0xx codes) and per-workflow cost profiles."""
        from tuvl_insight.cli.commands.validate import run_validation

        result, *_ = run_validation(project, echo=False)
        return {
            "valid": not result.errors,
            "errors": [i.to_json() for i in result.errors],
            "warnings": [i.to_json() for i in result.warnings],
            "profiles": result.profiles,
        }

    @server.tool()
    def spec_check(spec: str | None = None) -> dict[str, Any]:
        """Is the spec complete and consistent? Deterministic, zero tokens: findings S001–S016
        with line numbers (blocking errors, warnings, waivers) and the prose sections it does not
        check. Fix every error before building."""
        from tuvl_insight.spec.check import check_report

        _load(project)
        return check_report(project, spec)

    @server.tool()
    async def spec_verify(spec: str | None = None, run_tests: bool = True) -> dict[str, Any]:
        """Does the implementation match the spec, step by step? Zero tokens: findings R001–R010
        (errors = contradictions, warnings = unfinished work) and the traceability matrix
        spec step → agent → tests. Loop on it until it has no errors."""
        from tuvl.core.config import settings
        from tuvl.runtime.code import load_agents
        from tuvl_insight.spec.verify import verify_report

        _load(project)
        load_agents(settings.dir_for("agents"))
        return await verify_report(project, spec, run_tests=run_tests)

    @server.tool()
    async def spec_status(spec: str | None = None, run_tests: bool = True) -> dict[str, Any]:
        """Every TaskPlan task's derived status (todo → in_progress → implemented → tested → done),
        with blocked/stale overlays and the reasons. The source of truth for what to do next."""
        from tuvl_insight.spec.tasks import TaskPlanError, status_report

        try:
            return await status_report(project, spec, run_tests=run_tests)
        except TaskPlanError as exc:
            return {"error": str(exc)}

    @server.tool()
    def spec_diff(spec: str | None = None) -> dict[str, Any]:
        """How each spec changed since its TaskPlan: stale tasks and uncovered sections."""
        from tuvl_insight.spec.tasks import TaskPlanError, diff_report

        try:
            return diff_report(project, spec)
        except TaskPlanError as exc:
            return {"error": str(exc)}

    @server.tool()
    def list_agents(workflow: str | None = None) -> list[dict[str, Any]]:
        """Agents per workflow: id, engine (pending = not implemented yet), description, routes."""
        from tuvl.contract.registry import WORKFLOWS

        _load(project)
        return [
            {
                "workflow": name,
                "id": a.id,
                "engine": a.engine,
                "description": a.description,
                "routes": dict(a.routes),
                "pending": a.engine == "pending",
            }
            for name, doc in sorted(WORKFLOWS.items())
            if workflow in (None, name)
            for a in doc.spec.agents
        ]

    @server.tool()
    def get_contract(workflow: str, agent: str | None = None) -> dict[str, Any]:
        """The typed contract of an agent (inputs, outputs, routes, engine block) or a workflow
        (trigger input, agents, ends, policy), plus the generated class names a code agent uses."""
        from tuvl.contract.registry import WORKFLOWS
        from tuvl_insight.codegen.schemas import camel, class_base

        _load(project)
        doc = WORKFLOWS.get(workflow)
        if doc is None:
            return {"error": f"no workflow {workflow!r}", "workflows": sorted(WORKFLOWS)}
        if agent is None:
            return doc.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        found = next((a for a in doc.spec.agents if a.id == agent), None)
        if found is None:
            return {
                "error": f"no agent {agent!r} in {workflow}",
                "agents": [a.id for a in doc.spec.agents],
            }
        stem = class_base(dict(WORKFLOWS))[(workflow, agent)]
        return {
            **found.model_dump(mode="json", by_alias=True, exclude_defaults=True),
            "generated": {
                "module": "agents._generated.schemas",
                "inputs": f"{stem}In",
                "outputs": f"{stem}Out",
                "workflow_input": f"{camel(workflow)}Input",
            },
        }

    @server.tool()
    def explain_error(code: str) -> dict[str, Any]:
        """What a validation code (V001…) means and its severity."""
        from tuvl_insight.validate.codes import RULES

        key = code.strip().upper().strip("[]")
        if key not in RULES:
            return {"error": f"unknown code {code!r}", "codes": sorted(RULES)}
        severity, meaning = RULES[key]
        return {"code": key, "severity": severity, "meaning": meaning}

    @server.tool()
    def get_run(run_id: str) -> Any:
        """A run's status, end, output and cost, from the running server (TUVL_URL)."""
        return _http("GET", f"/api/runs/{run_id}")

    @server.tool()
    def get_run_events(run_id: str) -> Any:
        """A run's journal (events and checkpoints, secure fields redacted) from the running server."""
        return _http("GET", f"/api/runs/{run_id}/export")

    @server.tool()
    def codegen() -> dict[str, Any]:
        """Dry run of `tuvl generate code`: which generated files would change, with their new content."""
        from tuvl_insight import codegen as generator

        return generator.run(project, write=False)

    @server.tool()
    async def test(only: str | None = None, live: bool = False) -> dict[str, Any]:
        """Run the project's Test documents (offline unless live=True)."""
        from tuvl_insight.cli.commands.test import _run

        use_project(str(project))
        results, warnings = await _run(
            project, live=live, allow_uncertain=False, only=only, strict=False
        )
        return {
            "passed": all(r.status == "passed" for r in results),
            "stubbed": [r.name for r in results if r.status == "stubbed"],
            "tests": [r.to_json() for r in results],
            "warnings": warnings,
        }

    if not allow_write:
        return server

    @server.tool()
    def codegen_write() -> dict[str, Any]:
        """Run `tuvl generate code` and write the generated files (schemas, stubs, spec tests)."""
        from tuvl_insight import codegen as generator

        return generator.run(project, write=True)

    @server.tool()
    def run_workflow(workflow: str, input: dict[str, Any]) -> Any:
        """Start a run on the running server and wait for its result."""
        return _http(
            "POST",
            f"/api/workflows/{workflow}/runs",
            params={"wait": "true"},
            json={"input": input},
        )

    @server.tool()
    def pin_fixture(run_id: str, name: str | None = None) -> dict[str, Any]:
        """Pin a run as tests/fixtures/<workflow>/<name>.journal.json for `replay:`."""
        import json

        document = _http("GET", f"/api/runs/{run_id}/export")
        if "error" in document:
            return dict(document)
        target = (
            project
            / "tests"
            / "fixtures"
            / document["run"]["workflow"]
            / f"{name or run_id}.journal.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n")
        return {"pinned": str(target.relative_to(project))}

    return server
