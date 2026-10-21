"""Provider adapters: request shapes and response parsing (no network)."""
import json
import os
import subprocess
import sys
import time

import pytest

from qgissi.providers import parse_model_spec
from qgissi.providers.anthropic import BETA_FALLBACK_DEFAULT, BETA_THINKING_BINDING, AnthropicProvider
from qgissi.providers.base import Message, ModelRequest, ProviderError, ToolSpec, text_block, tool_result_block
from qgissi.providers.gemini import GeminiProvider, sanitize_schema
from qgissi.providers.ollama import OllamaProvider
from qgissi.providers.openai_chat import OpenAIChatProvider

from .fakes import FakeHttp, http_400

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOLS = [ToolSpec("zeta", "z", {"type": "object", "properties": {}, "required": []}),
         ToolSpec("alpha", "a", {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]})]


def request(model="claude-opus-5-5", **kw):
    return ModelRequest(model=model, system="SYS", messages=[Message("user", [text_block("hi")])], tools=TOOLS, **kw)


@pytest.mark.parametrize("spec,expected", [
    ("anthropic/claude-opus-5-5@high", ("anthropic", "claude-opus-5-5", "high")),
    ("claude-sonnet-5-5", ("anthropic", "claude-sonnet-5-5", None)),
    ("ollama/mistral:latest", ("ollama", "mistral:latest", None)),
    ("openai_compatible/anthropic/claude-x@low", ("openai_compatible", "anthropic/claude-x", "low")),
])
def test_parse_model_spec(spec, expected):
    assert parse_model_spec(spec) == expected


def test_parse_model_spec_rejects_unknown_provider():
    with pytest.raises(ValueError):
        parse_model_spec("acme/model")


# ------------------------------------------------------------------ Anthropic
def test_opus_55_payload_follows_api_rules():
    payload, betas = AnthropicProvider("k").build_payload(request(effort="high"))
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized",
                                   "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert payload["output_config"] == {"effort": "high"}
    assert "budget_tokens" not in json.dumps(payload) and "tool_choice" not in payload and "temperature" not in payload
    assert payload["fallbacks"] == "default" and betas == {BETA_FALLBACK_DEFAULT, BETA_THINKING_BINDING}
    assert payload["max_tokens"] == 64000 and payload["stream"] is True
    assert [t["name"] for t in payload["tools"]] == ["alpha", "zeta"]          # deterministic for caching
    assert all(t["eager_input_streaming"] for t in payload["tools"])
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_opus_55_default_effort_is_set_explicitly():
    payload, _ = AnthropicProvider("k").build_payload(request())
    assert payload["output_config"] == {"effort": "medium"}


def test_haiku_45_uses_budget_thinking_only_at_high_effort():
    low, _ = AnthropicProvider("k").build_payload(request("claude-haiku-4-5", effort="low"))
    high, _ = AnthropicProvider("k").build_payload(request("claude-haiku-4-5", effort="high"))
    assert "thinking" not in low and "output_config" not in low
    assert high["thinking"]["type"] == "enabled" and high["thinking"]["budget_tokens"] < high["max_tokens"]


def test_custom_base_url_and_benchmark_mode_disable_fallbacks():
    proxy, betas = AnthropicProvider("k", base_url="https://gateway.example/v1").build_payload(request())
    assert "fallbacks" not in proxy and BETA_FALLBACK_DEFAULT not in betas
    bench, _ = AnthropicProvider("k").build_payload(request(allow_fallback=False))
    assert "fallbacks" not in bench


def test_assistant_raw_content_is_replayed_verbatim():
    raw = [{"type": "thinking", "thinking": "", "signature": "SIG"},
           {"type": "tool_use", "id": "t1", "name": "alpha", "input": {"x": "1"}}]
    history = [Message("user", [text_block("hi")]), Message("assistant", raw, provider="anthropic", raw=raw),
               Message("user", [tool_result_block("t1", [text_block("ok")])])]
    converted = AnthropicProvider("k").convert_messages(history)
    assert converted[1]["content"] == raw
    assert raw[0] == {"type": "thinking", "thinking": "", "signature": "SIG"}  # not mutated by caching


