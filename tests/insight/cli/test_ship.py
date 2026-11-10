"""`tuvl ship` — artifact generation, the validate gate, and the docker handoff."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from tuvl.cli import app

runner = CliRunner()


def _project(tmp_path: Path, *, name: str = "Demo App", version: str = "1.2.3") -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        'requires-python = ">=3.13"\ndependencies = ["tuvl"]\n'
    )
    wf = {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": "ping"},
        "spec": {
            "trigger": {"http": {"path": "/api/ping", "method": "POST"}},
            "agents": [
                {
                    "id": "fetch",
                    "description": "Fetch a status page",
                    "engine": "tool",
                    "tool": {"use": "http", "method": "GET", "url": "https://example.com/status"},
                    "routes": {"default": "END", "error": "END"},
                }
            ],
        },
    }
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "ping.yaml").write_text(yaml.safe_dump(wf))
    locked = runner.invoke(app, ["generate", "lock", "-d", str(tmp_path)])
    assert locked.exit_code == 0, locked.output
    return tmp_path


def test_ship_generates_artifacts(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 0, res.output

    dockerfile = (proj / "deploy" / "Dockerfile").read_text()
    assert "TUVL_ENV=production" in dockerfile
    assert '"tuvl", "serve", "app"' in dockerfile
    assert "TUVL_DEV_MODE" not in dockerfile
    pinned = yaml.safe_load((proj / "tuvl.lock").read_text())["tuvl"]
    assert f'"tuvl=={pinned}"' in dockerfile and "--upgrade-package" not in dockerfile

    assert (proj / ".dockerignore").read_text().splitlines()[3] == "*"  # an allowlist

    chart = proj / "deploy" / "chart" / "demo-app"
    chart_meta = yaml.safe_load((chart / "Chart.yaml").read_text())
    assert chart_meta["name"] == "demo-app"
    assert chart_meta["appVersion"] == "1.2.3"

    values = yaml.safe_load((chart / "values.yaml").read_text())
    assert values["image"]["repository"] == "demo-app"
    assert values["existingSecret"] == "demo-app-env"

    deployment = (chart / "templates" / "deployment.yaml").read_text()
    assert '{{ include "app.fullname" $root }}' in deployment
    assert "/health" in deployment and "--role" in deployment

    # shutdown, proxy trust and pod hardening
    assert (
        values["terminationGracePeriodSeconds"] > values["gracefulTimeout"] + values["drainTimeout"]
    )
    assert values["forwardedAllowIps"] == "" and values["limitMaxRequests"] == 0
    hardening = values["securityContext"]
    assert hardening["runAsNonRoot"] is True and hardening["allowPrivilegeEscalation"] is False
    assert hardening["capabilities"] == {"drop": ["ALL"]}
    assert hardening["seccompProfile"] == {"type": "RuntimeDefault"}
    assert "--graceful-timeout" in deployment and "--drain-timeout" in deployment
    assert "--forwarded-allow-ips" in deployment and "--limit-max-requests" in deployment
    assert "terminationGracePeriodSeconds" in deployment and "preStop" in deployment


def test_ship_blocks_on_validation_error(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / "workflows" / "bad.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "Workflow",
                "metadata": {"name": "bad"},
                "spec": {"steps": [{"id": "s1", "kind": "NotAKind"}]},
            }
        )
    )
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 1
    assert not (proj / "deploy").exists()


def test_ship_requires_pyproject(tmp_path: Path) -> None:
    res = runner.invoke(app, ["ship", "-d", str(tmp_path), "--no-build"])
    assert res.exit_code == 1
    assert "pyproject.toml" in res.output


def test_ship_keeps_existing_unless_forced(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0

    dockerfile = proj / "deploy" / "Dockerfile"
    dockerfile.write_text("# customized\n")
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    assert dockerfile.read_text() == "# customized\n"

    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build", "--force"]).exit_code == 0
    assert "tuvl" in dockerfile.read_text()


def test_ship_builds_and_pushes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = _project(tmp_path)
    calls: list[list[str]] = []

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/docker")

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = runner.invoke(app, ["ship", "-d", str(proj), "--tag", "ghcr.io/acme/demo:1", "--push"])
    assert res.exit_code == 0, res.output
    docker = [c for c in calls if c[0] == "/usr/bin/docker"]  # the gate also asks git and uv things
    assert docker[0][:2] == ["/usr/bin/docker", "build"]
    assert "ghcr.io/acme/demo:1" in docker[0]
    labels = [a for a in docker[0] if a.startswith("io.tuvl.runtime-hash=")]
    assert len(labels) == 1 and len(labels[0].split("=")[1]) == 64
    assert docker[1] == ["/usr/bin/docker", "push", "ghcr.io/acme/demo:1"]


def test_ship_fails_cleanly_without_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = _project(tmp_path)
    monkeypatch.setattr("shutil.which", lambda _: None)
    res = runner.invoke(app, ["ship", "-d", str(proj)])
    assert res.exit_code == 1
    assert "--no-build" in res.output


def test_ship_warns_on_stale_chart_version(tmp_path: Path) -> None:
    proj = _project(tmp_path, version="1.2.3")
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    # Bump the project version; re-ship keeps the old Chart.yaml but must warn
    # the deployed image tag will not match the new build.
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "Demo App"\nversion = "2.0.0"\n'
        'requires-python = ">=3.13"\ndependencies = ["tuvl"]\n'
    )
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 0
    assert "appVersion" in res.output and "1.2.3" in res.output and "2.0.0" in res.output


def test_ship_aborts_on_malformed_pyproject(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / "pyproject.toml").write_text('[project]\nname = "broken\n')  # unterminated string
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 1
    assert "pyproject.toml" in res.output


def test_ship_requires_a_current_lock(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lock = proj / "tuvl.lock"
    data = yaml.safe_load(lock.read_text())
    lock.write_text(yaml.safe_dump({**data, "tuvl": "0.0.1"}))
    stale = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert stale.exit_code == 1 and "[V024] tuvl.lock is out of date" in stale.output
    assert "tuvl: 0.0.1 →" in stale.output
    lock.unlink()
    missing = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert missing.exit_code == 1 and "[V024] tuvl.lock is missing" in missing.output
    assert not (proj / "deploy").exists()


def _render(chart: Path, *overrides: str) -> list[dict]:
    args = ["helm", "template", "demo", str(chart)]
    for item in overrides:
        args += ["--set", item]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _role(deployment: dict) -> tuple[str, int, list[str]]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]
    return args[args.index("--role") + 1], deployment["spec"]["replicas"], args


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_chart_renders_single_and_split_roles(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    chart = proj / "deploy" / "chart" / "demo-app"

    single = _render(chart)
    deployments = [d for d in single if d["kind"] == "Deployment"]
    assert len(deployments) == 1
    role, replicas, args = _role(deployments[0])
    assert (role, replicas) == ("all", 1) and args[args.index("--concurrency") + 1] == "8"
    service = next(d for d in single if d["kind"] == "Service")
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "all"

    split = _render(chart, "split=true", "worker.replicaCount=3", "worker.concurrency=16")
    by_role = {_role(d)[0]: d for d in split if d["kind"] == "Deployment"}
    assert set(by_role) == {"api", "worker"}
    assert by_role["worker"]["metadata"]["name"].endswith("-worker")
    assert _role(by_role["worker"])[1] == 3
    worker_args = _role(by_role["worker"])[2]
    assert worker_args[worker_args.index("--concurrency") + 1] == "16"
    labels = by_role["api"]["spec"]["selector"]["matchLabels"]
    assert labels["app.kubernetes.io/component"] == "api"
    service = next(d for d in split if d["kind"] == "Service")
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "api"


def _use_laya(proj: Path, model: str = "auto") -> None:
    (proj / "llms").mkdir(exist_ok=True)
    (proj / "llms" / "laya.yaml").write_text(
        "kind: AgentModel\nmetadata: {name: laya}\n"
        f"spec: {{type: decision, provider: laya, model: {model}, api_base: 'http://localhost:8000'}}\n"
    )


@pytest.mark.parametrize(
    ("uses_laya", "flag", "enabled"),
    [
        (False, None, False),
        (True, None, True),
        (True, "--without-laya", False),
        (False, "--with-laya", True),
    ],
)
def test_ship_includes_laya_when_the_project_uses_it(
    tmp_path: Path, uses_laya: bool, flag: str | None, enabled: bool
) -> None:
    proj = _project(tmp_path)
    if uses_laya:
        _use_laya(proj)
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build", *([flag] if flag else [])])
    assert res.exit_code == 0, res.output
    chart = proj / "deploy" / "chart" / "demo-app"
    assert yaml.safe_load((chart / "values.yaml").read_text())["laya"]["enabled"] is enabled
    assert (chart / "templates" / "laya.yaml").exists()  # present either way; values decide


@pytest.mark.parametrize(
    ("model", "tag", "memory"),
    [
        ("english", "0.4.7-1-en", "2Gi"),
        ("auto", "0.4.7-1", "4Gi"),
        ("multilingual", "0.4.7-1", "4Gi"),
    ],
)
def test_ship_picks_the_laya_image_that_matches_the_model(
    tmp_path: Path, model: str, tag: str, memory: str
) -> None:
    proj = _project(tmp_path)
    _use_laya(proj, model)
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    values = yaml.safe_load((proj / "deploy" / "chart" / "demo-app" / "values.yaml").read_text())
    assert values["laya"]["image"]["tag"] == tag
    assert values["laya"]["resources"]["requests"]["memory"] == memory


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_chart_renders_laya_and_points_the_engine_at_it(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _use_laya(proj)
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    chart = proj / "deploy" / "chart" / "demo-app"

    docs = _render(chart, "split=true")
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert {("Deployment", "demo-demo-app-laya"), ("Service", "demo-demo-app-laya")} <= kinds
    laya_pod = next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-laya")
    )
    container = laya_pod["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].startswith("docker.io/tuvl/laya-serve:")
    key = next(e for e in container["env"] if e["name"] == "LAYA_API_KEY")
    assert key["valueFrom"]["secretKeyRef"]["optional"] is True
    engines = [
        d for d in docs if d["kind"] == "Deployment" and not d["metadata"]["name"].endswith("-laya")
    ]
    assert len(engines) == 2  # api + worker both reach laya
    for engine in engines:
        env = {
            e["name"]: e.get("value")
            for e in engine["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["LAYA_API_BASE"] == "http://demo-demo-app-laya:8000"
    # the engine and laya Services select disjoint pods
    selectors = [d["spec"]["selector"] for d in docs if d["kind"] == "Service"]
    assert len({s["app.kubernetes.io/component"] for s in selectors}) == 2

    off = _render(chart, "laya.enabled=false")
    assert not [d for d in off if d["metadata"]["name"].endswith("-laya")]
    assert all("LAYA_API_BASE" not in str(d) for d in off if d["kind"] == "Deployment")


def _dev_material(proj: Path) -> None:
    (proj / "specs").mkdir(exist_ok=True)
    (proj / "specs" / "refunds.md").write_text("# Intent\n")
    (proj / "specs" / "refunds.tasks.yaml").write_text("# a task list\n")
    for name in ("AGENTS.md", "CLAUDE.md", "compose.yaml", "requirements.docx"):
        (proj / name).write_text("x")
    (proj / ".claude" / "skills").mkdir(parents=True)
    (proj / ".claude" / "skills" / "SKILL.md").write_text("x")
    (proj / ".tuvl").mkdir(exist_ok=True)
    (proj / ".tuvl" / "system.yaml").write_text(
        "kind: SystemConfig\nversion: v1\nspec:\n  deployment_mode: single_tenant\n"
    )


def test_ship_image_holds_only_runtime_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tuvl_insight.cli.commands.ship import _ignore_rules, _image_files
    from tuvl_insight.shipgate import GateResult

    proj = _project(tmp_path)
    _dev_material(proj)
    # the junk specs are there to prove they stay out of the image, not to pass the spec gate
    monkeypatch.setattr("tuvl_insight.shipgate.spec_gate", lambda *a, **k: GateResult())
    monkeypatch.setattr("tuvl_insight.shipgate.codegen_current", lambda *a, **k: [])
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 0, res.output
    files = _image_files(proj, _ignore_rules(proj / ".dockerignore"))
    assert {"pyproject.toml", "tuvl.lock", "workflows/ping.yaml", ".tuvl/system.yaml"} <= set(files)
    assert not [f for f in files if f.startswith(("specs/", ".claude/", "deploy/"))]
    assert not {"AGENTS.md", "CLAUDE.md", "compose.yaml", "requirements.docx"} & set(files)
    assert "image contents:" in res.output


@pytest.mark.parametrize(
    ("dockerignore", "reason"),
    [
        (".env\n.git/\ntests/\n", "not an allowlist"),
        (
            "*\n!pyproject.toml\n!tuvl.lock\n!workflows/\n!specs/\n",
            "development file in the image: specs/",
        ),
    ],
)
def test_ship_refuses_development_files_in_the_image(
    tmp_path: Path, dockerignore: str, reason: str
) -> None:
    proj = _project(tmp_path)
    _dev_material(proj)
    (proj / ".dockerignore").write_text(dockerignore)
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 1
    assert "V027" in res.output and reason in res.output


@pytest.mark.parametrize(
    "path",
    [
        ".tuvl/audit/2026-10.jsonl",
        ".tuvl/audit/outbox.queue",
        ".tuvl/trace/blobs/ab",
        ".tuvl/live/tasks.json",
        ".tuvl/dev-telemetry.yaml",
        ".tuvl/agent.yaml",
    ],
)
def test_ship_refuses_the_audit_trail_and_the_developer_telemetry_config(
    tmp_path: Path, path: str
) -> None:
    proj = _project(tmp_path)
    target = proj / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x: 1\n")
    (proj / ".dockerignore").write_text("*\n!pyproject.toml\n!tuvl.lock\n!workflows/\n!.tuvl/\n")
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 1
    assert "V027" in res.output and f"development file in the image: {path}" in res.output


def test_ship_refuses_a_test_document_hidden_among_workflows(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / "workflows" / "check.yaml").write_text(
        "kind: Test\nversion: tuvl/v2\nmetadata: {name: check}\n"
        "spec: {workflow: ping, expect: {end: default}}\n"
    )
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build", "--force"])
    assert res.exit_code == 1
    assert "development file in the image: workflows/check.yaml" in res.output


def test_ship_refuses_pending_agents(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    wf = yaml.safe_load((proj / "workflows" / "ping.yaml").read_text())
    wf["spec"]["agents"][0] = {
        "id": "fetch",
        "description": "Fetch a status page",
        "engine": "pending",
        "outputs": {"status": "str"},
        "routes": {"default": "END"},
    }
    (proj / "workflows" / "ping.yaml").write_text(yaml.safe_dump(wf))
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 1
    assert "V010" in res.output and "Ship aborted" in res.output


def _use_storage(proj: Path) -> None:
    (proj / "storage").mkdir(exist_ok=True)
    (proj / "storage" / "files.yaml").write_text(
        "kind: Storage\nversion: v1\nmetadata: {name: files}\nspec: {provider: local, root: ./data/uploads}\n"
    )
    (proj / "storage" / "docs.yaml").write_text(
        "kind: Storage\nversion: v1\nmetadata: {name: docs}\nspec: {provider: local, root: /var/lib/docs}\n"
    )
    (proj / "storage" / "cloud.yaml").write_text(
        "kind: Storage\nversion: v1\nmetadata: {name: cloud}\n"
        "spec: {provider: s3, bucket: b, access_key: '${K:}', secret_key: '${S:}'}\n"
    )


def test_ship_puts_storage_definitions_in_the_image_and_mounts_local_roots(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _use_storage(proj)
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 0, res.output
    assert "!storage/" in (proj / ".dockerignore").read_text().splitlines()
    assert "storage/ 3" in res.output  # the definitions travel with the image
    chart = proj / "deploy" / "chart" / "demo-app"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    assert values["storage"]["local"]["enabled"] is True
    assert values["storage"]["local"]["mounts"] == [
        {"path": "/app/data/uploads", "subPath": "app-data-uploads"},
        {"path": "/var/lib/docs", "subPath": "var-lib-docs"},
    ]


def test_ship_without_local_stores_adds_no_volume(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    values = yaml.safe_load((proj / "deploy/chart/demo-app/values.yaml").read_text())
    assert values["storage"]["local"] == {
        "enabled": False,
        "size": "10Gi",
        "storageClass": "",
        "accessMode": "ReadWriteOnce",
        "existingClaim": "",
        "mounts": [],
    }


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_chart_renders_a_claim_and_mounts_for_local_stores(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _use_storage(proj)
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    chart = proj / "deploy" / "chart" / "demo-app"

    docs = _render(chart)
    claim = next(d for d in docs if d["kind"] == "PersistentVolumeClaim")
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["securityContext"] == {"fsGroup": 1001}
    mounts = pod["containers"][0]["volumeMounts"]
    assert [(m["mountPath"], m["subPath"]) for m in mounts] == [
        ("/app/data/uploads", "app-data-uploads"),
        ("/var/lib/docs", "var-lib-docs"),
    ]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == claim["metadata"]["name"]

    split = _render(
        chart,
        "split=true",
        "storage.local.accessMode=ReadWriteMany",
        "storage.local.storageClass=nfs",
    )
    claims = [d for d in split if d["kind"] == "PersistentVolumeClaim"]
    assert len(claims) == 1 and claims[0]["spec"]["storageClassName"] == "nfs"
    for deployment in (d for d in split if d["kind"] == "Deployment"):
        assert deployment["spec"]["template"]["spec"]["volumes"]

    existing = _render(chart, "storage.local.existingClaim=shared")
    assert not [d for d in existing if d["kind"] == "PersistentVolumeClaim"]
    pod = next(d for d in existing if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "shared"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_chart_without_local_stores_has_no_volume(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    docs = _render(proj / "deploy" / "chart" / "demo-app")
    assert not [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert "volumes" not in pod and "securityContext" not in pod


def test_ship_warns_when_a_kept_dockerignore_leaves_storage_out(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    assert runner.invoke(app, ["ship", "-d", str(proj), "--no-build"]).exit_code == 0
    ignore = proj / ".dockerignore"
    ignore.write_text(ignore.read_text().replace("!storage/\n", ""))
    _use_storage(proj)
    res = runner.invoke(app, ["ship", "-d", str(proj), "--no-build"])
    assert res.exit_code == 0, res.output
    assert "storage/ is not in the image" in res.output
