"""A workflow's typed public surface (P5.1/P5.2): schemas and derived effect."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from tuvl.api.triggers import trigger_openapi
from tuvl.contract.schema import WorkflowDocument
from tuvl.contract.surface import end_schema, input_schema, workflow_effect
from tuvl.contract.types import build_model

ORDER = build_model("OrderRead", {"id": "uuid", "total": "float", "state": "enum[open, paid]"})


def resolver(name: str, variant: str) -> type[BaseModel] | None:
    return ORDER if name == "Order" else None


def workflow(agents: str, outputs: str = "", trigger: str = "") -> WorkflowDocument:
    text = f"""
kind: Workflow
version: tuvl/v2
metadata: {{ name: refund, description: Refund an order }}
spec:
  trigger:
    http: {{ path: "/orders/{{order_id}}/refund", method: POST }}
    input: {{ order_id: uuid, reason: "str?" }}
    {trigger}
  agents:
{agents}
  outputs:
{outputs or "    default: {}"}
"""
    return WorkflowDocument.model_validate(yaml.safe_load(text))


LOAD = """    - id: load
      description: Load the order
      engine: tool
      tool: { use: db, model: Order, op: read, where: { id: "{{ order_id }}" } }
      inputs: { order_id: uuid }
      outputs: { order: Order }
      routes: { default: END, not_found: END.missing }
"""


def test_input_and_end_schemas() -> None:
    doc = workflow(
        LOAD,
        """    default:
      status: 200
      body: { id: "{{ order.id }}", state: "{{ order.state }}", note: "refunded {{ order.id }}", n: 1, ok: true }
    missing: { status: 404, body: { error: not found } }
    declared: { status: 409, type: { reason: str, retry: "bool?" } }
    empty: { status: 204 }
""",
    )
    schema = input_schema(doc, resolver)
    assert schema["properties"]["order_id"] == {
        "format": "uuid",
        "title": "Order Id",
        "type": "string",
    }
    assert schema["required"] == ["order_id"]
    body = end_schema(doc, "default", resolver)
    assert body is not None
    assert body["properties"]["id"] == {"format": "uuid", "type": "string"}
    assert body["properties"]["state"] == {"enum": ["open", "paid"], "type": "string"}
    assert body["properties"]["note"] == {"type": "string"}  # text around a template
    assert body["properties"]["n"] == {"type": "integer"}
    assert body["properties"]["ok"] == {"type": "boolean"}
    assert end_schema(doc, "missing", resolver) == {
        "type": "object",
        "properties": {"error": {"type": "string"}},
        "required": ["error"],
    }
    declared = end_schema(doc, "declared", resolver)
    assert declared is not None and declared["required"] == ["reason"]
    assert end_schema(doc, "empty", resolver) is None
    assert end_schema(doc, "nope", resolver) is None


def test_unknown_references_stay_open() -> None:
    doc = workflow(LOAD, '    default: { body: { x: "{{ ghost }}", y: "{{ order.ghost }}" } }')
    body = end_schema(doc, "default", resolver)
    assert body is not None and body["properties"] == {"x": {}, "y": {}}


def test_trigger_openapi_maps_path_params_body_and_ends() -> None:
    doc = workflow(LOAD, "    default: { status: 200 }\n    missing: { status: 404 }")
    op = trigger_openapi(doc, resolver)
    assert op["parameters"][0]["name"] == "order_id" and op["parameters"][0]["in"] == "path"
    request = op["requestBody"]["content"]["application/json"]["schema"]
    assert list(request["properties"]) == ["reason"] and request["required"] == []
    assert op["requestBody"]["required"] is False
    assert {"200", "404", "202", "422"} <= set(op["responses"])
    assert op["responses"]["404"]["description"] == "END.missing"
    assert op["x-tuvl-workflow"] == "refund"


@pytest.mark.parametrize(
    ("agents", "effect"),
    [
        (LOAD, "read"),
        (
            """    - id: wipe
      description: Delete it
      engine: tool
      tool: { use: db, model: Order, op: delete, where: { id: "{{ order_id }}" } }
      routes: { default: END }
""",
            "destructive",
        ),
        (
            """    - id: mark
      description: Mark it
      engine: code
      code: { run: mark }
      routes: { default: END }
""",
            "write",
        ),
    ],
)
def test_workflow_effect(agents: str, effect: str) -> None:
    assert workflow_effect(workflow(agents)) == effect


GOLDEN = Path(__file__).parent / "golden" / "triage_ticket.openapi.json"


@pytest.mark.requires_insight
def test_sample_trigger_openapi_snapshot(tmp_path: Path) -> None:
    """The sample project's trigger operation, generated from its contracts.
    Regenerate with UPDATE_GOLDEN=1."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TUVL_", "GEMINI_"))}
    made = subprocess.run(
        [sys.executable, "-c", "from tuvl.cli import app; app()", "init", "demo", "--sample", "-y"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert made.returncode == 0, made.stdout + made.stderr
    from tuvl.contract.registry import WORKFLOWS
    from tuvl.core.config import use_project
    from tuvl.core.config_loader import load_all_configs

    use_project(tmp_path / "demo")
    load_all_configs(tmp_path / "demo")
    current: Any = trigger_openapi(WORKFLOWS["triage_ticket"])
    text = json.dumps(current, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text)
    assert GOLDEN.read_text() == text, (
        "trigger OpenAPI changed — rerun with UPDATE_GOLDEN=1 if intended"
    )
