"""Read-only data access: features, statistics, expressions and measurements."""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, List, Optional

from qgis.core import (QgsCoordinateTransform, QgsDistanceArea, QgsExpression, QgsExpressionContext,
                       QgsExpressionContextUtils, QgsFeatureRequest, QgsPointXY, QgsVectorLayer)

from ._qgis import geometry_kind, json_value, parse_crs, require_field, resolve_vector
from .registry import Risk, ToolContext, ToolError, tool

MAX_ROWS = 200


def _request(layer: QgsVectorLayer, where: Optional[str], selected_only: bool) -> QgsFeatureRequest:
    request = QgsFeatureRequest()
    if where:
        expression = QgsExpression(where)
        if expression.hasParserError():
            raise ToolError(f"Invalid filter expression: {expression.parserErrorString()}")
        request.setFilterExpression(where)
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        request.setExpressionContext(context)
    if selected_only:
        request.setFilterFids(layer.selectedFeatureIds())
    return request


def _check_expression_errors(layer: QgsVectorLayer, where: Optional[str]) -> None:
    if not where:
        return
    expression = QgsExpression(where)
    context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    expression.prepare(context)
    if expression.hasEvalError():
        raise ToolError(f"Filter expression error: {expression.evalErrorString()}")


@tool("get_features", "Read feature attributes (and optionally geometry) from a vector layer, with an optional "
      "QGIS expression filter, field subset, ordering and limit. Use for inspecting records; use "
      "field_statistics or count_features for aggregates instead of reading many rows.",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "where": {"type": "string", "description": "QGIS expression filter, e.g. \"type\" = 'school'."},
       "fields": {"type": "array", "items": {"type": "string"}, "description": "Fields to return (default all)."},
       "limit": {"type": "integer", "description": f"Maximum rows (default 20, max {MAX_ROWS})."},
       "order_by": {"type": "string", "description": "Field or expression to sort by."},
       "descending": {"type": "boolean", "description": "Sort descending (default false)."},
       "geometry": {"type": "string", "enum": ["none", "wkt", "centroid", "bbox"],
                    "description": "Geometry representation (default none). WKT is truncated for large shapes."},
       "selected_only": {"type": "boolean", "description": "Only selected features."}},
      required=["layer"], category="query")
def get_features(ctx: ToolContext, layer: str, where: str = "", fields: Optional[List[str]] = None, limit: int = 20,
                 order_by: str = "", descending: bool = False, geometry: str = "none",
                 selected_only: bool = False) -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    names = [require_field(lyr, f) for f in fields] if fields else [f.name() for f in lyr.fields()]
    limit = max(1, min(int(limit), MAX_ROWS))
    request = _request(lyr, where, selected_only)
    if order_by:
        request.addOrderBy(f'"{order_by}"' if lyr.fields().indexOf(order_by) >= 0 else order_by, not descending)
    request.setLimit(limit)
    rows = []
    for feature in lyr.getFeatures(request):
        row: Dict[str, Any] = {"fid": feature.id()}
        for name in names:
            row[name] = json_value(feature[name])
        geom = feature.geometry()
        if geometry != "none" and geom is not None and not geom.isNull():
            if geometry == "wkt":
                wkt = geom.asWkt(6)
                row["wkt"] = wkt if len(wkt) <= 2000 else wkt[:2000] + "...(truncated)"
            elif geometry == "centroid":
                point = geom.centroid().asPoint()
                row["centroid"] = [round(point.x(), 6), round(point.y(), 6)]
            elif geometry == "bbox":
                box = geom.boundingBox()
                row["bbox"] = [round(box.xMinimum(), 6), round(box.yMinimum(), 6),
                               round(box.xMaximum(), 6), round(box.yMaximum(), 6)]
        rows.append(row)
    _check_expression_errors(lyr, where)
    total = lyr.featureCount() if not where and not selected_only else None
    result: Dict[str, Any] = {"layer": lyr.name(), "crs": lyr.crs().authid(), "returned": len(rows), "rows": rows}
    if total is not None and total > len(rows):
        result["note"] = f"{total} features in total; showing {len(rows)}."
    return result


