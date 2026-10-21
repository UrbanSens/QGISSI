"""Project and layer inspection / management tools."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from qgis.core import QgsRasterLayer, QgsUnitTypes, QgsVectorLayer, QgsWkbTypes

from ..core.context import crs_label, ordered_layers
from ._qgis import band_stats, json_value, parse_crs, rect_list, resolve_layer, to_wgs84_extent
from .registry import Risk, ToolContext, ToolError, ToolResult, tool


@tool("list_layers", "List every layer in the project with id, type, geometry, feature count, CRS and visibility. "
      "Call this when you need an overview or the context may be outdated.",
      {"include_fields": {"type": "boolean", "description": "Also list field names (default false)."}},
      category="project")
def list_layers(ctx: ToolContext, include_fields: bool = False) -> Dict[str, Any]:
    rows = []
    for layer, visible, group in ordered_layers(ctx.project):
        row: Dict[str, Any] = {"name": layer.name(), "id": layer.id(), "visible": visible,
                               "crs": layer.crs().authid()}
        if group:
            row["group"] = group
        if isinstance(layer, QgsVectorLayer):
            row.update(type="vector", geometry=QgsWkbTypes.displayString(layer.wkbType()),
                       features=layer.featureCount(), selected=layer.selectedFeatureCount())
            if include_fields:
                row["fields"] = [f.name() for f in layer.fields()]
        elif isinstance(layer, QgsRasterLayer):
            row.update(type="raster", bands=layer.bandCount(), size=[layer.width(), layer.height()])
        else:
            row["type"] = type(layer).__name__.replace("Qgs", "")
        rows.append(row)
    return {"project_crs": ctx.project.crs().authid(), "layer_count": len(rows), "layers": rows}


@tool("describe_layer", "Detailed description of one layer: fields with types, CRS and units, extent, feature "
      "count, geometry type, data source, filter, and a few sample features (vector) or band statistics "
      "(raster). Call this before using a layer's fields or CRS for the first time.",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "sample_size": {"type": "integer", "description": "Sample features to include (0-20, default 3)."}},
      required=["layer"], category="project", untrusted=True)
def describe_layer(ctx: ToolContext, layer: str, sample_size: int = 3) -> Dict[str, Any]:
    lyr = resolve_layer(ctx, layer)
    crs = lyr.crs()
    info: Dict[str, Any] = {
        "name": lyr.name(), "id": lyr.id(), "crs": crs.authid(), "crs_description": crs_label(crs),
        "crs_is_geographic": crs.isGeographic(), "map_units": QgsUnitTypes.toString(crs.mapUnits()),
        "provider": lyr.providerType(), "source": _short_source(lyr.source()),
        "extent": rect_list(lyr.extent()), "extent_wgs84": to_wgs84_extent(lyr, ctx.project),
    }
    if isinstance(lyr, QgsVectorLayer):
        info.update(type="vector", geometry=QgsWkbTypes.displayString(lyr.wkbType()),
                    feature_count=lyr.featureCount(), selected=lyr.selectedFeatureCount(),
                    subset_filter=lyr.subsetString() or None, editable=lyr.isEditable())
        info["fields"] = [{"name": f.name(), "type": f.typeName(), "length": f.length(), "precision": f.precision(),
                           **({"alias": f.alias()} if f.alias() else {})} for f in lyr.fields()]
        sample_size = max(0, min(int(sample_size), 20))
        if sample_size:
            names = [f.name() for f in lyr.fields()]
            samples = []
            for feature in lyr.getFeatures():
                samples.append({n: json_value(v) for n, v in zip(names, feature.attributes())})
                if len(samples) >= sample_size:
                    break
            info["sample_features"] = samples
    elif isinstance(lyr, QgsRasterLayer):
        provider = lyr.dataProvider()
        bands = []
        for band in range(1, lyr.bandCount() + 1):
            entry: Dict[str, Any] = {"band": band, "name": lyr.bandName(band),
                                     "data_type": int(provider.dataType(band)),
                                     "nodata": provider.sourceNoDataValue(band) if provider.sourceHasNoDataValue(band) else None}
            if band <= 4:
                stats = band_stats(provider, band, lyr.extent())
                entry.update(min=round(stats.minimumValue, 6), max=round(stats.maximumValue, 6),
                             mean=round(stats.mean, 6), stddev=round(stats.stdDev, 6))
            bands.append(entry)
        info.update(type="raster", width=lyr.width(), height=lyr.height(), bands=bands,
                    pixel_size=[lyr.rasterUnitsPerPixelX(), lyr.rasterUnitsPerPixelY()])
    else:
        info["type"] = type(lyr).__name__.replace("Qgs", "")
    return info


def _short_source(source: str) -> str:
    for secret in ("password=", "token=", "apikey=", "api_key=", "key="):
        if secret in source.lower():
            return source.split("?")[0] + " (credentials hidden)"
    return source if len(source) < 300 else source[:300] + "..."


@tool("get_project_info", "Project-level information: title, file, CRS, ellipsoid, layer count, print layouts "
      "and the current map view.", {}, category="project")
def get_project_info(ctx: ToolContext) -> Dict[str, Any]:
    project = ctx.project
    info: Dict[str, Any] = {"title": project.title(), "file": project.fileName() or None,
                            "crs": crs_label(project.crs()), "ellipsoid": project.ellipsoid(),
                            "layer_count": len(project.mapLayers()),
                            "layouts": [lay.name() for lay in project.layoutManager().layouts()]}
    if ctx.iface is not None:
        canvas = ctx.iface.mapCanvas()
        info["map_view"] = {"extent": rect_list(canvas.extent()), "scale": round(canvas.scale())}
    elif ctx.state.get("map_extent"):
        info["map_view"] = {"extent": ctx.state["map_extent"]}
    return info


@tool("set_project_crs", "Set the project (map) CRS. Layers are reprojected on the fly; data is unchanged.",
      {"crs": {"type": "string", "description": "Authority id, e.g. EPSG:3857 or EPSG:25832."}},
      required=["crs"], risk=Risk.WRITE, category="project")
def set_project_crs(ctx: ToolContext, crs: str) -> str:
    target = parse_crs(crs)
    ctx.project.setCrs(target)
    return f"Project CRS set to {crs_label(target)}."


@tool("rename_layer", "Rename a layer in the project.",
      {"layer": {"type": "string", "description": "Layer name or id."},
       "new_name": {"type": "string", "description": "New layer name."}},
      required=["layer", "new_name"], risk=Risk.WRITE, category="project")
def rename_layer(ctx: ToolContext, layer: str, new_name: str) -> str:
    lyr = resolve_layer(ctx, layer)
    old = lyr.name()
    lyr.setName(new_name)
    return f"Renamed '{old}' to '{new_name}'."


@tool("set_layer_visibility", "Show or hide layers in the map.",
      {"layers": {"type": "array", "items": {"type": "string"}, "description": "Layer names or ids."},
       "visible": {"type": "boolean", "description": "true to show, false to hide."}},
      required=["layers", "visible"], risk=Risk.WRITE, category="project")
def set_layer_visibility(ctx: ToolContext, layers: List[str], visible: bool) -> str:
    root = ctx.project.layerTreeRoot()
    changed = []
    for ref in layers:
        lyr = resolve_layer(ctx, ref)
        node = root.findLayer(lyr.id())
        if node is not None:
            node.setItemVisibilityChecked(visible)
            changed.append(lyr.name())
    return f"{'Showing' if visible else 'Hiding'}: {', '.join(changed)}."


@tool("remove_layer", "Remove a layer from the project (data on disk is not deleted; undo restores it).",
      {"layer": {"type": "string", "description": "Layer name or id."}},
      required=["layer"], risk=Risk.DESTRUCTIVE, category="project")
def remove_layer(ctx: ToolContext, layer: str) -> str:
    lyr = resolve_layer(ctx, layer)
    name = lyr.name()
    if ctx.checkpoint is not None:
        ctx.checkpoint.take_layer(lyr)
    else:
        ctx.project.removeMapLayer(lyr.id())
    return f"Removed layer '{name}' from the project."


@tool("move_layers_to_group", "Put layers into a layer-tree group (created if missing), e.g. to organise results.",
      {"layers": {"type": "array", "items": {"type": "string"}, "description": "Layer names or ids."},
       "group": {"type": "string", "description": "Group name."}},
      required=["layers", "group"], risk=Risk.WRITE, category="project", tier="extended")
def move_layers_to_group(ctx: ToolContext, layers: List[str], group: str) -> str:
    root = ctx.project.layerTreeRoot()
    target = root.findGroup(group) or root.insertGroup(0, group)
    moved = []
    for ref in layers:
        lyr = resolve_layer(ctx, ref)
        node = root.findLayer(lyr.id())
        if node is None:
            continue
        clone = node.clone()
        target.addChildNode(clone)
        node.parent().removeChildNode(node)
        moved.append(lyr.name())
    return f"Moved {', '.join(moved)} into group '{group}'."


@tool("ask_user", "Ask the user a clarifying question and wait for the answer. Use only when the request is "
      "ambiguous in a way that changes the result; offer concrete options when possible.",
      {"question": {"type": "string", "description": "The question."},
       "options": {"type": "array", "items": {"type": "string"}, "description": "Suggested answers (optional)."}},
      required=["question"], category="assistant")
def ask_user(ctx: ToolContext, question: str, options: Optional[List[str]] = None) -> ToolResult:
    if ctx.ask_user is None:
        return ToolResult(text="No user is available to answer. Proceed with the most reasonable assumption "
                               "and state it in your final answer.")
    answer = ctx.ask_user(question, options or [])
    if not answer:
        raise ToolError("The user did not answer. Proceed with a reasonable assumption and state it.")
    return ToolResult(text=f"User answered: {answer}")


@tool("save_project", "Save the QGIS project (to its current file, or to a new .qgz path).",
      {"path": {"type": "string", "description": "Optional new file path (.qgz/.qgs)."}},
      risk=Risk.WRITE, category="project")
def save_project(ctx: ToolContext, path: str = "") -> str:
    import os
    target = os.path.expanduser(path) if path else ctx.project.fileName()
    if not target:
        raise ToolError("The project has never been saved; give a path, e.g. analysis.qgz.")
    if path and not os.path.isabs(target) and ctx.output_dir:
        target = os.path.join(ctx.output_dir, target)
    if not ctx.project.write(target):
        raise ToolError(f"Could not save the project to {target}.")
    return f"Project saved to {target}."
