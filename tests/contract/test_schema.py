"""tuvl 2.0 document schema (spec §7–§13, §17.4, §18.1)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from tuvl.contract.schema import (
    DecisionModelSpec,
    JudgeSpec,
    RulesSpec,
    TaskPlanDocument,
    TestDocument,
    WorkflowDocument,
    duration_seconds,
    workflow_json_schema,
)

HERE = Path(__file__).parent
GOLDEN = HERE / "golden" / "workflow.schema.json"


def refund_flow() -> dict[str, Any]:
    return yaml.safe_load((HERE / "fixtures" / "refund_flow.yaml").read_text())


def workflow(*agents: dict[str, Any], **spec: Any) -> dict[str, Any]:
    return {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "wf"},
        "spec": {"agents": list(agents), **spec},
    }


def agent(engine: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": "a",
        "description": "does a thing",
        "engine": engine,
        "routes": {"default": "END"},
        **fields,
    }


def load(doc: dict[str, Any]) -> WorkflowDocument:
    return WorkflowDocument.model_validate(doc)


def error_of(doc: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as exc:
        load(doc)
    return str(exc.value)


# ── The spec's own example ──────────────────────────────────────────────────


def test_spec_appendix_a_validates() -> None:
    doc = load(refund_flow())
    spec = doc.spec
    assert [a.engine for a in spec.agents] == ["tool", "decide", "human", "code", "tool", "llm"]
    assert spec.entry == "load_order"
    assert spec.outputs["failed"].rollback is True
    assert spec.trigger.mcp is not None and spec.trigger.mcp.enabled
    decide = spec.agents[1]
    assert decide.engine == "decide"
    assert decide.decide.model is not None and decide.decide.model.min_confidence == 0.8
    human = spec.agents[2]
    assert set(human.routes) == {"true", "false", "expired", "error"}


def test_defaults() -> None:
    doc = load(workflow(agent("pending")))
    assert doc.spec.entry == "a"
    assert doc.spec.transaction == "per_agent"
    assert set(doc.spec.outputs) == {"default"}
    assert doc.spec.policy.max_depth == 2
    assert doc.enabled is True


def test_start_overrides_entry() -> None:
    doc = load(workflow(agent("pending"), {**agent("pending"), "id": "b"}, start="b"))
    assert doc.spec.entry == "b"


# ── One valid agent per engine ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "a",
    [
        agent("pending", outputs={"team": "enum[billing, technical]"}),
        agent("code", code={"run": "tuvl.data_search"}, effect="read"),
        agent(
            "tool",
            tool={"use": "db", "model": "Order", "op": "list", "limit": 10, "order_by": ["-id"]},
        ),
        agent(
            "tool",
            tool={"use": "db", "model": "Order", "op": "upsert", "values": {"id": "{{ id }}"}},
        ),
        agent(
            "tool",
            tool={
                "use": "http",
                "method": "POST",
                "url": "https://x.test",
                "body": {"a": 1},
                "idempotency": "auto",
            },
            effect="destructive",
        ),
        agent("tool", tool={"use": "mcp", "server": "artifact://zendesk@3", "name": "add_comment"}),
        agent(
            "llm",
            llm={"model": "default", "prompt": "Summarise."},
            guardrails={"output": ["artifact://judge-x@1"]},
        ),
        agent("decide", decide={"rules": [{"when": "amount > 1", "then": "big"}]}),
        agent("decide", decide={"rules": "artifact://refund-rules@4"}),
        agent(
            "decide",
            decide={
                "rules": [{"when": "true", "then": "x"}],
                "model": {"ref": "triage", "min_confidence": 0.7},
                "shadow": True,
            },
        ),
        agent(
            "loop",
            loop={
                "model": "default",
                "steering": "artifact://support-steering@4",
                "tools": [
                    {"agent": "lookup_order"},
                    {"workflow": "issue_refund", "approval": {"when": "args.amount > 200"}},
                    {"mcp": "artifact://zendesk@3", "allow": ["add_comment"], "max_calls": 3},
                    {"agent": "wipe", "approval": "required"},
                ],
                "supervisor": {
                    "rules": [
                        {
                            "when": "tool_repeated",
                            "tool": "lookup_order",
                            "count": 3,
                            "then": "abort",
                        },
                        {"when": "budget_fraction", "gt": 0.9, "then": "steer"},
                        {"when": "iteration_reached", "gte": 6, "then": "pause"},
                        {"when": "effect_count", "effect": "write", "count": 2, "then": "pause"},
                    ],
                    "judge": "artifact://resolution-judge@1",
                },
            },
            budget={
                "max_iterations": 8,
                "max_tokens": 40000,
                "max_tool_calls": 12,
                "timeout": "2m",
            },
        ),
        agent(
            "human",
            outputs={"approved": "bool", "note": "str?"},
            human={
                "title": "Refund {{ order.id }}",
                "form": {"approved": {"label": "Approve?", "widget": "toggle"}},
                "expires": "48h",
                "route_on": "approved",
            },
        ),
    ],
    ids=lambda a: a["engine"],
)
def test_valid_agents(a: dict[str, Any]) -> None:
    load(workflow(a))


# ── Rejections ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda a: a.update(engine="llm", tool={"use": "db"}), "llm.llm\n  Field required"),
        (lambda a: a.update(engine="robot"), "does not match any of the expected tags"),
        (lambda a: a.update(colour="red"), "Extra inputs are not permitted"),
        (lambda a: a.update(id="Bad-Id"), "String should match pattern"),
        (lambda a: a.update(description=""), "at least 1 character"),
        (lambda a: a.pop("routes"), "routes\n  Field required"),
        (lambda a: a.update(routes={"default": "End"}), "String should match pattern"),
        (lambda a: a.update(inputs={"n": "integer"}), "did you mean 'int'"),
        (
            lambda a: a.update(outputs={"v": {"type": "int", "required": True}}),
            "Extra inputs are not permitted",
        ),
        (lambda a: a.update(inputs={"2bad": "int"}), "invalid field name"),
        (lambda a: a.update(hooks=["audit-log"]), "String should match pattern"),
        (lambda a: a.update(budget={"timeout": "30 seconds"}), "String should match pattern"),
        (lambda a: a.update(budget={"retry": {"attempts": 2, "on": ["timeout"]}}), "Extra inputs"),
        (lambda a: a.update(budget={"retry": {"attempts": 0}}), "greater than or equal to 1"),
    ],
)
def test_agent_shape_errors(mutate: Any, fragment: str) -> None:
    a = agent("pending")
    mutate(a)
    assert fragment in error_of(workflow(a))


@pytest.mark.parametrize(
    ("a", "fragment"),
    [
        (agent("code", code={"run": "bad-name"}), "String should match pattern"),
        (
            agent("code", code={"run": "x"}, effect="maybe"),
            "Input should be 'read', 'write' or 'destructive'",
        ),
        (
            agent("tool", tool={"use": "db", "model": "order", "op": "list"}),
            "String should match pattern",
        ),
        (
            agent("tool", tool={"use": "db", "model": "Order", "op": "read"}),
            "op 'read' needs 'where'",
        ),
        (
            agent("tool", tool={"use": "db", "model": "Order", "op": "create"}),
            "op 'create' needs 'values'",
        ),
        (
            agent(
                "tool",
                tool={
                    "use": "db",
                    "model": "Order",
                    "op": "delete",
                    "where": {"id": 1},
                    "values": {"a": 1},
                },
            ),
            "does not take 'values'",
        ),
        (
            agent(
                "tool",
                tool={"use": "db", "model": "Order", "op": "read", "where": {"id": 1}, "limit": 5},
            ),
            "only apply to op 'list'",
        ),
        (
            agent(
                "tool", tool={"use": "http", "method": "GET", "url": "https://x", "body": {"a": 1}}
            ),
            "GET requests do not take a body",
        ),
        (
            agent(
                "tool",
                tool={"use": "http", "method": "POST", "url": "https://x", "idempotency": "off"},
            ),
            "Input should be 'auto' or 'none'",
        ),
        (agent("tool", tool={"use": "ftp"}), "does not match any of the expected tags"),
        (
            agent("tool", tool={"use": "mcp", "server": "zendesk", "name": "x"}),
            "String should match pattern",
        ),
        (agent("llm", llm={"model": "default"}), "needs 'system' and/or 'prompt'"),
        (agent("decide", decide={}), "needs 'rules', a 'model', or both"),
        (agent("decide", decide={"rules": [{"when": "x >", "then": "a"}]}), "expected a value"),
        (
            agent("decide", decide={"rules": [{"when": "x", "then": "a"}], "shadow": True}),
            "'shadow' needs a 'model'",
        ),
        (
            agent("decide", decide={"model": {"ref": "m", "min_confidence": 0.5}, "shadow": True}),
            "'shadow' needs 'rules'",
        ),
        (agent("decide", decide={"model": {"ref": "m"}}), "min_confidence\n  Field required"),
        (
            agent("decide", decide={"model": {"ref": "m", "min_confidence": 1.5}}),
            "less than or equal to 1",
        ),
        (agent("loop", loop={"model": "m", "steering": "s", "tools": []}), "at least 1 item"),
        (
            agent(
                "loop", loop={"model": "m", "steering": "s", "tools": [{"mcp": "artifact://z@1"}]}
            ),
            "allow",
        ),
        (
            agent(
                "loop",
                loop={
                    "model": "m",
                    "steering": "s",
                    "tools": [{"agent": "x", "approval": "maybe"}],
                },
            ),
            "approval",
        ),
        (
            agent(
                "loop",
                loop={
                    "model": "m",
                    "steering": "s",
                    "tools": [{"agent": "x", "approval": {"when": "args.a >"}}],
                },
            ),
            "expected a value",
        ),
        (
            agent(
                "loop",
                loop={"model": "m", "steering": "s", "tools": [{"agent": "x"}], "supervisor": {}},
            ),
            "needs 'rules', a 'judge', or both",
        ),
        (
            agent(
                "loop",
                loop={
                    "model": "m",
                    "steering": "s",
                    "tools": [{"agent": "x"}],
                    "supervisor": {"rules": [{"when": "budget_fraction", "then": "abort"}]},
                },
            ),
            "needs 'gt'",
        ),
        (
            agent(
                "loop",
                loop={
                    "model": "m",
                    "steering": "s",
                    "tools": [{"agent": "x"}],
                    "supervisor": {
                        "rules": [
                            {"when": "iteration_reached", "gte": 3, "count": 2, "then": "abort"}
                        ]
                    },
                },
            ),
            "does not take 'count'",
        ),
        (agent("human", human={"title": "t", "expires": "2 days"}), "String should match pattern"),
        (agent("human", human={"title": "t", "form": {"a": {"widget": "slider"}}}), "widget"),
    ],
)
def test_engine_param_errors(a: dict[str, Any], fragment: str) -> None:
    assert fragment in error_of(workflow(a))


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ({"agents": []}, "at least 1 item"),
        ({"trigger": {"schedule": "every hour"}}, "5-field cron"),
        ({"trigger": {"schedule": "61 * * * *"}}, "invalid cron expression"),
        ({"trigger": {"http": {"path": "api/x"}}}, "String should match pattern"),
        ({"trigger": {"http": {"path": "/x", "method": "OPTIONS"}}}, "method"),
        ({"trigger": {"input": {"n": "number"}}}, "unknown type 'number'"),
        ({"transaction": "per_step"}, "per_agent"),
        ({"outputs": {"Failed": {}}}, "String should match pattern"),
        ({"outputs": {"failed": {"status": 99}}}, "greater than or equal to 100"),
        ({"models": ["order"]}, "String should match pattern"),
        ({"policy": {"max_depth": 9}}, "less than or equal to 5"),
        ({"policy": {"require_human_before": ["read"]}}, "'write' or 'destructive'"),
    ],
)
def test_workflow_spec_errors(spec: dict[str, Any], fragment: str) -> None:
    doc = workflow(agent("pending"))
    doc["spec"].update(spec)
    assert fragment in error_of(doc)


def test_1x_documents_are_rejected() -> None:
    doc = workflow(agent("pending"))
    del doc["version"]
    assert "version\n  Field required" in error_of(doc)
    doc = workflow(agent("pending"))
    doc["spec"] = {"steps": [{"id": "a", "kind": "Functional"}]}
    assert "Extra inputs are not permitted" in error_of(doc)


def test_unquoted_boolean_route_keys_are_normalised() -> None:
    doc = yaml.safe_load(
        """
