"""Symbology and labelling tools."""
from __future__ import annotations

from typing import Any, Dict, Optional

from qgis.core import (QgsCategorizedSymbolRenderer, QgsClassificationEqualInterval, QgsClassificationJenks,
                       QgsClassificationPrettyBreaks, QgsClassificationQuantile,
                       QgsClassificationStandardDeviation, QgsColorRampShader, QgsContrastEnhancement,
                       QgsExpression, QgsGradientColorRamp, QgsGraduatedSymbolRenderer, QgsHeatmapRenderer,
                       QgsHillshadeRenderer, QgsMultiBandColorRenderer, QgsPalLayerSettings,
                       QgsRasterShader, QgsRendererCategory, QgsSingleBandGrayRenderer,
                       QgsSingleBandPseudoColorRenderer, QgsSingleSymbolRenderer, QgsStyle, QgsSymbol,
                       QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor

from ._qgis import band_stats, geometry_kind, json_value, resolve_layer, resolve_raster, resolve_vector
from .registry import Risk, ToolContext, ToolError, tool

RAMP_HINT = ("QGIS colour ramp name such as Viridis, Magma, Spectral, RdYlGn, Blues, Greens, Reds, YlOrRd, "
             "Turbo; default Viridis.")


def parse_color(value: str) -> QColor:
    text = (value or "").strip()
    if "," in text:
        parts = [int(float(p)) for p in text.replace("rgba(", "").replace("rgb(", "").rstrip(")").split(",")]
        color = QColor(*parts[:4])
    else:
        color = QColor(text)
    if not color.isValid():
        raise ToolError(f"Invalid colour '{value}'. Use #RRGGBB, a CSS name (e.g. 'steelblue') or 'r,g,b'.")
    return color


def _ramp(name: Optional[str], invert: bool = False):
    style = QgsStyle.defaultStyle()
    ramp = None
    wanted = (name or "Viridis").strip()
    for candidate in style.colorRampNames():
        if candidate.lower() == wanted.lower():
            ramp = style.colorRamp(candidate)
            break
    if ramp is None:
        ramp = style.colorRamp("Viridis") or QgsGradientColorRamp(QColor("#440154"), QColor("#fde725"))
    if invert:
        ramp.invert()
    return ramp


def _symbol_for(layer: QgsVectorLayer) -> QgsSymbol:
    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    if symbol is None:
        raise ToolError(f"Layer '{layer.name()}' has no geometry to symbolise.")
    return symbol


def _finish(layer) -> None:
    layer.triggerRepaint()
    try:
        layer.emitStyleChanged()
    except AttributeError:
        pass


@tool("set_single_symbol_style", "Give a vector layer one uniform symbol (fill/line/marker colour, outline, size).",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "color": {"type": "string", "description": "Fill/line/marker colour, e.g. #1f78b4 or 'orange'."},
       "outline_color": {"type": "string", "description": "Outline colour for polygons/points."},
       "outline_width": {"type": "number", "description": "Outline width in mm (default 0.26)."},
       "size": {"type": "number", "description": "Marker size (points) or line width (lines) in mm."},
       "opacity": {"type": "number", "description": "0-1, default 1."}},
      required=["layer", "color"], risk=Risk.WRITE, category="styling")
def set_single_symbol_style(ctx: ToolContext, layer: str, color: str, outline_color: str = "",
                            outline_width: Optional[float] = None, size: Optional[float] = None,
                            opacity: float = 1.0) -> str:
    lyr = resolve_vector(ctx, layer)
    symbol = _symbol_for(lyr)
    symbol.setColor(parse_color(color))
    symbol.setOpacity(max(0.0, min(1.0, float(opacity))))
    kind = geometry_kind(lyr)
    base = symbol.symbolLayer(0)
    if outline_color and hasattr(base, "setStrokeColor"):
        base.setStrokeColor(parse_color(outline_color))
    if outline_width is not None and hasattr(base, "setStrokeWidth"):
        base.setStrokeWidth(float(outline_width))
    if size is not None:
        if kind == "point":
            symbol.setSize(float(size))
        elif kind == "line":
            symbol.setWidth(float(size))
    lyr.setRenderer(QgsSingleSymbolRenderer(symbol))
    _finish(lyr)
    return f"Applied a single {kind} symbol ({color}) to '{lyr.name()}'."


