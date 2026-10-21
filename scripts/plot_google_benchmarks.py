#!/usr/bin/env python3
"""Chart QGISSI's results on Google's geospatial benchmarks next to the numbers TerraLab reports.

Reads ``rows_<model>.jsonl`` from the GeoKTools and GeoExpertQueries run folders (a run still in
progress gives a chart marked preliminary) and writes a light and a dark SVG. Standard library only.

    python3 scripts/plot_google_benchmarks.py --geoktools bench_runs/geoktools-claude-medium \\
        --geoexpert bench_runs/geoexpert-claude-medium --out docs/img/google-benchmarks
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qgissi.bench.scoring import bootstrap_ci, mean  # noqa: E402
from qgissi.bench.suites.rows import load_rows  # noqa: E402
from qgissi.providers import parse_model_spec  # noqa: E402
from qgissi.providers.catalog import model_info  # noqa: E402

MODELS = "anthropic/claude-opus-5-5@medium,anthropic/claude-sonnet-5-5@medium,anthropic/claude-haiku-5-5@medium"
QUESTIONS = {"geoktools": 85, "geoexpert": 25}  # public dev split
# Published by TerraLab (terra-lab.ai/blog/ai-agent-qgis-plugin-geospatial-benchmark, runs of 16-20 October
# 2026): GeoKTools with Google's deterministic scorer on 30 held-out questions, GeoExpertQueries with Google's
# autorater and a Gemini 3.8 Flash judge on 11, each the mean of 3 runs.
REPORTED = [
    ("TerraLab AI Agent plugin", "Low effort, GPT-6 Luna", 53.5, 62.6),
    ("Claude Sonnet 5.5 + QGIS MCP", "nkarasiak MCP server in Claude Code", 33.3, 29.3),
    ("GPT-6 Luna + QGIS MCP", "jjsantos01 MCP server in Codex", 39.5, 8.6),
]
THEMES = {
    "light": {"surface": "#fcfcfb", "border": "rgba(11,11,11,0.10)", "ink": "#0b0b0b", "ink2": "#52514e",
              "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "ours": "#008f96", "theirs": "#bdbcb5"},
    "dark": {"surface": "#1a1a19", "border": "rgba(255,255,255,0.10)", "ink": "#f0efec", "ink2": "#c3c2b7",
             "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "ours": "#16a6aa", "theirs": "#5f5e59"},
}
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
LOGO = os.path.join(ROOT, "docs", "img", "urbansens-logo.svg")

Stat = Dict[str, Any]  # value (0-100), ci (lo, hi), n, cost (agent dollars)


def slug(spec: str) -> str:
    provider, model, effort = parse_model_spec(spec)
    text = f"{provider}-{model}" + (f"-{effort}" if effort else "")
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in text)


def load_stat(run_dir: str, spec: str) -> Optional[Stat]:
    rows = list(load_rows(os.path.join(run_dir, f"rows_{slug(spec)}.jsonl")).values())
    if not rows:
        return None
    scores = [100.0 * r["score"] for r in rows]
    return {"value": mean(scores), "ci": bootstrap_ci(scores, 10000), "n": len(rows),
            "cost": sum(r.get("cost_usd") or 0.0 for r in rows)}


def text_width(text: str, size: float) -> float:
    return len(text) * size * 0.54  # system sans, close enough for layout and wrapping


def wrap(text: str, size: float, width: float) -> List[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and text_width(candidate, size) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return lines + [line] if line else lines


def bar_path(x0: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """Bar growing right from x0: square at the baseline, rounded data end."""
    r = min(r, w, h / 2)
    x1 = x0 + w
    return (f"M{x0:.1f},{y:.1f} H{x1 - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x1 - r:.1f},{y + h:.1f} H{x0:.1f} Z")


def logo(x: float, y: float, size: float) -> str:
    with open(LOGO, encoding="utf-8") as fh:
        inner = re.search(r"<svg[^>]*>(.*)</svg>", fh.read(), re.S).group(1)
    return f'<g transform="translate({x} {y}) scale({size / 100})">{inner}</g>'


def render(theme: str, ours: List[Tuple[str, str, Optional[Stat], Optional[Stat]]], note: str) -> str:
    t = THEMES[theme]
    W, M = 1200, 40
    panels = (("GeoKTools", "QGISSI 85 · TerraLab 30 questions", 480), ("GeoExpertQueries",
                                                                       "QGISSI 25 · TerraLab 11 questions", 840))
    scale = 250 / 100  # px per point
    row_h, bar_h = 54, 22
    out: List[str] = []

    def text(x, y, s, size, color, weight=400, anchor="start", extra=""):
        out.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" fill="{color}" '
                   f'text-anchor="{anchor}"{extra}>{escape(s)}</text>')

    out.append(logo(M, 26, 60))
    text(M + 78, 54, "AI agents for QGIS on Google's geospatial benchmarks", 28, t["ink"], 650)
    text(M + 78, 83, "QGISSI with Claude 5.5 models, next to the systems TerraLab compared. "
                     "Accuracy, 0–100, higher is better.", 16.5, t["ink2"])
    if note:
        text(M + 78, 106, note, 14.5, t["ink2"], 600)
    top = 150 if note else 136
    for name, sub, x0 in panels:
        text(x0, top, name, 19, t["ink"], 650)
        text(x0, top + 21, sub, 13.5, t["muted"])

    blocks = []  # (y_start, y_end) of each block of rows, for the grid
    y = top + 44
    groups = [("QGISSI · measured by UrbanSens · Google's full public dev split, one run per question",
               [(label, sub, gk, ge, True) for label, sub, gk, ge in ours]),
              ("Reported by TerraLab · 41 held-out questions of the same split, mean of 3 runs",
               [(label, sub, {"value": gk}, {"value": ge}, False) for label, sub, gk, ge in REPORTED])]
    rows_svg: List[str] = []
    for title, rows in groups:
        text(M, y + 16, title, 14, t["ink2"], 650)
        y += 26
        blocks.append((y, y + row_h * len(rows)))
        for label, sub, gk, ge, mine in rows:
            text(M, y + 24, label, 18, t["ink"], 550)
            text(M, y + 43, sub, 13.5, t["muted"])
            bar_y = y + (row_h - bar_h) / 2
            for stat, (_name, _sub, x0) in zip((gk, ge), panels):
                if stat is None:
                    continue
                end = x0 + stat["value"] * scale
                if stat["value"] > 0:
                    rows_svg.append(f'<path d="{bar_path(x0, bar_y, stat["value"] * scale, bar_h)}" '
                                    f'fill="{t["ours"] if mine else t["theirs"]}"/>')
                label_x = end
                if "ci" in stat and stat["n"] > 1:
                    lo, hi = (x0 + v * scale for v in stat["ci"])
                    mid = bar_y + bar_h / 2
                    rows_svg.append(f'<path d="M{lo:.1f},{mid:.1f} H{hi:.1f} M{lo:.1f},{mid - 5:.1f} V{mid + 5:.1f} '
                                    f'M{hi:.1f},{mid - 5:.1f} V{mid + 5:.1f}" stroke="{t["ink2"]}" '
                                    'stroke-width="1.5" fill="none"/>')
                    label_x = max(end, hi)
                rows_svg.append(f'<text x="{label_x + 8:.1f}" y="{bar_y + bar_h / 2 + 5.5:.1f}" font-size="16" '
                                f'font-weight="600" fill="{t["ink"]}">{stat["value"]:.1f}</text>')
            y += row_h
        y += 14
    bottom = y - 14
    for _name, _sub, x0 in panels:  # grid first, so bars and labels sit on top
        for tick in (0, 25, 50, 75, 100):
            x = x0 + tick * scale
            color = t["axis"] if tick == 0 else t["grid"]
            for start, end in blocks:
                out.append(f'<path d="M{x:.1f},{start:.1f} V{end:.1f}" stroke="{color}" stroke-width="1"/>')
            text(x, bottom + 22, str(tick), 13, t["muted"], anchor="middle",
                 extra=' style="font-variant-numeric: tabular-nums"')
    out.extend(rows_svg)

    y = bottom + 58
    notes = [
        ("GeoKTools: Google's deterministic scorer (10% tolerance, partial credit, ROUGE-L for names). "
         "GeoExpertQueries: Google's autorater prompt with 3 ratings per answer, judged by Claude Opus 5.5 for "
         "QGISSI and by Gemini 3.8 Flash in TerraLab's runs. Whiskers: 95% bootstrap interval over questions.",
         t["ink2"]),
        ("QGISSI: every public dev question (85 + 25), one run each at medium effort with all 50 tools, QGIS 3.36, "
         "21 October 2026. TerraLab's numbers as published on terra-lab.ai for runs of 16–20 October 2026. "
         "Question sets and judges differ, so read the comparison as indicative.", t["ink2"]),
        ("QGISSI by UrbanSens · github.com/UrbanSens/QGISSI", t["muted"]),
    ]
    for paragraph, color in notes:
        for line in wrap(paragraph, 13, W - 2 * M):
            text(M, y, line, 13, color)
            y += 19
        y += 7
    height = int(y + 18)

    desc = "; ".join(f"{label}: GeoKTools {gk['value']:.1f}, GeoExpertQueries {ge['value']:.1f}"
                     for label, _sub, gk, ge in ours if gk and ge)
    desc += "; " + "; ".join(f"{label} (reported by TerraLab): GeoKTools {gk}, GeoExpertQueries {ge}"
                             for label, _sub, gk, ge in REPORTED)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}" '
            f'role="img" aria-labelledby="title desc" font-family="{FONT}">\n'
            f'<title id="title">AI agents for QGIS on Google\'s geospatial benchmarks</title>\n'
            f'<desc id="desc">{escape(desc)}</desc>\n'
            f'<rect x="0.5" y="0.5" width="{W - 1}" height="{height - 1}" rx="16" fill="{t["surface"]}" '
            f'stroke="{t["border"]}"/>\n' + "\n".join(out) + "\n</svg>\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--geoktools", default="bench_runs/geoktools-claude-medium")
    parser.add_argument("--geoexpert", default="bench_runs/geoexpert-claude-medium")
    parser.add_argument("--models", default=MODELS, help="Comma-separated model specs, top to bottom")
    parser.add_argument("--out", default="docs/img/google-benchmarks", help="Output path prefix")
    args = parser.parse_args(argv)

    ours, counts = [], {"geoktools": [], "geoexpert": []}
    for spec in [s.strip() for s in args.models.split(",") if s.strip()]:
        provider, model, effort = parse_model_spec(spec)
        gk, ge = load_stat(args.geoktools, spec), load_stat(args.geoexpert, spec)
        if not gk and not ge:
            continue
        n = sum(s["n"] for s in (gk, ge) if s)
        cost = sum(s["cost"] for s in (gk, ge) if s)
        ours.append((f"QGISSI + {model_info(model, provider).label}",
                     f"{effort or 'default'} effort · ${cost / n:.3f} per question", gk, ge))
        for suite, stat in (("geoktools", gk), ("geoexpert", ge)):
            counts[suite].append(stat["n"] if stat else 0)
    if not ours:
        print("no results found", file=sys.stderr)
        return 1
    note = ""
    if any(n < QUESTIONS[suite] for suite, ns in counts.items() for n in ns):
        spans = [f"{name} {min(counts[suite])}" + (f"–{max(counts[suite])}" if max(counts[suite]) > min(counts[suite])
                                                    else "") + f" of {QUESTIONS[suite]}"
                 for suite, name in (("geoktools", "GeoKTools"), ("geoexpert", "GeoExpertQueries"))]
        note = f"PRELIMINARY: runs in progress, {spans[0]} and {spans[1]} questions done per model"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    for theme in THEMES:
        path = f"{args.out}-{theme}.svg"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render(theme, ours, note))
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
