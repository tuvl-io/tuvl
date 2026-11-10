"""Documented --json outputs (P3.7): the schema snapshot and real command output conforming to it."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl.cli.json_output import COMMANDS, json_schemas
from tuvl_insight.spec import parse_spec

SNAPSHOT = Path(__file__).parent / "snapshots" / "cli_json_schemas.json"
runner = CliRunner()


def test_schema_snapshot() -> None:
    current = json.loads(json.dumps(json_schemas(), sort_keys=True))
    assert current == json.loads(SNAPSHOT.read_text()), (
        "the --json output shapes changed — regenerate with "
        "`tuvl export schema --kind cli -o tests/cli/snapshots/cli_json_schemas.json` and document it"
    )


def _json(args: list[str]) -> dict[str, object]:
    result = runner.invoke(app, args)
    return json.loads(result.output[result.output.index("{") :])


def project(tmp_path: Path) -> Path:
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "w.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "Workflow",
                "version": "tuvl/v2",
                "metadata": {"name": "w"},
                "spec": {
                    "trigger": {"input": {"text": "str"}},
                    "agents": [
                        {
                            "id": "keep",
                            "description": "Keep.",
                            "engine": "pending",
                            "inputs": {"text": "str"},
                            "routes": {"default": "END", "error": "END"},
                        }
                    ],
                },
            }
        )
    )
    assert (
        runner.invoke(
            app, ["create", "spec", "keeping", "-d", str(tmp_path), "--workflow", "w"]
        ).exit_code
        == 0
    )
    spec_file = tmp_path / "specs" / "keeping.md"
    spec_file.write_text(
        spec_file.read_text().replace(
            "```yaml\nname: the happy path\ninput: { }",
            "```tuvl-example\nname: the happy path\ninput: { text: hi }",
        )
    )
    spec = parse_spec(spec_file, root=tmp_path)
    (tmp_path / "specs" / "keeping.tasks.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "TaskPlan",
                "version": "tuvl/v2",
                "metadata": {"name": "keeping", "spec": "specs/keeping.md", "spec_hash": spec.hash},
                "spec": {
                    "tasks": [
                        {
                            "id": "T1",
                            "title": "Keep",
                            "type": "workflow",
                            "target": {"workflow": "w"},
                        }
                    ]
                },
            }
        )
    )
    assert runner.invoke(app, ["generate", "code", "-d", str(tmp_path)]).exit_code == 0
    return tmp_path


def test_spec_new_parses_and_has_no_live_example(tmp_path: Path) -> None:
    assert (
        runner.invoke(app, ["create", "spec", "Refund Automation", "-d", str(tmp_path)]).exit_code
        == 0
    )
    spec = parse_spec(tmp_path / "specs" / "refund-automation.md", root=tmp_path)
    assert spec.front["workflow"] == "refund_automation" and spec.examples == []
    assert [s.anchor for s in spec.sections][:3] == ["intent", "data", "flows"]
    check = runner.invoke(app, ["lint", "spec", "-d", str(tmp_path), "--json"])
    assert check.exit_code == 0, check.output
    assert (
        runner.invoke(app, ["create", "spec", "refund-automation", "-d", str(tmp_path)]).exit_code
        == 1
    )


def test_command_outputs_match_their_schemas(tmp_path: Path) -> None:
    root = project(tmp_path)
    d = ["-d", str(root)]
    outputs = {
        "lint project": _json(["lint", "project", *d, "--json"]),
        "run tests": _json(["run", "tests", *d, "--json"]),
        "list tasks": _json(["list", "tasks", *d, "--json"]),
        "show drift": _json(["show", "drift", *d, "--json"]),
        "check system": _json(["check", "system", *d, "--json"]),
        "check storage": _json(["check", "storage", *d, "--json"]),
    }
    for command, payload in outputs.items():
        COMMANDS[command].model_validate(payload)
    assert outputs["run tests"]["passed"] is True
    fixture = root / "r.journal.json"
    fixture.write_text(
        json.dumps(
            {
                "format": "tuvl.journal/v1",
                "events": [],
                "checkpoints": [],
                "run": {
                    "workflow": "w",
                    "status": "completed",
                    "end_name": "default",
                    "input": {"text": "hi"},
                    "output": {"status": 200, "body": None},
                },
            }
        )
    )
    COMMANDS["replay runs"].model_validate(_json(["replay", "runs", str(fixture), *d, "--json"]))