@tool("count_features", "Count features in a vector layer, optionally matching a QGIS expression.",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "where": {"type": "string", "description": "Optional QGIS expression filter."},
       "selected_only": {"type": "boolean", "description": "Count only selected features."}},
      required=["layer"], category="query")
def count_features(ctx: ToolContext, layer: str, where: str = "", selected_only: bool = False) -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    if not where and not selected_only:
        return {"layer": lyr.name(), "count": lyr.featureCount()}
    request = _request(lyr, where, selected_only)
    request.setFlags(QgsFeatureRequest.NoGeometry)
    request.setNoAttributes()
    count = sum(1 for _ in lyr.getFeatures(request))
    _check_expression_errors(lyr, where)
    return {"layer": lyr.name(), "where": where or None, "count": count}


@tool("field_statistics", "Statistics of a field or expression over a vector layer (count, sum, mean, min, max, "
      "median, stddev for numbers; distinct count and most common values for text), optionally filtered and "
      "grouped by another field.",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "field": {"type": "string", "description": "Field name or QGIS expression, e.g. \"population\" or $area."},
       "where": {"type": "string", "description": "Optional QGIS expression filter."},
       "group_by": {"type": "string", "description": "Optional field to group by (max 50 groups returned)."}},
      required=["layer", "field"], category="query")
def field_statistics(ctx: ToolContext, layer: str, field: str, where: str = "",
                     group_by: str = "") -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    is_field = lyr.fields().indexOf(field) >= 0
    expression = None
    context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
    if not is_field:
        lowered = {f.name().lower(): f.name() for f in lyr.fields()}
        if field.lower() in lowered:
            field, is_field = lowered[field.lower()], True
        else:
            expression = QgsExpression(field)
            if expression.hasParserError():
                raise ToolError(f"'{field}' is neither a field nor a valid expression: {expression.parserErrorString()}")
            expression.prepare(context)
    group_field = require_field(lyr, group_by) if group_by else None
    groups: Dict[Any, List[Any]] = {}
    for feature in lyr.getFeatures(_request(lyr, where, False)):
        if expression is not None:
            context.setFeature(feature)
            value = json_value(expression.evaluate(context))
        else:
            value = json_value(feature[field])
        key = json_value(feature[group_field]) if group_field else "__all__"
        groups.setdefault(key, []).append(value)
    if expression is not None and expression.hasEvalError():
        raise ToolError(f"Expression error: {expression.evalErrorString()}")
    _check_expression_errors(lyr, where)
    summaries = {str(k): _summary(v) for k, v in list(groups.items())[:50]}
    if not group_field:
        return {"layer": lyr.name(), "field": field, "where": where or None, **summaries.get("__all__", _summary([]))}
    return {"layer": lyr.name(), "field": field, "group_by": group_field, "groups": summaries,
            "group_count": len(groups)}


def _summary(values: List[Any]) -> Dict[str, Any]:
    present = [v for v in values if v is not None]
    numeric = [float(v) for v in present if isinstance(v, (int, float)) and not isinstance(v, bool)
               and not math.isnan(float(v))]
    out: Dict[str, Any] = {"count": len(values), "null_count": len(values) - len(present)}
    if numeric and len(numeric) == len(present):
        ordered = sorted(numeric)
        n = len(ordered)
        mean = sum(ordered) / n
        median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
        var = sum((x - mean) ** 2 for x in ordered) / n
        out.update(sum=round(sum(ordered), 6), mean=round(mean, 6), min=ordered[0], max=ordered[-1],
                   median=round(median, 6), stddev=round(math.sqrt(var), 6))
    else:
        counts = Counter(str(v) for v in present)
        out.update(distinct=len(counts), most_common=[{"value": k, "count": c} for k, c in counts.most_common(15)])
    return out


