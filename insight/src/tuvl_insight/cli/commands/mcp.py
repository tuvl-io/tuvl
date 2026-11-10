"""``tuvl serve mcp`` — the development MCP server for coding agents (spec §20.2)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer


def mcp_cmd(
    project_dir: Annotated[
        Path, typer.Option("--project-dir", "-d", help="tuvl project directory")
    ] = Path("."),
    allow_write: Annotated[
        bool,
        typer.Option("--allow-write", help="Also offer codegen_write, run_workflow, pin_fixture"),
    ] = False,
) -> None:
    """Serve development tools to a coding agent over MCP (stdio)."""
    if os.environ.get("TUVL_ENV") == "production":
        typer.echo(
            "tuvl serve mcp is a development tool — it does not run with TUVL_ENV=production.",
            err=True,
        )
        raise typer.Exit(1)
    from tuvl.extras import require

    require("mcp")
    from tuvl_insight.devmcp import build_server

    build_server(project_dir, allow_write=allow_write).run("stdio")
