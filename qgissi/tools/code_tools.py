"""Run model-written PyQGIS code (requires approval unless autopilot is on).

Defence in depth, none of which is a sandbox:
1. static AST guard (``core.code_guard``),
2. a runtime audit-hook tripwire that is armed only on the executing thread
   while agent code runs: no process spawning (except GDAL/OGR/GRASS/SAGA
   binaries used by Processing), no sockets, no file writes or deletes outside
   the output and temp folders, no blocked imports,
3. a deadline enforced by a line tracer scoped to the agent's code,
4. the run checkpoint (layers backed up before code runs, undo available).
"""
from __future__ import annotations

import builtins
import contextlib
import io
import math
import os
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, Dict, Iterator, List

from ..core.cancel import CancelledError
from ..core.code_guard import BLOCKED_MODULES, check_code
from ._qgis import json_value, resolve_layer
from .registry import Risk, ToolContext, ToolError, ToolResult, tool

CODE_FILENAME = "<qgissi-code>"
DEFAULT_TIMEOUT = 120.0
_ALLOWED_BINARIES = ("gdal", "ogr", "grass", "saga", "otb", "whitebox", "pdal", "nearblack")
_SHELLS = {"sh", "bash", "zsh", "dash", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh"}
_SHELL_OPERATORS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "<&", ">&", "(", ")"}
_PATH_EVENTS = {"os.remove", "os.rmdir", "os.truncate", "os.chmod", "os.chown", "os.utime", "os.mkdir",
                "os.symlink", "shutil.rmtree", "shutil.chown"}
_TWO_PATH_EVENTS = {"os.rename", "os.replace", "os.link", "shutil.move"}
_state = threading.local()
_hook_installed = False


class TripwireError(PermissionError):
    pass


def _armed() -> bool:
    return bool(getattr(_state, "armed", False))


def _allowed_path(path: Any) -> bool:
    try:
        target = os.path.realpath(os.fspath(path))
    except TypeError:
        return True  # file descriptors
    roots = [os.path.realpath(r) for r in getattr(_state, "roots", []) if r]
    return any(target == root or target.startswith(root + os.sep) for root in roots)


def _program(args: Any) -> str:
    """Name of the program a subprocess.Popen call would run ('' if it is a chained shell command)."""
    import shlex
    executable, argv = args[0], args[1]
    seq = [str(a) for a in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
    program = os.path.basename(str(executable or (seq[0] if seq else ""))).lower()
    if program not in _SHELLS:
        return program
    command = seq[seq.index("-c") + 1] if "-c" in seq and seq.index("-c") + 1 < len(seq) else (seq[-1] if seq else "")
    if "`" in command or "$(" in command:
        return ""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return ""
    if not tokens or any(t in _SHELL_OPERATORS for t in tokens):
        return ""
    return os.path.basename(tokens[0]).lower()


def _audit(event: str, args: Any) -> None:
    if not _armed():
        return
    if event == "open":
        path = args[0]
        mode = args[1] if len(args) > 1 else "r"
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writes and path is not None and not _allowed_path(path):
            raise TripwireError(f"writing to {path} is outside the output folder")
    elif event in _PATH_EVENTS:
        target = args[1] if event == "os.symlink" else args[0]  # a symlink is created at its second argument
        if not _allowed_path(target):
            raise TripwireError(f"{event} on {target} is outside the output folder")
    elif event in _TWO_PATH_EVENTS:
        for target in args[:2]:
            if not _allowed_path(target):
                raise TripwireError(f"{event} involving {target} is outside the output folder")
    elif event == "subprocess.Popen":
        program = _program(args)
        if not program or not program.startswith(_ALLOWED_BINARIES):
            raise TripwireError(f"starting the process '{program or 'chained shell command'}' is not allowed")
    elif event.startswith("_thread.start"):  # start_new_thread (<= 3.12), start_joinable_thread (3.13+)
        raise TripwireError("starting threads is not allowed in agent code")
    elif event in ("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "os.kill",
                   "ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function", "socket.connect", "socket.bind",
                   "socket.sendto", "winreg.OpenKey"):
        raise TripwireError(f"{event} is not allowed in agent code")
    elif event == "import":
        name = str(args[0])
        if name in BLOCKED_MODULES or name.split(".")[0] in BLOCKED_MODULES:
            raise TripwireError(f"import of {name} is not allowed")


def _guard_thread_start() -> None:
    """Python < 3.10 raises no audit event when a thread starts, so wrap the primitive instead.

    The wrapper only refuses while agent code is running on the calling thread.
    """
    import _thread
    import threading

    original = _thread.start_new_thread

    def guarded(function, args, kwargs=None):
        if _armed():
            raise TripwireError("starting threads is not allowed in agent code")
        return original(function, args) if kwargs is None else original(function, args, kwargs)

    _thread.start_new_thread = guarded
    if getattr(threading, "_start_new_thread", None) is original:
        threading._start_new_thread = guarded


def _install_hook() -> None:
    global _hook_installed
    if not _hook_installed and hasattr(sys, "addaudithook"):
        sys.addaudithook(_audit)  # permanent by design; inert unless armed on this thread
        if sys.version_info < (3, 10):
            _guard_thread_start()
        _hook_installed = True


@contextlib.contextmanager
def _guarded(ctx: ToolContext, timeout: float) -> Iterator[None]:
    _install_hook()
    deadline = time.monotonic() + timeout
    previous = sys.gettrace()

    def local_trace(frame, event, arg):
        if time.monotonic() > deadline:
            raise TimeoutError(f"code exceeded the {timeout:.0f}s time limit")
        if ctx.cancel.cancelled:
            raise CancelledError(ctx.cancel.reason)
        return local_trace

    def global_trace(frame, event, arg):
        return local_trace if frame.f_code.co_filename == CODE_FILENAME else None

    _state.roots = [ctx.output_dir, tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())]
    sys.settrace(global_trace)
    _state.armed = True
    try:
        yield
    finally:
        _state.armed = False
        sys.settrace(previous)


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    root = name.split(".")[0]
    if name in BLOCKED_MODULES or root in BLOCKED_MODULES:
        raise ImportError(f"import of {name} is not allowed in agent code")
    return builtins.__import__(name, globals, locals, fromlist, level)


