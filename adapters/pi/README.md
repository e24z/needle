# adapters/pi

Pi adapter for Hay. This is native Pi extension glue, not a Python bridge:

```text
Pi extension -> Hay manager Unix socket -> pruner backend
```

The extension uses Pi lifecycle/tool/status events:

- `session_start`: ensure the machine-wide manager is running, acquire a lease,
  heartbeat while the Pi session is alive, and publish a Pi footer status via
  `ctx.ui.setStatus("hay", ...)`.
- `tool_result`: prune large `read`, `grep`, and `find` results through the Hay
  manager socket, returning a Pi partial result patch when savings clear the
  threshold.
- `session_shutdown`: release the lease.

Run locally without installing:

```bash
PI_CODING_AGENT_DIR=/tmp/pi-agent \
PI_CODING_AGENT_SESSION_DIR=/tmp/pi-sessions \
pi --extension adapters/pi/extension.mjs --no-session --offline
```

Once loaded, `/hay` shows the operator snapshot: manager residency, memory
pressure, lease count, current Pi-session savings, socket/home paths, and recent
local events. `/hay doctor` shows more events; `/hay events 30` changes the event
count.

Install from this repo as a Pi package:

```bash
pi install ./local/path/to/hay
```

The root `package.json` is the Pi package manifest. It points Pi at
`adapters/pi/extension.mjs`; the Python engine stays at the repo root so the
extension can start the manager with `uv run -m pruner manage`. For pre-1.0
distribution, prefer local or git installs pinned to a commit/tag. Keep
`package.json` and `pyproject.toml` versions aligned when cutting a release.

The adapter intentionally leaves `bash` alone for now. Shell output is high
variance and is the path that previously exposed memory-residency problems.
Add it only after the read/grep/find path is boring.
