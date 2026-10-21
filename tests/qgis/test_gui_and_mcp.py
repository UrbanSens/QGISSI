"""GUI dock flow (offscreen) and the MCP server, in-process."""
import time

from qgissi.providers.base import Message, ModelResponse, Provider, Usage, new_tool_id, text_block


class Scripted(Provider):
    name = "anthropic"

    def __init__(self):
        self.n = 0

    def complete(self, request, on_event=None, cancel=None):
        self.n += 1
        if self.n == 1:
            content = [{"type": "tool_use", "id": new_tool_id(), "name": "run_pyqgis",
                        "input": {"purpose": "Count schools", "code": "result = layer('schools').featureCount()"}}]
            return ModelResponse(Message("assistant", content, provider="test"), "tool_use", Usage(1000, 50),
                                 "claude-opus-5-5")
        return ModelResponse(Message("assistant", [text_block("There are **8** schools.")], provider="test"),
                             "end_turn", Usage(500, 20), "claude-opus-5-5")


def test_dock_runs_agent_with_approval(project, monkeypatch):
    from qgis.PyQt.QtCore import QCoreApplication
    from qgis.testing.mocked import get_iface

    import qgissi
    import qgissi.ui.dock as dock_module
    iface = get_iface()
    iface.activeLayer.return_value = None
    monkeypatch.setattr(dock_module, "create_provider", lambda config: Scripted())
    plugin = qgissi.classFactory(iface)
    plugin.initGui()
    plugin.toggle_dock(True)
    dock = plugin.dock
    dock.approval_combo.setCurrentIndex(dock.approval_combo.findData("ask_risky"))
    dock.composer.setPlainText("How many schools?")
    dock.send()
    approved = False
    deadline = time.time() + 30
    try:
        while time.time() < deadline and not dock.worker.isFinished():
            QCoreApplication.processEvents()
            if not dock.card.isHidden() and not approved:  # the mocked main window is never shown
                buttons = [dock.card_buttons.itemAt(i).widget() for i in range(dock.card_buttons.count())]
                next(b for b in buttons if b.text() == "Allow").click()
                approved = True
            time.sleep(0.01)
        for _ in range(30):
            QCoreApplication.processEvents()
    finally:
        dock.shutdown()
    kinds = [e["kind"] for e in dock.entries]
    assert approved and kinds[0] == "user" and "tool" in kinds and kinds[-1] == "footer", kinds
    tool = next(e for e in dock.entries if e["kind"] == "tool")
    assert tool["result"].startswith("result: 8") and not tool["is_error"]
    assert "8" in dock.to_markdown() and dock.send_button.text() == "Send"
    plugin.unload()


def test_mcp_server_in_process(project, tmp_path):
    from qgissi.mcp_server import McpServer
    server = McpServer(output_dir=str(tmp_path))
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "qgissi"
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert "ask_user" not in {t["name"] for t in tools} and all("inputSchema" in t for t in tools)
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "count_features", "arguments": {"layer": "parks"}}})
    assert reply["result"]["isError"] is False and "\"count\":5" in reply["result"]["content"][0]["text"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    read_only = McpServer(output_dir=str(tmp_path), read_only=True)
    assert all(t.risk == "read" for t in read_only.tools.values())
