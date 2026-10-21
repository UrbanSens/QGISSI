"""Shared PyQGIS helpers for tools (layer lookup, value conversion, outputs)."""
from __future__ import annotations

import difflib
import os
import re
from typing import Any, Dict, List, Optional

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsField, QgsMapLayer,
                       QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime, QVariant

from .registry import ToolContext, ToolError

try:  # QGIS >= 3.38 prefers QMetaType for field types
    from qgis.PyQt.QtCore import QMetaType
except ImportError:  # pragma: no cover
    QMetaType = None

_FIELD_TYPES = {"string": "QString", "text": "QString", "integer": "Int", "int": "Int", "integer64": "LongLong",
                "double": "Double", "real": "Double", "number": "Double", "float": "Double", "boolean": "Bool",
                "bool": "Bool", "date": "QDate", "datetime": "QDateTime", "time": "QTime"}


def make_field(name: str, type_name: str = "string", length: int = 0, precision: int = 0) -> QgsField:
    key = _FIELD_TYPES.get(type_name.lower())
    if key is None:
        raise ToolError(f"Unsupported field type '{type_name}'. Use one of: {', '.join(sorted(set(_FIELD_TYPES)))}")
    if QMetaType is not None and Qgis.QGIS_VERSION_INT >= 33800:
        meta = getattr(QMetaType.Type, key)
        return QgsField(name, meta, len=length, prec=precision)
    variant = {"QString": QVariant.String, "Int": QVariant.Int, "LongLong": QVariant.LongLong,
               "Double": QVariant.Double, "Bool": QVariant.Bool, "QDate": QVariant.Date,
               "QDateTime": QVariant.DateTime, "QTime": QVariant.Time}[key]
    return QgsField(name, variant, len=length, prec=precision)


def json_value(value: Any) -> Any:
    """Convert QVariant/Qt values to JSON-friendly Python values."""
    if value is None:
        return None
    if isinstance(value, QVariant):
        return None if value.isNull() else json_value(value.value())
    if isinstance(value, QDateTime):
        return value.toString("yyyy-MM-ddTHH:mm:ss") if value.isValid() else None
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd") if value.isValid() else None
    if isinstance(value, QTime):
        return value.toString("HH:mm:ss") if value.isValid() else None
    if isinstance(value, float):
        return round(value, 10)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


def geometry_kind(layer: QgsVectorLayer) -> str:
    """'point' | 'line' | 'polygon' | 'none' independent of the QGIS enum API version."""
    if not layer.isSpatial():
        return "none"
    label = QgsWkbTypes.geometryDisplayString(layer.geometryType()).lower()
    for kind in ("point", "line", "polygon"):
        if kind in label:
            return kind
    return "none"


def all_layers(project: QgsProject) -> List[QgsMapLayer]:
    return list(project.mapLayers().values())


def resolve_layer(ctx: ToolContext, ref: str, kind: Optional[str] = None) -> QgsMapLayer:
    """Find a layer by id, exact name, case-insensitive name or unique partial name."""
    if not ref:
        raise ToolError("A layer name or id is required.")
    project = ctx.project
    layer = project.mapLayer(ref)
    if layer is None:
        layers = all_layers(project)
        exact = [lyr for lyr in layers if lyr.name() == ref]
        folded = [lyr for lyr in layers if lyr.name().lower() == ref.lower().strip()]
        partial = [lyr for lyr in layers if ref.lower().strip() in lyr.name().lower()]
        for candidates in (exact, folded, partial):
            if len(candidates) == 1:
                layer = candidates[0]
                break
            if len(candidates) > 1:
                names = ", ".join(f'"{c.name()}" ({c.id()})' for c in candidates[:8])
                raise ToolError(f"Layer reference '{ref}' is ambiguous: {names}. Use the layer id.")
    if layer is None:
        names = [lyr.name() for lyr in all_layers(project)]
        close = difflib.get_close_matches(ref, names, n=3, cutoff=0.4)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ToolError(f"No layer named '{ref}'.{hint} Available layers: {', '.join(names) or 'none'}.")
    if kind == "vector" and not isinstance(layer, QgsVectorLayer):
        raise ToolError(f"Layer '{layer.name()}' is not a vector layer.")
    if kind == "raster" and not isinstance(layer, QgsRasterLayer):
        raise ToolError(f"Layer '{layer.name()}' is not a raster layer.")
    return layer


