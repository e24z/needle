"""Per-session savings, written by the hook and read by the statusline. Plain
and portable, so it lives in the core. Char-based for now; the statusline can
approximate tokens (~chars/4) at display time.

Keyed by Claude's session_id so the count reflects *this* session, not a
lifetime running total. Both the hook and the statusline see the same
session_id in their payloads, so they agree. HAY_STATE overrides everything
(tests, manual runs)."""

# TODO: this file is claude-specific. worth thinking about both naming and position in the boundary.

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import naming

_EMPTY = {"calls": 0, "original_chars": 0, "pruned_chars": 0, "saved_chars": 0}


def state_path(session_id: str | None = None) -> Path:
    env = os.environ.get("HAY_STATE")
    if env:
        return Path(env).expanduser()
    if session_id:
        return naming.app_home() / "sessions" / f"{session_id}.json"
    return naming.app_home() / "state.json"


def read(session_id: str | None = None) -> dict:
    try:
        return json.loads(state_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return dict(_EMPTY)


def record(
    *,
    original_len: int,
    pruned_len: int,
    tool: str = "",
    query: str = "",
    session_id: str = "",
) -> dict:
    s = read(session_id)
    s["calls"] = s.get("calls", 0) + 1
    s["original_chars"] = s.get("original_chars", 0) + original_len
    s["pruned_chars"] = s.get("pruned_chars", 0) + pruned_len
    s["saved_chars"] = s["original_chars"] - s["pruned_chars"]
    s["updated_at"] = time.time()
    p = state_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(p)  # atomic
    return s
