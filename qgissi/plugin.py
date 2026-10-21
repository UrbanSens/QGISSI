"""QGIS plugin entry point: toolbar button, menu and the chat dock."""
from __future__ import annotations

import os
from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QAction

ICON = os.path.join(os.path.dirname(__file__), "icons", "qgissi.svg")


class QgissiPlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self.dock = None
        self.action: Optional[QAction] = None
        self.settings_action: Optional[QAction] = None

    def initGui(self) -> None:  # noqa: N802 (QGIS API)
        icon = QIcon(ICON)
        self.action = QAction(icon, "QGISSI agent", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip("Open the QGISSI AI agent")
        self.action.triggered.connect(self.toggle_dock)
        self.settings_action = QAction("QGISSI settings…", self.iface.mainWindow())
        self.settings_action.triggered.connect(self.open_settings)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&QGISSI", self.action)
        self.iface.addPluginToMenu("&QGISSI", self.settings_action)

    def unload(self) -> None:
        if self.dock is not None:
            self.dock.shutdown()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        for action in (self.action, self.settings_action):
            if action is not None:
                self.iface.removePluginMenu("&QGISSI", action)
                self.iface.removeToolBarIcon(action)

    def _ensure_dock(self):
        if self.dock is None:
            from .ui.dock import ChatDock
            self.dock = ChatDock(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            self.dock.visibilityChanged.connect(lambda visible: self.action.setChecked(visible))
        return self.dock

    def toggle_dock(self, checked: bool = True) -> None:
        dock = self._ensure_dock()
        dock.setVisible(bool(checked) or not dock.isVisible())
        if dock.isVisible():
            dock.raise_()
            dock.composer.setFocus()

    def open_settings(self) -> None:
        self._ensure_dock().open_settings()
