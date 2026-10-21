"""Command line for the QGISSI benchmark harness.

    scripts/qgis-python -m qgissi.bench list
    scripts/qgis-python -m qgissi.bench run --models anthropic/claude-opus-5-5@medium,oracle
    scripts/qgis-python -m qgissi.bench report bench_runs/<run>
    scripts/qgis-python -m qgissi.bench geoktools --models anthropic/claude-opus-5-5 --subset open
    scripts/qgis-python -m qgissi.bench geoexpert --models anthropic/claude-opus-5-5 --judge anthropic/claude-opus-5-5
    python3 -m qgissi.bench judge --model anthropic/claude-opus-5-5@medium   # stdin -> stdout (no QGIS)

Run commands that touch QGIS with the interpreter bundled with QGIS
(``scripts/qgis-python``). API keys are read from the environment
(ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, OLLAMA_HOST, ...).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import subprocess
import sys
from typing import List

DEFAULT_CACHE = os.environ.get("QGISSI_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "qgissi"))


def _git_sha() -> str:
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _out_dir(args, suite: str) -> str:
    if args.out:
        return args.out
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join("bench_runs", f"{suite}-{stamp}")


def _write_meta(out_dir: str, args, suite: str, task_count: int) -> None:
    from qgis.core import Qgis

    from .. import __version__
    os.makedirs(out_dir, exist_ok=True)
    meta = {"run_id": os.path.basename(os.path.abspath(out_dir)), "suite": suite,
            "started": datetime.datetime.now().isoformat(timespec="seconds"), "argv": sys.argv[1:],
            "models": args.models.split(","), "tasks": task_count, "repeats": getattr(args, "repeats", 1),
            "max_steps": args.max_steps, "max_cost_usd": args.max_cost, "tool_tier": args.tool_tier,
            "allow_fallback": args.allow_fallback, "qgis": Qgis.QGIS_VERSION, "python": platform.python_version(),
            "qgissi": __version__, "git": _git_sha(), "platform": platform.platform()}
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)


def _estimate(models: List[str], n_tasks: int) -> None:
    from ..providers import parse_model_spec
    from ..providers.catalog import model_info
    print("Rough cost estimate (assumes ~7 steps/task, ~11k-token cached prefix, ~2.5k output tokens/step):")
    for spec in models:
        if spec == "oracle":
            print(f"  {spec:<40} $0 (scripted)")
            continue
        provider, model, effort = parse_model_spec(spec)
        info = model_info(model, provider)
        if info.price is None:
            print(f"  {spec:<40} price unknown")
            continue
        inp, out, write, read = info.price
        steps = 7
        per_task = (11_000 * write + steps * 11_000 * read + steps * 2_000 * inp + steps * 2_500 * out) / 1e6
        print(f"  {spec:<40} ~${per_task:.3f}/task -> ~${per_task * n_tasks:.2f} for {n_tasks} tasks")


def _bench_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--models", required=True, help="Comma-separated specs, e.g. anthropic/claude-opus-5-5@high,oracle")
    parser.add_argument("--out", help="Output directory (default bench_runs/<suite>-<timestamp>)")
    parser.add_argument("--cache", default=DEFAULT_CACHE, help=f"Cache directory (default {DEFAULT_CACHE})")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-cost", type=float, default=2.0, help="USD budget per task (0 = no limit)")
    parser.add_argument("--max-seconds", type=float, default=900.0)
    parser.add_argument("--tool-tier", choices=["full", "core"], default="full")
    parser.add_argument("--allow-fallback", action="store_true",
                        help="Allow Anthropic server-side refusal fallbacks (off: results stay attributable)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and a cost estimate, run nothing")


def cmd_list(_args) -> int:
    from .tasks import TASKS
    for task in TASKS:
        print(f"{task.id:<32} {task.category:<12} level {task.difficulty}  {task.prompt[:80]}")
    print(f"\n{len(TASKS)} tasks")
    return 0


def cmd_run(args) -> int:
    from ..headless import start_qgis
    start_qgis()
    from .report import write_reports
    from .runner import BenchConfig, make_target, run_suite
    from .tasks import select_tasks

    tasks = select_tasks(args.tasks)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"{len(tasks)} task(s) x {len(models)} model(s) x {args.repeats} repeat(s)")
    _estimate(models, len(tasks) * args.repeats)
    if args.dry_run:
        return 0
    cfg = BenchConfig(max_steps=args.max_steps, max_cost_usd=args.max_cost, max_seconds=args.max_seconds,
                      tool_tier=args.tool_tier, repeats=args.repeats, allow_fallback=args.allow_fallback,
                      network=args.network)
    targets = [make_target(spec, allow_fallback=args.allow_fallback) for spec in models]
    out_dir = _out_dir(args, "qgissi")
    _write_meta(out_dir, args, "qgissi", len(tasks))
    records = run_suite(tasks, targets, cfg, out_dir, args.cache)
    paths = write_reports(out_dir, records)
    with open(paths["markdown"], encoding="utf-8") as fh:
        print(fh.read())
    print(f"Reports: {paths['markdown']}  {paths['html']}")
    return 0


def cmd_report(args) -> int:
    from .report import write_reports
    from .runner import load_records
    paths = write_reports(args.run_dir, load_records(args.run_dir))
    with open(paths["markdown"], encoding="utf-8") as fh:
        print(fh.read())
    return 0


def cmd_fixtures(args) -> int:
    from ..headless import start_qgis
    start_qgis()
    from .fixtures import build_fixture_dir
    print(build_fixture_dir(args.directory))
    return 0


def cmd_judge(args) -> int:
    """Read a judge prompt on stdin, print the model's reply (for --judge command)."""
    from .judge import judge_main
    return judge_main(args.model, sys.stdin.read(), max_tokens=args.max_tokens)


