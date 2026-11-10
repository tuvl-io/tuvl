"""`tuvl generate ui-spec`: guidelines for building a UI on the implemented backend, with no model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl.core.system_config import load_system_config
from tuvl_insight.uispec import SPECS_DIR, check, collect, generate
from tuvl_insight.uispec.facts import describe, fields_of, sample

runner = CliRunner()


@pytest.fixture
def sample_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])
    res = runner.invoke(
        app, ["init", str(tmp_path / "demo"), "--sample", "--preset", "ollama", "-y", "--no-git"]
    )
    assert res.exit_code == 0, res.output
    return tmp_path / "demo"


def ui(project: Path, *args: str):
    return runner.invoke(app, ["generate", "ui-spec", "-d", str(project), "--no-tests", *args])


def test_init_scaffolds_the_folder_with_the_sample_already_documented(sample_project: Path) -> None:
    out = sample_project / SPECS_DIR
    assert {p.name for p in out.iterdir()} == {
        "README.md",
        "models.md",
        "triage_ticket.md",
        "ui-contract.json",
    }


def test_the_index_recommends_the_client_and_lists_the_workflows(sample_project: Path) -> None:
    text = (sample_project / SPECS_DIR / "README.md").read_text()
    assert "npm install @tuvl/client" in text and "npx tuvl-client generate" in text
    assert "[`triage_ticket`](triage_ticket.md)" in text and "`POST /api/tickets/triage`" in text
    assert "Call only the endpoints in these files" in text


def test_the_index_says_which_system_routes_a_ui_may_use(sample_project: Path) -> None:
    text = (sample_project / SPECS_DIR / "README.md").read_text()
    assert "## Backend surface" in text
    assert "`/auth/*`" in text and "`/api/runs/*`" in text
    assert (
        "`/admin/*`"
        not in text.split("## Backend surface")[1]
        .split("## Shared behaviour")[0]
        .split("Do not call")[0]
    )
    assert "Do not call `/api/_system`, `/admin` or `/api/artifacts`" in text
    assert "`tuvl dev`" in text  # production serves no OpenAPI by default


def test_the_surface_follows_the_project_settings(sample_project: Path) -> None:
    (sample_project / ".tuvl" / "system.yaml").write_text(
        "kind: SystemConfig\nspec:\n  api:\n    expose: [auth, admin]\n    docs: workflows\n"
    )
    assert ui(sample_project).exit_code == 0
    text = (sample_project / SPECS_DIR / "README.md").read_text()
    assert "`/admin/*`: engine administration" in text
    assert "workflow routes only" in text


def test_the_surface_says_whether_a_browser_on_another_origin_may_call(
    sample_project: Path,
) -> None:
    assert ui(sample_project).exit_code == 0
    text = (sample_project / SPECS_DIR / "README.md").read_text()
    assert "sends no CORS headers" in text and "spec.api.cors_origins" in text
    (sample_project / ".tuvl" / "system.yaml").write_text(
        'kind: SystemConfig\nspec:\n  api:\n    cors_origins: ["https://app.example.com"]\n'
    )
    load_system_config.cache_clear()
    assert ui(sample_project).exit_code == 0
    text = (sample_project / SPECS_DIR / "README.md").read_text()
    assert "from `https://app.example.com`" in text and "sends no CORS headers" not in text


def test_a_workflow_file_says_how_to_call_it_and_what_comes_back(sample_project: Path) -> None:
    text = (sample_project / SPECS_DIR / "triage_ticket.md").read_text()
    assert "`POST /api/tickets/triage`" in text and "`customer_email`" in text
    assert "| `200` | `END.dismissed` |" in text and "| `201` |" in text
    assert "`202`" in text and "`401`" in text and "`409`" in text and "`422`" in text
    assert "Charged twice" in text  # a real example from the acceptance cases
    assert (
        "Waiting for a person" in text and "`support_leads`" in text and "approvals:decide" in text
    )
    assert (
        'client.execute("triage_ticket"' in text and "RunHandle" in text and "@tuvl/client" in text
    )
    assert "| Which |" in text and "`POST /api/workflows/triage_ticket/runs`" in text
    assert text.startswith("---\nui_spec: workflow\nname: triage_ticket\ncontract_hash: ")


def test_models_with_crud_routes_get_their_own_file(sample_project: Path) -> None:
    text = (sample_project / SPECS_DIR / "models.md").read_text()
    assert "## Ticket" in text and "`GET /models/ticket/`" in text and "`ticket:read`" in text
    assert 'client.crud("ticket")' in text
    assert "`POST /models/ticket/`" not in text  # only the operations the model opted into


def test_the_contract_is_also_written_as_data(sample_project: Path) -> None:
    data = json.loads((sample_project / SPECS_DIR / "ui-contract.json").read_text())
    wf = data["workflows"][0]
    assert wf["name"] == "triage_ticket" and wf["http"]["path"] == "/api/tickets/triage"
    assert wf["human"][0]["group"] == "support_leads" and data["models"][0]["name"] == "Ticket"


def test_check_passes_when_current_and_names_what_is_stale_or_missing(sample_project: Path) -> None:
    assert ui(sample_project, "--check").exit_code == 0
    wf = sample_project / "workflows" / "triage_ticket.yaml"
    wf.write_text(wf.read_text().replace("/api/tickets/triage", "/api/tickets/intake"))
    res = ui(sample_project, "--check")
    assert res.exit_code == 1 and "triage_ticket.md is out of date" in res.output
    assert ui(sample_project).exit_code == 0
    assert "/api/tickets/intake" in (sample_project / SPECS_DIR / "triage_ticket.md").read_text()
    assert ui(sample_project, "--check").exit_code == 0
    (sample_project / SPECS_DIR / "models.md").unlink()
    res = ui(sample_project, "--check", "--json")
    assert res.exit_code == 1 and json.loads(res.output)["missing"] == ["models.md"]


def test_what_a_person_writes_below_the_marker_survives_regeneration(sample_project: Path) -> None:
    path = sample_project / SPECS_DIR / "triage_ticket.md"
    path.write_text(path.read_text() + "\nOur screen: /tickets/new, uses the shared form.\n")
    wf = sample_project / "workflows" / "triage_ticket.yaml"
    wf.write_text(wf.read_text().replace("/api/tickets/triage", "/api/tickets/intake"))
    assert ui(sample_project).exit_code == 0
    text = path.read_text()
    assert (
        "/api/tickets/intake" in text and "Our screen: /tickets/new, uses the shared form." in text
    )
    assert text.count("tuvl:notes") == 1


def test_a_workflow_with_a_pending_step_is_not_documented_until_it_is_built(
    sample_project: Path,
) -> None:
    wf = sample_project / "workflows" / "triage_ticket.yaml"
    doc = yaml.safe_load(wf.read_text())
    doc["spec"]["agents"][1] = {
        "id": "prioritise",
        "description": "to do",
        "engine": "pending",
        "inputs": doc["spec"]["agents"][1]["inputs"],
        "routes": {"urgent": "review", "normal": "store"},
    }
    wf.write_text(yaml.safe_dump(doc, sort_keys=False))
    (sample_project / SPECS_DIR / "triage_ticket.md").unlink()
    res = ui(sample_project)
    assert res.exit_code == 0 and "not ready yet" in res.output
    assert not (sample_project / SPECS_DIR / "triage_ticket.md").exists()
    assert "pending step `prioritise`" in (sample_project / SPECS_DIR / "README.md").read_text()
    assert ui(sample_project, "--all").exit_code == 0  # partial work can be written on request
    assert (sample_project / SPECS_DIR / "triage_ticket.md").exists()


def test_a_blank_project_gets_an_index_and_no_workflow_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])
    res = runner.invoke(app, ["init", str(tmp_path / "blank"), "-y", "--no-git"])
    assert res.exit_code == 0, res.output
    out = tmp_path / "blank" / SPECS_DIR
    assert {p.name for p in out.iterdir()} == {"README.md", "ui-contract.json"}
    assert "No workflow has an HTTP route yet." in (out / "README.md").read_text()


def test_secure_fields_are_flagged_and_unexposed_models_left_out(sample_project: Path) -> None:
    models = sample_project / "models"
    (models / "secret.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ModelDefinition",
                "version": "v1",
                "metadata": {"name": "Vault", "schema_version": "v1"},
                "spec": {
                    "tablename": "vaults",
                    "api": {"crud": ["list", "create"]},
                    "access": {"write_scope": "vault:admin", "write_groups": ["ops"]},
                    "fields": [
                        {
                            "name": "id",
                            "type": "uuid",
                            "primary_key": True,
                            "default": "uuid4",
                            "input": False,
                        },
                        {"name": "token", "type": "string", "required": True, "secure": True},
                    ],
                },
            }
        )
    )
    (models / "hidden.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ModelDefinition",
                "version": "v1",
                "metadata": {"name": "Hidden", "schema_version": "v1"},
                "spec": {
                    "tablename": "hidden",
                    "fields": [{"name": "id", "type": "uuid", "primary_key": True}],
                },
            }
        )
    )
    assert ui(sample_project).exit_code == 0
    text = (sample_project / SPECS_DIR / "models.md").read_text()
    assert "## Vault" in text and "`vault:admin`" in text and "`ops`" in text
    assert "secure: do not log, echo or cache" in text and "## Hidden" not in text


def test_the_audit_ledger_records_the_generation(sample_project: Path) -> None:
    from tuvl_insight.audit import read_events

    assert ui(sample_project).exit_code == 0
    events = [e for e in read_events(sample_project) if e["kind"] == "ui.spec.generated"]
    assert events and events[-1]["command"] == "generate ui-spec"


def test_generate_is_idempotent(sample_project: Path) -> None:
    again = generate(sample_project, run_tests=False)
    assert again.written == [] and set(again.unchanged) >= {"README.md", "triage_ticket.md"}
    assert check(sample_project, run_tests=False).clean


def test_facts_come_from_the_loaded_project(sample_project: Path) -> None:
    facts = collect(sample_project, run_tests=False)
    wf = facts["workflows"][0]
    assert wf["ready"] and wf["auth"] == {"public": False, "scope": None, "group": None}
    assert [e for r in wf["responses"] for e in r["ends"]] == ["END.dismissed", "END", "END.failed"]


def test_schema_helpers() -> None:
    assert describe({"enum": ["a", "b"]}) == "enum: a | b"
    assert (
        describe({"anyOf": [{"format": "uuid", "type": "string"}, {"type": "null"}]})
        == "uuid | null"
    )
    assert describe({"type": "array", "items": {"type": "integer"}}) == "integer[]"
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert fields_of(schema) == [{"name": "n", "type": "integer", "required": True}]
    assert sample(schema) == {"n": 1} and sample({"enum": ["x"]}) == "x"


def test_an_upload_trigger_is_documented_as_multipart() -> None:
    from tuvl_insight.uispec.render import _upload_md

    text = _upload_md(
        {
            "name": "intake",
            "upload": {
                "files": ["invoice"],
                "max_bytes": 1000,
                "max_files": 2,
                "accept": ["application/pdf"],
            },
        }
    )
    assert "multipart/form-data" in text and "`invoice`" in text and "`application/pdf`" in text
    assert "`413`" in text and "`415`" in text
    assert 'client.execute("intake", { invoice: fileInput.files[0]' in text
