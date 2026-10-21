"""Map view, rendering (vision) and print layouts."""
from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsLayoutExporter,
                       QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutItemPage,
                       QgsLayoutItemPicture, QgsLayoutItemScaleBar, QgsLayoutPoint, QgsLayoutSize,
                       QgsMapRendererParallelJob, QgsMapSettings, QgsPrintLayout, QgsRectangle, QgsUnitTypes)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QSize
from qgis.PyQt.QtGui import QColor, QFont, QImage

from ..core.context import ordered_layers
from ._qgis import output_path, parse_crs, rect_list, resolve_layer
from .registry import Risk, ToolContext, ToolError, ToolResult, tool

MAX_SIDE = 1600


def _dest_crs(ctx: ToolContext) -> QgsCoordinateReferenceSystem:
    if ctx.iface is not None:
        return ctx.iface.mapCanvas().mapSettings().destinationCrs()
    crs = ctx.project.crs()
    if crs.isValid():
        return crs
    for layer, _visible, _group in ordered_layers(ctx.project):
        if layer.crs().isValid():
            return layer.crs()
    return QgsCoordinateReferenceSystem("EPSG:3857")


def _visible_layers(ctx: ToolContext) -> List[Any]:
    return [layer for layer, visible, _group in ordered_layers(ctx.project) if visible]


def _full_extent(ctx: ToolContext, layers: List[Any]) -> QgsRectangle:
    dest = _dest_crs(ctx)
    extent = QgsRectangle()
    extent.setMinimal()
    for layer in layers:
        layer_extent = layer.extent()
        if layer_extent.isEmpty() or "type=xyz" in layer.source():
            continue
        box = QgsCoordinateTransform(layer.crs(), dest, ctx.project).transformBoundingBox(layer_extent)
        extent.combineExtentWith(box)
    if extent.isEmpty():
        raise ToolError("Nothing to show: no visible layer with an extent.")
    extent.scale(1.05)
    return extent


def _current_extent(ctx: ToolContext, layers: List[Any]) -> QgsRectangle:
    if ctx.iface is not None:
        return ctx.iface.mapCanvas().extent()
    stored = ctx.state.get("map_extent")
    if stored:
        return QgsRectangle(*stored)
    return _full_extent(ctx, layers)


def _extent_from_args(ctx: ToolContext, extent: Optional[List[float]], extent_crs: str) -> Optional[QgsRectangle]:
    if not extent:
        return None
    if len(extent) != 4:
        raise ToolError("extent must be [xmin, ymin, xmax, ymax].")
    rect = QgsRectangle(*[float(v) for v in extent])
    if extent_crs:
        rect = QgsCoordinateTransform(parse_crs(extent_crs), _dest_crs(ctx), ctx.project).transformBoundingBox(rect)
    return rect


def render_image(ctx: ToolContext, width: int, height: int, extent: Optional[QgsRectangle] = None,
                 layer_refs: Optional[List[str]] = None, dpi: int = 96) -> QImage:
    layers = [resolve_layer(ctx, ref) for ref in layer_refs] if layer_refs else _visible_layers(ctx)
    if not layers:
        raise ToolError("No visible layers to render.")
    settings = QgsMapSettings()
    settings.setLayers(layers)
    settings.setDestinationCrs(_dest_crs(ctx))
    settings.setTransformContext(ctx.project.transformContext())
    settings.setOutputSize(QSize(int(width), int(height)))
    settings.setOutputDpi(dpi)
    settings.setBackgroundColor(QColor(255, 255, 255))
    settings.setExtent(extent or _current_extent(ctx, layers))
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    return job.renderedImage()


def png_base64(image: QImage) -> str:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return base64.b64encode(bytes(data)).decode("ascii")


@tool("get_map_view", "Current map view: extent, CRS and scale.", {}, category="map")
def get_map_view(ctx: ToolContext) -> Dict[str, Any]:
    layers = _visible_layers(ctx)
    extent = _current_extent(ctx, layers) if layers else QgsRectangle()
    info: Dict[str, Any] = {"crs": _dest_crs(ctx).authid(), "extent": rect_list(extent)}
    if ctx.iface is not None:
        info["scale"] = round(ctx.iface.mapCanvas().scale())
    return info


