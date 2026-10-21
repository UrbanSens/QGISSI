"""Claude adapter for the Anthropic Messages API (``POST /v1/messages``).

Talks HTTPS directly because QGIS's embedded Python (3.9 on macOS) cannot
install the current ``anthropic`` SDK, which needs Python >= 3.10.

Request rules implemented here (Claude API reference, 2026-10):

* Opus 5.5 / Sonnet 5.5 / Fable 5.1 always think: send adaptive thinking,
  never ``disabled`` or ``budget_tokens``; depth is set with
  ``output_config.effort`` (Opus 5.5 defaults to ``medium``, so we always set
  it explicitly).
* ``tool_choice`` is left at ``auto`` (forced tool use is a 400 on the newest
  models).
* Thinking blocks are replayed verbatim. For models with preserved thinking we
  opt into ``prefix_mismatch_behavior: drop_block`` so an unexpected history
  edit drops a block instead of failing the request.
* Prompt caching: one breakpoint after tools+system, one on the newest block.
* Streaming with ``eager_input_streaming`` tools; tool inputs are parsed
  strictly and invalid JSON is reported back to the model as an error result.
* ``stop_reason == "refusal"`` is surfaced before content is used; optional
  server-side fallback (``fallbacks: "default"``) for the interactive plugin.
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.cancel import CancelToken
from .base import (EventCallback, Message, ModelRequest, ModelResponse, Provider, ProviderError,
                   StreamEvent, Usage)
from .catalog import model_info
from .http import HttpClient, HttpError

API_VERSION = "2023-06-01"
OFFICIAL_BASE = "https://api.anthropic.com"
BETA_FALLBACK_DEFAULT = "server-side-fallback-2026-07-01"
BETA_THINKING_BINDING = "thinking-binding-controls-2026-08-01"
BETA_OAUTH = "oauth-2025-04-20"

_STOP_MAP = {"stop_sequence": "end_turn", "model_context_window_exceeded": "max_tokens"}
# Models before Opus 4.7 return summarized thinking by default and predate ``display``.
_NO_DISPLAY = {"claude-opus-4-6", "claude-sonnet-4-6"}
# Optional features that can be switched off when an endpoint (proxy, older
# gateway) rejects them: feature -> substrings of the 400 error message.
_DEGRADABLE = (
    ("fallbacks", ("fallback",)),
    ("block_binding", ("block_binding", "prefix_mismatch", "thinking-binding")),
    ("eager_input_streaming", ("eager_input_streaming",)),
    ("display", ("display",)),
    ("output_config", ("output_config", "effort")),
    ("cache_control", ("cache_control",)),
)


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str = "", base_url: str = OFFICIAL_BASE, http: Optional[HttpClient] = None,
                 enable_fallbacks: bool = True, prompt_caching: bool = True, auth_token: str = "") -> None:
        self.api_key = api_key
        self.auth_token = auth_token
        self.base_url = (base_url or OFFICIAL_BASE).rstrip("/")
        self.http = http or HttpClient()
        self.enable_fallbacks = enable_fallbacks
        self.prompt_caching = prompt_caching
        self.disabled: Set[str] = set()

    # ---------------------------------------------------------------- helpers
    @property
    def official(self) -> bool:
        return self.base_url == OFFICIAL_BASE

    def _headers(self, betas: Set[str]) -> Dict[str, str]:
        headers = {"anthropic-version": API_VERSION}
        betas = set(betas)
        if self.api_key:
            headers["x-api-key"] = self.api_key
        elif self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
            betas.add(BETA_OAUTH)
        if betas:
            headers["anthropic-beta"] = ",".join(sorted(betas))
        return headers

    def _thinking(self, model: str, req: ModelRequest, max_tokens: int) -> Optional[Dict[str, Any]]:
        info = model_info(model, "anthropic")
        if info.thinking in ("always", "adaptive"):
            cfg: Dict[str, Any] = {"type": "adaptive"}
            if model not in _NO_DISPLAY and "display" not in self.disabled:
                cfg["display"] = req.thinking_display or "summarized"
            if info.preserved_thinking and "block_binding" not in self.disabled:
                cfg["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
            return cfg
        if info.thinking == "budget" and (req.effort or "") in ("high", "xhigh", "max"):
            budget = max(1024, min(16_000, max_tokens - 2048))
            return {"type": "enabled", "budget_tokens": budget}
        return None

    def build_payload(self, req: ModelRequest) -> Tuple[Dict[str, Any], Set[str]]:
        info = model_info(req.model, "anthropic")
        betas: Set[str] = set()
        max_tokens = min(req.max_tokens or info.default_max_tokens, info.max_output)
        payload: Dict[str, Any] = {"model": req.model, "max_tokens": max_tokens}
        caching = self.prompt_caching and "cache_control" not in self.disabled
        if req.system:
            block: Dict[str, Any] = {"type": "text", "text": req.system}
            if caching:
                block["cache_control"] = {"type": "ephemeral"}
            payload["system"] = [block]
        payload["messages"] = self.convert_messages(req.messages, caching)
        if req.tools:
            tools = []
            for spec in sorted(req.tools, key=lambda s: s.name):  # deterministic prefix for caching
                tool = {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
                if req.stream and "eager_input_streaming" not in self.disabled:
                    tool["eager_input_streaming"] = True
                tools.append(tool)
            payload["tools"] = tools
        thinking = self._thinking(req.model, req, max_tokens)
        if thinking:
            payload["thinking"] = thinking
            if "block_binding" in thinking:
                betas.add(BETA_THINKING_BINDING)
        if info.effort and "output_config" not in self.disabled:
            effort = req.effort or info.default_effort
            if effort:
                payload["output_config"] = {"effort": effort}
        if req.temperature is not None and info.thinking == "none":
            payload["temperature"] = req.temperature
        if (self.enable_fallbacks and req.allow_fallback and info.fallbacks == "default"
                and self.official and "fallbacks" not in self.disabled):
            payload["fallbacks"] = "default"
            betas.add(BETA_FALLBACK_DEFAULT)
        if req.stream:
            payload["stream"] = True
        return payload, betas

    @staticmethod
    def _clean_user_block(block: Dict[str, Any]) -> Dict[str, Any]:
        block = copy.deepcopy(block)
        if block.get("type") == "text" and not block.get("text"):
            block["text"] = "(empty)"
        if block.get("type") == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                content = [c for c in content if not (c.get("type") == "text" and not c.get("text"))]
                block["content"] = content or [{"type": "text", "text": "(no output)"}]
            elif not content:
                block["content"] = "(no output)"
        return block

    @staticmethod
    def _assistant_content(msg: Message) -> List[Dict[str, Any]]:
        if msg.provider == "anthropic" and isinstance(msg.raw, list):
            content = copy.deepcopy(msg.raw)
            # After a mid-output fallback, drop model-internal blocks that
            # precede the last fallback marker (they belong to the declined model).
            marks = [i for i, b in enumerate(content) if b.get("type") == "fallback"]
            if marks:
                cut = marks[-1]
                content = [b for i, b in enumerate(content)
                           if i > cut or b.get("type") in ("text", "fallback")]
            return content
        content = []
        for block in msg.content:
            if block.get("type") == "text" and block.get("text"):
                content.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                content.append({"type": "tool_use", "id": block["id"], "name": block["name"],
                                "input": block.get("input") or {}})
        return content

    def convert_messages(self, messages: List[Message], caching: bool = True) -> List[Dict[str, Any]]:
        out = []
        for msg in messages:
            if msg.role == "assistant":
                content = self._assistant_content(msg)
            else:
                content = [self._clean_user_block(b) for b in msg.content]
            if not content:
                content = [{"type": "text", "text": "(empty)"}]
            out.append({"role": msg.role, "content": content})
        if caching and out:
            last = out[-1]["content"][-1]
            if last.get("type") not in ("thinking", "redacted_thinking", "fallback"):
                last["cache_control"] = {"type": "ephemeral"}
        return out

    def _degrade(self, err: HttpError) -> bool:
        text = err.error_text().lower()
        for feature, needles in _DEGRADABLE:
            if feature not in self.disabled and any(n in text for n in needles):
                self.disabled.add(feature)
                return True
        return False

    # ------------------------------------------------------------------ calls
    def complete(self, request: ModelRequest, on_event: Optional[EventCallback] = None,
                 cancel: Optional[CancelToken] = None) -> ModelResponse:
        if not (self.api_key or self.auth_token):
            raise ProviderError("No Anthropic API key configured (Settings > QGISSI or ANTHROPIC_API_KEY).")
        url = f"{self.base_url}/v1/messages"
        attempts = 0
        while True:
            payload, betas = self.build_payload(request)
            started = time.time()
            try:
                if request.stream:
                    response = self._stream(url, payload, betas, on_event, cancel)
                else:
                    data = self.http.post_json(url, payload, self._headers(betas), cancel)
                    response = self._parse_message(data)
                response.latency_s = time.time() - started
                return response
            except HttpError as err:
                attempts += 1
                if err.status == 400 and attempts <= len(_DEGRADABLE) and self._degrade(err):
                    continue
                if err.retryable and attempts <= 2:  # e.g. mid-stream overloaded_error
                    if cancel is not None and cancel.wait(2.0 * attempts):
                        cancel.raise_if_cancelled()
                    continue
                raise ProviderError(_describe(err), err.status, err.retryable) from err

    def _stream(self, url: str, payload: Dict[str, Any], betas: Set[str],
                on_event: Optional[EventCallback], cancel: Optional[CancelToken]) -> ModelResponse:
        blocks: Dict[int, Dict[str, Any]] = {}
        partial: Dict[int, str] = {}
        invalid: Dict[str, str] = {}
        meta: Dict[str, Any] = {"model": payload["model"], "stop_reason": None, "stop_details": None, "id": None}
        usage: Dict[str, Any] = {}

        def emit(kind: str, text: str = "", index: int = 0, name: str = "") -> None:
            if on_event is not None:
                on_event(StreamEvent(kind=kind, text=text, index=index, name=name))

        for _event, data in self.http.stream_sse(url, payload, self._headers(betas), cancel):
            if not isinstance(data, dict):
                continue
            etype = data.get("type")
            if etype == "message_start":
                message = data.get("message") or {}
                meta["id"] = message.get("id")
                meta["model"] = message.get("model") or meta["model"]
                usage.update(message.get("usage") or {})
            elif etype == "content_block_start":
                index = data.get("index", len(blocks))
                block = copy.deepcopy(data.get("content_block") or {})
                if block.get("type") in ("tool_use", "server_tool_use"):
                    partial[index] = ""
                    if block.get("type") == "tool_use":
                        emit("tool_start", index=index, name=block.get("name", ""))
                blocks[index] = block
            elif etype == "content_block_delta":
                index = data.get("index", 0)
                delta = data.get("delta") or {}
                block = blocks.setdefault(index, {"type": "text", "text": ""})
                dtype = delta.get("type")
                if dtype == "text_delta":
                    block["text"] = block.get("text", "") + delta.get("text", "")
                    emit("text", delta.get("text", ""), index)
                elif dtype == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                    emit("thinking", delta.get("thinking", ""), index)
                elif dtype == "signature_delta":
                    block["signature"] = block.get("signature", "") + delta.get("signature", "")
                elif dtype == "input_json_delta":
                    partial[index] = partial.get(index, "") + delta.get("partial_json", "")
                    emit("tool_input", delta.get("partial_json", ""), index, block.get("name", ""))
                elif dtype == "citations_delta":
                    block.setdefault("citations", []).append(delta.get("citation"))
            elif etype == "content_block_stop":
                index = data.get("index", 0)
                if index in partial:
                    self._finalize_input(blocks[index], partial.pop(index), invalid)
            elif etype == "message_delta":
                delta = data.get("delta") or {}
                meta["stop_reason"] = delta.get("stop_reason") or meta["stop_reason"]
                meta["stop_details"] = delta.get("stop_details") or data.get("stop_details") or meta["stop_details"]
                usage.update(data.get("usage") or {})
            elif etype == "error":
                error = data.get("error") or {}
                status = 529 if error.get("type") == "overloaded_error" else 500
                raise HttpError(status, error.get("message", "stream error"), data)
            elif etype == "message_stop":
                break
        for index, raw in list(partial.items()):  # stream ended mid tool input
            self._finalize_input(blocks[index], raw, invalid)
        content = [blocks[i] for i in sorted(blocks)]
        return self._build_response(content, meta, usage, invalid)

    @staticmethod
    def _finalize_input(block: Dict[str, Any], raw: str, invalid: Dict[str, str]) -> None:
        if not raw.strip():
            block["input"] = block.get("input") or {}
            return
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if isinstance(value, dict):
            block["input"] = value
        else:
            block["input"] = {}
            if block.get("type") == "tool_use":
                invalid[block.get("id", "")] = raw

    def _parse_message(self, data: Dict[str, Any]) -> ModelResponse:
        meta = {"model": data.get("model"), "stop_reason": data.get("stop_reason"),
                "stop_details": data.get("stop_details"), "id": data.get("id")}
        return self._build_response(copy.deepcopy(data.get("content") or []), meta, data.get("usage") or {}, {})

    @staticmethod
    def _build_response(content: List[Dict[str, Any]], meta: Dict[str, Any], usage: Dict[str, Any],
                        invalid: Dict[str, str]) -> ModelResponse:
        stop = meta.get("stop_reason") or "end_turn"
        stop = _STOP_MAP.get(stop, stop)
        details = meta.get("stop_details") or {}
        iterations = usage.get("iterations") or []
        fallback_used = any(isinstance(it, dict) and it.get("type") == "fallback_message" for it in iterations)
        if iterations:
            totals = Usage()
            for it in iterations:
                if isinstance(it, dict):
                    totals.add(_usage_from(it))
        else:
            totals = _usage_from(usage)
        message = Message(role="assistant", content=content, provider="anthropic",
                          model=meta.get("model"), raw=copy.deepcopy(content))
        return ModelResponse(message=message, stop_reason=stop, usage=totals, model=meta.get("model") or "",
                             refusal_category=details.get("category") if stop == "refusal" else None,
                             fallback_used=fallback_used, request_id=meta.get("id"),
                             invalid_tool_inputs=invalid)

    def list_models(self, cancel: Optional[CancelToken] = None) -> List[str]:
        data = self.http.get_json(f"{self.base_url}/v1/models?limit=100", self._headers(set()), cancel)
        return [m.get("id") for m in data.get("data", []) if m.get("id")]


def _usage_from(data: Dict[str, Any]) -> Usage:
    def num(key: str) -> int:
        value = data.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    return Usage(input_tokens=num("input_tokens"), output_tokens=num("output_tokens"),
                 cache_read_tokens=num("cache_read_input_tokens"),
                 cache_write_tokens=num("cache_creation_input_tokens"))


def _describe(err: HttpError) -> str:
    detail = err.error_text()
    if err.status == 401:
        return f"Anthropic rejected the API key (401): {detail}"
    if err.status == 403:
        return f"Anthropic denied access (403): {detail}"
    if err.status == 404:
        return f"Model or endpoint not found (404): {detail}"
    if err.status == 429:
        return f"Rate limited by Anthropic (429): {detail}"
    if err.status == 0:
        return f"Network error contacting Anthropic: {err}"
    return f"Anthropic API error {err.status}: {detail}"
