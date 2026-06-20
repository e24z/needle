"""Line-level pruning helpers for model token scores."""

from __future__ import annotations

import os
from bisect import bisect_right
from typing import Iterable


def _silent_prune() -> bool:
    """When set, drop filtered spans entirely instead of leaving a
    ``[pruned N lines]`` breadcrumb. Experiment toggle (default off): the
    visible marker makes the agent re-read, taxing end-to-end savings."""
    return os.environ.get("HAY_SILENT_PRUNE", "").strip().lower() in {"1", "true", "yes"}


def aggregate_token_scores_to_lines(
    code: str,
    token_scores: Iterable[tuple[str, float]],
    token_offsets: Iterable[tuple[int, int]],
) -> dict[int, float]:
    """Average token relevance scores onto 1-indexed source lines."""
    line_ranges: list[tuple[int, int]] = []
    pos = 0
    for line in code.splitlines(keepends=True):
        content_end = pos + len(line.rstrip("\r\n"))
        line_ranges.append((pos, content_end))
        pos += len(line)

    starts = [start for start, _end in line_ranges]
    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    code_len = len(code)

    for (_token, raw_score), (raw_start, raw_end) in zip(token_scores, token_offsets):
        start = max(0, min(int(raw_start), code_len))
        end = max(0, min(int(raw_end), code_len))
        if end <= start:
            continue

        score = max(0.0, min(1.0, float(raw_score)))
        line_index = max(0, bisect_right(starts, start) - 1)
        while line_index < len(line_ranges):
            line_start, line_end = line_ranges[line_index]
            if line_start >= end:
                break
            overlap_start = max(start, line_start)
            overlap_end = min(end, line_end)
            overlap = max(0, overlap_end - overlap_start)
            if overlap:
                line_num = line_index + 1
                totals[line_num] = totals.get(line_num, 0.0) + score * overlap
                counts[line_num] = counts.get(line_num, 0) + overlap
            line_index += 1

    return {line_num: totals[line_num] / counts[line_num] for line_num in totals}


def prune_code_lines(
    code: str,
    line_scores: dict[int, float],
    threshold: float,
    always_keep_first_frags: bool = False,
) -> tuple[str, list[int]]:
    """Return pruned code and the 1-indexed line numbers kept."""
    lines = code.splitlines()
    forced_prefix_lines = 1 if always_keep_first_frags else 0
    kept_lines = [
        line_num
        for line_num in range(1, len(lines) + 1)
        if line_num <= forced_prefix_lines
        or line_scores.get(line_num, 0.0) >= threshold
    ]

    extra_kept = [
        right - 1
        for left, right in zip(kept_lines, kept_lines[1:])
        if right - left == 2
    ]
    kept_lines = sorted({*kept_lines, *extra_kept})
    kept_set = set(kept_lines)

    pruned_lines: list[str] = []
    filtered_buffer: list[str] = []
    filtered_count = 0
    filtered_chars = 0
    placeholder = "[pruned {} lines]"
    silent = _silent_prune()

    def flush_filtered() -> None:
        nonlocal filtered_count, filtered_chars, filtered_buffer
        if filtered_count <= 0:
            return
        if silent:
            pass  # drop the span silently; no breadcrumb for the agent to react to
        elif filtered_chars >= len(placeholder.format(filtered_count)):
            pruned_lines.append(placeholder.format(filtered_count))
        else:
            pruned_lines.extend(filtered_buffer)
        filtered_buffer = []
        filtered_count = 0
        filtered_chars = 0

    for line_num, line in enumerate(lines, start=1):
        if not line.strip():
            filtered_count += 1
            filtered_buffer.append(line)
            continue
        if line_num not in kept_set:
            filtered_count += 1
            filtered_buffer.append(line)
            filtered_chars += len(line)
            continue
        flush_filtered()
        pruned_lines.append(line)

    if filtered_count and not silent:
        pruned_lines.append(placeholder.format(filtered_count))

    return "\n".join(pruned_lines), kept_lines
