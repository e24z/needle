"""Optional real-MLX probe for code-pruner chunking and batching.

This script is intentionally outside the normal test suite. It loads the real
MLX backend when available, runs one synthetic or file-backed prune, and can
compare serial chunk scoring (`NEEDLE_MLX_MAX_BATCH_SIZE=1`) with a batched run.

Example:

    NEEDLE_PROFILE_MLX=1 uv run --extra backend-code-pruner-mlx \
      python3 tests/probes/mlx_backend_probe.py --functions 40 --max-length 1024 --batch-size 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

STAT_KEYS = (
    "chunks",
    "batches",
    "batch_sizes",
    "max_batch_size",
    "max_batch_tokens",
    "max_length",
    "max_length_profile",
    "batch_guardrail_splits",
    "batch_guardrail_singles_over_budget",
    "batch_retry_count",
    "batch_downgrade_reason",
    "original_code_tokens",
    "scored_code_tokens",
    "real_tokens",
    "padded_tokens",
    "pad_tokens",
    "padding_waste_ratio",
    "truncated_code_tokens",
    "forward_eval_ms",
    "host_sync_ms",
    "batch_total_ms",
    "total_ms",
    "saved_chars",
    "chunked",
    "batched",
)


def _fixture(functions: int) -> str:
    parts = [
        "import json",
        "",
        "DEFAULTS = {'timeout': 30, 'retries': 2}",
        "",
        "def load_config(path):",
        "    raw = json.loads(open(path).read())",
        "    merged = {**DEFAULTS, **raw}",
        "    validate_required_keys(merged)",
        "    return merged",
        "",
        "def validate_required_keys(config):",
        "    missing = [key for key in ('api_key', 'endpoint') if not config.get(key)]",
        "    if missing:",
        "        raise ValueError('missing config keys: ' + ', '.join(missing))",
        "",
    ]
    for idx in range(functions):
        parts.extend(
            [
                f"def unrelated_helper_{idx}(payload):",
                f"    value = payload.get('value_{idx}', {idx})",
                "    return {'value': value, 'ok': True}",
                "",
            ]
        )
    return "\n".join(parts)


def _selected_stats(stats: dict[str, object]) -> dict[str, object]:
    return {key: stats[key] for key in STAT_KEYS if key in stats}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _run_once(backend: Any, *, text: str, query: str, batch_size: int) -> dict[str, object]:
    os.environ["NEEDLE_MLX_MAX_BATCH_SIZE"] = str(batch_size)
    started = time.perf_counter()
    output = backend.prune(text=text, query=query)
    elapsed_ms = (time.perf_counter() - started) * 1000
    stats = getattr(backend, "last_stats", {})
    if not isinstance(stats, dict):
        stats = {}
    return {
        "batch_size": batch_size,
        "input_chars": len(text),
        "output_chars": len(output),
        "saved_chars": max(0, len(text) - len(output)),
        "elapsed_ms": elapsed_ms,
        "output_sha256": _sha(output),
        "stats": _selected_stats(stats),
    }


def _run_probe(args: argparse.Namespace) -> dict[str, object]:
    try:
        from needle.backends.code_pruner.model import CodePrunerBackend
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "skipped": True, "reason": f"backend unavailable: {exc}"}

    if args.profile_mlx:
        os.environ["NEEDLE_PROFILE_MLX"] = "1"
    if args.max_length:
        os.environ["NEEDLE_MLX_MAX_LENGTH"] = str(args.max_length)
    if args.max_batch_tokens:
        os.environ["NEEDLE_MLX_MAX_BATCH_TOKENS"] = str(args.max_batch_tokens)
    if args.no_repair:
        os.environ["NEEDLE_REPAIR"] = "0"

    text = Path(args.file).read_text(encoding="utf-8") if args.file else _fixture(args.functions)
    backend = CodePrunerBackend(model_dir=args.model_dir or None)
    batched = _run_once(
        backend,
        text=text,
        query=args.query,
        batch_size=args.batch_size,
    )
    result: dict[str, object] = {
        "ok": True,
        "fixture": args.file or f"synthetic:{args.functions}",
        "query": args.query,
        "batched": batched,
    }
    if args.compare_serial:
        serial = _run_once(backend, text=text, query=args.query, batch_size=1)
        result["serial"] = serial
        result["comparison"] = {
            "same_output": serial["output_sha256"] == batched["output_sha256"],
            "serial_output_sha256": serial["output_sha256"],
            "batched_output_sha256": batched["output_sha256"],
            "serial_saved_chars": serial["saved_chars"],
            "batched_saved_chars": batched["saved_chars"],
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optional real-MLX Needle backend probe.")
    parser.add_argument("--file", default="", help="Real file to prune instead of a synthetic fixture.")
    parser.add_argument("--functions", type=int, default=40, help="Synthetic unrelated helper count.")
    parser.add_argument("--query", default="Where does load_config validate required keys?")
    parser.add_argument("--model-dir", default="", help="Existing local code-pruner model directory.")
    parser.add_argument("--max-length", type=int, default=0, help="Set NEEDLE_MLX_MAX_LENGTH.")
    parser.add_argument("--batch-size", type=int, default=2, help="Batched run max batch size.")
    parser.add_argument("--max-batch-tokens", type=int, default=0, help="Set padded-token budget.")
    parser.add_argument("--compare-serial", action="store_true", help="Also run batch_size=1.")
    parser.add_argument("--no-profile-mlx", dest="profile_mlx", action="store_false")
    parser.add_argument("--repair", dest="no_repair", action="store_false", help="Allow AST repair.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero on unavailable MLX/model.")
    parser.set_defaults(profile_mlx=True, no_repair=True)
    args = parser.parse_args(argv)

    try:
        result = _run_probe(args)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "skipped": True, "reason": str(exc)}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if args.strict else 0

    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("ok"):
        return 0
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
