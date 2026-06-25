#!/usr/bin/env python3
"""Run a tiny real MLX backend probe and print timing/accounting stats.

This intentionally bypasses agents, Docker, Modal, and SWE-bench. It is for the
local question: "what did one pruning call cost, and why?"

Example:
  HAY_PROFILE_MLX=1 NEEDLE_MLX_PROFILE=local_adaptive \
    uv run --extra backend-code-pruner-mlx python3 tools/mlx_backend_probe.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time


def _parse_int_list(raw: str) -> list[int]:
    values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("provide at least one integer")
    return values


def _compact_stats(stats: dict[str, object]) -> dict[str, object]:
    fields = [
        "chunks",
        "batches",
        "batch_sizes",
        "batched",
        "max_length",
        "max_length_profile",
        "max_batch_size",
        "original_code_tokens",
        "scored_code_tokens",
        "truncated_code_tokens",
        "real_tokens",
        "padded_tokens",
        "padding_waste_ratio",
        "retained_hidden_states",
        "available_hidden_states",
        "forward_eval_ms",
        "host_sync_ms",
        "graph_build_ms",
        "total_ms",
        "mlx_active_mb_end",
        "mlx_cache_mb_end",
        "mlx_peak_mb_max",
    ]
    return {field: stats.get(field) for field in fields if field in stats}


def _rss_mb() -> float | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip()
        return int(out) / 1024 if out else None
    except Exception:
        return None


def _system_memory() -> dict[str, int] | None:
    try:
        from needle.runtime import sysmem

        pressure, available_mb = sysmem.memstat()
        return {"pressure": pressure, "available_mb": available_mb}
    except Exception:
        return None


def _mlx_memory() -> dict[str, float]:
    try:
        import mlx.core as mx
    except Exception:
        return {}

    stats: dict[str, float] = {}
    for key, name in (
        ("active_mb", "get_active_memory"),
        ("cache_mb", "get_cache_memory"),
        ("peak_mb", "get_peak_memory"),
    ):
        fn = getattr(mx, name, None)
        if fn is None:
            metal = getattr(mx, "metal", None)
            fn = getattr(metal, name, None) if metal is not None else None
        if fn is None:
            continue
        try:
            stats[key] = float(fn()) / (1024 * 1024)
        except Exception:
            continue
    return stats


def _snapshot() -> dict[str, object]:
    return {
        "rss_mb": _rss_mb(),
        "system_memory": _system_memory(),
        "mlx_memory": _mlx_memory(),
    }


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
    parser.add_argument(
        "--functions-list",
        type=_parse_int_list,
        default=None,
        help="Comma-separated function counts to sweep in one loaded process.",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=None)
    parser.add_argument(
        "--max-lengths",
        type=_parse_int_list,
        default=None,
        help="Comma-separated max lengths to sweep in one loaded process.",
    )
    parser.add_argument(
        "--batch-sizes",
        type=_parse_int_list,
        default=None,
        help="Comma-separated HAY_MLX_MAX_BATCH_SIZE values to sweep.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print only the probe fields that matter for perf triage.",
    )
    parser.add_argument(
        "--query",
        default="Find helper functions that update totals in loops.",
    )
    parser.add_argument("--model-dir", default=None)
    args = parser.parse_args(argv)

    if args.max_length is not None:
        os.environ["HAY_MAX_LENGTH"] = str(args.max_length)
    os.environ.setdefault("HAY_PROFILE_MLX", "1")
    functions_values = args.functions_list or [args.functions]
    if args.max_lengths is not None:
        max_lengths = args.max_lengths
    elif args.max_length is not None:
        max_lengths = [args.max_length]
    else:
        max_lengths = [None]
    batch_sizes = args.batch_sizes or [
        int(
            os.environ.get("NEEDLE_MLX_MAX_BATCH_SIZE")
            or os.environ.get("HAY_MLX_MAX_BATCH_SIZE", "1")
        )
    ]

    from pruner.backends.code_pruner.model import CodePrunerBackend

    before_load = _snapshot()
    load_started = time.perf_counter()
    backend = CodePrunerBackend(model_dir=args.model_dir)
    after_load = _snapshot()
    load_ms = (time.perf_counter() - load_started) * 1000

    runs = []
    for max_length in max_lengths:
        if max_length is not None:
            backend._max_length = max_length
        for batch_size in batch_sizes:
            os.environ["HAY_MLX_MAX_BATCH_SIZE"] = str(batch_size)
            for functions in functions_values:
                text = _synthetic_python(functions)
                for idx in range(args.repeats):
                    before_run = _snapshot()
                    started = time.perf_counter()
                    output = backend.prune(text=text, query=args.query)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    after_run = _snapshot()
                    stats = backend.last_stats
                    runs.append(
                        {
                            "run": idx + 1,
                            "functions": functions,
                            "max_length": max_length if max_length is not None else "adaptive",
                            "batch_size": batch_size,
                            "elapsed_ms": elapsed_ms,
                            "input_chars": len(text),
                            "output_chars": len(output),
                            "output_sha256": hashlib.sha256(
                                output.encode()
                            ).hexdigest()[:16],
                            "before_run": before_run if not args.compact else None,
                            "after_run": after_run,
                            "stats": _compact_stats(stats)
                            if args.compact
                            else stats,
                        }
                    )

    print(
        json.dumps(
            {
                "load_ms": load_ms,
                "before_load": before_load,
                "after_load": after_load,
                "runs": runs,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
