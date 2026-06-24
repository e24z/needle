#!/usr/bin/env python3
"""PostToolUse adapter: prune large read-like tool outputs.

A thin shim. Reads the hook payload, extracts a query (see extractors), asks the
manager (via client) to prune the tool result, and returns `updatedToolOutput`
when the savings clear a threshold.

Fails OPEN by design: any error -- malformed payload, manager down, unknown
output shape -- results in the original output passing through unchanged. A
pruner that's off must never break the agent.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Standalone script under adapters/claude/hooks/. Put the repo root on the path
# (for the engine package `pruner`) and the adapter root (for `hooks.extractors`
# and the adapter-local `state`).
_HERE = Path(__file__).resolve()
sys.path[0:0] = [str(_HERE.parents[3]), str(_HERE.parents[1])]  # repo root, adapters/claude

import state  # noqa: E402
from hooks.extractors import extract_query  # noqa: E402
from pruner import client, naming  # noqa: E402


def _breadcrumb(note: str) -> None:
    """Opt-in trace of hook invocations (separate from prune decisions). Off
    unless HAY_DEBUG is set. Invaluable when a hook silently no-ops."""
    if not os.environ.get("HAY_DEBUG"):
        return
    try:
        import time

        home = naming.app_home()
        home.mkdir(parents=True, exist_ok=True)
        with open(home / "hook-debug.log", "a") as fh:
            fh.write(f"{time.time():.0f} {note}\n")
    except Exception:
        pass

TARGET_TOOLS = {"Read", "Grep", "Glob"}
MIN_CHARS = int(os.environ.get("HAY_MIN_CHARS", "200"))
MIN_RATIO = float(os.environ.get("HAY_MIN_SAVINGS_RATIO", "0.10"))


# Claude Code puts the tool result under `tool_response` (verified from a real
# PostToolUse payload). Older docs say `tool_output`; accept both to be safe.
RESULT_KEYS = ("tool_response", "tool_output", "tool_result")


def _result_obj(payload: dict) -> object:
    for key in RESULT_KEYS:
        if key in payload:
            return payload[key]
    return None


def _as_text(result: object) -> str | None:
    """Pull the prunable text body out of a tool result.

    Read's real shape is {"type": "...", "file": {"filePath", "content", ...}};
    a re-read is {"type": "file_unchanged", "file": {"filePath"}} with no content
    (nothing to prune). Bash/Grep tend to be a string or carry stdout/content."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        file = result.get("file")
        if isinstance(file, dict) and isinstance(file.get("content"), str):
            return file["content"]
        for key in ("content", "text", "output", "stdout"):
            val = result.get(key)
            if isinstance(val, str):
                return val
        blocks = result.get("content")
        if isinstance(blocks, list):
            parts = [b.get("text", "") for b in blocks
                     if isinstance(b, dict) and isinstance(b.get("text"), str)]
            if parts:
                return "\n".join(parts)
    if isinstance(result, list):
        parts = [b.get("text", "") for b in result
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return None


def _with_text(result: object, new_text: str) -> object:
    """Return a copy of `result` with its text body swapped for `new_text`,
    preserving the original shape. `updatedToolOutput` replaces `tool_response`,
    so it must mirror that structure (e.g. a Read keeps {type, file:{...}} and
    only file.content changes) rather than collapsing to a bare string."""
    if isinstance(result, str):
        return new_text
    if isinstance(result, dict):
        out = dict(result)
        file = result.get("file")
        if isinstance(file, dict) and isinstance(file.get("content"), str):
            out["file"] = {**file, "content": new_text}
            return out
        for key in ("content", "text", "output", "stdout"):
            if isinstance(result.get(key), str):
                out[key] = new_text
                return out
    return new_text  # unknown shape: best-effort bare string


def build_response(payload: dict, prune_fn) -> dict | None:
    """Decide whether/how to rewrite the tool output. Pure given `prune_fn`
    (text, query) -> response dict, so it is testable without a live socket.
    Returns the hook output dict, or None to pass the original through."""
    if payload.get("tool_name") not in TARGET_TOOLS:
        return None
    result = _result_obj(payload)
    original = _as_text(result)
    if not original or len(original) < MIN_CHARS:
        return None

    query = extract_query(payload)
    resp = prune_fn(original, query)
    if not resp.get("ok"):
        return None

    pruned = resp["text"]
    saved = len(original) - len(pruned)
    if saved <= 0 or saved / len(original) < MIN_RATIO:
        return None  # not worth it: original passes through (and isn't counted)

    # Record only prunes the agent actually receives, so "N prunes" and the
    # tokens-saved total reflect applied rewrites rather than every attempt.
    state.record(
        original_len=len(original),
        pruned_len=len(pruned),
        tool=payload.get("tool_name", ""),
        query=query,
        session_id=payload.get("session_id", ""),
    )

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": _with_text(result, pruned),
        }
    }


def main() -> int:
    if os.environ.get("HAY_DISABLED"):
        return 0  # clean off-switch: the eval's baseline runs set this
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:
        _breadcrumb(f"invoked but unparseable stdin={len(raw)}B")
        return 0  # malformed -> fail open
    _breadcrumb(f"invoked tool={payload.get('tool_name')} stdin={len(raw)}B")
    try:
        result = build_response(
            payload,
            prune_fn=lambda text, query: client.prune(text=text, query=query),
        )
    except Exception:
        return 0  # server down / any error -> fail open
    if result:
        try:
            out = json.dumps(result)  # serialize fully before writing: never emit partial JSON
        except Exception:
            return 0  # fail open: a garbled rewrite is worse than none
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
