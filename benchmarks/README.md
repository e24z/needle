# Hay Benchmarking

This branch uses Mini-SWE Agent as the benchmark driver and validates generated
patches locally in SWE-bench sandboxes. SBCLI remains an optional cloud
submission/archive path, but its report distinguishes cloud job status from
patch acceptance.

The trust boundary is intentionally small:

1. Mini-SWE Agent runs the SWE-bench task and writes standard `preds.json`.
2. Baseline mode uses Mini-SWE's stock Modal environment by default.
3. Hay mode uses a thin Mini-SWE environment wrapper that prunes command output
   through the Hay manager before Mini-SWE turns it into a model observation.
4. Local validation applies each prediction plus the official SWE-bench
   `test_patch`, then runs `FAIL_TO_PASS + PASS_TO_PASS` in a fresh sandbox.
5. SBCLI can optionally submit each mode's `preds.json` to the SWE-bench cloud.
6. The terminal observer reads local run files plus Modal container state while
   the run is happening.

In benchmark mode, Hay gets the current Mini-SWE assistant narration as the
pruning query (`context_focus_question`). This mirrors the shipping adapters'
"latest assistant text" strategy rather than forcing the model to emit a
benchmark-only goal hint. `--hay-query` exists only as an explicit override for
experiments.

Useful upstream docs:

- Mini-SWE SWE-bench: https://mini-swe-agent.com/latest/usage/swebench/
- SBCLI: https://www.swebench.com/sb-cli/
- Modal billing CLI: https://modal.com/docs/reference/cli/billing
- OpenAI API pricing: https://openai.com/api/pricing/

## Setup

Install the benchmark dependencies:

```bash
uv sync --extra bench
```

The `bench` extra includes Mini-SWE Agent, SBCLI, Modal, Streamlit, and
SWE-ReX's Modal runtime support. The normal pilot path is terminal-first; the
Streamlit dashboard is experimental.

Authenticate the local Modal CLI once:

```bash
uv run --extra bench modal setup
```

Make sure these commands are available from the benchmark environment:

```bash
uv run --extra bench python benchmarks/swebench/run.py --help
sb-cli --help
uv run --extra bench streamlit version
```

VibeProxy should be running on its OpenAI-compatible endpoint:

```bash
curl -H 'Authorization: Bearer dummy' http://localhost:8317/v1/models
```

SBCLI requires the SWE-bench API key in the environment:

```bash
export SWEBENCH_API_KEY='...'
sb-cli get-quotas
```

## Run A Benchmark

A benchmark run is one folder under `benchmarks/runs/`. It groups one
configuration: subset, split, slice, backend, model, and modes. A run can
contain baseline generation, Hay generation, or both. Local validation happens
after generation by default. SBCLI submission can happen inside the same run
when `--submit` is set.

`--workers` is the number of SWE-bench instances generated in parallel. Keep it
at `1` for pilots unless you intentionally want concurrent Modal sandboxes and
concurrent model calls.

Start small:

```bash
uv run --extra bench python benchmarks/swebench/run.py \
  --subset lite \
  --split test \
  --slice 0:1 \
  --workers 1 \
  --model gpt-5.5 \
  --vibeproxy-url http://localhost:8317/v1
```

By default this runs both modes and writes prediction files locally:

- `baseline`: Mini-SWE agent with the benchmark Modal environment wrapper
- `hay`: `benchmarks.swebench.hay_environment.HayModalEnvironment`

By default the runner also validates each prediction locally. Validation is
not another model run: it starts a fresh SWE-bench sandbox, applies the model
patch and the official SWE-bench `test_patch`, then runs the official
`FAIL_TO_PASS + PASS_TO_PASS` pytest targets. Disable it with
`--no-validate-local` only when you intentionally want generation without
acceptance evidence.

For Modal, the runner deliberately uses
`benchmarks.swebench.hay_environment.BenchmarkModalEnvironment` instead of
Mini-SWE's stock `swerex_modal` wrapper. SWE-bench images rely on
`BASH_ENV=/root/.bashrc` plus `bash -c` to activate the per-instance `testbed`
conda environment. The upstream Modal wrapper sends commands through
SWE-ReX `shell=True`, which runs `/bin/sh` and bypasses that activation path.

Use `--backend docker` only when you intentionally want local Docker:

```bash
uv run --extra bench python benchmarks/swebench/run.py --backend docker --slice 0:1 --no-submit
```

Outputs land under:

```text
benchmarks/runs/<run-id>/
  manifest.json
  baseline/
    preds.json
    local-validation.json
    minisweagent.log
    sb-cli.stdout.log
    sb-cli-reports/
  hay/
    preds.json
    local-validation.json
    hay_telemetry.jsonl
    minisweagent.log
    sb-cli.stdout.log
    sb-cli-reports/
```

Raw run folders are intentionally ignored by Git. For a named benchmark slice,
use `--slice-id`; the runner loads `benchmarks/slices/<slice-id>.json`, records
the slice in `manifest.json`, and exports a compact summary under
`benchmarks/results/<slice-id>/` by default.

Current named policies:

- `baseline`: stock Mini-SWE behavior for the same backend/model/slice
- `hay-8192-floorless`: 8192-token pruner window, no benchmark floor
- `hay-2048-chunked`: 2048-token chunk scoring, 50-token overlap, no benchmark floor

For the current three-instance Django smoke slice:

```bash
uv run --extra bench python benchmarks/swebench/run.py \
  --slice-id lite-test-django3-a \
  --modes baseline \
  --policy-id baseline \
  --workers 1 \
  --model gpt-5.5 \
  --vibeproxy-url http://localhost:8317/v1 \
  --no-submit

uv run --extra bench python benchmarks/swebench/run.py \
  --slice-id lite-test-django3-a \
  --modes hay \
  --policy-id hay-2048-chunked \
  --workers 1 \
  --model gpt-5.5 \
  --vibeproxy-url http://localhost:8317/v1 \
  --no-submit
```