def cmd_geoktools(args) -> int:
    from .suites.geoktools import run_geoktools
    return run_geoktools(args)


def cmd_geoexpert(args) -> int:
    from .suites.geoexpert import run_geoexpert
    return run_geoexpert(args)


def cmd_fetch(args) -> int:
    from .suites.georeason import fetch
    print(fetch(args.cache, accept_terms=args.accept_terms))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qgissi.bench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List QGISSI-Bench tasks").set_defaults(func=cmd_list)

    run = sub.add_parser("run", help="Run QGISSI-Bench")
    _bench_args(run)
    run.add_argument("--tasks", default="all", help="all | ids | cat:<category> | level:<n> (comma-separated)")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--network", action="store_true", help="Allow network tools (off for reproducibility)")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="Rebuild the reports of a run directory")
    report.add_argument("run_dir")
    report.set_defaults(func=cmd_report)

    fixtures = sub.add_parser("fixtures", help="Write the Musterstadt fixture data to a directory")
    fixtures.add_argument("directory")
    fixtures.set_defaults(func=cmd_fixtures)

    judge = sub.add_parser("judge", help="LLM judge: prompt on stdin, reply on stdout (no QGIS needed)")
    judge.add_argument("--model", required=True)
    judge.add_argument("--max-tokens", type=int, default=16000)
    judge.set_defaults(func=cmd_judge)

    fetch = sub.add_parser("fetch", help="Download Google's geospatial-reasoning-benchmarks at the pinned commit")
    fetch.add_argument("--cache", default=DEFAULT_CACHE)
    fetch.add_argument("--accept-terms", action="store_true")
    fetch.set_defaults(func=cmd_fetch)

    for name, func, helptext in (("geoktools", cmd_geoktools, "Run GeoKTools (dev split)"),
                                 ("geoexpert", cmd_geoexpert, "Run GeoExpertQueries (dev split)")):
        p = sub.add_parser(name, help=helptext)
        _bench_args(p)
        p.add_argument("--limit", type=int, default=0, help="Only the first N cases")
        p.add_argument("--ids", default="", help="Comma-separated query ids")
        p.add_argument("--accept-terms", action="store_true", help="Acknowledge the benchmark data terms")
        p.add_argument("--official-python", default="", help="Python >= 3.10 to also run Google's official runners")
        if name == "geoktools":
            p.add_argument("--subset", choices=["all", "open"], default="all",
                           help="'open' = the 36 cases answerable from open data (unofficial diagnostic)")
        else:
            p.add_argument("--judge", required=True, help="Judge model spec, e.g. anthropic/claude-opus-5-5@medium")
            p.add_argument("--num-runs", type=int, default=3)
            p.add_argument("--prompt", choices=["tool_agnostic", "workflow"], default="tool_agnostic")
        p.set_defaults(func=func, network=True)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    from ..headless import exit_process
    exit_process(main())
