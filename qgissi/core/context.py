"""Compact text description of the open QGIS project for the model.

Only metadata is described (layer names, types, CRS, fields, counts); feature
data stays local unless a tool explicitly reads it.
"""
from __future__ import annotations

import hashlib
from typing import List, Tuple

from qgis.core import (QgsProject, QgsRasterLayer, QgsUnitTypes, QgsVectorLayer, QgsWkbTypes)

from ..tools.registry import ToolContext

MAX_LAYERS = 60
MAX_FIELDS = 30


def crs_label(crs) -> str:
    if not crs or not crs.isValid():
        return "unknown CRS"
    units = QgsUnitTypes.toString(crs.mapUnits())
    return f"{crs.authid() or 'custom'} ({crs.description()}, units: {units})"


def describe_layer_line(layer) -> str:
    base = f'- "{layer.name()}" [id: {layer.id()}]'
    if isinstance(layer, QgsVectorLayer):
        geom = QgsWkbTypes.displayString(layer.wkbType()) if layer.isSpatial() else "no geometry (table)"
        count = layer.featureCount()
        selected = layer.selectedFeatureCount()
        fields = layer.fields()
        names = [f"{f.name()}:{f.typeName().lower()}" for f in list(fields)[:MAX_FIELDS]]
        more = f" (+{fields.count() - MAX_FIELDS} more)" if fields.count() > MAX_FIELDS else ""
        sel = f", {selected} selected" if selected else ""
        editing = ", in edit mode" if layer.isEditable() else ""
        return (f"{base} vector {geom}, {count if count >= 0 else 'unknown number of'} features{sel}{editing}, "
                f"{crs_label(layer.crs())}, provider {layer.providerType()}; fields: {', '.join(names)}{more}")
    if isinstance(layer, QgsRasterLayer):
        provider = layer.dataProvider()
        dtype = provider.dataType(1) if provider and layer.bandCount() else None
        return (f"{base} raster {layer.width()}x{layer.height()} px, {layer.bandCount()} band(s), "
                f"pixel size {layer.rasterUnitsPerPixelX():.6g}x{layer.rasterUnitsPerPixelY():.6g}, "
                f"{crs_label(layer.crs())}, provider {layer.providerType()}"
                + (f", data type {int(dtype)}" if dtype is not None else ""))
    return f"{base} {type(layer).__name__.replace('Qgs', '')}, {crs_label(layer.crs())}"


def ordered_layers(project: QgsProject) -> List[Tuple[object, bool, str]]:
    """(layer, visible, group path) in layer-tree order, followed by unlisted layers."""
    result = []
    seen = set()
    for node in project.layerTreeRoot().findLayers():
        layer = node.layer()
        if layer is None:
            continue
        groups = []
        parent = node.parent()
        while parent is not None and parent.parent() is not None:
            groups.append(parent.name())
            parent = parent.parent()
        result.append((layer, node.isVisible(), "/".join(reversed(groups))))
        seen.add(layer.id())
    for layer in project.mapLayers().values():
        if layer.id() not in seen:
            result.append((layer, False, ""))
    return result


def build_project_context(ctx: ToolContext) -> Tuple[str, str]:
    project = ctx.project
    lines = [f"Project: {project.title() or project.baseName() or '(unsaved project)'}"
             f"{' — ' + project.fileName() if project.fileName() else ''}",
             f"Project CRS: {crs_label(project.crs())}"]
    layers = ordered_layers(project)
    layer_lines = []
    for layer, visible, group in layers[:MAX_LAYERS]:
        line = describe_layer_line(layer)
        extras = []
        if group:
            extras.append(f"group {group}")
        if not visible:
            extras.append("hidden")
        layer_lines.append(line + (f" ({', '.join(extras)})" if extras else ""))
    if len(layers) > MAX_LAYERS:
        layer_lines.append(f"... and {len(layers) - MAX_LAYERS} more layers (use list_layers).")
    lines.append(f"Layers ({len(layers)}), top of the layer tree first:")
    lines.extend(layer_lines or ["(no layers loaded)"])
    digest = hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()
    if ctx.iface is not None:
        try:
            canvas = ctx.iface.mapCanvas()
            extent = canvas.extent()
            lines.append(f"Map view: extent {extent.xMinimum():.6g},{extent.yMinimum():.6g} : "
                         f"{extent.xMaximum():.6g},{extent.yMaximum():.6g} (project CRS), scale 1:{canvas.scale():,.0f}")
            active = ctx.iface.activeLayer()
            if active is not None:
                lines.append(f'Active layer: "{active.name()}"')
        except Exception:
            pass
    if ctx.output_dir:
        lines.append(f"Output folder for files you create: {ctx.output_dir}")
    return "\n".join(lines), digest