@tool("set_categorized_style", "Colour a vector layer by the distinct values of a field (or expression), e.g. land "
      "use classes. Use explicit colors for meaningful categories, otherwise a colour ramp.",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "field": {"type": "string", "description": "Field name or QGIS expression."},
       "color_ramp": {"type": "string", "description": RAMP_HINT},
       "colors": {"type": "object", "description": "Optional value -> colour mapping, e.g. {\"forest\": \"#2b8a3e\"}."},
       "max_categories": {"type": "integer", "description": "Maximum categories (default 40)."}},
      required=["layer", "field"], risk=Risk.WRITE, category="styling")
def set_categorized_style(ctx: ToolContext, layer: str, field: str, color_ramp: str = "",
                          colors: Optional[Dict[str, str]] = None, max_categories: int = 40) -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    idx = lyr.fields().indexOf(field)
    if idx < 0:
        lowered = {f.name().lower(): f.name() for f in lyr.fields()}
        if field.lower() in lowered:
            field = lowered[field.lower()]
            idx = lyr.fields().indexOf(field)
    if idx >= 0:
        values = sorted(lyr.uniqueValues(idx), key=lambda v: (v is None, str(v)))
    else:
        expression = QgsExpression(field)
        if expression.hasParserError():
            raise ToolError(f"'{field}' is neither a field nor a valid expression.")
        from qgis.core import QgsExpressionContext, QgsExpressionContextUtils
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
        expression.prepare(context)
        found = set()
        for feature in lyr.getFeatures():
            context.setFeature(feature)
            found.add(expression.evaluate(context))
        values = sorted(found, key=lambda v: (v is None, str(v)))
    values = [v for v in values if json_value(v) is not None]
    if not values:
        raise ToolError(f"No values found for '{field}'.")
    max_categories = max(1, min(int(max_categories), 200))
    truncated = len(values) > max_categories
    values = values[:max_categories]
    ramp = _ramp(color_ramp)
    mapping = {str(k).lower(): v for k, v in (colors or {}).items()}
    categories = []
    for i, value in enumerate(values):
        symbol = _symbol_for(lyr)
        key = str(json_value(value))
        if key.lower() in mapping:
            symbol.setColor(parse_color(mapping[key.lower()]))
        else:
            symbol.setColor(ramp.color(i / max(1, len(values) - 1)))
        categories.append(QgsRendererCategory(value, symbol, key))
    lyr.setRenderer(QgsCategorizedSymbolRenderer(field, categories))
    _finish(lyr)
    result: Dict[str, Any] = {"layer": lyr.name(), "field": field, "categories": len(categories)}
    if truncated:
        result["note"] = f"Only the first {max_categories} values were styled; others are not drawn."
    return result


_METHODS = {"jenks": QgsClassificationJenks, "quantile": QgsClassificationQuantile,
            "equal_interval": QgsClassificationEqualInterval, "pretty": QgsClassificationPrettyBreaks,
            "stddev": QgsClassificationStandardDeviation}


@tool("set_graduated_style", "Colour a vector layer by classes of a numeric field or expression (choropleth / "
      "graduated colours).",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "field": {"type": "string", "description": "Numeric field or QGIS expression, e.g. \"pop\" / $area."},
       "classes": {"type": "integer", "description": "Number of classes (default 5)."},
       "method": {"type": "string", "enum": list(_METHODS), "description": "Classification (default jenks)."},
       "color_ramp": {"type": "string", "description": RAMP_HINT},
       "invert_ramp": {"type": "boolean", "description": "Reverse the ramp."}},
      required=["layer", "field"], risk=Risk.WRITE, category="styling")
