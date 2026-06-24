"""Prove the hook's decision logic without a live socket: shrinking output gets
replaced, unchanged output passes through, non-targets and tiny outputs are
ignored. Run directly: python3 tests/test_hook.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path[0:0] = [str(_ROOT), str(_ROOT / "adapters" / "claude")]  # engine, Claude adapter

# Keep state writes out of the real ~/.hay during tests.
_TMP = tempfile.mkdtemp()
os.environ["HAY_STATE"] = str(Path(_TMP) / "state.json")

from hooks.post_tool_use import build_response  # noqa: E402

BIG = "x" * 1000


def _read(content) -> dict:
    """A real-shape Read PostToolUse payload."""
    return {
        "tool_name": "Read",
        "transcript_path": "",
        "tool_response": {"type": "text", "file": {"filePath": "/x.py", "content": content}},
    }


def _shrink(text, query):
    return {"ok": True, "text": text[: len(text) // 2]}  # 50% smaller


def _unchanged(text, query):
    return {"ok": True, "text": text}


def test_replaces_when_savings_clear_threshold() -> None:
    out = build_response(_read(BIG), _shrink)
    assert out is not None
    uto = out["hookSpecificOutput"]["updatedToolOutput"]
    # Mirrors the Read shape: only file.content changes, type/filePath preserved.
    assert isinstance(uto, dict)
    assert uto["type"] == "text"
    assert uto["file"]["filePath"] == "/x.py"
    assert uto["file"]["content"] == BIG[:500]


def test_passes_through_when_nothing_saved() -> None:
    assert build_response(_read(BIG), _unchanged) is None


def test_ignores_non_target_tool() -> None:
    payload = {"tool_name": "Bash", "tool_response": {"stdout": BIG}, "transcript_path": ""}
    assert build_response(payload, _shrink) is None


def test_ignores_tiny_output() -> None:
    assert build_response(_read("short"), _shrink) is None


def test_ignores_file_unchanged_reread() -> None:
    # A re-read returns no content -> nothing to prune.
    payload = {
        "tool_name": "Read",
        "transcript_path": "",
        "tool_response": {"type": "file_unchanged", "file": {"filePath": "/x.py"}},
    }
    assert build_response(payload, _shrink) is None


if __name__ == "__main__":
    test_replaces_when_savings_clear_threshold()
    test_passes_through_when_nothing_saved()
    test_ignores_non_target_tool()
    test_ignores_tiny_output()
    test_ignores_file_unchanged_reread()
    print("ok: hook reads tool_response, replaces on real savings, passes through otherwise")
