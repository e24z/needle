from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class BatchBudgetResult(Generic[T]):
    batches: list[list[T]]
    splits: int
    singles_over_budget: int


def estimate_padded_tokens(lengths: list[int]) -> int:
    if not lengths:
        return 0
    return max(lengths) * len(lengths)


def split_batches_by_padded_token_budget(
    batches: list[list[T]],
    *,
    max_padded_tokens: int | None,
    length_fn: Callable[[T], int],
) -> BatchBudgetResult[T]:
    """Split batches so each rectangular model call stays under a token budget."""
    if max_padded_tokens is None:
        return BatchBudgetResult(batches=batches, splits=0, singles_over_budget=0)

    result: list[list[T]] = []
    splits = 0
    singles_over_budget = 0
    for batch in batches:
        pending = list(batch)
        while pending:
            lengths = [length_fn(item) for item in pending]
            padded_tokens = estimate_padded_tokens(lengths)
            if padded_tokens <= max_padded_tokens:
                result.append(pending)
                break
            if len(pending) == 1:
                singles_over_budget += 1
                result.append(pending)
                break
            mid = max(1, len(pending) // 2)
            result.append(pending[:mid])
            pending = pending[mid:]
            splits += 1
    return BatchBudgetResult(
        batches=result,
        splits=splits,
        singles_over_budget=singles_over_budget,
    )


def unrelated_formatter(row: dict[str, object]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(row.items()))


def unrelated_retry_message(attempt: int) -> str:
    return f"retrying attempt {attempt}"


def unrelated_safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
