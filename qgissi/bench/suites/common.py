"""Shared helpers for the external benchmark adapters."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from qgis.core import QgsCoordinateReferenceSystem, QgsProject

from ...core.agent import AgentConfig, AgentSession, RunResult
from ...core.context import build_project_context
from ...tools.registry import REGISTRY, ToolContext
from ..runner import ModelTarget, _transcript


def run_query(target: ModelTarget, query: str, args, work_dir: str) -> Tuple[RunResult, AgentSession]:
    """Run one unattended agent turn on an empty project (network tools enabled)."""
    project = QgsProject.instance()
    project.clear()
    project.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    output_dir = os.path.join(work_dir, "output")
    os.makedirs(output_dir)

    def ctx_factory(cancel, run_id):
        return ToolContext(project=project, output_dir=output_dir, cancel=cancel, run_id=run_id, headless=True,
                           allow_network=True)

    config = AgentConfig(provider=target.provider_name, model=target.model, effort=target.effort,
                         max_steps=args.max_steps, max_cost_usd=args.max_cost, max_seconds=args.max_seconds,
                         tool_tier=args.tool_tier, unattended=True, allow_fallback=args.allow_fallback)
    session = AgentSession(target.provider, config, ctx_factory=ctx_factory, context_builder=build_project_context)
    run = session.run_turn(query)
    with open(os.path.join(work_dir, "transcript.json"), "w", encoding="utf-8") as fh:
        json.dump({"query": query, "result": run.to_dict(), "messages": _transcript(session)}, fh,
                  ensure_ascii=False, indent=1, default=str)
    return run, session


def run_record(run: RunResult) -> Dict[str, Any]:
    return {"status": run.status, "error": run.error, "steps": len(run.steps), "tool_calls": len(run.tool_calls),
            "tool_errors": sum(1 for c in run.tool_calls if c.is_error), "usage": run.usage.to_dict(),
            "cost_usd": None if run.cost_usd is None else round(run.cost_usd, 6), "wall_s": round(run.wall_s, 2),
            "served_models": run.served_models, "fallback_used": run.fallback_used,
            "refusal_category": run.refusal_category}


def tool_manifest() -> str:
    lines = []
    for definition in REGISTRY.select("full", gui=False):
        args = ", ".join(f"{name}: {spec.get('type', 'any')}" for name, spec in definition.parameters.items())
        lines.append(f"- {definition.name}({args}): {definition.description}")
    return "\n".join(lines)


def official_run(python: str, repo_root: str, module: str, arguments: List[str],
                 env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """Run one of Google's official runners in a separate (Python >= 3.10) process."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    proc = subprocess.run([python, "-m", module, *arguments], cwd=repo_root, capture_output=True, text=True,
                          env=full_env)
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def select_cases(cases: List[Dict[str, Any]], ids: str, limit: int) -> List[Dict[str, Any]]:
    if ids:
        wanted = {i.strip() for i in ids.split(",") if i.strip()}
        cases = [c for c in cases if c["query_id"] in wanted or c["query_id"].split("_")[-1] in wanted]
    if limit:
        cases = cases[:limit]
    return cases
