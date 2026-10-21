"""Static checks for model-written PyQGIS code.

This is a tripwire, not a sandbox: Python cannot be sandboxed in-process. The
real control is human approval of every code run (the default policy). The
guard stops the most damaging mistakes and prompt-injection payloads when a
user enables autopilot: spawning processes, raw sockets, deleting files,
quitting QGIS and classic sandbox-escape attribute chains.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import List

BLOCKED_MODULES = {
    "subprocess", "socket", "ctypes", "multiprocessing", "pty", "signal", "winreg", "_winapi",
    "http.server", "socketserver", "smtplib", "ftplib", "telnetlib", "paramiko", "pexpect",
    "importlib", "imp", "runpy", "code", "codeop", "marshal", "pickle", "shelve",
}
BLOCKED_CALLS = {
    "eval", "exec", "compile", "__import__", "breakpoint", "input", "exit", "quit",
    "globals", "vars", "setattr", "delattr",
}
BLOCKED_ATTR_CALLS = {
    # module/object attribute -> reason
    "system": "runs a shell command", "popen": "runs a shell command", "spawnl": "spawns a process",
    "spawnv": "spawns a process", "execv": "replaces the process", "execl": "replaces the process",
    "fork": "forks the process", "kill": "kills a process", "_exit": "terminates QGIS",
    "remove": "deletes a file", "unlink": "deletes a file", "rmdir": "deletes a folder",
    "removedirs": "deletes folders", "rmtree": "deletes a folder tree", "chmod": "changes permissions",
    "chown": "changes ownership", "removeAllMapLayers": "removes every layer from the project",
    "clear": None,  # only blocked on QgsProject (checked below)
    "exitQgis": "shuts QGIS down", "actionExit": "quits QGIS",
    "settrace": "disables the time limit", "setprofile": "disables the time limit",
    "addaudithook": "tampers with the safety tripwire",
}
BLOCKED_DUNDERS = {
    "__subclasses__", "__globals__", "__builtins__", "__code__", "__closure__", "__getattribute__",
    "__dict__", "__bases__", "__mro__", "__loader__", "__spec__", "f_globals", "f_locals", "gi_frame",
}


@dataclass
class GuardReport:
    ok: bool
    problems: List[str] = field(default_factory=list)
    writes_files: bool = False
    uses_network: bool = False

    def message(self) -> str:
        return "Code rejected by the safety guard: " + "; ".join(self.problems)


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(_dotted(node.func) + "()")
    return ".".join(reversed(parts))


def check_code(source: str) -> GuardReport:
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return GuardReport(False, [f"syntax error on line {exc.lineno}: {exc.msg}"])
    report = GuardReport(True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                if name in BLOCKED_MODULES or root in BLOCKED_MODULES:
                    report.problems.append(f"import of '{name}' is not allowed")
                if root in ("requests", "urllib", "http"):
                    report.uses_network = True
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_CALLS:
                report.problems.append(f"call to {func.id}() is not allowed")
            elif isinstance(func, ast.Name) and func.id == "open":
                report.writes_files = report.writes_files or _opens_for_write(node)
            elif isinstance(func, ast.Attribute):
                dotted = _dotted(func)
                attr = func.attr
                if attr == "clear":
                    if "QgsProject" in dotted or dotted.startswith("project."):
                        report.problems.append("clearing the whole project is not allowed")
                elif attr in BLOCKED_ATTR_CALLS and BLOCKED_ATTR_CALLS[attr]:
                    if attr in ("remove", "unlink", "rmdir") and not _is_filesystem_call(dotted):
                        continue  # list.remove(), set.remove() etc. are harmless
                    report.problems.append(f"{dotted}() is not allowed ({BLOCKED_ATTR_CALLS[attr]})")
                elif dotted.endswith("sys.exit"):
                    report.problems.append("sys.exit() would close QGIS")
                if attr in ("write_text", "write_bytes", "to_file", "writeAsVectorFormat",
                            "writeAsVectorFormatV2", "writeAsVectorFormatV3"):
                    report.writes_files = True
        elif isinstance(node, ast.Attribute) and node.attr in BLOCKED_DUNDERS:
            report.problems.append(f"access to '{node.attr}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__loader__"):
            report.problems.append(f"access to '{node.id}' is not allowed")
    report.ok = not report.problems
    return report


def _is_filesystem_call(dotted: str) -> bool:
    head = dotted.split(".")[0]
    return head in ("os", "shutil", "pathlib", "Path") or "path" in dotted.lower() or dotted.startswith("os.")


def _opens_for_write(call: ast.Call) -> bool:
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and any(c in mode for c in "wax+")
