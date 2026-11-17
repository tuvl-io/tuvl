"""The federation-provider admin API checks what it writes, and a masked secret does not replace the real one."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tuvl.core.auth import federation, iam_service
from tuvl.core.auth.federation_loader import FEDERATION_REGISTRY
from tuvl.core.auth.iam_service import ValidationError
from tuvl.core.auth.token import mint_token
from tuvl.core.config import settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "tuvl_project_dir", str(tmp_path))
    snapshot = dict(FEDERATION_REGISTRY)
    FEDERATION_REGISTRY.clear()
    yield tmp_path / "federation"
    FEDERATION_REGISTRY.clear()
    FEDERATION_REGISTRY.update(snapshot)


def doc(**spec: Any) -> dict[str, Any]:
    return {
        "kind": "FederationProvider",
        "version": "v1",
        "metadata": {"name": "acme"},
        "spec": {"provider": "google", "client_id": "cid", "client_secret": "shh", **spec},
    }


async def test_a_valid_provider_is_saved_and_read_back_masked(folder) -> None:
    await iam_service.save_federation_provider("acme", doc(allowed_domains=["example.com"]))
    shown = await iam_service.get_federation_provider("acme")
    assert shown["spec"]["client_secret"] == "***" and shown["spec"]["client_id"] == "cid"
    assert FEDERATION_REGISTRY["acme"]["spec"]["client_secret"] == "shh"


async def test_an_environment_reference_is_a_valid_secret(folder, monkeypatch) -> None:
    monkeypatch.setenv("ACME_SECRET", "from-env")
    await iam_service.save_federation_provider("acme", doc(client_secret="${ACME_SECRET}"))
    stored = yaml.safe_load((folder / "acme.yaml").read_text())
    assert stored["spec"]["client_secret"] == "${ACME_SECRET}"
    assert FEDERATION_REGISTRY["acme"]["spec"]["client_secret"] == "from-env"


async def test_the_masked_secret_keeps_the_one_stored(folder) -> None:
    await iam_service.save_federation_provider("acme", doc())
    edited = await iam_service.get_federation_provider("acme")  # what an admin UI holds
    edited["spec"]["client_id"] = "renamed"
    await iam_service.save_federation_provider("acme", edited)
    stored = yaml.safe_load((folder / "acme.yaml").read_text())
    assert stored["spec"] == {"provider": "google", "client_id": "renamed", "client_secret": "shh"}
    assert FEDERATION_REGISTRY["acme"]["spec"]["client_secret"] == "shh"


async def test_the_mask_cannot_create_a_provider(folder) -> None:
    with pytest.raises(ValidationError, match="client_secret"):
        await iam_service.save_federation_provider("acme", doc(client_secret="***"))
    assert not (folder / "acme.yaml").exists()


def broken(**change: Any) -> dict[str, Any]:
    out = copy.deepcopy(doc())
    for dotted, value in change.items():
        target = out
        *path, last = dotted.split("__")
        for part in path:
            target = target[part]
        if value is None:
            target.pop(last, None)
        else:
            target[last] = value
    return out


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        (broken(kind="Workflow"), "kind"),
        (broken(metadata__name="other"), "name"),
        (broken(spec=None), "spec"),
        (broken(spec__provider="myspace"), "provider"),
        (broken(spec__client_id=None), "client_id"),
        (broken(spec__surprise="x"), "surprise"),
        (broken(spec__token_url="http://idp.example.com/token"), "https"),
        (broken(spec__userinfo_url="ftp://idp.example.com/me"), "https"),
        (broken(spec__allowed_users=["someone"]), "allowed_users"),
        (broken(spec__allowed_domains="example.com"), "allowed_domains"),
        (broken(spec__pkce="yes"), "pkce"),
        (broken(enabled="no"), "enabled"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_a_document_that_could_not_work_is_refused(folder, body, complaint) -> None:
    with pytest.raises(ValidationError, match=complaint):
        await iam_service.save_federation_provider("acme", body)
    assert not (folder / "acme.yaml").exists()


async def test_a_local_http_endpoint_is_allowed_for_development(folder) -> None:
    await iam_service.save_federation_provider(
        "acme",
        doc(
            provider="custom",
            auth_url="http://localhost:9000/auth",
            token_url="http://127.0.0.1:9000/token",
            userinfo_url="http://localhost:9000/me",
        ),
    )


async def test_allowed_users_is_for_github_only(folder) -> None:
    await iam_service.save_federation_provider(
        "acme", doc(provider="github", allowed_users=["octocat"])
    )


async def test_rest_answers_400_and_writes_nothing(folder) -> None:
    app = FastAPI()
    app.include_router(federation.admin_router)
    headers = {"Authorization": "Bearer " + mint_token("a", [], ["iam:admin"], ttl_seconds=300)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        bad = await client.put(
            "/auth/admin/federation/acme", json=broken(spec__provider="myspace"), headers=headers
        )
        assert bad.status_code == 400 and "provider" in bad.json()["detail"]
        ok = await client.put("/auth/admin/federation/acme", json=doc(), headers=headers)
        assert ok.status_code == 204
        shown = (await client.get("/auth/admin/federation/acme", headers=headers)).json()
        assert shown["spec"]["client_secret"] == "***"
        again = await client.put("/auth/admin/federation/acme", json=shown, headers=headers)
        assert again.status_code == 204
    assert yaml.safe_load((folder / "acme.yaml").read_text())["spec"]["client_secret"] == "shh"
