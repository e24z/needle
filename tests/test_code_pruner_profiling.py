"""Padding and batching accounting for the MLX code-pruner path.

Run: PYTHONPATH=src python3 tests/test_code_pruner_profiling.py
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.backends.code_pruner.profiling import (  # noqa: E402
    current_serial_fixed_padding,
    length_bucket_batches,
    one_dynamic_batch,
    summarize_batches,
)


def test_dynamic_batch_padding_waste_examples() -> None:
    small_gap = summarize_batches(one_dynamic_batch([100, 120, 90]))
    assert small_gap.real_tokens == 310
    assert small_gap.padded_tokens == 360
    assert small_gap.pad_tokens == 50
    assert round(small_gap.padding_waste_ratio, 3) == 0.139
    assert small_gap.label == "fine"

    giant_gap = summarize_batches(one_dynamic_batch([100, 120, 2000]))
    assert giant_gap.real_tokens == 2220
    assert giant_gap.padded_tokens == 6000
    assert giant_gap.pad_tokens == 3780
    assert round(giant_gap.work_multiplier, 2) == 2.70
    assert giant_gap.label == "bad"


def test_length_bucket_separates_tiny_and_giant_chunks() -> None:
    batches = length_bucket_batches(
        [100, 120, 2000],
        max_batch_size=4,
        max_length_ratio=1.5,
    )
    assert [batch.lengths for batch in batches] == [(100, 120), (2000,)]

    bucketed = summarize_batches(batches)
    assert bucketed.real_tokens == 2220
    assert bucketed.padded_tokens == 2240
    assert round(bucketed.padding_waste_ratio, 3) == 0.009


def test_current_serial_path_models_fixed_max_length_padding() -> None:
    batches = current_serial_fixed_padding([100, 120, 90], max_length=512)
    assert [batch.lengths for batch in batches] == [(100,), (120,), (90,)]
    assert [batch.padded_length for batch in batches] == [512, 512, 512]

    current = summarize_batches(batches)
    assert current.real_tokens == 310
    assert current.padded_tokens == 1536
    assert round(current.work_multiplier, 2) == 4.95
    assert current.label == "bad"


def main() -> int:
    test_dynamic_batch_padding_waste_examples()
    test_length_bucket_separates_tiny_and_giant_chunks()
    test_current_serial_path_models_fixed_max_length_padding()
    print("test_code_pruner_profiling OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
