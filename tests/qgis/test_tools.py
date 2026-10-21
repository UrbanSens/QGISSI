"""Tool behaviour on the fixture project."""
import os

from .conftest import call


def test_layer_lookup_is_forgiving_but_precise(ctx):
    assert call(ctx, "count_features", layer="SCHOOLS").data["count"] == 8
    missing = call(ctx, "count_features", layer="schol")
    assert missing.is_error and "Did you mean: schools" in missing.text


def test_processing_degree_guard_and_enum_labels(ctx):
    guarded = call(ctx, "run_processing_algorithm", algorithm_id="native:buffer",
                   parameters={"INPUT": "wells", "DISTANCE": 100})
    assert guarded.is_error and "degrees" in guarded.text
    ok = call(ctx, "run_processing_algorithm", algorithm_id="native:buffer", output_name="b",
              parameters={"input": "schools", "DISTANCE": 100, "END_CAP_STYLE": "Square"})
    assert not ok.is_error and ok.data["outputs"]["OUTPUT"]["features"] == 8


def test_optional_primary_output_is_still_produced(ctx):
    result = call(ctx, "run_processing_algorithm", algorithm_id="native:joinattributestable", output_name="joined",
                  parameters={"INPUT": "districts", "FIELD": "district_id", "INPUT_2": "census_table",
                              "FIELD_2": "district_id"})
    assert result.data["outputs"]["OUTPUT"]["features"] == 6


def test_wrong_field_name_is_explained(ctx):
    result = call(ctx, "run_processing_algorithm", algorithm_id="native:dissolve",
                  parameters={"INPUT": "buildings", "FIELD": ["kind"]})
    assert result.is_error and "Fields:" in result.text


def test_code_tool_guard_tripwire_and_deadline(ctx):
    assert call(ctx, "run_pyqgis", purpose="ok", code="result = layer('schools').featureCount()").text.startswith("result: 8")
    assert "safety guard" in call(ctx, "run_pyqgis", purpose="x", code="import socket").text
    assert "safety guard" in call(ctx, "run_pyqgis", purpose="x", code="import threading").text
    outside = os.path.join(os.path.expanduser("~"), "qgissi_tripwire_test.txt")
    blocked = call(ctx, "run_pyqgis", purpose="x", code=f"import os\nopen({outside!r}, 'w')")
    assert blocked.is_error and "Tripwire" in blocked.text and not os.path.exists(outside)
    slow = call(ctx, "run_pyqgis", purpose="x", code="while True:\n    pass", timeout_s=5)
    assert slow.is_error and "time limit" in slow.text


def test_checkpoint_undo_restores_project(ctx, project, tmp_path):
    from qgissi.core.checkpoints import RunCheckpoint
    from qgissi.tools import REGISTRY
    ctx.checkpoint = RunCheckpoint(project, str(tmp_path / "backups"), "t")
    for name, args in (("set_categorized_style", {"layer": "buildings", "field": "type"}),
                       ("calculate_field", {"layer": "parks", "field": "area_m2", "expression": "$area"}),
                       ("delete_features", {"layer": "buildings", "where": "\"levels\" > 3"}),
                       ("remove_layer", {"layer": "trees"}),
                       ("create_layer", {"name": "scratch", "geometry_type": "point"})):
        ctx.checkpoint.before_change(REGISTRY.get(name), args)
        assert not call(ctx, name, **args).is_error
    messages = ctx.checkpoint.undo()
    names = sorted(layer.name() for layer in project.mapLayers().values())
    assert "scratch" not in names and "trees" in names, messages
    buildings = project.mapLayersByName("buildings")[0]
    parks = project.mapLayersByName("parks")[0]
    assert buildings.renderer().type() == "singleSymbol"
    assert buildings.featureCount() == 320 and sum(1 for _ in buildings.getFeatures()) == 320
    assert "area_m2" not in [f.name() for f in parks.fields()]


def test_render_and_layout_export(ctx):
    image = call(ctx, "capture_map_image", width=200, height=150)
    assert image.images and image.images[0][0] == "image/png"
    assert not call(ctx, "create_print_layout", name="L", title="Test").is_error
    exported = call(ctx, "export_print_layout", name="L", filename="l.pdf")
    assert exported.data["exists"]


def test_raster_and_vector_styles(ctx):
    for style in ("pseudocolor", "hillshade", "grayscale"):
        result = call(ctx, "set_raster_style", layer="dem", style=style, color_ramp="Viridis")
        assert not result.is_error, result.text
    for args in (("set_single_symbol_style", {"layer": "parks", "color": "#2b8a3e", "outline_color": "black"}),
                 ("set_graduated_style", {"layer": "districts", "field": "population", "classes": 4}),
                 ("set_heatmap_style", {"layer": "trees"}),
                 ("set_layer_opacity", {"layer": "dem", "opacity": 0.5}),
                 ("set_labels", {"layer": "districts", "text": "\"name\" || ' ' || \"population\""})):
        result = call(ctx, args[0], **args[1])
        assert not result.is_error, (args[0], result.text)


def _victim(tmp_path_factory_dir):
    path = os.path.join(tmp_path_factory_dir, "victim.txt")
    with open(path, "w") as fh:
        fh.write("original")
    return path