@tool("unique_values", "Distinct values of a field with their counts (most frequent first).",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "field": {"type": "string", "description": "Field name."},
       "where": {"type": "string", "description": "Optional QGIS expression filter."},
       "limit": {"type": "integer", "description": "Maximum distinct values to return (default 50)."}},
      required=["layer", "field"], category="query")
def unique_values(ctx: ToolContext, layer: str, field: str, where: str = "", limit: int = 50) -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    name = require_field(lyr, field)
    request = _request(lyr, where, False)
    request.setFlags(QgsFeatureRequest.NoGeometry)
    request.setSubsetOfAttributes([name], lyr.fields())
    counts = Counter(json_value(f[name]) for f in lyr.getFeatures(request))
    _check_expression_errors(lyr, where)
    items = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    limit = max(1, min(int(limit), 500))
    return {"layer": lyr.name(), "field": name, "distinct": len(items),
            "values": [{"value": v, "count": c} for v, c in items[:limit]]}


@tool("select_features", "Select features in a vector layer with a QGIS expression (highlights them on the map and "
      "lets later steps use 'selected only').",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "where": {"type": "string", "description": "QGIS expression; empty clears the selection."},
       "mode": {"type": "string", "enum": ["new", "add", "remove", "intersect"],
                "description": "Selection behaviour (default new)."}},
      required=["layer"], risk=Risk.WRITE, category="query")
def select_features(ctx: ToolContext, layer: str, where: str = "", mode: str = "new") -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    if not where:
        lyr.removeSelection()
        return {"layer": lyr.name(), "selected": 0}
    request = _request(lyr, where, False)
    request.setFlags(QgsFeatureRequest.NoGeometry)
    request.setNoAttributes()
    ids = {f.id() for f in lyr.getFeatures(request)}
    _check_expression_errors(lyr, where)
    current = set(lyr.selectedFeatureIds())
    if mode == "add":
        ids = current | ids
    elif mode == "remove":
        ids = current - ids
    elif mode == "intersect":
        ids = current & ids
    lyr.selectByIds(list(ids))
    return {"layer": lyr.name(), "selected": len(ids)}


@tool("evaluate_expression", "Evaluate a QGIS expression once in the project context, e.g. aggregates across layers: "
      "aggregate('parcels','sum',\"area_m2\"), or geometry maths on WKT. Optionally evaluate it per feature of a "
      "layer (returns up to 50 values).",
      {"expression": {"type": "string", "description": "QGIS expression."},
       "layer": {"type": "string", "description": "Optional layer to evaluate against, per feature."},
       "where": {"type": "string", "description": "Optional filter when a layer is given."}},
      required=["expression"], category="query")
def evaluate_expression(ctx: ToolContext, expression: str, layer: str = "", where: str = "") -> Dict[str, Any]:
    expr = QgsExpression(expression)
    if expr.hasParserError():
        raise ToolError(f"Parser error: {expr.parserErrorString()}")
    if not layer:
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(None))
        value = expr.evaluate(context)
        if expr.hasEvalError():
            raise ToolError(f"Evaluation error: {expr.evalErrorString()}")
        return {"expression": expression, "value": json_value(value)}
    lyr = resolve_vector(ctx, layer)
    context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
    expr.prepare(context)
    values = []
    request = _request(lyr, where, False)
    request.setLimit(50)
    for feature in lyr.getFeatures(request):
        context.setFeature(feature)
        values.append({"fid": feature.id(), "value": json_value(expr.evaluate(context))})
        if expr.hasEvalError():
            raise ToolError(f"Evaluation error: {expr.evalErrorString()}")
    return {"expression": expression, "layer": lyr.name(), "values": values}


@tool("measure_geometries", "Measure features on the ellipsoid (WGS84), independent of the layer CRS: total and "
      "per-feature area (polygons), length (lines) or perimeter. Units: square metres / metres, plus km and "
      "hectares. Reliable for layers in geographic CRS.",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "where": {"type": "string", "description": "Optional QGIS expression filter."},
       "per_feature": {"type": "boolean", "description": "Return per-feature values (max 100, default false)."},
       "label_field": {"type": "string", "description": "Field used to label per-feature values."}},
      required=["layer"], category="query")
