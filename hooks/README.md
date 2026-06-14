# hooks/ — Claude plugin component (adapter)

Thin shims that route Claude's tool I/O through the `pruner` core. No substance
lives here.

- `post_tool_use.py` (Ring 2) — read PostToolUse JSON, call `pruner.client.prune`
  on Read/Grep/Glob output, return `updatedToolOutput` when savings clear a
  threshold; fail open (pass original through) if the server is down.

The query passed to `prune()` is extracted here. Per the old silent-breakage
bug, that extraction is first-class: log it and verify it's a real goal, never
the file path.
