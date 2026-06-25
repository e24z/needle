---
description: Show Hay's status — model residency, sessions, memory pressure, and recent events
---

# Hay Status

Run the operator snapshot and show the user its output verbatim:

```bash
cd "${CLAUDE_PLUGIN_ROOT}" && python3 -m pruner status
```

This is **read-only diagnostics**: it queries the local manager over its Unix
socket and reads the local event log. No arguments, nothing destructive, nothing
leaves the machine. It is plain stdlib, so it works even when the model's uv
environment is broken — which is exactly when it's useful.

How to read the first line for the user:

- **`ready (code-pruner resident)`** — the real model is loaded and idle. Healthy.
- **`cold (model not loaded)`** — manager is up but the model isn't loaded (it
  loads lazily on the next prune, or was evicted when idle / under memory
  pressure). Normal.
- **`DEGRADED (fake …)`** — the manager is up but the real model couldn't load
  (its reason is in the parentheses); Hay is passing text through unchanged. This
  is the one to flag — the model isn't actually pruning.
- **`down (not running)`** — no manager for this machine; the hook is failing
  open (harmless), but nothing is being pruned.

Surface the output as-is; don't embellish or invent fields.

## The status line glyph

If the user is asking what the small animated glyph in their status line means,
map it for them (it's the same residency state, one character):

- **`-` gray** — down (no manager).
- **`·` blue** — cold: manager up, model not loaded (lazy / evicted). The usual
  resting state.
- **`⠋` amber spinning** — loading the model or mid-prune.
- **`✗` red** — degraded: model couldn't load, passing text through unchanged.
- **`⠿` green pulsing** — ready: model resident and idle.
- **`⠋` cyan spinning** — active: a prune landed in the last few seconds.

If they don't have a status line at all, send them to `/hay setup`.
