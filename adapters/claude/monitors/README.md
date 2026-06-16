# monitors/ — Claude plugin component (lifecycle owner)

`monitors.json` (Ring 3) declares one monitor that `cd`s into
`${CLAUDE_PLUGIN_ROOT}`, picks `.venv/bin/python` if present (else `python3`),
and runs `HAY_BACKEND=${HAY_BACKEND:-mlx} <py> -m pruner serve`. The monitor
runs the server for the session's lifetime and tears it down on session end —
this is the "session owns the daemon" mechanism.

Gotchas:
- Requires Claude Code >= 2.1.105 (have 2.1.177).
- Monitors do NOT load for project-scope plugins. Hay must be installed
  personal-scope for the monitor to fire.
- The monitor is just a caller of `python3 -m pruner serve`; launchd or a manual
  run are interchangeable owners of the same command.
