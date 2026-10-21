"""GeoExpertQueries trace condensing (no QGIS)."""
from types import SimpleNamespace

from qgissi.bench.suites.geoexpert import condense_trace
from qgissi.providers.base import Message


def test_trace_includes_notes_calls_results_and_execution_summary():
    history = [
        Message("user", [{"type": "text", "text": "query"}]),
        Message("assistant", [{"type": "thinking", "thinking": "Plan: reproject, then buffer.", "signature": "s"},
                              {"type": "text", "text": "Starting."},
                              {"type": "tool_use", "id": "t1", "name": "run_processing_algorithm",
                               "input": {"algorithm_id": "native:buffer"}}]),
        Message("user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                          "content": [{"type": "text", "text": "bad CRS"}]}]),
    ]
    run = SimpleNamespace(status="completed", steps=[1, 2], layers_created=[], final_text="Answer: 3",
                          tool_calls=[SimpleNamespace(is_error=True)])
    trace = condense_trace(SimpleNamespace(history=history), run, "anthropic/claude-opus-5-5@medium")
    assert "[NOTE] Plan: reproject, then buffer." in trace and "[NOTE] Starting." in trace
    assert "[1] TOOL run_processing_algorithm" in trace and "-> ERROR: bad CRS" in trace
    assert "[EXECUTION] 1 tool call(s) were executed (1 returned errors)." in trace
    assert trace.endswith("[FINAL ANSWER] Answer: 3")


def test_trace_without_tools_says_so():
    run = SimpleNamespace(status="completed", steps=[1], layers_created=[], final_text="I loaded data…", tool_calls=[])
    trace = condense_trace(SimpleNamespace(history=[Message("user", [])]), run, "ollama/x")
    assert "[EXECUTION] 0 tool call(s) were executed" in trace
