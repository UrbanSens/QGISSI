"""Scoring utilities (no third-party dependencies).

``score_structured_answer`` re-implements the deterministic GeoKTools scorer
of google-research/geospatial-reasoning-benchmarks (commit c21a6d9,
geoktools/scoring/scoring_functions.py) so it can run inside QGIS's Python
without numpy or rouge-score; tests check it against the upstream test
vectors. ``parse_rating`` mirrors the GeoExpertQueries autorater parser.
"""
from __future__ import annotations

import json
import random
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ------------------------------------------------------------------ ROUGE-L
def _tokenize(text: str) -> List[str]:
    """rouge_score's default tokenizer: lowercase, non [a-z0-9] -> space, split."""
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return [t for t in text.split() if t]


def _lcs(a: Sequence[str], b: Sequence[str]) -> int:
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0]
        for j, y in enumerate(b, 1):
            current.append(previous[j - 1] + 1 if x == y else max(previous[j], current[j - 1]))
        previous = current
    return previous[-1]


def rouge_l_f1(target: str, prediction: str) -> float:
    t, p = _tokenize(target), _tokenize(prediction)
    if not t or not p:
        return 0.0
    lcs = _lcs(t, p)
    if lcs == 0:
        return 0.0
    precision, recall = lcs / len(p), lcs / len(t)
    return 2 * precision * recall / (precision + recall)


# --------------------------------------------------------------- GeoKTools
def _strip_stopwords(text: str) -> str:
    text = text.strip()
    text = re.sub(r" (county|parish|census area).*", "", text)
    text = re.sub(r"(city|town) of ", "", text)
    return text.replace("-", "")


def _score_strings(gold: str, result: str) -> float:
    return rouge_l_f1(_strip_stopwords(gold), _strip_stopwords(result))


def _score_numeric(gold: float, result: Any, tolerance_ratio: float) -> float:
    if not isinstance(result, (int, float)):
        if not isinstance(result, str):
            return 0.0
        matches = re.findall(r"-?\d+\.?\d*|\.\d+", result)
        if not matches:
            return 0.0
        result = float(matches[0])
    if gold == 0:
        return float(result == 0)
    if abs(result - gold) <= abs(gold * tolerance_ratio):
        return 1.0
    return max(1 - abs((result - gold) / gold), 0.0)


def _score_nested(gold: Any, result: Any, tolerance_ratio: float) -> float:
    if isinstance(gold, list):
        if not isinstance(result, list) or not gold:
            return 1.0 if gold == result else 0.0
        scores = []
        max_len = max(len(gold), len(result))
        for i in range(max_len):
            gold_item = gold[i] if i < len(gold) else None
            result_item = result[i] if i < len(result) else None
            scores.append(_score_nested(gold_item, result_item, tolerance_ratio) if gold_item is not None else 0.0)
        return sum(scores) / max_len
    if isinstance(gold, (int, float)):
        return _score_numeric(gold, result, tolerance_ratio)
    return _score_strings(str(gold), str(result) if result is not None else "")


def score_structured_answer(gold_response: str, response: str, tolerance_ratio: float = 0.1) -> float:
    try:
        gold = json.loads(gold_response.lower())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid gold response: {exc}") from exc
    result = None
    if response:
        match = re.search(r"(\[.+\])", response, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0).lower())
            except ValueError:
                result = None
    if gold is None or result is None or not isinstance(gold, list) or not isinstance(result, list):
        return 0.0
    return _score_nested(gold, result, tolerance_ratio)


def extract_answer_list(text: str, blanks: int) -> Tuple[Optional[str], bool]:
    """Last JSON list in ``text`` with ``blanks`` elements (else the last list that parses).

    Returns (json_text, ok). Avoids the scorer's first-'['-to-last-']' trap when
    the agent mentions brackets elsewhere in its answer.
    """
    text = re.sub(r"```(?:json)?", "", text or "")
    candidates: List[list] = []
    for start in [m.start() for m in re.finditer(r"\[", text)]:
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "[":
                depth += 1
            elif text[end] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:end + 1])
                    except ValueError:
                        value = None
                    if isinstance(value, list):
                        candidates.append(value)
                    break
    if not candidates:
        return None, False
    exact = [c for c in candidates if len(c) == blanks]
    chosen = exact[-1] if exact else candidates[-1]
    return json.dumps(chosen, ensure_ascii=False), bool(exact)


