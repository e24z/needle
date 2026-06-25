#!/usr/bin/env python3
"""Explain padding waste for current vs batched code-pruner shapes.

This script is intentionally MLX-free. It answers the shape/accounting question
before the expensive model enters the room.

Example:
  PYTHONPATH=. python3 tools/mlx_padding_probe.py --lengths 100,120,2000
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable

from pruner.backends.code_pruner.profiling import (
    BatchProfile,
    current_serial_fixed_padding,
    length_bucket_batches,
    one_dynamic_batch,
    summarize_batches,
)


def _parse_lengths(raw: str) -> list[int]:
    lengths = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not lengths:
        raise argparse.ArgumentTypeError("provide at least one length")
    return lengths


def _format_pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def _print_batches(title: str, batches: Iterable[BatchProfile]) -> None:
    batch_list = list(batches)
    total = summarize_batches(batch_list)
    print(title)
    print("  batch  rows  padded_len  real_tokens  sent_tokens  waste   work")
    for idx, batch in enumerate(batch_list, start=1):
        padding = batch.padding
        print(
            f"  {idx:>5}  {batch.batch_size:>4}  {batch.padded_length:>10}  "
            f"{batch.real_tokens:>11}  {batch.padded_tokens:>11}  "
            f"{_format_pct(padding.padding_waste_ratio):>6}  "
            f"{padding.work_multiplier:>4.2f}x"
        )
    print(
        f"  total  {sum(batch.batch_size for batch in batch_list):>4}  {'-':>10}  "
        f"{total.real_tokens:>11}  {total.padded_tokens:>11}  "
        f"{_format_pct(total.padding_waste_ratio):>6}  "
        f"{total.work_multiplier:>4.2f}x  {total.label}"
    )
    print("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lengths",
        type=_parse_lengths,
        default=_parse_lengths("100,120,2000"),
        help="Comma-separated real token lengths. Default: 100,120,2000.",
    )
    parser.add_argument(
        "--max-length",
        type=int,
        default=2048,
        help="Old single-chunk fixed padding length.",
    )
    parser.add_argument(
        "--max-batch-size",
        type=int,
        default=2,
        help="Maximum rows in a bucketed batch.",
    )
    parser.add_argument(
        "--max-length-ratio",
        type=float,
        default=1.5,
        help="Start a new bucket when longest / shortest would exceed this.",
    )
    args = parser.parse_args(argv)

    lengths = args.lengths
    print(f"lengths: {lengths}")
    print("")
    _print_batches(
        f"old serial path: one row per call, padded to {args.max_length}",
        current_serial_fixed_padding(lengths, max_length=args.max_length),
    )
    _print_batches(
        "naive dynamic batch: all rows together, padded to longest row",
        one_dynamic_batch(lengths),
    )
    _print_batches(
        (
            "bucketed dynamic batches: sorted rows, "
            f"max_batch_size={args.max_batch_size}, "
            f"max_length_ratio={args.max_length_ratio:g}"
        ),
        length_bucket_batches(
            lengths,
            max_batch_size=args.max_batch_size,
            max_length_ratio=args.max_length_ratio,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
