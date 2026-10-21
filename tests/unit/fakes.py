"""Test doubles: a scripted HTTP client and a scripted provider."""
from qgissi.providers.base import Message, ModelResponse, Provider, Usage, text_block
from qgissi.providers.http import HttpError


class FakeHttp:
    def __init__(self, events=None, json_reply=None, errors=None):
        self.events = events or []
        self.json_reply = json_reply
        self.errors = list(errors or [])
        self.requests = []

    def _maybe_fail(self):
        if self.errors:
            raise self.errors.pop(0)

    def stream_sse(self, url, payload, headers=None, cancel=None):
        self.requests.append({"url": url, "payload": payload, "headers": headers})
        self._maybe_fail()
        for event in self.events:
            yield event.get("type", "message"), event

    def stream_ndjson(self, url, payload, headers=None, cancel=None):
        self.requests.append({"url": url, "payload": payload, "headers": headers})
        self._maybe_fail()
        yield from self.events

    def post_json(self, url, payload, headers=None, cancel=None):
        self.requests.append({"url": url, "payload": payload, "headers": headers})
        self._maybe_fail()
        return self.json_reply

    def get_json(self, url, headers=None, cancel=None):
        self.requests.append({"url": url, "headers": headers})
        return self.json_reply


def http_400(message):
    return HttpError(400, "HTTP 400", {"type": "error", "error": {"type": "invalid_request_error", "message": message}})


class ScriptedProvider(Provider):
    """Returns pre-built assistant turns in order."""
    name = "anthropic"

    def __init__(self, turns, usage=None):
        self.turns = list(turns)
        self.requests = []
        self.usage = usage or Usage(100, 20)

    def complete(self, request, on_event=None, cancel=None):
        self.requests.append(request)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, ModelResponse):
            return turn
        content, stop = turn
        if isinstance(content, str):
            content = [text_block(content)]
        return ModelResponse(Message("assistant", content, provider="anthropic", model="claude-opus-5-5",
                                     raw=content), stop, self.usage, "claude-opus-5-5", 0.01)
