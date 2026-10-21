"""Threading glue between the agent loop and the QGIS GUI.

The agent loop runs in a worker QThread so streaming network calls never
freeze QGIS. Everything that touches QGIS objects (tools, the project context)
is marshalled to the main thread, because PyQGIS objects are not thread-safe.
Approval and question cards block the worker until the user answers.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from ..core.agent import AgentSession
from ..core.cancel import CancelledError, CancelToken


class _Call:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None


class MainThreadExecutor(QObject):
    """Runs callables on the thread that owns this object (the GUI thread)."""

    _invoke = pyqtSignal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._invoke.connect(self._run)
        self.cancel: Optional[CancelToken] = None

    @pyqtSlot(object)
    def _run(self, call: _Call) -> None:
        try:
            call.result = call.fn()
        except BaseException as exc:  # re-raised in the worker thread
            call.error = exc
        finally:
            call.done.set()

    def run(self, fn: Callable[[], Any]) -> Any:
        if QThread.currentThread() is self.thread():
            return fn()
        call = _Call(fn)
        self._invoke.emit(call)
        while not call.done.wait(0.1):
            if self.cancel is not None and self.cancel.cancelled and not call.done.is_set():
                # the main thread will still finish the call; keep waiting so state stays consistent
                continue
        if call.error is not None:
            raise call.error
        return call.result


class UserPrompter(QObject):
    """Blocking approval / question requests from the worker to the GUI."""

    approval_requested = pyqtSignal(object)   # dict: tool, args, risk, reply(callable)
    question_requested = pyqtSignal(object)   # dict: question, options, reply(callable)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.cancel: Optional[CancelToken] = None

    def _wait(self, signal, payload: Dict[str, Any], default: str) -> str:
        answer: List[str] = []
        ready = threading.Event()

        def reply(value: str) -> None:
            answer.append(value)
            ready.set()

        payload["reply"] = reply
        signal.emit(payload)
        while not ready.wait(0.1):
            if self.cancel is not None and self.cancel.cancelled:
                return default
        return answer[0]

    def ask_approval(self, definition, args: Dict[str, Any]) -> str:
        return self._wait(self.approval_requested,
                          {"tool": definition.name, "risk": definition.risk, "description": definition.description,
                           "args": args}, "deny")

    def ask_question(self, question: str, options: List[str]) -> str:
        return self._wait(self.question_requested, {"question": question, "options": options}, "")


class AgentWorker(QThread):
    """Runs one agent turn and forwards agent events as a Qt signal."""

    event = pyqtSignal(str, object)
    finished_run = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, session: AgentSession, text: str, images, cancel: CancelToken,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.session = session
        self.text = text
        self.images = images
        self.cancel = cancel
        session.emit = lambda kind, payload: self.event.emit(kind, payload)

    def run(self) -> None:  # noqa: D401 (QThread API)
        try:
            result = self.session.run_turn(self.text, self.images, self.cancel)
            self.finished_run.emit(result)
        except CancelledError:
            self.failed.emit("Stopped.")
        except Exception as exc:  # surfaced in the panel
            self.failed.emit(f"{type(exc).__name__}: {exc}")
