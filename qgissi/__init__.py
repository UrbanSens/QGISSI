"""QGISSI - QGIS Spatial Intelligence: an open, bring-your-own-model AI agent for QGIS."""

__version__ = "0.1.0"


def classFactory(iface):  # noqa: N802 (QGIS plugin entry point)
    from .plugin import QgissiPlugin

    return QgissiPlugin(iface)
