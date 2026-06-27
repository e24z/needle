"""`needle.runtime status` renders an honest snapshot from (stats, events): down / cold /
DEGRADED / ready, plus recent events. Pure function, no live manager.

Run: PYTHONPATH=src python3 tests/test_status.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.runtime.cli import _render_status  # noqa: E402


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

    identified = _render_status(
        _stats(
            package_id="e24z/mlx-pi-soft-lamr",
            host_binding="pi/native-tools",
            runtime_profile="local_mlx_adaptive",
            backend_id="e24z/code-pruner-mlx",
        ),
        [],
    )
    assert "package e24z/mlx-pi-soft-lamr" in identified
    assert "host pi/native-tools" in identified
    assert "profile local_mlx_adaptive" in identified
    assert "backend-id e24z/code-pruner-mlx" in identified

    recent = _render_status(
        _stats(
            last_prune={
                "backend": "code-pruner",
                "saved_chars": 128,
                "chunks": 4,
                "batches": 2,
                "batch_retry_count": 1,
                "batch_downgrade_reason": "retry_serial_after_resource_error",
                "batch_sizes": [2, 2],
                "max_length": 1024,
                "padding_waste_ratio": 0.25,
                "total_ms": 42.0,
            }
        ),
        [],
    )
    assert "last prune" in recent, recent
    assert "chunks 4" in recent and "batches 2" in recent, recent
    assert "batch_retries 1" in recent, recent
    assert "batch_sizes [2,2]" in recent and "padding 25.0%" in recent, recent

    ev = _render_status(None, [{"ts": 0, "event": "model_load", "backend": "code-pruner"}])
    assert "recent events" in ev and "model_load" in ev and "backend=code-pruner" in ev

    print("test_status OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
