"""Version handshake: a manager started on one code version steps aside when a
session leases announcing a different version, so an edit takes effect on the
next session instead of a stale manager lingering. Fake backend, temp socket.

Run: PYTHONPATH=src python3 tests/test_version.py
"""

from __future__ import annotations

import os

os.environ["HAY_NO_EVENTS"] = "1"  # legacy compatibility alias; don't write the real local event log

import socket  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.runtime import naming  # noqa: E402
from needle.runtime.backends import FakePruner  # noqa: E402
from needle.runtime.manager import serve_manager  # noqa: E402
from needle.runtime.protocol import decode, encode  # noqa: E402


def _call(sock_path: Path, req: dict) -> dict:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(2)
    c.connect(str(sock_path))
    try:
        c.sendall(encode(req))
        with c.makefile("rb") as f:
            return decode(f.readline())
    finally:
        c.close()


def _wait_until(pred, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "manager.sock"
    ready = threading.Event()
    stop = threading.Event()
    t = threading.Thread(
        target=serve_manager,
        kwargs=dict(
            backend_factory=FakePruner, socket_path=str(tmp), version="v-old",
            ready_cb=lambda _p: ready.set(), stop_event=stop, poll_interval=0.03,
        ),
        daemon=True,
    )
    t.start()
    assert ready.wait(2), "manager never signalled ready"

    try:
        # Matching version leases fine.
        assert _call(tmp, {"op": "lease", "session": "a", "version": "v-old"})["ok"]
        # No version announced -> accepted (backward compatible).
        assert _call(tmp, {"op": "lease", "session": "b", "version": ""})["ok"]
        # A different (newer) version -> refused as stale, and the manager steps aside.
        r = _call(tmp, {"op": "lease", "session": "c", "version": "v-new"})
        assert r["ok"] is False and r.get("stale") is True, r
        assert _wait_until(lambda: not naming.socket_is_live(tmp)), "stale manager did not step aside"
        print("test_version OK")
        return 0
    finally:
        stop.set()
        t.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
