"""OpenAI chat-completions and embeddings (OpenAI, Gemini, Ollama, Groq, vLLM, …)."""

from __future__ import annotations

import copy
import json
from typing import Any

from tuvl.llm.errors import LLMError, from_status
from tuvl.llm.routes import Route
from tuvl.llm.types import (
    Choice,
    Completion,
    Embeddings,
    Function,
    Logprobs,
    Message,
    TokenLogprob,
    ToolCall,
    Usage,
)


def headers(route: Route) -> dict[str, str]:
    return {"Authorization": f"Bearer {route.api_key}"} if route.api_key else {}


def _drop_additional_properties(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: _drop_additional_properties(v)
            for k, v in node.items()
            if k != "additionalProperties"
        }
    if isinstance(node, list):
        return [_drop_additional_properties(v) for v in node]
    return node


def chat_request(route: Route, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {"model": route.model, "messages": params["messages"]}
    fmt = params.get("response_format")
    if fmt is not None:
        if route.provider.json_object_schema:
            fmt = {"type": "json_object", "schema": fmt["json_schema"]["schema"]}
        elif not route.provider.strict_schema:
            fmt = copy.deepcopy(fmt)
            fmt["json_schema"]["schema"] = _drop_additional_properties(fmt["json_schema"]["schema"])
        body["response_format"] = fmt
    if params.get("tools"):
        body["tools"] = params["tools"]
        body["tool_choice"] = params.get("tool_choice") or "auto"
    if params.get("temperature") is not None:
        body["temperature"] = params["temperature"]
    if params.get("max_tokens") is not None:
        key = "max_completion_tokens" if route.provider.completion_tokens_param else "max_tokens"
        body[key] = params["max_tokens"]
    if params.get("logprobs"):
        body["logprobs"] = True
    if params.get("stream"):
        body["stream"] = True
        if params.get("stream_usage", True):
            body["stream_options"] = {"include_usage": True}
    return route.base_url + "/chat/completions", body


def parse_chat(data: dict[str, Any], model: str) -> Completion:
    choices = []
    for raw in data.get("choices") or []:
        msg = raw.get("message") or {}
        calls = [
            ToolCall(
                id=str(c.get("id") or ""),
                function=Function(
                    name=str((c.get("function") or {}).get("name") or ""),
                    arguments=str((c.get("function") or {}).get("arguments") or ""),
                ),
                extra_content=c.get("extra_content")
                if isinstance(c.get("extra_content"), dict)
                else None,
            )
            for c in msg.get("tool_calls") or []
        ]
        lp = raw.get("logprobs")
        logprobs = None
        if isinstance(lp, dict) and lp.get("content"):
            logprobs = Logprobs(
                [
                    TokenLogprob(str(t.get("token", "")), float(t["logprob"]))
                    for t in lp["content"]
                    if t.get("logprob") is not None
                ]
            )
        choices.append(
            Choice(
                Message(content=msg.get("content"), tool_calls=calls or None),
                finish_reason=raw.get("finish_reason"),
                logprobs=logprobs,
            )
        )
    usage = data.get("usage") or {}
    prompt, completion = (
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )
    return Completion(
        choices,
        Usage(prompt, completion, int(usage.get("total_tokens") or prompt + completion)),
        str(data.get("model") or model),
    )


class StreamBuilder:
    """Folds ``chat.completion.chunk`` events into the response ``parse_chat`` reads."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.text: list[str] = []
        self.calls: dict[Any, dict[str, Any]] = {}
        self.finish: str | None = None
        self.usage: dict[str, Any] | None = None
        self.logprobs: list[Any] = []
        self.finished = False

    def feed(self, event: str, data: str) -> str:
        """One SSE event; returns the text it carries (possibly empty)."""
        if data == "[DONE]":
            self.finished = True
            return ""
        try:
            chunk = json.loads(data)
        except ValueError:
            return ""
        if not isinstance(chunk, dict):
            return ""
        error = chunk.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            raise from_status(code if isinstance(code, int) else 500, str(message or error))
        if chunk.get("model"):
            self.model = str(chunk["model"])
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        choice = choices[0]
        delta = choice.get("delta") or {}
        piece = str(delta.get("content") or "")
        if piece:
            self.text.append(piece)
        for call in delta.get("tool_calls") or []:
            key = call.get("index", call.get("id", len(self.calls)))
            entry = self.calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
            fn = call.get("function") or {}
            entry["id"] = str(call.get("id") or entry["id"])
            entry["name"] = str(fn.get("name") or entry["name"])
            entry["arguments"] += str(fn.get("arguments") or "")
            if isinstance(call.get("extra_content"), dict):
                entry["extra_content"] = call["extra_content"]
        logprobs = choice.get("logprobs")
        if isinstance(logprobs, dict) and logprobs.get("content"):
            self.logprobs.extend(logprobs["content"])
        if choice.get("finish_reason"):
            self.finish = str(choice["finish_reason"])
            self.finished = True
        return piece

    def completion(self) -> Completion:
        if not self.finished:
            raise LLMError("the stream ended before the model finished")
        message: dict[str, Any] = {"content": "".join(self.text) or None}
        if self.calls:
            message["tool_calls"] = [
                {
                    "id": c["id"],
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                    **(
                        {"extra_content": c["extra_content"]}
                        if c.get("extra_content") is not None
                        else {}
                    ),
                }
                for c in self.calls.values()
            ]
        choice: dict[str, Any] = {"message": message, "finish_reason": self.finish}
        if self.logprobs:
            choice["logprobs"] = {"content": self.logprobs}
        return parse_chat(
            {"choices": [choice], "usage": self.usage, "model": self.model}, self.model
        )


def embed_request(route: Route, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {"model": route.model, "input": params["input"]}
    field = route.provider.dimensions_param
    if params.get("dimensions") and field and route.model != "text-embedding-ada-002":
        body[field] = params["dimensions"]
    return route.base_url + "/embeddings", body


def parse_embeddings(data: dict[str, Any]) -> Embeddings:
    items = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
    usage = data.get("usage") or {}
    total = int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
    return Embeddings([{"embedding": d["embedding"]} for d in items], Usage(total, 0, total))
