"""Token-window chunking helpers for the code-pruner backend.

This module stays import-light so offset behavior can be tested without MLX.
The model backend owns inference; this module only splits text into
token-bounded windows and merges per-window token scores back onto original
character offsets.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Protocol


class OffsetTokenizer(Protocol):
    def __call__(self, text: str, **kwargs): ...


@dataclass(frozen=True)
class TokenChunk:
    text: str
    start_char: int
    end_char: int
    start_token: int
    end_token: int
    token_ids: tuple[int, ...] = ()
    token_offsets: tuple[tuple[int, int], ...] = ()

    @property
    def token_count(self) -> int:
        return (
            len(self.token_ids)
            if self.token_ids
            else self.end_token - self.start_token
        )

    @property
    def relative_token_offsets(self) -> list[tuple[int, int]]:
        """Original-token offsets shifted into this chunk's text coordinate space."""
        return [
            (start - self.start_char, end - self.start_char)
            for start, end in self.token_offsets
        ]


def estimate_token_count(text: str, tokenizer: OffsetTokenizer) -> int:
    enc = tokenizer(text, add_special_tokens=False, return_attention_mask=False)
    return len(enc["input_ids"])


def split_text_into_token_chunks(
    text: str,
    tokenizer: OffsetTokenizer,
    *,
    chunk_max_tokens: int,
    overlap_tokens: int = 50,
) -> list[TokenChunk]:
    """Split text into tokenizer-bounded chunks with overlapping windows."""
    if chunk_max_tokens <= 0:
        raise ValueError("chunk_max_tokens must be positive")
    if not text:
        return []

    enc = tokenizer(
        text,
        add_special_tokens=False,
        truncation=False,
        return_attention_mask=False,
        return_offsets_mapping=True,
    )
    token_ids = enc["input_ids"]
    offsets = enc["offset_mapping"]
    total_tokens = len(token_ids)
    if total_tokens == 0:
        return [
            TokenChunk(
                text=text,
                start_char=0,
                end_char=len(text),
                start_token=0,
                end_token=0,
                token_ids=(),
                token_offsets=(),
            )
        ]
    if total_tokens <= chunk_max_tokens:
        return [
            TokenChunk(
                text=text,
                start_char=0,
                end_char=len(text),
                start_token=0,
                end_token=total_tokens,
                token_ids=tuple(int(token_id) for token_id in token_ids),
                token_offsets=tuple(
                    (int(start), int(end)) for start, end in offsets
                ),
            )
        ]

    overlap = max(0, min(overlap_tokens, chunk_max_tokens - 1))
    stride = chunk_max_tokens - overlap
    chunks: list[TokenChunk] = []
    start_token = 0
    while start_token < total_tokens:
        end_token = min(start_token + chunk_max_tokens, total_tokens)
        start_char = int(offsets[start_token][0])
        end_char = int(offsets[end_token - 1][1])
        if end_char > start_char:
            chunks.append(
                TokenChunk(
                    text=text[start_char:end_char],
                    start_char=start_char,
                    end_char=end_char,
                    start_token=start_token,
                    end_token=end_token,
                    token_ids=tuple(
                        int(token_id) for token_id in token_ids[start_token:end_token]
                    ),
                    token_offsets=tuple(
                        (int(start), int(end))
                        for start, end in offsets[start_token:end_token]
                    ),
                )
            )
        if end_token >= total_tokens:
            break
        start_token += stride
    return chunks


def bucket_token_chunks(
    chunks: list[TokenChunk],
    *,
    max_batch_size: int = 4,
    max_length_ratio: float = 1.5,
) -> list[list[TokenChunk]]:
    """Group similar-length chunks for batched inference."""
    if max_batch_size <= 0:
        raise ValueError("max_batch_size must be positive")
    if max_length_ratio < 1.0:
        raise ValueError("max_length_ratio must be >= 1.0")

    batches: list[list[TokenChunk]] = []
    bucket: list[TokenChunk] = []
    for chunk in sorted(chunks, key=lambda item: item.token_count):
        if not bucket:
            bucket = [chunk]
            continue
        would_exceed_size = len(bucket) >= max_batch_size
        shortest = max(bucket[0].token_count, 1)
        would_exceed_ratio = chunk.token_count / shortest > max_length_ratio
        if would_exceed_size or would_exceed_ratio:
            batches.append(bucket)
            bucket = [chunk]
        else:
            bucket.append(chunk)
    if bucket:
        batches.append(bucket)
    return batches


def merge_token_scores_from_chunks(
    text: str,
    chunk_results: Iterable[
        tuple[list[tuple[str, float]], list[tuple[int, int]], int]
    ],
) -> tuple[list[tuple[str, float]], list[tuple[int, int]]]:
    """Merge chunk-relative token scores into original-text offsets.

    Overlapping chunks may score the same original token span more than once.
    Scores for identical spans are averaged, matching the upstream SWE-Pruner
    strategy while keeping the output deterministic.
    """
    position_to_scores: dict[tuple[int, int], list[tuple[str, float]]] = defaultdict(list)

    for token_scores, offsets, start_char in chunk_results:
        for (token_str, score), (raw_start, raw_end) in zip(token_scores, offsets):
            abs_start = start_char + int(raw_start)
            abs_end = start_char + int(raw_end)
            if abs_end <= abs_start:
                continue
            if abs_start < 0 or abs_end > len(text):
                continue
            position_to_scores[(abs_start, abs_end)].append((token_str, float(score)))

    merged_scores: list[tuple[str, float]] = []
    merged_offsets: list[tuple[int, int]] = []
    for abs_start, abs_end in sorted(position_to_scores):
        scores = position_to_scores[(abs_start, abs_end)]
        token_str = scores[0][0]
        avg_score = sum(score for _token, score in scores) / len(scores)
        merged_scores.append((token_str, avg_score))
        merged_offsets.append((abs_start, abs_end))

    return merged_scores, merged_offsets
