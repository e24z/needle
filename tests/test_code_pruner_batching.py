"""Batch guardrail helpers for the code-pruner backend.

Run: PYTHONPATH=python python3 tests/test_code_pruner_batching.py
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from needle_worker.soft_lamr.batching import (  # noqa: E402
    BatchRetryFailed,
    score_batches_with_retry,
    split_batches_by_padded_token_budget,
)
from needle_worker.soft_lamr.config import configured_max_batch_tokens  # noqa: E402


def test_split_batches_by_padded_token_budget_keeps_calls_bounded() -> None:
    result = split_batches_by_padded_token_budget(
        [[100, 120, 140], [500, 700]],
        max_padded_tokens=300,
        length_fn=lambda item: item,
    )

    assert result.batches == [[100, 120], [140], [500], [700]]
    assert result.splits == 2
    assert result.singles_over_budget == 2


def test_score_batches_retries_retryable_batch_serially() -> None:
    calls: list[list[int]] = []

    def score(batch: list[int]):
        calls.append(batch)
        if len(batch) > 1:
            raise RuntimeError("metal resource exhausted")
        return [batch[0] * 10], {"batch_size": len(batch)}

    results, stats, summary = score_batches_with_retry(
        [[1, 2], [3]],
        score,
        lambda exc: "resource exhausted" in str(exc),
    )

    assert results == [10, 20, 30]
    assert calls == [[1, 2], [1], [2], [3]]
    assert summary["batch_retry_count"] == 1
    assert summary["batch_retry_from_sizes"] == [2]
    assert stats[0]["batch_downgrade_reason"] == "retry_serial_after_resource_error"
    assert stats[1]["batch_retry_from_size"] == 2


def test_score_batches_raises_when_serial_retry_still_fails() -> None:
    def score(batch: list[int]):
        raise RuntimeError("out of memory")

    try:
        score_batches_with_retry([[1, 2]], score, lambda exc: "memory" in str(exc))
    except BatchRetryFailed as exc:
        assert exc.summary["batch_retry_count"] == 1
        assert exc.summary["batch_retry_from_sizes"] == [2]
    else:
        raise AssertionError("serial retry failure should surface as BatchRetryFailed")


def test_configured_max_batch_tokens_reads_needle_env() -> None:
    assert configured_max_batch_tokens({"NEEDLE_MLX_MAX_BATCH_TOKENS": "20"}) == 20
    assert configured_max_batch_tokens({}) is None


def main() -> int:
    test_split_batches_by_padded_token_budget_keeps_calls_bounded()
    test_score_batches_retries_retryable_batch_serially()
    test_score_batches_raises_when_serial_retry_still_fails()
    test_configured_max_batch_tokens_reads_needle_env()
    print("test_code_pruner_batching OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