def resolve_vector(ctx: ToolContext, ref: str) -> QgsVectorLayer:
    return resolve_layer(ctx, ref, "vector")  # type: ignore[return-value]


def resolve_raster(ctx: ToolContext, ref: str) -> QgsRasterLayer:
    return resolve_layer(ctx, ref, "raster")  # type: ignore[return-value]


def require_field(layer: QgsVectorLayer, name: str) -> str:
    if layer.fields().indexOf(name) >= 0:
        return name
    lowered = {f.name().lower(): f.name() for f in layer.fields()}
    if name.lower() in lowered:
        return lowered[name.lower()]
    close = difflib.get_close_matches(name, list(lowered.values()), n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise ToolError(f"Layer '{layer.name()}' has no field '{name}'.{hint} "
                    f"Fields: {', '.join(f.name() for f in layer.fields())}.")


def unique_layer_name(project: QgsProject, name: str) -> str:
    existing = {lyr.name() for lyr in project.mapLayers().values()}
    if name not in existing:
        return name
    index = 2
    while f"{name} ({index})" in existing:
        index += 1
    return f"{name} ({index})"


def add_to_project(ctx: ToolContext, layer: QgsMapLayer, name: Optional[str] = None,
                   created: Optional[List[str]] = None) -> QgsMapLayer:
    if not layer.isValid():
        raise ToolError(f"Layer '{name or layer.name()}' is not valid and was not added.")
    layer.setName(unique_layer_name(ctx.project, name or layer.name()))
    ctx.project.addMapLayer(layer)
    if created is not None:
        created.append(layer.id())
    return layer


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", name).strip().replace(" ", "_")
    return cleaned or "output"


def output_path(ctx: ToolContext, filename: str, overwrite: bool = False) -> str:
    """Resolve a file name inside the output folder (absolute paths are kept)."""
    if not os.path.isabs(filename):
        base = ctx.output_dir or os.path.join(os.path.expanduser("~"), "QGISSI")
        os.makedirs(base, exist_ok=True)
        filename = os.path.join(base, filename)
    folder = os.path.dirname(filename)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    if os.path.exists(filename) and not overwrite:
        raise ToolError(f"File already exists: {filename}. Choose another name or set overwrite=true.")
    return filename


def parse_crs(value: str) -> QgsCoordinateReferenceSystem:
    crs = QgsCoordinateReferenceSystem(value)
    if not crs.isValid() and value.isdigit():
        crs = QgsCoordinateReferenceSystem(f"EPSG:{value}")
    if not crs.isValid():
        raise ToolError(f"Unknown CRS '{value}'. Use an authority id such as EPSG:25832.")
    return crs


def to_wgs84_extent(layer: QgsMapLayer, project: QgsProject) -> Optional[List[float]]:
    try:
        extent = layer.extent()
        if extent.isEmpty():
            return None
        transform = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), project)
        box = transform.transformBoundingBox(extent)
        return [round(box.xMinimum(), 6), round(box.yMinimum(), 6), round(box.xMaximum(), 6), round(box.yMaximum(), 6)]
    except Exception:
        return None


def rect_list(rect: QgsRectangle, digits: int = 6) -> List[float]:
    return [round(rect.xMinimum(), digits), round(rect.yMinimum(), digits),
            round(rect.xMaximum(), digits), round(rect.yMaximum(), digits)]


def summarize(rows: List[Dict[str, Any]], limit: int) -> Dict[str, Any]:
    return {"returned": len(rows[:limit]), "rows": rows[:limit]}
