"""Ollama adapter (``/api/chat``) for fully local, offline models."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core.cancel import CancelToken
from .base import (EventCallback, Message, ModelRequest, ModelResponse, Provider, ProviderError,
                   StreamEvent, Usage, new_tool_id)
from .http import HttpClient, HttpError

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_NUM_PREDICT = 8192


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, base_url: str = OLLAMA_BASE, http: Optional[HttpClient] = None,
                 num_ctx: int = 32768, keep_alive: str = "15m") -> None:
        self.base_url = (base_url or OLLAMA_BASE).rstrip("/")
        self.http = http or HttpClient(timeout=1800)
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self._capabilities: Dict[str, List[str]] = {}

    def capabilities(self, model: str, cancel: Optional[CancelToken] = None) -> List[str]:
        if model not in self._capabilities:
            try:
                data = self.http.post_json(f"{self.base_url}/api/show", {"model": model}, {}, cancel)
                self._capabilities[model] = list(data.get("capabilities") or [])
            except Exception:
                self._capabilities[model] = []
        return self._capabilities[model]

    def convert_messages(self, system: str, messages: List[Message]) -> List[Dict[str, Any]]:
        names = self.tool_names_by_id(messages)
        out: List[Dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for msg in messages:
            if msg.role == "assistant":
                entry: Dict[str, Any] = {"role": "assistant", "content": msg.text()}
                calls = [{"function": {"name": b["name"], "arguments": b.get("input") or {}}} for b in msg.tool_uses()]
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
                continue
            texts: List[str] = []
            images: List[str] = []
            for block in msg.content:
                btype = block.get("type")
                if btype == "tool_result":
                    text, tool_images = self.result_text_and_images(block)
                    if block.get("is_error"):
                        text = f"ERROR: {text}"
                    out.append({"role": "tool", "content": text or "(no output)",
                                "tool_name": names.get(block["tool_use_id"], "")})
                    images.extend(data for _media, data in tool_images)
                elif btype == "text":
                    texts.append(block.get("text", ""))
                elif btype == "image":
                    images.append(block.get("source", {}).get("data", ""))
            if texts or images:
                entry = {"role": "user", "content": "\n\n".join(texts) or "Image(s) returned by the tool call(s) above."}
                if images:
                    entry["images"] = images
                out.append(entry)
        return out

    def complete(self, request: ModelRequest, on_event: Optional[EventCallback] = None,
                 cancel: Optional[CancelToken] = None) -> ModelResponse:
        caps = self.capabilities(request.model, cancel)
        payload: Dict[str, Any] = {
            "model": request.model,
            "messages": self.convert_messages(request.system, request.messages),
            "stream": request.stream,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx},
        }
        if request.tools:
            payload["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                  "parameters": t.input_schema}}
                                for t in sorted(request.tools, key=lambda s: s.name)]
        # Bound every generation: small local models sometimes loop until the context is full.
        payload["options"]["num_predict"] = request.max_tokens or DEFAULT_NUM_PREDICT
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature
        if "thinking" in caps:
            payload["think"] = request.effort != "low"
        url = f"{self.base_url}/api/chat"
        started = time.time()
        text, thinking, calls = [], [], []
        final: Dict[str, Any] = {}
        try:
            stream = (self.http.stream_ndjson(url, payload, {}, cancel) if request.stream
                      else iter([self.http.post_json(url, payload, {}, cancel)]))
            for chunk in stream:
                if chunk.get("error"):
                    raise ProviderError(f"Ollama error: {chunk['error']}")
                message = chunk.get("message") or {}
                if message.get("content"):
                    text.append(message["content"])
                    if on_event:
                        on_event(StreamEvent("text", message["content"]))
                if message.get("thinking"):
                    thinking.append(message["thinking"])
                    if on_event:
                        on_event(StreamEvent("thinking", message["thinking"]))
                for call in message.get("tool_calls") or []:
                    calls.append(call)
                    if on_event:
                        on_event(StreamEvent("tool_start", name=(call.get("function") or {}).get("name", "")))
                if chunk.get("done"):
                    final = chunk
        except HttpError as err:
            hint = " Is Ollama running (ollama serve)?" if err.status == 0 else ""
            raise ProviderError(f"Ollama error {err.status}: {err.error_text()}{hint}", err.status, err.retryable) from err
        content: List[Dict[str, Any]] = []
        if thinking:
            content.append({"type": "thinking", "thinking": "".join(thinking)})
        if text:
            content.append({"type": "text", "text": "".join(text)})
        invalid: Dict[str, str] = {}
        for call in calls:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            call_id = call.get("id") or new_tool_id("ol")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    invalid[call_id] = args
                    args = {}
            content.append({"type": "tool_use", "id": call_id, "name": fn.get("name", ""),
                            "input": args if isinstance(args, dict) else {}})
        if calls:
            stop = "tool_use"
        elif final.get("done_reason") == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        usage = Usage(input_tokens=int(final.get("prompt_eval_count") or 0),
                      output_tokens=int(final.get("eval_count") or 0))
        message = Message(role="assistant", content=content, provider=self.name, model=request.model)
        return ModelResponse(message=message, stop_reason=stop, usage=usage, model=request.model,
                             latency_s=time.time() - started, invalid_tool_inputs=invalid)

    def list_models(self, cancel: Optional[CancelToken] = None) -> List[str]:
        data = self.http.get_json(f"{self.base_url}/api/tags", {}, cancel)
        return sorted(m.get("name") for m in data.get("models", []) if m.get("name"))
