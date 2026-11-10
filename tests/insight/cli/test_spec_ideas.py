"""`tuvl spec import | ideas | idea | renumber` and the doctor `ideas` group."""

import http.server
import json
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight.coding_agents import Settings, read_settings, write_settings
from tuvl_insight.spec import ids
from tuvl_insight.spec.parse import content_hash

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    write_settings(p, Settings("SK", "none"))
    return p


@pytest.fixture
def brief(tmp_path: Path) -> Path:
    d = tmp_path / "brief"
    d.mkdir()
    (d / "overview.md").write_text("# Refund automation\n\nBody\n")
    return d


def run(project: Path, *args: str):
    return runner.invoke(app, [*args, "-d", str(project)])


def test_import_numbers_the_idea_and_lists_it(project: Path, brief: Path) -> None:
    done = run(project, "import", "idea", str(brief))
    assert done.exit_code == 0, done.output
    assert "IDEA-SK-0001" in done.output
    again = run(project, "import", "idea", str(brief / "overview.md"), "--title", "Second")
    assert "IDEA-SK-0002" in again.output
    listing = json.loads(run(project, "list", "ideas", "--json").output)
    assert [r["id"] for r in listing["ideas"]] == ["IDEA-SK-0001", "IDEA-SK-0002"]
    shown = run(project, "show", "idea", "IDEA-SK-0002")
    assert "Second" in shown.output and "source/overview.md" in shown.output