@tool("set_map_view", "Zoom/pan the map: to a layer (or its selected features), to an extent, or to a centre "
      "point at a scale.",
      {"layer": {"type": "string", "description": "Zoom to this layer."},
       "selected_only": {"type": "boolean", "description": "Zoom to the layer's selected features."},
       "extent": {"type": "array", "items": {"type": "number"}, "description": "[xmin, ymin, xmax, ymax]"},
       "extent_crs": {"type": "string", "description": "CRS of extent/center (default: map CRS)."},
       "center": {"type": "array", "items": {"type": "number"}, "description": "[x, y] centre point."},
       "scale": {"type": "number", "description": "Map scale denominator for center, e.g. 25000."}},
      risk=Risk.WRITE, category="map")
def set_map_view(ctx: ToolContext, layer: str = "", selected_only: bool = False,
                 extent: Optional[List[float]] = None, extent_crs: str = "", center: Optional[List[float]] = None,
                 scale: Optional[float] = None) -> Dict[str, Any]:
    dest = _dest_crs(ctx)
    rect: Optional[QgsRectangle] = None
    if layer:
        lyr = resolve_layer(ctx, layer)
        box = lyr.boundingBoxOfSelected() if selected_only and hasattr(lyr, "boundingBoxOfSelected") else lyr.extent()
        if box.isEmpty():
            raise ToolError(f"'{lyr.name()}' has an empty extent{' selection' if selected_only else ''}.")
        rect = QgsCoordinateTransform(lyr.crs(), dest, ctx.project).transformBoundingBox(box)
        rect.scale(1.1)
    elif extent:
        rect = _extent_from_args(ctx, extent, extent_crs)
    elif center:
        from qgis.core import QgsPointXY
        point = QgsPointXY(float(center[0]), float(center[1]))
        if extent_crs:
            point = QgsCoordinateTransform(parse_crs(extent_crs), dest, ctx.project).transform(point)
        denominator = float(scale or 25000)
        if ctx.iface is not None:
            canvas = ctx.iface.mapCanvas()
            canvas.setCenter(point)
            canvas.zoomScale(denominator)
            canvas.refresh()
            return {"extent": rect_list(canvas.extent()), "scale": round(canvas.scale())}
        width_px, height_px = ctx.canvas_size
        metres_per_px = denominator * 0.0254 / 96.0
        if dest.isGeographic():
            metres_per_px /= 111_320.0
        half_w, half_h = width_px * metres_per_px / 2, height_px * metres_per_px / 2
        rect = QgsRectangle(point.x() - half_w, point.y() - half_h, point.x() + half_w, point.y() + half_h)
    if rect is None:
        raise ToolError("Give a layer, an extent or a center.")
    if ctx.iface is not None:
        canvas = ctx.iface.mapCanvas()
        canvas.setExtent(rect)
        canvas.refresh()
        return {"extent": rect_list(canvas.extent()), "scale": round(canvas.scale()), "crs": dest.authid()}
    ctx.state["map_extent"] = rect_list(rect, 9)
    return {"extent": rect_list(rect), "crs": dest.authid()}


@tool("capture_map_image", "Render the current map view (or a given extent/layers) to an image that you can look "
      "at, to check styling, labels and spatial patterns visually. Costs image tokens; use sparingly.",
      {"width": {"type": "integer", "description": f"Pixels (default 1024, max {MAX_SIDE})."},
       "height": {"type": "integer", "description": "Pixels (default 768)."},
       "extent": {"type": "array", "items": {"type": "number"}, "description": "Optional [xmin, ymin, xmax, ymax]."},
       "extent_crs": {"type": "string"},
       "layers": {"type": "array", "items": {"type": "string"}, "description": "Optional layers (default visible)."}},
      category="map")
def capture_map_image(ctx: ToolContext, width: int = 1024, height: int = 768, extent: Optional[List[float]] = None,
                      extent_crs: str = "", layers: Optional[List[str]] = None) -> ToolResult:
    width = max(64, min(int(width), MAX_SIDE))
    height = max(64, min(int(height), MAX_SIDE))
    rect = _extent_from_args(ctx, extent, extent_crs)
    image = render_image(ctx, width, height, rect, layers)
    used = rect or _current_extent(ctx, _visible_layers(ctx))
    return ToolResult(text=f"Map image {width}x{height}px, extent {rect_list(used)} ({_dest_crs(ctx).authid()}).",
                      images=[("image/png", png_base64(image))])


