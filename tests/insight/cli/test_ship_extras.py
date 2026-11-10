"""`tuvl ship` installs only the tuvl extras a project uses and checks its imports."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl.extras import EXTRAS
from tuvl_insight.cli.commands import ship_deps

runner = CliRunner()


def _project(root: Path, *, deps: str = '"tuvl"') -> Path:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "1.0.0"\nrequires-python = ">=3.13"\n'
        f"dependencies = [{deps}]\n"
    )
    return root


def _workflow(root: Path, agent: dict, *, trigger: dict | None = None, name: str = "wf") -> None:
    (root / "workflows").mkdir(exist_ok=True)
    doc = {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": name},
        "spec": {
            "trigger": trigger or {"http": {"path": f"/api/{name}", "method": "POST"}},
            "agents": [
                {
                    "id": "a",
                    "description": "x",
                    "routes": {"default": "END", "error": "END"},
                    **agent,
                }
            ],
        },
    }
    (root / "workflows" / f"{name}.yaml").write_text(yaml.safe_dump(doc))


HTTP_TOOL = {"engine": "tool", "tool": {"use": "http", "method": "GET", "url": "https://x.test"}}


def test_a_plain_project_needs_no_extras(tmp_path: Path) -> None:
    _workflow(_project(tmp_path), HTTP_TOOL)
    assert ship_deps.detect(tmp_path).names == []


def test_a_redis_datasource_needs_redis(tmp_path: Path) -> None:
    _project(tmp_path)
    (tmp_path / "datasources").mkdir()
    (tmp_path / "datasources" / "cache.yaml").write_text(
        yaml.safe_dump(
            {"kind": "DataSource", "metadata": {"name": "cache"}, "spec": {"type": "redis"}}
        )
    )
    assert ship_deps.detect(tmp_path).names == ["redis"]


def test_an_mcp_tool_needs_mcp(tmp_path: Path) -> None:
    mcp = {"engine": "tool", "tool": {"use": "mcp", "server": "artifact://s", "name": "t"}}
    _workflow(_project(tmp_path), mcp)
    assert ship_deps.detect(tmp_path).names == ["mcp"]


def test_an_mcp_export_needs_mcp(tmp_path: Path) -> None:
    trigger = {"http": {"path": "/api/wf", "method": "POST"}, "mcp": {"enabled": True}}
    _workflow(_project(tmp_path), HTTP_TOOL, trigger=trigger)
    assert ship_deps.detect(tmp_path).names == ["mcp"]


def test_a_loop_that_calls_an_mcp_server_needs_mcp(tmp_path: Path) -> None:
    loop = {"engine": "loop", "loop": {"tools": [{"mcp": "artifact://s", "allow": ["t"]}]}}
    _workflow(_project(tmp_path), loop)
    assert ship_deps.detect(tmp_path).names == ["mcp"]


def test_the_vector_builtins_and_collections_need_vector(tmp_path: Path) -> None:
    _project(tmp_path)
    _workflow(tmp_path, {"engine": "code", "code": {"run": "tuvl.data_search"}})
    assert ship_deps.detect(tmp_path).names == ["vector"]
    other = _project(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    (other / "models").mkdir()
    (other / "models" / "c.yaml").write_text(
        yaml.safe_dump({"kind": "CollectionConfig", "metadata": {"name": "kb"}, "spec": {}})
    )
    assert "vector" in ship_deps.detect(other).names


def test_forced_extras_are_added(tmp_path: Path) -> None:
    _project(tmp_path)
    assert ship_deps.detect(tmp_path, ("redis",)).extras == {"redis": ["--extra"]}


def _agent(root: Path, source: str, name: str = "tool.py") -> None:
    (root / "agents").mkdir(exist_ok=True)
    (root / "agents" / name).write_text(source)


def test_imports_of_an_extra_pull_it_in(tmp_path: Path) -> None:
    _project(tmp_path)
    _agent(tmp_path, "import redis\nfrom pgvector import Vector\n")
    assert ship_deps.detect(tmp_path).names == ["redis", "vector"]


def test_stdlib_tuvl_local_and_relative_imports_are_not_dependencies(tmp_path: Path) -> None:
    _project(tmp_path)
    _agent(tmp_path, "import json\nfrom tuvl import x\nimport helpers\nfrom . import y\n")
    _agent(tmp_path, "X = 1\n", name="helpers.py")
    needs = ship_deps.detect(tmp_path)
    assert (needs.undeclared, needs.unresolved) == ([], [])


def test_an_installed_package_the_project_does_not_declare_is_flagged(tmp_path: Path) -> None:
    _project(tmp_path)
    _agent(tmp_path, "import pytest\n")
    assert ship_deps.detect(tmp_path).undeclared == [("agents/tool.py", "pytest")]
    declared = _project(
        tmp_path / "ok" if (tmp_path / "ok").mkdir() is None else tmp_path,
        deps='"tuvl", "pytest>=9"',
    )
    _agent(declared, "import pytest\n")
    assert ship_deps.detect(declared).undeclared == []


def test_packages_tuvl_itself_depends_on_are_not_flagged(tmp_path: Path) -> None:
    _project(tmp_path)
    _agent(tmp_path, "import httpx\nimport yaml\nimport pydantic\n")
    assert ship_deps.detect(tmp_path).undeclared == []


def test_a_module_that_is_not_installed_is_unresolved_not_an_error(tmp_path: Path) -> None:
    _project(tmp_path)
    _agent(tmp_path, "import not_a_real_package_xyz\n")
    needs = ship_deps.detect(tmp_path)
    assert needs.unresolved == [("agents/tool.py", "not_a_real_package_xyz")]
    assert needs.undeclared == []


def test_declared_but_unimported_dependencies_are_reported(tmp_path: Path) -> None:
    _project(tmp_path, deps='"tuvl", "pytest>=9"')
    _agent(tmp_path, "import json\n")
    assert ship_deps.detect(tmp_path).unused == ["pytest"]


# ── the command ──────────────────────────────────────────────────────────────


def _ship(root: Path, *flags: str):
    assert runner.invoke(app, ["generate", "lock", "-d", str(root)]).exit_code == 0
    return runner.invoke(app, ["ship", "-d", str(root), "--no-build", *flags])


def test_ship_installs_only_the_needed_extras(tmp_path: Path) -> None:
    trigger = {"http": {"path": "/api/wf", "method": "POST"}}
    _workflow(_project(tmp_path), HTTP_TOOL, trigger=trigger)
    res = _ship(tmp_path)
    assert res.exit_code == 0, res.output
    dockerfile = (tmp_path / "deploy" / "Dockerfile").read_text()
    pinned = yaml.safe_load((tmp_path / "tuvl.lock").read_text())["tuvl"]
    assert f'"tuvl=={pinned}"' in dockerfile
    assert 'LABEL io.tuvl.extras=""' in dockerfile
    assert "not installed: mcp, redis, vector, otel-grpc" in res.output


def test_ship_pins_the_extras_a_project_uses(tmp_path: Path) -> None:
    _workflow(_project(tmp_path), HTTP_TOOL)
    _agent(tmp_path, "import redis\n")
    res = _ship(tmp_path, "--extra", "mcp")
    assert res.exit_code == 0, res.output
    dockerfile = (tmp_path / "deploy" / "Dockerfile").read_text()
    pinned = yaml.safe_load((tmp_path / "tuvl.lock").read_text())["tuvl"]
    assert f'"tuvl[mcp,redis]=={pinned}"' in dockerfile
    assert 'LABEL io.tuvl.extras="mcp,redis"' in dockerfile
    assert "redis: agents/tool.py: imports redis" in res.output


def test_ship_refuses_an_undeclared_import(tmp_path: Path) -> None:
    _workflow(_project(tmp_path), HTTP_TOOL)
    _agent(tmp_path, "import pytest\n")
    res = _ship(tmp_path)
    assert res.exit_code == 1
    assert "pytest" in res.output and "does not declare" in res.output
    assert not (tmp_path / "deploy" / "Dockerfile").exists()


def test_ship_rejects_a_dev_or_unknown_extra(tmp_path: Path) -> None:
    _workflow(_project(tmp_path), HTTP_TOOL)
    for bad in ("codegen", "nope"):
        res = _ship(tmp_path, "--extra", bad)
        assert res.exit_code == 1 and "choose from" in res.output


def test_a_kept_dockerfile_that_lacks_a_needed_extra_aborts(tmp_path: Path) -> None:
    _workflow(_project(tmp_path), HTTP_TOOL)
    assert _ship(tmp_path).exit_code == 0
    res = _ship(tmp_path, "--extra", "redis")
    assert res.exit_code == 1 and "does not install tuvl[redis]" in res.output
    forced = _ship(tmp_path, "--extra", "redis", "--force")
    assert forced.exit_code == 0
    assert 'LABEL io.tuvl.extras="redis"' in (tmp_path / "deploy" / "Dockerfile").read_text()


def test_a_kept_dockerfile_from_before_extras_aborts_only_when_extras_are_needed(
    tmp_path: Path,
) -> None:
    _workflow(_project(tmp_path), HTTP_TOOL)
    assert _ship(tmp_path).exit_code == 0
    dockerfile = tmp_path / "deploy" / "Dockerfile"
    dockerfile.write_text(
        "\n".join(line for line in dockerfile.read_text().splitlines() if "LABEL" not in line)
    )
    assert _ship(tmp_path).exit_code == 0
    res = _ship(tmp_path, "--extra", "mcp")
    assert res.exit_code == 1 and "predates extras" in res.output


def test_only_runtime_extras_are_shippable() -> None:
    assert {n for n, e in EXTRAS.items() if e.shipped} == {"mcp", "redis", "vector", "otel-grpc"}


def test_a_grpc_otlp_protocol_needs_otel_grpc(tmp_path: Path) -> None:
    _project(tmp_path)
    (tmp_path / ".tuvl").mkdir()
    (tmp_path / ".tuvl" / "telemetry.yaml").write_text("otlp_protocol: grpc\n")
    assert ship_deps.detect(tmp_path).names == ["otel-grpc"]
    (tmp_path / ".tuvl" / "telemetry.yaml").write_text("otlp_protocol: http/protobuf\n")
    assert ship_deps.detect(tmp_path).names == []
