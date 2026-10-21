"""QGISSI as a Model Context Protocol server (stdio).

Exposes the QGISSI tool set on a headless QGIS project, so any MCP client
(Claude Code, Claude Desktop, Codex, Gemini CLI, Cursor, ...) can work with
QGIS through the same tools the plugin uses.

    claude mcp add qgissi -- /path/to/QGISSI/scripts/qgis-python -m qgissi.mcp_server \
        --project ~/maps/city.qgz --output-dir ~/maps/out

Safety: the client's own permission prompts apply to every call; tools carry
MCP annotations (readOnlyHint / destructiveHint / openWorldHint). Use
--read-only to expose only read-only tools, or --no-code to hide run_pyqgis.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any, Dict, Optional

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class McpServer:
    def __init__(self, project_path: str = "", output_dir: str = "", read_only: bool = False,
                 allow_code: bool = True, allow_network: bool = True) -> None:
        from .headless import make_context, start_qgis
        start_qgis()
        from qgis.core import QgsProject

        from .tools import REGISTRY, Risk
        self.project = QgsProject.instance()
        if project_path:
            if not self.project.read(os.path.expanduser(project_path)):
                raise SystemExit(f"Could not open project {project_path}")
        self.ctx = make_context(output_dir or None, self.project)
        self.ctx.allow_network = allow_network
        self.registry = REGISTRY
        tools = REGISTRY.select("full", gui=False)
        if read_only:
            tools = [t for t in tools if t.risk in (Risk.READ,)]
        if not allow_code:
            tools = [t for t in tools if t.risk != Risk.CODE]
        tools = [t for t in tools if t.name != "ask_user"]  # the MCP client talks to the user itself
        self.tools = {t.name: t for t in tools}

    # ---------------------------------------------------------------- methods
    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:  # notification
            return None
        try:
            if method == "initialize":
                requested = (message.get("params") or {}).get("protocolVersion")
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "qgissi", "version": __version__},
                          "instructions": "QGIS tools on a headless project. Call describe_layer before using a "
                                          "layer's fields; use search_processing_algorithms + get_algorithm_help "
                                          "before run_processing_algorithm."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.input_schema(),
                                     "annotations": {**t.annotations(), "title": t.name.replace("_", " ")}}
                                    for t in self.tools.values()]}
            elif method == "tools/call":
                result = self.call_tool(message.get("params") or {})
            else:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method {method}"}}
        except Exception as exc:  # protocol-level failure
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from .core.scrub import scrub
        name = params.get("name", "")
        definition = self.tools.get(name)
        if definition is None:
            return {"content": [{"type": "text", "text": f"Unknown tool '{name}'."}], "isError": True}
        if self.ctx.checkpoint is None:
            from .core.checkpoints import RunCheckpoint
            backups = os.path.join(self.ctx.output_dir, ".qgissi_backups")
            self.ctx.checkpoint = RunCheckpoint(self.project, backups, "mcp")
        if definition.risk in ("write", "destructive", "code"):
            self.ctx.checkpoint.before_change(definition, params.get("arguments") or {})
        result = self.registry.invoke(definition, params.get("arguments") or {}, self.ctx)
        content = [{"type": "text", "text": scrub(result.render_text(60000))}]
        content += [{"type": "image", "data": data, "mimeType": media} for media, data in result.images]
        return {"content": content, "isError": bool(result.is_error)}


def serve(server: McpServer) -> None:
    # Keep the protocol channel clean: anything else written to stdout (by QGIS
    # or libraries) goes to stderr.
    protocol_out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            protocol_out.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                           "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            continue
        batch = message if isinstance(message, list) else [message]
        replies = [r for r in (server.handle(m) for m in batch) if r is not None]
        for reply in replies:
            protocol_out.write(json.dumps(reply, ensure_ascii=False) + "\n")
        protocol_out.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qgissi.mcp_server", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="", help="QGIS project to open (.qgz/.qgs)")
    parser.add_argument("--output-dir", default="", help="Folder for files the tools write")
    parser.add_argument("--read-only", action="store_true", help="Expose only read-only tools")
    parser.add_argument("--no-code", action="store_true", help="Do not expose run_pyqgis")
    parser.add_argument("--offline", action="store_true", help="Disable web data tools")
    args = parser.parse_args(argv)
    serve(McpServer(args.project, args.output_dir, args.read_only, not args.no_code, not args.offline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
