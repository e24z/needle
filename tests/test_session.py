"""Session presence: the `manage` subprocess actually serving, and run_session's
lease/heartbeat/release loop.

Fake backend, temp socket -- no real model, no Claude. The detached-spawn path is
exercised via a directly-managed subprocess (so we can tear it down); the lease
loop is exercised against an in-thread manager. Run:

    PYTHONPATH=. python3 tests/test_session.py
"""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["HAY_NO_EVENTS"] = "1"  # legacy compatibility alias; don't write the real local event log

from needle.runtime import client, naming  # noqa: E402
from needle.runtime import session as session_mod  # noqa: E402
from needle.runtime.backends import FakePruner  # noqa: E402
from needle.runtime.manager import serve_manager  # noqa: E402
from needle.runtime.session import run_session  # noqa: E402

_ROOT = str(Path(__file__).resolve().parent.parent)


def _wait(pred, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_manage_subprocess_serves(tmp_sock: Path) -> None:
    env = dict(
        os.environ,
        HAY_MANAGER_SOCKET=str(tmp_sock),
        HAY_BACKEND="fake",
        HAY_NO_EVENTS="1",  # legacy compatibility alias; don't write the real local event log
        PYTHONPATH=_ROOT,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "needle.runtime", "manage"],
        cwd=_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait(lambda: naming.socket_is_live(tmp_sock)), "manage subprocess never served"
        r = client.prune(text="x" * 50, query="q", socket_path=tmp_sock)
        assert r["ok"] and r["backend"] == "fake", r
        assert client.stats(socket_path=tmp_sock)["ok"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_session_lease_loop(tmp_sock: Path) -> None:
    os.environ["HAY_MANAGER_SOCKET"] = str(tmp_sock)
    ready = threading.Event()
    mgr_stop = threading.Event()
    mgr = threading.Thread(
        target=serve_manager,
        kwargs=dict(
            backend_factory=FakePruner, socket_path=str(tmp_sock),
            ready_cb=lambda _p: ready.set(), stop_event=mgr_stop, poll_interval=0.03,
        ),
        daemon=True,
    )
    mgr.start()
    assert ready.wait(5), "manager not ready"

    sess_stop = threading.Event()
    sess = threading.Thread(
        target=run_session, kwargs=dict(stop_event=sess_stop, session_id="sess-1"), daemon=True
    )
    sess.start()
    try:
        # run_session finds the live manager (no spawn) and acquires a lease.
        assert _wait(lambda: client.stats(socket_path=tmp_sock)["sessions"] == 1), "lease not acquired"
    finally:
        sess_stop.set()
        sess.join(timeout=3)
    # Released on exit.
    assert _wait(lambda: client.stats(socket_path=tmp_sock)["sessions"] == 0), "lease not released"
    mgr_stop.set()
    mgr.join(timeout=3)
    os.environ.pop("HAY_MANAGER_SOCKET", None)


def test_session_failure_is_visible() -> None:
    old_ensure = session_mod._ensure_manager
    session_mod._ensure_manager = lambda timeout=10.0: False
    err = StringIO()
    try:
        with redirect_stderr(err):
            code = session_mod.run_session(stop_event=threading.Event(), session_id="sess-fail")
    finally:
        session_mod._ensure_manager = old_ensure
    assert code == 1
    assert "manager did not start" in err.getvalue()
    assert "pruning disabled" in err.getvalue()


def test_prune_without_manager_has_recovery_text(tmp_sock: Path) -> None:
    env = dict(os.environ, HAY_MANAGER_SOCKET=str(tmp_sock), HAY_NO_EVENTS="1", PYTHONPATH=_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "needle.runtime", "prune", "-q", "focus"],
        cwd=_ROOT,
        env=env,
        input="hello",
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "manager is not reachable" in proc.stderr
    assert "python -m needle.runtime status" in proc.stderr
    assert "Traceback" not in proc.stderr


if __name__ == "__main__":
    import tempfile

    test_manage_subprocess_serves(Path(tempfile.mkdtemp()) / "m1.sock")
    test_session_lease_loop(Path(tempfile.mkdtemp()) / "m2.sock")
    test_session_failure_is_visible()
    test_prune_without_manager_has_recovery_text(Path(tempfile.mkdtemp()) / "m3.sock")
    print("test_session OK")
