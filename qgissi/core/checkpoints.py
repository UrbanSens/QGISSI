"""Undo support: a checkpoint records what one agent run changes.

``begin()`` runs when a run starts. It stores the layer list, each layer's
name, visibility and style and, for layers the user is editing, the position
of their undo stack. Before a destructive tool or agent code touches vector
data, the checkpoint also snapshots that data through the data provider
(field definitions and features). Restoring through the provider works the
same for memory layers, GeoPackage, Shapefile or PostGIS, and avoids copying
files that are still open: a GeoPackage keeps recent changes in its
write-ahead log, so a file copy would not undo them.

``undo()``:

* removes the layers the run created, but not layers added after the run;
* restores removed layers;
* rewinds edit buffers to where the run started, keeping the user's own
  unsaved edits;
* restores snapshotted data as a minimal diff that keeps feature ids;
* puts names, styles and visibility back.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from qgis.core import QgsFeature, QgsField, QgsMapLayerStyle, QgsProject, QgsVectorDataProvider, QgsVectorLayer

from ..tools.registry import Risk

MAX_SNAPSHOT_FEATURES = 250_000
_LAYER_ARGS = ("layer", "input", "target_layer", "layers")


class RunCheckpoint:
    def __init__(self, project: QgsProject, backup_root: str = "", run_id: str = "") -> None:
        self.project = project
        self.run_id = run_id
        self.started = False
        self.changed = False
        self.layers_before: set = set()
        self.layers_after: Optional[set] = None
        self.layer_state: Dict[str, Tuple[str, str, bool]] = {}   # id -> (name, style xml, visible)
        self.undo_index: Dict[str, int] = {}                     # layers in edit mode at the start
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
            if isinstance(layer, QgsVectorLayer) and layer.isEditable() and layer.undoStack() is not None:
                self.undo_index[layer_id] = layer.undoStack().index()

    def before_change(self, definition, args: Dict, risk: Optional[str] = None) -> None:
        self.begin()
        risk = risk or definition.risk
        if risk == Risk.READ:
            return
        self.changed = True
        if risk == Risk.CODE:
            for layer in self.project.mapLayers().values():
                self.backup_layer(layer)
        elif risk == Risk.DESTRUCTIVE:
            for layer in self._referenced_layers(args):
                self.backup_layer(layer)

    def finish(self) -> None:
        """Called when the run ends: remember which layers exist, so undo spares later additions."""
        self.layers_after = set(self.project.mapLayers())

    def _referenced_layers(self, args: Dict) -> List:
        refs: List[str] = []
        for key in _LAYER_ARGS:
            value = args.get(key)
            refs.extend(value if isinstance(value, list) else [value])
        for value in (args.get("parameters") or {}).values():  # Processing parameters
            refs.extend(value if isinstance(value, list) else [value])
        found = []
        for ref in refs:
            layer = self._find(ref) if isinstance(ref, str) else None
            if layer is not None and layer not in found:
                found.append(layer)
        return found

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
        self.changed = True
        self.removed[layer.id()] = self.project.takeMapLayer(layer)

    # ----------------------------------------------------------------- undo
    @property
    def has_changes(self) -> bool:
        return self.changed

    @staticmethod
    def _restore_data(layer: QgsVectorLayer, fields: List[QgsField], features: List[QgsFeature]) -> None:
        """Bring the provider back to the snapshot with the fewest changes, keeping feature ids."""
        provider = layer.dataProvider()
        original = [f.name() for f in fields]
        extra = [i for i, f in enumerate(provider.fields()) if f.name() not in original]
        if extra:
            provider.deleteAttributes(extra)
        present = {f.name() for f in provider.fields()}
        missing = [f for f in fields if f.name() not in present]
        if missing:
            provider.addAttributes(missing)
        layer.updateFields()
        target = provider.fields()
        snapshot = {f.id(): f for f in features}
        current = {f.id(): QgsFeature(f) for f in provider.getFeatures()}
        gone = [fid for fid in current if fid not in snapshot]
        if gone:
            provider.deleteFeatures(gone)
        attribute_changes: Dict[int, Dict[int, object]] = {}
        geometry_changes: Dict[int, object] = {}
        re_add: List[QgsFeature] = []
        for fid, old in snapshot.items():
            cur = current.get(fid)
            if cur is None:
                feature = QgsFeature(target)
                for index, field in enumerate(old.fields()):
                    position = target.indexOf(field.name())
                    if position >= 0:
                        feature.setAttribute(position, old.attribute(index))
                if old.hasGeometry():
                    feature.setGeometry(old.geometry())
                re_add.append(feature)
                continue
            changes = {}
            for index, field in enumerate(old.fields()):
                position = target.indexOf(field.name())
                if position >= 0 and (cur.fields().indexOf(field.name()) < 0
                                      or cur.attribute(field.name()) != old.attribute(index)):
                    changes[position] = old.attribute(index)
            if changes:
                attribute_changes[fid] = changes
            if old.hasGeometry() != cur.hasGeometry() or (old.hasGeometry() and not old.geometry().equals(cur.geometry())):
                geometry_changes[fid] = old.geometry()
        if attribute_changes:
            provider.changeAttributeValues(attribute_changes)
        if geometry_changes:
            provider.changeGeometryValues(geometry_changes)
        if re_add:
            provider.addFeatures(re_add)
        layer.updateExtents()

    def undo(self) -> List[str]:
        messages: List[str] = []
        if not self.started:
            return ["Nothing to undo."]
        created = [lid for lid in self.project.mapLayers()
                   if lid not in self.layers_before and (self.layers_after is None or lid in self.layers_after)]
        if created:
            names = [self.project.mapLayer(lid).name() for lid in created]
            self.project.removeMapLayers(created)
            messages.append(f"Removed {len(created)} layer(s) created by the run: {', '.join(names)}")
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
            if isinstance(layer, QgsVectorLayer):
                if layer_id in self.undo_index and layer.isEditable():
                    layer.undoStack().setIndex(self.undo_index[layer_id])  # keep the user's own edits
                elif layer_id not in self.undo_index and layer.isEditable():
                    layer.rollBack()  # editing was started during the run
                if layer_id in self.snapshots:
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
        self.changed = False
        return messages
