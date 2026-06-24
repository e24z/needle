# adapters/pi

Pi adapter for Needle. This is native Pi extension glue, not a Python bridge:

```text
Pi extension -> Needle runtime Unix socket -> pruner backend
```

The extension uses Pi lifecycle/tool/status events:

- `session_start`: ensure the machine-wide manager is running, acquire a lease,
  heartbeat while the Pi session is alive, and publish a Pi footer status via
  `ctx.ui.setStatus("hay", ...)`. The footer mirrors the Claude statusline
  ontology: down, cold, loading, degraded, ready, and active.
- `read` override: register a Pi tool named `read`, delegate to Pi's own
  built-in read implementation, then prune large textual results through the
  Needle runtime socket when `context_focus_question` is present and savings clear
  the threshold. The model and user still see the normal `read` tool name. If
  the focus question is missing, the original output passes through unchanged.
- `bash` override: when Pi exposes its native bash tool factory, register the
  same wrapper for shell output. Mutation still belongs to Pi's native edit and
  write tools; Needle only prunes textual observations.
- `session_shutdown`: release the lease.

Run locally without installing:

```bash
PI_CODING_AGENT_DIR=/tmp/pi-agent \
PI_CODING_AGENT_SESSION_DIR=/tmp/pi-sessions \
pi --extension adapters/pi/extension.js --no-session --offline
```

Once loaded, `/needle` shows the operator snapshot: manager residency, memory
pressure, lease count, current Pi-session exact chars trimmed, socket/home
paths, and recent local events. `/needle doctor` also shows the exact extension
path, active Needle package, capability, backend, model directory, package
version, pyproject version, Git branch/commit, and dirty/clean state.
`/needle events 30` changes the event count. `/needle packages` is a Pi-local view of
Needle packages whose host binding is `pi/native-tools`; the canonical package
control plane is the host-neutral `needle package ...` CLI. `/hay` remains a
temporary alias.

Install from this repo as a Pi package:

```bash
cd ./local/path/to/hay
uv tool install --editable .
pi install .
```

The root `package.json` is the Pi package manifest. It points Pi at
`adapters/pi/extension.js`; the Python engine stays at the repo root so the
extension can start the manager with `uv run --extra mlx -m pruner manage`.
For pre-1.0 distribution, prefer local or git installs pinned to a commit/tag.
Keep `package.json` and `pyproject.toml` versions aligned when cutting a
release. During local development, prefer `pi -e .` for the active working tree
and `/needle doctor` to confirm which checkout/ref Pi is actually running. The
`needle` CLI itself stays lightweight.

The bash path uses the same explicit focus contract as read. Missing
`context_focus_question` passes through unchanged.

List, inspect, and select Needle runtime packages with the host-neutral CLI:

```bash
needle package list --host-binding pi/native-tools
needle package current --host-binding pi/native-tools
needle package doctor --host-binding pi/native-tools
needle package use e24z/pi-local-mac-soft-lamr
```

Without the tool install, run the same commands from the repo with
`uv run needle ...`.

`e24z/pi-local-mac` is the default SWE-Pruner reference package: no AST repair.
`e24z/pi-local-mac-soft-lamr` extends the reference capability with Python AST
repair. If the resident runtime is already running, restart it after changing
the selected package so the backend policy and `/needle doctor` agree.

For one run only, an environment variable can override the configured package:

```bash
NEEDLE_PACKAGE=e24z/pi-local-mac pi
NEEDLE_PACKAGE=e24z/pi-local-mac-soft-lamr pi
```

Stop the resident manager cleanly with:

```bash
needle stop
```

Remove the Pi extension from Pi's settings with Pi's native package command:

```bash
cd ./local/path/to/hay
pi uninstall .
```

Remove Needle-owned local runtime/config/model files with Needle:

```bash
needle uninstall --yes
```

Remove the CLI entrypoint installed above with uv:

```bash
uv tool uninstall needle
```
