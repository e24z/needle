"""Pure offset tests for code-pruner chunk splitting/merging.

Run: PYTHONPATH=src python3 tests/test_code_pruner_chunking.py
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.backends.code_pruner.chunking import (  # noqa: E402
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


class BoundarySensitiveTokenizer:
    """Tokenizer fixture that changes behavior when substrings are re-tokenized."""

    def __call__(self, text: str, **_kwargs):
        if text == "abcdefgh":
            return {
                "input_ids": [101, 102, 103, 104],
                "offset_mapping": [(0, 2), (2, 4), (4, 6), (6, 8)],
            }
        return {
            "input_ids": [200 + idx for idx, _char in enumerate(text)],
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


def test_split_preserves_original_token_ids_and_offsets() -> None:
    tokenizer = BoundarySensitiveTokenizer()
    chunks = split_text_into_token_chunks(
        "abcdefgh",
        tokenizer,
        chunk_max_tokens=2,
        overlap_tokens=0,
    )

    assert [(c.text, c.start_char, c.end_char) for c in chunks] == [
        ("abcd", 0, 4),
        ("efgh", 4, 8),
    ]
    assert [c.token_ids for c in chunks] == [(101, 102), (103, 104)]
    assert [c.token_offsets for c in chunks] == [
        ((0, 2), (2, 4)),
        ((4, 6), (6, 8)),
    ]
    assert [c.relative_token_offsets for c in chunks] == [
        [(0, 2), (2, 4)],
        [(0, 2), (2, 4)],
    ]
    assert tokenizer(chunks[0].text)["input_ids"] != list(chunks[0].token_ids)


def test_original_offsets_survive_split_score_stub_and_merge() -> None:
    chunks = split_text_into_token_chunks(
        "abcdefgh",
        BoundarySensitiveTokenizer(),
        chunk_max_tokens=2,
        overlap_tokens=1,
    )

    scores, offsets = merge_token_scores_from_chunks(
        "abcdefgh",
        [
            (
                [("", 0.5) for _offset in chunk.relative_token_offsets],
                chunk.relative_token_offsets,
                chunk.start_char,
            )
            for chunk in chunks
        ],
    )

    assert offsets == [(0, 2), (2, 4), (4, 6), (6, 8)]
    assert scores == [("", 0.5), ("", 0.5), ("", 0.5), ("", 0.5)]


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


def test_merge_keeps_partial_nested_adjacent_and_whitespace_spans_distinct() -> None:
    scores, offsets = merge_token_scores_from_chunks(
        "a bc",
        [
            (
                [
                    ("a", 0.1),
                    ("a b", 0.2),
                    (" ", 0.3),
                    (" b", 0.4),
                    (" bc", 0.5),
                    ("bc", 0.6),
                ],
                [(0, 1), (0, 3), (1, 2), (1, 3), (1, 4), (2, 4)],
                0,
            )
        ],
    )

    assert offsets == [(0, 1), (0, 3), (1, 2), (1, 3), (1, 4), (2, 4)]
    assert scores == [
        ("a", 0.1),
        ("a b", 0.2),
        (" ", 0.3),
        (" b", 0.4),
        (" bc", 0.5),
        ("bc", 0.6),
    ]


def main() -> int:
    test_split_text_into_overlapping_token_chunks()
    test_split_preserves_original_token_ids_and_offsets()
    test_original_offsets_survive_split_score_stub_and_merge()
    test_bucket_token_chunks_groups_similar_lengths()
    test_merge_token_scores_averages_overlapping_offsets()
    test_merge_keeps_partial_nested_adjacent_and_whitespace_spans_distinct()
    print("test_code_pruner_chunking OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
