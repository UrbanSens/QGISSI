"""Threading glue between the agent loop and the QGIS GUI.

The agent loop runs in a worker QThread so streaming network calls never
freeze QGIS. Everything that touches QGIS objects (tools, the project context)
is marshalled to the main thread, because PyQGIS objects are not thread-safe.
Approval and question cards block the worker until the user answers.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from qgis.PyQt.QtCore import QEventLoop, QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from ..core.agent import AgentSession
from ..core.cancel import CancelledError, CancelToken


class _Call:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.started = False
        self.abandoned = False
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
        with call.lock:
            if call.abandoned:
                call.done.set()
                return
            call.started = True
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
            if self.cancel is not None and self.cancel.cancelled:
                with call.lock:
                    if not call.started:  # never ran: drop it (QGIS may be closing)
                        call.abandoned = True
                        raise CancelledError(self.cancel.reason)
                # already running on the main thread: wait so the project stays consistent
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
        if QThread.currentThread() is self.thread():
            # Called on the GUI thread: blocking here would freeze the card's own buttons,
            # so run a local event loop until the user answers (like a modal dialog).
            loop = QEventLoop()

            def reply_local(value: str) -> None:
                answer.append(value)
                loop.quit()

            def check_cancel() -> None:
                if self.cancel is not None and self.cancel.cancelled:
                    loop.quit()

            timer = QTimer()
            timer.timeout.connect(check_cancel)
            timer.start(200)
            payload["reply"] = reply_local
            signal.emit(payload)
            if not answer:
                loop.exec()
            timer.stop()
            return answer[0] if answer else default
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
        except BaseException as exc:  # nothing may escape QThread.run: PyQt would end QGIS
            self.failed.emit(f"{type(exc).__name__}: {exc}")
