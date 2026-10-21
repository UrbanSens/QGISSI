# Development log

The development of QGISSI 0.1.0 (21 October 2026), recorded in order.

## 1. Research

- **TerraLab AI Agent v1.9.0.** We read the GPL source: about 480 files, 229 tools, a WebSocket
  protocol to `agent.terra-lab.ai`, and client-side safety. Findings are in
  [REVERSE_ENGINEERING.md](REVERSE_ENGINEERING.md). The key point is that the model, the prompts and
  the tool descriptions are all server-side. The client is a tool executor with a strong safety kernel,
  and it offers no model choice, offline mode or evaluation.
- **Google geospatial-reasoning-benchmarks** (commit `c21a6d9`). We read the data formats, the scorer,
  the judge contracts and the licensing. Golden answers contain Google Maps-derived values that are
  excluded from the licences, so we download them on demand and do not vendor them. The judge runners
  need Python ≥ 3.10, which means they run out of process.
- **`rea` (Reverse Engineer Anything).** This MCP toolkit targets binaries, Electron apps, APKs and
  network captures. TerraLab's plugin is plain Python source, so reading it directly was the faithful
  method, and `rea` was not needed.
- **Environment.** The development machine has QGIS 3.36.3 on macOS with Python 3.9.5 and 683 Processing
  algorithms, plus Ollama with `mistral:latest`. The 27B local model does not fit in 16 GB of RAM. No
  Anthropic API key was available during development.

## 2. Key decisions

| Decision | Why |
|---|---|
| Agent loop inside QGIS, bring-your-own model | Removes the vendor server, the account and the lock-in; makes the plugin auditable and usable offline |
| Standard-library HTTPS instead of vendor SDKs | QGIS on macOS ships Python 3.9; current SDKs need ≥ 3.10, and plugins cannot rely on pip |
| Anthropic content blocks as the canonical message format | Claude replies, including thinking signatures, replay verbatim; other providers convert to and from it |
| Append-only history, frozen system prompt and tools | Prompt-cache hits, and validity of preserved thinking on Opus 5.5 / Sonnet 5.5 / Haiku 5.5 / Fable 5.1 |
| Explicit effort for Opus 5.5; never `budget_tokens` or forced `tool_choice` | Required by the API on the 5.5 models (`budget_tokens` and forced `tool_choice` return 400); Opus 5.5 defaults to `medium` |
| Server-side fallback on in the plugin, off in benchmarks | Users get resilience; benchmark results stay attributable to one model |
| Processing via search, help, run (3 tools) instead of one tool per algorithm | About 700 algorithms with a small, stable tool list |
| Refuse distance parameters in degrees | A classic silent GIS error (a 50° buffer instead of 50 m) |
| Benchmark checkers inspect project state; independent ground truth; oracle | Scores reflect real outcomes; the oracle validates every checker |
| MCP server | Other agents (Claude Code, Claude Desktop, ...) can use the same QGIS tools |

## 3. Bugs found by testing and fixed

