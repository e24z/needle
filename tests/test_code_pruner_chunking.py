"""Pure offset tests for code-pruner chunk splitting/merging.

Run: PYTHONPATH=. python3 tests/test_code_pruner_chunking.py
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pruner.backends.code_pruner.chunking import (  # noqa: E402
    TokenChunk,
    bucket_token_chunks,
    merge_token_scores_from_chunks,
    split_text_into_token_chunks,
)


class CharTokenizer:
    def __call__(self, text: str, **_kwargs):
        return {
            "input_ids": list(range(len(text))),
            "offset_mapping": [(idx, idx + 1) for idx in range(len(text))],
        }


def test_split_text_into_overlapping_token_chunks() -> None:
    chunks = split_text_into_token_chunks(
        "abcdefghij",
        CharTokenizer(),
        chunk_max_tokens=4,
        overlap_tokens=1,
    )

    assert [(c.text, c.start_char, c.end_char) for c in chunks] == [
        ("abcd", 0, 4),
        ("defg", 3, 7),
        ("ghij", 6, 10),
    ]
    assert [(c.start_token, c.end_token) for c in chunks] == [
        (0, 4),
        (3, 7),
        (6, 10),
    ]


def test_bucket_token_chunks_groups_similar_lengths() -> None:
    chunks = [
        TokenChunk("a" * 100, 0, 100, 0, 100),
        TokenChunk("b" * 120, 100, 220, 100, 220),
        TokenChunk("c" * 2000, 220, 2220, 220, 2220),
    ]

    batches = bucket_token_chunks(chunks, max_batch_size=4, max_length_ratio=1.5)

    assert [[chunk.token_count for chunk in batch] for batch in batches] == [
        [100, 120],
        [2000],
    ]


def test_merge_token_scores_averages_overlapping_offsets() -> None:
    scores, offsets = merge_token_scores_from_chunks(
        "abc",
        [
            ([("a", 0.2), ("b", 0.4)], [(0, 1), (1, 2)], 0),
            ([("b", 0.8), ("c", 1.0)], [(0, 1), (1, 2)], 1),
        ],
    )

    assert offsets == [(0, 1), (1, 2), (2, 3)]
    assert scores == [("a", 0.2), ("b", 0.6000000000000001), ("c", 1.0)]


def main() -> int:
    test_split_text_into_overlapping_token_chunks()
    test_bucket_token_chunks_groups_similar_lengths()
    test_merge_token_scores_averages_overlapping_offsets()
    print("test_code_pruner_chunking OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
