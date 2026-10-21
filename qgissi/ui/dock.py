"""The QGISSI chat panel."""
from __future__ import annotations

import html
import json
import time
from typing import Any, Dict, List, Optional

from qgis.core import QgsProject
from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDockWidget, QFileDialog, QFrame, QHBoxLayout, QLabel,
                                 QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSizePolicy, QTextBrowser,
                                 QToolButton, QVBoxLayout, QWidget)

try:  # Qt5 keeps QShortcut in QtWidgets, Qt6 moved it to QtGui
    from qgis.PyQt.QtWidgets import QShortcut
except ImportError:  # pragma: no cover
    from qgis.PyQt.QtGui import QShortcut

from .. import __version__
from ..core.agent import AgentConfig, AgentSession, ApprovalPolicy, RunResult
from ..core.cancel import CancelToken
from ..core.checkpoints import RunCheckpoint
from ..core.context import build_project_context
from ..core.pricing import format_cost
from ..core.settings import PluginSettings, provider_config
from ..providers import create_provider
from ..providers.catalog import MODELS, PROVIDER_LABELS
from ..tools.registry import Risk, ToolContext
from .markdown import to_html
from .worker import AgentWorker, MainThreadExecutor, UserPrompter

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
_ORPHANED_WORKERS: List[AgentWorker] = []  # still running at unload; destroying a running QThread aborts
APPROVAL_MODES = [("ask_risky", "Balanced: ask before edits & code"), ("ask_all", "Ask before every change"),
                  ("autopilot", "Autopilot: never ask")]
RISK_LABELS = {Risk.READ: "reads data", Risk.NETWORK: "contacts a web service", Risk.WRITE: "changes the project",
               Risk.DESTRUCTIVE: "edits or removes data", Risk.CODE: "runs Python code"}


