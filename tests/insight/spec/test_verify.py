"""``tuvl spec verify``: the sample conforms to its spec; each divergence is reported with its rule."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tuvl.contract.schema import WorkflowDocument
from tuvl_insight.spec import parse_text
from tuvl_insight.spec.verify import model_definitions, verify_spec


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("verify")
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


def _verify(project: Path, edit=None, models_edit=None):
    raw = yaml.safe_load((project / "workflows" / "triage_ticket.yaml").read_text())
    if edit:
        edit(raw["spec"])
    defs = model_definitions(project)
    if models_edit:
        models_edit(defs)
    text = (project / "specs" / "triage-ticket.md").read_text()
    spec = parse_text(text, path="specs/triage-ticket.md")
    return verify_spec(spec, text, {"triage_ticket": WorkflowDocument.model_validate(raw)}, defs)


def _agent(spec: dict, agent_id: str) -> dict:
    return next(a for a in spec["agents"] if a["id"] == agent_id)


def test_the_sample_conforms(project: Path) -> None:
    result = _verify(project)
    assert not result.errors, [(f.code, f.message) for f in result.errors]
    assert result.summary() == {"steps": 4, "implemented": 4, "mismatched": 0, "exercised": 0}


@pytest.mark.parametrize(
    ("code", "edit"),
    [
        ("R001", lambda s: s["trigger"]["http"].update(path="/api/tickets")),
        ("R001", lambda s: s["trigger"]["input"].pop("customer_email")),
        ("R002", lambda s: _agent(s, "store")["tool"].update(op="upsert")),
        (
            "R003",
            lambda s: _agent(s, "classify")["outputs"].update(urgency="enum[low, medium, high]"),
        ),
        ("R004", lambda s: _agent(s, "review")["routes"].update({"false": "store"})),
        ("R005", lambda s: s["outputs"]["dismissed"].update(status=204)),
        ("R007", lambda s: _agent(s, "classify")["budget"].update(max_tokens=900)),
        ("R008", lambda s: _agent(s, "review")["human"].update(expires="8h")),
    ],
)
def test_each_divergence_is_reported(project: Path, code: str, edit) -> None:
    result = _verify(project, edit)
    assert code in {f.code for f in result.errors}, [(f.code, f.message) for f in result.findings]


def test_a_model_field_flag_divergence_is_r006(project: Path) -> None:
    def drop_secure(defs: dict) -> None:
        for f in defs["Ticket"]["fields"]:
            if f["name"] == "customer_email":
                f["secure"] = False

    result = _verify(project, models_edit=drop_secure)
    assert any(f.code == "R006" and "secure" in f.message for f in result.errors)


def test_a_pending_agent_is_unfinished_work_not_a_mismatch(project: Path) -> None:
    def to_pending(spec: dict) -> None:
        a = _agent(spec, "store")
        for key in ("tool",):
            a.pop(key)
        a["engine"] = "pending"

    result = _verify(project, to_pending)
    assert not result.errors, [(f.code, f.message) for f in result.errors]
    assert any(f.code == "R002" and "pending" in f.message for f in result.warnings)
    assert [s.impl for s in result.flows[0].steps] == ["ok", "ok", "ok", "pending"]


def test_cli_verify_runs_the_tests_and_is_strict_clean(project: Path) -> None:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("TUVL_", "GEMINI_", "POSTGRES_"))
    }
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tuvl.cli import app; app()",
            "verify",
            "spec",
            "--strict",
            "--json",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    report = json.loads(done.stdout)
    assert done.returncode == 0 and report["ok"] is True, done.stdout + done.stderr
    steps = {s["step"]: s for s in report["specs"][0]["flows"][0]["steps"]}
    assert steps["store"]["tests"]["passed"] and steps["classify"]["tests"]["mocked"]


async def test_task_status_follows_spec_check_and_verify(project: Path, tmp_path: Path) -> None:
    import shutil

    from tuvl.core.config import use_project
    from tuvl_insight.spec.tasks import status_report

    copy = tmp_path / "demo"
    shutil.copytree(project, copy)
    use_project(str(copy))
    workflow = copy / "workflows" / "triage_ticket.yaml"
    workflow.write_text(workflow.read_text().replace("expires: 4h", "expires: 8h"))
    tasks = {t["id"]: t for t in (await status_report(copy, run_tests=False))["plans"][0]["tasks"]}
    assert tasks["T4"]["status"] == "in_progress"  # the review step contradicts the spec
    assert any("expires 8h, spec says 4h" in r for r in tasks["T4"]["reasons"])
    assert not any(
        t["blocked"] and "blocking finding" in " ".join(t["reasons"]) for t in tasks.values()
    )

    spec = copy / "specs" / "triage-ticket.md"
    spec.write_text(spec.read_text().replace("   - expired → `store`\n", ""))
    tasks = {t["id"]: t for t in (await status_report(copy, run_tests=False))["plans"][0]["tasks"]}
    assert all(t["blocked"] for t in tasks.values())
    assert "the spec has 1 blocking finding(s) — tuvl lint spec" in tasks["T1"]["reasons"]


def test_boolean_defaults_match_their_yaml_value() -> None:
    from tuvl_insight.spec.verify import _same_default

    assert _same_default("false", False) and _same_default("1", "1") and _same_default("0", 0)
    assert not _same_default("true", False)
