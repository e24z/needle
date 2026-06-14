"""The whole first ring, proven: start the server, send text, get it back
unchanged. No pytest required -- run directly:

    python3 tests/test_roundtrip.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

# Make the repo root importable whether run directly or under a test runner.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pruner import client, server  # noqa: E402
from pruner.backends import FakePruner  # noqa: E402


def test_roundtrip_returns_text_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sock = Path(tmp) / "test.sock"
        ready = threading.Event()
        stop = threading.Event()

        t = threading.Thread(
            target=server.serve_forever,
            kwargs=dict(
                backend=FakePruner(),
                socket_path=str(sock),
                ready_cb=lambda _p: ready.set(),
                stop_event=stop,
            ),
            daemon=True,
        )
        t.start()
        assert ready.wait(timeout=5), "server did not become ready"

        text = "def f():\n    return 42\n" * 100
        resp = client.prune(text=text, query="the function f", socket_path=str(sock))

        assert resp["ok"] is True
        assert resp["text"] == text  # fake pruner: unchanged
        assert resp["original_len"] == len(text)
        assert resp["pruned_len"] == len(text)
        assert resp["backend"] == "fake"

        stop.set()
        t.join(timeout=5)


if __name__ == "__main__":
    test_roundtrip_returns_text_unchanged()
    print("ok: round-trip returns text unchanged")
