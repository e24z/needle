# Benchmark Results

This directory is the tracked evidence layer for benchmark runs. Raw run
folders stay under `benchmarks/runs/` and are intentionally ignored because
they contain large trajectories, logs, caches, and generated predictions.

Each result summary should identify:

- the frozen slice (`benchmarks/slices/<slice-id>.json`)
- the policy (`baseline`, `hay-8192-floorless`, `hay-2048-chunked`, or an
  explicitly named custom policy)
- the backend and model
- the source run IDs and raw run directories
- Git identity when the run was produced by the current runner
- local validation counts, cost/call totals, and Hay telemetry

`benchmarks/swebench/run.py` exports compact summaries automatically when
`--slice-id` is set. Use `--no-export-result` only for scratch runs.

The initial `lite-test-django3-a` summaries were backfilled from raw smoke runs
that predated manifest Git stamping, so their `git` field records that caveat.
Future summaries should carry the runner-recorded Git metadata directly.
