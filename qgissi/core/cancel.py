"""Cooperative cancellation shared by the agent loop, HTTP streams and tools."""
from __future__ import annotations

import threading
from typing import Callable, List


class CancelledError(Exception):
    """Raised when the user (or a budget guard) stops a run."""


class CancelToken:
    """Thread-safe cancellation flag with close-callbacks.

    Network code registers a callback that closes the active socket so a
    blocking read returns immediately when the user presses Stop.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[], None]] = []
        self.reason = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled by user") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:  # closing an already-closed socket is fine
                pass

    def register(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def unregister(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError(self.reason or "cancelled")

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; returns True if cancelled meanwhile."""
        return self._event.wait(seconds)
