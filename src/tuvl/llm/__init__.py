"""tuvl's model client: chat completions and embeddings over HTTP.

Model ids are ``provider/model`` (a bare id is an OpenAI model). OpenAI-compatible
endpoints (OpenAI, Gemini, Ollama, Groq, Mistral, vLLM, any gateway through
``api_base``) share one wire format; Anthropic has its own adapter.
"""

from __future__ import annotations

import asyncio
import os
import time
import weakref
from collections.abc import AsyncIterator
from typing import Any

import httpx
from opentelemetry import trace

from tuvl.llm import anthropic_wire, openai_wire
from tuvl.llm.errors import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    InternalServerError,
    LLMError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
    UnsupportedProviderError,
    from_status,
)
from tuvl.llm.routes import KEYLESS, PROVIDERS, provider_of, resolve
from tuvl.llm.types import Completion, Embeddings, StreamEnd, StreamEvent, TextDelta

__all__ = [
    "KEYLESS",
    "PROVIDERS",
    "APIConnectionError",
    "AuthenticationError",
    "BadRequestError",
    "Completion",
    "ConfigurationError",
    "Embeddings",
    "InternalServerError",
    "LLMError",
    "RateLimitError",
    "ServiceUnavailableError",
    "StreamEnd",
    "StreamEvent",
    "TextDelta",
    "Timeout",
    "UnsupportedProviderError",
    "aclose",
    "acompletion",
    "aembedding",
    "astream",
    "provider_of",
]

_DEFAULT_TIMEOUT = 600.0
_tracer = trace.get_tracer("tuvl.llm")


def _limits() -> httpx.Limits:
    """No connection cap unless ``TUVL_LLM_MAX_CONNECTIONS`` sets one (each call used to get its own client;
    a long completion must not queue behind others, but a burst of runs must not open thousands of
    sockets either: with a cap, calls past it wait for a connection). Idle connections expire before the
    5 s keep-alive of uvicorn-served models (Ollama, vLLM), so a reused connection is not one the server
    is closing."""
    raw = os.environ.get("TUVL_LLM_MAX_CONNECTIONS", "").strip()
    cap = int(raw) if raw.isdigit() and int(raw) > 0 else None
    return httpx.Limits(max_connections=cap, max_keepalive_connections=32, keepalive_expiry=4.0)


_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def _client() -> httpx.AsyncClient:
    """This event loop's pooled client; the timeout is set per request."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, limits=_limits())
        _clients[loop] = client
    return client


async def aclose() -> None:
    """Close the running event loop's pooled client (server shutdown)."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()


def _message_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or response.reason_phrase
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(error, str):
        return error
    return str(body)[:500]


