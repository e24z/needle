# Examples

This directory contains small, inspectable Needle examples. They are product
evidence, not benchmark results: each one shows what a Pi session saw before
and after Needle pruned a tool observation.

## What Is Here

- `fixtures/` contains the raw observation text used as input.
- `traces/pi-sessions.jsonl` contains generated Pi-session trace records.
- `traces/README.md` documents how to regenerate those records.

A fixture is just a stable test input. For example,
`fixtures/noisy_sentinel.txt` is a synthetic noisy command output. It includes
one deliberately important line:

```text
NEEDLE_SENTINEL: pruning works
```

That line is the sentinel: a planted line that the pruner should keep while
discarding surrounding noise.

## How The Traces Work

`scripts/generate-pi-session-traces.mjs` loads the real Needle Pi extension in
a tiny in-process Pi harness. For each fixture it:

1. Pretends the agent made a Pi tool call (`read` or `bash`).
2. Sends the tool output through the Needle extension and daemon.
3. Captures the pruned result returned to the agent.
4. Captures `/needle status`, `/needle original`, the statusline text, and
   Needle's counters.
5. Writes one JSONL record to `traces/pi-sessions.jsonl`.

The committed records were generated with the real local code-pruner backend.
The generator also has a `--backend trace` mode for CI-friendly shape checks
without MLX or model residency.

## What The Examples Show

- `pi-read-batch-guardrail`: a source-code `read` keeps the batching data
  structure and split function while removing unrelated helpers.
- `pi-bash-noisy-sentinel`: a noisy `bash` observation keeps the
  `NEEDLE_SENTINEL` line and removes a large amount of surrounding noise.
- `pi-read-late-statusline-cost`: a `read` keeps relevant cost-statusline logic
  near the end of the file, guarding against beginning-only pruning behavior.

Each trace also proves that `/needle original` can recover the exact fixture
text for the most recent prune.

## Validation

`tests/test_example_traces.py` checks that the committed trace records still
match their fixtures, hashes, counters, and must-keep/must-drop assertions. The
PR workflow runs that verifier with the rest of the worker tests.
