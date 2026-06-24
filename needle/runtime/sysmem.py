"""macOS memory signals (stdlib only, via sysctl/vm_stat).

Used by the manager to refuse a ~GB cold model load when the machine can't take
it. Degrades to "plenty of memory / no pressure" on platforms (or in
environments) where these signals aren't available, so Needle never accidentally
blocks itself off macOS.
"""

from __future__ import annotations

import re
import subprocess

PRESSURE_NORMAL = 1
PRESSURE_WARN = 2
PRESSURE_CRITICAL = 4

_UNKNOWN_AVAIL_MB = 1 << 20  # 1 TB sentinel: "unknown" must never block a load


def pressure_level() -> int:
    """macOS kernel memory-pressure: 1 normal, 2 warning, 4 critical.
    Returns PRESSURE_NORMAL if the signal isn't readable (non-macOS, etc.)."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=2,
        )
        return int(out.stdout.strip())
    except Exception:
        return PRESSURE_NORMAL


def available_mb() -> int:
    """Approximate reclaimable memory in MB (free + inactive + purgeable +
    speculative pages, from vm_stat). Returns a huge sentinel if unreadable, so
    an unknown environment never blocks a load."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
        page = 4096
        m = re.search(r"page size of (\d+)", out)
        if m:
            page = int(m.group(1))

        def pages(label: str) -> int:
            mm = re.search(rf"{label}:\s+(\d+)\.", out)
            return int(mm.group(1)) if mm else 0

        reclaimable = (
            pages("Pages free")
            + pages("Pages inactive")
            + pages("Pages purgeable")
            + pages("Pages speculative")
        )
        return reclaimable * page // (1024 * 1024)
    except Exception:
        return _UNKNOWN_AVAIL_MB


def memstat() -> tuple[int, int]:
    """(pressure_level, available_mb). One call so callers read both together."""
    return pressure_level(), available_mb()
