# Needle

Needle is a local pruning layer for [Pi](https://github.com/mariozechner/pi).
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
The other 61% of those tokens never entered its context window; retention is
what the measurements table below has to earn.

## Install

```bash
curl -fsSL https://e24z.github.io/needle/install.sh | bash
needle
```

The bare `needle` command runs the setup wizard on an unconfigured machine:
system check, Pi check, private worker venv, model download (~1.5 GB), Pi
integration. Every mutation is behind its own confirmation; everything lands
under `NEEDLE_HOME` (default `~/Library/Application Support/Needle`);
`needle setup --dry-run` prints intentions and touches nothing. Uninstall is
first-class: `needle uninstall`, or `--purge` to remove all local state
including the model.

Requires an Apple Silicon Mac (the model runs on MLX) and Pi. See
[TESTING.md](TESTING.md) for a full walkthrough with expected results.

## The contract

**If Needle is on, it's on.** Supported observations go through the model —
cold start, slow load, and memory pressure are never reasons to silently skip
pruning. The first tool call after a cold start blocks until the model is
resident; the statusline spinner is the explanation for the wait.

**Failure is loud.** If the model can't load (including the low-memory refusal
on constrained machines), the observation arrives unpruned with a visible
`[needle failed: ...]` banner and an off-ramp (`/needle off`). There is no
silent raw-text fallback anywhere in the runtime.

**Structure is protected.** Exit codes, error statuses, and truncation notices
ride outside the pruner and are never dropped. Pruned Python is repaired to
parse (`ast.parse`-verified); when repair can't guarantee that, it says so and
steps aside. Unchanged results carry explicit reasons, not shrugs.

**The original is recoverable.** The daemon caches the pre-prune text of each
session's last prune — `/needle original` retrieves it, so an over-pruned
non-idempotent command never forces a re-run.

## Why Pi only

Pruning quality is bounded by the focus question the agent attaches to each
tool call. Pi lets an extension own the native tool schemas, so
`context_focus_question` is *required* — the model must write one. Hosts where
an extension can only offer a competing tool (the MCP path) cannot enforce
that contract: the model may ignore the tool or omit the question, and the
pruning layer degrades to an ornament. That asymmetry is the finding, and it
is why there is no MCP surface here.

A bundled skill (`needle-goal-hints`) teaches the agent to write good focus
questions; `verbatim: true` is the escape hatch for patch-sensitive reads.

## Measurements

Numbers pending — the harness exists and the lane is chosen (SWE-bench subset,
containers hosted on Modal, pruning local). This table is a stub to be replaced
before these claims are cited anywhere:

| metric | value |
| --- | --- |
| observations evaluated | _TBD_ |
| mean char reduction | _TBD_ |
| answer retention (LLM-judged) | _TBD_ |
| cold model load | _TBD_ |
| warm prune p50 / p95 | _TBD_ |
| repair on vs off savings | _TBD_ |

What exists today is behavioral evidence: every contract above is covered by
tests (Rust daemon integration, Node extension suite, Python worker/repair
suites), and the install/prune/uninstall loop is verified live on this
hardware.

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

The daemon is a campfire: the first session's `enable` lights it and blocks
until the model is resident; the last `disable` — or a lease missing its
heartbeats — unloads the model, removes the socket, and exits. No sessions, no
resident model, no daemon.

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
