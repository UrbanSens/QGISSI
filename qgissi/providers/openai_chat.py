"""OpenAI Chat Completions adapter.

Also covers every OpenAI-compatible server: OpenRouter, Azure OpenAI (with a
deployment base URL), Mistral, Groq, DeepSeek, LM Studio, vLLM, llama.cpp and
Ollama's ``/v1`` endpoint.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.cancel import CancelToken
from .base import (EventCallback, Message, ModelRequest, ModelResponse, Provider, ProviderError,
                   StreamEvent, Usage, new_tool_id)
from .http import HttpClient, HttpError

OPENAI_BASE = "https://api.openai.com/v1"
_FINISH = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use",
           "length": "max_tokens", "content_filter": "refusal"}
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def _data_url(media_type: str, data: str) -> str:
    return f"data:{media_type};base64,{data}"


class OpenAIChatProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str = "", base_url: str = OPENAI_BASE, http: Optional[HttpClient] = None,
                 name: str = "openai", reasoning: Optional[bool] = None,
                 extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.api_key = api_key
        self.base_url = (base_url or OPENAI_BASE).rstrip("/")
        self.http = http or HttpClient()
        self.name = name
        self.reasoning = reasoning  # None = guess from the model name
        self.extra_headers = extra_headers or {}
        self._no_stream_usage = False
        self._no_effort = False

    @property
    def official(self) -> bool:
        return self.base_url.startswith(OPENAI_BASE)

    def _headers(self) -> Dict[str, str]:
        headers = dict(self.extra_headers)
        if self.api_key:
            if "azure" in self.base_url:
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _is_reasoning(self, model: str) -> bool:
        if self.reasoning is not None:
            return self.reasoning
        lowered = model.lower()
        return lowered.startswith(("o1", "o3", "o4", "gpt-5")) or "reasoning" in lowered

    # ------------------------------------------------------------ conversion
    def convert_messages(self, system: str, messages: List[Message]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for msg in messages:
            if msg.role == "assistant":
                text = msg.text()
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                         for b in msg.tool_uses()]
                entry: Dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                if entry["content"] is None and not calls:
                    entry["content"] = ""
                out.append(entry)
                continue
            parts: List[Dict[str, Any]] = []
            pending_images: List[Dict[str, Any]] = []
            for block in msg.content:
                btype = block.get("type")
                if btype == "tool_result":
                    text, images = self.result_text_and_images(block)
                    if block.get("is_error"):
                        text = f"ERROR: {text}"
                    out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text or "(no output)"})
                    for media_type, data in images:
                        pending_images.append({"type": "image_url", "image_url": {"url": _data_url(media_type, data)}})
                elif btype == "text":
                    parts.append({"type": "text", "text": block.get("text", "")})
                elif btype == "image":
                    src = block.get("source", {})
                    parts.append({"type": "image_url",
                                  "image_url": {"url": _data_url(src.get("media_type", "image/png"), src.get("data", ""))}})
            if pending_images:  # tool messages cannot carry images: attach them in a user turn
                out.append({"role": "user", "content": [{"type": "text", "text": "Image(s) returned by the tool call(s) above:"}]
                            + pending_images})
            if parts:
                if all(p["type"] == "text" for p in parts):
                    out.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
                else:
                    out.append({"role": "user", "content": parts})
        return out

    def build_payload(self, req: ModelRequest) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"model": req.model, "messages": self.convert_messages(req.system, req.messages)}
        if req.tools:
            payload["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                  "parameters": t.input_schema}}
                                for t in sorted(req.tools, key=lambda s: s.name)]
        if req.max_tokens:
            key = "max_completion_tokens" if self.official else "max_tokens"
            payload[key] = req.max_tokens
        if self._is_reasoning(req.model) and req.effort and not self._no_effort:
            payload["reasoning_effort"] = _EFFORT.get(req.effort, "medium")
        elif req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.stream:
            payload["stream"] = True
            if not self._no_stream_usage:
                payload["stream_options"] = {"include_usage": True}
        return payload

    # ------------------------------------------------------------------ calls
    def complete(self, request: ModelRequest, on_event: Optional[EventCallback] = None,
                 cancel: Optional[CancelToken] = None) -> ModelResponse:
        url = f"{self.base_url}/chat/completions"
        attempts = 0
        while True:
            payload = self.build_payload(request)
            started = time.time()
            try:
                if request.stream:
                    response = self._stream(url, payload, on_event, cancel)
                else:
                    response = self._parse(self.http.post_json(url, payload, self._headers(), cancel), request.model)
                response.latency_s = time.time() - started
                return response
            except HttpError as err:
                attempts += 1
                text = err.error_text().lower()
                if err.status in (400, 422) and attempts < 4:
                    if "stream_options" in text and not self._no_stream_usage:
                        self._no_stream_usage = True
                        continue
                    if "reasoning_effort" in text and not self._no_effort:
                        self._no_effort = True
                        continue
                raise ProviderError(f"{self.name} API error {err.status}: {err.error_text()}", err.status,
                                    err.retryable) from err

    def _stream(self, url: str, payload: Dict[str, Any], on_event: Optional[EventCallback],
                cancel: Optional[CancelToken]) -> ModelResponse:
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        calls: Dict[int, Dict[str, Any]] = {}
        finish = None
        usage = Usage()
        model = payload["model"]
        for _event, data in self.http.stream_sse(url, payload, self._headers(), cancel):
            if data == "[DONE]":
                break
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                raise HttpError(500, str(data["error"]), data)
            model = data.get("model") or model
            if data.get("usage"):
                usage = _usage(data["usage"])
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                    if on_event:
                        on_event(StreamEvent("text", delta["content"]))
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    reasoning_parts.append(reasoning)
                    if on_event:
                        on_event(StreamEvent("thinking", reasoning))
                for call in delta.get("tool_calls") or []:
                    slot = calls.setdefault(call.get("index", len(calls)), {"id": None, "name": "", "args": ""})
                    if call.get("id"):
                        slot["id"] = call["id"]
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                        if on_event:
                            on_event(StreamEvent("tool_start", name=slot["name"]))
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        return self._build("".join(text_parts), "".join(reasoning_parts),
                           [calls[i] for i in sorted(calls)], finish, usage, model)

    def _parse(self, data: Dict[str, Any], model: str) -> ModelResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = [{"id": c.get("id"), "name": (c.get("function") or {}).get("name", ""),
                  "args": (c.get("function") or {}).get("arguments", "")}
                 for c in message.get("tool_calls") or []]
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        return self._build(message.get("content") or "", reasoning if isinstance(reasoning, str) else "",
                           calls, choice.get("finish_reason"), _usage(data.get("usage") or {}),
                           data.get("model") or model)

    def _build(self, text: str, reasoning: str, calls: List[Dict[str, Any]], finish: Optional[str],
               usage: Usage, model: str) -> ModelResponse:
        content: List[Dict[str, Any]] = []
        invalid: Dict[str, str] = {}
        if reasoning:
            content.append({"type": "thinking", "thinking": reasoning})
        if text:
            content.append({"type": "text", "text": text})
        for call in calls:
            call_id = call.get("id") or new_tool_id("call")
            raw = call.get("args") or ""
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError:
                args = None
            if not isinstance(args, dict):
                invalid[call_id] = raw
                args = {}
            content.append({"type": "tool_use", "id": call_id, "name": call.get("name", ""), "input": args})
        stop = _FINISH.get(finish or "", "end_turn")
        if calls and stop == "end_turn":
            stop = "tool_use"
        message = Message(role="assistant", content=content, provider=self.name, model=model)
        return ModelResponse(message=message, stop_reason=stop, usage=usage, model=model,
                             refusal_category="content_filter" if stop == "refusal" else None,
                             invalid_tool_inputs=invalid)

    def list_models(self, cancel: Optional[CancelToken] = None) -> List[str]:
        data = self.http.get_json(f"{self.base_url}/models", self._headers(), cancel)
        return sorted(m.get("id") for m in data.get("data", []) if m.get("id"))


def _usage(data: Dict[str, Any]) -> Usage:
    prompt = int(data.get("prompt_tokens") or 0)
    cached = int(((data.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    reasoning = int(((data.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    return Usage(input_tokens=max(0, prompt - cached), output_tokens=int(data.get("completion_tokens") or 0),
                 cache_read_tokens=cached, reasoning_tokens=reasoning)
