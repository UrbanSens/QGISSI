"""Provider-neutral request/response types.

The canonical conversation format uses Anthropic-style content blocks
(``text``, ``image``, ``tool_use``, ``tool_result``, ``thinking``). Each
provider adapter converts to and from its own wire format. Assistant messages
also keep the provider-native payload in ``Message.raw`` so they can be
replayed verbatim to the provider that produced them (Claude thinking
signatures, Gemini thought signatures).
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.cancel import CancelToken

Block = Dict[str, Any]

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


# --------------------------------------------------------------------- blocks
def text_block(text: str) -> Block:
    return {"type": "text", "text": text}


def image_block(data_b64: str, media_type: str = "image/png") -> Block:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data_b64}}


def tool_result_block(tool_use_id: str, content: List[Block], is_error: bool = False) -> Block:
    block: Block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def new_tool_id(prefix: str = "call") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# ------------------------------------------------------------------- messages
@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: List[Block]
    provider: Optional[str] = None
    model: Optional[str] = None
    raw: Any = None

    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    def tool_uses(self) -> List[Block]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def thinking_text(self) -> str:
        return "\n".join(b.get("thinking", "") for b in self.content
                         if b.get("type") == "thinking" and b.get("thinking"))

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "content": copy.deepcopy(self.content),
                "provider": self.provider, "model": self.model, "raw": copy.deepcopy(self.raw)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        return cls(role=data["role"], content=data.get("content", []), provider=data.get("provider"),
                   model=data.get("model"), raw=data.get("raw"))


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0          # uncached input tokens
    output_tokens: int = 0         # includes reasoning tokens where the API bills them as output
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0      # informational

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.reasoning_tokens += other.reasoning_tokens

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def to_dict(self) -> Dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens,
                "reasoning_tokens": self.reasoning_tokens}


@dataclass
class ModelRequest:
    model: str
    system: str
    messages: List[Message]
    tools: List[ToolSpec] = field(default_factory=list)
    max_tokens: int = 0              # 0 = model default from the catalog
    effort: Optional[str] = None     # low | medium | high | xhigh | max
    thinking_display: str = "summarized"  # summarized | omitted
    temperature: Optional[float] = None
    stream: bool = True
    allow_fallback: bool = True      # Anthropic server-side refusal fallback


@dataclass
class ModelResponse:
    message: Message
    stop_reason: str                 # end_turn | tool_use | max_tokens | refusal | pause_turn
    usage: Usage
    model: str                       # model that actually served the response
    latency_s: float = 0.0
    refusal_category: Optional[str] = None
    fallback_used: bool = False
    request_id: Optional[str] = None
    invalid_tool_inputs: Dict[str, str] = field(default_factory=dict)  # tool_use_id -> raw text


@dataclass
class StreamEvent:
    kind: str                        # text | thinking | tool_start | tool_input | status
    text: str = ""
    index: int = 0
    name: str = ""


EventCallback = Callable[[StreamEvent], None]


# Failures that say nothing about the request or the model: authentication, billing, an unknown
# model, rate limits, server outages and network or configuration errors (status 0).
OUTAGE_STATUS = {0, 401, 402, 403, 404, 408, 429, 500, 502, 503, 504, 520, 522, 524, 529}


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 0, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable

    @property
    def outage(self) -> bool:
        """The account, service or connection is unusable, rather than this request being bad."""
        return self.status in OUTAGE_STATUS or "credit balance" in str(self).lower()


class Provider:
    """Interface every model backend implements."""

    name = "base"

    def complete(self, request: ModelRequest, on_event: Optional[EventCallback] = None,
                 cancel: Optional[CancelToken] = None) -> ModelResponse:
        raise NotImplementedError

    def list_models(self, cancel: Optional[CancelToken] = None) -> List[str]:
        return []

    # Helpers shared by adapters -------------------------------------------
    @staticmethod
    def tool_names_by_id(messages: List[Message]) -> Dict[str, str]:
        names: Dict[str, str] = {}
        for msg in messages:
            for block in msg.content:
                if block.get("type") == "tool_use":
                    names[block["id"]] = block["name"]
        return names

    @staticmethod
    def result_text_and_images(block: Block):
        """Split a tool_result block into (text, [(media_type, b64)])."""
        content = block.get("content")
        if isinstance(content, str):
            return content, []
        texts, images = [], []
        for part in content or []:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image":
                src = part.get("source", {})
                images.append((src.get("media_type", "image/png"), src.get("data", "")))
        return "\n".join(texts), images
