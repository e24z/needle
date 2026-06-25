"""Pluggable query extractors for the PostToolUse adapter.

The query is the relevance goal handed to prune(). Getting this right is the
whole ballgame: old Needle silently passed the file PATH as the query, and AST
repair masked the failure so nobody noticed for ages.

Default strategy: the agent's own pre-tool narration -- the most recent
assistant `text` block(s). Right before a tool call the agent says "let me do
X", which reads like a singular goal-hint. Strategy and lookback are swappable
(env vars) so theories can be A/B tested without code changes.

Transcript parsing is Claude-specific, so this lives in the hook adapter, not
the portable core.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Iterator


def _iter_lines_reversed(transcript_path: str) -> Iterator[dict]:
    if not transcript_path:
        return
    try:
        lines = Path(transcript_path).read_text(errors="replace").splitlines()
    except OSError:
        return
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _assistant_text(obj: dict) -> str | None:
    """The visible narration of an assistant turn: `text` blocks only.
    Skips `thinking` (internal) and `tool_use` (not speech)."""
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message") or {}
    if msg.get("role") != "assistant":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip() or None
    return None


def last_assistant(payload: dict, lookback: int = 1) -> str:
    """Up to `lookback` most recent assistant narrations, newest first."""
    found: list[str] = []
    for obj in _iter_lines_reversed(payload.get("transcript_path", "")):
        text = _assistant_text(obj)
        if text:
            found.append(text)
            if len(found) >= max(lookback, 1):
                break
    return "\n\n".join(found)


REGISTRY: dict[str, Callable[..., str]] = {
    "last_assistant": last_assistant,
}


def extract_query(payload: dict) -> str:
    """Resolve the configured strategy and run it. Never raises: a bad strategy
    or unreadable transcript yields an empty query, not a crash."""
    name = os.environ.get("HAY_QUERY_STRATEGY", "last_assistant")
    try:
        lookback = int(os.environ.get("HAY_QUERY_LOOKBACK", "1"))
    except ValueError:
        lookback = 1
    fn = REGISTRY.get(name, last_assistant)
    try:
        return fn(payload, lookback=lookback)
    except Exception:
        return ""
