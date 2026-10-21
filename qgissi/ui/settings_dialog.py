"""Settings dialog: provider keys, endpoints, agent behaviour and privacy."""
from __future__ import annotations

from typing import Dict

from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                                 QTabWidget, QVBoxLayout, QWidget)

from ..core.settings import PluginSettings, key_source, load_api_key, provider_config, store_api_key
from ..providers import create_provider
from ..providers.catalog import PROVIDER_LABELS

PRIVACY = """<p><b>What leaves your computer.</b> QGISSI talks only to the model provider you choose and to the
open-data services its tools call. Each request to the model contains your message, a description of the
project (layer names, types, CRS, field names, feature counts; no feature data) and the results of tools the agent
ran, which can include attribute values it read. Secrets in tool output are masked before sending.</p>
<p>Geocoding and OpenStreetMap downloads send search text or an area to nominatim.openstreetmap.org and
overpass-api.de. With Ollama or another local server, nothing leaves your machine except those tool calls.</p>
<p>There is no telemetry. Chats are kept in memory only, unless you export them.</p>"""


class SettingsDialog(QDialog):
    def __init__(self, settings: PluginSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("QGISSI settings")
        self.setMinimumWidth(560)
        self.settings = settings
        self.key_edits: Dict[str, QLineEdit] = {}
        self.url_edits: Dict[str, QLineEdit] = {}
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._providers_tab(), "Models")
        tabs.addTab(self._agent_tab(), "Agent")
        about = QLabel(PRIVACY)
        about.setWordWrap(True)
        tabs.addTab(about, "Privacy")
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _providers_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        urls = {"anthropic": self.settings.anthropic_base_url, "openai": self.settings.openai_base_url,
                "openai_compatible": self.settings.compat_base_url, "ollama": self.settings.ollama_base_url}
        for provider, label in PROVIDER_LABELS.items():
            row = QHBoxLayout()
            if provider != "ollama":
                key = QLineEdit(load_api_key(provider))
                key.setEchoMode(QLineEdit.EchoMode.Password)
                key.setPlaceholderText("API key")
                self.key_edits[provider] = key
                row.addWidget(key, 2)
            if provider in urls:
                url = QLineEdit(urls[provider])
                url.setPlaceholderText("Base URL (optional)" if provider != "openai_compatible" else "http://host:port/v1")
                self.url_edits[provider] = url
                row.addWidget(url, 2)
            test = QPushButton("Test")
            test.clicked.connect(lambda _=False, p=provider: self._test(p))
            row.addWidget(test)
            form.addRow(QLabel(f"<b>{label}</b>"), row)
            if provider != "ollama":
                form.addRow("", QLabel(f"<small>Stored in: {key_source(provider)}</small>"))
        note = QLabel("<small>Keys are kept in the QGIS authentication database (encrypted with your master "
                      "password). Without a master password they are stored unencrypted in the QGIS settings.</small>")
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def _agent_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        s = self.settings
        self.default_model = QLineEdit(f"{s.provider}/{s.model}")
        self.effort = QComboBox()
        self.effort.addItems(["low", "medium", "high", "xhigh", "max"])
        self.effort.setCurrentText(s.effort)
        self.approval = QComboBox()
        for key, label in (("ask_risky", "Balanced (ask before edits and code)"), ("ask_all", "Ask before every change"),
                           ("autopilot", "Autopilot (never ask)")):
            self.approval.addItem(label, key)
        self.approval.setCurrentIndex(max(0, self.approval.findData(s.approval_mode)))
        self.tier = QComboBox()
        self.tier.addItem("All tools", "full")
        self.tier.addItem("Core tools (better for small local models)", "core")
        self.tier.setCurrentIndex(max(0, self.tier.findData(s.tool_tier)))
        self.max_steps = QSpinBox()
        self.max_steps.setRange(1, 200)
        self.max_steps.setValue(s.max_steps)
        self.max_cost = QDoubleSpinBox()
        self.max_cost.setRange(0, 1000)
        self.max_cost.setDecimals(2)
        self.max_cost.setPrefix("$ ")
        self.max_cost.setValue(s.max_cost_usd)
        self.max_cost.setToolTip("Stop a run when it costs more than this (0 = no limit)")
        self.thinking = QComboBox()
        self.thinking.addItem("Show reasoning summaries", "summarized")
        self.thinking.addItem("Hide reasoning", "omitted")
        self.thinking.setCurrentIndex(max(0, self.thinking.findData(s.thinking_display)))
        self.network = QCheckBox("Allow web data tools (geocoding, OpenStreetMap)")
        self.network.setChecked(s.allow_network)
        self.fallbacks = QCheckBox("Anthropic server-side fallback when a request is declined")
        self.fallbacks.setChecked(s.enable_fallbacks)
        self.num_ctx = QSpinBox()
        self.num_ctx.setRange(4096, 1_048_576)
        self.num_ctx.setSingleStep(4096)
        self.num_ctx.setValue(s.ollama_num_ctx)
        out_row = QHBoxLayout()
        self.output_dir = QLineEdit(s.output_dir)
        self.output_dir.setPlaceholderText(s.effective_output_dir())
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        out_row.addWidget(self.output_dir)
        out_row.addWidget(browse)
        for label, widget in (("Default model", self.default_model), ("Effort", self.effort),
                              ("Approvals", self.approval), ("Tools", self.tier), ("Max steps per run", self.max_steps),
                              ("Max cost per run", self.max_cost), ("Reasoning", self.thinking),
                              ("Ollama context length", self.num_ctx)):
            form.addRow(label, widget)
        form.addRow("Output folder", out_row)
        form.addRow(self.network)
        form.addRow(self.fallbacks)
        return page

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Output folder", self.output_dir.text())
        if folder:
            self.output_dir.setText(folder)

    def _apply_urls(self) -> None:
        s = self.settings
        s.anthropic_base_url = self.url_edits["anthropic"].text().strip()
        s.openai_base_url = self.url_edits["openai"].text().strip()
        s.compat_base_url = self.url_edits["openai_compatible"].text().strip()
        s.ollama_base_url = self.url_edits["ollama"].text().strip() or "http://localhost:11434"

    def _test(self, provider: str) -> None:
        self._apply_urls()
        config = provider_config(self.settings, provider)
        if provider in self.key_edits and self.key_edits[provider].text().strip():
            config.api_key = self.key_edits[provider].text().strip()
        try:
            models = create_provider(config).list_models()
        except Exception as exc:
            QMessageBox.warning(self, "QGISSI", f"{PROVIDER_LABELS[provider]}: {exc}")
            return
        preview = ", ".join(models[:25]) + (" …" if len(models) > 25 else "")
        QMessageBox.information(self, "QGISSI", f"Connected. {len(models)} model(s): {preview or '(none listed)'}")

    def _save(self) -> None:
        s = self.settings
        self._apply_urls()
        provider, _, model = self.default_model.text().strip().partition("/")
        if provider in PROVIDER_LABELS and model:
            s.provider, s.model = provider, model
        s.effort = self.effort.currentText()
        s.approval_mode = self.approval.currentData()
        s.tool_tier = self.tier.currentData()
        s.max_steps = self.max_steps.value()
        s.max_cost_usd = self.max_cost.value()
        s.thinking_display = self.thinking.currentData()
        s.allow_network = self.network.isChecked()
        s.enable_fallbacks = self.fallbacks.isChecked()
        s.ollama_num_ctx = self.num_ctx.value()
        s.output_dir = self.output_dir.text().strip()
        s.save()
        unencrypted = []
        for name, edit in self.key_edits.items():
            value = edit.text().strip()
            if value != load_api_key(name) or not value:
                where = store_api_key(name, value)
                if where == "settings":
                    unencrypted.append(PROVIDER_LABELS[name])
        if unencrypted:
            QMessageBox.information(self, "QGISSI", "No QGIS master password is available, so these keys were "
                                    "stored unencrypted in the QGIS settings: " + ", ".join(unencrypted))
        self.accept()
