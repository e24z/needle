# Needle package layout

This is the installable Python package. The repository uses a `src/` layout so
tests do not accidentally import Python modules from the repository root.

The package is split by product surface:

- `cli.py` owns the public `needle` command.
- `runtime/` owns the resident manager, socket protocol, event log, and session
  lease code.
- `backends/` owns pruning implementations behind the text transform contract.
- `hosts/pi/` owns the packaged Pi adapter.
- `hosts/mcp/` owns the portable MCP bash server.
- `registry_data/` is the built-in registry snapshot: protocols, capabilities,
  backends, host bindings, packages, package cards, claims, and fixture evidence.

The root README is for users. This file is for people already reading the source.

The installable product is `needle`. The legacy `pruner` package and entrypoints
are not part of this branch's public install surface. `HAY_*` environment names
are accepted only as early-install compatibility aliases; new manifests and docs
should use `NEEDLE_*`.
