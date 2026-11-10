"""tuvl mcp (P3.8): the dev MCP server's tools on a fresh sample project."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tuvl_insight.devmcp import build_server


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("mcp")
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("TUVL_", "GEMINI_", "POSTGRES_"))
    }
    created = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tuvl.cli import app; app()",
            "init",
            "demo",
            "--sample",
            "--preset",
            "ollama",
            "-y",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    return root / "demo"


async def call(server: Any, tool: str, **args: Any) -> Any:
    result = await server.call_tool(tool, args)
    if isinstance(result, tuple) and isinstance(result[1], dict):  # structured output
        structured = result[1]
        return structured["result"] if set(structured) == {"result"} else structured
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads("".join(getattr(b, "text", "") for b in blocks))


async def test_tools_listed_by_mode(project: Path) -> None:
    read = {t.name for t in await build_server(project).list_tools()}
    assert {
        "validate",
        "spec_check",
        "spec_verify",
        "spec_status",
        "spec_diff",
        "get_contract",
        "list_agents",
        "explain_error",
        "get_run",
        "get_run_events",
        "codegen",
        "test",
    } <= read
    assert not read & {"codegen_write", "run_workflow", "pin_fixture", "spec_analyse", "spec_apply"}
    write = {t.name for t in await build_server(project, allow_write=True).list_tools()}
    assert {"codegen_write", "run_workflow", "pin_fixture"} <= write
    assert not write & {"spec_analyse", "spec_apply"}


async def test_read_tools(project: Path) -> None:
    server = build_server(project)
    assert (await call(server, "validate"))["valid"] is True
    check = await call(server, "spec_check")
    assert check["specs"] and all("findings" in s for s in check["specs"])
    verify = await call(server, "spec_verify")
    assert verify["ok"] is True and verify["specs"][0]["summary"]["implemented"] == 4
    status = await call(server, "spec_status")
    assert (
        status["done"] is True
        and [t["status"] for t in status["plans"][0]["tasks"]] == ["done"] * 8
    )
    agents = await call(server, "list_agents", workflow="triage_ticket")
    assert {a["id"]: a["engine"] for a in agents}["prioritise"] == "code"
    contract = await call(server, "get_contract", workflow="triage_ticket", agent="prioritise")
    assert (
        contract["generated"]["inputs"] == "PrioritiseIn"
        and contract["code"]["run"] == "prioritise_ticket"
    )
    assert "error" in await call(server, "get_contract", workflow="nope")
    assert (await call(server, "explain_error", code="[V024]"))["severity"] == "E (ship)"
    assert (await call(server, "codegen"))["would_change"] == []
    assert (await call(server, "test"))["passed"] is True
    assert (await call(server, "spec_diff"))["specs"][0]["changed"] is False


async def test_codegen_write(project: Path) -> None:
    (project / "agents" / "_generated" / "schemas.py").unlink()
    server = build_server(project, allow_write=True)
    written = await call(server, "codegen_write")
    assert {"path": "agents/_generated/schemas.py", "action": "create"} in written["written"]
    assert (project / "agents" / "_generated" / "schemas.py").is_file()


def test_refuses_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from tuvl.cli import app

    monkeypatch.setenv("TUVL_ENV", "production")
    result = CliRunner().invoke(app, ["serve", "mcp"])
    assert result.exit_code == 1 and "development tool" in result.output
