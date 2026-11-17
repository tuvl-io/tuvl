"""Anthropic's Messages API."""

from __future__ import annotations

import json
from typing import Any

from tuvl.llm.errors import (
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    LLMError,
    RateLimitError,
    ServiceUnavailableError,
)
from tuvl.llm.routes import Route
from tuvl.llm.types import Choice, Completion, Function, Message, ToolCall, Usage

VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096
_JSON_TOOL = "json_response"
_STOP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def headers(route: Route) -> dict[str, str]:
    out = {"anthropic-version": VERSION}
    if route.api_key:
        out["x-api-key"] = route.api_key
    return out


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message["role"]
    if role == "tool":
        return [
            {
                "type": "tool_result",
                "tool_use_id": message["tool_call_id"],
                "content": str(message.get("content") or ""),
            }
        ]
    blocks: list[dict[str, Any]] = []
    content = message.get("content")
    if content:
        blocks.append({"type": "text", "text": str(content)})
    for call in message.get("tool_calls") or []:
        fn = call["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        blocks.append({"type": "tool_use", "id": call["id"], "name": fn["name"], "input": args})
    return blocks


def _convert(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] == "system":
            system.append(str(message.get("content") or ""))
            continue
        role = "assistant" if message["role"] == "assistant" else "user"
        blocks = _blocks(message)
        if not blocks:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})
    return "\n\n".join(s for s in system if s), turns


def _tool_choice(choice: Any) -> dict[str, Any]:
    """The OpenAI-style choice in Anthropic's terms: ``required`` is ``any``, a named function is a named tool."""
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, dict) and choice.get("type") == "function":
        name = (choice.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": str(name)}
    return {"type": "auto"}


def chat_request(route: Route, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    system, turns = _convert(params["messages"])
    body: dict[str, Any] = {
        "model": route.model,
        "messages": turns,
        "max_tokens": int(params.get("max_tokens") or _DEFAULT_MAX_TOKENS),
    }
    if system:
        body["system"] = system
    if params.get("temperature") is not None:
        body["temperature"] = params["temperature"]
    tools = [
        {
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}},
        }
        for t in params.get("tools") or []
    ]
    fmt = params.get("response_format")
    if fmt is not None and not tools:
        # Structured output: force a tool whose input is the schema; read it back as JSON.
        tools = [
            {
                "name": _JSON_TOOL,
                "description": "Respond with the result as this object.",
                "input_schema": fmt["json_schema"]["schema"],
            }
        ]
        body["tool_choice"] = {"type": "tool", "name": _JSON_TOOL}
    elif tools:
        body["tool_choice"] = _tool_choice(params.get("tool_choice"))
    if tools:
        body["tools"] = tools
    if params.get("stream"):
        body["stream"] = True
    return route.base_url + "/v1/messages", body


def parse_chat(data: dict[str, Any], model: str) -> Completion:
    text: list[str] = []
    calls: list[ToolCall] = []
    structured: str | None = None
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            args = json.dumps(block.get("input") or {})
            if block.get("name") == _JSON_TOOL:
                structured = args
            else:
                calls.append(ToolCall(str(block["id"]), Function(str(block["name"]), args)))
    content = structured if structured is not None else "".join(text)
    stop = _STOP.get(str(data.get("stop_reason")), data.get("stop_reason"))
    if structured is not None and stop == "tool_calls":
        stop = "stop"
    usage = data.get("usage") or {}
    prompt, completion = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return Completion(
        [Choice(Message(content=content, tool_calls=calls or None), finish_reason=stop)],
        Usage(prompt, completion, prompt + completion),
        str(data.get("model") or model),
    )


_ERRORS: dict[str, type[LLMError]] = {
    "overloaded_error": ServiceUnavailableError,
    "rate_limit_error": RateLimitError,
    "authentication_error": AuthenticationError,
    "permission_error": AuthenticationError,
    "invalid_request_error": BadRequestError,
    "not_found_error": BadRequestError,
    "api_error": InternalServerError,
}


class StreamBuilder:
    """Folds Messages-API stream events into the response ``parse_chat`` reads."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.blocks: dict[int, dict[str, Any]] = {}
        self.stop: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.finished = False

    def feed(self, event: str, data: str) -> str:
        """One SSE event; returns the text it carries (possibly empty)."""
        try:
            body = json.loads(data)
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        kind = str(body.get("type") or event)
        if kind == "error":
            error = body.get("error") or {}
            raise _ERRORS.get(str(error.get("type")), InternalServerError)(
                str(error.get("message") or "the stream failed")
            )
        if kind == "message_start":
            message = body.get("message") or {}
            self.model = str(message.get("model") or self.model)
            self.input_tokens = int((message.get("usage") or {}).get("input_tokens") or 0)
        elif kind == "content_block_start":
            block = dict(body.get("content_block") or {})
            block["_json"] = ""
            self.blocks[int(body.get("index", len(self.blocks)))] = block
        elif kind == "content_block_delta":
            block = self.blocks.setdefault(int(body.get("index", 0)), {"type": "text", "_json": ""})
            delta = body.get("delta") or {}
            if delta.get("type") == "text_delta":
                piece = str(delta.get("text") or "")
                block["text"] = str(block.get("text") or "") + piece
                return piece
            if delta.get("type") == "input_json_delta":
                piece = str(delta.get("partial_json") or "")
                block["_json"] += piece
                return piece if block.get("name") == _JSON_TOOL else ""
        elif kind == "message_delta":
            self.stop = (body.get("delta") or {}).get("stop_reason") or self.stop
            self.output_tokens = int(
                (body.get("usage") or {}).get("output_tokens") or self.output_tokens
            )
        elif kind == "message_stop":
            self.finished = True
        return ""

    def completion(self) -> Completion:
        if not self.finished:
            raise LLMError("the stream ended before the model finished")
        content: list[dict[str, Any]] = []
        for _, block in sorted(self.blocks.items()):
            if block.get("type") == "tool_use":
                try:
                    args = json.loads(block["_json"] or "{}")
                except ValueError:
                    args = {}
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": args,
                    }
                )
            elif block.get("type") == "text":
                content.append({"type": "text", "text": block.get("text") or ""})
        return parse_chat(
            {
                "content": content,
                "stop_reason": self.stop,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
                "model": self.model,
            },
            self.model,
        )