# --------------------------------------------------------- GeoExpertQueries
# Same rules and order as geoexpertqueries/autorater/run_autorater.py (c21a6d9).
_END_SCORE = re.compile(r'(?<![\w./])([012])(?:\.0)?[\s."\'}\])`]*$')
_START_SCORES = (
    re.compile(r'^[(\[`"\']*(?:scores?|ratings?)\s*[:=-]?\s*([012])(?:\.0)?\b(?!\.\d|\s*/)', re.IGNORECASE),
    re.compile(r'^[(\[`"\']*([012])(?:\.0)?[ \t."\'}\])`]*(?:\r?\n|$)'),
)
_PLACEHOLDER = re.compile(r"\{\{\s*(model_prompt|golden_plan|candidate_workflow|tool_manifest)\s*\}\}")
_TOOLS_SECTION = re.compile(r"\n*Available Tools:[ \t]*\n*\{\{\s*tool_manifest\s*\}\}")
_DATE_HEADER = ("Today is {date}. Even though this might seem to be in the future for you, "
                "keep in mind you are doing inference well past your training date.\n\n")
NORMALIZED = {0: 0.0, 1: 0.5, 2: 1.0}


def parse_rating(reply: Optional[str]) -> Optional[int]:
    text = (reply or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("score", "rating", "Score", "Rating"):
            if key in data:
                try:
                    value = float(data[key])
                except (TypeError, ValueError):
                    break
                if value in (0, 1, 2):
                    return int(value)
    clean = text.replace("*", "").strip()
    match = _END_SCORE.search(clean)
    if match:
        return int(match.group(1))
    for pattern in _START_SCORES:
        match = pattern.match(clean)
        if match:
            return int(match.group(1))
    return None


def render_autorater_prompt(template: str, case: Dict[str, Any], trace: str, date_header: bool = True,
                            tool_manifest: Optional[str] = None, today: Optional[str] = None) -> str:
    if not tool_manifest:
        template = _TOOLS_SECTION.sub("", template)
    values = {"model_prompt": case["query"], "golden_plan": case["golden_workflow_raw"],
              "candidate_workflow": trace, "tool_manifest": tool_manifest or ""}
    body = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
    if date_header:
        import datetime
        body = _DATE_HEADER.format(date=today or datetime.date.today()) + body
    return body


# ------------------------------------------------------------------ stats
def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def bootstrap_ci(values: Sequence[float], n_boot: int = 2000, confidence: float = 0.95,
                 seed: int = 0) -> Tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    lo = means[int((1 - confidence) / 2 * n_boot)]
    hi = means[min(n_boot - 1, int((1 + confidence) / 2 * n_boot))]
    return (round(lo, 4), round(hi, 4))


def summarize(records: List[Dict[str, Any]], key: str = "model_spec") -> List[Dict[str, Any]]:
    """Per-model aggregates for QGISSI-Bench records."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(record[key], []).append(record)
    rows = []
    for name, items in groups.items():
        scores = [r["score"] for r in items]
        costs = [r["cost_usd"] for r in items if r.get("cost_usd") is not None]
        by_category: Dict[str, List[float]] = {}
        for record in items:
            by_category.setdefault(record["category"], []).append(record["score"])
        usage_in = sum(r["usage"]["input_tokens"] + r["usage"]["cache_read_tokens"] + r["usage"]["cache_write_tokens"]
                       for r in items)
        usage_out = sum(r["usage"]["output_tokens"] for r in items)
        walls = sorted(r["wall_s"] for r in items)
        rows.append({
            "model": name, "runs": len(items), "score": round(mean(scores), 4), "ci95": bootstrap_ci(scores),
            "pass_rate": round(mean(1.0 if r["passed"] else 0.0 for r in items), 4),
            "by_category": {c: round(mean(v), 3) for c, v in sorted(by_category.items())},
            "cost_usd": round(sum(costs), 4) if len(costs) == len(items) else None,
            "cost_per_task": round(sum(costs) / len(items), 4) if len(costs) == len(items) and items else None,
            "input_tokens": usage_in, "output_tokens": usage_out,
            "median_wall_s": walls[len(walls) // 2] if walls else 0,
            "mean_steps": round(mean(r["steps"] for r in items), 2),
            "tool_error_rate": round(sum(r["tool_errors"] for r in items) / max(1, sum(r["tool_calls"] for r in items)), 3),
            "errors": sum(1 for r in items if r["status"] == "error"),
            "refusals": sum(1 for r in items if r["status"] == "refusal"),
            "fallbacks": sum(1 for r in items if r.get("fallback_used")),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows
