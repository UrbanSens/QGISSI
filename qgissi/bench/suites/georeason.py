"""Download Google's geospatial-reasoning-benchmarks at a pinned commit.

Nothing from that repository is vendored into QGISSI: GeoKTools golden
answers contain Google Maps-derived values that are excluded from the
repository's licences and provided only for evaluation. The files are
downloaded on demand into the user cache, verified against SHA-256 hashes and
kept outside every folder the agent's tools work in.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from typing import Any, Dict

from ...providers.http import HttpClient

COMMIT = "c21a6d95c3fc446cba9205855785bc5ae31c0814"
REPO = "google-research/geospatial-reasoning-benchmarks"
ARCHIVE = f"https://codeload.github.com/{REPO}/tar.gz/{COMMIT}"
FILES = {
    "geoktools/dev_queries.json": "8b90c0fe778e1196b8c10c0a58136b655e60ed1d988168506b466b7ca5af29bd",
    "geoexpertqueries/dev_queries.json": "1d3fb538594f49a55f3c26edd03072adbc10daa6a146bdcd60d8e8af639cb15f",
    "geoktools/scoring/scoring_functions.py": "06f02b3af9220232137788184249426f6dca8db9d5e3fa65fc39139bd1fa1876",
    "geoktools/scoring/run_scoring.py": "2b623e92ecca5a7bf468a9966937188a6dcc37e285c9a8ef2d85a77c7c1b3418",
    "geoktools/scoring/run_llm_judge.py": "9f2b99b77f1c130b3572bf0412cc07ea9e1982e6ed572881b135de5ec4678f6b",
    "geoktools/scoring/llm_judge_system_prompt.txt": "7f98e19e9955d992504a3cba424bc015b63d39f5d14553da15d9f41f055494a3",
    "geoexpertqueries/autorater/run_autorater.py": "08e75e2138d49f4010c67429530e095db8bee0d14a2bbc55313d8e13f049ca37",
    "geoexpertqueries/autorater/tool_agnostic_autorater_prompt.txt": "7afa2d44b5b361890ccb9eb22d402ed5cf21bd0eb12370a40679d4ad3b7d67c9",
    "geoexpertqueries/autorater/workflow_autorater_prompt.txt": "c4f04a05211e034b1f5b266c2123697a5592c218da2d1e118fcc876b8626bee2",
    "geoexpertqueries/autorater/likert_scales.json": "000bd4413b8b5deb3e43a4da87b109765aff11c023bf417cd5f5dc2b41731caf",
}
EXTRA = ("LICENSE", "LICENSE-DATA", "README.md", "CITATION.bib", "geoktools/__init__.py",
         "geoktools/scoring/__init__.py", "geoexpertqueries/__init__.py", "geoexpertqueries/autorater/__init__.py")

TERMS = f"""Google geospatial-reasoning-benchmarks ({REPO} @ {COMMIT[:7]})
  Code: Apache-2.0. Benchmark materials: CC BY 4.0, (c) 2026 Google LLC.
  Google Maps-derived values in GeoKTools golds are EXCLUDED from both licences and are
  provided solely to allow evaluation (Google Maps / Google Earth Additional Terms of Service).
  Google asks that the data not be used for training. Use it for evaluation only.
Re-run with --accept-terms to acknowledge and download."""


def cache_root(cache_dir: str) -> str:
    return os.path.join(cache_dir, "benchmarks", f"georeason@{COMMIT[:7]}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(cache_dir: str, accept_terms: bool = False) -> str:
    root = cache_root(cache_dir)
    stamp = os.path.join(root, ".verified")
    if os.path.exists(stamp):
        return root
    if not (accept_terms or os.environ.get("QGISSI_ACCEPT_BENCHMARK_TERMS") == "1"):
        raise SystemExit(TERMS)
    data = HttpClient(timeout=120).get_bytes(ARCHIVE, limit=50_000_000)
    wanted = set(FILES) | set(EXTRA)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) != 2 or parts[1] not in wanted or not member.isfile():
                continue
            relative = parts[1]
            content = archive.extractfile(member).read()  # type: ignore[union-attr]
            expected = FILES.get(relative)
            if expected and _sha(content) != expected:
                raise SystemExit(f"Checksum mismatch for {relative}; refusing to use the download.")
            target = os.path.join(root, *relative.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(content)
    missing = [f for f in FILES if not os.path.exists(os.path.join(root, *f.split("/")))]
    if missing:
        raise SystemExit(f"Download incomplete, missing: {missing}")
    with open(os.path.join(root, "EVALUATION_ONLY.txt"), "w", encoding="utf-8") as fh:
        fh.write(TERMS.rsplit("\n", 1)[0] + "\n")
    with open(stamp, "w") as fh:
        fh.write(COMMIT)
    return root


def load_cases(root: str, name: str) -> Dict[str, Any]:
    path = os.path.join(root, name, "dev_queries.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
