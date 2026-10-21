"""Stop must interrupt a stalled stream at once (real local HTTP server)."""
import http.server
import threading
import time

import pytest

from qgissi.core.cancel import CancelledError, CancelToken
from qgissi.providers.anthropic import AnthropicProvider
from qgissi.providers.base import Message, ModelRequest, text_block
from qgissi.providers.http import HttpClient, HttpError

from .fakes import FakeHttp


class _Stalling(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'event: ping\ndata: {"type": "ping"}\n\n')
        self.wfile.flush()
        time.sleep(8)  # the model "thinks" silently

    def log_message(self, *args):
        pass


def test_cancel_interrupts_a_silent_stream_immediately():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stalling)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token = CancelToken()
    outcome = {}

    def read():
        try:
            for _ in HttpClient(timeout=30).stream_sse(f"http://127.0.0.1:{server.server_address[1]}/", {}, {}, token):
                pass
            outcome["result"] = "finished"
        except CancelledError:
            outcome["result"] = "cancelled"
        except Exception as exc:  # pragma: no cover - reported in the assertion
            outcome["result"] = f"{type(exc).__name__}: {exc}"

    reader = threading.Thread(target=read)
    reader.start()
    time.sleep(0.5)
    started = time.time()
    token.cancel()
    cancel_took = time.time() - started
    reader.join(3)
    server.shutdown()
    assert cancel_took < 0.5, cancel_took
    assert not reader.is_alive() and outcome.get("result") == "cancelled", outcome


class _FlakyHttp(FakeHttp):
    """First attempt streams some text, then fails with overloaded_error; the retry succeeds."""

    def __init__(self, good_events):
        super().__init__(good_events)
        self.calls = 0

    def stream_sse(self, url, payload, headers=None, cancel=None):
        self.calls += 1
        if self.calls == 1:
            yield "message", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
            yield "message", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Half"}}
            yield "message", {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
            return
        yield from super().stream_sse(url, payload, headers, cancel)


def test_retry_after_partial_stream_resets_the_ui():
    good = [{"type": "message_start", "message": {"id": "m", "model": "claude-opus-5-5", "usage": {"input_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Whole answer"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
            {"type": "message_stop"}]
    events = []
    provider = AnthropicProvider("k", http=_FlakyHttp(good))
    request = ModelRequest("claude-opus-5-5", "S", [Message("user", [text_block("hi")])])
    response = provider.complete(request, on_event=events.append, cancel=CancelToken())
    kinds = [e.kind for e in events]
    assert "reset" in kinds and kinds.index("reset") > kinds.index("text")
    assert response.message.text() == "Whole answer"


def test_http_error_is_not_swallowed():
    with pytest.raises(HttpError):
        HttpClient(timeout=2, max_retries=0).get_json("http://127.0.0.1:9/")
