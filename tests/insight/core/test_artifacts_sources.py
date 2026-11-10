"""DB-source merge, the production /api/artifacts surface, and external
sha256-pinned sources (fetch, verify, cache, refuse-unpinned)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from tuvl.core.artifacts.db import merge_db_artifacts
from tuvl.core.artifacts.external import load_external_artifacts
from tuvl.core.artifacts.models import SystemArtifact
from tuvl.core.artifacts.registry import (
    ARTIFACT_REGISTRY,
    register_artifact,
    resolve_prose,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    ARTIFACT_REGISTRY.clear()
    yield
    ARTIFACT_REGISTRY.clear()


# ── DB merge ──────────────────────────────────────────────────────────────────


def _fake_sessions(rows):
    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: rows)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result())

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return None

    return {"main": lambda: _Ctx()}


@pytest.mark.asyncio
async def test_db_rows_merge_into_registry(monkeypatch):
    from tuvl.core.datasources import postgres as pg

    rows = [
        SystemArtifact(
            name="db-prompt",
            type="prompt",
            version=1,
            content="from the database",
            content_hash="x",
        ),
        SystemArtifact(
            name="db-guard",
            type="guardrail",
            version=1,
            content=json.dumps({"checks": [{"check": "max_chars", "limit": 5}]}),
            content_hash="y",
        ),
    ]
    monkeypatch.setattr(pg, "_engines_rw", {"main": object()})
    monkeypatch.setattr(pg, "_PRIMARY_DS", "main")
    monkeypatch.setattr(pg, "_sessions_rw", _fake_sessions(rows))

    await merge_db_artifacts()
    assert resolve_prose("artifact://db-prompt", site="prompt", where="t") == "from the database"
    assert ARTIFACT_REGISTRY["db-guard"].versions[1].content == {
        "checks": [{"check": "max_chars", "limit": 5}]
    }
    assert ARTIFACT_REGISTRY["db-prompt"].source == "db"


@pytest.mark.asyncio
async def test_file_source_shadows_db_merge(monkeypatch):
    from tuvl.core.datasources import postgres as pg

    register_artifact(name="p", type_="prompt", version=1, content="file wins", source="file")
    rows = [
        SystemArtifact(name="p", type="prompt", version=1, content="db loses", content_hash="x")
    ]
    monkeypatch.setattr(pg, "_engines_rw", {"main": object()})
    monkeypatch.setattr(pg, "_PRIMARY_DS", "main")
    monkeypatch.setattr(pg, "_sessions_rw", _fake_sessions(rows))

    await merge_db_artifacts()
    assert resolve_prose("artifact://p", site="prompt", where="t") == "file wins"


@pytest.mark.asyncio
async def test_db_merge_noop_without_postgres(monkeypatch):
    from tuvl.core.datasources import postgres as pg

    monkeypatch.setattr(pg, "_engines_rw", {})
    await merge_db_artifacts()  # must not raise
    assert ARTIFACT_REGISTRY == {}


# ── production API ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_creates_version_and_registers():
    from tuvl.core.api.artifacts_router import ArtifactUpload, upload_artifact

    session = AsyncMock()
    body = ArtifactUpload(name="team-prompt", type="prompt", content="Be helpful.")
    resp = await upload_artifact(body, session=session, _=None)
    assert resp["version"] == 1
    assert session.add.called and session.commit.awaited
    assert ARTIFACT_REGISTRY["team-prompt"].source == "db"

    # Second upload → next version, never overwrite.
    resp2 = await upload_artifact(
        ArtifactUpload(name="team-prompt", type="prompt", content="v2 text"),
        session=AsyncMock(),
        _=None,
    )
    assert resp2["version"] == 2
    assert resolve_prose("artifact://team-prompt", site="prompt", where="t") == "v2 text"


@pytest.mark.asyncio
async def test_upload_rejects_existing_version_and_bad_type():
    from fastapi import HTTPException

    from tuvl.core.api.artifacts_router import ArtifactUpload, upload_artifact

    register_artifact(name="p", type_="prompt", version=1, content="x", source="db")
    with pytest.raises(HTTPException) as exc:
        await upload_artifact(
            ArtifactUpload(name="p", type="prompt", content="y", version=1),
            session=AsyncMock(),
            _=None,
        )
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        await upload_artifact(
            ArtifactUpload(name="q", type="nonsense", content="y"),
            session=AsyncMock(),
            _=None,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_upload_structured_requires_json_object():
    from fastapi import HTTPException

    from tuvl.core.api.artifacts_router import ArtifactUpload, upload_artifact

    with pytest.raises(HTTPException) as exc:
        await upload_artifact(
            ArtifactUpload(name="g", type="guardrail", content="not json"),
            session=AsyncMock(),
            _=None,
        )
    assert exc.value.status_code == 422

    resp = await upload_artifact(
        ArtifactUpload(
            name="g",
            type="guardrail",
            content=json.dumps({"checks": [{"check": "max_chars", "limit": 5}]}),
        ),
        session=AsyncMock(),
        _=None,
    )
    assert resp["version"] == 1
    assert isinstance(ARTIFACT_REGISTRY["g"].versions[1].content, dict)


def test_artifacts_router_requires_scopes():
    from tuvl.core.api.artifacts_router import router
    from tuvl.core.auth.access import bind_principal_context

    assert any(d.dependency is bind_principal_context for d in router.dependencies)


# ── external sources ──────────────────────────────────────────────────────────


def _pack(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fname, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(fname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _md(name: str, type_: str, body: str) -> str:
    front = yaml.safe_dump({"name": name, "type": type_, "version": 1})
    return f"---\n{front}---\n\n{body}"


def _mock_http(monkeypatch, payload: bytes):
    from tuvl.core.artifacts import external as ext

    async def _fetch(url):
        return payload

    monkeypatch.setattr(ext, "_fetch", _fetch)


def _set_sources(monkeypatch, tmp_path, sources):
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))
    monkeypatch.setattr(
        type(settings),
        "_project_config",
        property(lambda self: {"spec": {"artifact_sources": sources}}),
        raising=False,
    )


@pytest.mark.asyncio
async def test_external_fetch_verify_cache_register(monkeypatch, tmp_path):
    payload = _pack({"prompts/org.md": _md("org-prompt", "prompt", "Org voice.")})
    sha = hashlib.sha256(payload).hexdigest()
    _set_sources(monkeypatch, tmp_path, [{"name": "pack", "url": "https://x/p.tgz", "sha256": sha}])
    _mock_http(monkeypatch, payload)

    await load_external_artifacts()
    assert resolve_prose("artifact://org-prompt", site="prompt", where="t") == "Org voice."
    assert ARTIFACT_REGISTRY["org-prompt"].source == "external"
    # Warm cache on disk, keyed by the pinned hash.
    assert (tmp_path / ".tuvl" / "artifacts" / "cache" / sha).is_dir()

    # Second boot: cache warm → no fetch needed.
    from tuvl.core.artifacts import external as ext

    async def _no_fetch(url):  # pragma: no cover - guard
        raise AssertionError("must not fetch with a warm cache")

    monkeypatch.setattr(ext, "_fetch", _no_fetch)
    ARTIFACT_REGISTRY.clear()
    await load_external_artifacts()
    assert "org-prompt" in ARTIFACT_REGISTRY


@pytest.mark.asyncio
async def test_external_integrity_mismatch_refused(monkeypatch, tmp_path):
    payload = _pack({"p.md": _md("evil", "prompt", "tampered")})
    _set_sources(
        monkeypatch, tmp_path, [{"name": "pack", "url": "https://x/p.tgz", "sha256": "0" * 64}]
    )
    _mock_http(monkeypatch, payload)
    await load_external_artifacts()
    assert "evil" not in ARTIFACT_REGISTRY
    assert not (tmp_path / ".tuvl" / "artifacts" / "cache" / ("0" * 64)).exists()


@pytest.mark.asyncio
async def test_external_unpinned_source_refused(monkeypatch, tmp_path):
    payload = _pack({"p.md": _md("unpinned", "prompt", "x")})
    _set_sources(monkeypatch, tmp_path, [{"name": "pack", "url": "https://x/p.tgz"}])
    _mock_http(monkeypatch, payload)
    await load_external_artifacts()
    assert "unpinned" not in ARTIFACT_REGISTRY


@pytest.mark.asyncio
async def test_external_never_shadows_project_file(monkeypatch, tmp_path):
    register_artifact(name="p", type_="prompt", version=1, content="file wins", source="file")
    payload = _pack({"p.md": _md("p", "prompt", "external loses")})
    sha = hashlib.sha256(payload).hexdigest()
    _set_sources(monkeypatch, tmp_path, [{"name": "pack", "url": "https://x/p.tgz", "sha256": sha}])
    _mock_http(monkeypatch, payload)
    await load_external_artifacts()
    assert resolve_prose("artifact://p", site="prompt", where="t") == "file wins"


# ── validator: sources must be pinned; cold cache reported ────────────────────


def test_validate_artifact_sources(tmp_path):
    from tuvl_insight.cli.commands.validate import ValidateResult, _validate_artifacts

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ProjectConfig",
                "spec": {
                    "artifact_sources": [
                        {"name": "unpinned", "url": "https://x/a.tgz"},
                        {"name": "cold", "url": "https://x/b.tgz", "sha256": "a" * 64},
                    ]
                },
            }
        )
    )
    result = ValidateResult()
    _validate_artifacts(tmp_path, result)
    assert any("sha256 is REQUIRED" in i.message for i in result.errors)
    assert any("not fetched" in i.message for i in result.warnings)


def test_validate_warm_cache_artifacts_known(tmp_path):
    from tuvl_insight.cli.commands.validate import ValidateResult, _validate_artifacts

    sha = "b" * 64
    cache = tmp_path / ".tuvl" / "artifacts" / "cache" / sha
    cache.mkdir(parents=True)
    (cache / "org.md").write_text(_md("org-prompt", "prompt", "text"))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ProjectConfig",
                "spec": {"artifact_sources": [{"name": "warm", "url": "https://x", "sha256": sha}]},
            }
        )
    )
    result = ValidateResult()
    known = _validate_artifacts(tmp_path, result)
    assert "org-prompt" in known