def set_graduated_style(ctx: ToolContext, layer: str, field: str, classes: int = 5, method: str = "jenks",
                        color_ramp: str = "", invert_ramp: bool = False) -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    lowered = {f.name().lower(): f.name() for f in lyr.fields()}
    if lyr.fields().indexOf(field) < 0 and field.lower() in lowered:
        field = lowered[field.lower()]
    renderer = QgsGraduatedSymbolRenderer(field)
    renderer.setSourceSymbol(_symbol_for(lyr))
    renderer.setSourceColorRamp(_ramp(color_ramp, invert_ramp))
    renderer.setClassificationMethod(_METHODS.get(method, QgsClassificationJenks)())
    renderer.updateClasses(lyr, max(2, min(int(classes), 20)))
    if not renderer.ranges():
        raise ToolError(f"Could not classify '{field}' - is it numeric and non-empty?")
    lyr.setRenderer(renderer)
    _finish(lyr)
    return {"layer": lyr.name(), "field": field, "method": method,
            "classes": [{"label": r.label(), "lower": round(r.lowerValue(), 6), "upper": round(r.upperValue(), 6)}
                        for r in renderer.ranges()]}


@tool("set_heatmap_style", "Render a point layer as a heatmap (density surface).",
      {"layer": {"type": "string", "description": "Point layer name or id."},
       "radius": {"type": "number", "description": "Radius in millimetres on screen (default 10)."},
       "color_ramp": {"type": "string", "description": RAMP_HINT},
       "weight_field": {"type": "string", "description": "Optional numeric weight field."}},
      required=["layer"], risk=Risk.WRITE, category="styling", tier="extended")
def set_heatmap_style(ctx: ToolContext, layer: str, radius: float = 10.0, color_ramp: str = "",
                      weight_field: str = "") -> str:
    lyr = resolve_vector(ctx, layer)
    if geometry_kind(lyr) != "point":
        raise ToolError("Heatmaps need a point layer.")
    renderer = QgsHeatmapRenderer()
    renderer.setRadius(float(radius))
    renderer.setColorRamp(_ramp(color_ramp or "Magma"))
    if weight_field:
        renderer.setWeightExpression(f'"{weight_field}"')
    lyr.setRenderer(renderer)
    _finish(lyr)
    return f"Heatmap applied to '{lyr.name()}'."


@tool("set_raster_style", "Style a raster: 'pseudocolor' (single band with a colour ramp), 'grayscale', "
      "'hillshade' (for DEMs) or 'rgb' (multiband).",
      {"layer": {"type": "string", "description": "Raster layer name or id."},
       "style": {"type": "string", "enum": ["pseudocolor", "grayscale", "hillshade", "rgb"]},
       "band": {"type": "integer", "description": "Band for single-band styles (default 1)."},
       "color_ramp": {"type": "string", "description": RAMP_HINT},
       "invert_ramp": {"type": "boolean"},
       "min_value": {"type": "number", "description": "Minimum value (default: band min)."},
       "max_value": {"type": "number", "description": "Maximum value (default: band max)."},
       "rgb_bands": {"type": "array", "items": {"type": "integer"}, "description": "[r, g, b] bands for rgb."},
       "opacity": {"type": "number", "description": "0-1."}},
      required=["layer", "style"], risk=Risk.WRITE, category="styling")