| Found by | Problem | Fix |
|---|---|---|
| Oracle run (28/30) | `native:joinattributestable` and `native:joinbynearest` declare their main `OUTPUT` optional, so no output layer was produced | Always produce the primary output |
| Oracle run | CRS strings in Processing parameters made GDAL probe "files" called `EPSG:25832` | Pass `QgsCoordinateReferenceSystem` objects |
| Smoke test | A tool parameter named `color_ramp` shadowed a helper function | Renamed the helper |
| Smoke test | A trace function raising `TimeoutError` caused an audit event that reported the wrong error | Do not audit `sys.settrace`; the AST guard blocks `settrace` instead |
| Undo test | Undo did not remove fields the run had added | Restore the schema as well as the features |
| Undo test | A GeoPackage keeps recent writes in its WAL file, so copying the `.gpkg` back did not undo them, and overwriting an open SQLite file is unsafe | Snapshot schema and features through the data provider |
| QGIS 4.2 CI (39/40) | `QIODevice.WriteOnly` does not exist in PyQt6 | `QIODevice.OpenModeFlag.WriteOnly` |
| QGIS 4.2 CI | Benchmark CLI segfaulted during interpreter teardown after finishing | Clean `exitQgis()` and `os._exit()` |
| Repeated test runs | `reorderGroupLayers()` segfaulted intermittently (2 of 5 runs) | Insert layers at explicit tree positions (8/8 runs clean) |
| Repeated test runs | Checkers opened a GeoPackage connection per query, and SpatiaLite's log volume got the process throttled by macOS | Read ground truth into memory once |
| Long local-model run (stalled at task 18) | Every GeoPackage open initialises SpatiaLite, whose SQLite warnings macOS's system SQLite sends to the unified log; after a few hundred opens macOS throttled the process | Fixture layers are read once per process; each task gets in-memory copies (240 oracle runs in 16 s) |
| GUI probe | Headless runs wrote settings into a QGIS profile folder outside the project | Isolated profile via `QGIS_CUSTOM_CONFIG_PATH` (the constructor's `profileFolder` argument is ignored) |
| Live API check | Open-Meteo returns no precipitation for ERA5-Land | Per-variable fallback to ERA5, with provenance in the result |
| Local-model baseline | Mistral produced an unbounded generation (958 s for one task) | Cap Ollama output (`num_predict`) |
| Local-model baseline | Correct guesses with no tool calls earned credit; the injection task could be passed by doing nothing | Grounding rule; the injection task must summarise the real notes |

## 4. Independent code review

A separate review pass, which reproduced most of its findings with QGIS's Python, found eight defects
before the merge to `main`. All are fixed and each has a regression test:

| Severity | Defect | Fix |
|---|---|---|
| Critical | `ask_user` ran on the GUI thread and waited for an answer there, freezing QGIS | Tools can declare `main_thread=False`; `ask_user` runs on the worker like approvals; the prompter falls back to a local event loop on the GUI thread |
| Critical | `raise SystemExit` in agent code escaped every handler and closed QGIS | `BaseException` contained in the code tool, registry, agent loop and worker; the guard rejects raising it |
| High | In-place Processing algorithms (`native:truncatetable`, SQL execution, imports that overwrite tables) and file overwrites ran without approval or undo | Per-call risk: such calls are *destructive* (approval in Balanced mode, inputs snapshotted); Processing refuses existing outputs unless `overwrite=true` |
| High | Tripwire bypasses: `os.replace`/`os.truncate`, threads, a Python subprocess, shell chaining | Both paths of renames and hard links checked; more path events confined; thread start blocked (audit event on 3.10+, wrapped primitive on 3.9, which has no such event); only single GIS binaries may be spawned |
| Medium | Stop blocked the GUI on the HTTP reader's lock and ended the run as an error | Stop shuts the socket down; unstarted main-thread calls are abandoned; a worker still running at unload is kept alive |
| Medium | Undo could revert an older run | Baseline taken at run start; every non-read tool marks the run as changed; undo removes only layers the run created |
| Medium | Undo discarded the user's own unsaved edits | Edit buffers rewound to the undo-stack position at run start; data restored as a minimal diff that keeps feature ids |
| Low | A retried stream duplicated text in the panel | The provider signals a reset; the panel drops the partial output |

## 5. Verification

- **Unit tests:** 103 tests, no QGIS, including a real local HTTP server that checks Stop interrupts a
  silent stream at once. They cover provider payloads and stream parsing (Anthropic,
  OpenAI, Gemini, Ollama), agent-loop semantics, registry coercion, the code guard, scrubbing, pricing,
  Markdown, and scorer parity with all 30 upstream test vectors. They pass on Python 3.9 and 3.12.
- **QGIS integration:** 53 tests: all 30 benchmark tasks via the oracle, negative scoring tests, tool
  guards, every reported tripwire bypass, undo (including your own unsaved edits and destructive
  Processing), rendering and layouts, raster and vector styling, the GUI approval and question flows
  (offscreen), encrypted key storage and the MCP server. They pass on QGIS 3.36 (macOS) and, in CI, on QGIS 3.44 LTR and
  QGIS 4.2 (Qt6).
- **Real application:** the packaged ZIP loads in the QGIS 3.36 desktop app; the dock opens with all
  model presets.
- **Live services:** Nominatim geocoding with boundary polygons; Overpass downloads; Open-Meteo ERA5 and
  ERA5-Land; the Rhode Island boundary extent matches the published GeoKTools example answer.
- **Benchmark pipelines** run end to end with a local model on GeoKTools and GeoExpertQueries, producing
  official-format predictions and traces.

## 6. Open items

- Run the Claude model benchmarks (Opus 5.5 / Sonnet 5.5 / Haiku 5.5) once an API key is available, and
  publish the results in [BENCHMARKS.md](BENCHMARKS.md).
- Run long Processing algorithms in a `QgsTask`, so QGIS stays responsive.
- Optional child-process isolation for agent code.
- OpenAI Responses API adapter, to carry reasoning between turns.
- Data Commons, NEX-GDDP-CMIP6 and Aqueduct tools, to reach more GeoKTools templates.
- Persist chats per project.