kind: Workflow
version: tuvl/v2
metadata: {name: wf}
spec:
  agents:
    - id: ask
      description: ask
      engine: human
      outputs: {ok: bool}
      human: {title: "Approve?", route_on: ok}
      routes:
        true: END
        false: END.rejected
"""
    )
    assert set(load(doc).spec.agents[0].routes) == {"true", "false"}


@pytest.mark.parametrize(
    ("text", "seconds"), [("250ms", 0.25), ("30s", 30), ("1.5m", 90), ("2h", 7200), ("7d", 604800)]
)
def test_duration_seconds(text: str, seconds: float) -> None:
    assert duration_seconds(text) == seconds


def test_duration_seconds_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        duration_seconds("soon")


# ── Structured artifacts and AgentModel ─────────────────────────────────────


def test_judge_rules_and_decision_specs() -> None:
    JudgeSpec.model_validate({"type": "judge", "model": "m", "rubric": "r"})
    RulesSpec.model_validate({"type": "rules", "rules": [{"when": "a > 1", "then": "x"}]})
    DecisionModelSpec.model_validate(
        {"type": "decision", "provider": "laya", "model": "auto", "api_base": "http://laya:8000"}
    )
    with pytest.raises(ValidationError, match="needs api_base"):
        DecisionModelSpec.model_validate(
            {"type": "decision", "provider": "jev", "model": "jev-1.13.0"}
        )
    with pytest.raises(ValidationError, match="laya model must be one of"):
        DecisionModelSpec.model_validate(
            {"type": "decision", "provider": "laya", "model": "convai/laya", "api_base": "http://x"}
        )
    with pytest.raises(ValidationError, match="must contain 'pass' and 'fail'"):
        JudgeSpec.model_validate(
            {"type": "judge", "model": "m", "rubric": "r", "verdict": ["pass"]}
        )
    with pytest.raises(ValidationError, match="at least 1 item"):
        RulesSpec.model_validate({"type": "rules", "rules": []})
    with pytest.raises(ValidationError, match="provider"):
        DecisionModelSpec.model_validate({"type": "decision", "provider": "openai", "model": "x"})
    DecisionModelSpec.model_validate(
        {
            "type": "decision",
            "provider": "llm",
            "model": "openai/laya",
            "api_base": "http://laya.internal/v1",
            "api_key": "${LAYA_API_KEY}",
        }
    )
    with pytest.raises(ValidationError, match="ENV_VAR"):
        DecisionModelSpec.model_validate(
            {"type": "decision", "provider": "llm", "model": "m", "api_key": "sk-live-123"}
        )


def test_artifact_loader_validates_judge_and_rules() -> None:
    from tuvl.core.artifacts.loader import _register_artifact_from_dict
    from tuvl.core.artifacts.registry import ARTIFACT_REGISTRY

    good = {
        "kind": "Artifact",
        "metadata": {"name": "reply-quality", "version": 2},
        "spec": {"type": "judge", "model": "m", "rubric": "be precise"},
    }
    try:
        _register_artifact_from_dict(good)
        assert ARTIFACT_REGISTRY["reply-quality"].type == "judge"
        bad = copy.deepcopy(good)
        bad["metadata"]["name"] = "bad-judge"
        del bad["spec"]["rubric"]
        with pytest.raises(ValueError, match="(?s)Artifact 'bad-judge'.*rubric"):
            _register_artifact_from_dict(bad)
        with pytest.raises(ValueError, match="Artifact 'bad-rules'"):
            _register_artifact_from_dict(
                {
                    "kind": "Artifact",
                    "metadata": {"name": "bad-rules"},
                    "spec": {"type": "rules", "rules": [{"when": "x >", "then": "a"}]},
                }
            )
    finally:
        ARTIFACT_REGISTRY.pop("reply-quality", None)


def test_agent_model_type_validation() -> None:
    from tuvl.core.models.agent_models import AGENT_MODEL_REGISTRY, _register_agent_model_from_dict

    try:
        _register_agent_model_from_dict(
            {
                "kind": "AgentModel",
                "metadata": {"name": "triage"},
                "spec": {
                    "type": "decision",
                    "provider": "jev",
                    "model": "jev-1.13.0",
                    "api_base": "https://jev.example/",
                },
            }
        )
        assert AGENT_MODEL_REGISTRY["triage"]["provider"] == "jev"
        _register_agent_model_from_dict(
            {"kind": "AgentModel", "metadata": {"name": "plain"}, "spec": {"model": "openai/x"}}
        )
        with pytest.raises(ValueError, match="(?s)AgentModel 'bad'.*provider"):
            _register_agent_model_from_dict(
                {
                    "kind": "AgentModel",
                    "metadata": {"name": "bad"},
                    "spec": {"type": "decision", "model": "x"},
                }
            )
        with pytest.raises(ValueError, match="must be 'llm' or 'decision'"):
            _register_agent_model_from_dict(
                {"kind": "AgentModel", "metadata": {"name": "odd"}, "spec": {"type": "vision"}}
            )
    finally:
        for name in ("triage", "plain"):
            AGENT_MODEL_REGISTRY.pop(name, None)


# ── Test and TaskPlan ───────────────────────────────────────────────────────


def test_test_document() -> None:
    doc = TestDocument.model_validate(
        {
            "kind": "Test",
            "version": "tuvl/v2",
            "metadata": {"name": "small refund", "generated_from": "specs/refunds.md#acceptance"},
            "spec": {
                "workflow": "refund_flow",
                "input": {"order_id": "o1"},
                "mocks": {
                    "load_order": {"outputs": {"order": {"total": 40}}},
                    "triage": {"signal": "auto_refund"},
                },
                "expect": {
                    "end": "default",
                    "path": ["load_order", "triage"],
                    "decisions": {"triage": {"route": "auto_refund", "source": "rule#1"}},
                    "assertions": ["output.body.amount == 40"],
                    "judge": [{"judge": "artifact://reply-quality@2", "target": "{{ text }}"}],
                },
            },
        }
    )
    assert doc.spec.mocks["triage"].signal == "auto_refund"
    bad = {
        "kind": "Test",
        "version": "tuvl/v2",
        "metadata": {"name": "t"},
        "spec": {"workflow": "w", "mocks": {"a": {}}},
    }
    with pytest.raises(ValidationError, match="needs 'outputs', 'signal', or both"):
        TestDocument.model_validate(bad)
    bad["spec"] = {"workflow": "w", "expect": {"assertions": ["a >"]}}
    with pytest.raises(ValidationError, match="expected a value"):
        TestDocument.model_validate(bad)
    bad["spec"] = {"workflow": "w", "expect": {"decisions": {"t": {"source": "magic"}}}}
    with pytest.raises(ValidationError, match="String should match pattern"):
        TestDocument.model_validate(bad)


def test_task_plan_document() -> None:
    base = {
        "kind": "TaskPlan",
        "version": "tuvl/v2",
        "metadata": {"name": "refunds", "spec": "specs/refunds.md", "spec_hash": "9f2c"},
    }
    doc = TaskPlanDocument.model_validate(
        {
            **base,
            "spec": {
                "tasks": [
                    {
                        "id": "T3",
                        "title": "Triage refunds",
                        "type": "agent",
                        "spec_ref": {"anchor": "flows", "hash": "41ad"},
                        "target": {"workflow": "refund_flow", "agent": "triage"},
                        "depends_on": ["T1"],
                    },
                    {"id": "T9", "title": "Get payments key", "type": "manual", "status": "done"},
                ]
            },
        }
    )
    assert doc.spec.tasks[0].status == "derived"
    with pytest.raises(ValidationError, match="effort"):  # no estimates
        TaskPlanDocument.model_validate(
            {
                **base,
                "spec": {"tasks": [{"id": "T1", "title": "x", "type": "manual", "effort": {}}]},
            }
        )
    with pytest.raises(ValidationError, match="only manual tasks take a manual status"):
        TaskPlanDocument.model_validate(
            {
                **base,
                "spec": {"tasks": [{"id": "T1", "title": "x", "type": "agent", "status": "done"}]},
            }
        )


# ── JSON Schema ─────────────────────────────────────────────────────────────


def test_workflow_json_schema_snapshot() -> None:
    """Insight forms and editor integrations consume this schema: changes must
    be deliberate. Regenerate with UPDATE_GOLDEN=1."""
    current = json.dumps(workflow_json_schema(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(current)
    assert GOLDEN.read_text() == current, (
        "workflow JSON Schema changed — rerun with UPDATE_GOLDEN=1 if intended"
    )


def test_json_schema_describes_engines() -> None:
    schema = workflow_json_schema()
    agent_union = schema["$defs"]["WorkflowSpec"]["properties"]["agents"]["items"]
    assert set(agent_union["discriminator"]["mapping"]) == {
        "pending",
        "code",
        "tool",
        "decide",
        "llm",
        "loop",
        "human",
        "map",
        "parallel",
    }
    assert schema["additionalProperties"] is False


@pytest.mark.requires_insight
def test_schema_export_cli(tmp_path: Path) -> None:
    from tuvl.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["export", "schema"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["$id"].endswith("/workflow.json")
    out = tmp_path / "all.json"
    result = runner.invoke(app, ["export", "schema", "--kind", "all", "--out", str(out)])
    assert result.exit_code == 0
    assert set(json.loads(out.read_text())) == {"Workflow", "Test", "TaskPlan"}
    assert runner.invoke(app, ["export", "schema", "--kind", "Nope"]).exit_code == 2


def test_a_collection_loop_tool_is_named_for_the_model() -> None:
    from tuvl.contract.schema import CollectionToolRef

    assert CollectionToolRef(collection="kb").tool_name == "search_kb"
    assert CollectionToolRef(collection="Support-Docs.v2").tool_name == "search_support_docs_v2"
    assert CollectionToolRef(collection="kb", name="find_docs").tool_name == "find_docs"
    assert CollectionToolRef(collection="kb").top_k == 5
    with pytest.raises(ValidationError):
        CollectionToolRef(collection="kb", top_k=0)
    loop = load(
        workflow(
            agent(
                "loop",
                loop={"model": "m", "steering": "s", "tools": [{"collection": "kb", "top_k": 2}]},
                budget={
                    "max_iterations": 3,
                    "max_tokens": 100,
                    "max_tool_calls": 3,
                    "timeout": "1m",
                },
                routes={"done": "END"},
            )
        )
    )
    assert loop.spec.agents[0].loop.tools[0].collection == "kb"
