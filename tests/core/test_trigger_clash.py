"""Two workflows claiming one HTTP path: a mistake the build should not carry to production."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from tuvl.api.triggers import mount_triggers
from tuvl.contract.schema import WorkflowDocument
from tuvl.core.config import settings


def workflow(name: str, path: str = "/orders") -> WorkflowDocument:
    return WorkflowDocument.model_validate(
        {
            "kind": "Workflow",
            "version": "tuvl/v2",
            "metadata": {"name": name},
            "spec": {
                "trigger": {"http": {"path": path, "method": "POST"}},
                "agents": [
                    {
                        "id": "p",
                        "description": "d",
                        "engine": "pending",
                        "routes": {"default": "END"},
                    }
                ],
            },
        }
    )


def test_in_development_the_second_workflow_is_skipped_with_an_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tuvl_dev_mode", True)
    app = FastAPI()
    assert mount_triggers(app, {"a": workflow("a"), "b": workflow("b")}) == 1


def test_in_production_a_path_clash_stops_the_boot(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    monkeypatch.setattr(settings, "tuvl_env", "production")
    app = FastAPI()
    with pytest.raises(RuntimeError, match=r"'b'.*POST /orders"):
        mount_triggers(app, {"a": workflow("a"), "b": workflow("b")})


def test_in_production_a_clash_with_an_existing_route_stops_the_boot(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tuvl_dev_mode", False)
    monkeypatch.setattr(settings, "tuvl_env", "production")
    app = FastAPI()

    @app.post("/orders")
    def existing() -> None: ...

    with pytest.raises(RuntimeError, match="/orders"):
        mount_triggers(app, {"a": workflow("a")})