async def _post(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: float | None
) -> dict[str, Any]:
    try:
        response = await _client().post(
            url, headers=headers, json=body, timeout=timeout or _DEFAULT_TIMEOUT
        )
    except httpx.TimeoutException as exc:
        raise Timeout(f"no answer from {url} within {timeout or _DEFAULT_TIMEOUT:g}s") from exc
    except httpx.HTTPError as exc:
        raise APIConnectionError(f"cannot reach {url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400:
        raise from_status(response.status_code, _message_of(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise InternalServerError(f"{url} did not return JSON") from exc
    if not isinstance(data, dict):
        raise InternalServerError(f"{url} returned an unexpected body")
    return data


async def acompletion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    logprobs: bool | None = None,
) -> Completion:
    """One chat completion in the OpenAI response shape."""
    route = resolve(model, api_base, api_key)
    if route.provider.embeddings_only:
        raise UnsupportedProviderError(f"'{route.provider.name}' only serves embeddings")
    params = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "tools": tools,
        "tool_choice": tool_choice,
        "logprobs": logprobs,
    }
    wire = anthropic_wire if route.provider.wire == "anthropic" else openai_wire
    url, body = wire.chat_request(route, params)
    with _tracer.start_as_current_span(f"chat {route.model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", route.provider.name)
        span.set_attribute("gen_ai.request.model", route.model)
        try:
            data = await _post(url, wire.headers(route), body, timeout)
        except LLMError as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
            raise
        result: Completion = wire.parse_chat(data, route.model)
        span.set_attribute("gen_ai.usage.input_tokens", result.usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", result.usage.completion_tokens)
        if result.choices and result.choices[0].finish_reason:
            span.set_attribute("gen_ai.response.finish_reasons", [result.choices[0].finish_reason])
    return result


async def _sse(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """``(event, data)`` of each server-sent event; comments and keep-alives are skipped."""
    event = ""
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "", []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
    if data:
        yield event, "\n".join(data)


async def _stream_events(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: float | None
) -> AsyncIterator[tuple[str, str]]:
    """POST and yield the response's server-sent events. ``timeout`` is the longest silence,
    not the whole call: a stream that keeps producing never times out here."""
    limit = timeout or _DEFAULT_TIMEOUT
    try:
        async with _client().stream(
            "POST", url, headers=headers, json=body, timeout=limit
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise from_status(response.status_code, _message_of(response))
            async for item in _sse(response):
                yield item
    except httpx.TimeoutException as exc:
        raise Timeout(f"nothing from {url} for {limit:g}s") from exc
    except httpx.HTTPError as exc:
        raise APIConnectionError(f"cannot reach {url}: {type(exc).__name__}: {exc}") from exc


async def astream(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    logprobs: bool | None = None,
) -> AsyncIterator[StreamEvent]:
    """A chat completion as it is produced: ``TextDelta`` events, then one ``StreamEnd`` holding
    the ``Completion`` ``acompletion`` would have returned. With a structured ``response_format``
    the deltas are pieces of the JSON text. ``timeout`` is the longest silence between chunks."""
    route = resolve(model, api_base, api_key)
    if route.provider.embeddings_only:
        raise UnsupportedProviderError(f"'{route.provider.name}' only serves embeddings")
    params: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "tools": tools,
        "tool_choice": tool_choice,
        "logprobs": logprobs,
        "stream": True,
    }
    wire = anthropic_wire if route.provider.wire == "anthropic" else openai_wire
    with _tracer.start_as_current_span(f"chat {route.model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", route.provider.name)
        span.set_attribute("gen_ai.request.model", route.model)
        span.set_attribute("tuvl.llm.stream", True)
        started = time.monotonic()
        for with_usage in (True, False):
            url, request = wire.chat_request(route, {**params, "stream_usage": with_usage})
            builder = wire.StreamBuilder(route.model)
            first = True
            try:
                async for event, data in _stream_events(url, wire.headers(route), request, timeout):
                    piece = builder.feed(event, data)
                    if piece:
                        if first:
                            first = False
                            span.set_attribute(
                                "tuvl.llm.first_token_ms",
                                round((time.monotonic() - started) * 1000),
                            )
                        yield TextDelta(piece)
                result: Completion = builder.completion()
            except BadRequestError as exc:
                # a compatible server that rejects ``stream_options``: ask again without it
                if with_usage and first and "stream_options" in str(exc):
                    continue
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
                raise
            except LLMError as exc:
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
                raise
            break
        span.set_attribute("gen_ai.usage.input_tokens", result.usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", result.usage.completion_tokens)
        if result.choices and result.choices[0].finish_reason:
            span.set_attribute("gen_ai.response.finish_reasons", [result.choices[0].finish_reason])
    yield StreamEnd(result)


async def aembedding(
    *,
    model: str,
    input: list[str],
    dimensions: int | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float | None = None,
) -> Embeddings:
    """Embeddings for *input*, in order."""
    route = resolve(model, api_base, api_key)
    if route.provider.wire != "openai":
        raise UnsupportedProviderError(f"'{route.provider.name}' has no embeddings endpoint")
    url, body = openai_wire.embed_request(route, {"input": input, "dimensions": dimensions})
    with _tracer.start_as_current_span(f"embeddings {route.model}") as span:
        span.set_attribute("gen_ai.operation.name", "embeddings")
        span.set_attribute("gen_ai.system", route.provider.name)
        span.set_attribute("gen_ai.request.model", route.model)
        data = await _post(url, openai_wire.headers(route), body, timeout)
    return openai_wire.parse_embeddings(data)
