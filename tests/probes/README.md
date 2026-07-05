# Optional probes

This directory contains probes that are useful before a release but too heavy
or environment-specific for the normal test suite.

## Port parity

`port_parity.py` compares upstream SWE-Pruner's PyTorch implementation with
Needle's MLX port on identical `(observation, goal hint)` pairs. It compares
the line masks each implementation keeps after thresholding, then reports exact
mask agreement, line agreement, and kept-line Jaccard.

This is not a product benchmark. It does not claim that the pruner solves SWE-
bench or that the selected lines are always sufficient. It answers a narrower
porting question:

> Given the same checkpoint, prompt format, threshold, and input text, does
> Needle's MLX implementation select the same lines as upstream SWE-Pruner?

On small Apple Silicon machines, run the two backends in separate processes.
That lets torch/MPS and MLX release memory between runs.

```sh
cases=tests/probes/fixtures/port_parity_cases.jsonl
model=/Users/enoch/.needle/models/ayanami-kitasan--code-pruner
upstream=/Users/enoch/repos/swe-pruner/swe-pruner
torch_py=/Users/enoch/repos/needle-code-pruner-mps/experiments/code-pruner-mps/.venv/bin/python
mlx_py=/tmp/needle-pr37-home/python/venv/bin/python

"$torch_py" tests/probes/port_parity.py \
  --cases "$cases" \
  --backends torch \
  --torch-device mps \
  --upstream-repo "$upstream" \
  --model-dir "$upstream/model" \
  --max-length 512 \
  --output /tmp/needle-port-parity-torch.json

PYTHONPATH=python NEEDLE_MODEL_DIR="$model" "$mlx_py" tests/probes/port_parity.py \
  --cases "$cases" \
  --backends mlx \
  --model-dir "$model" \
  --max-length 512 \
  --mlx-batch-size 1 \
  --output /tmp/needle-port-parity-mlx.json

python3 tests/probes/port_parity.py \
  --merge-reports /tmp/needle-port-parity-torch.json /tmp/needle-port-parity-mlx.json \
  --output /tmp/needle-port-parity-merged.json
```

For a no-model parser/report sanity check:

```sh
python3 tests/probes/port_parity.py \
  --cases tests/probes/fixtures/port_parity_cases.jsonl \
  --backends none \
  --output /tmp/needle-port-parity-none.json
```

For a built-in chunking stress case:

```sh
python3 tests/probes/port_parity.py --synthetic --backends none
```

## Current evidence

`results/2026-07-05-port-parity-summary.json` records the first local parity
run. The checkpoint used by upstream torch/MPS and Needle MLX was verified
byte-identical with `cmp`, and both configs had the same hash.

The recorded run supports one claim: for these mask-generation fixtures,
Needle's MLX port matches upstream SWE-Pruner. It is not a quality evaluation.
Several fixture-level misses appear in both ports, which means those misses
belong to the model/policy rather than the MLX port.
