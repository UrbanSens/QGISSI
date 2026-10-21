"""Per-case result rows of the external suites, kept as JSONL so a run can resume."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, IO


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
