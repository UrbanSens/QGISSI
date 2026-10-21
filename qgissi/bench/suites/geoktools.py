"""GeoKTools adapter: run QGISSI on the dev split and score it.

Scoring uses the verified port of the official deterministic scorer; with
``--official-python`` the official ``run_scoring`` is run on the same
predictions too (that interpreter needs numpy and rouge-score).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from ..scoring import bootstrap_ci, extract_answer_list, mean, score_structured_answer
from .georeason import fetch, load_cases

# Templates whose answers can be reproduced from open data (unofficial diagnostic,
# see docs/BENCHMARKS.md); the rest depend on Google Maps POIs or a gated API.
OPEN_TEMPLATES = {
    "k1_location_extreme_coordinate", "k1_datacommons_variable_ratio",
    "k2_climate_risk_ee_admin_days_above_temp", "k2_climate_risk_ee_admin_water_stress",
    "k2_climate_risk_buffer_temp_diff", "k2_climate_risk_buffer_water_stress",
    "k2_climate_risk_city_buffer_water_stress_diff", "k3_places_dc_location_farthest_from_landmark",
}


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    scores = [r["score"] for r in rows]
    by_k: Dict[str, Any] = {}
    for k in sorted({r["k_level"] for r in rows}):
        subset = [r["score"] for r in rows if r["k_level"] == k]
        by_k[f"K={k}"] = {"n": len(subset), "accuracy": round(mean(subset), 4), "ci95": bootstrap_ci(subset, 10000)}
    open_scores = [r["score"] for r in rows if r["template_id"] in OPEN_TEMPLATES]
    return {"n": len(rows), "accuracy": round(mean(scores), 4), "ci95": bootstrap_ci(scores, 10000), "by_k_level": by_k,
            "open_subset": {"n": len(open_scores), "accuracy": round(mean(open_scores), 4)} if open_scores else None,
            "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
            "extraction_failed": sum(1 for r in rows if not r["extracted"])}


def run_geoktools(args) -> int:
    from ...headless import start_qgis
    start_qgis()
    from ..runner import make_target
    from .common import official_run, run_query, run_record, select_cases

    root = fetch(args.cache, args.accept_terms)
    cases = load_cases(root, "geoktools")["cases"]
    if args.subset == "open":
        cases = [c for c in cases if c["template_id"] in OPEN_TEMPLATES]
    cases = select_cases(cases, args.ids, args.limit)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"GeoKTools dev: {len(cases)} case(s) x {len(models)} model(s)")
    if args.dry_run:
        return 0
    out_dir = args.out or os.path.join("bench_runs", "geoktools")
    os.makedirs(out_dir, exist_ok=True)
    summaries: Dict[str, Any] = {}
    for spec in models:
        target = make_target(spec, allow_fallback=args.allow_fallback)
        rows: List[Dict[str, Any]] = []
        predictions_path = os.path.join(out_dir, f"predictions_{target.slug}.jsonl")
        with open(predictions_path, "w", encoding="utf-8") as preds:
            for i, case in enumerate(cases, 1):
                work_dir = os.path.join(out_dir, "runs", target.slug, case["query_id"])
                run, _session = run_query(target, case["query"], args, work_dir)
                response, extracted = extract_answer_list(run.final_text, case["query"].count("__"))
                answer = response if response is not None else (run.final_text or "")
                score = score_structured_answer(case["golden_answer_raw"], answer)  # golds are read only here
                preds.write(json.dumps({"query_id": case["query_id"], "response": answer}, ensure_ascii=False) + "\n")
                row = {"query_id": case["query_id"], "k_level": case["k_level"], "template_id": case["template_id"],
                       "score": round(score, 4), "extracted": extracted, **run_record(run)}
                rows.append(row)
                print(f"[{i}/{len(cases)}] {spec:<36} {case['query_id']}  K={case['k_level']}  score={score:.2f}  "
                      f"{row['status']}  ${row['cost_usd'] or 0:.4f}  {row['wall_s']:.0f}s")
        summary = _summary(rows)
        summaries[spec] = summary
        with open(os.path.join(out_dir, f"results_{target.slug}.json"), "w", encoding="utf-8") as fh:
            json.dump({"benchmark": "GeoKTools", "version": "1.0.0", "scorer": "qgissi port of score_structured_answer",
                       "model": spec, "overall": summary, "per_case": rows}, fh, indent=2)
        if args.official_python:
            code, log = official_run(args.official_python, root, "geoktools.scoring.run_scoring",
                                     ["--predictions", os.path.abspath(predictions_path), "--output",
                                      os.path.abspath(os.path.join(out_dir, f"results_official_{target.slug}.json"))])
            print(f"official scorer exit {code}\n{log}")
    lines = ["# GeoKTools (dev) results", "", "| Model | Score | 95% CI | K=1 | K=2 | K=3 | K=4 | Open subset | Cost |",
             "|---|---|---|---|---|---|---|---|---|"]
    for spec, s in summaries.items():
        ks = [s["by_k_level"].get(f"K={k}", {}).get("accuracy") for k in (1, 2, 3, 4)]
        open_cell = (f"{s['open_subset']['accuracy']:.2f} (n={s['open_subset']['n']})" if s["open_subset"] else "–")
        k_cells = " | ".join("–" if v is None else f"{v:.2f}" for v in ks)
        lines.append(f"| `{spec}` | **{s['accuracy']:.3f}** | {s['ci95'][0]:.2f}–{s['ci95'][1]:.2f} | {k_cells} | "
                     f"{open_cell} | ${s['cost_usd']:.2f} |")
    lines += ["", "Deterministic protocol (10% relative tolerance, 1-MAPE partial credit, ROUGE-L for strings). "
              "Golds use Google Maps data for 41 of 85 cases, so an open-data agent has a ceiling below 1.0; the "
              "open-subset column is an unofficial diagnostic."]
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0
