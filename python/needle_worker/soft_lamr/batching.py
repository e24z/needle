"""Import-light batching guardrails for the code-pruner backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class BatchBudgetResult(Generic[T]):
    batches: list[list[T]]
    splits: int
    singles_over_budget: int


class BatchRetryFailed(RuntimeError):
    """Raised when fallback scoring cannot recover from a retryable batch error."""

    def __init__(self, original: BaseException, summary: dict[str, object]) -> None:
        super().__init__(str(original))
        self.original = original
        self.summary = summary


def padded_token_count(batch: list[T], length_fn: Callable[[T], int]) -> int:
    if not batch:
        return 0
    return len(batch) * max(length_fn(item) for item in batch)


def split_batches_by_padded_token_budget(
    batches: list[list[T]],
    *,
    max_padded_tokens: int | None,
    length_fn: Callable[[T], int],
) -> BatchBudgetResult[T]:
    """Split batches so each rectangular model call stays under a token budget.

    A single long row can exceed the budget; that is recorded but not split.
    """
    if max_padded_tokens is None:
        return BatchBudgetResult(batches=batches, splits=0, singles_over_budget=0)
    if max_padded_tokens <= 0:
        raise ValueError("max_padded_tokens must be positive")

    out: list[list[T]] = []
    splits = 0
    singles_over_budget = 0

    def append_batch(batch: list[T]) -> None:
        nonlocal singles_over_budget
        if len(batch) == 1 and padded_token_count(batch, length_fn) > max_padded_tokens:
            singles_over_budget += 1
        out.append(batch)

    for batch in batches:
        current: list[T] = []
        for item in batch:
            candidate = [*current, item]
            if current and padded_token_count(candidate, length_fn) > max_padded_tokens:
                append_batch(current)
                splits += 1
                current = [item]
            else:
                current = candidate
        if current:
            append_batch(current)
    return BatchBudgetResult(
        batches=out,
        splits=splits,
        singles_over_budget=singles_over_budget,
    )


def score_batches_with_retry(
    batches: list[list[T]],
    score_batch: Callable[[list[T]], tuple[list[R], dict[str, object]]],
    is_retryable_error: Callable[[BaseException], bool],
) -> tuple[list[R], list[dict[str, object]], dict[str, object]]:
    """Score batches, retrying retryable multi-row failures one row at a time."""
    results: list[R] = []
    stats: list[dict[str, object]] = []
    retry_count = 0
    retry_from_sizes: list[int] = []
    summary: dict[str, object] = {"batch_retry_count": 0}

    for batch in batches:
        try:
            batch_results, batch_stats = score_batch(batch)
        except Exception as exc:
            if not is_retryable_error(exc):
                raise
            if len(batch) <= 1:
                raise BatchRetryFailed(exc, summary) from exc

            retry_count += 1
            retry_from_sizes.append(len(batch))
            summary = {
                "batch_retry_count": retry_count,
                "batch_downgrade_reason": "retry_serial_after_resource_error",
                "batch_retry_from_sizes": list(retry_from_sizes),
            }
            for item in batch:
                try:
                    item_results, item_stats = score_batch([item])
                except Exception as item_exc:
                    if is_retryable_error(item_exc):
                        raise BatchRetryFailed(item_exc, summary) from item_exc
                    raise
                results.extend(item_results)
                stats.append(
                    {
                        **item_stats,
                        "batch_downgrade_reason": "retry_serial_after_resource_error",
                        "batch_retry_from_size": len(batch),
                    }
                )
            continue

        results.extend(batch_results)
        stats.append(batch_stats)

    if retry_count:
        summary = {
            "batch_retry_count": retry_count,
            "batch_downgrade_reason": "retry_serial_after_resource_error",
            "batch_retry_from_sizes": list(retry_from_sizes),
        }
    return results, stats, summary
