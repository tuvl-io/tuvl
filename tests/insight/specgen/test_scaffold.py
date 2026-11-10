import yaml

from tuvl.contract.schema import WorkflowDocument
from tuvl_insight.cli.commands.init import _SAMPLE_TRIAGE_SPEC
from tuvl_insight.codegen import scaffold
from tuvl_insight.spec.grammar import parse_structure
from tuvl_insight.spec.parse import parse_text
from tuvl_insight.spec.verify import verify_spec

SPEC = """---
name: refunds
workflow: refund_requests
---
# Data
- **Refund** (new) — `id` (uuid), `order_number` (string; required; unique), `amount` (numeric; required), `status` (enum: approved | waiting | rejected; required; default waiting), `note` (text), `owner` (uuid → Account), `ssn` (string; secure), `created_at` (timestamptz)
- **Account** (existing) — `id` (uuid), `name` (string)

# Flows

## Flow: refund_requests
Trigger: POST /refunds · group finance
Input: `text` (str)
Ends: default 201 · failed 500

1. `read` — llm default · in `text` · out `order_number` (str), `amount` (float) · budget 800 tok, 20s
   - parse_error, timeout, budget_exceeded → END.failed
2. `store` — tool db create Refund · in `order_number`, `amount` · out `refund` (Refund)
   - conflict → END.failed
   - default → END
"""


def load(text: str = SPEC):
    return parse_text(text, path="specs/x.md", default_name="x"), parse_structure(text)


def test_a_new_model_follows_the_conventions_of_every_model() -> None:
    _, structure = load()
    doc = scaffold.model_definition(structure.data["Refund"], structure)
    assert doc["kind"] == "ModelDefinition" and doc["metadata"]["name"] == "Refund"
    assert doc["spec"]["tablename"] == "refunds"
    fields = {f["name"]: f for f in doc["spec"]["fields"]}
    assert fields["id"] == {
        "name": "id",
        "type": "uuid",
        "primary_key": True,
        "default": "uuid4",
        "input": False,
    }
    assert fields["order_number"] == {
        "name": "order_number",
        "type": "string",
        "required": True,
        "unique": True,
    }
    assert (
        fields["status"]["enum_values"] == ["approved", "waiting", "rejected"]
        and fields["status"]["default"] == "waiting"
    )
    assert fields["ssn"]["secure"] is True and fields["note"]["type"] == "text"
    assert fields["owner"]["foreign_key"] == "accounts.id"
    assert fields["created_at"] == {"name": "created_at", "type": "timestamptz", "input": False}
    assert list(fields)[0] == "id" and list(fields).count("id") == 1


def test_an_id_is_added_when_the_spec_has_none() -> None:
    text = SPEC.replace("`id` (uuid), `order_number`", "`order_number`").replace(
        "`created_at` (timestamptz)", "`updated_at` (timestamptz)"
    )
    _, structure = load(text)
    names = [
        f["name"]
        for f in scaffold.model_definition(structure.data["Refund"], structure)["spec"]["fields"]
    ]
    assert names[0] == "id" and names[-1] == "created_at" and "updated_at" in names


def test_table_names() -> None:
    assert [
        scaffold.table_name(n)
        for n in ("Invoice", "Category", "Address", "SupportTicket", "Day", "Box")
    ] == [
        "invoices",
        "categories",
        "addresses",
        "support_tickets",
        "days",
        "boxes",
    ]


def test_the_skeleton_is_a_valid_workflow_of_pending_steps() -> None:
    spec, structure = load()
    raw = scaffold.workflow_skeleton(spec, structure.flows["refund_requests"], structure)
    doc = WorkflowDocument.model_validate(raw)
    assert [a.engine for a in doc.spec.agents] == ["pending", "pending"]
    assert doc.metadata.required_scope == "finance"
    assert doc.spec.trigger.http.path == "/refunds" and doc.spec.trigger.input == {"text": "str"}
    read, store = doc.spec.agents
    assert read.budget.max_tokens == 800 and read.budget.timeout == "20s"
    assert store.inputs == {
        "order_number": "str",
        "amount": "float",
    }  # types come from what produced them
    assert (
        dict(store.routes)["conflict"] == "END.failed" and dict(read.routes)["default"] == "store"
    )
    assert {k: v.status for k, v in doc.spec.outputs.items()} == {"default": 201, "failed": 500}
    assert raw["spec"]["models"] == ["Refund", "Account"] or "Refund" in raw["spec"]["models"]


def test_the_skeleton_satisfies_verify_except_for_being_pending() -> None:
    spec, structure = load()
    raw = scaffold.workflow_skeleton(spec, structure.flows["refund_requests"], structure)
    docs = {"refund_requests": WorkflowDocument.model_validate(raw)}
    models = {"Refund": scaffold.model_definition(structure.data["Refund"], structure)["spec"]}
    result = verify_spec(spec, SPEC, docs, models)
    assert [f for f in result.findings if f.severity == "error"] == []
    assert {f.code for f in result.findings} <= {
        "R002",
        "R006",
    }  # pending steps; Account is not in this project


def test_the_sample_projects_spec_gives_a_valid_skeleton() -> None:
    """The ticket-triage spec has llm, code, human and tool steps and per-signal outputs."""
    spec, structure = load(_SAMPLE_TRIAGE_SPEC)
    for flow in structure.flows.values():
        doc = WorkflowDocument.model_validate(scaffold.workflow_skeleton(spec, flow, structure))
        assert all(a.engine == "pending" for a in doc.spec.agents)
        result = verify_spec(spec, _SAMPLE_TRIAGE_SPEC, {flow.name: doc}, {})
        assert [f for f in result.findings if f.severity == "error" and f.code != "R006"] == []


def test_write_scaffold_creates_only_what_is_missing(tmp_path) -> None:
    spec, structure = load()
    made = scaffold.write_scaffold(tmp_path, spec, structure)
    assert sorted(made) == ["models/refund.yaml", "workflows/refund_requests.yaml"]
    yaml.safe_load((tmp_path / "models" / "refund.yaml").read_text())
    (tmp_path / "workflows" / "refund_requests.yaml").write_text(
        "# hand edited\n" + (tmp_path / "workflows" / "refund_requests.yaml").read_text()
    )
    assert scaffold.write_scaffold(tmp_path, spec, structure) == []
    assert (tmp_path / "workflows" / "refund_requests.yaml").read_text().startswith("# hand edited")
    # a model that exists under another file name is not written again
    other = tmp_path / "other"
    (other / "models").mkdir(parents=True)
    (other / "models" / "somewhere.yaml").write_text(
        "kind: ModelDefinition\nmetadata: {name: Refund}\nspec: {fields: []}\n"
    )
    assert "models/refund.yaml" not in scaffold.write_scaffold(other, spec, structure)
