"""Data loading, open data (OSM / Nominatim), layer creation and export."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFields,
                       QgsGeometry, QgsPointXY, QgsRasterFileWriter, QgsRasterLayer, QgsRasterPipe,
                       QgsVectorFileWriter, QgsVectorLayer)

from ..providers.http import HttpClient, HttpError
from ._qgis import add_to_project, make_field, output_path, parse_crs, resolve_layer, resolve_vector, safe_filename
from .registry import Risk, ToolContext, ToolError, ToolResult, tool

NOMINATIM = os.environ.get("QGISSI_NOMINATIM_URL", "https://nominatim.openstreetmap.org")
OVERPASS = os.environ.get("QGISSI_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

_http = HttpClient(timeout=180, max_retries=2)
_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]

BASEMAPS = {
    "openstreetmap": ("OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19, "© OpenStreetMap contributors"),
    "opentopomap": ("OpenTopoMap", "https://tile.opentopomap.org/{z}/{x}/{y}.png", 17, "© OpenStreetMap contributors, SRTM | © OpenTopoMap (CC-BY-SA)"),
    "esri_imagery": ("Esri World Imagery", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", 19, "Esri, Maxar, Earthstar Geographics"),
    "esri_topo": ("Esri World Topo", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", 19, "Esri"),
    "esri_hillshade": ("Esri World Hillshade", "https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}", 16, "Esri"),
    "carto_positron": ("CARTO Positron", "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png", 20, "© OpenStreetMap contributors © CARTO"),
    "carto_dark": ("CARTO Dark Matter", "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", 20, "© OpenStreetMap contributors © CARTO"),
}

AREA_KEYS = {"building", "landuse", "leisure", "amenity", "natural", "water", "landcover", "place", "boundary",
             "aeroway", "military", "tourism", "historic", "shop", "office", "craft", "parking", "golf",
             "building:part", "area:highway", "waterway"}


def _nominatim(path: str, params: Dict[str, Any], ctx: ToolContext) -> Any:
    if not ctx.allow_network:
        raise ToolError("Network access is disabled in the settings.")
    with _nominatim_lock:  # Nominatim usage policy: at most one request per second
        wait = 1.1 - (time.time() - _nominatim_last[0])
        if wait > 0:
            time.sleep(wait)
        _nominatim_last[0] = time.time()
    url = f"{NOMINATIM}/{path}?{urllib.parse.urlencode(params)}"
    try:
        return _http.get_json(url, {"Accept-Language": "en"}, ctx.cancel)
    except HttpError as exc:
        raise ToolError(f"Geocoding service error: {exc.error_text()}")


# ------------------------------------------------------------------ loading
@tool("load_layer", "Add a dataset to the project: a file (GeoPackage, Shapefile, GeoJSON, KML, CSV, GeoTIFF, ...), "
      "a URL to a vector/raster file, or a provider URI (WMS/WFS/XYZ). For CSV points give x_field and y_field.",
      {"source": {"type": "string", "description": "File path, URL or URI."},
       "name": {"type": "string", "description": "Layer name (default: file name)."},
       "kind": {"type": "string", "enum": ["auto", "vector", "raster", "csv", "wms", "wfs", "xyz"],
                "description": "Data kind (default auto)."},
       "sublayer": {"type": "string", "description": "Layer/table inside a GeoPackage or multi-layer file."},
       "x_field": {"type": "string", "description": "CSV longitude/easting column."},
       "y_field": {"type": "string", "description": "CSV latitude/northing column."},
       "crs": {"type": "string", "description": "CRS of CSV coordinates (default EPSG:4326)."}},
      required=["source"], risk=Risk.WRITE, category="data")
def load_layer(ctx: ToolContext, source: str, name: str = "", kind: str = "auto", sublayer: str = "",
               x_field: str = "", y_field: str = "", crs: str = "EPSG:4326") -> ToolResult:
    source = os.path.expanduser(source.strip())
    is_url = source.lower().startswith(("http://", "https://"))
    if not is_url and kind in ("auto", "vector", "raster", "csv") and not os.path.exists(source.split("|")[0]):
        raise ToolError(f"File not found: {source}")
    label = name or os.path.splitext(os.path.basename(source.split("|")[0].split("?")[0]))[0] or "layer"
    created: List[str] = []
    layer = None
    if kind == "csv" or (kind == "auto" and source.lower().endswith((".csv", ".txt")) and x_field and y_field):
        if not (x_field and y_field):
            raise ToolError("CSV point layers need x_field and y_field.")
        uri = (f"file://{urllib.parse.quote(os.path.abspath(source))}?type=csv&delimiter=,&detectTypes=yes"
               f"&xField={urllib.parse.quote(x_field)}&yField={urllib.parse.quote(y_field)}"
               f"&crs={parse_crs(crs).authid()}&spatialIndex=yes")
        layer = QgsVectorLayer(uri, label, "delimitedtext")
    elif kind == "wms" or kind == "xyz":
        uri = source if "url=" in source else f"type=xyz&url={urllib.parse.quote(source, safe=':/{}')}"
        layer = QgsRasterLayer(uri, label, "wms")
    elif kind == "wfs":
        layer = QgsVectorLayer(source, label, "WFS")
    else:
        path = source + (f"|layername={sublayer}" if sublayer else "")
        if kind in ("auto", "vector"):
            layer = QgsVectorLayer(path, label, "ogr")
            if not layer.isValid() and kind == "auto":
                layer = QgsRasterLayer(source, label, "gdal")
        else:
            layer = QgsRasterLayer(source, label, "gdal")
    if layer is None or not layer.isValid():
        sub = layer.dataProvider().subLayers() if isinstance(layer, QgsVectorLayer) and layer.dataProvider() else []
        hint = f" Sublayers: {', '.join(s.split('!!::!!')[1] for s in sub if '!!::!!' in s)}" if sub else ""
        raise ToolError(f"Could not load '{source}' as {kind}.{hint}")
    add_to_project(ctx, layer, label, created)
    info: Dict[str, Any] = {"name": layer.name(), "id": layer.id(), "crs": layer.crs().authid()}
    if isinstance(layer, QgsVectorLayer):
        info.update(features=layer.featureCount(), fields=[f.name() for f in layer.fields()][:40])
    return ToolResult(data=info, layers_created=created)


@tool("add_basemap", "Add a web basemap below all layers. Presets: " + ", ".join(BASEMAPS) +
      ". Or pass a custom XYZ URL template with {x} {y} {z}.",
      {"basemap": {"type": "string", "description": "Preset key or XYZ URL template."},
       "name": {"type": "string", "description": "Layer name for a custom URL."}},
      required=["basemap"], risk=Risk.WRITE, category="data")
def add_basemap(ctx: ToolContext, basemap: str, name: str = "") -> ToolResult:
    key = basemap.strip().lower().replace(" ", "_")
    if key in BASEMAPS:
        label, url, zmax, attribution = BASEMAPS[key]
    elif "{z}" in basemap and basemap.startswith("http"):
        label, url, zmax, attribution = name or "Basemap", basemap, 19, ""
    else:
        raise ToolError(f"Unknown basemap '{basemap}'. Presets: {', '.join(BASEMAPS)}.")
    uri = f"type=xyz&url={urllib.parse.quote(url, safe=':/')}&zmax={zmax}&zmin=0"
    layer = QgsRasterLayer(uri, label, "wms")
    if not layer.isValid():
        raise ToolError(f"Could not create basemap layer for {url}")
    if attribution:
        layer.setAttribution(attribution)
    ctx.project.addMapLayer(layer, False)
    root = ctx.project.layerTreeRoot()
    root.insertLayer(len(root.children()), layer)
    return ToolResult(text=f"Added basemap '{label}' at the bottom of the layer list.", layers_created=[layer.id()])


# ------------------------------------------------------------------ geocoding
@tool("geocode", "Find places or addresses with OpenStreetMap Nominatim. Returns coordinates (WGS84), type and "
      "bounding box; can add the results as a point layer. Sends the query text to nominatim.openstreetmap.org.",
      {"query": {"type": "string", "description": "Place or address, e.g. 'Würzburg Residenz'."},
       "limit": {"type": "integer", "description": "Maximum results (default 5)."},
       "country_codes": {"type": "string", "description": "Optional ISO codes filter, e.g. 'de,at'."},
       "add_to_map": {"type": "boolean", "description": "Add results as a point layer (default false)."}},
      required=["query"], risk=Risk.NETWORK, category="data")
def geocode(ctx: ToolContext, query: str, limit: int = 5, country_codes: str = "",
            add_to_map: bool = False) -> ToolResult:
    params: Dict[str, Any] = {"q": query, "format": "jsonv2", "limit": max(1, min(int(limit), 20))}
    if country_codes:
        params["countrycodes"] = country_codes
    results = _nominatim("search", params, ctx) or []
    rows = [{"name": r.get("display_name"), "lat": float(r["lat"]), "lon": float(r["lon"]),
             "category": r.get("category") or r.get("class"), "type": r.get("type"),
             "osm": f"{r.get('osm_type')}/{r.get('osm_id')}",
             "bbox_wgs84": [float(r["boundingbox"][2]), float(r["boundingbox"][0]),
                            float(r["boundingbox"][3]), float(r["boundingbox"][1])] if r.get("boundingbox") else None}
            for r in results]
    created: List[str] = []
    if add_to_map and rows:
        layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string(254)&field=type:string(60)",
                               f"Geocoded: {query}"[:60], "memory")
        feats = []
        for row in rows:
            feature = QgsFeature(layer.fields())
            feature.setAttributes([row["name"], row["type"]])
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(row["lon"], row["lat"])))
            feats.append(feature)
        layer.dataProvider().addFeatures(feats)
        layer.updateExtents()
        add_to_project(ctx, layer, layer.name(), created)
    return ToolResult(data={"query": query, "results": rows or "no match"}, layers_created=created)


@tool("reverse_geocode", "Address / place name at a WGS84 coordinate (OpenStreetMap Nominatim).",
      {"lat": {"type": "number"}, "lon": {"type": "number"}},
      required=["lat", "lon"], risk=Risk.NETWORK, category="data", tier="extended")
def reverse_geocode(ctx: ToolContext, lat: float, lon: float) -> Dict[str, Any]:
    result = _nominatim("reverse", {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18}, ctx) or {}
    if result.get("error"):
        raise ToolError(str(result["error"]))
    return {"name": result.get("display_name"), "address": result.get("address"),
            "osm": f"{result.get('osm_type')}/{result.get('osm_id')}"}


# ----------------------------------------------------------------- OpenStreetMap
def _tag_filter(spec: str) -> str:
    parts = []
    for clause in [c.strip() for c in spec.split(",") if c.strip()]:
        if "~" in clause:
            key, _, value = clause.partition("~")
            parts.append(f'["{key.strip()}"~"{value.strip()}"]')
        elif "!=" in clause:
            key, _, value = clause.partition("!=")
            parts.append(f'["{key.strip()}"!="{value.strip()}"]')
        elif "=" in clause:
            key, _, value = clause.partition("=")
            value = value.strip()
            if value in ("*", ""):
                parts.append(f'["{key.strip()}"]')
            elif "|" in value:
                parts.append(f'["{key.strip()}"~"^({value})$"]')
            else:
                parts.append(f'["{key.strip()}"="{value}"]')
        else:
            parts.append(f'["{clause}"]')
    return "".join(parts)


def _is_area(tags: Dict[str, str], closed: bool) -> bool:
    if not closed or tags.get("area") == "no":
        return False
    if tags.get("area") == "yes":
        return True
    if "highway" in tags or "barrier" in tags:
        return False
    if tags.get("natural") in ("coastline", "tree_row", "cliff", "ridge"):
        return False
    if tags.get("waterway") and tags.get("waterway") not in ("riverbank", "dock", "boatyard"):
        return False
    return bool(AREA_KEYS & set(tags))


def _join_rings(lines: List[List[Tuple[float, float]]]) -> List[List[Tuple[float, float]]]:
    pending = [list(line) for line in lines if len(line) >= 2]
    rings = []
    while pending:
        current = pending.pop(0)
        changed = True
        while current[0] != current[-1] and changed:
            changed = False
            for i, other in enumerate(pending):
                if other[0] == current[-1]:
                    current = current + other[1:]
                elif other[-1] == current[-1]:
                    current = current + list(reversed(other))[1:]
                elif other[-1] == current[0]:
                    current = other + current[1:]
                elif other[0] == current[0]:
                    current = list(reversed(other)) + current[1:]
                else:
                    continue
                pending.pop(i)
                changed = True
                break
        if current[0] == current[-1] and len(current) >= 4:
            rings.append(current)
    return rings


def _relation_geometry(element: Dict[str, Any]) -> Optional[QgsGeometry]:
    outer, inner = [], []
    for member in element.get("members", []):
        if member.get("type") != "way" or not member.get("geometry"):
            continue
        coords = [(p["lon"], p["lat"]) for p in member["geometry"] if p]
        (inner if member.get("role") == "inner" else outer).append(coords)
    polygons = []
    inner_rings = _join_rings(inner)
    for ring in _join_rings(outer):
        shell = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]])
        holes = [[QgsPointXY(x, y) for x, y in r] for r in inner_rings
                 if shell.contains(QgsGeometry.fromPointXY(QgsPointXY(*r[0])))]
        polygons.append([[QgsPointXY(x, y) for x, y in ring]] + holes)
    if not polygons:
        return None
    return QgsGeometry.fromMultiPolygonXY(polygons)


def _resolve_area(ctx: ToolContext, place: str, bbox: Optional[List[float]], layer: str) -> Tuple[str, str]:
    """Overpass area clause and a human description."""
    if bbox:
        if len(bbox) != 4:
            raise ToolError("bbox must be [min_lon, min_lat, max_lon, max_lat] in WGS84.")
        w, s, e, n = [float(v) for v in bbox]
        return f"({s},{w},{n},{e})", f"bbox {bbox}"
    if layer:
        lyr = resolve_layer(ctx, layer)
        box = QgsCoordinateTransform(lyr.crs(), WGS84, ctx.project).transformBoundingBox(lyr.extent())
        return (f"({box.yMinimum()},{box.xMinimum()},{box.yMaximum()},{box.xMaximum()})",
                f"extent of '{lyr.name()}'")
    if place:
        results = _nominatim("search", {"q": place, "format": "jsonv2", "limit": 1}, ctx)
        if not results:
            raise ToolError(f"Place '{place}' not found.")
        hit = results[0]
        if hit.get("osm_type") == "relation":
            return f"(area:{3600000000 + int(hit['osm_id'])})", f"boundary of {hit.get('display_name')}"
        s, n, w, e = [float(v) for v in hit["boundingbox"]]
        return f"({s},{w},{n},{e})", f"bounding box of {hit.get('display_name')}"
    if ctx.iface is not None:
        canvas = ctx.iface.mapCanvas()
        box = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), WGS84, ctx.project) \
            .transformBoundingBox(canvas.extent())
        return f"({box.yMinimum()},{box.xMinimum()},{box.yMaximum()},{box.xMaximum()})", "current map view"
    raise ToolError("Give a place, a bbox or a layer to define the area.")


@tool("download_osm_data", "Download OpenStreetMap features (Overpass API) for tags in an area and add them as "
      "point/line/polygon layers in EPSG:4326. Tags: 'building', 'amenity=school', 'highway=primary|secondary', "
      "'shop=*', 'name~Park' (comma inside one string = AND; separate strings = OR). Area: a place name (uses "
      "its exact boundary when available), a WGS84 bbox, or a layer's extent.",
      {"tags": {"type": "array", "items": {"type": "string"}, "description": "Tag filters."},
       "place": {"type": "string", "description": "Place name, e.g. 'Würzburg, Germany'."},
       "bbox": {"type": "array", "items": {"type": "number"}, "description": "[min_lon, min_lat, max_lon, max_lat]"},
       "extent_layer": {"type": "string", "description": "Use this layer's extent as the area."},
       "name": {"type": "string", "description": "Base name for the new layers."},
       "max_features": {"type": "integer", "description": "Cap on downloaded elements (default 20000)."}},
      required=["tags"], risk=Risk.NETWORK, category="data")
def download_osm_data(ctx: ToolContext, tags: List[str], place: str = "", bbox: Optional[List[float]] = None,
                      extent_layer: str = "", name: str = "", max_features: int = 20000) -> ToolResult:
    if not ctx.allow_network:
        raise ToolError("Network access is disabled in the settings.")
    if not tags:
        raise ToolError("Give at least one tag filter.")
    area, area_label = _resolve_area(ctx, place, bbox, extent_layer)
    limit = max(1, min(int(max_features), 100000))
    if area.startswith("(area:"):
        area_id = area[6:-1]
        header = f"area(id:{area_id})->.a;"
        selector = "(area.a)"
    else:
        header, selector = "", area
    unions = "".join(f"nwr{_tag_filter(t)}{selector};" for t in tags)
    query = f"[out:json][timeout:120];{header}({unions});out geom {limit};"
    ctx.report(f"Downloading OSM data ({', '.join(tags)}) for {area_label}")
    try:
        data = _http.post_form(OVERPASS, {"data": query}, {}, ctx.cancel)
    except HttpError as exc:
        raise ToolError(f"Overpass API error: {exc.error_text()[:300]}")
    elements = data.get("elements", [])
    tag_keys: List[str] = []
    for spec in tags:
        for clause in spec.split(","):
            key = clause.replace("!=", "=").replace("~", "=").split("=")[0].strip()
            if key and key not in tag_keys and key != "name":
                tag_keys.append(key)
    fields = QgsFields()
    for field_name in ["osm_id", "osm_type", "name"] + tag_keys[:12] + ["tags"]:
        fields.append(make_field(field_name, "string"))
    buckets: Dict[str, List[QgsFeature]] = {"points": [], "lines": [], "polygons": []}
    for element in elements:
        etags = element.get("tags") or {}
        geometry = None
        bucket = None
        if element["type"] == "node" and "lat" in element:
            geometry, bucket = QgsGeometry.fromPointXY(QgsPointXY(element["lon"], element["lat"])), "points"
        elif element["type"] == "way" and element.get("geometry"):
            coords = [(p["lon"], p["lat"]) for p in element["geometry"] if p]
            if len(coords) < 2:
                continue
            if _is_area(etags, coords[0] == coords[-1] and len(coords) >= 4):
                geometry = QgsGeometry.fromMultiPolygonXY([[[QgsPointXY(x, y) for x, y in coords]]])
                bucket = "polygons"
            else:
                geometry, bucket = QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in coords]), "lines"
        elif element["type"] == "relation" and etags.get("type") in ("multipolygon", "boundary"):
            geometry, bucket = _relation_geometry(element), "polygons"
        if geometry is None or bucket is None or geometry.isEmpty():
            continue
        feature = QgsFeature(fields)
        values = [f"{element['type'][0]}{element['id']}", element["type"], etags.get("name")]
        values += [etags.get(k) for k in tag_keys[:12]]
        values.append(json.dumps(etags, ensure_ascii=False)[:2000])
        feature.setAttributes(values)
        feature.setGeometry(geometry)
        buckets[bucket].append(feature)
    base = name or f"OSM {' + '.join(tags)}"[:60]
    created: List[str] = []
    summary: Dict[str, Any] = {"area": area_label, "elements": len(elements), "layers": []}
    geometry_types = {"points": "Point", "lines": "LineString", "polygons": "MultiPolygon"}
    for bucket, features in buckets.items():
        if not features:
            continue
        layer = QgsVectorLayer(f"{geometry_types[bucket]}?crs=EPSG:4326", f"{base} ({bucket})", "memory")
        layer.dataProvider().addAttributes(fields.toList())
        layer.updateFields()
        layer.dataProvider().addFeatures(features)
        layer.updateExtents()
        add_to_project(ctx, layer, layer.name() if sum(1 for f in buckets.values() if f) > 1 else base, created)
        summary["layers"].append({"name": layer.name(), "features": layer.featureCount()})
    if len(elements) >= limit:
        summary["note"] = f"Result hit the {limit}-element cap; the data may be incomplete."
    if not created:
        summary["note"] = "No features found. Check the tags (OSM keys are case-sensitive) and the area."
    summary["license"] = "© OpenStreetMap contributors, ODbL 1.0"
    return ToolResult(data=summary, layers_created=created)


# ------------------------------------------------------------------ creation
_GEOMETRIES = {"point": "Point", "multipoint": "MultiPoint", "linestring": "LineString", "line": "LineString",
               "multilinestring": "MultiLineString", "polygon": "Polygon", "multipolygon": "MultiPolygon",
               "none": "None"}


def _features_from_specs(layer: QgsVectorLayer, specs: List[Dict[str, Any]], wkt_crs: str,
                         ctx: ToolContext) -> List[QgsFeature]:
    transform = None
    if wkt_crs:
        source = parse_crs(wkt_crs)
        if source != layer.crs():
            transform = QgsCoordinateTransform(source, layer.crs(), ctx.project)
    names = [f.name() for f in layer.fields()]
    features = []
    for i, spec in enumerate(specs):
        feature = QgsFeature(layer.fields())
        attributes = spec.get("attributes") or {}
        unknown = [k for k in attributes if k not in names]
        if unknown:
            raise ToolError(f"Feature {i}: unknown field(s) {unknown}. Fields: {names}.")
        for key, value in attributes.items():
            feature[key] = value
        if spec.get("wkt"):
            geometry = QgsGeometry.fromWkt(spec["wkt"])
            if geometry.isNull():
                raise ToolError(f"Feature {i}: invalid WKT.")
            if transform is not None:
                geometry.transform(transform)
            feature.setGeometry(geometry)
        features.append(feature)
    return features


@tool("create_layer", "Create a new empty (or pre-filled) vector layer: memory by default, or a GeoPackage file. "
      "Use it for digitising points/areas from coordinates or storing computed results.",
      {"name": {"type": "string"},
       "geometry_type": {"type": "string", "enum": list(_GEOMETRIES)},
       "crs": {"type": "string", "description": "Layer CRS, e.g. EPSG:4326 (default)."},
       "fields": {"type": "array", "items": {"type": "object"},
                  "description": "[{\"name\": \"label\", \"type\": \"string|integer|double|date|boolean\"}]"},
       "features": {"type": "array", "items": {"type": "object"},
                    "description": "Optional [{\"wkt\": \"POINT(9.93 49.79)\", \"attributes\": {...}}]"},
       "wkt_crs": {"type": "string", "description": "CRS of the WKT coordinates if different from crs."},
       "save_to": {"type": "string", "description": "Optional .gpkg file name to persist the layer."}},
      required=["name", "geometry_type"], risk=Risk.WRITE, category="data")
def create_layer(ctx: ToolContext, name: str, geometry_type: str, crs: str = "EPSG:4326",
                 fields: Optional[List[Dict[str, Any]]] = None, features: Optional[List[Dict[str, Any]]] = None,
                 wkt_crs: str = "", save_to: str = "") -> ToolResult:
    target_crs = parse_crs(crs)
    geom = _GEOMETRIES[geometry_type.lower()]
    layer = QgsVectorLayer(f"{geom}?crs={target_crs.authid()}" if geom != "None" else "None", name, "memory")
    defs = [make_field(str(f.get("name")), str(f.get("type", "string"))) for f in (fields or []) if f.get("name")]
    if defs:
        layer.dataProvider().addAttributes(defs)
        layer.updateFields()
    if features:
        layer.dataProvider().addFeatures(_features_from_specs(layer, features, wkt_crs, ctx))
        layer.updateExtents()
    created: List[str] = []
    if save_to:
        path = output_path(ctx, save_to if save_to.lower().endswith(".gpkg") else save_to + ".gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = safe_filename(name)
        error = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, ctx.project.transformContext(), options)
        if error[0] != QgsVectorFileWriter.NoError:
            raise ToolError(f"Could not write {path}: {error[1]}")
        layer = QgsVectorLayer(f"{path}|layername={options.layerName}", name, "ogr")
    add_to_project(ctx, layer, name, created)
    return ToolResult(data={"name": layer.name(), "id": layer.id(), "features": layer.featureCount(),
                            "fields": [f.name() for f in layer.fields()]}, layers_created=created)


@tool("add_features", "Append features (WKT geometry + attributes) to an existing vector layer.",
      {"layer": {"type": "string"},
       "features": {"type": "array", "items": {"type": "object"},
                    "description": "[{\"wkt\": \"POINT(x y)\", \"attributes\": {\"field\": value}}]"},
       "wkt_crs": {"type": "string", "description": "CRS of the WKT if different from the layer CRS."}},
      required=["layer", "features"], risk=Risk.DESTRUCTIVE, category="editing")
def add_features(ctx: ToolContext, layer: str, features: List[Dict[str, Any]], wkt_crs: str = "") -> Dict[str, Any]:
    lyr = resolve_vector(ctx, layer)
    feats = _features_from_specs(lyr, features, wkt_crs, ctx)
    ok, _added = lyr.dataProvider().addFeatures(feats)
    if not ok:
        raise ToolError(f"The data provider refused the features: {lyr.dataProvider().lastError()}")
    lyr.updateExtents()
    lyr.triggerRepaint()
    return {"layer": lyr.name(), "added": len(feats), "feature_count": lyr.featureCount()}


# ------------------------------------------------------------------ export
_FORMATS = {"gpkg": ("GPKG", ".gpkg"), "geojson": ("GeoJSON", ".geojson"), "shapefile": ("ESRI Shapefile", ".shp"),
            "csv": ("CSV", ".csv"), "kml": ("KML", ".kml"), "flatgeobuf": ("FlatGeobuf", ".fgb"),
            "xlsx": ("XLSX", ".xlsx"), "geotiff": ("GTiff", ".tif")}


@tool("export_layer", "Save a layer to a file in the output folder (GeoPackage, GeoJSON, Shapefile, CSV, KML, "
      "FlatGeobuf, XLSX; GeoTIFF for rasters), optionally reprojected or only selected features.",
      {"layer": {"type": "string"},
       "filename": {"type": "string", "description": "File name or path."},
       "format": {"type": "string", "enum": list(_FORMATS)},
       "crs": {"type": "string", "description": "Optional target CRS."},
       "selected_only": {"type": "boolean"},
       "overwrite": {"type": "boolean"}},
      required=["layer", "filename"], risk=Risk.WRITE, category="data")
def export_layer(ctx: ToolContext, layer: str, filename: str, format: str = "gpkg", crs: str = "",  # noqa: A002
                 selected_only: bool = False, overwrite: bool = False) -> Dict[str, Any]:
    lyr = resolve_layer(ctx, layer)
    driver, ext = _FORMATS[format.lower()]
    if not filename.lower().endswith(ext):
        filename += ext
    path = output_path(ctx, filename, overwrite)
    if isinstance(lyr, QgsRasterLayer):
        writer = QgsRasterFileWriter(path)
        pipe = QgsRasterPipe()
        pipe.set(lyr.dataProvider().clone())
        error = writer.writeRaster(pipe, lyr.width(), lyr.height(), lyr.extent(), lyr.crs())
        if error != 0:
            raise ToolError(f"Raster export failed (code {error}).")
        return {"layer": lyr.name(), "path": path}
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = driver
    options.onlySelectedFeatures = bool(selected_only)
    if driver == "GPKG":
        options.layerName = safe_filename(lyr.name())
    if crs:
        options.ct = QgsCoordinateTransform(lyr.crs(), parse_crs(crs), ctx.project)
    result = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, path, ctx.project.transformContext(), options)
    if result[0] != QgsVectorFileWriter.NoError:
        raise ToolError(f"Export failed: {result[1]}")
    return {"layer": lyr.name(), "path": path, "format": driver}
