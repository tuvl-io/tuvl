"""``tuvl schema`` — export the 2.0 document JSON Schemas (Insight forms,
editor integrations, docs). Hidden until the 2.0 surface is released."""

from __future__ import annotations

import json
from pathlib import Path

import typer

schema_app = typer.Typer(
    name="schema",
    help="Export the tuvl 2.0 document JSON Schemas.",
    no_args_is_help=True,
    hidden=True,
)


@schema_app.command("export")
def export_cmd(
    kind: str = typer.Option(
        "Workflow",
        "--kind",
        "-k",
        help="Workflow, Test, TaskPlan, all, or cli (the --json output shapes).",
    ),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write to a file instead of stdout."),
) -> None:
    """Print (or write) the JSON Schema for a 2.0 document kind."""
    from tuvl.contract.schema import DOCUMENT_MODELS, document_json_schemas, workflow_json_schema

    if kind == "cli":
        from tuvl_insight.cli.json_output import json_schemas

        payload: object = json_schemas()
    elif kind == "all":
        payload = document_json_schemas()
    elif kind == "Workflow":
        payload = workflow_json_schema()
    elif kind in DOCUMENT_MODELS:
        payload = DOCUMENT_MODELS[kind].model_json_schema()
    else:
        typer.echo(
            f"Unknown kind {kind!r} — one of: {', '.join([*DOCUMENT_MODELS, 'all'])}", err=True
        )
        raise typer.Exit(code=2)

    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text)
        typer.echo(f"Wrote {out}", err=True)
