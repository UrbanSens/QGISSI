"""GeoExpertQueries adapter: run QGISSI, condense its trajectory, judge it.

The judge uses Google's autorater prompt (downloaded at the pinned commit),
rendered and parsed exactly as the official runner does, with any QGISSI
provider as the judge model. With ``--official-python`` the official
``run_autorater`` is run too, calling ``python -m qgissi.bench judge``.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
from typing import Dict, List

from ..scoring import NORMALIZED, mean, parse_rating, render_autorater_prompt
from .georeason import fetch, load_cases
from .rows import (append_row, clear_inflight, crashed_record, load_rows, mark_inflight, open_rows, row_cost,
                   take_inflight)

PROMPTS = {"tool_agnostic": "tool_agnostic_autorater_prompt.txt", "workflow": "workflow_autorater_prompt.txt"}


def condense_trace(session, run, spec: str, limit_chars: int = 60000) -> str:
    from ... import __version__
    lines = [f"[AGENT] QGISSI {__version__} | model={spec} | started={datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')} | "
             f"status={run.status} | steps={len(run.steps)}"]
    step = 0
    names: Dict[str, str] = {}
    for message in session.history[1:]:
        for block in message.content:
            kind = block.get("type")
            if message.role == "assistant" and kind == "text" and block.get("text", "").strip():
                lines.append(f"[NOTE] {block['text'].strip()[:1500]}")
            elif message.role == "assistant" and kind == "thinking" and block.get("thinking", "").strip():
                # Claude 5.5 models return progress notes between tool calls as summarised thinking
                lines.append(f"[NOTE] {block['thinking'].strip()[:800]}")
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
    from ..judge import ask_with_cost
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
    targets = {spec: make_target(spec, allow_fallback=args.allow_fallback) for spec in models}
    paths = {spec: os.path.join(out_dir, f"rows_{t.slug}.jsonl") for spec, t in targets.items()}
    done = {spec: load_rows(paths[spec]) if args.resume else {} for spec in models}
    spent = sum(row_cost(r) for rows in done.values() for r in rows.values())
    stopped = False
    with contextlib.ExitStack() as stack:
        files = {spec: stack.enter_context(open_rows(paths[spec], args.resume)) for spec in models}
        died = take_inflight(out_dir)
        if died and args.resume and args.record_crash and died["spec"] in done:
            case = next((c for c in cases if c["query_id"] == died["query_id"]), None)
            if case and case["query_id"] not in done[died["spec"]]:
                row = {"query_id": case["query_id"], "domain_cluster": case["domain_cluster"], "solvability": case["solvability"], "score": 0.0, "runs": [], "judge_cost_usd": 0.0, "trace": "", **crashed_record()}
                append_row(files[died["spec"]], row)
                done[died["spec"]][case["query_id"]] = row
                print(f"Recorded {died['spec']} {case['query_id']} as crashed", flush=True)
        for i, case in enumerate(cases, 1):
            # interleaved, so every model meets the web services in the same state
            for spec, target in targets.items():
                if case["query_id"] in done[spec]:
                    continue
                if args.budget and spent >= args.budget:
                    stopped = True
                    break
                work_dir = os.path.join(out_dir, "runs", target.slug, case["query_id"])
                mark_inflight(out_dir, spec, case["query_id"])
                run, session = run_query(target, case["query"], args, work_dir)
                trace = condense_trace(session, run, spec)
                prompt = render_autorater_prompt(template, case, trace, True, manifest)
                ratings, judge_cost = [], 0.0
                for _ in range(args.num_runs):
                    try:
                        reply, cost = ask_with_cost(args.judge, prompt)
                        judge_cost += cost or 0.0
                        rating = parse_rating(reply)
                        ratings.append({"rating": rating, "normalized": NORMALIZED.get(rating, 0.0), "reply": reply[-600:]})
                    except Exception as exc:
                        ratings.append({"rating": None, "normalized": 0.0, "error": str(exc)})
                score = mean(r["normalized"] for r in ratings)
                row = {"query_id": case["query_id"], "domain_cluster": case["domain_cluster"],
                       "solvability": case["solvability"], "score": round(score, 4), "runs": ratings,
                       "judge_cost_usd": round(judge_cost, 6), "trace": trace, **run_record(run)}
                append_row(files[spec], row)
                clear_inflight(out_dir)
                done[spec][case["query_id"]] = row
                spent += row_cost(row)
                print(f"[{i}/{len(cases)}] {spec:<36} {case['query_id']}  score={score:.2f}  {row['status']}  "
                      f"${row['cost_usd'] or 0:.4f} + judge ${judge_cost:.4f}  {row['wall_s']:.0f}s  (total ${spent:.2f})", flush=True)
            if stopped:
                print(f"Budget ${args.budget:.2f} reached (${spent:.2f} recorded); stopping. "
                      "Continue with --resume and a higher --budget.")
                break
    table: List[str] = []
    crashes: Dict[str, int] = {}
    for spec, target in targets.items():
        per_case = [done[spec][c["query_id"]] for c in cases if c["query_id"] in done[spec]]
        if not per_case:
            continue
        traces_path = os.path.join(out_dir, f"traces_{target.slug}.jsonl")
        with open(traces_path, "w", encoding="utf-8") as traces:
            for row in per_case:
                traces.write(json.dumps({"query_id": row["query_id"], "agent_execution_trace": row["trace"]},
                                        ensure_ascii=False) + "\n")
        overall = mean(r["score"] for r in per_case)
        if any(r["status"] == "crashed" for r in per_case):
            crashes[spec] = sum(1 for r in per_case if r["status"] == "crashed")
        groups = {key: {value: round(mean(r["score"] for r in per_case if r[key] == value), 4)
                        for value in sorted({r[key] for r in per_case})}
                  for key in ("solvability", "domain_cluster")}
        with open(os.path.join(out_dir, f"ratings_{target.slug}.json"), "w", encoding="utf-8") as fh:
            json.dump({"benchmark": "GeoExpertQueries", "version": "1.0.0", "model": spec,
                       "judge_config": {"judge": args.judge, "prompt": args.prompt, "num_runs": args.num_runs,
                                        "tool_manifest": manifest_path},
                       "complete": len(per_case) == len(cases),
                       "overall": {"n": len(per_case), "mean_score": round(overall, 4)},
                       "by_solvability": groups["solvability"], "by_domain_cluster": groups["domain_cluster"],
                       "per_case": [{k: v for k, v in r.items() if k != "trace"} for r in per_case]}, fh, indent=2)
        table.append(f"| `{spec}` | {len(per_case)}/{len(cases)} | **{overall:.3f}** | "
                     f"{groups['solvability'].get('solvable', 0):.2f} | "
                     f"{groups['solvability'].get('beyond_current_toolset', 0):.2f} | "
                     f"${sum(r['cost_usd'] or 0 for r in per_case):.2f} | "
                     f"${sum(r.get('judge_cost_usd') or 0 for r in per_case):.2f} |")
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
             "| Model | Cases | Score | Solvable | Beyond toolset | Agent cost | Judge cost |",
             "|---|---|---|---|---|---|---|", *table]
    if crashes:
        lines += ["", "QGIS crashed during " + ", ".join(f"{n} `{spec}` case(s)" for spec, n in crashes.items())
                  + "; each is scored 0."]
    if stopped:
        lines += ["", f"Stopped early by the ${args.budget:.2f} budget: scores cover only the recorded cases."]
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 3 if stopped else 0
