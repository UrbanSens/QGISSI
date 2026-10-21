"""Small HTTP client used by every provider.

Only the Python standard library is used so the plugin works inside any QGIS
build (QGIS on macOS still ships Python 3.9, where current vendor SDKs no
longer install). Features: JSON requests, Server-Sent-Events and NDJSON
streaming, retries with exponential backoff that honour ``retry-after``,
proxy support and cooperative cancellation.
"""
from __future__ import annotations

import json
import os
import random
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, Optional, Tuple

from ..core.cancel import CancelledError, CancelToken

USER_AGENT = "QGISSI/0.1 (+https://github.com/UrbanSens/QGISSI)"
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}

_spacing_lock = threading.Lock()
_spacing_last: Dict[str, float] = {}


def space_requests(service: str, interval: float, stamp_dir: Optional[str] = None) -> None:
    """Block until a request to ``service`` may start, at least ``interval`` s after the previous one.

    The last start is kept in a locked stamp file, so separate QGIS or benchmark processes share the
    limit. Without file locks (Windows) or a usable stamp file, the limit holds within this process.
    """
    with _spacing_lock:
        path = os.path.join(stamp_dir or tempfile.gettempdir(), f"qgissi-{service}.stamp")
        try:
            import fcntl
            with open(path, "a+", encoding="ascii") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)  # released when the file closes
                fh.seek(0)
                try:
                    last = float(fh.read().strip() or 0)
                except ValueError:
                    last = 0.0
                _sleep_until(last + interval, interval)
                fh.seek(0)
                fh.truncate()
                fh.write(repr(time.time()))
            return
        except (ImportError, OSError):
            pass
        _sleep_until(_spacing_last.get(service, 0.0) + interval, interval)
        _spacing_last[service] = time.time()


def _sleep_until(when: float, longest: float) -> None:
    wait = when - time.time()
    if 0 < wait <= longest:  # a stamp from the future (clock change) is ignored
        time.sleep(wait)


