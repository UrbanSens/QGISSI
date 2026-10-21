"""Per-case result rows of the external suites, kept as JSONL so a run can resume."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, IO, Optional

INFLIGHT = "inflight.json"


def load_rows(path: str) -> Dict[str, Dict[str, Any]]:
    """Rows recorded in ``path`` keyed by query id; a torn line (process killed mid-write) is ignored."""
    rows: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "query_id" in row:
                rows[row["query_id"]] = row
    return rows


def open_rows(path: str, resume: bool) -> IO[str]:
    """Open the rows file to append to (resume) or afresh; appends always start on a new line."""
    if not resume or not os.path.exists(path) or not os.path.getsize(path):
        return open(path, "w", encoding="utf-8")
    with open(path, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        torn = fh.read(1) != b"\n"
    handle = open(path, "a", encoding="utf-8")
    if torn:
        handle.write("\n")
    return handle


def append_row(handle: IO[str], row: Dict[str, Any]) -> None:
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    handle.flush()


def row_cost(row: Dict[str, Any]) -> float:
    """Agent plus judge dollars of one row (unknown prices count as zero)."""
    return (row.get("cost_usd") or 0.0) + (row.get("judge_cost_usd") or 0.0)


def mark_inflight(out_dir: str, spec: str, query_id: str) -> None:
    """Note the model/case about to run, so a process that dies inside native code leaves a trace."""
    with open(os.path.join(out_dir, INFLIGHT), "w", encoding="utf-8") as fh:
        json.dump({"spec": spec, "query_id": query_id}, fh)


def clear_inflight(out_dir: str) -> None:
    path = os.path.join(out_dir, INFLIGHT)
    if os.path.exists(path):
        os.remove(path)


def take_inflight(out_dir: str) -> Optional[Dict[str, str]]:
    """The model/case a previous process was running when it died, if any; the marker is removed."""
    path = os.path.join(out_dir, INFLIGHT)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            marker = json.load(fh)
    except ValueError:
        marker = None
    os.remove(path)
    return marker if isinstance(marker, dict) and {"spec", "query_id"} <= set(marker) else None


def crashed_record() -> Dict[str, Any]:
    """Run fields for a case during which QGIS crashed: scored as a failure, cost unknown."""
    return {"status": "crashed", "error": "QGIS crashed while running this case (tool calls: calls.jsonl in "
                                          "its run folder)",
            "steps": 0, "tool_calls": 0, "tool_errors": 0, "usage": {}, "cost_usd": None, "wall_s": 0.0,
            "served_models": [], "fallback_used": False, "refusal_category": None}
