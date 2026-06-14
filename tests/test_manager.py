"""Manager: lazy model lifecycle, leases, idle eviction, first-writer-wins bind.

Fake spy backends + tiny timeouts -- no real model, no Claude.
Run: PYTHONPATH=. python3 tests/test_manager.py
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
from pathlib import Path

from pruner.manager import serve_manager
from pruner.protocol import decode, encode


class SpyBackend:
    name = "spy"

    def __init__(self) -> None:
        self.evicted = 0

    def prune(self, *, text: str, query: str) -> str:
        return text[: len(text) // 2]  # visibly shorter so we can assert pruning

    def evict(self) -> None:
        self.evicted += 1


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
    builds: list[SpyBackend] = []

    def factory() -> SpyBackend:
        b = SpyBackend()
        builds.append(b)
        return b

    ready = threading.Event()
    stop = threading.Event()
    t = threading.Thread(
        target=serve_manager,
        kwargs=dict(
            backend_factory=factory,
            socket_path=tmp,
            ready_cb=lambda _p: ready.set(),
            stop_event=stop,
            lease_ttl=0.5,
            idle_timeout=0.25,
            poll_interval=0.03,
        ),
        daemon=True,
    )
    t.start()
    assert ready.wait(2), "manager never signalled ready"

    try:
        # Leasing does NOT load the model (lazy): nothing built yet.
        assert _call(tmp, {"op": "lease", "session": "s1"})["ok"]
        s = _call(tmp, {"op": "stats"})
        assert s["sessions"] == 1 and s["resident"] is False, s
        assert builds == [], "model loaded before any prune"

        # First prune loads the model.
        r = _call(tmp, {"op": "prune", "text": "abcdefghij", "query": "x"})
        assert r["ok"] and r["pruned_len"] < r["original_len"], r
        assert len(builds) == 1, builds
        assert _call(tmp, {"op": "stats"})["resident"] is True

        # Heartbeat holds the lease; no eviction while leased.
        time.sleep(0.12)
        assert _call(tmp, {"op": "heartbeat", "session": "s1"})["ok"]
        assert _call(tmp, {"op": "stats"})["sessions"] == 1
        assert builds[0].evicted == 0

        # Release -> idle -> model evicted AND dropped (memory freed).
        assert _call(tmp, {"op": "release", "session": "s1"})["ok"]
        assert _wait_until(lambda: builds[0].evicted >= 1), "model not evicted when idle"
        assert _call(tmp, {"op": "stats"})["resident"] is False

        # Next prune reloads (a fresh backend is built).
        assert _call(tmp, {"op": "prune", "text": "abcdefghij", "query": "x"})["ok"]
        assert len(builds) == 2, "model did not reload after eviction"

        # A crashed session (leases, never heartbeats) is reaped after lease_ttl.
        assert _call(tmp, {"op": "lease", "session": "s2"})["ok"]
        assert _wait_until(
            lambda: _call(tmp, {"op": "stats"})["sessions"] == 0
        ), "stale lease was not reaped"

        # First-writer-wins: a second manager on the same socket defers and returns.
        second = threading.Event()
        threading.Thread(
            target=lambda: (
                serve_manager(backend_factory=factory, socket_path=tmp, ready_cb=lambda _p: None),
                second.set(),
            ),
            daemon=True,
        ).start()
        assert second.wait(2), "second manager did not defer to the first"
        assert _call(tmp, {"op": "stats"})["ok"], "first manager stopped serving"

        print("test_manager OK")
        return 0
    finally:
        stop.set()
        t.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