class HttpError(Exception):
    """HTTP or transport failure. ``status`` is 0 for connection errors."""

    def __init__(self, status: int, message: str, body: Any = None,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status in RETRYABLE_STATUS

    def error_text(self) -> str:
        """Best-effort human readable error text from a JSON error body."""
        body = self.body
        if isinstance(body, dict):
            err = body.get("error", body)
            if isinstance(err, dict):
                return str(err.get("message") or err.get("type") or err)
            return str(err)
        return str(body or self)


def _ssl_context() -> ssl.SSLContext:
    try:  # QGIS bundles certifi on macOS and Windows
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _retry_after(headers: Any) -> Optional[float]:
    if headers is None:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class HttpClient:
    def __init__(self, timeout: float = 600.0, max_retries: int = 3,
                 proxy: Optional[str] = None, verify_ssl: bool = True) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        context = _ssl_context() if verify_ssl else ssl._create_unverified_context()  # noqa: S323
        handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    # ------------------------------------------------------------------ core
    def _open(self, method: str, url: str, payload: Any, headers: Dict[str, str],
              cancel: Optional[CancelToken]):
        data = None
        hdrs = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        return self._open_raw(method, url, data, hdrs, cancel)

    def _open_raw(self, method: str, url: str, data: Optional[bytes], headers: Dict[str, str],
                  cancel: Optional[CancelToken]):
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        hdrs.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        attempt = 0
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled()
            try:
                return self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    body = raw.decode("utf-8", "replace")[:2000]
                err = HttpError(exc.code, f"HTTP {exc.code} for {url}", body,
                                _retry_after(exc.headers))
            except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError) as exc:
                err = HttpError(0, f"Connection error for {url}: {exc}")
            if not err.retryable or attempt >= self.max_retries:
                raise err
            delay = err.retry_after if err.retry_after is not None else min(30.0, 1.5 * 2 ** attempt)
            delay += random.uniform(0, 0.5)
            attempt += 1
            if cancel is not None and cancel.wait(delay):
                raise CancelledError(cancel.reason)
            if cancel is None:
                time.sleep(delay)

    def request_json(self, method: str, url: str, payload: Any = None,
                     headers: Optional[Dict[str, str]] = None,
                     cancel: Optional[CancelToken] = None) -> Any:
        response = self._open(method, url, payload, headers or {}, cancel)
        with response:
            raw = response.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def post_json(self, url: str, payload: Any, headers: Optional[Dict[str, str]] = None,
                  cancel: Optional[CancelToken] = None) -> Any:
        return self.request_json("POST", url, payload, headers, cancel)

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None,
                 cancel: Optional[CancelToken] = None) -> Any:
        return self.request_json("GET", url, None, headers, cancel)

    def post_form(self, url: str, fields: Dict[str, str], headers: Optional[Dict[str, str]] = None,
                  cancel: Optional[CancelToken] = None) -> Any:
        """POST application/x-www-form-urlencoded data and parse the JSON reply."""
        import urllib.parse

        body = urllib.parse.urlencode(fields).encode("utf-8")
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        hdrs.update(headers or {})
        response = self._open_raw("POST", url, body, hdrs, cancel)
        with response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def get_bytes(self, url: str, headers: Optional[Dict[str, str]] = None,
                  cancel: Optional[CancelToken] = None, limit: int = 200_000_000) -> bytes:
        response = self._open("GET", url, None, headers or {}, cancel)
        with response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise HttpError(0, f"Response from {url} exceeds {limit} bytes")
        return data

    # ------------------------------------------------------------- streaming
    def _iter_lines(self, response, cancel: Optional[CancelToken]) -> Iterator[str]:
        def _close() -> None:
            try:
                response.close()
            except Exception:
                pass

        def _abort() -> None:
            # Called from another thread (Stop button). Closing the response there would block on
            # the reader's buffer lock; shutting the socket down wakes the blocked read at once.
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            try:
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        if cancel is not None:
            cancel.register(_abort)
        try:
            while True:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                try:
                    line = response.readline()
                except (socket.timeout, ConnectionError, ValueError, OSError, AttributeError) as exc:
                    if cancel is not None and cancel.cancelled:
                        raise CancelledError(cancel.reason)
                    raise HttpError(0, f"Stream interrupted: {exc}")
                if not line:
                    if cancel is not None and cancel.cancelled:
                        raise CancelledError(cancel.reason)
                    return
                yield line.decode("utf-8", "replace").rstrip("\r\n")
        finally:
            if cancel is not None:
                cancel.unregister(_abort)
            _close()

    def stream_sse(self, url: str, payload: Any, headers: Optional[Dict[str, str]] = None,
                   cancel: Optional[CancelToken] = None) -> Iterator[Tuple[str, Any]]:
        """Yield ``(event_name, data)`` pairs; ``data`` is parsed JSON when possible."""
        hdrs = {"Accept": "text/event-stream"}
        hdrs.update(headers or {})
        response = self._open("POST", url, payload, hdrs, cancel)
        event, data_lines = "", []
        for line in self._iter_lines(response, cancel):
            if line == "":
                if data_lines:
                    yield event or "message", _parse_data("\n".join(data_lines))
                event, data_lines = "", []
                continue
            if line.startswith(":"):
                continue  # SSE comment / keep-alive
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            yield event or "message", _parse_data("\n".join(data_lines))

    def stream_ndjson(self, url: str, payload: Any, headers: Optional[Dict[str, str]] = None,
                      cancel: Optional[CancelToken] = None) -> Iterator[Any]:
        response = self._open("POST", url, payload, headers or {}, cancel)
        for line in self._iter_lines(response, cancel):
            if line.strip():
                yield json.loads(line)


def _parse_data(text: str) -> Any:
    if text == "[DONE]":
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text
