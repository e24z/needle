# Needle Source Layout

This is the installable Python package. The repository uses a `src/` layout so
tests do not accidentally import Python modules from the repository root.

The package is split by runtime surface:

- `cli.py` owns the public `needle` command.
- `runtime/` owns the resident manager, socket wire format, event log, and
  session lease code.
- `hosts/mcp/` owns the MCP bash observation server.
- `backends/` owns pruning implementations. The product path is
  `backends/code_pruner/`, which keeps the Python/MLX hot loop in Python.
- `model_download.py` owns local model snapshot resolution and provenance.

The root README is for users. This file is for people already reading the
source.

The installable product is `needle`. The legacy `pruner` package and entrypoints
are not part of this branch's public install surface. `HAY_*` environment names
are accepted only as early-install compatibility aliases; new docs should use
`NEEDLE_*`.