def measure_geometries(ctx: ToolContext, layer: str, where: str = "", per_feature: bool = False,
                       label_field: str = "") -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    kind = geometry_kind(lyr)
    if kind not in ("line", "polygon"):
        raise ToolError(f"'{lyr.name()}' has {kind} geometry; only lines and polygons can be measured.")
    calculator = QgsDistanceArea()
    calculator.setSourceCrs(lyr.crs(), ctx.project.transformContext())
    calculator.setEllipsoid("EPSG:7030")
    label = require_field(lyr, label_field) if label_field else None
    total = 0.0
    perimeter_total = 0.0
    rows = []
    count = 0
    for feature in lyr.getFeatures(_request(lyr, where, False)):
        geom = feature.geometry()
        if geom is None or geom.isNull():
            continue
        count += 1
        if kind == "polygon":
            value = calculator.measureArea(geom)
            perimeter_total += calculator.measurePerimeter(geom)
        else:
            value = calculator.measureLength(geom)
        total += value
        if per_feature and len(rows) < 100:
            rows.append({"fid": feature.id(), **({"label": json_value(feature[label])} if label else {}),
                         ("area_m2" if kind == "polygon" else "length_m"): round(value, 3)})
    _check_expression_errors(lyr, where)
    out: Dict[str, Any] = {"layer": lyr.name(), "features_measured": count, "method": "ellipsoidal (WGS84)"}
    if kind == "polygon":
        out.update(total_area_m2=round(total, 3), total_area_ha=round(total / 10_000, 4),
                   total_area_km2=round(total / 1_000_000, 6), total_perimeter_m=round(perimeter_total, 3))
    else:
        out.update(total_length_m=round(total, 3), total_length_km=round(total / 1000, 6))
    if per_feature:
        out["features"] = rows
    return out


@tool("transform_coordinates", "Transform point coordinates between coordinate reference systems.",
      {"points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                  "description": "List of [x, y] pairs (x = longitude/easting)."},
       "source_crs": {"type": "string", "description": "e.g. EPSG:4326"},
       "target_crs": {"type": "string", "description": "e.g. EPSG:25832"}},
      required=["points", "source_crs", "target_crs"], category="query", tier="extended")
def transform_coordinates(ctx: ToolContext, points: List[List[float]], source_crs: str,
                          target_crs: str) -> Dict[str, Any]:
    transform = QgsCoordinateTransform(parse_crs(source_crs), parse_crs(target_crs), ctx.project)
    out = []
    for pair in points[:500]:
        if len(pair) < 2:
            raise ToolError("Each point needs [x, y].")
        point = transform.transform(QgsPointXY(float(pair[0]), float(pair[1])))
        out.append([round(point.x(), 6), round(point.y(), 6)])
    return {"source_crs": source_crs, "target_crs": target_crs, "points": out}


@tool("geodesic_distance", "Ellipsoidal (WGS84) distance in metres between two points given in any CRS.",
      {"from_point": {"type": "array", "items": {"type": "number"}, "description": "[x, y]"},
       "to_point": {"type": "array", "items": {"type": "number"}, "description": "[x, y]"},
       "crs": {"type": "string", "description": "CRS of the coordinates (default EPSG:4326)."}},
      required=["from_point", "to_point"], category="query", tier="extended")
def geodesic_distance(ctx: ToolContext, from_point: List[float], to_point: List[float],
                      crs: str = "EPSG:4326") -> Dict[str, Any]:
    calculator = QgsDistanceArea()
    calculator.setSourceCrs(parse_crs(crs), ctx.project.transformContext())
    calculator.setEllipsoid("EPSG:7030")
    a = QgsPointXY(float(from_point[0]), float(from_point[1]))
    b = QgsPointXY(float(to_point[0]), float(to_point[1]))
    metres = calculator.measureLine(a, b)
    return {"distance_m": round(metres, 3), "distance_km": round(metres / 1000, 6)}

