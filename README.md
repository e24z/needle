# Needle

Needle is being rebuilt as a Pi-first local pruning runtime.

The product is the Rust `needle` binary. Python is private worker machinery for
the MLX Soft-LaMR model path.

## Current Shape

```text
crates/
  needle-manager/        # Rust runtime skeleton

python/
  needle_worker/         # private Python worker package
    worker.py            # worker entrypoint
    soft_lamr/           # MLX model implementation

tests/                   # worker/model tests
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
