"""GeoExpertQueries adapter: run QGISSI, condense its trajectory, judge it.

The judge uses Google's autorater prompt (downloaded at the pinned commit),
rendered and parsed exactly as the official runner does, with any QGISSI
provider as the judge model. With ``--official-python`` the official
``run_autorater`` is run too, calling ``python -m qgissi.bench judge``.
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List

from ..scoring import NORMALIZED, mean, parse_rating, render_autorater_prompt
from .georeason import fetch, load_cases

PROMPTS = {"tool_agnostic": "tool_agnostic_autorater_prompt.txt", "workflow": "workflow_autorater_prompt.txt"}


def condense_trace(session, run, spec: str, limit_chars: int = 60000) -> str:
    from ... import __version__
    lines = [f"[AGENT] QGISSI {__version__} | model={spec} | started={datetime.datetime.utcnow().isoformat()}Z | "
             f"status={run.status} | steps={len(run.steps)}"]
    step = 0
    names: Dict[str, str] = {}
    for message in session.history[1:]:
        for block in message.content:
            kind = block.get("type")
            if message.role == "assistant" and kind == "text" and block.get("text", "").strip():
                lines.append(f"[NOTE] {block['text'].strip()[:1500]}")
            elif kind == "tool_use":
                step += 1
                names[block["id"]] = block["name"]
                args = json.dumps(block.get("input") or {}, ensure_ascii=False)
                if len(args) > 4000:
                    args = args[:3000] + " … " + args[-800:]
                lines.append(f"[{step}] TOOL {block['name']}({args})")
            elif kind == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else " ".join(
                    c.get("text", "") for c in content or [] if c.get("type") == "text")
                text = text.replace("\n", " ")
                if len(text) > 2000:
                    text = text[:1500] + " … " + text[-400:]
                status = "ERROR" if block.get("is_error") else "OK"
                lines.append(f"    -> {status}: {text}")
    if run.layers_created:
        lines.append(f"[ARTIFACTS] layers created: {', '.join(run.layers_created)}")
    errors = sum(1 for c in run.tool_calls if c.is_error)
    lines.append(f"[EXECUTION] {len(run.tool_calls)} tool call(s) were executed ({errors} returned errors).")
    lines.append(f"[FINAL ANSWER] {run.final_text or '(none)'}")
    trace = "\n".join(lines)
    return trace if len(trace) <= limit_chars else trace[:limit_chars - 2000] + "\n…\n" + trace[-1900:]


def run_geoexpert(args) -> int:
    from ...headless import start_qgis
    start_qgis()
    from ..judge import ask
    from ..runner import make_target
    from .common import official_run, run_query, run_record, select_cases, tool_manifest

    root = fetch(args.cache, args.accept_terms)
    cases = select_cases(load_cases(root, "geoexpertqueries")["cases"], args.ids, args.limit)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"GeoExpertQueries dev: {len(cases)} case(s) x {len(models)} model(s); judge {args.judge} x{args.num_runs}")
    if args.dry_run:
        return 0
    with open(os.path.join(root, "geoexpertqueries", "autorater", PROMPTS[args.prompt]), encoding="utf-8") as fh:
        template = fh.read()
    manifest = tool_manifest()
    out_dir = args.out or os.path.join("bench_runs", "geoexpert")
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "qgissi_tools.txt")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        fh.write(manifest + "\n")
    table: List[str] = []
    for spec in models:
        target = make_target(spec, allow_fallback=args.allow_fallback)
        traces_path = os.path.join(out_dir, f"traces_{target.slug}.jsonl")
        per_case: List[Dict[str, Any]] = []
        with open(traces_path, "w", encoding="utf-8") as traces:
            for i, case in enumerate(cases, 1):
                work_dir = os.path.join(out_dir, "runs", target.slug, case["query_id"])
                run, session = run_query(target, case["query"], args, work_dir)
                trace = condense_trace(session, run, spec)
                traces.write(json.dumps({"query_id": case["query_id"], "agent_execution_trace": trace},
                                        ensure_ascii=False) + "\n")
                ratings = []
                for _ in range(args.num_runs):
                    prompt = render_autorater_prompt(template, case, trace, True, manifest)
                    try:
                        reply = ask(args.judge, prompt)
                        rating = parse_rating(reply)
                        ratings.append({"rating": rating, "normalized": NORMALIZED.get(rating, 0.0), "reply": reply[-600:]})
                    except Exception as exc:
                        ratings.append({"rating": None, "normalized": 0.0, "error": str(exc)})
                score = mean(r["normalized"] for r in ratings)
                row = {"query_id": case["query_id"], "domain_cluster": case["domain_cluster"],
                       "solvability": case["solvability"], "score": round(score, 4), "runs": ratings, **run_record(run)}
                per_case.append(row)
                print(f"[{i}/{len(cases)}] {spec:<36} {case['query_id']}  score={score:.2f}  {row['status']}  "
                      f"${row['cost_usd'] or 0:.4f}  {row['wall_s']:.0f}s")
        overall = mean(r["score"] for r in per_case)
        groups = {key: {value: round(mean(r["score"] for r in per_case if r[key] == value), 4)
                        for value in sorted({r[key] for r in per_case})}
                  for key in ("solvability", "domain_cluster")}
        with open(os.path.join(out_dir, f"ratings_{target.slug}.json"), "w", encoding="utf-8") as fh:
            json.dump({"benchmark": "GeoExpertQueries", "version": "1.0.0", "model": spec,
                       "judge_config": {"judge": args.judge, "prompt": args.prompt, "num_runs": args.num_runs,
                                        "tool_manifest": manifest_path},
                       "overall": {"n": len(per_case), "mean_score": round(overall, 4)},
                       "by_solvability": groups["solvability"], "by_domain_cluster": groups["domain_cluster"],
                       "per_case": per_case}, fh, indent=2)
        table.append(f"| `{spec}` | **{overall:.3f}** | {groups['solvability'].get('solvable', 0):.2f} | "
                     f"{groups['solvability'].get('beyond_current_toolset', 0):.2f} | "
                     f"${sum(r['cost_usd'] or 0 for r in per_case):.2f} |")
        if args.official_python:
            repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            command = f'PYTHONPATH="{repo}" "{args.official_python}" -m qgissi.bench judge --model {args.judge}'
            code, log = official_run(args.official_python, root, "geoexpertqueries.autorater.run_autorater",
                                     ["--traces", os.path.abspath(traces_path), "--judge", "command",
                                      "--judge_command", command, "--prompt", args.prompt, "--num_runs",
                                      str(args.num_runs), "--tool_manifest", os.path.abspath(manifest_path),
                                      "--output", os.path.abspath(os.path.join(out_dir, f"ratings_official_{target.slug}.json"))])
            print(f"official autorater exit {code}\n{log}")
    lines = ["# GeoExpertQueries (dev) results", "", f"Judge: `{args.judge}`, prompt `{args.prompt}`, "
             f"{args.num_runs} run(s) per case, Likert-3 mapped to 0/0.5/1.", "",
             "| Model | Score | Solvable | Beyond toolset | Agent cost |", "|---|---|---|---|---|", *table]
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0

