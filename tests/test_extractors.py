"""Prove the query extractor pulls the agent's narration -- the thing that was
silently broken before. Run directly: python3 tests/test_extractors.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters" / "claude"))

from hooks.extractors import last_assistant  # noqa: E402


def _write_transcript(lines: list[dict]) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for obj in lines:
        fh.write(json.dumps(obj) + "\n")
    fh.close()
    return fh.name


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def test_picks_last_assistant_text_ignoring_thinking_and_tooluse() -> None:
    path = _write_transcript([
        _user("help me with the parser"),
        _assistant({"type": "text", "text": "First narration"}),
        _user("now read the file"),
        _assistant(
            {"type": "thinking", "thinking": "internal reasoning, must be ignored"},
            {"type": "text", "text": "Let me read the config loader"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
        ),
    ])
    q = last_assistant({"transcript_path": path})
    assert q == "Let me read the config loader", repr(q)


def test_lookback_concatenates_newest_first() -> None:
    path = _write_transcript([
        _assistant({"type": "text", "text": "older"}),
        _user("..."),
        _assistant({"type": "text", "text": "newer"}),
    ])
    q = last_assistant({"transcript_path": path}, lookback=2)
    assert q == "newer\n\nolder", repr(q)


def test_missing_transcript_yields_empty() -> None:
    assert last_assistant({"transcript_path": "/no/such/file.jsonl"}) == ""
    assert last_assistant({}) == ""


if __name__ == "__main__":
    test_picks_last_assistant_text_ignoring_thinking_and_tooluse()
    test_lookback_concatenates_newest_first()
    test_missing_transcript_yields_empty()
    print("ok: extractor picks agent narration, honors lookback, fails soft")
