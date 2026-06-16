#!/usr/bin/env python3
"""Hay status line: Hay's own state, one animated line.

    ⠿ hay · 1.2k tokens saved · 8 prunes

The leading glyph is the manager's REAL residency, queried from `stats` (not just
"is the socket up?"). Everything animates (needs refreshInterval: 1) — no glyph
is ever frozen:
  -    down      gray   — no manager (brightness breathe)
  ·    cold      blue   — manager up, model NOT loaded (brightness breathe)
  ⠋⠙… loading   amber  — manager busy/unresponsive: cold-loading or mid-prune (spin)
  ✗    degraded  red    — manager up but the model couldn't load; passing through
                         unchanged (a "fake (…)" backend) (brightness breathe)
  ⠤⠶⠿ ready      green  — model resident, idle (fill breathe)
  ⠋⠙… active     cyan   — a prune landed within the last few seconds (spin)

Glyphs/colours are constants below; behavior thresholds are env vars in the
manager. Savings are per-session (Claude's session_id); tokens ≈ saved chars / 4.

Width note: animated glyphs are braille (U+2800-28FF, reliably 1 cell). The
separator and cold dot are U+00B7 MIDDLE DOT (·) — the same interpunct Claude's
own statusbar uses, so it renders fine. (It is technically East-Asian-ambiguous
width; if the bar ever tears, suspect that and fall back to ' / ' — but it tests
clean here.) Fails silent.
"""

# TODO: this should be an actual saving metric if not already

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

# adapters/claude/statusline.py: put the repo root on the path (engine package
# `pruner`) and this adapter dir (for the adapter-local `state`).
_HERE = Path(__file__).resolve()
sys.path[0:0] = [str(_HERE.parents[2]), str(_HERE.parent)]  # repo root, adapters/claude

ACTIVE_SECS = 3  # a prune within this many seconds → "active"
STATS_TIMEOUT = (
    0.25  # short: a blocked (loading) manager shows as "loading", not a hang
)
SEP = " · "

# Full braille rotation (includes the left-vertical ⠇⠏ so it doesn't teleport).
SPIN_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
# "ready" breathes: bottom-2 → bottom-4 → all-6 → bottom-4 → (loop).
PULSE_FRAMES = ["⠤", "⠶", "⠿", "⠶"]
# Brightness breathe for single-char glyphs (faint → normal → bold → normal),
# so a static dot still reads as "alive, just idle".
INTENSITY_FRAMES = ["2", "", "1", ""]
COLD_GLYPH = "·"  # U+00B7 middle dot: model not in memory
DEGRADED_GLYPH = "✗"  # U+2717: model meant to be loaded but isn't (running fake)

CLR_DOWN = "38;5;240"  # gray
CLR_COLD = "38;5;67"  # steel blue
CLR_LOADING = "38;5;179"  # amber
CLR_DEGRADED = "38;5;196"  # red
CLR_READY = "38;5;35"  # green
CLR_ACTIVE = "38;5;87"  # cyan


def _ansi(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _breathe(color: str, glyph: str) -> str:
    """A single glyph pulsing via SGR intensity, so it's never frozen."""
    i = INTENSITY_FRAMES[int(time.time()) % len(INTENSITY_FRAMES)]
    return _ansi(f"{color};{i}" if i else color, glyph)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{round(n / 1_000)}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _query():
    """Ask the manager for stats. Returns the dict, the sentinel "loading" if the
    manager is up but unresponsive (blocked cold-loading or mid-prune — it can't
    answer while it's busy), or None if there's no manager at all."""
    try:
        from pruner import client

        return client.stats(timeout=STATS_TIMEOUT)
    except socket.timeout:
        return "loading"
    except OSError:
        return None  # no socket / refused / unclean


def _decide(stats, recent: bool) -> str:
    """Pure: map (stats, recent-prune?) → indicator state. Testable offline."""
    if stats is None:
        return "down"
    if stats == "loading":
        return "loading"
    if not isinstance(stats, dict) or not stats.get("ok"):
        return "down"
    if not stats.get("resident"):
        return "cold"
    backend = stats.get("backend")
    if isinstance(backend, str) and backend.startswith("fake ("):
        return "degraded"  # resident, but it's the named fake — don't show green
    return "active" if recent else "ready"


def _state(payload: dict) -> tuple[str, int, int]:
    """Return (indicator_state, calls, tokens)."""
    import state

    s = state.read(payload.get("session_id") or None)
    calls = int(s.get("calls", 0))
    tokens = int(s.get("saved_chars", 0)) // 4
    recent = time.time() - float(s.get("updated_at", 0.0)) < ACTIVE_SECS
    return _decide(_query(), recent), calls, tokens


def _indicator(state: str) -> str:
    t = int(time.time())
    if state == "down":
        return _breathe(CLR_DOWN, "-")
    if state == "cold":
        return _breathe(CLR_COLD, COLD_GLYPH)
    if state == "degraded":
        return _breathe(CLR_DEGRADED, DEGRADED_GLYPH)
    if state == "loading":
        return _ansi(CLR_LOADING, SPIN_FRAMES[t % len(SPIN_FRAMES)])
    if state == "active":
        return _ansi(CLR_ACTIVE, SPIN_FRAMES[t % len(SPIN_FRAMES)])
    return _ansi(CLR_READY, PULSE_FRAMES[t % len(PULSE_FRAMES)])  # ready


def _render(state: str, calls: int, tokens: int) -> str | None:
    """Build the line, degrading to shorter forms rather than wrapping. Returns
    None if even the bare name won't fit (print nothing)."""
    cols = int(os.environ.get("COLUMNS", "80"))
    name = "hay"
    plural = "s" if calls != 1 else ""
    forms = [
        f"{name}{SEP}{_fmt_tokens(tokens)} tokens saved{SEP}{calls} prune{plural}",
        f"{name}{SEP}{_fmt_tokens(tokens)}t{SEP}{calls}p",
        name,
    ]
    ind = _indicator(state)
    for line in forms:
        if 2 + len(line) <= cols - 1:  # 1 glyph + 1 space, plain-text width
            return f"{ind} {line}"
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    try:
        state, calls, tokens = _state(payload)
        line = _render(state, calls, tokens)
        if line:
            sys.stdout.write(line)
    except Exception:
        pass  # silent: never break the status line
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