def test_import_needs_initials_and_saves_them_once(tmp_path: Path, brief: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    refused = run(bare, "import", "idea", str(brief))
    assert refused.exit_code == 1 and "initials" in refused.output
    ok = run(bare, "import", "idea", str(brief), "--initials", "al")
    assert ok.exit_code == 0 and "IDEA-AL-0001" in ok.output
    assert read_settings(bare) == Settings("AL", "none")
    assert run(bare, "import", "idea", str(brief)).exit_code == 0  # now remembered


def test_bad_initials_are_refused(project: Path, brief: Path) -> None:
    assert run(project, "import", "idea", str(brief), "--initials", "1").exit_code == 2


def test_dry_run_writes_nothing(project: Path, brief: Path) -> None:
    done = run(project, "import", "idea", str(brief), "--dry-run", "--json")
    data = json.loads(done.output)
    assert data["id"] == "IDEA-SK-0001" and data["files"] == ["overview.md"]
    assert not (project / "idea").exists()


def test_import_reports_what_it_skipped(project: Path, brief: Path) -> None:
    (brief / ".env").write_text("K=1")
    done = json.loads(run(project, "import", "idea", str(brief), "--json").output)
    assert done["skipped"] == [{"path": ".env", "reason": "looks like a secret or key file"}]


def test_unknown_idea_and_empty_import_exit_1(project: Path, tmp_path: Path) -> None:
    assert run(project, "show", "idea", "IDEA-SK-0042").exit_code == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(project, "import", "idea", str(empty)).exit_code == 1


# ── renumber ─────────────────────────────────────────────────────────────────


def seed(project: Path, brief: Path) -> None:
    run(project, "import", "idea", str(brief))
    run(project, "import", "idea", str(brief))  # IDEA-SK-0002 supersedes target below
    specs = project / "specs"
    specs.mkdir()
    spec = "---\nid: SPEC-SK-0001-01\nideas: [IDEA-SK-0001]\nname: refunds\n---\n# Intent\nx\n"
    (specs / "SPEC-SK-0001-01.md").write_text(spec)
    (specs / "SPEC-SK-0001-01.tasks.yaml").write_text(
        "kind: TaskPlan\nversion: tuvl/v2\nmetadata:\n  name: refunds\n"
        "  spec: specs/SPEC-SK-0001-01.md\n  spec_hash: old\nspec:\n  tasks:\n"
        "  - id: TASK-SK-0001-01-001\n    title: one\n    type: manual\n"
        "  - id: TASK-SK-0001-01-002\n    title: two\n    type: manual\n"
        "    depends_on: [TASK-SK-0001-01-001]\n"
    )
    idx = ids.read_index(project)
    idx[0]["specs"] = ["SPEC-SK-0001-01"]
    idx[1]["supersedes"] = "IDEA-SK-0001"
    ids.write_index(project, idx)


def test_renumber_carries_specs_tasks_and_links(project: Path, brief: Path) -> None:
    seed(project, brief)
    done = run(project, "renumber", "idea", "IDEA-SK-0001")
    assert done.exit_code == 0, done.output
    assert "IDEA-SK-0001 → IDEA-SK-0003" in done.output
    assert not (project / "idea" / "IDEA-SK-0001").exists()
    assert (project / "idea" / "IDEA-SK-0003" / "source" / "overview.md").is_file()
    record = yaml.safe_load((project / "idea" / "IDEA-SK-0003" / "IDEA.yaml").read_text())
    assert record["id"] == "IDEA-SK-0003"
    specs = project / "specs"
    assert sorted(p.name for p in specs.iterdir()) == [
        "SPEC-SK-0003-01.md",
        "SPEC-SK-0003-01.tasks.yaml",
    ]
    spec_text = (specs / "SPEC-SK-0003-01.md").read_text()
    assert "id: SPEC-SK-0003-01" in spec_text and "ideas: [IDEA-SK-0003]" in spec_text
    tasks = yaml.safe_load((specs / "SPEC-SK-0003-01.tasks.yaml").read_text())
    assert [t["id"] for t in tasks["spec"]["tasks"]] == [
        "TASK-SK-0003-01-001",
        "TASK-SK-0003-01-002",
    ]
    assert tasks["spec"]["tasks"][1]["depends_on"] == ["TASK-SK-0003-01-001"]
    assert tasks["metadata"]["spec"] == "specs/SPEC-SK-0003-01.md"
    assert tasks["metadata"]["spec_hash"] == content_hash(spec_text)
    index = {r["id"]: r for r in ids.read_index(project)}
    assert set(index) == {"IDEA-SK-0002", "IDEA-SK-0003"}
    assert index["IDEA-SK-0003"]["specs"] == ["SPEC-SK-0003-01"]
    assert index["IDEA-SK-0002"]["supersedes"] == "IDEA-SK-0003"


def test_renumber_refuses_clashes(project: Path, brief: Path) -> None:
    seed(project, brief)
    assert run(project, "renumber", "idea", "IDEA-SK-0001", "--to", "IDEA-SK-0002").exit_code == 1
    assert run(project, "renumber", "idea", "IDEA-SK-0001", "--to", "nonsense").exit_code == 1
    assert run(project, "renumber", "idea", "IDEA-SK-0099").exit_code == 1
    assert (project / "idea" / "IDEA-SK-0001").is_dir()


def test_renumber_to_a_chosen_id_and_other_initials(project: Path, brief: Path) -> None:
    seed(project, brief)
    done = run(project, "renumber", "idea", "IDEA-SK-0001", "--to", "IDEA-AL-0007")
    assert done.exit_code == 0
    assert (project / "specs" / "SPEC-AL-0007-01.md").is_file()


# ── doctor ───────────────────────────────────────────────────────────────────


def doctor(project: Path) -> dict[str, str]:
    done = runner.invoke(app, ["check", "system", "-d", str(project), "--json", "--only", "ideas"])
    data = json.loads(done.output[done.output.index('{\n  "healthy"') :])
    return {c["id"]: c["status"] for c in data["checks"]}


def test_doctor_ideas_group(project: Path, brief: Path) -> None:
    assert doctor(project) == {"ideas": "ok"}
    run(project, "import", "idea", str(brief))
    checks = doctor(project)
    assert checks["index"] == "ok" and checks["duplicates"] == "ok"
    idx = ids.read_index(project)
    ids.write_index(project, idx + idx)  # a merge that kept both rows
    assert doctor(project)["duplicate IDEA-SK-0001"] == "fail"


def test_doctor_flags_an_idea_the_base_branch_numbered_differently(
    project: Path, brief: Path
) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    git("init", "-q", "-b", "develop")
    git("config", "user.email", "a@b.c")
    git("config", "user.name", "A")
    run(project, "import", "idea", str(brief))
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("switch", "-q", "-c", "feature")
    git("rm", "-rq", "idea")
    (brief / "overview.md").write_text("# A different brief\n")
    run(project, "import", "idea", str(brief))  # same number, different content
    checks = doctor(project)
    assert checks["conflict IDEA-SK-0001"] == "fail"


def test_idea_files_are_not_unknown_kinds_to_validate(project: Path, brief: Path) -> None:
    """`idea/INDEX.yaml` and `idea/*/IDEA.yaml` are project records, not runtime resources."""
    run(project, "import", "idea", str(brief))
    done = runner.invoke(app, ["lint", "project", "-d", str(project), "--json"])
    report = json.loads(done.output[done.output.index("{") :])
    messages = [e["message"] for e in report["errors"]]
    assert not [m for m in messages if "Unknown kind" in m], messages


PAGE = b"<html><head><title>Loyalty brief</title></head><body><main><h1>Loyalty points</h1><p>Earn one point per order.</p></main></body></html>"


@pytest.fixture
def site() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/brief"
    server.shutdown()


def test_a_url_is_imported_as_a_snapshot_with_its_origin(
    project: Path, brief: Path, site: str
) -> None:
    done = run(project, "import", "idea", str(brief), "--url", site, "--allow-private")
    assert done.exit_code == 0, done.output
    record = yaml.safe_load((project / "idea/IDEA-SK-0001/IDEA.yaml").read_text())
    by_path = {f["path"]: f for f in record["files"]}
    page = next(f for path, f in by_path.items() if path.startswith("source/web-127-0-0-1"))
    assert (
        page["origin"]["url"] == site
        and page["origin"]["kind"] == "web"
        and page["origin"]["fetched_at"]
    )
    assert "origin" not in by_path["source/overview.md"]
    saved = project / "idea/IDEA-SK-0001" / page["path"]
    assert saved.read_text() == "# Loyalty points\n\nEarn one point per order.\n"
    assert "origin" in json.dumps(
        json.loads(run(project, "show", "idea", "IDEA-SK-0001", "--json").output)
    )


def test_a_url_alone_makes_an_idea_titled_from_the_page(project: Path, site: str) -> None:
    done = run(project, "import", "idea", "--url", site, "--allow-private")
    assert done.exit_code == 0, done.output
    listing = json.loads(run(project, "list", "ideas", "--json").output)
    assert [r["title"] for r in listing["ideas"]] == ["Loyalty points"]


def test_an_internal_url_needs_allow_private_and_nothing_is_imported_without_it(
    project: Path, site: str
) -> None:
    refused = run(project, "import", "idea", "--url", site)
    assert refused.exit_code == 1 and "--allow-private" in refused.output
    assert not (project / "idea").exists() or not ids.idea_ids(project)


def test_dry_run_does_not_fetch_and_a_bare_import_is_refused(project: Path) -> None:
    dry = run(project, "import", "idea", "--url", "https://a.invalid/x", "--dry-run", "--json")
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["urls"] == ["https://a.invalid/x"]
    bare = run(project, "import", "idea")
    assert bare.exit_code == 2 and "--url" in bare.output
