# QGISSI: QGIS Spatial Intelligence

[![CI](https://github.com/UrbanSens/QGISSI/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/UrbanSens/QGISSI/actions/workflows/ci.yml)
![QGIS 3.28 to 4.x](https://img.shields.io/badge/QGIS-3.28%E2%80%934.x-589632)
![License GPL-2.0-or-later](https://img.shields.io/badge/license-GPL--2.0--or--later-blue)

**QGISSI is an open AI agent for QGIS that runs with your choice of model.** You describe a task in plain
language; the agent inspects your layers, runs QGIS Processing algorithms, queries and edits data, styles
maps, builds print layouts and checks its own results, inside the project you have open. It also ships a
**benchmark harness** that measures how well each model does that work, including Claude Opus 5.5.

QGISSI began as a source analysis of TerraLab's *AI Agent* plugin ([what we found](docs/REVERSE_ENGINEERING.md)).
That plugin sends every request to a closed vendor server and its model. QGISSI runs the agent loop locally
and works with any model:

| | TerraLab AI Agent | QGISSI |
|---|---|---|
| Model | Fixed by the vendor, server-side | **Claude Opus 5.5** (default), Sonnet 5.5, Haiku 5.5, Fable 5.1, OpenAI, Gemini, any OpenAI-compatible server, **Ollama (fully local)** |
| Account and cost | Vendor account, quota, paid plan | None. You pay your model provider at list price, or nothing with local models |
| Prompts and tool docs | Hidden on the server | In this repository |
| Autopilot, effort levels | Paid features | Free |
| Telemetry | On by default | None |
| Evaluation | None | QGISSI-Bench plus Google's GeoKTools and GeoExpertQueries |
| Other AI clients | No | MCP server for Claude Code, Claude Desktop and other MCP clients |

## Features

- **50 tools.** Project and layer inspection, statistics, expressions and ellipsoidal measurement. All
  ~700 QGIS Processing algorithms through *search → help → run*, with argument repair and a guard that
  refuses distances that would silently be in degrees. Single, categorised, graduated, heatmap and raster
  styling, plus labels. OpenStreetMap (Overpass), geocoding with boundaries (Nominatim), and ERA5
  historical weather (Open-Meteo). Layer creation, editing and export. Map rendering the model can look at.
  Print layouts and PDF export. Guarded PyQGIS code for everything else.
- **Built for current Claude models.** Adaptive thinking with an explicit effort level, prompt caching of
  tools and system prompt, preserved-thinking-safe append-only history, streaming tool input with strict
  JSON validation, refusal handling, and optional server-side fallback. Token and dollar costs are tracked
  live.
- **You stay in control.** Three approval modes: *Balanced* asks before edits and code, *Ask first*,
  and *Autopilot*. Per-run **undo** restores layers, styles, schema and features. Agent code passes a
  static guard, a runtime audit-hook tripwire and a time limit. Tool output from data or the web is labelled
  as untrusted to resist prompt injection, and secrets are masked before anything reaches a model.
- **Measurable.** A headless benchmark harness with 30 verifiable QGIS tasks and adapters for Google's
  geospatial reasoning benchmarks. It reports score, confidence intervals, cost, tokens and latency per
  model.

## Install

Requires QGIS 3.28 or newer, on Windows, macOS or Linux.

1. Build the plugin ZIP with `python3 scripts/package.py`, or download the `qgissi-plugin` artifact from a
   CI run.
2. In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP**, choose the file, then enable
   **QGISSI**.
3. Click the QGISSI toolbar icon, open **⋯ → Settings**, and add a key (for example from
   [console.anthropic.com](https://console.anthropic.com)). Keys are stored in the QGIS authentication
   database. To run without a key, pick an Ollama model such as `ollama/qwen3:8b`.

For development, `scripts/install_dev.sh` links this checkout into your QGIS profile.

## Use

Type a request in the panel, for example:

- *Which districts have the most schools? Make a choropleth of school density.*
- *Download the parks in Würzburg from OpenStreetMap and give me their total area in hectares.*
- *Buffer the river by 100 m, find the residential buildings inside and make an A4 PDF map.*

The panel streams the model's answer and shows every tool call (click to expand). When an action needs
approval, the exact arguments are shown first, and for code the full source. **⋯ → Undo last run** reverts
what a run changed. Tick **Attach map view** to send the model an image of the current map (vision models).

| Setting | Values |
|---|---|
| Model | `anthropic/claude-opus-5-5`, `anthropic/claude-sonnet-5-5`, `anthropic/claude-haiku-5-5`, `anthropic/claude-fable-5-1`, `openai/<model>`, `gemini/<model>`, `ollama/<model>`, `openai_compatible/<model>` |
| Effort | `low`, `medium` (Opus 5.5 default), `high`, `xhigh`, `max`. Higher effort thinks longer and costs more |
| Tools | All 50, or a 30-tool core set for small local models |
| Limits | Max steps and max dollar cost per run |

## Use QGIS from other AI agents (MCP)

QGISSI also runs as a [Model Context Protocol](https://modelcontextprotocol.io) server on a headless QGIS
project, so Claude Code, Claude Desktop and other MCP clients can use the same tools:

```bash
claude mcp add qgissi -- /path/to/QGISSI/scripts/qgis-python -m qgissi.mcp_server --project ~/maps/city.qgz --output-dir ~/maps/out
```

Use `--read-only` to expose only read-only tools, `--no-code` to hide code execution, and `--offline` to
disable the web data tools.

## Benchmarks

```bash
scripts/qgis-python -m qgissi.bench list                                     # the 30 tasks
scripts/qgis-python -m qgissi.bench run --models anthropic/claude-opus-5-5@medium,anthropic/claude-sonnet-5-5@medium --dry-run
scripts/qgis-python -m qgissi.bench run --models anthropic/claude-opus-5-5@medium --repeats 3
scripts/qgis-python -m qgissi.bench geoktools --models anthropic/claude-opus-5-5 --accept-terms
scripts/qgis-python -m qgissi.bench geoexpert --models anthropic/claude-opus-5-5 --judge anthropic/claude-opus-5-5 --accept-terms
```

QGISSI-Bench gives an agent a synthetic town and 30 tasks: inspection, measurement, CRS traps, vector and
raster analysis, styling, layouts, editing, multi-step workflows, and safety (a planted prompt injection and
a question about a district that does not exist). Checkers inspect the real QGIS project afterwards (layers,
fields, renderers, files), and ground truth is computed independently with GEOS and GDAL. A scripted
reference solution scores 30/30, and CI checks this on every push. Methods, the Google suites and all
results are in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

## Development

```bash
python3 -m pytest tests/unit                  # 100 tests, no QGIS needed
scripts/qgis-python -m pytest tests/qgis      # 45 tests in headless QGIS: tools, GUI, MCP, settings, all 30 checkers
python3 scripts/package.py                    # dist/qgissi-<version>.zip
```

`scripts/qgis-python` runs Python with the interpreter and libraries bundled with QGIS. Headless runs use
an isolated QGIS profile and never write to your QGIS settings. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for the design and [docs/DEVLOG.md](docs/DEVLOG.md) for the development log.

```
qgissi/
  core/       agent loop, prompts, context, approvals, undo, code guard, pricing, settings
  providers/  Anthropic, OpenAI-compatible, Gemini, Ollama (standard-library HTTPS, SSE streaming)
  tools/      the 50 QGIS tools and their registry
  ui/         chat dock, settings dialog, worker-thread bridge
  bench/      QGISSI-Bench, oracle, runner, scoring, reports, Google benchmark adapters
  mcp_server.py, headless.py, plugin.py
```

## License and attribution

QGISSI is free software under the **GNU GPL v2 or later** (see [LICENSE](LICENSE)).

- The design was informed by reading the GPL-2.0-or-later source of TerraLab's *AI Agent* plugin. No
  TerraLab code was copied. TerraLab, AI Agent, AI Segmentation and AI Edit are TerraLab's trademarks;
  QGISSI is not affiliated with or endorsed by TerraLab.
- The benchmark adapters work with [google-research/geospatial-reasoning-benchmarks](https://github.com/google-research/geospatial-reasoning-benchmarks)
  (code Apache-2.0, data CC BY 4.0, © 2026 Google LLC). The data is downloaded on demand for evaluation
  only and is not redistributed here.
- Map data © OpenStreetMap contributors (ODbL). Weather data from Open-Meteo, based on ERA5 / ERA5-Land
  (Copernicus).
