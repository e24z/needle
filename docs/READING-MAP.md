# Reading map — understanding Needle in an hour

Read the **orchestration**, in dependency order. The model is a sealed black box
-- you do not need ML to understand Needle.
The hard, interesting part is the systems: a machine-wide model that loads lazily,
stays alive across sessions, and evicts itself before it freezes the laptop.

Three active layers:

- `needle/registry.py` plus `protocols/`, `capabilities/`, `backends/`,
  `bindings/`, `packages/`, `claims/`, and `package-cards/` are the package
  graph: what a user installs, what it claims, which backend it uses, and what
  evidence backs it.
- `needle/runtime/` is the resident runtime: socket protocol, client, manager,
  session leases, event log, memory guard.
- `pruner/` is now a compatibility facade for old imports and `python -m pruner`.
- `adapters/pi/` is the Pi host binding: native read/bash wrappers, status, and
  package identity.

## Read in this order

1. **`needle/runtime/naming.py`** — *Where does everything live, and how is identity
   derived?* Paths, the single machine-wide socket, and `code_version()` (the basis
   for the stale-manager handshake). Foundation for everything else.

2. **`needle/registry.py`** — *What is the active package graph?* This validates
   protocols, capabilities, backends, bindings, packages, claim cards, fixture
   packs, and backend launch metadata without importing MLX.

3. **`needle/cli.py`** — *How does a user control packages and runtime state?*
   The public Typer CLI owns `needle package ...`, `needle evidence check`,
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

8. **`adapters/pi/client.mjs`** — *How does a Pi tool result become a pruned
   result, and why can it never break the agent?* The fail-open boundary:
   missing focus, backend errors, tiny output, and unsupported shapes pass the
   original text through unchanged.

9. **`adapters/pi/extension.js`** — *How is the package exposed to Pi?* Lifecycle,
   tool registration, slash commands, and status surface.

10. **`adapters/pi/demo-canary.mjs`** — *Can I see the Pi path work without
    Docker, paid APIs, SWE-bench, or live MLX?* This replays the checked fixture
    pack through mock Pi native tools and a mock Needle manager.

**Do NOT read** `pruner/backends/code_pruner/model.py` to "understand the model."
It's the sealed box: `prune_text(text, query) -> text`. Treat it as given.

## Poke at it in isolation (own state, won't touch your real `~/.needle`)

```bash
# no-model Pi canary: extension path, read prune, bash prune, pass-through, status
cd /path/to/hay
npm run demo:pi-canary

# package/evidence checks
uv run needle package doctor --host-binding pi/native-tools
uv run needle evidence check --host-binding pi/native-tools

# terminal 1: a manager on its own socket/home (downloads the model on first prune)
cd /tmp/needle-sandbox && NEEDLE_HOME=/tmp/needle-sandbox-home uv run -m needle.runtime manage

# terminal 2: feed it text and watch it prune
cd /tmp/needle-sandbox && NEEDLE_HOME=/tmp/needle-sandbox-home \
  uv run -m needle.runtime prune -q "what does the manager do" < needle/runtime/manager.py
# and the operator view:
cd /tmp/needle-sandbox && NEEDLE_HOME=/tmp/needle-sandbox-home uv run -m needle.runtime status
```

The first `prune` is slow (one-time model download + env build under uv); after
that it's warm. Watch `status` go down → cold → (loading) → ready, and
`~/.needle`-equivalent (`/tmp/needle-sandbox-home`) fill with `events.jsonl`.
```
