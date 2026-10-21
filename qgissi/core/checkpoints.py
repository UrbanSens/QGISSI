"""Undo support: a checkpoint records what one agent run changes.

Before the first change of a run the checkpoint stores the layer list, each
layer's name, visibility and full style. Before destructive tools or code it
also backs up layer data: features of memory layers, and the files of
file-based vector layers (GeoPackage, Shapefile, GeoJSON, ...). ``undo()``
removes layers the run added, restores layers it removed, puts styles back
and restores backed-up data.
"""
from __future__ import annotations

import glob
import os
import shutil
from typing import Dict, List, Optional, Tuple

from qgis.core import QgsFeature, QgsMapLayerStyle, QgsProject, QgsVectorLayer

from ..tools.registry import Risk

MAX_MEMORY_FEATURES = 250_000
MAX_FILE_BYTES = 400 * 1024 * 1024
_SIDECARS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbn", ".sbx", ".qix")


def layer_file_path(layer) -> Optional[str]:
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
        return None
    path = layer.source().split("|")[0]
    return path if os.path.isfile(path) else None


class RunCheckpoint:
    def __init__(self, project: QgsProject, backup_root: str, run_id: str) -> None:
        self.project = project
        self.backup_dir = os.path.join(backup_root, run_id)
        self.run_id = run_id
        self.started = False
        self.layers_before: set = set()
        self.layer_state: Dict[str, Tuple[str, str, bool]] = {}   # id -> (name, style xml, visible)
        self.removed: Dict[str, object] = {}
        self.memory: Dict[str, List[QgsFeature]] = {}
        self.memory_fields: Dict[str, List[str]] = {}
        self.files: List[Tuple[str, str]] = []                    # (original, backup)
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
        if layer.providerType() == "memory":
            if layer.featureCount() <= MAX_MEMORY_FEATURES:
                self.memory[layer.id()] = [QgsFeature(f) for f in layer.getFeatures()]
                self.memory_fields[layer.id()] = [f.name() for f in layer.fields()]
            else:
                self.warnings.append(f"'{layer.name()}' is too large to back up; its edits cannot be undone.")
            return
        path = layer_file_path(layer)
        if not path:
            self.warnings.append(f"'{layer.name()}' ({layer.providerType()}) cannot be backed up.")
            return
        stem, ext = os.path.splitext(path)
        paths = [p for p in glob.glob(stem + ".*") if os.path.splitext(p)[1].lower() in _SIDECARS] \
            if ext.lower() == ".shp" else [path] + [p for p in (path + "-wal", path + "-shm") if os.path.exists(p)]
        if sum(os.path.getsize(p) for p in paths) > MAX_FILE_BYTES:
            self.warnings.append(f"'{layer.name()}' is too large to back up; its edits cannot be undone.")
            return
        os.makedirs(self.backup_dir, exist_ok=True)
        for original in paths:
            if any(o == original for o, _ in self.files):
                continue
            backup = os.path.join(self.backup_dir, f"{len(self.files)}_{os.path.basename(original)}")
            shutil.copy2(original, backup)
            self.files.append((original, backup))

    def take_layer(self, layer) -> None:
        """Remove a layer from the project but keep it alive so undo can restore it."""
        self.begin()
        self.removed[layer.id()] = self.project.takeMapLayer(layer)

    # ----------------------------------------------------------------- undo
    @property
    def has_changes(self) -> bool:
        return self.started

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
        for original, backup in self.files:
            shutil.copy2(backup, original)
        if self.files:
            messages.append(f"Restored {len(self.files)} data file(s)")
        root = self.project.layerTreeRoot()
        for layer_id, (name, xml, visible) in self.layer_state.items():
            layer = self.project.mapLayer(layer_id)
            if layer is None:
                continue
            if layer_id in self.memory and isinstance(layer, QgsVectorLayer):
                if layer.isEditable():
                    layer.rollBack()
                provider = layer.dataProvider()
                original = self.memory_fields.get(layer_id, [])
                extra = [i for i, f in enumerate(layer.fields()) if f.name() not in original]
                if extra:
                    provider.deleteAttributes(extra)
                    layer.updateFields()
                provider.truncate()
                provider.addFeatures(self.memory[layer_id])
                layer.updateExtents()
                messages.append(f"Restored features of '{name}'")
            elif any(layer_file_path(layer) == o for o, _ in self.files):
                if layer.isEditable():
                    layer.rollBack()
                layer.dataProvider().reloadData()
                layer.reload()
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
