"""QGIS Processing: discover, explain and run any of the ~700 algorithms."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from qgis.core import (QgsApplication, QgsMapLayer, QgsProcessingAlgorithm, QgsProcessingContext,
                       QgsProcessingException, QgsProcessingFeedback, QgsProcessingParameterDefinition,
                       QgsRasterLayer, QgsVectorLayer)

from ._qgis import add_to_project, json_value, parse_crs, resolve_layer
from .registry import Risk, ToolContext, ToolError, ToolResult, tool

LAYER_TYPES = {"source", "vector", "raster", "layer", "mesh", "pointcloud", "annotation", "vectortile"}
DESTINATION_TYPES = {"sink", "vectorDestination", "rasterDestination", "fileDestination", "folderDestination",
                     "pointCloudDestination", "vectorTileDestination"}
PREFERRED_PROVIDERS = {"native": 3, "qgis": 2, "gdal": 1}
# Algorithms that modify existing data in place: truncation, arbitrary SQL, shapefile repair, and
# imports that can overwrite database tables. Ids verified against QGIS 3.36's registry.
IN_PLACE_ALGORITHMS = {
    "native:truncatetable", "native:repairshapefile", "native:setlayerencoding",
    "native:postgisexecutesql", "qgis:postgisexecutesql", "qgis:postgisexecuteandloadsql",
    "native:spatialiteexecutesql", "native:spatialiteexecutesqlregistered", "qgis:spatialiteexecutesql",
    "native:importintopostgis", "qgis:importintospatialite",
    "gdal:importvectorintopostgisdatabaseavailableconnections", "gdal:importvectorintopostgisdatabasenewconnection",
}


def processing_risk(args: Dict[str, Any]) -> str:
    """Destructive when the algorithm edits its input in place or the call overwrites files."""
    if str(args.get("algorithm_id", "")).strip().lower() in IN_PLACE_ALGORITHMS or args.get("overwrite"):
        return Risk.DESTRUCTIVE
    return Risk.WRITE


def _as_int(flags: Any) -> int:
    try:
        return int(flags)
    except TypeError:  # PyQt6 flag objects
        return int(getattr(flags, "value", 0))


def _flag(owner: Any, legacy: str, enum_name: str, member: str) -> int:
    value = getattr(owner, legacy, None)
    if value is None:  # QGIS 4 moved the flags into Qgis.* enums
        from qgis.core import Qgis
        value = getattr(getattr(Qgis, enum_name), member)
    return _as_int(value)


_OPTIONAL = _flag(QgsProcessingParameterDefinition, "FlagOptional", "ProcessingParameterFlag", "Optional")
_HIDDEN = _flag(QgsProcessingParameterDefinition, "FlagHidden", "ProcessingParameterFlag", "Hidden")
_DEPRECATED = _flag(QgsProcessingAlgorithm, "FlagDeprecated", "ProcessingAlgorithmFlag", "Deprecated")


def _is_optional(param) -> bool:
    return bool(_as_int(param.flags()) & _OPTIONAL)


def _is_hidden(param) -> bool:
    return bool(_as_int(param.flags()) & _HIDDEN)


def _registry():
    return QgsApplication.processingRegistry()


def _get_algorithm(algorithm_id: str):
    alg = _registry().algorithmById(algorithm_id)
    if alg is None:
        import difflib
        ids = [a.id() for a in _registry().algorithms()]
        close = difflib.get_close_matches(algorithm_id, ids, n=5, cutoff=0.5)
        hint = f" Close matches: {', '.join(close)}." if close else ""
        raise ToolError(f"Unknown algorithm '{algorithm_id}'.{hint} Use search_processing_algorithms.")
    return alg


def _plain(text: str, limit: int = 1500) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "..."


@tool("search_processing_algorithms", "Search QGIS Processing algorithms (buffer, clip, intersection, join "
      "attributes by location, zonal statistics, reproject, dissolve, raster calculator, slope, ...). Returns "
      "algorithm ids to use with get_algorithm_help and run_processing_algorithm.",
      {"query": {"type": "string", "description": "Keywords, e.g. 'count points in polygon'."},
       "limit": {"type": "integer", "description": "Maximum results (default 12)."}},
      required=["query"], category="processing")
def search_processing_algorithms(ctx: ToolContext, query: str, limit: int = 12) -> Dict[str, Any]:
    tokens = [t for t in re.split(r"[^\w]+", query.lower()) if t]
    if not tokens:
        raise ToolError("Give at least one keyword.")
    scored = []
    for alg in _registry().algorithms():
        name = alg.displayName().lower()
        alg_id = alg.id().lower()
        tags = " ".join(alg.tags()).lower()
        group = (alg.group() or "").lower()
        helptext = (alg.shortDescription() or alg.shortHelpString() or "")[:400].lower()
        score = 0.0
        for token in tokens:
            if re.search(rf"\b{re.escape(token)}", name):
                score += 6
            elif token in name:
                score += 3
            if token in alg_id:
                score += 3
            if token in tags:
                score += 2
            if token in group:
                score += 1
            if token in helptext:
                score += 1
        if score <= 0:
            continue
        if all(t in name for t in tokens):
            score += 5
        provider = alg.provider().id() if alg.provider() else ""
        score += PREFERRED_PROVIDERS.get(provider, 0) * 0.5
        if _as_int(alg.flags()) & _DEPRECATED:
            score -= 10
        scored.append((score, alg))
    scored.sort(key=lambda item: (-item[0], item[1].id()))
    limit = max(1, min(int(limit), 40))
    results = []
    for _score, alg in scored[:limit]:
        summary = _plain(alg.shortDescription() or alg.shortHelpString(), 160)
        results.append({"id": alg.id(), "name": alg.displayName(), "group": alg.group(),
                        **({"summary": summary} if summary else {})})
    if not results:
        return {"query": query, "results": [], "hint": "No match. Try simpler or English keywords."}
    return {"query": query, "results": results}


def _param_info(param) -> Dict[str, Any]:
    info: Dict[str, Any] = {"name": param.name(), "type": param.type(), "description": param.description(),
                            "optional": _is_optional(param)}
    default = param.defaultValue()
    if default is not None:
        info["default"] = json_value(default)
    ptype = param.type()
    if ptype == "enum":
        info["options"] = {str(i): option for i, option in enumerate(param.options())}
        info["multiple"] = param.allowMultiple()
        info["note"] = "Pass the integer index (or a list of indices if multiple)."
    elif ptype == "field":
        info["parent_layer_parameter"] = param.parentLayerParameterName()
        info["multiple"] = param.allowMultiple()
    elif ptype == "distance":
        info["note"] = (f"Distance in the units of the CRS of parameter '{param.parentParameterName()}'"
                        " (degrees for geographic CRS!).")
    elif ptype == "number":
        info["number_type"] = "integer" if int(param.dataType()) == 0 else "double"
    elif ptype in ("multilayer",):
        info["note"] = "List of layer names/ids."
    elif ptype in DESTINATION_TYPES:
        info["note"] = "Use 'TEMPORARY_OUTPUT' for a temporary layer or a file name/path to save."
    elif ptype == "expression":
        info["parent_layer_parameter"] = param.parentLayerParameterName()
    return info


@tool("get_algorithm_help", "Parameters (names, types, enum options, defaults) and outputs of a Processing "
      "algorithm, plus an example parameter dictionary. Call this before run_processing_algorithm.",
      {"algorithm_id": {"type": "string", "description": "e.g. native:buffer"}},
      required=["algorithm_id"], category="processing")
def get_algorithm_help(ctx: ToolContext, algorithm_id: str) -> Dict[str, Any]:
    alg = _get_algorithm(algorithm_id)
    params = [_param_info(p) for p in alg.parameterDefinitions() if not _is_hidden(p)]
    example = {}
    for p in alg.parameterDefinitions():
        if _is_hidden(p) or _is_optional(p):
            continue
        if p.type() in DESTINATION_TYPES:
            example[p.name()] = "TEMPORARY_OUTPUT"
        elif p.type() in LAYER_TYPES:
            example[p.name()] = "<layer name>"
        elif p.defaultValue() is not None:
            example[p.name()] = json_value(p.defaultValue())
        else:
            example[p.name()] = f"<{p.type()}>"
    return {"id": alg.id(), "name": alg.displayName(), "help": _plain(alg.shortHelpString()),
            "parameters": params,
            "outputs": [{"name": o.name(), "type": o.type(), "description": o.description()}
                        for o in alg.outputDefinitions()],
            "example_parameters": example}


class _Feedback(QgsProcessingFeedback):
    def __init__(self, ctx: ToolContext) -> None:
        super().__init__()
        self.lines: List[str] = []
        self.errors: List[str] = []
        self._ctx = ctx
        self.progressChanged.connect(self._check_cancel)

    def _check_cancel(self, _progress: float) -> None:
        if self._ctx.cancel.cancelled:
            self.cancel()

    def reportError(self, error, fatalError=False):  # noqa: N802 (Qt API)
        self.errors.append(str(error))

    def pushInfo(self, info):  # noqa: N802
        self.lines.append(str(info))

    def pushWarning(self, warning):  # noqa: N802
        self.lines.append("WARNING: " + str(warning))


def _resolve_layer_value(ctx: ToolContext, value: Any) -> Any:
    if isinstance(value, str):
        if value.upper() == "TEMPORARY_OUTPUT" or os.path.exists(value.split("|")[0]):
            return value
        return resolve_layer(ctx, value).id()
    if isinstance(value, dict) and "layer" in value:  # allow {"layer": "...", "selected_only": true}
        from qgis.core import QgsProcessingFeatureSourceDefinition
        layer = resolve_layer(ctx, value["layer"])
        return QgsProcessingFeatureSourceDefinition(layer.id(), bool(value.get("selected_only")))
    return value


def _prepare(ctx: ToolContext, alg, parameters: Dict[str, Any], allow_geographic_distance: bool,
             overwrite: bool = False) -> Dict[str, Any]:
    definitions = {p.name(): p for p in alg.parameterDefinitions()}
    lowered = {name.lower(): name for name in definitions}
    prepared: Dict[str, Any] = {}
    for key, value in parameters.items():
        name = key if key in definitions else lowered.get(key.lower())
        if name is None:
            raise ToolError(f"'{alg.id()}' has no parameter '{key}'. Parameters: {', '.join(definitions)}.")
        prepared[name] = value
    destinations = [p for p in definitions.values() if p.type() in DESTINATION_TYPES and p.type() != "folderDestination"]
    primary = next((p.name() for p in destinations if p.name() == "OUTPUT"), destinations[0].name() if destinations else None)
    for name, param in definitions.items():
        ptype = param.type()
        value = prepared.get(name)
        if value is None:
            # Some algorithms declare their main output optional (e.g. native:joinattributestable);
            # always produce the primary output so the result can be inspected.
            if ptype in DESTINATION_TYPES and ptype != "folderDestination" and (not _is_optional(param) or name == primary):
                prepared[name] = "TEMPORARY_OUTPUT"
            continue
        if ptype == "crs" and isinstance(value, str):
            prepared[name] = parse_crs(value)
            continue
        if ptype in LAYER_TYPES:
            prepared[name] = _resolve_layer_value(ctx, value)
        elif ptype == "multilayer":
            prepared[name] = [_resolve_layer_value(ctx, v) for v in (value if isinstance(value, list) else [value])]
        elif ptype == "enum":
            prepared[name] = _enum_value(param, value)
        elif ptype in DESTINATION_TYPES and isinstance(value, str) and value.upper() != "TEMPORARY_OUTPUT":
            path = value
            if not os.path.isabs(value) and ctx.output_dir:
                os.makedirs(ctx.output_dir, exist_ok=True)
                path = os.path.join(ctx.output_dir, value)
            if os.path.exists(path.split("|")[0]) and not overwrite and ptype != "folderDestination":
                raise ToolError(f"Not run: output '{path}' already exists and would be overwritten. Use another "
                                "file name, or pass overwrite=true if replacing it is intended.")
            prepared[name] = path
    for name, param in definitions.items():  # second pass: needs resolved layers
        value = prepared.get(name)
        if value is None:
            continue
        if param.type() == "field":
            prepared[name] = _field_value(ctx, param, prepared, value)
        elif param.type() == "distance" and not allow_geographic_distance:
            parent = prepared.get(param.parentParameterName())
            layer = ctx.project.mapLayer(parent) if isinstance(parent, str) else None
            if layer is not None and layer.crs().isGeographic():
                raise ToolError(
                    f"Not run: '{name}' would be interpreted in degrees because '{layer.name()}' uses "
                    f"{layer.crs().authid()} (geographic). Reproject the input to a projected CRS in metres "
                    "first (native:reprojectlayer, e.g. to the local UTM zone), or pass "
                    "allow_geographic_distance=true if degrees are really intended.")
    return prepared


def _enum_value(param, value: Any) -> Any:
    options = [o.lower() for o in param.options()]

    def one(item: Any) -> int:
        if isinstance(item, bool):
            raise ToolError(f"Enum parameter '{param.name()}' needs an index.")
        if isinstance(item, (int, float)):
            return int(item)
        text = str(item).strip()
        if text.isdigit():
            return int(text)
        if text.lower() in options:
            return options.index(text.lower())
        raise ToolError(f"'{item}' is not an option of '{param.name()}'. Options: "
                        + ", ".join(f"{i}={o}" for i, o in enumerate(param.options())))

    if isinstance(value, list):
        return [one(v) for v in value]
    return one(value)


def _field_value(ctx: ToolContext, param, prepared: Dict[str, Any], value: Any) -> Any:
    parent = prepared.get(param.parentLayerParameterName())
    layer = ctx.project.mapLayer(parent) if isinstance(parent, str) else None
    if not isinstance(layer, QgsVectorLayer):
        return value
    names = {f.name().lower(): f.name() for f in layer.fields()}

    def fix(item: Any) -> Any:
        if isinstance(item, str) and item not in names.values() and item.lower() in names:
            return names[item.lower()]
        if isinstance(item, str) and item and item.lower() not in names:
            raise ToolError(f"Field '{item}' not found in '{layer.name()}'. Fields: {', '.join(names.values())}.")
        return item

    return [fix(v) for v in value] if isinstance(value, list) else fix(value)


def _layer_summary(layer: QgsMapLayer) -> Dict[str, Any]:
    info: Dict[str, Any] = {"name": layer.name(), "id": layer.id(), "crs": layer.crs().authid()}
    if isinstance(layer, QgsVectorLayer):
        info["features"] = layer.featureCount()
        info["fields"] = [f.name() for f in layer.fields()][:40]
    elif isinstance(layer, QgsRasterLayer):
        info["size"] = [layer.width(), layer.height()]
        info["bands"] = layer.bandCount()
    return info


@tool("run_processing_algorithm", "Run a QGIS Processing algorithm. Layer parameters accept layer names or ids "
      "(or {\"layer\": name, \"selected_only\": true}); enum parameters accept indices or option labels; "
      "missing outputs default to TEMPORARY_OUTPUT. Outputs are added to the project (unless load_outputs is "
      "false) and summarised with feature counts.",
      {"algorithm_id": {"type": "string", "description": "e.g. native:buffer"},
       "parameters": {"type": "object", "description": "Parameter name -> value, as listed by get_algorithm_help."},
       "output_name": {"type": "string", "description": "Name for the main output layer (recommended)."},
       "load_outputs": {"type": "boolean", "description": "Add layer outputs to the project (default true)."},
       "allow_geographic_distance": {"type": "boolean",
                                     "description": "Permit distance parameters in degrees (default false)."},
       "overwrite": {"type": "boolean", "description": "Allow replacing existing output files (needs approval)."}},
      required=["algorithm_id", "parameters"], risk=Risk.WRITE, category="processing", risk_fn=processing_risk)
def run_processing_algorithm(ctx: ToolContext, algorithm_id: str, parameters: Dict[str, Any],
                             output_name: str = "", load_outputs: bool = True,
                             allow_geographic_distance: bool = False, overwrite: bool = False) -> ToolResult:
    import processing  # noqa: deferred so tool registration works without the Processing plugin

    alg = _get_algorithm(algorithm_id)
    prepared = _prepare(ctx, alg, parameters or {}, allow_geographic_distance, overwrite)
    context = QgsProcessingContext()
    context.setProject(ctx.project)
    feedback = _Feedback(ctx)
    ok, message = alg.checkParameterValues(prepared, context)
    if not ok:
        raise ToolError(f"Invalid parameters for {algorithm_id}: {message}")
    ctx.report(f"Running {alg.displayName()}")
    try:
        results = processing.run(algorithm_id, prepared, feedback=feedback, context=context)
    except QgsProcessingException as exc:
        details = "; ".join(feedback.errors[-5:])
        raise ToolError(f"{algorithm_id} failed: {exc}{' | ' + details if details else ''}")
    if feedback.isCanceled():
        raise ToolError("Algorithm cancelled.")
    created: List[str] = []
    outputs: Dict[str, Any] = {}
    layer_outputs = [o for o in alg.outputDefinitions() if o.type() in ("outputVector", "outputRaster", "outputLayer",
                                                                          "outputMultilayer", "outputPointCloud")]
    for out in alg.outputDefinitions():
        value = results.get(out.name())
        if value is None:
            continue
        is_layer_output = out in layer_outputs
        if isinstance(value, QgsMapLayer) or (is_layer_output and isinstance(value, str)):
            layer = value if isinstance(value, QgsMapLayer) else _load_output(value, out.type(), ctx)
            if layer is None:
                outputs[out.name()] = value
                continue
            if load_outputs:
                base = output_name or f"{alg.displayName()}"
                if len(layer_outputs) > 1 and output_name:
                    base = f"{output_name} ({out.description() or out.name()})"
                add_to_project(ctx, layer, base, created)
            outputs[out.name()] = _layer_summary(layer)
            if isinstance(value, str):
                outputs[out.name()]["path"] = value
        elif isinstance(value, list) and is_layer_output:
            outputs[out.name()] = [str(v) for v in value]
        else:
            outputs[out.name()] = json_value(value)
    warnings = [line for line in feedback.lines if line.startswith("WARNING")][-5:]
    data: Dict[str, Any] = {"algorithm": algorithm_id, "outputs": outputs}
    if warnings:
        data["warnings"] = warnings
    if feedback.errors:
        data["errors"] = feedback.errors[-5:]
    empty = [o["name"] for o in outputs.values() if isinstance(o, dict) and o.get("features") == 0]
    if empty:
        data["check"] = f"Output {', '.join(empty)} has 0 features - verify inputs, CRS and parameters."
    return ToolResult(data=data, layers_created=created)


def _load_output(value: str, output_type: str, ctx: ToolContext) -> Optional[QgsMapLayer]:
    existing = ctx.project.mapLayer(value)
    if existing is not None:
        return None
    path = value.split("|")[0]
    if not os.path.exists(path):
        return None
    name = os.path.splitext(os.path.basename(path))[0]
    if output_type == "outputRaster":
        layer = QgsRasterLayer(value, name)
    else:
        layer = QgsVectorLayer(value, name, "ogr")
        if not layer.isValid():
            layer = QgsRasterLayer(value, name)
    return layer if layer.isValid() else None
