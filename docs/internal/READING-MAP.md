# Reading map — understanding Needle in an hour

Read the **orchestration**, in dependency order. The model is a sealed black box
-- you do not need ML to understand Needle.
The hard, interesting part is the systems: a machine-wide model that loads lazily,
stays alive across sessions, and evicts itself before it freezes the laptop.

Three active layers:

- `needle/registry.py` plus `needle/registry_data/` are the built-in package
  graph: what a user installs, what it claims, which backend it uses, and what
  evidence backs it. Package-level runtime profiles live here too; they are
  launch/tuning presets, not capabilities.
- `needle/backends/` is the backend contract plus fake/debug backends and the
  current MLX code-pruner backend.
- `needle/runtime/` is the resident runtime: socket protocol, client, manager,
  session leases, event log, memory guard.
- `pruner/` is a compatibility facade for old imports and `python -m pruner`.
- `needle/hosts/pi/` is the packaged Pi host binding: native read/bash
  wrappers, status, package identity, and the Pi package manifest.
- `needle/hosts/mcp/` is the packaged MCP host binding: one bash observation
  tool plus the stdio server launched by `needle mcp serve`.

## Read in this order

1. **`needle/runtime/naming.py`** — *Where does everything live, and how is identity
   derived?* Paths, the single machine-wide socket, and `code_version()` (the basis
   for the stale-manager handshake). Foundation for everything else.

2. **`needle/registry.py`** — *What is the active package graph?* This validates
   protocols, capabilities, backends, bindings, packages, runtime profiles,
   claim cards, fixture packs, and backend launch metadata without importing
   MLX.

3. **`needle/cli.py`** — *How does a user control packages and runtime state?*
   The public Typer CLI owns `needle setup pi`, `needle setup claude-code`,
   `needle setup codex`, `needle mcp serve`, `needle package ...`, `needle evidence check`,
   `needle status`, `needle stop`, `needle uninstall`, and `needle model ...`.

4. **`needle/runtime/protocol.py` + `needle/runtime/client.py`** — *How do the pieces talk?* The
   wire format and the thin client every surface (adapter, status, CLI) uses to
   reach the manager. Small; read together.

5. **`needle/runtime/session.py`** — *How does a session keep the manager alive without
   owning it?* This is the presence model: ensure-a-manager-exists (detached
   spawn), acquire a lease, heartbeat while alive, release on exit, and step aside
   for newer code. Was "the session owns the daemon"; now the session owns only its
   lease.

6. **`needle/runtime/manager.py`** — *Who owns the model, and when does it load/evict?*
   THE file. Lease accounting, lazy load on first prune, idle + memory-pressure
   eviction, first-writer-wins socket bind, the version handshake. Read it after 1–3
   so the vocabulary is already familiar.

7. **`needle/runtime/sysmem.py`** — *What stops it freezing an 8 GB laptop?* The memory gate
   that refuses the cold load / evicts under pressure. This is the crash-safety the
   whole residency design hinges on.

Then the **adapter surface** (how it becomes visible and safe):

8. **`needle/hosts/pi/client.mjs`** — *How does a Pi tool result become a pruned
   result, and why can it never break the agent?* The fail-open boundary:
   missing focus, backend errors, tiny output, and unsupported shapes pass the
   original text through unchanged.

9. **`needle/hosts/pi/extension.js`** — *How is the package exposed to Pi?* Lifecycle,
   tool registration, slash commands, and status surface.

10. **`needle/hosts/pi/demo-canary.mjs`** — *Can I see the Pi path work without
    Docker, paid APIs, SWE-bench, or live MLX?* This replays the checked fixture
    pack through mock Pi native tools and a mock Needle manager.

11. **`needle/hosts/mcp/bash.py` + `needle/hosts/mcp/server.py`** — *What is the
    portable reference adapter?* A single `needle_bash` observation tool,
    explicit optional focus, fail-open behavior, and no mutation ownership.

**Do NOT start with** `needle/backends/code_pruner/model.py` to "understand the
product." It's the sealed box: `prune_text(text, query) -> text`. Read it only
when working on MLX performance or model behavior.

## Poke at it in isolation (own state, won't touch your real `~/.needle`)

```bash
# no-model Pi canary: extension path, read prune, bash prune, pass-through, status
cd /path/to/needle
npm run demo:pi-canary

# package/evidence checks
uv run needle package doctor --host-binding pi/native-tools
uv run needle evidence check --host-binding pi/native-tools
uv run needle package doctor --host-binding mcp/bash
uv run needle evidence check --host-binding mcp/bash
uv run needle setup pi --dry-run
uv run needle setup claude-code --dry-run
uv run needle setup codex --dry-run

# terminal 1: a manager on its own socket/home (downloads the model on first prune)
cd /tmp/needle-sandbox && NEEDLE_HOME=/tmp/needle-sandbox-home uv run needle runtime manage

# terminal 2: feed it text and watch it prune
cd /tmp/needle-sandbox && NEEDLE_HOME=/tmp/needle-sandbox-home \
  uv run needle runtime prune -q "what does the manager do" < needle/runtime/manager.py
# and the operator view:
cd /tmp/needle-sandbox && NEEDLE_HOME=/tmp/needle-sandbox-home uv run needle runtime status
```

The first `prune` is slow (one-time model download + env build under uv); after
that it's warm. Watch `status` go down → cold → (loading) → ready, and
`~/.needle`-equivalent (`/tmp/needle-sandbox-home`) fill with `events.jsonl`.
```
