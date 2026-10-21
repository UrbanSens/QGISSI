"""Google Gemini adapter (Generative Language REST API, ``generateContent``)."""
from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional

from ..core.cancel import CancelToken
from .base import (EventCallback, Message, ModelRequest, ModelResponse, Provider, ProviderError,
                   StreamEvent, Usage, new_tool_id)
from .http import HttpClient, HttpError

GEMINI_BASE = "https://generativelanguage.googleapis.com"
_REFUSAL = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}
_UNSUPPORTED_SCHEMA_KEYS = {"additionalProperties", "default", "examples", "$schema", "title", "$id", "$defs"}
_BUDGET = {"low": 1024, "medium": 8192, "high": 24576, "xhigh": 32768, "max": -1}


def sanitize_schema(schema: Any) -> Any:
    """Reduce JSON Schema to the OpenAPI subset Gemini function declarations accept."""
    if isinstance(schema, list):
        return [sanitize_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "const":
            out["enum"] = [value]
            continue
        if key == "type" and isinstance(value, list):  # ["string", "null"] -> nullable string
            types = [t for t in value if t != "null"]
            out["type"] = types[0] if types else "string"
            if "null" in value:
                out["nullable"] = True
            continue
        out[key] = sanitize_schema(value)
    if out.get("type") == "object" and not out.get("properties"):
        out.pop("required", None)
    return out


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, api_key: str = "", base_url: str = GEMINI_BASE, http: Optional[HttpClient] = None) -> None:
        self.api_key = api_key
        self.base_url = (base_url or GEMINI_BASE).rstrip("/")
        self.http = http or HttpClient()

    def _headers(self) -> Dict[str, str]:
        return {"x-goog-api-key": self.api_key} if self.api_key else {}

    def convert(self, req: ModelRequest) -> Dict[str, Any]:
        names = self.tool_names_by_id(req.messages)
        contents: List[Dict[str, Any]] = []
        for msg in req.messages:
            if msg.role == "assistant":
                if msg.provider == self.name and isinstance(msg.raw, list):
                    contents.append({"role": "model", "parts": copy.deepcopy(msg.raw)})
                    continue
                parts = []
                if msg.text():
                    parts.append({"text": msg.text()})
                for block in msg.tool_uses():
                    parts.append({"functionCall": {"name": block["name"], "args": block.get("input") or {}}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
                continue
            parts = []
            for block in msg.content:
                btype = block.get("type")
                if btype == "text":
                    parts.append({"text": block.get("text", "")})
                elif btype == "image":
                    src = block.get("source", {})
                    parts.append({"inlineData": {"mimeType": src.get("media_type", "image/png"), "data": src.get("data", "")}})
                elif btype == "tool_result":
                    text, images = self.result_text_and_images(block)
                    key = "error" if block.get("is_error") else "result"
                    parts.append({"functionResponse": {"name": names.get(block["tool_use_id"], "tool"),
                                                       "response": {key: text}}})
                    for media_type, data in images:
                        parts.append({"inlineData": {"mimeType": media_type, "data": data}})
            contents.append({"role": "user", "parts": parts or [{"text": ""}]})
        payload: Dict[str, Any] = {"contents": contents}
        if req.system:
            payload["systemInstruction"] = {"parts": [{"text": req.system}]}
        if req.tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": sanitize_schema(t.input_schema)}
                for t in sorted(req.tools, key=lambda s: s.name)]}]
        config: Dict[str, Any] = {}
        if req.max_tokens:
            config["maxOutputTokens"] = req.max_tokens
        if req.temperature is not None:
            config["temperature"] = req.temperature
        thinking: Dict[str, Any] = {"includeThoughts": True}
        if req.effort:
            if "gemini-3" in req.model:
                thinking["thinkingLevel"] = "low" if req.effort == "low" else "high"
            else:
                thinking["thinkingBudget"] = _BUDGET.get(req.effort, -1)
        config["thinkingConfig"] = thinking
        payload["generationConfig"] = config
        return payload

    def complete(self, request: ModelRequest, on_event: Optional[EventCallback] = None,
                 cancel: Optional[CancelToken] = None) -> ModelResponse:
        if not self.api_key:
            raise ProviderError("No Gemini API key configured.")
        payload = self.convert(request)
        started = time.time()
        model_path = request.model if request.model.startswith("models/") else f"models/{request.model}"
        try:
            if request.stream:
                url = f"{self.base_url}/v1beta/{model_path}:streamGenerateContent?alt=sse"
                chunks = []
                for _event, data in self.http.stream_sse(url, payload, self._headers(), cancel):
                    if isinstance(data, dict):
                        chunks.append(data)
                        if on_event:
                            for part in _parts(data):
                                if part.get("text"):
                                    on_event(StreamEvent("thinking" if part.get("thought") else "text", part["text"]))
                                elif part.get("functionCall"):
                                    on_event(StreamEvent("tool_start", name=part["functionCall"].get("name", "")))
            else:
                url = f"{self.base_url}/v1beta/{model_path}:generateContent"
                chunks = [self.http.post_json(url, payload, self._headers(), cancel)]
        except HttpError as err:
            raise ProviderError(f"Gemini API error {err.status}: {err.error_text()}", err.status, err.retryable) from err
        response = self._merge(chunks, request.model)
        response.latency_s = time.time() - started
        return response

    def _merge(self, chunks: List[Dict[str, Any]], model: str) -> ModelResponse:
        raw_parts: List[Dict[str, Any]] = []
        finish = None
        usage_meta: Dict[str, Any] = {}
        for chunk in chunks:
            for part in _parts(chunk):
                if part.get("text") and raw_parts and _mergeable(raw_parts[-1], part):
                    raw_parts[-1]["text"] += part["text"]
                    if part.get("thoughtSignature"):
                        raw_parts[-1]["thoughtSignature"] = part["thoughtSignature"]
                else:
                    raw_parts.append(copy.deepcopy(part))
            candidate = (chunk.get("candidates") or [{}])[0]
            finish = candidate.get("finishReason") or finish
            usage_meta = chunk.get("usageMetadata") or usage_meta
            if chunk.get("promptFeedback", {}).get("blockReason"):
                finish = "SAFETY"
        content: List[Dict[str, Any]] = []
        for part in raw_parts:
            if part.get("thought") and part.get("text"):
                content.append({"type": "thinking", "thinking": part["text"]})
            elif part.get("text"):
                content.append({"type": "text", "text": part["text"]})
            elif part.get("functionCall"):
                call = part["functionCall"]
                content.append({"type": "tool_use", "id": call.get("id") or new_tool_id("gc"),
                                "name": call.get("name", ""), "input": call.get("args") or {}})
        if any(b["type"] == "tool_use" for b in content):
            stop = "tool_use"
        elif finish == "MAX_TOKENS":
            stop = "max_tokens"
        elif finish in _REFUSAL:
            stop = "refusal"
        else:
            stop = "end_turn"
        usage = Usage(
            input_tokens=max(0, int(usage_meta.get("promptTokenCount") or 0) - int(usage_meta.get("cachedContentTokenCount") or 0)),
            output_tokens=int(usage_meta.get("candidatesTokenCount") or 0) + int(usage_meta.get("thoughtsTokenCount") or 0),
            cache_read_tokens=int(usage_meta.get("cachedContentTokenCount") or 0),
            reasoning_tokens=int(usage_meta.get("thoughtsTokenCount") or 0))
        message = Message(role="assistant", content=content, provider=self.name, model=model, raw=raw_parts)
        return ModelResponse(message=message, stop_reason=stop, usage=usage, model=model,
                             refusal_category=finish if stop == "refusal" else None)

    def list_models(self, cancel: Optional[CancelToken] = None) -> List[str]:
        data = self.http.get_json(f"{self.base_url}/v1beta/models?pageSize=200", self._headers(), cancel)
        return sorted(m["name"].split("/", 1)[-1] for m in data.get("models", [])
                      if "generateContent" in (m.get("supportedGenerationMethods") or []))


def _parts(chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidate = (chunk.get("candidates") or [{}])[0]
    return (candidate.get("content") or {}).get("parts") or []


def _mergeable(previous: Dict[str, Any], part: Dict[str, Any]) -> bool:
    return ("text" in previous and "functionCall" not in previous
            and bool(previous.get("thought")) == bool(part.get("thought")))
