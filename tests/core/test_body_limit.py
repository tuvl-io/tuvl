"""A cap on the size of a request body, enforced before the application has read it all."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tuvl.core.body_limit import BodyLimitMiddleware

pytestmark = pytest.mark.asyncio

LIMIT = 1024
reached: list[int] = []


async def echo(request: Request) -> JSONResponse:
    body = await request.body()
    reached.append(len(body))
    return JSONResponse({"bytes": len(body)})


def client(limit: int = LIMIT) -> httpx.AsyncClient:
    app = Starlette(routes=[Route("/{path:path}", echo, methods=["POST"])])
    app.add_middleware(BodyLimitMiddleware, limit=limit)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear() -> None:
    reached.clear()


async def test_a_body_within_the_limit_passes() -> None:
    async with client() as c:
        response = await c.post("/x", content=b"a" * LIMIT)
    assert response.status_code == 200 and response.json() == {"bytes": LIMIT}


async def test_a_declared_length_over_the_limit_is_refused_without_reading_it() -> None:
    async with client() as c:
        response = await c.post("/x", content=b"a" * (LIMIT + 1))
    assert response.status_code == 413
    assert reached == []


async def test_a_chunked_body_over_the_limit_is_refused() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"a" * 300

    async with client() as c:
        response = await c.post("/x", content=chunks())  # no Content-Length to check
    assert response.status_code == 413
    assert reached == []


async def test_a_multipart_body_is_left_to_the_upload_settings() -> None:
    async with client() as c:
        response = await c.post(
            "/x",
            content=b"a" * (LIMIT * 4),
            headers={"content-type": "multipart/form-data; boundary=x"},
        )
    assert response.status_code == 200


async def test_the_dev_api_is_not_capped() -> None:
    async with client() as c:
        response = await c.post("/dev/text/specs/big.md", content=b"a" * (LIMIT * 4))
    assert response.status_code == 200


async def test_a_limit_of_zero_turns_it_off() -> None:
    async with client(limit=0) as c:
        response = await c.post("/x", content=b"a" * (LIMIT * 4))
    assert response.status_code == 200
