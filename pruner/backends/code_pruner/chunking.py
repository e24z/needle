"""Token-window chunking helpers for the code-pruner backend.

These stay import-light so tests can exercise the offset math without importing
MLX. The model backend owns inference; this module only splits text into
token-bounded windows and merges per-window token scores back onto the original
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

    @property
    def token_count(self) -> int:
        return self.end_token - self.start_token


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
    """Split text into tokenizer-bounded chunks with overlapping token windows."""
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
    offsets = enc["offset_mapping"]
    total_tokens = len(enc["input_ids"])
    if total_tokens == 0:
        return [TokenChunk(text=text, start_char=0, end_char=len(text), start_token=0, end_token=0)]
    if total_tokens <= chunk_max_tokens:
        return [
            TokenChunk(
                text=text,
                start_char=0,
                end_char=len(text),
                start_token=0,
                end_token=total_tokens,
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
                )
            )
        if end_token >= total_tokens:
            break
        start_token += stride
    return chunks


def merge_token_scores_from_chunks(
    text: str,
    chunk_results: Iterable[
        tuple[list[tuple[str, float]], list[tuple[int, int]], int]
    ],
) -> tuple[list[tuple[str, float]], list[tuple[int, int]]]:
    """Merge chunk-relative token scores into original-text character offsets.

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