def set_raster_style(ctx: ToolContext, layer: str, style: str, band: int = 1, color_ramp: str = "",
                     invert_ramp: bool = False, min_value: Optional[float] = None,
                     max_value: Optional[float] = None, rgb_bands: Optional[list] = None,
                     opacity: Optional[float] = None) -> str:
    lyr = resolve_raster(ctx, layer)
    provider = lyr.dataProvider()
    if band < 1 or band > lyr.bandCount():
        raise ToolError(f"Band {band} does not exist (layer has {lyr.bandCount()}).")
    stats = band_stats(provider, band, lyr.extent())
    lo = stats.minimumValue if min_value is None else float(min_value)
    hi = stats.maximumValue if max_value is None else float(max_value)
    if style == "pseudocolor":
        ramp_shader = QgsColorRampShader(lo, hi)
        ramp_shader.setSourceColorRamp(_ramp(color_ramp, invert_ramp))
        ramp_shader.setColorRampType(QgsColorRampShader.Interpolated)
        ramp_shader.classifyColorRamp(10, band, lyr.extent(), provider)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp_shader)
        renderer = QgsSingleBandPseudoColorRenderer(provider, band, shader)
        renderer.setClassificationMin(lo)
        renderer.setClassificationMax(hi)
    elif style == "grayscale":
        renderer = QgsSingleBandGrayRenderer(provider, band)
        enhancement = QgsContrastEnhancement(provider.dataType(band))
        enhancement.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
        enhancement.setMinimumValue(lo)
        enhancement.setMaximumValue(hi)
        renderer.setContrastEnhancement(enhancement)
    elif style == "hillshade":
        renderer = QgsHillshadeRenderer(provider, band, 315.0, 45.0)
        renderer.setZFactor(1.0)
    elif style == "rgb":
        bands = rgb_bands or [1, 2, 3]
        if len(bands) != 3 or any(b < 1 or b > lyr.bandCount() for b in bands):
            raise ToolError(f"rgb_bands must be three bands between 1 and {lyr.bandCount()}.")
        renderer = QgsMultiBandColorRenderer(provider, *bands)
    else:
        raise ToolError("style must be pseudocolor, grayscale, hillshade or rgb.")
    lyr.setRenderer(renderer)
    if opacity is not None:
        lyr.renderer().setOpacity(max(0.0, min(1.0, float(opacity))))
    _finish(lyr)
    return f"Applied {style} style to '{lyr.name()}' (range {lo:.6g} to {hi:.6g})."


@tool("set_labels", "Turn on labels for a vector layer using a field or expression (or turn them off).",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "text": {"type": "string", "description": "Field name or QGIS expression, e.g. \"name\" || ' (' || \"pop\" || ')'."},
       "enabled": {"type": "boolean", "description": "false removes labels (default true)."},
       "size": {"type": "number", "description": "Font size in points (default 9)."},
       "color": {"type": "string", "description": "Text colour (default #222222)."},
       "buffer": {"type": "boolean", "description": "White halo for legibility (default true)."}},
      required=["layer"], risk=Risk.WRITE, category="styling")
def set_labels(ctx: ToolContext, layer: str, text: str = "", enabled: bool = True, size: float = 9.0,
               color: str = "#222222", buffer: bool = True) -> str:
    lyr = resolve_vector(ctx, layer)
    if not enabled:
        lyr.setLabelsEnabled(False)
        _finish(lyr)
        return f"Labels disabled on '{lyr.name()}'."
    if not text:
        raise ToolError("Provide the field or expression to label with.")
    settings = QgsPalLayerSettings()
    is_field = lyr.fields().indexOf(text) >= 0
    settings.fieldName = text
    settings.isExpression = not is_field
    if settings.isExpression and QgsExpression(text).hasParserError():
        raise ToolError(f"Invalid label expression: {QgsExpression(text).parserErrorString()}")
    fmt = QgsTextFormat()
    fmt.setSize(float(size))
    fmt.setColor(parse_color(color))
    if buffer:
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(0.8)
        buf.setColor(QColor("white"))
        fmt.setBuffer(buf)
    settings.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    lyr.setLabelsEnabled(True)
    _finish(lyr)
    return f"Labels on '{lyr.name()}' from {text}."


@tool("set_layer_opacity", "Set the overall opacity of any layer (0 transparent - 1 opaque).",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "opacity": {"type": "number", "description": "0-1."}},
      required=["layer", "opacity"], risk=Risk.WRITE, category="styling")
def set_layer_opacity(ctx: ToolContext, layer: str, opacity: float) -> str:
    lyr = resolve_layer(ctx, layer)
    value = max(0.0, min(1.0, float(opacity)))
    if hasattr(lyr, "setOpacity"):
        lyr.setOpacity(value)
    elif lyr.renderer() is not None:
        lyr.renderer().setOpacity(value)
    _finish(lyr)
    return f"Opacity of '{lyr.name()}' set to {value:.2f}."
