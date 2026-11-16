"""The model client opens as many connections as it is asked to, unless the operator sets a cap."""

from __future__ import annotations

import pytest

from tuvl import llm


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    class Client:
        is_closed = False

        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(llm.httpx, "AsyncClient", Client)
    llm._clients.clear()
    yield seen
    llm._clients.clear()


async def test_by_default_there_is_no_cap(captured, monkeypatch) -> None:
    monkeypatch.delenv("TUVL_LLM_MAX_CONNECTIONS", raising=False)
    llm._client()
    assert captured["limits"].max_connections is None


async def test_an_operator_can_cap_the_connections(captured, monkeypatch) -> None:
    monkeypatch.setenv("TUVL_LLM_MAX_CONNECTIONS", "64")
    llm._client()
    assert captured["limits"].max_connections == 64


@pytest.mark.parametrize("raw", ["", "0", "-3", "many"])
async def test_a_value_that_is_not_a_positive_number_means_no_cap(
    captured, monkeypatch, raw
) -> None:
    monkeypatch.setenv("TUVL_LLM_MAX_CONNECTIONS", raw)
    llm._client()
    assert captured["limits"].max_connections is None
