# Pi Adapter

Packaged Pi adapter for Needle. This is native Pi extension glue, not a Python
bridge:

```text
Pi extension -> Needle runtime Unix socket -> pruner backend
```

The extension uses Pi lifecycle/tool/status events:

- `session_start`: ensure the machine-wide manager is running, acquire a lease,
  heartbeat while the Pi session is alive, and publish a Pi footer status via
  `ctx.ui.setStatus("needle", ...)`. The footer uses the status ontology developed
  during the archived Claude adapter work: down, cold, loading, degraded, ready,
  and active.
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
pi --extension needle/hosts/pi/extension.js --no-session --offline
```

Once loaded, `/needle` shows the operator snapshot: manager residency, memory
pressure, lease count, current Pi-session exact chars trimmed, socket/home
paths, and recent local events. `/needle doctor` also shows the exact extension
path, active Needle package, capability, backend, model directory, package
version, pyproject version, Git branch/commit, and dirty/clean state.
`/needle events 30` changes the event count. `/needle packages` is a Pi-local view of
Needle packages whose host binding is `pi/native-tools`; the canonical package
control plane is the host-neutral `needle package ...` CLI.

The Pi footer has a compact status indicator:

- `down`: no manager is listening; Needle fails open.
- `cold`: the manager is up, but the model is not loaded.
- `loading`: the serial manager is cold-loading or busy.
- `degraded`: Needle is running with a fallback backend.
- `ready`: the manager and backend are resident.
- `active`: a prune is in flight right now.

Install through Needle:

```bash
needle setup pi
```

`needle setup pi` calls Pi's native package command with the packaged adapter
directory at `needle/hosts/pi`. The root `package.json` remains a source-tree
development manifest and points at the same packaged extension file. The
extension resolves the active package backend from Needle's built-in registry
snapshot and starts the manager with the backend-declared launcher, currently
`needle runtime manage`.

For pre-1.0 distribution, `uv tool install --editable .` remains acceptable for
developers. Public users should get Needle through Homebrew or another package
manager once a release is tagged. Keep `package.json`, `pyproject.toml`, and
`needle/hosts/pi/package.json` versions aligned when cutting a release. During
local development, prefer `pi -e .` for the active working tree and
`/needle doctor` to confirm which checkout/ref Pi is actually running.

The bash path uses the same explicit focus contract as read. Missing
`context_focus_question` passes through unchanged.

Needle-owned runtime state defaults to `~/.needle`: config, socket, event log,
and local model files. `HAY_*` environment variables remain compatibility
aliases for early local installs, but new docs and commands should prefer
`NEEDLE_*`.

List, inspect, and select Needle runtime packages with the host-neutral CLI:

```bash
needle package list --host-binding pi/native-tools
needle package current --host-binding pi/native-tools
needle package doctor --host-binding pi/native-tools
needle evidence check --host-binding pi/native-tools
needle package use e24z/pi-local-mac-soft-lamr
```

Without the tool install, run the same commands from the repo with
`uv run needle ...`.

`e24z/pi-local-mac` is the default SWE-Pruner reference package: no AST repair.
`e24z/pi-local-mac-soft-lamr` extends the reference capability with Python AST
repair. If the resident runtime is already running, restart it after changing
the selected package so the backend policy and `/needle doctor` agree.

Run the Pi demo canary without Docker, paid APIs, SWE-bench, or live MLX:

```bash
npm run demo:pi-canary
```

The canary mounts the Pi extension against mock Pi native `read`/`bash` tools
and a mock Needle manager, then replays the checked evidence fixture pack. It
shows one read prune, one bash prune, one missing-focus pass-through, exact
character accounting, `/needle status` output, and recent local events. This
proves the Pi extension path and fixture wiring. It does not prove MLX model
quality, SWE-bench acceptance, token savings, or dollar savings.

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
needle setup pi --uninstall
```

Remove Needle-owned local runtime/config/model files with Needle:

```bash
needle uninstall --yes
```

Remove the CLI entrypoint with the package manager used to install Needle:

```bash
brew uninstall needle
uv tool uninstall needle
```