Future `--slice-id` runs stamp the current Git identity into the manifest and
result summary. This makes the benchmark vocabulary stable: slice, policy,
backend, model, run ID, and commit.

Benchmark runs also set `HAY_BENCH_ABORT_ON_LOW_MEMORY=1` by default. If the
manager would return `passthrough:low-memory`, the run fails loudly and does not
export a result summary. Close memory-heavy apps and rerun instead of accepting
contaminated evidence.

For generation without cloud submission:

```bash
uv run --extra bench python benchmarks/swebench/run.py --slice 0:1 --no-submit
```

To revalidate an existing run without spending model tokens or SBCLI quota,
reuse its run ID. Existing `preds.json` entries are skipped unless
`--redo-existing` is set:

```bash
uv run --extra bench python benchmarks/swebench/run.py \
  --run-id <run-id> \
  --slice 0:1 \
  --no-submit
```

For an explicit one-mode run:

```bash
uv run --extra bench python benchmarks/swebench/run.py --modes baseline --slice 0:1 --no-submit
uv run --extra bench python benchmarks/swebench/run.py --modes hay --slice 0:1 --no-submit
```

Run IDs are generated from the actual benchmark settings, for example
`modal-paired-lite-test-slice-0-1-20260617-151500`. Hay benchmark runs use a
short run-specific `HAY_HOME` under `/tmp/hay-bench-*`, so they do not attach
to a stale production/global Hay manager and do not exceed macOS's AF_UNIX
socket path length limit.

The runner checks VibeProxy before starting and checks that the Hay manager is
reachable before `hay` mode starts. Use `--skip-vibeproxy-check` only when the
proxy is intentionally hidden from the runner.

The benchmark wrapper prunes only query-backed read commands by default. It
requires a `context_focus_question`/query, skips test execution, diffs, patch
submission, writes, and other non-read commands, and accepts any real reduction.
The code-pruner backend defaults to the SWE-Pruner-style 8192-token window and
returns the model's pruned text directly. Set `HAY_PRUNE_FLOOR=1` only when you
intentionally want the older product safety floor that falls back from tiny
snippets to skeleton/original output. Benchmark runs also set `HAY_MIN_FREE_MB=0`
unless the caller already supplied a value, so low-memory passthrough does not
mask pruning behavior. This is benchmark-only; production adapters keep the
manager's normal memory gate. If a
pilot needs explicit bounds, set `HAY_BENCH_MIN_CHARS`,
`HAY_BENCH_MIN_SAVINGS_RATIO`, or `HAY_BENCH_PRUNE_TIMEOUT` in the environment.
Hay's code-pruner backend now scores long outputs in overlapping token chunks;
telemetry records chunk count, estimated original/pruned tokens, estimated saved
tokens, and pruner model-input tokens when the backend provides them.

Check Modal usage from the CLI:

```bash
modal billing report --for "this month" --json
```

Check active Modal compute from the CLI:

```bash
modal container list
modal container logs <container-id> -f
```

Interpret SBCLI reports carefully. `failed_instances` means remote evaluator
jobs failed, not necessarily that submitted patches failed tests. A trustworthy
patch verdict requires `completed_instances > 0`; unresolved completed jobs are
test failures, while `completed_instances: 0` with `failed_instances > 0` is
cloud-job failure. The benchmark manifest stores local validation separately
under `modes.<mode>.local_validation` so dashboards should use that as the
acceptance source of truth.

On Modal's Starter plan, remaining monthly credit is approximately `$30 minus
this month's reported cost`.

## Live Status

Terminal benchmark observer:

```bash
uv run --extra bench python benchmarks/watch.py
```

or:

```bash
benchmarks/monitor.sh
```

Pass a specific run directory if you do not want the latest run:

```bash
uv run --extra bench python benchmarks/watch.py benchmarks/runs/<run-id>
```

The observer is read-only. It shows baseline and Hay side by side when both
modes exist, including local processes, Modal containers/log tails, Hay prune
telemetry, run logs, and simple in-memory sparklines as the run progresses.

Human watch mode:

```bash
python3 -m pruner status --watch --interval 1
```

Machine-readable one-shot status:

```bash
python3 -m pruner status --json
```

Machine-readable stream:

```bash
python3 -m pruner status --json --watch --interval 1
```

`--json --watch` emits newline-delimited snapshots so wrappers can consume it
without scraping the human text.

## Experimental Streamlit Dashboard

Run the Streamlit dashboard:

```bash
uv run --extra bench streamlit run benchmarks/dashboard.py
```

For a phone or Tailnet view, bind Streamlit to all interfaces and use the
Tailscale address from another device:

```bash
uv run --extra bench streamlit run benchmarks/dashboard.py --server.address 0.0.0.0
```

The dashboard refreshes while a run is in progress. It reads:

- `manifest.json`
- Mini-SWE `preds.json`
- Mini-SWE `*.traj.json`
- Hay `hay_telemetry.jsonl`
- SBCLI report JSON files
- live Hay manager status and recent events
- local macOS memory pressure and available-memory signals
- dashboard-launched process metadata from `dashboard-process.json`

The dashboard can also start and stop benchmark runs. Runs launched from the
dashboard write process metadata and a `dashboard-run.log` file into the run
folder so the page can reconnect from another browser session.

The chart palette uses Viridis.

## Existing Eval Folders

`evals/run_deterministic.py` remains useful as a small local integrity harness.
It is not the public benchmark story.

`eval/run_task.py` is older Claude-specific scaffolding and should not be used
for published results without being rewritten.
