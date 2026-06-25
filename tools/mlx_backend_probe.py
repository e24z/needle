#!/usr/bin/env python3
"""Run a tiny real MLX backend probe and print timing/accounting stats.

This intentionally bypasses agents, Docker, Modal, and SWE-bench. It is for the
local question: "what did one pruning call cost, and why?"

Example:
  HAY_PROFILE_MLX=1 HAY_MAX_LENGTH=2048 \
    uv run --extra backend-code-pruner-mlx python3 tools/mlx_backend_probe.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time


def _synthetic_python(functions: int) -> str:
    parts: list[str] = ["import os", "import sys", ""]
    for idx in range(functions):
        parts.extend(
            [
                f"def helper_{idx}(value):",
                f"    total = value + {idx}",
                "    for step in range(3):",
                "        total += step",
                "    return total",
                "",
            ]
        )
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--functions", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=None)
    parser.add_argument(
        "--query",
        default="Find helper functions that update totals in loops.",
    )
    parser.add_argument("--model-dir", default=None)
    args = parser.parse_args(argv)

    if args.max_length is not None:
        os.environ["HAY_MAX_LENGTH"] = str(args.max_length)
    os.environ.setdefault("HAY_PROFILE_MLX", "1")

    from pruner.backends.code_pruner.model import CodePrunerBackend

    text = _synthetic_python(args.functions)
    backend = CodePrunerBackend(model_dir=args.model_dir)

    runs = []
    for idx in range(args.repeats):
        started = time.perf_counter()
        output = backend.prune(text=text, query=args.query)
        elapsed_ms = (time.perf_counter() - started) * 1000
        runs.append(
            {
                "run": idx + 1,
                "elapsed_ms": elapsed_ms,
                "input_chars": len(text),
                "output_chars": len(output),
                "output_sha256": hashlib.sha256(output.encode()).hexdigest()[:16],
                "stats": backend.last_stats,
            }
        )

    print(json.dumps({"runs": runs}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
