"""In-place data editing (requires approval in the default policy)."""
from __future__ import annotations

from typing import Any, Dict

from qgis.core import (QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeatureRequest,
                       QgsVectorDataProvider)

from ._qgis import json_value, make_field, resolve_vector
from .registry import Risk, ToolContext, ToolError, tool


def _can(layer, capability) -> bool:
    return bool(int(layer.dataProvider().capabilities()) & int(capability))


def _editable_or_fail(layer, capability, what: str) -> None:
    if not _can(layer, capability):
        raise ToolError(f"'{layer.name()}' ({layer.providerType()}) does not support {what}. Write a copy instead, "
                        "e.g. run_processing_algorithm('native:fieldcalculator', ...) or export_layer to GeoPackage.")


@tool("add_field", "Add an attribute field to a vector layer's data source.",
      {"layer": {"type": "string"},
       "name": {"type": "string"},
       "type": {"type": "string", "enum": ["string", "integer", "integer64", "double", "boolean", "date", "datetime"]},
       "length": {"type": "integer", "description": "Optional length (strings)."},
       "precision": {"type": "integer", "description": "Optional decimals (double)."}},
      required=["layer", "name", "type"], risk=Risk.DESTRUCTIVE, category="editing", tier="extended")
def add_field(ctx: ToolContext, layer: str, name: str, type: str, length: int = 0,  # noqa: A002
              precision: int = 0) -> str:
    lyr = resolve_vector(ctx, layer)
    if lyr.fields().indexOf(name) >= 0:
        raise ToolError(f"Field '{name}' already exists in '{lyr.name()}'.")
    _editable_or_fail(lyr, QgsVectorDataProvider.AddAttributes, "adding fields")
    if not lyr.dataProvider().addAttributes([make_field(name, type, length, precision)]):
        raise ToolError(f"Could not add field: {lyr.dataProvider().lastError()}")
    lyr.updateFields()
    return f"Added {type} field '{name}' to '{lyr.name()}'."


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer64"
    if isinstance(value, float):
        return "double"
    return "string"


@tool("calculate_field", "Field calculator: set a field (created if missing) from a QGIS expression for all or "
      "filtered features, editing the layer in place. To keep the source unchanged, prefer "
      "run_processing_algorithm('native:fieldcalculator') which writes a new layer.",
      {"layer": {"type": "string"},
       "field": {"type": "string", "description": "Target field (created if it does not exist)."},
       "expression": {"type": "string", "description": "QGIS expression, e.g. round($area / 10000, 2)."},
       "field_type": {"type": "string", "enum": ["string", "integer", "integer64", "double", "boolean", "date"],
                      "description": "Type for a new field (default: inferred)."},
       "where": {"type": "string", "description": "Optional filter; only matching features are updated."}},
      required=["layer", "field", "expression"], risk=Risk.DESTRUCTIVE, category="editing")
def calculate_field(ctx: ToolContext, layer: str, field: str, expression: str, field_type: str = "",
                    where: str = "") -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    expr = QgsExpression(expression)
    if expr.hasParserError():
        raise ToolError(f"Expression error: {expr.parserErrorString()}")
    context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
    expr.prepare(context)
    request = QgsFeatureRequest()
    if where:
        filt = QgsExpression(where)
        if filt.hasParserError():
            raise ToolError(f"Filter error: {filt.parserErrorString()}")
        request.setFilterExpression(where)
    values: Dict[int, Any] = {}
    errors = 0
    first_error = ""
    for feature in lyr.getFeatures(request):
        context.setFeature(feature)
        value = expr.evaluate(context)
        if expr.hasEvalError():
            errors += 1
            first_error = first_error or expr.evalErrorString()
            continue
        values[feature.id()] = value
    if errors and not values:
        raise ToolError(f"Expression failed on every feature: {first_error}")
    idx = lyr.fields().indexOf(field)
    if idx < 0:
        _editable_or_fail(lyr, QgsVectorDataProvider.AddAttributes, "adding fields")
        sample = next((v for v in values.values() if json_value(v) is not None), None)
        kind = field_type or _infer_type(json_value(sample))
        lyr.dataProvider().addAttributes([make_field(field, kind)])
        lyr.updateFields()
        idx = lyr.fields().indexOf(field)
    if lyr.isEditable():
        for fid, value in values.items():
            lyr.changeAttributeValue(fid, idx, value)
        mode = "edit buffer (save edits to persist)"
    else:
        _editable_or_fail(lyr, QgsVectorDataProvider.ChangeAttributeValues, "changing attribute values")
        if not lyr.dataProvider().changeAttributeValues({fid: {idx: v} for fid, v in values.items()}):
            raise ToolError(f"Update failed: {lyr.dataProvider().lastError()}")
        mode = "data source"
    lyr.triggerRepaint()
    out: Dict[str, Any] = {"layer": lyr.name(), "field": field, "updated": len(values), "written_to": mode,
                           "sample": [json_value(v) for v in list(values.values())[:5]]}
    if errors:
        out["errors"] = f"{errors} feature(s) failed: {first_error}"
    return out


@tool("delete_features", "Delete features matching a QGIS expression from a vector layer (in place).",
      {"layer": {"type": "string"},
       "where": {"type": "string", "description": "Expression selecting the features to delete (required)."}},
      required=["layer", "where"], risk=Risk.DESTRUCTIVE, category="editing", tier="extended")
def delete_features(ctx: ToolContext, layer: str, where: str) -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    if not where.strip():
        raise ToolError("A filter expression is required.")
    _editable_or_fail(lyr, QgsVectorDataProvider.DeleteFeatures, "deleting features")
    request = QgsFeatureRequest().setFilterExpression(where)
    request.setNoAttributes()
    ids = [f.id() for f in lyr.getFeatures(request)]
    if not ids:
        return {"layer": lyr.name(), "deleted": 0}
    if lyr.isEditable():
        lyr.deleteFeatures(ids)
    elif not lyr.dataProvider().deleteFeatures(ids):
        raise ToolError(f"Delete failed: {lyr.dataProvider().lastError()}")
    lyr.triggerRepaint()
    return {"layer": lyr.name(), "deleted": len(ids), "remaining": lyr.featureCount()}
