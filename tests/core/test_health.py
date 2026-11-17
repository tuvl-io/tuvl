"""``/health`` says whether the worker loop is alive; ``/ready`` whether this process can serve."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from tuvl.core.app import app
from tuvl.core.datasources import postgres
from tuvl.runtime import host as host_module


class FakeHost:
    def __init__(self, role: str = "all", state: str = "ok") -> None:
        self.role = role
        self._state = state

    def worker_state(self, stall_seconds: float) -> str:
        return self._state


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _not_draining():
    app.state.draining = False
    yield
    app.state.draining = False


def test_health_is_ok_while_the_runtime_is_still_starting(client, monkeypatch):
    monkeypatch.setattr(host_module, "_HOST", None)
    assert client.get("/health").status_code == 200


def test_health_is_ok_while_the_worker_polls(client, monkeypatch):
    monkeypatch.setattr(host_module, "_HOST", FakeHost(state="ok"))
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("state", ["stopped", "stalled"])
def test_health_fails_when_the_worker_is_gone_or_stuck(client, monkeypatch, state):
    monkeypatch.setattr(host_module, "_HOST", FakeHost(state=state))
    response = client.get("/health")
    assert response.status_code == 503 and response.json()["worker"] == state


def test_health_ignores_the_worker_of_an_api_only_process(client, monkeypatch):
    monkeypatch.setattr(host_module, "_HOST", FakeHost(role="api", state="none"))
    assert client.get("/health").status_code == 200


def _primary(monkeypatch, engine) -> None:
    monkeypatch.setattr(postgres, "_PRIMARY_DS", "main")
    monkeypatch.setitem(postgres._engines_rw, "main", engine)
    monkeypatch.setattr(host_module, "_HOST", FakeHost())


def test_ready_pings_the_database(client, monkeypatch):
    _primary(monkeypatch, create_async_engine("sqlite+aiosqlite:///:memory:"))
    response = client.get("/ready")
    assert response.status_code == 200 and response.json()["status"] == "ready"


def test_ready_fails_when_the_database_does_not_answer(client, monkeypatch, tmp_path):
    _primary(monkeypatch, create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/no/such/dir/x.db"))
    assert client.get("/ready").status_code == 503


def test_ready_fails_before_the_runtime_has_started(client, monkeypatch):
    _primary(monkeypatch, create_async_engine("sqlite+aiosqlite:///:memory:"))
    monkeypatch.setattr(host_module, "_HOST", None)
    assert client.get("/ready").status_code == 503


def test_ready_fails_while_draining(client, monkeypatch):
    _primary(monkeypatch, create_async_engine("sqlite+aiosqlite:///:memory:"))
    app.state.draining = True
    assert client.get("/ready").status_code == 503
