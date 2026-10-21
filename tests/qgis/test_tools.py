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