@tool("export_map_image", "Save a rendered map image (PNG/JPG) of the current view or an extent to a file.",
      {"filename": {"type": "string"},
       "width": {"type": "integer", "description": "Default 1600."},
       "height": {"type": "integer", "description": "Default 1200."},
       "extent": {"type": "array", "items": {"type": "number"}},
       "extent_crs": {"type": "string"},
       "dpi": {"type": "integer", "description": "Default 96."},
       "overwrite": {"type": "boolean"}},
      required=["filename"], risk=Risk.WRITE, category="map", tier="extended")
def export_map_image(ctx: ToolContext, filename: str, width: int = 1600, height: int = 1200,
                     extent: Optional[List[float]] = None, extent_crs: str = "", dpi: int = 96,
                     overwrite: bool = False) -> Dict[str, Any]:
    if not filename.lower().endswith((".png", ".jpg", ".jpeg")):
        filename += ".png"
    path = output_path(ctx, filename, overwrite)
    image = render_image(ctx, min(int(width), 8000), min(int(height), 8000), _extent_from_args(ctx, extent, extent_crs),
                         dpi=int(dpi))
    if not image.save(path):
        raise ToolError(f"Could not save {path}")
    return {"path": path, "size": [image.width(), image.height()]}


def _set_label_font(label: QgsLayoutItemLabel, size: float, bold: bool = False) -> None:
    font = QFont()
    font.setPointSizeF(size)
    font.setBold(bold)
    if hasattr(label, "setTextFormat"):  # QGIS >= 3.24
        from qgis.core import QgsTextFormat
        fmt = QgsTextFormat.fromQFont(font)
        fmt.setSize(size)
        label.setTextFormat(fmt)
    else:
        label.setFont(font)


_PAGE_SIZES = ("A5", "A4", "A3", "A2", "A1", "A0", "Letter", "Legal", "Tabloid")


@tool("create_print_layout", "Create a print layout with the map, a title, legend, scale bar and north arrow.",
      {"name": {"type": "string", "description": "Layout name."},
       "title": {"type": "string"},
       "subtitle": {"type": "string", "description": "Optional second line, e.g. data sources."},
       "page_size": {"type": "string", "enum": list(_PAGE_SIZES)},
       "orientation": {"type": "string", "enum": ["landscape", "portrait"]},
       "extent": {"type": "array", "items": {"type": "number"}, "description": "Map extent (default current view)."},
       "extent_crs": {"type": "string"},
       "legend": {"type": "boolean"}, "scale_bar": {"type": "boolean"}, "north_arrow": {"type": "boolean"}},
      required=["name"], risk=Risk.WRITE, category="layout")
