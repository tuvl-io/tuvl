"""`tuvl ship`: the deterministic gate before packaging, and the runnable archive."""

import hashlib
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight import shiparchive, shipgate
from tuvl_insight.spec import lifecycle

runner = CliRunner()


def ship(project: Path, *args: str):
    return runner.invoke(app, ["ship", "-d", str(project), "--no-image", *args])


def tar_members(path: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(path) as tar:
        return {m.name: m for m in tar.getmembers()}


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tuvl_insight.coding_agents.detect", lambda *a, **k: [])
    monkeypatch.setattr("tuvl_insight.shipgate.uv_lock_current", lambda project: None)


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    done = runner.invoke(
        app, ["init", str(tmp_path / "demo"), "--sample", "-y", "--preset", "ollama", "--no-git"]
    )
    assert done.exit_code == 0, done.output
    project = tmp_path / "demo"
    (project / "uv.lock").write_text("version = 1\n")
    return project


# ── the archive ──────────────────────────────────────────────────────────────


def test_the_archive_holds_the_runtime_files_and_how_to_run_them(demo: Path) -> None:
    done = ship(demo, "--format", "archive")
    assert done.exit_code == 0, done.output
    archive = demo / "dist" / "demo-0.1.0.tar.gz"
    members = tar_members(archive)
    names = {n.split("/", 1)[1] for n in members}
    assert {
        "pyproject.toml",
        "uv.lock",
        "tuvl.lock",
        "workflows/triage_ticket.yaml",
        "RUN.md",
        "run.sh",
        ".env.example",
        "BUILD-INFO.json",
    } <= names
    assert not [
        n
        for n in names
        if n.startswith(("specs/", "tests/", ".agents/", "deploy/", ".claude/"))
        or n in {"AGENTS.md", "CLAUDE.md", ".env"}
    ]
    assert (
        members["demo-0.1.0/run.sh"].mode == 0o755
        and members["demo-0.1.0/pyproject.toml"].mode == 0o644
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (
        demo / "dist" / "demo-0.1.0.tar.gz.sha256"
    ).read_text() == f"{digest}  demo-0.1.0.tar.gz\n"
    assert digest in done.output


def test_the_run_script_installs_the_locked_dependencies_and_serves(demo: Path) -> None:
    ship(demo, "--format", "archive")
    with tarfile.open(demo / "dist" / "demo-0.1.0.tar.gz") as tar:
        script = tar.extractfile("demo-0.1.0/run.sh").read().decode()  # type: ignore[union-attr]
        info = yaml.safe_load(tar.extractfile("demo-0.1.0/BUILD-INFO.json").read())  # type: ignore[union-attr]
        env = tar.extractfile("demo-0.1.0/.env.example").read().decode()  # type: ignore[union-attr]
    pinned = yaml.safe_load((demo / "tuvl.lock").read_text())["tuvl"]
    assert "uv sync --frozen --no-dev --no-install-project" in script
    assert f'uv pip install --python .venv/bin/python "tuvl=={pinned}"' in script
    assert "exec .venv/bin/tuvl serve app --project-dir ." in script and ".env" in script
    assert (
        subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True).returncode == 0
    )
    assert (
        info["project"] == "demo" and info["version"] == "0.1.0" and len(info["runtime_hash"]) == 64
    )
    assert "TUVL_SIGNING_KEY=" in env and "POSTGRES_PASSWORD=" in env


def test_the_same_files_give_the_same_bytes(demo: Path) -> None:
    ship(demo, "--format", "archive")
    first = (demo / "dist" / "demo-0.1.0.tar.gz").read_bytes()
    (demo / "dist" / "demo-0.1.0.tar.gz").unlink()
    ship(demo, "--format", "archive")
    assert (demo / "dist" / "demo-0.1.0.tar.gz").read_bytes() == first
    workflow = demo / "workflows" / "triage_ticket.yaml"
    workflow.write_text(workflow.read_text() + "\n# touched\n")
    ship(demo, "--format", "archive", "--allow-incomplete")
    assert (demo / "dist" / "demo-0.1.0.tar.gz").read_bytes() != first


def test_a_zip_archive_is_reproducible_too(demo: Path) -> None:
    ship(demo, "--format", "archive", "--archive-format", "zip")
    path = demo / "dist" / "demo-0.1.0.zip"
    with zipfile.ZipFile(path) as z:
        assert {i.date_time for i in z.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert "demo-0.1.0/run.sh" in z.namelist()
    first = path.read_bytes()
    path.unlink()
    ship(demo, "--format", "archive", "--archive-format", "zip")
    assert path.read_bytes() == first


def test_both_formats_and_a_chosen_output_folder(
    demo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    out = tmp_path / "out"
    done = runner.invoke(app, ["ship", "-d", str(demo), "--format", "both", "--out", str(out)])
    assert done.exit_code == 0, done.output
    assert (out / "demo-0.1.0.tar.gz").is_file()
    assert any(c[:2] == ["/usr/bin/docker", "build"] for c in calls)


def test_the_archive_needs_a_current_uv_lock(demo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tuvl_insight.shipgate.uv_lock_current",
        lambda project: "uv.lock is out of date: run `uv lock`",
    )
    refused = ship(demo, "--format", "archive")
    assert refused.exit_code == 1 and "uv.lock is out of date" in " ".join(refused.output.split())
    assert not (demo / "dist").exists()
    image_only = ship(demo)  # an image build only warns: it does not use --frozen
    assert image_only.exit_code == 0


def test_formats_are_checked(demo: Path) -> None:
    assert ship(demo, "--format", "tarball").exit_code == 2
    assert ship(demo, "--format", "archive", "--archive-format", "rar").exit_code == 2


# ── the gate ─────────────────────────────────────────────────────────────────


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_a_spec_that_is_not_approved_stops_the_ship(demo: Path) -> None:
    spec = next((demo / "specs").glob("*.md"))
    spec.write_text(lifecycle.with_front(spec.read_text(), {"status": "draft"}))
    done = ship(demo)
    assert done.exit_code == 1 and "is draft: it must be approved before it ships" in " ".join(
        done.output.split()
    )
    spec.write_text(lifecycle.approve(spec.read_text(), by="Ada"))
    spec.write_text(spec.read_text().replace("# Intent", "# Intent\nA late edit.", 1))
    stale = ship(demo)
    assert stale.exit_code == 1 and "its body changed since" in " ".join(stale.output.split())


def test_an_unmanaged_spec_only_warns(demo: Path) -> None:
    done = ship(demo)
    assert done.exit_code == 0, done.output
    assert "not under approval" in " ".join(done.output.split())


def test_unfinished_tasks_stop_the_ship_unless_allowed(demo: Path) -> None:
    plan = next((demo / "specs").glob("*.tasks.yaml"))
    doc = yaml.safe_load(plan.read_text())
    doc["spec"]["tasks"].append(
        {"id": "T99", "title": "Sign the contract", "type": "manual", "status": "todo"}
    )
    plan.write_text(yaml.safe_dump(doc))
    done = ship(demo)
    assert (
        done.exit_code == 1 and "T99" in done.output and "not done" in " ".join(done.output.split())
    )
    assert ship(demo, "--allow-incomplete").exit_code == 0


def test_out_of_date_generated_files_stop_the_ship(demo: Path) -> None:
    (demo / "agents" / "_generated" / "schemas.py").write_text("# stale\n")
    done = ship(demo)
    assert done.exit_code == 1 and "generated files are out of date" in " ".join(
        done.output.split()
    )


def test_review_errors_stop_the_ship(demo: Path) -> None:
    agent = next(p for p in (demo / "agents").glob("*.py") if p.name != "__init__.py")
    agent.write_text(agent.read_text() + "\nimport subprocess\nsubprocess.run(['ls'])\n")
    done = ship(demo)
    assert done.exit_code == 1 and "review: C002" in " ".join(done.output.split())


def test_uncommitted_changes_stop_the_ship_in_a_repository(
    demo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gc"))
    (tmp_path / "gc").write_text("[user]\n\tname = A\n\temail = a@b.c\n")
    git(demo, "init", "-q", "-b", "develop")
    git(demo, "add", "-A")
    git(demo, "commit", "-q", "-m", "init")
    clean = ship(demo, "--format", "archive")
    assert clean.exit_code == 0, clean.output
    commit = git(demo, "rev-parse", "--short", "HEAD")
    with tarfile.open(demo / "dist" / "demo-0.1.0.tar.gz") as tar:
        assert commit in tar.extractfile("demo-0.1.0/BUILD-INFO.json").read().decode()  # type: ignore[union-attr]
    (demo / "models" / "extra.txt").write_text("x")
    dirty = ship(demo)
    assert dirty.exit_code == 1 and "uncommitted change" in " ".join(dirty.output.split())
    assert ship(demo, "--allow-dirty").exit_code == 0


def test_packaging_is_recorded_in_the_ledger(demo: Path) -> None:
    from tuvl_insight.audit import read_events

    ship(demo, "--format", "archive")
    event = read_events(demo)[-1]
    assert event["kind"] == "ship.packaged" and len(event["refs"]["archive_sha256"]) == 64
    assert len(event["refs"]["runtime_hash"]) == 64 and event["refs"]["format"] == "archive"


# ── the pieces ───────────────────────────────────────────────────────────────


def test_the_runtime_hash_changes_with_content_and_ignores_missing_files(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("a")
    one = shipgate.runtime_hash(tmp_path, ["a.yaml", "gone.yaml"])
    assert one == shipgate.runtime_hash(tmp_path, ["gone.yaml", "a.yaml"])
    (tmp_path / "a.yaml").write_text("b")
    assert shipgate.runtime_hash(tmp_path, ["a.yaml"]) != one


def test_env_example_lists_each_variable_once() -> None:
    text = shiparchive.env_example(["OPENAI_API_KEY", "POSTGRES_HOST", "OPENAI_API_KEY"])
    assert text.count("POSTGRES_HOST=") == 1 and text.count("OPENAI_API_KEY=") == 1


def test_env_example_describes_declared_variables() -> None:
    from tuvl.core.envconfig import EnvVar

    text = shiparchive.env_example(
        ["CRM_TOKEN", "THRESHOLD"],
        {
            "CRM_TOKEN": EnvVar("CRM_TOKEN", "CRM access token", secret=True),
            "THRESHOLD": EnvVar("THRESHOLD", "Escalation score", default="0.8"),
        },
    )
    assert "# CRM access token\nCRM_TOKEN=\n" in text
    assert "# Escalation score\nTHRESHOLD=0.8\n" in text
