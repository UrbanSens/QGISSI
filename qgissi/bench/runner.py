"""Headless benchmark runner for QGISSI-Bench.

For every (model, task, repeat) the runner starts from a clean QGIS project
with a fresh copy of the fixture data, runs the unattended agent with the same
system prompt and tools the plugin uses, then scores the resulting project
state with the task's checker. Every run is written as one JSON line, and the
full transcript is kept next to it for auditing.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from qgis.core import QgsCoordinateReferenceSystem, QgsFeatureRequest, QgsProject, QgsRasterLayer, QgsVectorLayer

from ..core.agent import AgentConfig, AgentSession
from ..core.context import build_project_context
from ..providers import ProviderConfig, config_from_env, create_provider, parse_model_spec
from ..providers.base import Provider
from ..tools.registry import ToolContext
from .fixtures import VECTOR_LAYERS, build_fixture_dir
from .tasks import CheckContext, CheckResult, Task

LOAD_ORDER = ["schools", "bus_stops", "trees", "wells", "roads", "river", "buildings", "parks", "districts",
              "census_table"]


@dataclass
class BenchConfig:
    max_steps: int = 30
    max_cost_usd: float = 2.0
    max_seconds: float = 900.0
    tool_tier: str = "full"
    repeats: int = 1
    allow_fallback: bool = False   # keep runs attributable to the requested model
    network: bool = False


@dataclass
class ModelTarget:
    spec: str
    provider_name: str
    model: str
    effort: Optional[str]
    provider: Provider

    @property
    def slug(self) -> str:
        text = f"{self.provider_name}-{self.model}" + (f"-{self.effort}" if self.effort else "")
        return "".join(c if c.isalnum() or c in "-._" else "_" for c in text)


def make_target(spec: str, provider_factory: Optional[Callable[[str, str], Provider]] = None,
                allow_fallback: bool = False) -> ModelTarget:
    if spec == "oracle":
        from .oracle import OracleProvider
        return ModelTarget(spec, "oracle", "oracle", None, OracleProvider())
    provider_name, model, effort = parse_model_spec(spec)
    if provider_factory is not None:
        provider = provider_factory(provider_name, model)
    else:
        config: ProviderConfig = config_from_env(provider_name)
        config.enable_fallbacks = allow_fallback
        provider = create_provider(config)
    return ModelTarget(spec, provider_name, model, effort, provider)


_TEMPLATES: Dict[str, Dict[str, QgsVectorLayer]] = {}


def _templates(template_dir: str) -> Dict[str, QgsVectorLayer]:
    """Fixture layers read once per process into memory.

    Opening a GeoPackage initialises SpatiaLite, whose SQLite warnings macOS's
    system SQLite sends to the unified log; after a few hundred opens macOS
    throttles the process. Each benchmark task therefore gets in-memory copies.
    """
    key = os.path.realpath(template_dir)
    if key not in _TEMPLATES:
        gpkg = os.path.join(key, "musterstadt.gpkg")
        layers = {}
        for name in LOAD_ORDER:
            source = QgsVectorLayer(f"{gpkg}|layername={name}", name, "ogr")
            if not source.isValid():
                raise RuntimeError(f"Fixture layer {name} failed to load")
            layers[name] = source.materialize(QgsFeatureRequest())
        _TEMPLATES[key] = layers
    return _TEMPLATES[key]


def load_fixture_project(project: QgsProject, data_dir: str, template_dir: Optional[str] = None) -> Dict[str, str]:
    """Load Musterstadt into the project: from ``data_dir``'s GeoPackage, or as fresh in-memory
    copies of the layers in ``template_dir`` (used by the benchmark runner)."""
    project.clear()
    project.setCrs(QgsCoordinateReferenceSystem("EPSG:25832"))
    project.setTitle("Musterstadt")
    gpkg = os.path.join(data_dir, "musterstadt.gpkg")
    templates = _templates(template_dir) if template_dir else None
    ids: Dict[str, str] = {}
    root = project.layerTreeRoot()
    for index, name in enumerate(LOAD_ORDER):  # top of the layer tree first
        if templates is not None:
            layer = templates[name].materialize(QgsFeatureRequest())
            layer.setName(name)
        else:
            layer = QgsVectorLayer(f"{gpkg}|layername={name}", name, "ogr")
        if not layer.isValid():
            raise RuntimeError(f"Fixture layer {name} failed to load")
        project.addMapLayer(layer, False)
        root.insertLayer(index, layer)
        ids[name] = layer.id()
    dem = QgsRasterLayer(os.path.join(data_dir, "dem.tif"), "dem", "gdal")
    project.addMapLayer(dem, False)
    root.insertLayer(len(LOAD_ORDER), dem)
    ids["dem"] = dem.id()
    return ids


def _transcript(session: AgentSession) -> List[Dict[str, Any]]:
    out = []
    for message in session.history:
        content = []
        for block in message.content:
            block = dict(block)
            if block.get("type") == "image":
                block = {"type": "image", "omitted": True}
            elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                block["content"] = [c if c.get("type") != "image" else {"type": "image", "omitted": True}
                                    for c in block["content"]]
            elif block.get("type") in ("thinking", "redacted_thinking"):
                block.pop("signature", None)
                block.pop("data", None)
            content.append(block)
        out.append({"role": message.role, "model": message.model, "content": content})
    return out


def run_task(task: Task, target: ModelTarget, cfg: BenchConfig, fixture_dir: str, work_dir: str,
             repeat: int = 0) -> Dict[str, Any]:
    project = QgsProject.instance()
    data_dir = os.path.join(work_dir, "data")
    output_dir = os.path.join(work_dir, "output")
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(output_dir)
    os.makedirs(data_dir)
    shutil.copy2(os.path.join(fixture_dir, "dem.tif"), data_dir)
    initial = load_fixture_project(project, data_dir, template_dir=fixture_dir)

    def ctx_factory(cancel, run_id):
        return ToolContext(project=project, output_dir=output_dir, cancel=cancel, run_id=run_id, headless=True,
                           allow_network=cfg.network)

    agent_cfg = AgentConfig(provider=target.provider_name, model=target.model, effort=target.effort,
                            max_steps=cfg.max_steps, max_cost_usd=cfg.max_cost_usd, max_seconds=cfg.max_seconds,
                            tool_tier=cfg.tool_tier, unattended=True, allow_fallback=cfg.allow_fallback)
    if target.provider_name == "oracle":
        target.provider.prepare(task.id)  # type: ignore[attr-defined]
    session = AgentSession(target.provider, agent_cfg, ctx_factory=ctx_factory, context_builder=build_project_context)
    started = time.time()
    run = session.run_turn(task.full_prompt())
    check_ctx = CheckContext(project=project, final_text=run.final_text, run=run, output_dir=output_dir,
                             fixture_dir=fixture_dir, initial_layer_ids={k: v for k, v in initial.items()})
    try:
        result = task.check(check_ctx)
    except Exception as exc:  # a checker crash must not abort the benchmark
        result = CheckResult(0.0, f"checker error: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    if task.grounded and result.score > 0 and not any(not c.is_error for c in run.tool_calls):
        result = CheckResult(0.0, "not grounded: no successful tool call (a guessed answer scores 0)",
                             result.expected, result.got)
    record = {
        "suite": "qgissi", "task": task.id, "category": task.category, "difficulty": task.difficulty,
        "model_spec": target.spec, "provider": target.provider_name, "model": target.model, "effort": target.effort,
        "repeat": repeat, "score": round(float(result.score), 4), "passed": result.score >= 0.999,
        "detail": result.detail, "expected": _jsonable(result.expected), "got": _jsonable(result.got),
        "status": run.status, "error": run.error, "steps": len(run.steps), "tool_calls": len(run.tool_calls),
        "tool_errors": sum(1 for c in run.tool_calls if c.is_error),
        "tools_used": [c.name for c in run.tool_calls], "usage": run.usage.to_dict(),
        "cost_usd": None if run.cost_usd is None else round(run.cost_usd, 6), "wall_s": round(time.time() - started, 2),
        "model_latency_s": round(sum(s.latency_s for s in run.steps), 2),
        "served_models": run.served_models, "fallback_used": run.fallback_used,
        "refusal_category": run.refusal_category, "final_text": (run.final_text or "")[:4000],
    }
    with open(os.path.join(work_dir, "transcript.json"), "w", encoding="utf-8") as fh:
        json.dump({"record": record, "system_prompt": session.system_prompt,
                   "tools": [d.name for d in session.tool_defs], "messages": _transcript(session)},
                  fh, ensure_ascii=False, indent=1, default=str)
    project.clear()
    return record


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def run_suite(tasks: List[Task], targets: List[ModelTarget], cfg: BenchConfig, out_dir: str, cache_dir: str,
              progress: Callable[[str], None] = print) -> List[Dict[str, Any]]:
    fixture_dir = build_fixture_dir(os.path.join(cache_dir, "fixtures"))
    os.makedirs(out_dir, exist_ok=True)
    results_path = os.path.join(out_dir, "results.jsonl")
    records: List[Dict[str, Any]] = []
    total = len(tasks) * len(targets) * cfg.repeats
    index = 0
    for target in targets:
        for repeat in range(cfg.repeats):
            for task in tasks:
                index += 1
                work_dir = os.path.join(out_dir, "runs", target.slug, f"{task.id}_r{repeat}")
                record = run_task(task, target, cfg, fixture_dir, work_dir, repeat)
                records.append(record)
                with open(results_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                cost = "n/a" if record["cost_usd"] is None else f"${record['cost_usd']:.4f}"
                progress(f"[{index}/{total}] {target.spec:<38} {task.id:<30} score={record['score']:.2f} "
                         f"status={record['status']:<9} steps={record['steps']:<2} cost={cost} {record['wall_s']:.0f}s"
                         + (f"  ({record['detail']})" if record["detail"] and record["score"] < 1 else ""))
    return records


def load_records(out_dir: str) -> List[Dict[str, Any]]:
    path = os.path.join(out_dir, "results.jsonl")
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
