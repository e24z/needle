"""Small, dependency-free profiling helpers for the code-pruner backend.

These helpers model the shape/accounting questions around batching without
loading MLX. They are intentionally boring: they let us test padding waste and
batch grouping before touching the expensive model path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaddingProfile:
    real_tokens: int
    padded_tokens: int

    @property
    def pad_tokens(self) -> int:
        return self.padded_tokens - self.real_tokens

    @property
    def padding_waste_ratio(self) -> float:
        if self.padded_tokens <= 0:
            return 0.0
        return self.pad_tokens / self.padded_tokens

    @property
    def work_multiplier(self) -> float:
        if self.real_tokens <= 0:
            return 0.0
        return self.padded_tokens / self.real_tokens

    @property
    def label(self) -> str:
        return padding_waste_label(self.padding_waste_ratio)


@dataclass(frozen=True)
class BatchProfile:
    lengths: tuple[int, ...]
    padded_length: int

    @property
    def batch_size(self) -> int:
        return len(self.lengths)

    @property
    def real_tokens(self) -> int:
        return sum(self.lengths)

    @property
    def padded_tokens(self) -> int:
        return self.batch_size * self.padded_length

    @property
    def padding(self) -> PaddingProfile:
        return PaddingProfile(
            real_tokens=self.real_tokens,
            padded_tokens=self.padded_tokens,
        )


def padding_waste_label(waste_ratio: float) -> str:
    """Human label for a padding waste ratio.

    These are starting heuristics, not universal laws. A real backend decision
    still needs timing, memory, and output-quality measurements.
    """
    if waste_ratio <= 0.20:
        return "fine"
    if waste_ratio <= 0.40:
        return "measure"
    return "bad"


def current_serial_fixed_padding(
    lengths: list[int],
    *,
    max_length: int,
) -> list[BatchProfile]:
    """Model today's shape: one chunk per model call, padded to max_length."""
    _validate_lengths(lengths)
    _validate_positive(max_length, "max_length")
    return [
        BatchProfile(lengths=(min(length, max_length),), padded_length=max_length)
        for length in lengths
    ]


def one_dynamic_batch(lengths: list[int]) -> list[BatchProfile]:
    """Model one rectangular batch padded only to the longest row."""
    _validate_lengths(lengths)
    if not lengths:
        return []
    return [BatchProfile(lengths=tuple(lengths), padded_length=max(lengths))]


def length_bucket_batches(
    lengths: list[int],
    *,
    max_batch_size: int = 4,
    max_length_ratio: float = 1.5,
) -> list[BatchProfile]:
    """Sort lengths and batch together rows that have similar lengths.

    A new bucket starts when either:
    - the bucket reaches max_batch_size, or
    - adding the next row would make longest / shortest exceed max_length_ratio.
    """
    _validate_lengths(lengths)
    _validate_positive(max_batch_size, "max_batch_size")
    if max_length_ratio < 1.0:
        raise ValueError("max_length_ratio must be >= 1.0")

    batches: list[BatchProfile] = []
    bucket: list[int] = []
    for length in sorted(lengths):
        if not bucket:
            bucket = [length]
            continue
        would_exceed_size = len(bucket) >= max_batch_size
        would_exceed_ratio = length / max(bucket[0], 1) > max_length_ratio
        if would_exceed_size or would_exceed_ratio:
            batches.append(BatchProfile(tuple(bucket), max(bucket)))
            bucket = [length]
        else:
            bucket.append(length)
    if bucket:
        batches.append(BatchProfile(tuple(bucket), max(bucket)))
    return batches


def summarize_batches(batches: list[BatchProfile]) -> PaddingProfile:
    return PaddingProfile(
        real_tokens=sum(batch.real_tokens for batch in batches),
        padded_tokens=sum(batch.padded_tokens for batch in batches),
    )


def _validate_lengths(lengths: list[int]) -> None:
    for length in lengths:
        _validate_positive(length, "length")


def _validate_positive(value: int, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
