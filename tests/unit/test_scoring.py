"""Parity with google-research/geospatial-reasoning-benchmarks (c21a6d9) test vectors."""
import pytest

from qgissi.bench.scoring import (bootstrap_ci, extract_answer_list, parse_rating, render_autorater_prompt,
                                  rouge_l_f1, score_structured_answer, summarize)

STRUCTURED = [
    ("[100, \"abc\"]", "[100, \"abc\"]", 1.0), ("[10, 20]", "[10]", 0.5), ("[10, 20]", "[10, 20, 30]", 0.67),
    ("[[1, 2], \"abc\"]", "[[1, 2], \"abc\"]", 1.0), ("[[1, 2], \"abc\"]", "[[1, 3], \"abc\"]", 0.875),
    ("[[1, 2]]", "[\"abc\"]", 0.0), ("[[100, 200]]", "[[105, 190]]", 1.0), ("[[1, 2, 3]]", "[[1, 2]]", 0.67),
    ("[[1, 2]]", "[[1, 2, 3]]", 0.67), ("[10]", "[\"the value is 10.5\"]", 1.0), ("[1020]", "[[10, 20]]", 0.0),
    ("[[]]", "[[]]", 1.0), ("[[[1]]]", "[[[1]]]", 1.0),
    # behaviour shared with the legacy scorer
    ("[100]", "[111]", 0.89), ("[\"fairfax\"]", "[\"fairfax county\"]", 1.0), ("[\"Abc\"]", "[\"abc\"]", 1.0),
    ("[10]", "The result is [10].", 1.0), ("[10]", "```json\n[10]\n```", 1.0), ("[10]", "", 0.0),
    ("[10]", "[10,,]", 0.0), ("[\"Salem\"]", "[\"Town of Salem\"]", 1.0), ("[0]", "[0.0001]", 0.0),
]


@pytest.mark.parametrize("gold,response,expected", STRUCTURED)
def test_structured_answer_matches_upstream(gold, response, expected):
    assert score_structured_answer(gold, response) == pytest.approx(expected, abs=0.005)


def test_invalid_gold_raises():
    with pytest.raises(ValueError):
        score_structured_answer("not json", "[1]")


RATINGS = [("2", 2), ("The plan is sound.\n\n1", 1), ("Reasoning...\nFinal score: **2**", 2), ("Final answer: `1`.", 1),
           ("{\"score\": 0}", 0), ("{\"rating\": \"2\"}", 2), ("{\"score\": 5, \"rating\": 1}", 1),
           ("```json\n{\"score\": 2}\n```", 2), ("Score: 2\nThe agent used the right data.", 2),
           ("1\n\nDetails follow.", 1), ("Rating = 0 - plan only", 0), ("2.0", 2), ("no rating here", None),
           ("", None), ("The score is 3", None), ("Score: 2/3 of the dimensions", None), ("version 1.2", None)]


@pytest.mark.parametrize("reply,expected", RATINGS)
def test_parse_rating_matches_upstream(reply, expected):
    assert parse_rating(reply) == expected


def test_rouge_l():
    assert rouge_l_f1("apple orange", "apple") == pytest.approx(2 / 3)
    assert rouge_l_f1("", "x") == 0.0


def test_extract_answer_list_prefers_matching_length():
    assert extract_answer_list("Using [meters] units. Final: [\"Ohio\", 3.5]", 2) == ("[\"Ohio\", 3.5]", True)
    assert extract_answer_list("no list", 1) == (None, False)
    assert extract_answer_list("```json\n[1, 2, 3]\n```", 2) == ("[1, 2, 3]", False)


def test_render_autorater_prompt_drops_tools_section_and_dates():
    template = "Q: {{ model_prompt }}\nGold: {{ golden_plan }}\nRun: {{ candidate_workflow }}\n\nAvailable Tools:\n{{ tool_manifest }}"
    case = {"query": "q?", "golden_workflow_raw": "plan"}
    text = render_autorater_prompt(template, case, "trace", date_header=True, tool_manifest=None, today="2026-10-21")
    assert text.startswith("Today is 2026-10-21.") and "Available Tools" not in text and "Run: trace" in text
    with_tools = render_autorater_prompt(template, case, "trace", date_header=False, tool_manifest="- a(): b")
    assert with_tools.endswith("- a(): b")


def test_bootstrap_ci_is_deterministic():
    values = [0, 1, 1, 0.5, 1]
    assert bootstrap_ci(values) == bootstrap_ci(values)
    assert bootstrap_ci([1.0, 1.0]) == (1.0, 1.0)


def test_summarize_groups_by_model():
    base = {"category": "vector", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
                                           "cache_write_tokens": 0}, "wall_s": 1, "steps": 2, "tool_calls": 2,
            "tool_errors": 0, "status": "completed"}
    rows = summarize([{**base, "model_spec": "a", "score": 1.0, "passed": True, "cost_usd": 0.1},
                      {**base, "model_spec": "a", "score": 0.0, "passed": False, "cost_usd": 0.1},
                      {**base, "model_spec": "b", "score": 1.0, "passed": True, "cost_usd": None}])
    assert [r["model"] for r in rows] == ["b", "a"]
    assert rows[1]["score"] == 0.5 and rows[1]["cost_usd"] == 0.2 and rows[0]["cost_usd"] is None
