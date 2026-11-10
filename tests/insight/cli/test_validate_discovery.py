"""TD-14 regression: `tuvl validate`'s file discovery must match the runtime's
recursive, kind-dispatched walk (``config_loader.load_all_configs``), not a
non-recursive glob over `config.yaml`'s fixed ``dirs:``.

Before the fix: a resource living in a subdirectory, a ``.yml`` file, or one
of several less-common kinds (EmbeddingRegistry/Config, CollectionRegistry/
Config, RedisConfig, FederationProvider) was invisible to `tuvl validate` /
the `tuvl ship` gate even though the runtime loaded it fine. A misspelled
``kind:`` was also silently ignored by both.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from tuvl.cli import app
from tuvl_insight.cli.commands.validate import run_validation

cli_runner = CliRunner()


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


def _minimal_workflow(name: str, path: str) -> dict:
    return {
        "kind": "Workflow",
        "version": "tuvl/v2",
        "metadata": {"name": name},
        "spec": {
            "trigger": {"http": {"path": path, "method": "POST"}},
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


# ── subdirectory / extension-agnostic discovery ──────────────────────────────


def test_workflow_in_nested_subdirectory_is_validated(tmp_path: Path) -> None:
    _write(
        tmp_path / "workflows" / "nested" / "deep" / "wf.yaml",
        _minimal_workflow("nested_wf", "/api/nested"),
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert not result.errors
    assert not any("No Workflow YAML files found" in i.message for i in result.warnings)


def test_workflow_outside_conventional_directory_is_validated(tmp_path: Path) -> None:
    # Lives entirely outside workflows/ — the runtime dispatches purely by
    # `kind:`, independent of directory name.
    _write(
        tmp_path / "resources" / "misc" / "standalone.yaml",
        _minimal_workflow("standalone", "/api/standalone"),
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert not result.errors
    assert not any("No Workflow YAML files found" in i.message for i in result.warnings)


def test_workflow_subdirectory_seen_via_cli(tmp_path: Path) -> None:
    """End-to-end through the `tuvl validate` command, not just the internal
    function — proves the ship gate itself sees the file."""
    _write(
        tmp_path / "workflows" / "nested" / "wf.yaml",
        _minimal_workflow("nested_wf", "/api/nested"),
    )
    res = cli_runner.invoke(app, ["lint", "project", "-d", str(tmp_path)])
    assert res.exit_code == 0, res.output


def test_yml_model_file_is_validated(tmp_path: Path) -> None:
    _write(
        tmp_path / "models" / "book.yml",
        {
            "kind": "ModelDefinition",
            "metadata": {"name": "Book"},
            "spec": {"tablename": "books", "fields": [{"name": "id", "type": "uuid"}]},
        },
    )
    result, known_models, _known_nodes, _known_llms = run_validation(tmp_path, echo=False)
    assert "Book" in known_models
    assert not result.errors


# ── misspelled / unrecognized kind ───────────────────────────────────────────


def test_misspelled_kind_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path / "workflows" / "typo.yaml",
        {"kind": "Worfklow", "metadata": {"name": "x"}, "spec": {}},
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert any("Unknown kind 'Worfklow'" in i.message for i in result.errors)


# ── previously-unvalidated kinds: recognized, and a real error is caught ────


def test_redisconfig_error_is_caught(tmp_path: Path) -> None:
    _write(
        tmp_path / "datasources" / "cache.yaml",
        {"kind": "RedisConfig", "metadata": {"name": "cache"}, "spec": {"connection": {}}},
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert any("spec.type is required" in i.message for i in result.errors)


def test_redisconfig_clean_does_not_error(tmp_path: Path) -> None:
    _write(
        tmp_path / "datasources" / "cache.yaml",
        {
            "kind": "RedisConfig",
            "metadata": {"name": "cache"},
            "spec": {"type": "redis", "connection": {"host": "localhost", "port": 6379}},
        },
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert not result.errors


def test_federationprovider_missing_name_is_caught(tmp_path: Path) -> None:
    _write(
        tmp_path / "federation" / "google.yaml",
        {"kind": "FederationProvider", "metadata": {}, "spec": {"provider": "google"}},
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert any("metadata.name is required" in i.message for i in result.errors)


def test_embeddingconfig_is_recognized(tmp_path: Path) -> None:
    _write(
        tmp_path / "models" / "embeddings.yaml",
        {
            "kind": "EmbeddingConfig",
            "metadata": {"name": "default"},
            "spec": {"provider": "openai", "model": "openai/text-embedding-3-small"},
        },
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert not result.errors
    assert not any("Unknown kind" in i.message for i in result.errors)


def test_collectionregistry_unknown_embedding_warns(tmp_path: Path) -> None:
    _write(
        tmp_path / "models" / "embeddings.yaml",
        {
            "kind": "EmbeddingRegistry",
            "metadata": {"name": "default"},
            "spec": {"embeddings": [{"name": "default", "model": "openai/text-embedding-3-small"}]},
        },
    )
    _write(
        tmp_path / "models" / "collections.yaml",
        {
            "kind": "CollectionRegistry",
            "metadata": {"name": "default"},
            "spec": {"collections": [{"name": "kb", "embedding": "ghost"}]},
        },
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert any("embedding 'ghost' matches no known" in i.message for i in result.warnings)


# ── tuvl-ship's own output must not confuse the discovery it gates ─────────


def test_previously_shipped_deploy_dir_does_not_break_revalidation(tmp_path: Path) -> None:
    """`deploy/` (written by `tuvl ship`) carries literal Kubernetes `kind:`
    fields (Deployment, Service) via Helm templates using Go `{{ }}` syntax
    that isn't valid YAML. A re-run of `tuvl validate`/`tuvl ship` against an
    already-shipped project must not choke on its own prior output."""
    _write(tmp_path / "workflows" / "wf.yaml", _minimal_workflow("wf", "/api/wf"))
    templates = tmp_path / "deploy" / "chart" / "demo" / "templates"
    templates.mkdir(parents=True)
    (templates / "deployment.yaml").write_text(
        'apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ include "app.fullname" . }}\n'
    )
    (templates / "service.yaml").write_text(
        'apiVersion: v1\nkind: Service\nmetadata:\n  name: {{ include "app.fullname" . }}\n'
    )
    result, *_ = run_validation(tmp_path, echo=False)
    assert not result.errors