SSE = [
    {"type": "message_start", "message": {"id": "msg_1", "model": "claude-opus-5-5",
                                          "usage": {"input_tokens": 12, "cache_read_input_tokens": 900,
                                                    "cache_creation_input_tokens": 0, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Plan: "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "abc"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Counting."}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1",
                                                                  "name": "alpha", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"x\": "}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "\"7\"}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 55}},
    {"type": "message_stop"},
]


def test_stream_parsing_assembles_blocks_and_usage():
    events = []
    provider = AnthropicProvider("k", http=FakeHttp(SSE))
    response = provider.complete(request(), on_event=events.append)
    blocks = response.message.content
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[0]["signature"] == "abc" and blocks[2]["input"] == {"x": "7"}
    assert response.stop_reason == "tool_use" and response.usage.cache_read_tokens == 900
    assert response.usage.output_tokens == 55 and response.request_id == "msg_1"
    assert {e.kind for e in events} >= {"thinking", "text", "tool_start", "tool_input"}


def test_invalid_tool_json_is_reported_not_executed():
    broken = [e for e in SSE if e.get("delta", {}).get("partial_json") != "\"7\"}"]
    response = AnthropicProvider("k", http=FakeHttp(broken)).complete(request())
    assert response.invalid_tool_inputs == {"toolu_1": "{\"x\": "}
    assert response.message.tool_uses()[0]["input"] == {}


def test_refusal_is_surfaced_with_category():
    events = [SSE[0], {"type": "message_delta", "delta": {"stop_reason": "refusal",
                                                          "stop_details": {"type": "refusal", "category": "cyber"}},
                       "usage": {"output_tokens": 0}}, {"type": "message_stop"}]
    response = AnthropicProvider("k", http=FakeHttp(events)).complete(request())
    assert response.stop_reason == "refusal" and response.refusal_category == "cyber"


def test_rejected_optional_feature_is_dropped_and_retried():
    http = FakeHttp(SSE, errors=[http_400("fallbacks: Extra inputs are not permitted")])
    provider = AnthropicProvider("k", http=http)
    provider.complete(request())
    assert "fallbacks" in http.requests[0]["payload"] and "fallbacks" not in http.requests[1]["payload"]
    assert "fallbacks" in provider.disabled


def test_missing_key_raises_clear_error():
    with pytest.raises(ProviderError, match="API key"):
        AnthropicProvider("").complete(request())


def test_fallback_usage_iterations_are_summed():
    events = [SSE[0], {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                       "usage": {"output_tokens": 9, "iterations": [
                           {"type": "message", "input_tokens": 5, "output_tokens": 0},
                           {"type": "fallback_message", "input_tokens": 5, "output_tokens": 9}]}},
              {"type": "message_stop"}]
    response = AnthropicProvider("k", http=FakeHttp(events)).complete(request())
    assert response.fallback_used and response.usage.input_tokens == 10 and response.usage.output_tokens == 9


# --------------------------------------------------------------------- OpenAI
def test_openai_conversion_moves_tool_images_into_user_turn():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}}
    history = [Message("user", [text_block("hi")]),
               Message("assistant", [text_block("look"), {"type": "tool_use", "id": "c1", "name": "alpha",
                                                           "input": {"x": "1"}}]),
               Message("user", [tool_result_block("c1", [text_block("ok"), image])])]
    out = OpenAIChatProvider("k").convert_messages("SYS", history)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assert out[2]["tool_calls"][0]["function"]["arguments"] == "{\"x\": \"1\"}"
    assert out[4]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_stream_with_split_tool_arguments():
    events = [{"choices": [{"delta": {"content": "Hi"}}]},
              {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9", "function": {"name": "alpha", "arguments": "{\"x\":"}}]}}]},
              {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"2\"}"}}]}, "finish_reason": "tool_calls"}]},
              {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 60}}},
              "[DONE]"]
    http = FakeHttp([e if isinstance(e, dict) else {"type": "done"} for e in events])
    http.stream_sse = lambda url, payload, headers=None, cancel=None: iter(("message", e) for e in events)
    response = OpenAIChatProvider("k", http=http).complete(request("gpt-5"))
    assert response.stop_reason == "tool_use" and response.message.tool_uses()[0]["input"] == {"x": "2"}
    assert response.usage.input_tokens == 40 and response.usage.cache_read_tokens == 60


# --------------------------------------------------------------------- Gemini
def test_gemini_schema_sanitising():
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "a": {"type": ["string", "null"], "default": "x"}, "b": {"const": 3}}}
    assert sanitize_schema(schema) == {"type": "object", "properties": {"a": {"type": "string", "nullable": True},
                                                                         "b": {"enum": [3]}}}


def test_gemini_function_responses_use_tool_names_and_replay_signatures():
    raw = [{"functionCall": {"name": "alpha", "args": {"x": "1"}}, "thoughtSignature": "SIG"}]
    history = [Message("user", [text_block("hi")]),
               Message("assistant", [{"type": "tool_use", "id": "g1", "name": "alpha", "input": {"x": "1"}}],
                       provider="gemini", raw=raw),
               Message("user", [tool_result_block("g1", [text_block("done")])])]
    payload = GeminiProvider("k").convert(ModelRequest("gemini-3-pro", "SYS", history, TOOLS, effort="low"))
    assert payload["contents"][1]["parts"] == raw
    assert payload["contents"][2]["parts"][0]["functionResponse"] == {"name": "alpha", "response": {"result": "done"}}
    assert payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"


# --------------------------------------------------------------------- Ollama
def test_ollama_stream_with_tool_calls():
    chunks = [{"message": {"role": "assistant", "content": "", "thinking": "hmm"}},
              {"message": {"role": "assistant", "content": "",
                           "tool_calls": [{"function": {"name": "alpha", "arguments": {"x": "3"}}}]}},
              {"done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 12}]
    provider = OllamaProvider(http=FakeHttp(chunks))
    provider._capabilities["qwen3:8b"] = ["tools", "thinking"]
    response = provider.complete(request("qwen3:8b"))
    assert response.stop_reason == "tool_use" and response.message.tool_uses()[0]["input"] == {"x": "3"}
    assert response.usage.input_tokens == 50 and response.message.thinking_text() == "hmm"
    payload = provider.http.requests[0]["payload"]
    assert payload["think"] is True and payload["options"]["num_ctx"] == 32768


def test_request_spacing_is_shared_between_processes(tmp_path):
    from qgissi.providers.http import space_requests

    start = time.time()
    space_requests("svc", 0.3, str(tmp_path))
    space_requests("svc", 0.3, str(tmp_path))
    assert time.time() - start >= 0.25
    child = ("import sys, time; from qgissi.providers.http import space_requests; t = time.time(); "
             "space_requests('svc', 2.0, sys.argv[1]); print(time.time() - t)")
    space_requests("svc", 2.0, str(tmp_path))  # a stamp written now: the other process must wait
    proc = subprocess.run([sys.executable, "-c", child, str(tmp_path)], cwd=ROOT, capture_output=True,
                          text=True, check=True)
    assert float(proc.stdout) >= 0.5
