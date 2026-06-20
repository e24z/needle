"""`pruner status` renders an honest snapshot from (stats, events): down / cold /
DEGRADED / ready, plus recent events. Pure function, no live manager.

Run: PYTHONPATH=. python3 tests/test_status.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pruner.cli import _render_status, _status_payload  # noqa: E402


def _stats(**kw) -> dict:
    base = {
        "ok": True, "resident": True, "backend": "code-pruner",
        "sessions": 1, "version": "deadbeefcafe", "pressure": 1, "available_mb": 4096,
    }
    base.update(kw)
    return base


def main() -> int:
    assert "down" in _render_status(None, [])
    assert "down" in _render_status({"ok": False}, [])

    assert "cold" in _render_status(_stats(resident=False, backend=None), [])

    degraded = _render_status(_stats(backend="fake (code-pruner unavailable: no mlx)"), [])
    assert "DEGRADED" in degraded and "ready" not in degraded  # never lie about a fake

    ready = _render_status(_stats(pressure=2, available_mb=1536), [])
    assert "ready" in ready and "code-pruner" in ready
    assert "warning" in ready and "1.5 GB" in ready  # pressure label + free GB

    ev = _render_status(None, [{"ts": 0, "event": "model_load", "backend": "code-pruner"}])
    assert "recent events" in ev and "model_load" in ev and "backend=code-pruner" in ev

    payload = _status_payload(None, [{"event": "release"}])
    assert payload["ok"] is False
    assert payload["manager"] == {"ok": False, "state": "down"}
    assert payload["events"] == [{"event": "release"}]
    assert payload["app"] == "hay"
    assert isinstance(payload["generated_at"], float)
    assert payload["socket"].endswith(".sock")

    live = _status_payload(_stats(sessions=2), [])
    assert live["ok"] is True
    assert live["manager"]["sessions"] == 2

    print("test_status OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
