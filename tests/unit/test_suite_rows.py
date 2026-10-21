"""Resumable per-case rows and judge cost of the external suites (no QGIS)."""
import pytest

from qgissi.bench import judge
from qgissi.bench.suites.rows import append_row, load_rows, open_rows, row_cost
from qgissi.providers.base import Message, ModelResponse, Usage, text_block


def test_rows_resume_after_a_torn_write(tmp_path):
    path = str(tmp_path / "rows.jsonl")
    with open_rows(path, resume=False) as fh:
        append_row(fh, {"query_id": "a", "cost_usd": 0.5})
        fh.write('{"query_id": "b", "cost')  # killed mid-write
    assert set(load_rows(path)) == {"a"}
    with open_rows(path, resume=True) as fh:
        append_row(fh, {"query_id": "c", "cost_usd": None, "judge_cost_usd": 0.25})
    rows = load_rows(path)
    assert set(rows) == {"a", "c"}
    assert sum(row_cost(r) for r in rows.values()) == pytest.approx(0.75)


def test_rows_start_afresh_without_resume(tmp_path):
    path = str(tmp_path / "rows.jsonl")
    with open_rows(path, resume=False) as fh:
        append_row(fh, {"query_id": "a"})
    with open_rows(path, resume=False) as fh:
        append_row(fh, {"query_id": "b"})
    assert set(load_rows(path)) == {"b"}
    assert load_rows(str(tmp_path / "missing.jsonl")) == {}


def test_judge_reports_its_cost(monkeypatch):
    class Fake:
        def complete(self, request):
            usage = Usage(input_tokens=1_000_000, output_tokens=100_000)
            return ModelResponse(Message("assistant", [text_block(" Rating: 2 ")]), "end_turn", usage, request.model)

    monkeypatch.setattr(judge, "create_provider", lambda config: Fake())
    reply, cost = judge.ask_with_cost("anthropic/claude-opus-5-5@medium", "prompt")
    assert reply == "Rating: 2" and cost == pytest.approx(4.0 + 2.0)
    assert judge.ask("anthropic/claude-opus-5-5@medium", "prompt") == "Rating: 2"

