# Changelog

## 0.1.0 (2026-10-21)

First release.

- **Agent**: local, provider-agnostic agent loop with streaming, approvals, budgets, loop detection,
  append-only history and per-step token and cost accounting.
- **Models**: Anthropic Claude (Opus 5.5 default; Sonnet 5.5, Haiku 5.5, Fable 5.1 and earlier),
  OpenAI and OpenAI-compatible servers, Google Gemini, Ollama. Standard-library HTTPS only.
- **Tools**: 50 tools for inspection, statistics, Processing (search, help, run with a CRS-unit guard),
  styling, open data (OSM, Nominatim boundaries, ERA5 weather), editing, rendering, layouts and guarded
  PyQGIS. A 30-tool core set for small models.
- **Safety**: approval modes, AST code guard, audit-hook tripwire, deadline, untrusted-output labels,
  secret scrubbing, and per-run undo of layers, styles, schema and features.
- **Plugin UI**: chat dock with model, effort and approval selectors, tool cards, approval and question
  cards, map attachment, cost meter, export; settings with encrypted key storage.
- **MCP server** exposing the tools to Claude Code, Claude Desktop and other MCP clients.
- **Benchmarks**: QGISSI-Bench (30 tasks, state-based checkers, oracle), adapters for Google's GeoKTools
  and GeoExpertQueries with a parity-tested scorer port and a configurable judge, Markdown and HTML
  reports.
- **Quality**: 100 unit tests and 45 QGIS integration tests. CI runs on QGIS 3.44 LTR and QGIS 4.2 (Qt6).
