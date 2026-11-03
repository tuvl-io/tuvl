"""The built-in model client against faked HTTP endpoints."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tuvl import llm
from tuvl.core.models import agent_models

Handler = Callable[[httpx.Request], httpx.Response]


class Http(list[httpx.Request]):
    """Requests the client made; ``serve`` sets how the fake endpoint answers."""

    handler: Handler

    def serve(self, handler: Handler) -> None:
        self.handler = handler


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Http:
    seen = Http()
    real = httpx.AsyncClient

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return seen.handler(request)

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(llm.httpx, "AsyncClient", client)
    return seen


def serve(http: Http, handler: Handler) -> None:
    http.serve(handler)


def body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def chat_json(content: str = "hi", **extra: Any) -> dict[str, Any]:
    return {
        "model": "m",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        **extra,
    }


SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
    "additionalProperties": False,
}
FORMAT = {"type": "json_schema", "json_schema": {"name": "x", "schema": SCHEMA}}


async def test_openai_request_and_response(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json("hello")))
    out = await llm.acompletion(
        model="openai/gpt-4o",
        messages=[{"role": "user", "content": "x"}],
        api_key="sk-test",
        temperature=0.2,
        max_tokens=50,
        response_format=FORMAT,
    )
    request = http[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    sent = body(request)
    assert sent["model"] == "gpt-4o" and sent["temperature"] == 0.2
    assert sent["max_completion_tokens"] == 50 and "max_tokens" not in sent
    assert sent["response_format"] == FORMAT
    assert out.choices[0].message.content == "hello"
    assert (out.usage.total_tokens, out.choices[0].finish_reason) == (5, "stop")


async def test_bare_model_id_is_openai(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    await llm.acompletion(model="gpt-4o", messages=[], api_key="k")
    assert body(http[0])["model"] == "gpt-4o"
    assert http[0].url.host == "api.openai.com"


async def test_key_comes_from_the_provider_environment(
    http: Http, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk-env")
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    await llm.acompletion(model="groq/llama3", messages=[], max_tokens=7)
    assert http[0].headers["authorization"] == "Bearer gsk-env"
    assert http[0].url.host == "api.groq.com"
    assert body(http[0])["max_tokens"] == 7


async def test_ollama_uses_the_v1_endpoint_of_its_base(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    await llm.acompletion(model="ollama/llama3", messages=[], api_base="http://box:11434/")
    await llm.acompletion(model="ollama_chat/llama3", messages=[], api_base="http://box:11434/v1")
    assert [str(r.url) for r in http] == ["http://box:11434/v1/chat/completions"] * 2
    assert "authorization" not in http[0].headers


async def test_gemini_schema_drops_additional_properties(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    await llm.acompletion(
        model="gemini/gemini-2.5-flash", messages=[], api_key="g", response_format=FORMAT
    )
    sent = body(http[0])["response_format"]["json_schema"]["schema"]
    assert "additionalProperties" not in sent and sent["required"] == ["a"]
    assert FORMAT["json_schema"]["schema"]["additionalProperties"] is False  # the caller's copy


async def test_custom_endpoint_for_an_unknown_provider(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    await llm.acompletion(model="acme/m-1", messages=[], api_base="https://gw.example/v1")
    assert str(http[0].url) == "https://gw.example/v1/chat/completions"
    assert body(http[0])["model"] == "m-1"


async def test_tool_calls_and_logprobs_are_parsed(http: Http) -> None:
    payload = chat_json(None)
    payload["choices"][0]["message"] = {
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"x": 1}'}}
        ],
    }
    payload["choices"][0]["logprobs"] = {"content": [{"token": "ok", "logprob": -0.1}]}
    serve(http, lambda r: httpx.Response(200, json=payload))
    out = await llm.acompletion(
        model="openai/gpt-4o",
        messages=[],
        api_key="k",
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        tool_choice="auto",
        logprobs=True,
    )
    call = out.choices[0].message.tool_calls[0]  # type: ignore[index]
    assert (call.id, call.function.name, call.function.arguments) == ("c1", "f", '{"x": 1}')
    assert out.choices[0].logprobs.content[0].logprob == -0.1  # type: ignore[union-attr]
    sent = body(http[0])
    assert sent["tool_choice"] == "auto" and sent["logprobs"] is True


async def test_gemini_tool_call_extra_content_is_preserved(http: Http) -> None:
    payload = chat_json(None)
    payload["choices"][0]["message"] = {
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"id":"o-1"}'},
                "extra_content": {"google": {"thought_signature": "sig-123"}},
            }
        ],
    }
    serve(http, lambda r: httpx.Response(200, json=payload))

    out = await llm.acompletion(model="gemini/gemini-3.1-flash-lite", messages=[], api_key="k")

    call = out.choices[0].message.tool_calls[0]  # type: ignore[index]
    assert call.extra_content == {"google": {"thought_signature": "sig-123"}}


async def test_gemini_tool_call_extra_content_is_sent_on_followup(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json("ok")))
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
                "extra_content": {"google": {"thought_signature": "sig-123"}},
            }
        ],
    }

    await llm.acompletion(
        model="gemini/gemini-3.1-flash-lite",
        messages=[{"role": "user", "content": "lookup"}, assistant],
        api_key="k",
    )

    sent_call = body(http[0])["messages"][1]["tool_calls"][0]
    assert sent_call["extra_content"] == {"google": {"thought_signature": "sig-123"}}


async def test_anthropic_structured_output_goes_through_a_forced_tool(
    http: Http,
) -> None:
    serve(
        http,
        lambda r: httpx.Response(
            200,
            json={
                "model": "claude-x",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "json_response", "input": {"a": "z"}}
                ],
                "usage": {"input_tokens": 11, "output_tokens": 4},
            },
        ),
    )
    out = await llm.acompletion(
        model="anthropic/claude-x",
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "go"},
        ],
        api_key="ak",
        response_format=FORMAT,
        temperature=0,
    )
    request = http[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "ak" and "anthropic-version" in request.headers
    sent = body(request)
    assert sent["system"] == "be terse" and sent["max_tokens"] == 4096
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
    assert sent["tool_choice"] == {"type": "tool", "name": "json_response"}
    assert sent["tools"][0]["input_schema"] == SCHEMA
    assert json.loads(out.choices[0].message.content or "") == {"a": "z"}
    assert (out.choices[0].finish_reason, out.usage.total_tokens) == ("stop", 15)


async def test_anthropic_tool_loop_roundtrip(http: Http) -> None:
    serve(
        http,
        lambda r: httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "looking"},
                    {"type": "tool_use", "id": "t9", "name": "lookup", "input": {"q": "x"}},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "find"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "a", "arguments": '{"n": 1}'},
                },
                {"id": "t2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup", "description": "d", "parameters": {"type": "object"}},
        }
    ]
    out = await llm.acompletion(
        model="anthropic/claude-x", messages=messages, api_key="k", tools=tools, tool_choice="auto"
    )
    sent = body(http[0])
    assert sent["tools"] == [
        {"name": "lookup", "description": "d", "input_schema": {"type": "object"}}
    ]
    assert sent["tool_choice"] == {"type": "auto"}
    assistant, results = sent["messages"][1], sent["messages"][2]
    assert [b["type"] for b in assistant["content"]] == ["tool_use", "tool_use"]
    assert assistant["content"][0]["input"] == {"n": 1}
    assert results["role"] == "user"
    assert [(b["tool_use_id"], b["content"]) for b in results["content"]] == [
        ("t1", "r1"),
        ("t2", "r2"),
    ]
    message = out.choices[0].message
    assert message.content == "looking" and message.tool_calls[0].function.name == "lookup"  # type: ignore[index]
    assert json.loads(message.tool_calls[0].function.arguments) == {"q": "x"}  # type: ignore[index]
    assert out.choices[0].finish_reason == "tool_calls"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, llm.BadRequestError),
        (401, llm.AuthenticationError),
        (403, llm.AuthenticationError),
        (429, llm.RateLimitError),
        (503, llm.ServiceUnavailableError),
        (500, llm.InternalServerError),
    ],
)
async def test_http_errors_are_classified(
    http: Http, status: int, error: type[llm.LLMError]
) -> None:
    serve(http, lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(error, match="nope") as caught:
        await llm.acompletion(model="openai/gpt-4o", messages=[], api_key="k")
    assert caught.value.status_code == status


async def test_timeout_and_connection_failures(http: Http) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    serve(http, slow)
    with pytest.raises(llm.Timeout):
        await llm.acompletion(model="openai/gpt-4o", messages=[], api_key="k", timeout=1)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    serve(http, down)
    with pytest.raises(llm.APIConnectionError, match="refused"):
        await llm.acompletion(model="ollama/llama3", messages=[])


async def test_unsupported_providers_say_what_to_do() -> None:
    with pytest.raises(llm.UnsupportedProviderError, match="gateway"):
        await llm.acompletion(model="sagemaker/my-endpoint", messages=[])
    with pytest.raises(llm.UnsupportedProviderError, match="api_base"):
        await llm.acompletion(model="acme/m", messages=[])


async def test_embeddings(http: Http) -> None:
    serve(
        http,
        lambda r: httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
                "usage": {"total_tokens": 4},
            },
        ),
    )
    out = await llm.aembedding(
        model="openai/text-embedding-3-small", input=["a", "b"], dimensions=2, api_key="k"
    )
    assert str(http[0].url) == "https://api.openai.com/v1/embeddings"
    assert body(http[0]) == {
        "model": "text-embedding-3-small",
        "input": ["a", "b"],
        "dimensions": 2,
    }
    assert [d["embedding"] for d in out.data] == [[0.1, 0.2], [0.3]]
    with pytest.raises(llm.UnsupportedProviderError):
        await llm.aembedding(model="anthropic/claude-x", input=["a"], api_key="k")


def test_provider_detection() -> None:
    assert llm.provider_of("openai/gpt-4o") == "openai"
    assert llm.provider_of("gpt-4o") == "openai"
    assert llm.provider_of("ollama_chat/x") == "ollama_chat"
    assert llm.provider_of("acme/x") is None


def test_default_credentials_fill_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from tuvl.core.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-settings")
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama:11434")
    assert agent_models.default_credentials("openai/gpt-4o", None, None) == ("sk-settings", None)
    assert agent_models.default_credentials("openai/gpt-4o", "own", None)[0] == "own"
    assert agent_models.default_credentials("ollama/llama3", None, None)[1] == "http://ollama:11434"


def test_the_beta_provider_name_is_an_alias() -> None:
    agent_models.AGENT_MODEL_REGISTRY.pop("old-style", None)
    agent_models._register_agent_model_from_dict(
        {
            "kind": "AgentModel",
            "metadata": {"name": "old-style"},
            "spec": {"type": "decision", "provider": "litellm", "model": "openai/x"},
        }
    )
    try:
        assert agent_models.AGENT_MODEL_REGISTRY["old-style"]["provider"] == "llm"
    finally:
        agent_models.AGENT_MODEL_REGISTRY.pop("old-style", None)


async def test_ada_002_gets_no_dimensions(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]}))
    await llm.aembedding(
        model="openai/text-embedding-ada-002", input=["a"], dimensions=1536, api_key="k"
    )
    assert "dimensions" not in body(http[0])


async def test_cohere_compatibility_api(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json('{"a": "x"}')))
    await llm.acompletion(
        model="cohere/command-a-03-2025", messages=[], api_key="k", response_format=FORMAT
    )
    assert str(http[0].url) == "https://api.cohere.ai/compatibility/v1/chat/completions"
    assert body(http[0])["response_format"] == {"type": "json_object", "schema": SCHEMA}
    serve(http, lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]}))
    await llm.aembedding(model="cohere/embed-v4.0", input=["a"], dimensions=1536, api_key="k")
    assert str(http[1].url) == "https://api.cohere.ai/compatibility/v1/embeddings"
    assert "dimensions" not in body(http[1])


async def test_voyage_is_embeddings_only_with_output_dimension(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]}))
    await llm.aembedding(model="voyage/voyage-3-large", input=["a"], dimensions=512, api_key="k")
    assert str(http[0].url) == "https://api.voyageai.com/v1/embeddings"
    assert body(http[0]) == {"model": "voyage-3-large", "input": ["a"], "output_dimension": 512}
    with pytest.raises(llm.UnsupportedProviderError, match="only serves embeddings"):
        await llm.acompletion(model="voyage/voyage-3", messages=[], api_key="k")


async def test_azure_v1_endpoint_from_the_environment(
    http: Http, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    await llm.acompletion(model="azure/my-gpt4o-deployment", messages=[], max_tokens=9)
    request = http[0]
    assert str(request.url) == "https://res.openai.azure.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer az-key"
    assert body(request)["model"] == "my-gpt4o-deployment"
    assert body(request)["max_completion_tokens"] == 9
    await llm.acompletion(model="azure/d", messages=[], api_base="https://other/openai/v1/")
    assert str(http[1].url) == "https://other/openai/v1/chat/completions"
    await llm.acompletion(model="azure/d", messages=[], api_base="https://res.openai.azure.com")
    assert str(http[2].url) == "https://res.openai.azure.com/openai/v1/chat/completions"
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    with pytest.raises(llm.ConfigurationError, match="AZURE_OPENAI_ENDPOINT"):
        await llm.acompletion(model="azure/d", messages=[])


async def test_bedrock_runtime_openai_endpoint(http: Http, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key")
    await llm.acompletion(model="bedrock/openai.gpt-oss-120b-1:0", messages=[])
    assert (
        str(http[0].url)
        == "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/chat/completions"
    )
    assert http[0].headers["authorization"] == "Bearer bedrock-key"
    assert body(http[0])["model"] == "openai.gpt-oss-120b-1:0"
    monkeypatch.delenv("AWS_REGION")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(llm.ConfigurationError, match="AWS_REGION"):
        await llm.acompletion(model="bedrock/x", messages=[])


async def test_vertex_openai_endpoint(http: Http, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    monkeypatch.setenv("VERTEX_ACCESS_TOKEN", "ya29.token")
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    await llm.acompletion(model="vertex_ai/google/gemini-2.5-flash", messages=[])
    assert str(http[0].url) == (
        "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/proj-1"
        "/locations/us-central1/endpoints/openapi/chat/completions"
    )
    assert body(http[0])["model"] == "google/gemini-2.5-flash"
    assert http[0].headers["authorization"] == "Bearer ya29.token"
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    await llm.acompletion(model="vertex_ai/google/gemini-2.5-flash", messages=[])
    assert http[1].url.host == "aiplatform.googleapis.com"


def test_model_needs_list_the_endpoint_variables() -> None:
    from tuvl.core.models.requirements import need_for

    assert need_for("m", {"model": "azure/d"}).env == [
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
    ]
    assert need_for("m", {"model": "azure/d", "api_base": "https://x/openai/v1"}).env == [
        "AZURE_OPENAI_API_KEY"
    ]
    assert need_for("m", {"model": "bedrock/x"}).env == ["AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"]
    assert need_for("m", {"model": "vertex_ai/google/g"}).env == [
        "VERTEX_ACCESS_TOKEN",
        "GOOGLE_CLOUD_PROJECT",
    ]
    assert need_for("m", {"model": "cohere/command-a"}).env == ["COHERE_API_KEY"]


async def test_litellm_proxy_needs_an_api_base(http: Http, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    monkeypatch.setenv("LITELLM_API_KEY", "sk-proxy")
    await llm.acompletion(
        model="litellm_proxy/my-alias", messages=[], api_base="http://proxy:4000/v1"
    )
    assert str(http[0].url) == "http://proxy:4000/v1/chat/completions"
    assert body(http[0])["model"] == "my-alias"
    assert http[0].headers["authorization"] == "Bearer sk-proxy"
    with pytest.raises(llm.ConfigurationError, match="api_base"):
        await llm.acompletion(model="litellm_proxy/my-alias", messages=[])


async def test_embedding_entry_endpoint_reaches_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from tuvl.core.datasources import postgres
    from tuvl.core.models import collections_loader, embeddings_loader
    from tuvl.runtime import builtins

    embeddings_loader._register_embedding_from_dict(
        {
            "kind": "EmbeddingRegistry",
            "spec": {
                "embeddings": [
                    {
                        "name": "gw",
                        "model": "openai/embed-alias",
                        "dimensions": 8,
                        "api_base": "${GW_URL}",
                        "api_key": "${GW_KEY}",
                    }
                ]
            },
        }
    )
    monkeypatch.setitem(
        collections_loader.COLLECTION_REGISTRY, "kb", {"name": "kb", "embedding": "gw"}
    )
    monkeypatch.setattr(postgres, "_VECTOR_STORE_READY", True)
    monkeypatch.setenv("GW_URL", "http://gw:4000/v1")
    monkeypatch.setenv("GW_KEY", "sk-gw")
    seen: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return llm.Embeddings(data=[{"embedding": [0.5]}])

    monkeypatch.setattr(llm, "aembedding", fake)
    try:
        assert await builtins._embed("kb", "hello") == [0.5]
    finally:
        embeddings_loader.EMBEDDING_REGISTRY.pop("gw", None)
    assert (seen["api_base"], seen["api_key"], seen["dimensions"]) == (
        "http://gw:4000/v1",
        "sk-gw",
        8,
    )


async def test_calls_share_one_pooled_connection_with_per_call_timeouts() -> None:
    import asyncio

    connections = 0
    reply = json.dumps(chat_json("pooled")).encode()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode().lower()
                length = int(head.split("content-length: ")[1].split("\r\n")[0])
                await reader.readexactly(length)
                writer.write(
                    b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                    + f"content-length: {len(reply)}\r\n\r\n".encode()
                    + reply
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        for _ in range(3):
            out = await llm.acompletion(
                model="ollama/m", messages=[], api_base=f"http://127.0.0.1:{port}", timeout=7
            )
            assert out.choices[0].message.content == "pooled"
        assert connections == 1
    finally:
        await llm.aclose()
        server.close()
        await server.wait_closed()


async def test_the_timeout_is_set_on_each_request(http: Http) -> None:
    serve(http, lambda r: httpx.Response(200, json=chat_json()))
    await llm.acompletion(model="openai/gpt-4o", messages=[], api_key="k", timeout=3)
    await llm.acompletion(model="openai/gpt-4o", messages=[], api_key="k")
    assert http[0].extensions["timeout"]["read"] == 3
    assert http[1].extensions["timeout"]["read"] == 600


def test_each_event_loop_gets_its_own_client() -> None:
    import asyncio
    import gc
    import weakref

    async def current() -> httpx.AsyncClient:
        return llm._client()

    loops = [asyncio.new_event_loop(), asyncio.new_event_loop()]
    try:
        a, b = (loop.run_until_complete(current()) for loop in loops)
        assert a is not b
        assert loops[0].run_until_complete(current()) is a
    finally:
        for loop in loops:
            loop.close()
    dead = [weakref.ref(loop) for loop in loops]  # the pool must not keep a closed loop alive
    del loops, loop
    gc.collect()
    assert all(ref() is None for ref in dead)
