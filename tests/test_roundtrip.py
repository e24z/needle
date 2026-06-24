"""Round-trip through the machine-wide manager: start it, prune via the client,
get a response back. No pytest required:

    python3 tests/test_roundtrip.py
"""

from __future__ import annotations

import os

os.environ["HAY_NO_EVENTS"] = "1"  # compatibility alias; don't write the real local event log

import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pruner import client  # noqa: E402
from pruner.backends import FakePruner  # noqa: E402
from pruner.manager import serve_manager  # noqa: E402


def test_roundtrip_returns_text_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sock = Path(tmp) / "manager.sock"
        ready = threading.Event()
        stop = threading.Event()

        t = threading.Thread(
            target=serve_manager,
            kwargs=dict(
                backend_factory=FakePruner,
                socket_path=str(sock),
                ready_cb=lambda _p: ready.set(),
                stop_event=stop,
                poll_interval=0.05,
            ),
            daemon=True,
        )
        t.start()
        assert ready.wait(timeout=5), "manager did not become ready"

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
    print("ok: round-trip through manager returns text unchanged")
