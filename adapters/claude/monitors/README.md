# monitors/ — Claude plugin component (session lease)

`monitors.json` declares one monitor that `cd`s into `${CLAUDE_PLUGIN_ROOT}` and
runs:

```bash
NEEDLE_BACKEND="${NEEDLE_BACKEND:-e24z/code-pruner-mlx}" uv run --extra backend-code-pruner-mlx -m pruner session --session "$CLAUDE_SESSION_ID"
```

The monitor does **not** own the model process. It owns this Claude session's
lease: it ensures the machine-wide manager is running, acquires a lease,
heartbeats while the session is alive, and releases on exit. The manager
outlives individual sessions and decides when to load or evict the model.

Gotchas:
- Requires Claude Code >= 2.1.105 (have 2.1.177).
- Monitors do NOT load for project-scope plugins. Hay must be installed
  personal-scope for the monitor to fire.
- `uv` must be installed. If the `code-pruner` dependencies are unavailable, the
  manager degrades loudly to pass-through instead of silently pretending to be
  healthy.
