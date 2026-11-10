"""Loading ``version: tuvl/v2`` documents (P0.5)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from tuvl.contract import registry
from tuvl.contract.registry import DocumentLoadError, parse_document, yaml_error_hint
from tuvl.core.config_loader import load_all_configs

FIXTURE = Path(__file__).parents[2] / "contract" / "fixtures" / "refund_flow.yaml"

V1_WORKFLOW = """\
kind: Workflow
metadata: {name: legacy}
spec:
  trigger: {path: /legacy, method: POST}
  steps:
    - id: done
      kind: Response
      source: x
"""

TEST_DOC = """\
kind: Test
version: tuvl/v2
metadata: {name: small-refund}
spec:
  workflow: refund_flow
  mocks:
    load_order: {outputs: {order: {total: 40}}}
"""

TASK_PLAN = """\
kind: TaskPlan
version: tuvl/v2
metadata: {name: refunds, spec: specs/refunds.md, spec_hash: abc}
spec:
  tasks:
    - {id: T1, title: Triage, type: agent}
"""


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    write(tmp_path, "workflows/refund_flow.yaml", FIXTURE.read_text())
    write(tmp_path, "tests/small_refund.yaml", TEST_DOC)
    write(tmp_path, "specs/refunds.tasks.yaml", TASK_PLAN)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_registries():
    yield
    registry.clear()


# ── load_all_configs ────────────────────────────────────────────────────────


def test_v2_documents_load_into_the_contract_registry(project: Path) -> None:
    load_all_configs(project)

    assert set(registry.WORKFLOWS) == {"refund_flow"}
    assert registry.WORKFLOWS["refund_flow"].spec.entry == "load_order"
    assert set(registry.TESTS) == {"small-refund"}
    assert registry.TASK_PLANS == {}  # the task list is checked by `tuvl spec check`, not loaded
    assert registry.SOURCES[("Workflow", "refund_flow")] == "refund_flow.yaml"


def test_a_production_server_skips_tests_and_task_plans(project: Path) -> None:
    load_all_configs(project, include_dev_documents=False)

    assert set(registry.WORKFLOWS) == {"refund_flow"}
    assert registry.TESTS == {} and registry.TASK_PLANS == {}


def test_reload_clears_previous_documents(project: Path) -> None:
    load_all_configs(project)
    (project / "workflows" / "refund_flow.yaml").unlink()
    load_all_configs(project)
    assert registry.WORKFLOWS == {}


def test_v1_workflows_are_rejected(project: Path) -> None:
    write(project, "workflows/legacy.yaml", V1_WORKFLOW)
    with pytest.raises(DocumentLoadError) as exc:
        load_all_configs(project)
    message = str(exc.value)
    assert "Workflow 'legacy' in 'legacy.yaml'" in message
    assert "tuvl 1.x workflow" in message and "version: tuvl/v2" in message


def test_invalid_v2_document_fails_the_load_with_author_paths(project: Path) -> None:
    doc = yaml.safe_load(FIXTURE.read_text())
    doc["spec"]["agents"][1]["decide"]["rules"][0]["when"] = "order.total >"
    doc["spec"]["agents"][3]["code"]["run"] = "bad-name"
    write(project, "workflows/refund_flow.yaml", yaml.safe_dump(doc))

    with pytest.raises(DocumentLoadError) as exc:
        load_all_configs(project)
    paths = [e.path for e in exc.value.errors]
    assert "spec.agents[triage_refund].decide.rules[0].when" in paths
    assert "spec.agents[issue_refund].code.run" in paths
    assert "Workflow 'refund_flow' in 'refund_flow.yaml' is invalid" in str(exc.value)


def test_duplicate_v2_names_are_rejected(project: Path) -> None:
    write(project, "workflows/copy.yaml", FIXTURE.read_text())
    with pytest.raises(DocumentLoadError, match="duplicate Workflow name — already defined in"):
        load_all_configs(project)


def test_capitalised_test_kind_requires_version(project: Path) -> None:
    write(project, "tests/bad.yaml", TEST_DOC.replace("version: tuvl/v2\n", ""))
    with pytest.raises(DocumentLoadError, match="version"):
        load_all_configs(project)


# ── Error presentation ──────────────────────────────────────────────────────


def _errors(doc: dict) -> list[str]:
    with pytest.raises(DocumentLoadError) as exc:
        parse_document(doc, "wf.yaml")
    return [str(e) for e in exc.value.errors]


def _doc(**agent: object) -> dict:
    base = {"id": "fetch", "description": "d", "routes": {"default": "END"}}
    return {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "wf"},
        "spec": {"agents": [{**base, **agent}]},
    }


def test_union_tags_do_not_leak_into_paths() -> None:
    errors = _errors(
        _doc(engine="tool", tool={"use": "http", "method": "GET", "url": "https://x", "body": {}})
    )
    assert errors == ["spec.agents[fetch].tool: GET requests do not take a body"]


def test_unknown_engine_and_unknown_key_messages() -> None:
    assert _errors(_doc(engine="robot")) == [
        "spec.agents[fetch]: unknown value 'robot' — expected one of 'pending', 'code', 'tool', 'decide', 'llm', 'loop', 'human', 'map', 'parallel'"
    ]
    assert _errors(_doc(engine="pending", colour="red")) == [
        "spec.agents[fetch].colour: unknown key — not part of the 2.0 schema"
    ]


def test_field_map_errors_point_at_the_field() -> None:
    assert _errors(_doc(engine="pending", inputs={"n": "integer"})) == [
        "spec.agents[fetch].inputs: n: unknown type 'integer' — did you mean 'int'? at column 1 in 'integer'"
    ]


def test_yaml_boolean_hint() -> None:
    doc = yaml.safe_load(
        """
        kind: Workflow
        version: tuvl/v2
        metadata: {name: wf}
        spec:
          agents:
            - id: pay
              description: d
              engine: tool
              tool: {use: http, method: POST, url: "https://x", idempotency: off}
              routes: {default: END}
        """
    )
    (error,) = _errors(doc)
    assert error.startswith("spec.agents[pay].tool.idempotency: Input should be 'auto' or 'none'")
    assert "YAML reads bare on/off/yes/no/true/false as booleans" in error


def test_yaml_flow_mapping_hint() -> None:
    with pytest.raises(yaml.YAMLError) as exc:
        yaml.safe_load("outputs: { route: enum[a, b] }")
    hint = yaml_error_hint(exc.value)
    assert hint is not None and '"enum[a, b]"' in hint
    assert yaml_error_hint(ValueError("other")) is None


# ── tuvl validate ───────────────────────────────────────────────────────────


def _validate(root: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    from tuvl_insight.cli.commands.validate import run_validation

    result, *_ = run_validation(root, echo=False)
    errors = [(i.file, i.message) for i in result.issues if i.severity == "error"]
    warnings = [(i.file, i.message) for i in result.issues if i.severity == "warning"]
    return errors, warnings


SUPPORT_FILES = {
    "models/order.yaml": """\
        kind: ModelDefinition
        metadata: {name: Order}
        spec:
          fields:
            - {name: id, type: uuid, primary_key: true}
            - {name: total, type: numeric, required: true}
            - {name: owner_id, type: uuid}
    """,
    "models/refund.yaml": """\
        kind: ModelDefinition
        metadata: {name: Refund}
        spec:
          fields:
            - {name: id, type: uuid, primary_key: true}
            - {name: order_id, type: uuid, required: true}
            - {name: amount, type: numeric, required: true}
            - {name: status, type: string}
    """,
    "llms/default.yaml": """\
        kind: AgentModel
        metadata: {name: default}
        spec: {model: gemini/gemini-2.5-flash}
    """,
    "llms/triage.yaml": """\
        kind: AgentModel
        metadata: {name: triage-classifier}
        spec: {type: decision, provider: llm, model: gemini/gemini-2.5-flash}
    """,
    "artifacts/support-voice.md": """\
        ---
        name: support-voice
        type: prompt
        version: 3
        ---
        You are a concise, friendly support agent.
    """,
    "artifacts/refund-reply-quality.yaml": """\
        kind: Artifact
        metadata: {name: refund-reply-quality, version: 2}
        spec: {type: judge, model: gemini/gemini-2.5-flash, rubric: States the amount.}
    """,
}


def full_project(root: Path) -> Path:
    write(root, "workflows/refund_flow.yaml", FIXTURE.read_text())
    for rel, text in SUPPORT_FILES.items():
        write(root, rel, text)
    write(
        root,
        "agents/issue_refund.py",
        "from tuvl import agent\n\n\n@agent('issue_refund')\nasync def issue_refund(inp, ctx):\n    return {}\n",
    )
    return root


def test_validate_accepts_a_v2_only_project(tmp_path: Path) -> None:
    full_project(tmp_path)
    write(tmp_path, "tests/small_refund.yaml", TEST_DOC)
    errors, warnings = _validate(tmp_path)
    assert errors == []
    assert not any("No Workflow YAML files" in m for _f, m in warnings)


def test_validate_reports_rule_codes_with_lines_and_profile(tmp_path: Path) -> None:
    from tuvl_insight.cli.commands.validate import run_validation

    full_project(tmp_path)
    doc = yaml.safe_load(FIXTURE.read_text())
    doc["spec"]["agents"][1]["decide"]["rules"][0]["when"] = "order.totl > 500"
    write(tmp_path, "workflows/refund_flow.yaml", yaml.safe_dump(doc, sort_keys=False))
    result, *_ = run_validation(tmp_path, echo=False)
    (issue,) = [i for i in result.issues if i.code == "V008"]
    assert issue.file.startswith("workflows/refund_flow.yaml:") and issue.line
    assert "spec.agents[triage_refund].decide.rules[0].when" in issue.message
    assert result.profiles["refund_flow"]["summary"].startswith("6 agents (1 off-spine)")


def test_validate_json_output(tmp_path: Path) -> None:
    import json

    from typer.testing import CliRunner

    from tuvl.cli import app

    full_project(tmp_path)
    ok = CliRunner().invoke(app, ["lint", "project", "--project-dir", str(tmp_path), "--json"])
    report = json.loads(ok.stdout)
    assert ok.exit_code == 0 and report["valid"] is True
    assert report["profiles"]["refund_flow"]["worst_case_tokens"] == 1500

    doc = yaml.safe_load(FIXTURE.read_text())
    del doc["spec"]["agents"][0]["routes"]["not_found"]
    write(tmp_path, "workflows/refund_flow.yaml", yaml.safe_dump(doc, sort_keys=False))
    bad = CliRunner().invoke(app, ["lint", "project", "--project-dir", str(tmp_path), "--json"])
    report = json.loads(bad.stdout)
    assert bad.exit_code == 1 and report["valid"] is False
    assert any(e["code"] == "V004" and "not_found" in e["message"] for e in report["errors"])


def test_validate_reports_v2_problems(tmp_path: Path) -> None:
    full_project(tmp_path)
    doc = yaml.safe_load(FIXTURE.read_text())
    doc["spec"]["agents"][0]["inputs"] = {"order_id": "uid"}
    write(tmp_path, "workflows/refund_flow.yaml", yaml.safe_dump(doc))
    write(
        tmp_path,
        "workflows/flow_style.yaml",
        "kind: Workflow\nversion: tuvl/v2\nspec: { a: enum[x, y] }\n",
    )
    write(tmp_path, "tests/unversioned.yaml", TEST_DOC.replace("version: tuvl/v2\n", ""))
    errors = dict(_validate(tmp_path)[0])
    assert "unknown type 'uid'" in errors["workflows/refund_flow.yaml"]
    assert "spec.agents[load_order].inputs" in errors["workflows/refund_flow.yaml"]
    assert 'quote types like "enum[a, b]"' in errors["workflows/flow_style.yaml"]
    assert errors["tests/unversioned.yaml"] == "Test documents need 'version: tuvl/v2'"