def test_tripwire_blocks_reported_bypasses(ctx):
    import tempfile
    outside = tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix=".qgissi_victim_")  # not under output/temp
    try:
        victim = _victim(outside)
        payloads = {
            "replace": f"import os\np = os.path.join(output_dir, 'x.txt')\nopen(p, 'w').write('evil')\nos.replace(p, {victim!r})",
            "truncate": f"import os\nos.truncate({victim!r}, 0)",
            "python_subprocess": "import webbrowser\nwebbrowser.subprocess.run(['python3', '-c', 'print(1)'])",
            "thread": "import webbrowser\nt = webbrowser.threading.Thread(target=print)\nt.start()",
            "shell_chain": "import webbrowser\nwebbrowser.subprocess.run('gdalinfo --version; touch /tmp/qgissi_pwn', shell=True)",
        }
        for name, code in payloads.items():
            result = call(ctx, "run_pyqgis", purpose=name, code=code)
            assert result.is_error, (name, result.text)
            assert "Tripwire" in result.text or "safety guard" in result.text, (name, result.text)
        with open(victim) as fh:
            assert fh.read() == "original"
        assert not os.path.exists("/tmp/qgissi_pwn")
    finally:
        import shutil
        shutil.rmtree(outside, ignore_errors=True)


def test_system_exit_never_escapes(ctx):
    guarded = call(ctx, "run_pyqgis", purpose="x", code="raise SystemExit('stop')")
    assert guarded.is_error and "SystemExit" in guarded.text
    indirect = call(ctx, "run_pyqgis", purpose="x",
                    code="import builtins\ne = getattr(builtins, 'System' + 'Exit')\nraise e('stop')")
    assert indirect.is_error and "SystemExit" in indirect.text


def test_gdal_algorithm_still_runs_from_code(ctx):
    code = ("import os\nr = processing.run('gdal:translate', {'INPUT': layer('dem'), "
            "'OUTPUT': os.path.join(output_dir, 'dem_copy.tif')})\nresult = os.path.exists(r['OUTPUT'])")
    result = call(ctx, "run_pyqgis", purpose="copy dem", code=code)
    assert not result.is_error and "result: True" in result.text, result.text


def test_in_place_algorithms_and_overwrites_are_destructive(ctx):
    from qgissi.tools import REGISTRY
    tool = REGISTRY.get("run_processing_algorithm")
    assert tool.effective_risk({"algorithm_id": "native:truncatetable", "parameters": {"INPUT": "roads"}}) == "destructive"
    assert tool.effective_risk({"algorithm_id": "native:buffer", "parameters": {}}) == "write"
    assert tool.effective_risk({"algorithm_id": "native:buffer", "parameters": {}, "overwrite": True}) == "destructive"
    first = call(ctx, "run_processing_algorithm", algorithm_id="native:buffer",
                 parameters={"INPUT": "schools", "DISTANCE": 10, "OUTPUT": "b.gpkg"})
    assert not first.is_error
    again = call(ctx, "run_processing_algorithm", algorithm_id="native:buffer",
                 parameters={"INPUT": "schools", "DISTANCE": 10, "OUTPUT": "b.gpkg"})
    assert again.is_error and "already exists" in again.text
    assert REGISTRY.get("export_layer").effective_risk({"overwrite": True}) == "destructive"


def _run(ctx, checkpoint, tool_name, **args):
    from qgissi.tools import REGISTRY
    definition = REGISTRY.get(tool_name)
    checkpoint.before_change(definition, args, definition.effective_risk(args))
    result = call(ctx, tool_name, **args)
    assert not result.is_error, result.text
    return result


def test_undo_keeps_the_users_unsaved_edits(ctx, project):
    from qgissi.core.checkpoints import RunCheckpoint
    parks = project.mapLayersByName("parks")[0]
    parks.startEditing()
    first = next(parks.getFeatures())
    parks.changeAttributeValue(first.id(), parks.fields().indexOf("name"), "User's park")  # pending, unsaved
    checkpoint = RunCheckpoint(project)
    checkpoint.begin()
    _run(ctx, checkpoint, "calculate_field", layer="parks", field="area_m2", expression="$area")
    checkpoint.finish()
    checkpoint.undo()
    assert parks.isEditable() and parks.isModified()
    assert parks.getFeature(first.id())["name"] == "User's park"
    assert "area_m2" not in [f.name() for f in parks.fields()]
    parks.rollBack()


def test_undo_reverts_only_the_last_run(ctx, project):
    from qgis.core import QgsVectorLayer

    from qgissi.core.checkpoints import RunCheckpoint
    first_run = RunCheckpoint(project)
    first_run.begin()
    _run(ctx, first_run, "calculate_field", layer="parks", field="area_m2", expression="$area")
    first_run.finish()
    second_run = RunCheckpoint(project)
    second_run.begin()
    _run(ctx, second_run, "create_layer", name="from_run_two", geometry_type="point")
    second_run.finish()
    assert second_run.has_changes
    manual = QgsVectorLayer("Point?crs=EPSG:25832", "added_by_user_later", "memory")
    project.addMapLayer(manual)
    second_run.undo()
    names = {layer.name() for layer in project.mapLayers().values()}
    assert "from_run_two" not in names and "added_by_user_later" in names
    assert "area_m2" in [f.name() for f in project.mapLayersByName("parks")[0].fields()]  # first run untouched


def test_destructive_processing_is_snapshotted_and_undone(ctx, project):
    from qgissi.core.checkpoints import RunCheckpoint
    roads = project.mapLayersByName("roads")[0]
    count = roads.featureCount()
    checkpoint = RunCheckpoint(project)
    checkpoint.begin()
    _run(ctx, checkpoint, "run_processing_algorithm", algorithm_id="native:truncatetable", parameters={"INPUT": "roads"})
    assert roads.featureCount() == 0
    checkpoint.finish()
    checkpoint.undo()
    assert roads.featureCount() == count
