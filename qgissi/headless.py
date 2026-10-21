"""Run QGISSI without the QGIS desktop (benchmarks, MCP server, tests).

Run with the Python interpreter that ships with QGIS, e.g. via
``scripts/qgis-python`` which sets the required environment variables.
"""
from __future__ import annotations

import glob
import os
import sys
import tempfile
from typing import Optional

_APP = None


def guess_prefix() -> str:
    env = os.environ.get("QGIS_PREFIX_PATH")
    if env:
        return env
    if sys.platform == "darwin":
        apps = sorted(glob.glob("/Applications/QGIS*.app/Contents/MacOS"))
        if apps:
            return apps[-1]
    if sys.platform.startswith("win"):
        for root in (os.environ.get("OSGEO4W_ROOT", ""), r"C:\OSGeo4W", r"C:\Program Files\QGIS"):
            candidate = os.path.join(root, "apps", "qgis") if root else ""
            if candidate and os.path.isdir(candidate):
                return candidate
    return "/usr"


def start_qgis(gui: bool = False):
    """Start (once) a QgsApplication and the Processing framework."""
    global _APP
    from qgis.core import QgsApplication

    existing = QgsApplication.instance()
    if existing is not None:
        _init_processing()
        return existing
    if _APP is not None:
        return _APP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QgsApplication.setPrefixPath(guess_prefix(), True)
    _APP = QgsApplication([], gui)
    _APP.initQgis()
    _init_processing()
    return _APP


def _init_processing() -> None:
    from qgis.core import QgsApplication

    plugin_dir = os.path.join(QgsApplication.pkgDataPath(), "python", "plugins")
    if os.path.isdir(plugin_dir) and plugin_dir not in sys.path:
        sys.path.append(plugin_dir)
    if not QgsApplication.processingRegistry().providerById("native"):
        from qgis.analysis import QgsNativeAlgorithms
        QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())
    from processing.core.Processing import Processing  # type: ignore
    Processing.initialize()


def stop_qgis() -> None:
    global _APP
    if _APP is not None:
        _APP.exitQgis()
        _APP = None


def make_context(output_dir: Optional[str] = None, project=None):
    """A ToolContext bound to the global project, for headless runs."""
    from qgis.core import QgsProject

    from .tools.registry import ToolContext

    out = output_dir or tempfile.mkdtemp(prefix="qgissi_")
    return ToolContext(project=project or QgsProject.instance(), output_dir=out, headless=True)
