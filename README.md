# Needle

Needle is being rebuilt as a Pi-first local pruning runtime.

The product is the Rust `needle` binary. Python is private worker machinery for
the MLX Soft-LaMR model path.

## Current Shape

```text
crates/
  needle-manager/        # Rust runtime: CLI, daemon, worker lifecycle

pi/                      # Pi package: extension + goal-hints skill
  extension.js           # overrides read/bash, requires context_focus_question
  client.mjs             # NDJSON socket client
  skills/needle-goal-hints/

python/
  needle_worker/         # private Python worker package
    worker.py            # worker entrypoint
    soft_lamr/           # MLX model implementation

tests/                   # worker/model/extension tests
```

The old Python CLI, MCP host, backend registry, Homebrew formula, and runtime
manager have been removed from this worktree. They were part of the previous
product story and should not be treated as live architecture.

## Worker Checks

```bash
PYTHONPATH=python python3 -m needle_worker --help
PYTHONPATH=python python3 tests/test_worker.py
PYTHONPATH=python python3 tests/test_repair.py
PYTHONPATH=python python3 tests/test_backends.py
PYTHONPATH=python python3 tests/test_code_pruner_batching.py
PYTHONPATH=python python3 tests/test_code_pruner_chunking.py
PYTHONPATH=python python3 tests/test_code_pruner_profiling.py
PYTHONPATH=python python3 tests/test_code_pruner_backbone.py
PYTHONPATH=python python3 tests/test_model_download.py
```

## Rust Checks

```bash
cargo check
cargo test
```

## Prune From The CLI (development)

The `needle` binary drives the Python worker end to end:

```bash
cargo build
./target/debug/needle prune --query "what does the merge step do?" path/to/file.py
./target/debug/needle prune --query "..." --json < input.txt
```

Pruned text goes to stdout; a `decision (reason) · chars · backend` summary goes
to stderr (`--json` emits one envelope on stdout instead). Worker or model
failures exit non-zero and loudly — there is no silent raw-text fallback.
`NEEDLE_PYTHON` selects the worker Python; `NEEDLE_MODEL_DIR` points at a local
model directory.

## Daemon

```bash
./target/debug/needle daemon          # foreground; socket under NEEDLE_HOME/runtime
./target/debug/needle status          # mode · backend status · sessions
```

The daemon serves NDJSON over a unix socket: `enable`, `disable`, `heartbeat`,
`prune`, `mode`, `backend_status`, `status`, and `original` (the pre-prune text
of a session's last prune, for over-pruned non-idempotent commands). Campfire
lifecycle: the first `enable` lights it and blocks until the model is resident;
the last `disable` — or a lease missing its heartbeats — unloads the worker,
removes the socket, and exits the process. Control ops never queue behind model
work. The socket is same-UID only, mode 0600, with 16 MiB bounded frames.

## Pi Extension (development)

```bash
cargo build
NEEDLE_BIN=$PWD/target/debug/needle pi --no-extensions -e pi/extension.js --skill pi/skills/needle-goal-hints
```

The extension overrides Pi's native `read` and `bash` tools with
`context_focus_question` required in the schema, spawns the daemon on demand,
and routes observations through it. Blocking semantics: the first tool call
waits for daemon startup and model residency. Missing focus questions and
runtime failures produce a visible banner in the observation — never a silent
pass-through. Host envelope lines (truncation notices) are split off before
pruning and reattached verbatim; error results (non-zero exits) are never
pruned. The statusline shows off/loading/busy/resident/failed plus session
savings; `/needle status|on|off` controls it.

## Direction

Rust owns:

- CLI and setup flow
- Pi integration
- daemon/session/lease lifecycle
- worker process lifecycle
- status and visible failure states

Python owns:

- MLX imports
- model download/load
- inference
- model-local cleanup

The next product milestone is a Rust `Worker` that owns the long-running
`python -m needle_worker` child process.
