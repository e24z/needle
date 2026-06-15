"""The statusline's residency decision is a pure function of (stats, recent).
This pins the honest ontology without a live manager.

Run: PYTHONPATH=. python3 tests/test_statusline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "statusline"))

import statusline  # noqa: E402

D = statusline._decide


def main() -> int:
    assert D(None, False) == "down"                         # no manager
    assert D("loading", False) == "loading"                 # up but unresponsive
    assert D({"ok": False}, False) == "down"                # bad response
    assert D({"ok": True, "resident": False}, False) == "cold"   # up, model not loaded
    assert D({"ok": True, "resident": True}, False) == "ready"   # loaded, idle
    assert D({"ok": True, "resident": True}, True) == "active"   # loaded + recent prune
    # recent-prune is irrelevant unless the model is actually resident
    assert D({"ok": True, "resident": False}, True) == "cold"

    # every state maps to a non-empty indicator (no crashes)
    for st in ("down", "cold", "loading", "ready", "active"):
        assert statusline._indicator(st)

    print("test_statusline OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
