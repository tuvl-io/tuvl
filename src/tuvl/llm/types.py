"""The response shapes callers read (OpenAI chat-completions layout)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Function:
    name: str
    arguments: str


@dataclass
class ToolCall:
    id: str
    function: Function
    type: str = "function"
    extra_content: dict[str, Any] | None = None


@dataclass
class Message:
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    role: str = "assistant"


@dataclass
class TokenLogprob:
    token: str
    logprob: float


@dataclass
class Logprobs:
    content: list[TokenLogprob] = field(default_factory=list)


@dataclass
class Choice:
    message: Message
    finish_reason: str | None = None
    logprobs: Logprobs | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class Completion:
    choices: list[Choice]
    usage: Usage = field(default_factory=Usage)
    model: str = ""


@dataclass
class TextDelta:
    """A piece of the answer as the provider produced it."""

    text: str


@dataclass
class StreamEnd:
    """The last event of a stream: the same ``Completion`` ``acompletion`` would have returned."""

    completion: Completion


StreamEvent = TextDelta | StreamEnd


@dataclass
class Embeddings:
    #: ``[{"embedding": [...]}, ...]`` in input order.
    data: list[dict[str, Any]]
    usage: Usage = field(default_factory=Usage)
