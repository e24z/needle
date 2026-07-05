# Needle

Needle is a local pruning layer for [Pi](https://github.com/earendil-works/pi).
It sits between a tool call and the model: large `read`/`bash` observations go
through a small local MLX model that keeps the lines relevant to what the agent
is actually trying to learn, and drops the rest. Nothing leaves your machine.

Here is a real prune from this repo (local model, Apple Silicon):

```text
needle prune --query "how does the batch guardrail split oversized batches?" \
  python/needle_worker/soft_lamr/batching.py

needle: pruned (model) · 4390 -> 1723 chars · backend code-pruner
```

```python
[pruned]
@dataclass(frozen=True)
class BatchBudgetResult(Generic[T]):
    batches: list[list[T]]
    splits: int
    singles_over_budget: int
[pruned]
def split_batches_by_padded_token_budget(
    batches: list[list[T]],
    *,
    max_padded_tokens: int | None,
    length_fn: Callable[[T], int],
) -> BatchBudgetResult[T]:
    """Split batches so each rectangular model call stays under a token budget.

    A single long row can exceed the budget; that is recorded but not split.
    """
    ...
```

In this demo run, the agent asked a question and got 39% of the file back.
The other 61% of those tokens never entered its context window. The evidence
below explains what has been checked so far, and what has not.

## Install

```bash
curl -fsSL https://e24z.github.io/needle/install.sh | bash
```

The installer copies the binary, then starts the setup wizard immediately when
it is running in an interactive terminal. The wizard checks the system and Pi,
creates the private worker venv, downloads the model (~1.5 GB), and registers
the Pi integration. Every mutation asks first; everything lands under
`NEEDLE_HOME` (default `~/Library/Application Support/Needle`). If setup cannot
start because there is no terminal, the installer prints the exact
`needle setup` command to run later. `needle setup --dry-run` prints the planned
changes and touches nothing. `needle uninstall` removes the Pi integration;
`needle uninstall --purge` also removes local state, including the model.

Requires an Apple Silicon Mac (the model runs on MLX) and Pi. See
[TESTING.md](TESTING.md) for a full walkthrough with expected results.

## The contract

When Needle is enabled, supported observations wait for the daemon and resident
model. A cold start or slow model load can delay the first tool call, but it
does not silently bypass pruning.

If the model cannot load, including the low-memory refusal on constrained
machines, Needle returns the original observation with a visible
`[needle failed: ...]` banner and an off-ramp (`/needle off`). There is no
silent raw-text fallback in the runtime.

Exit codes, error statuses, and truncation notices stay outside the pruner.
Pruned Python is repaired to parse (`ast.parse`-verified); when repair cannot
guarantee that, Needle returns the original text with an explicit reason.

The daemon caches the pre-prune text for each session's last prune.
`/needle original` retrieves it, so an over-pruned non-idempotent command does
not force a re-run.

## Why Pi only

Pruning quality is bounded by the focus question the agent attaches to each
tool call. Pi lets an extension own the native tool schemas, so
`context_focus_question` is required: the model must write one. Hosts where
an extension can only offer a competing tool (the MCP path) cannot enforce
that contract: the model may ignore the tool or omit the question, and the
pruning quality then depends on optional behavior. For now, Needle targets Pi
only.

A bundled skill (`needle-goal-hints`) teaches the agent to write good focus
questions; `verbatim: true` is the escape hatch for patch-sensitive reads.

## Evidence

Needle does not yet have a SWE-bench quality benchmark or published latency
numbers. The current evidence is narrower:

| claim | current evidence |
| --- | --- |
| Release artifact installs | `scripts/package-release.sh` builds the macOS Apple Silicon tarball with the Rust binary, Pi package, goal-hints skill, and worker wheel. `site/install.sh --archive-url ... --prefix ...` installs that tarball, starts setup when an interactive terminal is available, and the installed binary reports `needle 0.1.0`. |
| Setup is recoverable | Rust setup tests cover dry-run, full setup into a throwaway `NEEDLE_HOME`, idempotent rerun, and bare `needle` entering the wizard on an unconfigured home. |
| Runtime contract is covered | `cargo test` covers daemon/session behavior, leases, status, prune/original recovery, frame limits, setup, and uninstall paths. |
| Pi integration is covered | Node tests cover tool overrides, required `context_focus_question`, daemon client behavior, status controls, and visible failure banners. |
| Worker behavior is covered | Python tests cover the worker protocol, backend selection, batching and chunking guardrails, profiling metadata, model download handling, AST repair, and example traces. |
| MLX port matches the upstream mask path on fixtures | `tests/probes/results/2026-07-05-port-parity-summary.json` records 6 local fixture/synthetic comparisons, 589 total lines, and exact mask agreement between upstream SWE-Pruner torch/MPS and Needle MLX using the same checkpoint, `max_length=512`, `threshold=0.5`, and repair disabled. |
| Quality evaluation is still pending | The parity probe does not measure answer retention or pruning quality. Fixture-level misses appear in both ports, so those misses belong to the model/policy, not the MLX port. |

## How it's put together

```text
crates/needle-manager/   # the product: CLI, setup wizard, daemon, worker lifecycle (Rust)
pi/                      # Pi package: extension + goal-hints skill + socket client
python/needle_worker/    # private MLX worker: model load, scoring, repair
site/                    # curl installer (GitHub Pages)
scripts/                 # release packaging, eval harness
tests/                   # Python, Rust-adjacent Node, and worker test suites
```

Rust owns the CLI, setup, Pi integration, daemon/session/lease lifecycle,
worker process lifecycle, and every visible state. Python owns MLX: model
download, load, inference, model-local cleanup. They speak one NDJSON protocol
over stdin/stdout (worker) and a 0600 unix socket (daemon; same-UID peers,
bounded frames).

The first session's `enable` starts the daemon and blocks until the model is
resident. The last `disable`, or a lease that misses its heartbeats, unloads
the model, removes the socket, and exits. No sessions means no resident model
and no daemon.

## Development

```bash
cargo build && cargo test                 # runtime
PYTHONPATH=python python3 tests/test_worker.py    # worker (see tests/ for the full list)
node tests/test_pi_extension.mjs          # extension behaviors
NEEDLE_BIN=target/debug/needle node tests/test_pi_client_daemon.mjs

# prune from the CLI
./target/debug/needle prune --query "..." path/to/file.py

# run Pi against the dev extension without touching your Pi config
NEEDLE_BIN=$PWD/target/debug/needle pi --no-extensions -e pi/extension.js \
  --skill pi/skills/needle-goal-hints

# build the release artifact (bin + Pi package + worker wheel)
bash scripts/package-release.sh
```

Installed runtimes honor `NEEDLE_HOME`, `NEEDLE_MODEL_DIR` (reuse an existing
snapshot), `NEEDLE_SOCKET`, and `NEEDLE_WORKER_OP_TIMEOUT_SECS` (default 600;
the per-operation worker response deadline). `NEEDLE_DEV_*` hooks are for
development and tests only and are not part of the install contract.