def _namespace(ctx: ToolContext, created: List[str]) -> Dict[str, Any]:
    import qgis.core as core
    from qgis.PyQt import QtCore, QtGui

    safe_builtins = {k: getattr(builtins, k) for k in dir(builtins)
                     if k not in ("eval", "exec", "compile", "breakpoint", "input", "exit", "quit", "help",
                                  "globals", "vars", "memoryview", "__import__")}
    safe_builtins["__import__"] = _guarded_import
    ns: Dict[str, Any] = {"__builtins__": safe_builtins, "__name__": "__qgissi__"}
    ns.update({name: getattr(core, name) for name in dir(core) if name.startswith(("Qgs", "Qgis"))})
    ns.update({"QVariant": QtCore.QVariant, "QDate": QtCore.QDate, "QDateTime": QtCore.QDateTime,
               "QColor": QtGui.QColor, "QSize": QtCore.QSize, "math": math,
               "iface": ctx.iface, "project": ctx.project, "output_dir": ctx.output_dir, "result": None})
    try:
        import processing  # type: ignore
        ns["processing"] = processing
    except ImportError:
        pass

    def layer(ref: str):
        return resolve_layer(ctx, ref)

    def add_layer(lyr, name: str = ""):
        from ._qgis import add_to_project
        return add_to_project(ctx, lyr, name or lyr.name(), created)

    ns["layer"] = layer
    ns["add_layer"] = add_layer
    return ns


@tool("run_pyqgis", "Run Python (PyQGIS) code in the QGIS session when no tool or Processing algorithm fits. "
      "Available names: project, iface (GUI only), processing, all Qgs* classes, QVariant, QColor, math, "
      "layer(name_or_id), add_layer(layer, name), output_dir. print() output is returned; assign a value to "
      "`result` to return it. Do not use it for things dedicated tools can do. Layers you add with "
      "add_layer() or project.addMapLayer() are reported.",
      {"code": {"type": "string", "description": "Python source."},
       "purpose": {"type": "string", "description": "One sentence: what the code does (shown to the user)."},
       "timeout_s": {"type": "integer", "description": f"Time limit (default {int(DEFAULT_TIMEOUT)}, max 600)."}},
      required=["code", "purpose"], risk=Risk.CODE, category="code", untrusted=True)
def run_pyqgis(ctx: ToolContext, code: str, purpose: str, timeout_s: int = int(DEFAULT_TIMEOUT)) -> ToolResult:
    report = check_code(code)
    if not report.ok:
        raise ToolError(report.message())
    created: List[str] = []
    if ctx.output_dir:
        os.makedirs(ctx.output_dir, exist_ok=True)
    namespace = _namespace(ctx, created)
    before = set(ctx.project.mapLayers())
    stdout = io.StringIO()
    error = None
    started = time.time()
    try:
        compiled = compile(code, CODE_FILENAME, "exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout), \
                _guarded(ctx, max(5.0, min(float(timeout_s), 600.0))):
            exec(compiled, namespace)  # noqa: S102 - guarded execution of approved code
    except (CancelledError, KeyboardInterrupt):
        raise
    except BaseException as exc:  # incl. SystemExit, which would otherwise close QGIS
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == CODE_FILENAME]
        where = f" (line {frames[-1].lineno}: {frames[-1].line})" if frames else ""
        error = f"{type(exc).__name__}: {exc}{where}"
    elapsed = time.time() - started
    created += [lid for lid in ctx.project.mapLayers() if lid not in before and lid not in created]
    printed = stdout.getvalue()
    if len(printed) > 8000:
        printed = printed[:8000] + f"\n... [{len(printed) - 8000} more characters]"
    parts = []
    if printed.strip():
        parts.append(f"stdout:\n{printed.rstrip()}")
    value = namespace.get("result")
    if value is not None:
        rendered = repr(json_value(value))
        parts.append(f"result: {rendered[:6000]}")
    if created:
        names = [ctx.project.mapLayer(lid).name() for lid in created if ctx.project.mapLayer(lid)]
        parts.append(f"new layers: {', '.join(names)}")
    parts.append(f"time: {elapsed:.1f}s")
    if error:
        return ToolResult(text=f"Error: {error}\n" + "\n".join(parts), is_error=True, layers_created=created)
    return ToolResult(text="\n".join(parts), layers_created=created)