def create_print_layout(ctx: ToolContext, name: str, title: str = "", subtitle: str = "", page_size: str = "A4",
                        orientation: str = "landscape", extent: Optional[List[float]] = None, extent_crs: str = "",
                        legend: bool = True, scale_bar: bool = True, north_arrow: bool = True) -> Dict[str, Any]:
    manager = ctx.project.layoutManager()
    if manager.layoutByName(name) is not None:
        raise ToolError(f"A layout named '{name}' already exists.")
    layout = QgsPrintLayout(ctx.project)
    layout.initializeDefaults()
    layout.setName(name)
    page = layout.pageCollection().page(0)
    page.setPageSize(page_size, QgsLayoutItemPage.Landscape if orientation == "landscape" else QgsLayoutItemPage.Portrait)
    page_w, page_h = page.pageSize().width(), page.pageSize().height()
    mm = QgsUnitTypes.LayoutMillimeters
    margin, top = 10.0, 22.0 if title else 10.0
    legend_w = 55.0 if legend else 0.0
    map_item = QgsLayoutItemMap(layout)
    map_item.setFrameEnabled(True)
    map_item.attemptMove(QgsLayoutPoint(margin, top, mm))
    map_item.attemptResize(QgsLayoutSize(page_w - 2 * margin - legend_w - (5 if legend else 0), page_h - top - margin, mm))
    map_item.setCrs(_dest_crs(ctx))
    layers = _visible_layers(ctx)
    map_item.setExtent(_extent_from_args(ctx, extent, extent_crs) or _current_extent(ctx, layers))
    layout.addLayoutItem(map_item)
    if title:
        label = QgsLayoutItemLabel(layout)
        label.setText(title)
        _set_label_font(label, 20, bold=True)
        label.attemptMove(QgsLayoutPoint(margin, 4, mm))
        label.attemptResize(QgsLayoutSize(page_w - 2 * margin, 11, mm))
        layout.addLayoutItem(label)
        if subtitle:
            sub = QgsLayoutItemLabel(layout)
            sub.setText(subtitle)
            _set_label_font(sub, 10)
            sub.attemptMove(QgsLayoutPoint(margin, 14.5, mm))
            sub.attemptResize(QgsLayoutSize(page_w - 2 * margin, 6, mm))
            layout.addLayoutItem(sub)
    if legend:
        item = QgsLayoutItemLegend(layout)
        item.setLinkedMap(map_item)
        item.setTitle("Legend")
        item.attemptMove(QgsLayoutPoint(page_w - margin - legend_w, top, mm))
        item.attemptResize(QgsLayoutSize(legend_w, page_h - top - margin, mm))
        item.setResizeToContents(False)
        layout.addLayoutItem(item)
    if scale_bar:
        bar = QgsLayoutItemScaleBar(layout)
        bar.setStyle("Single Box")
        bar.setLinkedMap(map_item)
        bar.applyDefaultSize()
        bar.attemptMove(QgsLayoutPoint(margin + 4, page_h - margin - 14, mm))
        layout.addLayoutItem(bar)
    if north_arrow:
        arrow = QgsLayoutItemPicture(layout)
        arrow.setPicturePath(":/images/north_arrows/layout_default_north_arrow.svg")
        arrow.attemptResize(QgsLayoutSize(12, 16, mm))
        map_right = page_w - margin - legend_w - (5 if legend else 0)
        arrow.attemptMove(QgsLayoutPoint(map_right - 16, top + 4, mm))
        layout.addLayoutItem(arrow)
    manager.addLayout(layout)
    return {"layout": name, "page": f"{page_size} {orientation}", "items": len(layout.items())}


@tool("export_print_layout", "Export a print layout to PDF or PNG in the output folder; optionally return a "
      "preview image so you can check the result visually.",
      {"name": {"type": "string", "description": "Layout name."},
       "filename": {"type": "string"},
       "format": {"type": "string", "enum": ["pdf", "png"]},
       "dpi": {"type": "integer", "description": "Default 200."},
       "preview": {"type": "boolean", "description": "Also return a small preview image (default false)."},
       "overwrite": {"type": "boolean"}},
      required=["name", "filename"], risk=Risk.WRITE, category="layout")
def export_print_layout(ctx: ToolContext, name: str, filename: str, format: str = "pdf", dpi: int = 200,  # noqa: A002
                        preview: bool = False, overwrite: bool = False) -> ToolResult:
    layout = ctx.project.layoutManager().layoutByName(name)
    if layout is None:
        names = [lay.name() for lay in ctx.project.layoutManager().layouts()]
        raise ToolError(f"No layout '{name}'. Layouts: {', '.join(names) or 'none'}.")
    ext = "." + format.lower()
    if not filename.lower().endswith(ext):
        filename += ext
    path = output_path(ctx, filename, overwrite)
    exporter = QgsLayoutExporter(layout)
    if format == "pdf":
        settings = QgsLayoutExporter.PdfExportSettings()
        settings.dpi = int(dpi)
        code = exporter.exportToPdf(path, settings)
    else:
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = int(dpi)
        code = exporter.exportToImage(path, settings)
    if code != QgsLayoutExporter.Success:
        raise ToolError(f"Export failed (code {int(code)}): {exporter.errorMessage() if hasattr(exporter, 'errorMessage') else ''}")
    images = []
    if preview:
        image = exporter.renderPageToImage(0, QSize(1200, 0), 72)
        images.append(("image/png", png_base64(image)))
    return ToolResult(data={"layout": name, "path": path, "exists": os.path.exists(path)}, images=images)
