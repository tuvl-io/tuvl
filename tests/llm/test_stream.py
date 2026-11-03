"""``astream``: server-sent events from faked endpoints, folded into the same ``Completion``."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.llm.test_client import FORMAT, Http, body, chat_json, http, serve  # noqa: F401
from tuvl import llm

MESSAGES = [{"role": "user", "content": "x"}]


def sse(*events: Any, event_names: list[str] | None = None, tail: str = "") -> httpx.Response:
    """A ``text/event-stream`` body: each dict is JSON, a str is sent as is."""
    lines: list[str] = []
    for i, item in enumerate(events):
        if event_names:
            lines.append(f"event: {event_names[i]}")
        lines.append("data: " + (item if isinstance(item, str) else json.dumps(item)))
        lines.append("")
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("\n".join(lines) + tail).encode(),
    )


def chunk(
    delta: dict[str, Any] | None = None, finish: str | None = None, **extra: Any
) -> dict[str, Any]:
    choices = (
        [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
        if (delta is not None or finish)
        else []
    )
    return {"model": "m", "choices": choices, **extra}


async def collect(**kwargs: Any) -> tuple[list[str], llm.Completion]:
    deltas: list[str] = []
    end: llm.Completion | None = None
    async for event in llm.astream(messages=MESSAGES, api_key="k", **kwargs):
        if isinstance(event, llm.TextDelta):
            deltas.append(event.text)
        else:
            end = event.completion
    assert end is not None
    return deltas, end


OPENAI_TEXT = [
    chunk({"role": "assistant", "content": ""}),
    chunk({"content": "Hel"}),
    chunk({"content": "lo"}),
    chunk({}, "stop"),
    chunk(usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
    "[DONE]",
]


async def test_openai_text_stream(http: Http) -> None:  # noqa: F811
    serve(http, lambda r: sse(*OPENAI_TEXT))
    deltas, done = await collect(model="openai/gpt-4o", max_tokens=50)
    assert deltas == ["Hel", "lo"]
    assert done.choices[0].message.content == "Hello"
    assert done.choices[0].finish_reason == "stop"
    assert (done.usage.prompt_tokens, done.usage.completion_tokens, done.usage.total_tokens) == (
        3,
        2,
        5,
    )
    sent = body(http[0])
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert sent["max_completion_tokens"] == 50


async def test_a_stream_ends_with_the_completion_acompletion_returns(http: Http) -> None:  # noqa: F811
    serve(http, lambda r: httpx.Response(200, json=chat_json("Hello")))
    plain = await llm.acompletion(model="openai/gpt-4o", messages=MESSAGES, api_key="k")
    serve(http, lambda r: sse(*OPENAI_TEXT))
    _, streamed = await collect(model="openai/gpt-4o")
    assert streamed.choices[0].message.content == plain.choices[0].message.content
    assert streamed.choices[0].finish_reason == plain.choices[0].finish_reason
    assert streamed.usage == plain.usage


async def test_structured_output_streams_as_json_text(http: Http) -> None:  # noqa: F811
    parts = ['{"a"', ': "he', 'llo"}']
    serve(
        http,
        lambda r: sse(*[chunk({"content": p}) for p in parts], chunk({}, "stop"), "[DONE]"),
    )
    deltas, done = await collect(model="openai/gpt-4o", response_format=FORMAT)
    assert "".join(deltas) == '{"a": "hello"}'
    assert json.loads(done.choices[0].message.content or "") == {"a": "hello"}


async def test_tool_call_arguments_are_assembled(http: Http) -> None:  # noqa: F811
    calls = [
        chunk(
            {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": ""}}]}
        ),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"x"'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]}),
        chunk(
            {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "g", "arguments": "{}"}}]}
        ),
        chunk({}, "tool_calls"),
        "[DONE]",
    ]
    serve(http, lambda r: sse(*calls))
    deltas, done = await collect(
        model="openai/gpt-4o", tools=[{"type": "function", "function": {"name": "f"}}]
    )
    message = done.choices[0].message
    assert deltas == [] and message.content is None
    assert [(c.id, c.function.name, c.function.arguments) for c in message.tool_calls or []] == [
        ("c1", "f", '{"x": 1}'),
        ("c2", "g", "{}"),
    ]
    assert done.choices[0].finish_reason == "tool_calls"


async def test_gemini_tool_call_extra_content_is_preserved_in_stream(http: Http) -> None:  # noqa: F811
    calls = [
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c1",
                        "function": {"name": "lookup", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": "sig-123"}},
                    }
                ]
            }
        ),
        chunk({}, "tool_calls"),
        "[DONE]",
    ]
    serve(http, lambda r: sse(*calls))

    _, done = await collect(model="gemini/gemini-3.1-flash-lite")

    call = done.choices[0].message.tool_calls[0]  # type: ignore[index]
    assert call.extra_content == {"google": {"thought_signature": "sig-123"}}


async def test_comments_and_multiline_data_are_handled(http: Http) -> None:  # noqa: F811
    text = (
        ": keep-alive\n\n"
        'data: {"model": "m",\ndata: "choices": [{"delta": {"content": "ok"}, "finish_reason": null}]}\n\n'
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    serve(http, lambda r: httpx.Response(200, content=text.encode()))
    deltas, done = await collect(model="openai/gpt-4o")
    assert deltas == ["ok"] and done.choices[0].message.content == "ok"


async def test_a_server_that_rejects_stream_options_is_asked_again_without_them(http: Http) -> None:  # noqa: F811
    def answer(request: httpx.Request) -> httpx.Response:
        if "stream_options" in body(request):
            return httpx.Response(400, json={"error": {"message": "Unknown field: stream_options"}})
        return sse(chunk({"content": "hi"}), chunk({}, "stop"), "[DONE]")

    serve(http, answer)
    deltas, done = await collect(model="ollama/llama3", api_base="http://box:11434")
    assert deltas == ["hi"] and done.usage.total_tokens == 0
    assert len(http) == 2 and "stream_options" not in body(http[1])


async def test_an_error_before_the_stream_starts_is_raised(http: Http) -> None:  # noqa: F811
    serve(http, lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(llm.RateLimitError, match="slow down"):
        await collect(model="openai/gpt-4o")


async def test_an_error_in_the_middle_of_the_stream_is_raised_after_its_deltas(http: Http) -> None:  # noqa: F811
    serve(
        http, lambda r: sse(chunk({"content": "par"}), {"error": {"message": "boom", "code": 503}})
    )
    seen: list[str] = []
    with pytest.raises(llm.ServiceUnavailableError, match="boom"):
        async for event in llm.astream(model="openai/gpt-4o", messages=MESSAGES, api_key="k"):
            if isinstance(event, llm.TextDelta):
                seen.append(event.text)
    assert seen == ["par"]


async def test_a_stream_that_stops_early_is_an_error(http: Http) -> None:  # noqa: F811
    serve(http, lambda r: sse(chunk({"content": "half"})))
    with pytest.raises(llm.LLMError, match="before the model finished"):
        await collect(model="openai/gpt-4o")


async def test_silence_is_a_timeout(http: Http) -> None:  # noqa: F811
    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("quiet", request=request)

    serve(http, answer)
    with pytest.raises(llm.Timeout):
        await collect(model="openai/gpt-4o", timeout=0.5)


async def test_a_provider_that_only_embeds_cannot_stream() -> None:
    with pytest.raises(llm.UnsupportedProviderError):
        await collect(model="voyage/voyage-3")


def anthropic(*events: tuple[str, dict[str, Any]]) -> httpx.Response:
    return sse(*[e[1] for e in events], event_names=[e[0] for e in events])


def message_start(tokens: int = 7) -> tuple[str, dict[str, Any]]:
    return "message_start", {
        "type": "message_start",
        "message": {"model": "claude-x", "usage": {"input_tokens": tokens, "output_tokens": 1}},
    }


def block_start(index: int, block: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return "content_block_start", {
        "type": "content_block_start",
        "index": index,
        "content_block": block,
    }


def block_delta(index: int, delta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}


def message_end(stop: str, tokens: int) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop},
                "usage": {"output_tokens": tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


async def test_anthropic_text_stream(http: Http) -> None:  # noqa: F811
    serve(
        http,
        lambda r: anthropic(
            message_start(),
            ("ping", {"type": "ping"}),
            block_start(0, {"type": "text", "text": ""}),
            block_delta(0, {"type": "text_delta", "text": "Hel"}),
            block_delta(0, {"type": "text_delta", "text": "lo"}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            *message_end("end_turn", 4),
        ),
    )
    deltas, done = await collect(model="anthropic/claude-x", max_tokens=100)
    assert deltas == ["Hel", "lo"] and done.choices[0].message.content == "Hello"
    assert done.choices[0].finish_reason == "stop"
    assert (done.usage.prompt_tokens, done.usage.completion_tokens, done.usage.total_tokens) == (
        7,
        4,
        11,
    )
    assert body(http[0])["stream"] is True and "stream_options" not in body(http[0])
    assert done.model == "claude-x"


async def test_anthropic_structured_output_streams_the_json(http: Http) -> None:  # noqa: F811
    serve(
        http,
        lambda r: anthropic(
            message_start(),
            block_start(0, {"type": "tool_use", "id": "t1", "name": "json_response", "input": {}}),
            block_delta(0, {"type": "input_json_delta", "partial_json": '{"a": "he'}),
            block_delta(0, {"type": "input_json_delta", "partial_json": 'llo"}'}),
            *message_end("tool_use", 6),
        ),
    )
    deltas, done = await collect(model="anthropic/claude-x", response_format=FORMAT)
    assert "".join(deltas) == '{"a": "hello"}'
    assert json.loads(done.choices[0].message.content or "") == {"a": "hello"}
    assert done.choices[0].finish_reason == "stop" and not done.choices[0].message.tool_calls


async def test_anthropic_tool_use_is_assembled_and_not_streamed_as_text(http: Http) -> None:  # noqa: F811
    serve(
        http,
        lambda r: anthropic(
            message_start(),
            block_start(0, {"type": "text", "text": ""}),
            block_delta(0, {"type": "text_delta", "text": "Looking."}),
            block_start(1, {"type": "tool_use", "id": "t9", "name": "lookup", "input": {}}),
            block_delta(1, {"type": "input_json_delta", "partial_json": '{"id": '}),
            block_delta(1, {"type": "input_json_delta", "partial_json": "42}"}),
            *message_end("tool_use", 9),
        ),
    )
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    deltas, done = await collect(model="anthropic/claude-x", tools=tools)
    message = done.choices[0].message
    assert deltas == ["Looking."] and message.content == "Looking."
    assert [
        (c.id, c.function.name, json.loads(c.function.arguments)) for c in message.tool_calls or []
    ] == [("t9", "lookup", {"id": 42})]
    assert done.choices[0].finish_reason == "tool_calls"


async def test_anthropic_overload_in_the_middle_of_a_stream(http: Http) -> None:  # noqa: F811
    serve(
        http,
        lambda r: anthropic(
            message_start(),
            block_start(0, {"type": "text", "text": ""}),
            block_delta(0, {"type": "text_delta", "text": "par"}),
            (
                "error",
                {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            ),
        ),
    )
    with pytest.raises(llm.ServiceUnavailableError, match="Overloaded"):
        await collect(model="anthropic/claude-x")