class ChatDock(QDockWidget):
    def __init__(self, iface, parent: Optional[QWidget] = None) -> None:
        super().__init__("QGISSI", parent)
        self.setObjectName("QgissiDock")
        self.iface = iface
        self.settings = PluginSettings.load()
        self.executor = MainThreadExecutor(self)
        self.prompter = UserPrompter(self)
        self.prompter.approval_requested.connect(self._show_approval)
        self.prompter.question_requested.connect(self._show_question)
        self.session: Optional[AgentSession] = None
        self.worker: Optional[AgentWorker] = None
        self.cancel: Optional[CancelToken] = None
        self.checkpoints: List[RunCheckpoint] = []
        self._current_checkpoint: Optional[RunCheckpoint] = None
        self._tool_state: Dict[str, Any] = {}
        self.entries: List[Dict[str, Any]] = []
        self.session_cost: Optional[float] = 0.0
        self.session_tokens = 0
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render)
        self._build_ui()
        self._render()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        top = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.model_combo.setToolTip("Model as provider/model, e.g. anthropic/claude-opus-5-5 or ollama/qwen3:8b")
        self._fill_models()
        self.effort_combo = QComboBox()
        self.effort_combo.addItems(EFFORTS)
        self.effort_combo.setCurrentText(self.settings.effort)
        self.effort_combo.setToolTip("Reasoning effort: higher thinks longer and costs more")
        self.approval_combo = QComboBox()
        for key, label in APPROVAL_MODES:
            self.approval_combo.addItem(label.split(":")[0], key)
            self.approval_combo.setItemData(self.approval_combo.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
        self.approval_combo.setCurrentIndex(max(0, [k for k, _ in APPROVAL_MODES].index(self.settings.approval_mode)))
        menu_button = QToolButton()
        menu_button.setText("⋯")
        menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(menu_button)
        menu.addAction("New chat", self.new_chat)
        self.undo_action = menu.addAction("Undo last run", self.undo_last_run)
        menu.addAction("Export chat…", self.export_chat)
        menu.addSeparator()
        menu.addAction("Settings…", self.open_settings)
        menu_button.setMenu(menu)
        for widget in (self.model_combo, self.effort_combo, self.approval_combo, menu_button):
            top.addWidget(widget)
        layout.addLayout(top)

        self.transcript = QTextBrowser()
        self.transcript.setOpenLinks(False)
        self.transcript.document().setDefaultStyleSheet("a { text-decoration: none; } code { font-family: monospace; }")
        self.transcript.anchorClicked.connect(self._anchor_clicked)
        layout.addWidget(self.transcript, 1)

        self.card = QFrame()
        self.card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(self.card)
        self.card_title = QLabel()
        self.card_title.setWordWrap(True)
        self.card_details = QPlainTextEdit()
        self.card_details.setReadOnly(True)
        self.card_details.setMaximumHeight(160)
        self.card_input = QLineEdit()
        self.card_buttons = QHBoxLayout()
        card_layout.addWidget(self.card_title)
        card_layout.addWidget(self.card_details)
        card_layout.addWidget(self.card_input)
        card_layout.addLayout(self.card_buttons)
        self.card.hide()
        layout.addWidget(self.card)

        self.composer = QPlainTextEdit()
        self.composer.setPlaceholderText("Ask QGISSI to find, analyse, style or map your data…  (Ctrl+Enter to send)")
        self.composer.setMaximumHeight(110)
        layout.addWidget(self.composer)
        shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.composer)
        shortcut.activated.connect(self.send)

        bottom = QHBoxLayout()
        self.attach_map = QCheckBox("Attach map view")
        self.attach_map.setToolTip("Send an image of the current map with the message (vision models)")
        self.status = QLabel("Ready")
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.cost_label = QLabel()
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._send_or_stop)
        for widget in (self.attach_map, self.status, self.cost_label, self.send_button):
            bottom.addWidget(widget)
        layout.addLayout(bottom)
        self.setWidget(root)
        self._update_cost()

    def _fill_models(self) -> None:
        self.model_combo.clear()
        for info in MODELS.values():
            self.model_combo.addItem(f"{info.provider}/{info.id}")
        current = f"{self.settings.provider}/{self.settings.model}"
        if self.model_combo.findText(current) < 0:
            self.model_combo.insertItem(0, current)
        self.model_combo.setCurrentText(current)

    # ------------------------------------------------------------- session
    def _selected_model(self):
        text = self.model_combo.currentText().strip()
        provider, _, model = text.partition("/")
        if not model:
            provider, model = ("anthropic", provider) if provider.startswith("claude-") else (self.settings.provider, provider)
        if provider not in PROVIDER_LABELS:
            raise ValueError(f"Unknown provider '{provider}'. Use one of: {', '.join(PROVIDER_LABELS)}")
        return provider, model

    def _ctx_factory(self, cancel: CancelToken, run_id: str) -> ToolContext:
        return ToolContext(project=QgsProject.instance(), iface=self.iface,
                           output_dir=self.settings.effective_output_dir(), cancel=cancel, run_id=run_id,
                           headless=False, checkpoint=self._current_checkpoint, ask_user=self.prompter.ask_question,
                           progress=self._progress, allow_network=self.settings.allow_network, state=self._tool_state)

    def _progress(self, text: str) -> None:
        self.status.setText(text[:120])

    def _prepare_session(self) -> AgentSession:
        provider_name, model = self._selected_model()
        effort = self.effort_combo.currentText()
        mode = self.approval_combo.currentData()
        provider = create_provider(provider_config(self.settings, provider_name))
        if self.session is None:
            config = AgentConfig(provider=provider_name, model=model, effort=effort, max_steps=self.settings.max_steps,
                                 max_cost_usd=self.settings.max_cost_usd, approval_mode=mode,
                                 tool_tier=self.settings.tool_tier, thinking_display=self.settings.thinking_display,
                                 allow_fallback=self.settings.enable_fallbacks)
            approval = ApprovalPolicy(mode, ask_fn=self.prompter.ask_approval)
            self.session = AgentSession(provider, config, ctx_factory=self._ctx_factory, executor=self.executor,
                                        approval=approval, context_builder=build_project_context, gui=True)
        else:
            self.session.provider = provider
            self.session.config.provider, self.session.config.model, self.session.config.effort = provider_name, model, effort
            self.session.approval.mode = mode
        self.settings.provider, self.settings.model, self.settings.effort, self.settings.approval_mode = (
            provider_name, model, effort, mode)
        self.settings.save()
        return self.session

    # ----------------------------------------------------------------- send
    def _send_or_stop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.stop()
        else:
            self.send()

    def send(self) -> None:
        text = self.composer.toPlainText().strip()
        if not text or (self.worker is not None and self.worker.isRunning()):
            return
        try:
            session = self._prepare_session()
        except Exception as exc:
            self._add("error", text=str(exc))
            return
        images = []
        if self.attach_map.isChecked():
            try:
                from ..tools.map_tools import png_base64, render_image
                canvas = self.iface.mapCanvas()
                images.append(("image/png", png_base64(render_image(self._ctx_factory(CancelToken(), "attach"),
                                                                   min(canvas.width(), 1400),
                                                                   min(canvas.height(), 1000)))))
            except Exception as exc:
                self._add("error", text=f"Could not capture the map view: {exc}")
        self.composer.clear()
        self.cancel = CancelToken()
        self.executor.cancel = self.cancel
        self.prompter.cancel = self.cancel
        self._current_checkpoint = RunCheckpoint(QgsProject.instance(), self.settings.backup_dir(),
                                                 time.strftime("%Y%m%d-%H%M%S"))
        self._current_checkpoint.begin()  # the run's baseline, taken before anything can change
        self._add("user", text=text, images=len(images))
        self.worker = AgentWorker(session, text, images, self.cancel, self)
        self.worker.event.connect(self._on_event)
        self.worker.finished_run.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.send_button.setText("Stop")
        self.status.setText(f"Working with {session.config.model}…")
        self.worker.start()

    def stop(self) -> None:
        if self.cancel is not None:
            self.cancel.cancel("stopped by user")
            self.status.setText("Stopping…")
        self.card.hide()

    # ---------------------------------------------------------------- events
    def _last(self, kind: str) -> Optional[Dict[str, Any]]:
        if self.entries and self.entries[-1]["kind"] == kind and self.entries[-1].get("open"):
            return self.entries[-1]
        return None

    def _on_event(self, kind: str, payload: Any) -> None:
        if kind == "text_delta":
            entry = self._last("assistant") or self._add("assistant", text="", open=True, defer=True)
            entry["text"] += payload["text"]
        elif kind == "thinking_delta":
            entry = self._last("thinking") or self._add("thinking", text="", open=True, defer=True)
            entry["text"] += payload["text"]
        elif kind == "tool_call":
            self._close_open()
            self._add("tool", name=payload["name"], input=payload["input"], risk=payload.get("risk"), result=None,
                      defer=True)
        elif kind == "tool_result":
            record = payload["record"]
            for entry in reversed(self.entries):
                if entry["kind"] == "tool" and entry.get("result") is None and entry["name"] == record.name:
                    entry.update(result=record.result, is_error=record.is_error, duration=record.duration_s,
                                 approved=record.approved)
                    break
        elif kind == "step":
            self._close_open()
            record = payload["record"]
            self.session_tokens += sum(record.usage.values())
            if record.cost_usd is None or self.session_cost is None:
                self.session_cost = None
            else:
                self.session_cost += record.cost_usd
            self._update_cost()
        elif kind == "stream_reset":  # the provider retries the request: drop its partial output
            while self.entries and self.entries[-1].get("open") and self.entries[-1]["kind"] in ("assistant", "thinking"):
                self.entries.pop()
        elif kind == "status":
            self.status.setText(str(payload.get("text", ""))[:120])
        self._schedule_render()

    def _close_open(self) -> None:
        for entry in self.entries:
            entry.pop("open", None)

    def _on_finished(self, result: RunResult) -> None:
        self._close_open()
        self._record_checkpoint()
        summary = (f"{len(result.steps)} step(s) · {len(result.tool_calls)} tool call(s) · "
                   f"{format_cost(result.cost_usd)} · {result.wall_s:.0f}s")
        if result.status == "completed":
            if result.final_text and not any(e["kind"] == "assistant" and e.get("text") == result.final_text
                                             for e in self.entries[-3:]):
                self._add("assistant", text=result.final_text, defer=True)
            self._add("footer", text=summary, defer=True)
        else:
            reason = {"error": result.error or "error", "refusal": result.final_text, "cancelled": "Stopped.",
                      "budget": "Stopped: cost budget for one run reached (Settings).",
                      "max_steps": "Stopped: step limit reached (Settings).", "timeout": "Stopped: time limit reached."
                      }.get(result.status, result.status)
            self._add("error", text=f"{reason}  ({summary})", defer=True)
            if result.status == "error" and "API key" in (result.error or ""):
                self._add("error", text="Open ⋯ > Settings to add an API key, or pick an Ollama model.", defer=True)
        self._finish_ui()

    def _on_failed(self, message: str) -> None:
        self._record_checkpoint()
        self._add("error", text=message, defer=True)
        self._finish_ui()

    def _record_checkpoint(self) -> None:
        checkpoint, self._current_checkpoint = self._current_checkpoint, None
        if checkpoint is not None:
            checkpoint.finish()
            if checkpoint.has_changes:
                self.checkpoints.append(checkpoint)

    def _finish_ui(self) -> None:
        self.send_button.setText("Send")
        self.status.setText("Ready")
        self.card.hide()
        self._render()

    # ----------------------------------------------------------------- cards
    def _clear_card_buttons(self) -> None:
        while self.card_buttons.count():
            item = self.card_buttons.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _show_approval(self, payload: Dict[str, Any]) -> None:
        self._clear_card_buttons()
        args = payload["args"]
        self.card_title.setText(f"<b>Allow {html.escape(payload['tool'])}?</b> It "
                                f"{RISK_LABELS.get(payload['risk'], 'changes something')}.")
        if payload["tool"] == "run_pyqgis":
            details = f"# {args.get('purpose', '')}\n{args.get('code', '')}"
        else:
            details = json.dumps(args, indent=2, ensure_ascii=False)
        self.card_details.setPlainText(details)
        self.card_details.show()
        self.card_input.hide()
        for label, answer in (("Allow", "allow"), ("Always allow in this chat", "always"), ("Deny", "deny")):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, a=answer: self._answer(payload, a))
            self.card_buttons.addWidget(button)
        self.card.show()
        self.status.setText("Waiting for your approval")

    def _show_question(self, payload: Dict[str, Any]) -> None:
        self._clear_card_buttons()
        self.card_title.setText(f"<b>QGISSI asks:</b> {html.escape(payload['question'])}")
        self.card_details.hide()
        self.card_input.clear()
        self.card_input.setPlaceholderText("Type an answer…")
        self.card_input.show()
        for option in payload.get("options") or []:
            button = QPushButton(option)
            button.clicked.connect(lambda _=False, o=option: self._answer(payload, o))
            self.card_buttons.addWidget(button)
        send = QPushButton("Answer")
        send.clicked.connect(lambda: self._answer(payload, self.card_input.text().strip() or "(no answer)"))
        self.card_buttons.addWidget(send)
        self.card.show()
        self.status.setText("Waiting for your answer")

    def _answer(self, payload: Dict[str, Any], answer: str) -> None:
        self.card.hide()
        self.status.setText("Working…")
        payload["reply"](answer)

    # --------------------------------------------------------------- actions
    def new_chat(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self.session = None
        self.entries.clear()
        self._tool_state.clear()
        self.session_cost, self.session_tokens = 0.0, 0
        self._update_cost()
        self._render()

    def undo_last_run(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if not self.checkpoints:
            self._add("status", text="Nothing to undo.")
            return
        messages = self.checkpoints.pop().undo()
        self._add("status", text="Undo: " + "; ".join(messages))
        if self.iface is not None:
            self.iface.mapCanvas().refreshAllLayers()

    def export_chat(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export chat", "qgissi-chat.md", "Markdown (*.md)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_markdown())

    def to_markdown(self) -> str:
        lines = [f"# QGISSI chat ({time.strftime('%Y-%m-%d %H:%M')})", ""]
        for entry in self.entries:
            kind = entry["kind"]
            if kind == "user":
                lines += [f"**You:** {entry['text']}", ""]
            elif kind == "assistant":
                lines += [entry["text"], ""]
            elif kind == "tool":
                lines += [f"- `{entry['name']}` {json.dumps(entry['input'], ensure_ascii=False)[:300]}"
                          f" → {'error' if entry.get('is_error') else 'ok'}"]
            elif kind in ("footer", "status", "error"):
                lines += ["", f"_{entry['text']}_", ""]
        return "\n".join(lines)

    def open_settings(self) -> None:
        from .settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings = PluginSettings.load()
            self._fill_models()
            self.effort_combo.setCurrentText(self.settings.effort)
            if self.session is not None:
                self.session.config.max_steps = self.settings.max_steps
                self.session.config.max_cost_usd = self.settings.max_cost_usd

    def _anchor_clicked(self, url: QUrl) -> None:
        target = url.toString()
        if target.startswith("#toggle-"):
            index = int(target.split("-")[1])
            self.entries[index]["expanded"] = not self.entries[index].get("expanded")
            self._render()
        elif target.startswith("http"):
            from qgis.PyQt.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)

    # ------------------------------------------------------------- rendering
    def _add(self, kind: str, defer: bool = False, **data: Any) -> Dict[str, Any]:
        entry = {"kind": kind, **data}
        self.entries.append(entry)
        if defer:
            self._schedule_render()
        else:
            self._render()
        return entry

    def _schedule_render(self) -> None:
        if not self._render_timer.isActive():
            self._render_timer.start(80)

    def _update_cost(self) -> None:
        cost = format_cost(self.session_cost) if self.session_cost is not None else "cost n/a"
        self.cost_label.setText(f"{cost} · {self.session_tokens:,} tok")

    def _render(self) -> None:
        bar = self.transcript.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 30
        parts = ["<div style='font-size:13px'>"]
        if not self.entries:
            parts.append(f"<p><b>QGISSI {__version__}</b> — an open AI agent for QGIS. Ask in plain language; "
                         "QGISSI inspects your layers, runs Processing, styles maps and builds layouts using the "
                         "model you choose. Changes can be undone from the ⋯ menu.</p>"
                         "<p style='color:gray'>Examples: <i>Which districts have the most schools?</i> · "
                         "<i>Buffer the rivers by 100 m and find the buildings inside</i> · "
                         "<i>Make a choropleth of population density and an A4 layout</i></p>")
        for index, entry in enumerate(self.entries):
            parts.append(self._entry_html(index, entry))
        parts.append("</div>")
        self.transcript.setHtml("".join(parts))
        if at_bottom:
            bar.setValue(bar.maximum())

    def _entry_html(self, index: int, entry: Dict[str, Any]) -> str:
        kind = entry["kind"]
        esc = html.escape
        if kind == "user":
            extra = " <span style='color:gray'>(+ map image)</span>" if entry.get("images") else ""
            return (f"<table width='100%' cellpadding='6'><tr><td style='background-color:rgba(47,129,247,0.12)'>"
                    f"<b>You</b>{extra}<br>{esc(entry['text']).replace(chr(10), '<br>')}</td></tr></table>")
        if kind == "assistant":
            return to_html(entry.get("text", ""))
        if kind == "thinking":
            text = entry.get("text", "").strip()
            if not text:
                return ""
            short = text if entry.get("expanded") or len(text) < 220 else text[:220] + "…"
            toggle = "" if len(text) < 220 else f" <a href='#toggle-{index}'>{'less' if entry.get('expanded') else 'more'}</a>"
            return f"<p style='color:gray'><i>{esc(short)}</i>{toggle}</p>"
        if kind == "tool":
            result = entry.get("result")
            mark = "…" if result is None else ("✗" if entry.get("is_error") else "✓")
            color = "#b42318" if entry.get("is_error") else "gray"
            summary = _arg_summary(entry["name"], entry.get("input") or {})
            duration = f" {entry['duration']:.1f}s" if entry.get("duration") is not None else ""
            head = (f"<p style='margin:2px 0'><a href='#toggle-{index}'>{'▾' if entry.get('expanded') else '▸'}</a> "
                    f"<span style='color:{color}'>{mark}</span> <code>{esc(entry['name'])}</code> "
                    f"<span style='color:gray'>{esc(summary)}{duration}</span></p>")
            if not entry.get("expanded"):
                return head
            body = esc(json.dumps(entry.get("input"), indent=1, ensure_ascii=False))[:4000]
            out = esc(str(result or ""))[:4000]
            return (head + f"<pre style='color:gray;margin-left:16px'>{body}</pre>"
                    f"<pre style='margin-left:16px'>{out}</pre>")
        if kind == "footer":
            return f"<p style='color:gray;font-size:11px'>— {esc(entry['text'])}</p>"
        if kind == "status":
            return f"<p style='color:gray'>{esc(entry['text'])}</p>"
        if kind == "error":
            return f"<p style='color:#b42318'>{esc(entry['text'])}</p>"
        return ""

    def shutdown(self) -> None:
        if self.cancel is not None:
            self.cancel.cancel("QGIS is closing")
        worker = self.worker
        if worker is not None and worker.isRunning() and not worker.wait(5000):
            worker.setParent(None)
            _ORPHANED_WORKERS.append(worker)
            worker.finished.connect(lambda w=worker: _ORPHANED_WORKERS.remove(w) if w in _ORPHANED_WORKERS else None)


def _arg_summary(name: str, args: Dict[str, Any]) -> str:
    if name == "run_processing_algorithm":
        return f"{args.get('algorithm_id', '')} → {args.get('output_name', '')}".strip(" →")
    if name == "run_pyqgis":
        return str(args.get("purpose", ""))[:80]
    for key in ("layer", "query", "expression", "name", "tags", "basemap", "source"):
        if key in args:
            return f"{key}={str(args[key])[:60]}"
    return ""
