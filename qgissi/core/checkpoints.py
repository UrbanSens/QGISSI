"""Undo support: a checkpoint records what one agent run changes.

Before the first change of a run the checkpoint stores the layer list and
each layer's name, visibility and full style. Before a destructive tool or
agent code touches vector data, it snapshots that data through the data
provider: the field definitions and every feature. Restoring through the
provider works the same for memory layers, GeoPackage, Shapefile or PostGIS
and avoids copying files that are still open (a GeoPackage keeps recent
changes in its write-ahead log, so a file copy would not undo them).

``undo()`` removes layers the run added, restores layers it removed, puts
styles back and restores the snapshotted data and schema.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from qgis.core import QgsFeature, QgsField, QgsMapLayerStyle, QgsProject, QgsVectorDataProvider, QgsVectorLayer

from ..tools.registry import Risk

MAX_SNAPSHOT_FEATURES = 250_000


class RunCheckpoint:
    def __init__(self, project: QgsProject, backup_root: str = "", run_id: str = "") -> None:
        self.project = project
        self.run_id = run_id
        self.started = False
        self.layers_before: set = set()
        self.layer_state: Dict[str, Tuple[str, str, bool]] = {}   # id -> (name, style xml, visible)
        self.removed: Dict[str, object] = {}
        self.snapshots: Dict[str, Tuple[List[QgsField], List[QgsFeature]]] = {}
        self.backed_up: set = set()
        self.warnings: List[str] = []

    # --------------------------------------------------------------- record
    def begin(self) -> None:
        if self.started:
            return
        self.started = True
        root = self.project.layerTreeRoot()
        for layer_id, layer in self.project.mapLayers().items():
            self.layers_before.add(layer_id)
            style = QgsMapLayerStyle()
            style.readFromLayer(layer)
            node = root.findLayer(layer_id)
            self.layer_state[layer_id] = (layer.name(), style.xmlData(), bool(node and node.itemVisibilityChecked()))

    def before_change(self, definition, args: Dict) -> None:
        self.begin()
        if definition.risk == Risk.CODE:
            for layer in self.project.mapLayers().values():
                self.backup_layer(layer)
        elif definition.risk == Risk.DESTRUCTIVE:
            for key in ("layer", "input", "target_layer"):
                ref = args.get(key)
                layer = self._find(ref) if isinstance(ref, str) else None
                if layer is not None:
                    self.backup_layer(layer)

    def _find(self, ref: str):
        layer = self.project.mapLayer(ref)
        if layer is None:
            matches = [lyr for lyr in self.project.mapLayers().values() if lyr.name().lower() == ref.lower()]
            layer = matches[0] if matches else None
        return layer

    def backup_layer(self, layer) -> None:
        if layer is None or layer.id() in self.backed_up or not isinstance(layer, QgsVectorLayer):
            return
        self.backed_up.add(layer.id())
        caps = int(layer.dataProvider().capabilities())
        if not caps & int(QgsVectorDataProvider.DeleteFeatures) or not caps & int(QgsVectorDataProvider.AddFeatures):
            return  # read-only sources cannot be changed by the agent either
        if layer.featureCount() > MAX_SNAPSHOT_FEATURES:
            self.warnings.append(f"'{layer.name()}' is too large to snapshot; data edits to it cannot be undone.")
            return
        fields = [QgsField(f) for f in layer.dataProvider().fields()]
        self.snapshots[layer.id()] = (fields, [QgsFeature(f) for f in layer.dataProvider().getFeatures()])

    def take_layer(self, layer) -> None:
        """Remove a layer from the project but keep it alive so undo can restore it."""
        self.begin()
        self.removed[layer.id()] = self.project.takeMapLayer(layer)

    # ----------------------------------------------------------------- undo
    @property
    def has_changes(self) -> bool:
        return self.started

    @staticmethod
    def _restore_data(layer: QgsVectorLayer, fields: List[QgsField], features: List[QgsFeature]) -> None:
        if layer.isEditable():
            layer.rollBack()
        provider = layer.dataProvider()
        original = {f.name() for f in fields}
        extra = [i for i, f in enumerate(provider.fields()) if f.name() not in original]
        if extra:
            provider.deleteAttributes(extra)
        current = {f.name() for f in provider.fields()}
        missing = [f for f in fields if f.name() not in current]
        if missing:
            provider.addAttributes(missing)
        layer.updateFields()
        if not provider.truncate():
            provider.deleteFeatures([f.id() for f in provider.getFeatures()])
        target = provider.fields()
        restored = []
        for old in features:
            feature = QgsFeature(target)
            for index, field in enumerate(old.fields()):
                position = target.indexOf(field.name())
                if position >= 0:
                    feature.setAttribute(position, old.attribute(index))
            if old.hasGeometry():
                feature.setGeometry(old.geometry())
            restored.append(feature)
        provider.addFeatures(restored)
        layer.updateExtents()
        layer.reload()

    def undo(self) -> List[str]:
        messages: List[str] = []
        if not self.started:
            return ["Nothing to undo."]
        added = [lid for lid in self.project.mapLayers() if lid not in self.layers_before]
        if added:
            names = [self.project.mapLayer(lid).name() for lid in added]
            self.project.removeMapLayers(added)
            messages.append(f"Removed {len(added)} layer(s) added by the run: {', '.join(names)}")
        for layer_id, layer in list(self.removed.items()):
            if layer is not None and self.project.mapLayer(layer_id) is None:
                self.project.addMapLayer(layer)
                messages.append(f"Restored removed layer '{layer.name()}'")
        self.removed.clear()
        root = self.project.layerTreeRoot()
        for layer_id, (name, xml, visible) in self.layer_state.items():
            layer = self.project.mapLayer(layer_id)
            if layer is None:
                continue
            if layer_id in self.snapshots and isinstance(layer, QgsVectorLayer):
                fields, features = self.snapshots[layer_id]
                self._restore_data(layer, fields, features)
                messages.append(f"Restored data of '{name}'")
            if layer.name() != name:
                layer.setName(name)
            QgsMapLayerStyle(xml).writeToLayer(layer)
            node = root.findLayer(layer_id)
            if node is not None:
                node.setItemVisibilityChecked(visible)
            layer.triggerRepaint()
        messages.extend(self.warnings)
        self.started = False
        return messages
