"""Leaderboard reports (Markdown and a self-contained HTML page)."""
from __future__ import annotations

import html
import json
import os
from typing import Any, Dict, List

from .scoring import summarize


def _money(value) -> str:
    if value is None:
        return "n/a"
    if value == 0:
        return "$0"
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def build_markdown(records: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    rows = summarize(records)
    categories = sorted({r["category"] for r in records})
    tasks = []
    for record in records:
        if record["task"] not in tasks:
            tasks.append(record["task"])
    lines = [f"# QGISSI-Bench results", "",
             f"- Run: `{meta.get('run_id', '')}` · {meta.get('started', '')} · QGIS {meta.get('qgis', '?')} · "
             f"QGISSI {meta.get('qgissi', '?')} ({meta.get('git', '?')})",
             f"- Tasks: {meta.get('tasks', len(tasks))} · repeats: {meta.get('repeats', 1)} · tool tier: "
             f"{meta.get('tool_tier', 'full')} · max steps {meta.get('max_steps', '?')} · fallbacks "
             f"{'on' if meta.get('allow_fallback') else 'off'}", "",
             "## Leaderboard", "",
             "| # | Model | Score | 95% CI | Pass rate | Cost / task | Total cost | Median time | Steps | Tool errors | Errors | Refusals |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, row in enumerate(rows, 1):
        lines.append(f"| {i} | `{row['model']}` | **{row['score']:.3f}** | {row['ci95'][0]:.2f}–{row['ci95'][1]:.2f} | "
                     f"{row['pass_rate']:.0%} | {_money(row['cost_per_task'])} | {_money(row['cost_usd'])} | "
                     f"{row['median_wall_s']:.0f}s | {row['mean_steps']:.1f} | {row['tool_error_rate']:.0%} | "
                     f"{row['errors']} | {row['refusals']} |")
    lines += ["", "## Score by category", "", "| Model | " + " | ".join(categories) + " |",
              "|---|" + "---|" * len(categories)]
    for row in rows:
        lines.append(f"| `{row['model']}` | " + " | ".join(f"{row['by_category'].get(c, 0):.2f}" for c in categories) + " |")
    models = [row["model"] for row in rows]
    lines += ["", "## Per task (mean score over repeats)", "", "| Task | Category | " + " | ".join(f"`{m}`" for m in models) + " |",
              "|---|---|" + "---|" * len(models)]
    for task in tasks:
        cells = []
        category = next(r["category"] for r in records if r["task"] == task)
        for model in models:
            scores = [r["score"] for r in records if r["task"] == task and r["model_spec"] == model]
            cells.append(f"{sum(scores) / len(scores):.2f}" if scores else "–")
        lines.append(f"| {task} | {category} | " + " | ".join(cells) + " |")
    lines += ["", "Scores are in [0, 1]; a task passes at 1.0. Costs use list prices from "
              "`qgissi/providers/catalog.py`; 'n/a' means the price of a model is unknown.", ""]
    return "\n".join(lines)


_CSS = """
:root{--bg:#ffffff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--bar:#2f81f7;--soft:#f6f8fa}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#30363d;--bar:#4493f8;--soft:#161b22}}
body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:24px 16px}
main{max-width:1100px;margin:auto} h1{font-size:22px} h2{font-size:17px;margin-top:28px}
.meta{color:var(--muted)} table{border-collapse:collapse;width:100%;margin:8px 0;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap} th{background:var(--soft)}
.wrap{overflow-x:auto} .bar{display:inline-block;height:8px;background:var(--bar);border-radius:4px;vertical-align:middle;margin-right:6px}
code{font-size:12px}
"""


def build_html(records: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    rows = summarize(records)
    categories = sorted({r["category"] for r in records})
    esc = html.escape
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>QGISSI-Bench results</title><style>{_CSS}</style></head><body><main>",
             "<h1>QGISSI-Bench results</h1>",
             f"<p class='meta'>Run {esc(str(meta.get('run_id', '')))} · {esc(str(meta.get('started', '')))} · "
             f"QGIS {esc(str(meta.get('qgis', '?')))} · {meta.get('tasks', '?')} tasks × {meta.get('repeats', 1)} repeat(s)</p>",
             "<h2>Leaderboard</h2><div class='wrap'><table><tr><th>#</th><th>Model</th><th>Score</th><th>95% CI</th>"
             "<th>Pass</th><th>Cost/task</th><th>Total</th><th>Median time</th><th>Steps</th><th>Tool errors</th></tr>"]
    for i, row in enumerate(rows, 1):
        width = int(row["score"] * 120)
        parts.append(f"<tr><td>{i}</td><td><code>{esc(row['model'])}</code></td>"
                     f"<td><span class='bar' style='width:{width}px'></span>{row['score']:.3f}</td>"
                     f"<td>{row['ci95'][0]:.2f}–{row['ci95'][1]:.2f}</td><td>{row['pass_rate']:.0%}</td>"
                     f"<td>{_money(row['cost_per_task'])}</td><td>{_money(row['cost_usd'])}</td>"
                     f"<td>{row['median_wall_s']:.0f}s</td><td>{row['mean_steps']:.1f}</td><td>{row['tool_error_rate']:.0%}</td></tr>")
    parts.append("</table></div><h2>By category</h2><div class='wrap'><table><tr><th>Model</th>"
                 + "".join(f"<th>{esc(c)}</th>" for c in categories) + "</tr>")
    for row in rows:
        parts.append(f"<tr><td><code>{esc(row['model'])}</code></td>"
                     + "".join(f"<td>{row['by_category'].get(c, 0):.2f}</td>" for c in categories) + "</tr>")
    parts.append("</table></div><h2>Runs</h2><div class='wrap'><table><tr><th>Model</th><th>Task</th><th>Score</th>"
                 "<th>Status</th><th>Steps</th><th>Cost</th><th>Time</th><th>Detail</th></tr>")
    for record in records:
        parts.append(f"<tr><td><code>{esc(record['model_spec'])}</code></td><td>{esc(record['task'])}</td>"
                     f"<td>{record['score']:.2f}</td><td>{esc(record['status'])}</td><td>{record['steps']}</td>"
                     f"<td>{_money(record['cost_usd'])}</td><td>{record['wall_s']:.0f}s</td>"
                     f"<td>{esc(str(record.get('detail') or ''))[:120]}</td></tr>")
    parts.append("</table></div></main></body></html>")
    return "".join(parts)


def write_reports(out_dir: str, records: List[Dict[str, Any]]) -> Dict[str, str]:
    meta_path = os.path.join(out_dir, "meta.json")
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
    paths = {"markdown": os.path.join(out_dir, "report.md"), "html": os.path.join(out_dir, "report.html"),
             "summary": os.path.join(out_dir, "summary.json")}
    with open(paths["markdown"], "w", encoding="utf-8") as fh:
        fh.write(build_markdown(records, meta))
    with open(paths["html"], "w", encoding="utf-8") as fh:
        fh.write(build_html(records, meta))
    with open(paths["summary"], "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "models": summarize(records)}, fh, indent=2)
    return paths
