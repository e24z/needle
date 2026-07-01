"""Session presence: the `manage` subprocess actually serving, and run_session's
lease/heartbeat/release loop.

Fake backend, temp socket -- no real model, no Claude. The detached-spawn path is
exercised via a directly-managed subprocess (so we can tear it down); the lease
loop is exercised against an in-thread manager. Run:

    PYTHONPATH=src python3 tests/test_session.py
"""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ["HAY_NO_EVENTS"] = "1"  # legacy compatibility alias; don't write the real local event log

from needle.runtime import client, naming  # noqa: E402
from needle.runtime import session as session_mod  # noqa: E402
from needle.runtime.backends import FakePruner  # noqa: E402
from needle.runtime.manager import serve_manager  # noqa: E402
from needle.runtime.session import run_session  # noqa: E402

_ROOT = str(Path(__file__).resolve().parent.parent / "src")


def _wait(pred, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


class BlockingBackend:
    name = "blocking"

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    def prune(self, *, text: str, query: str) -> str:
        self.started.set()
        assert self.release.wait(5), "blocked prune was not released"
        return text


def test_manage_subprocess_serves(tmp_sock: Path) -> None:
    env = dict(
        os.environ,
        HAY_MANAGER_SOCKET=str(tmp_sock),
        HAY_BACKEND="fake",
        HAY_NO_EVENTS="1",  # legacy compatibility alias; don't write the real local event log
        PYTHONPATH=_ROOT,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "needle.runtime", "manage", "--raw"],
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
            runtime_identity=session_mod._requested_runtime_identity(),
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


def test_session_manager_spawn_command_carries_package_context() -> None:
    argv = session_mod._manager_argv(
        package_id="e24z/mlx-mcp-bash-reference",
        host_binding="mcp/bash",
    )
    assert argv[:3] == [sys.executable, "-m", "needle.runtime"]
    assert "--package" in argv
    assert "e24z/mlx-mcp-bash-reference" in argv
    assert "--host-binding" in argv
    assert "mcp/bash" in argv


def test_client_lease_rejects_reserved_runtime_identity_fields() -> None:
    old_request = client._request
    seen: dict[str, object] = {}

    def fake_request(req, socket_path=None, timeout: float = 30.0):  # noqa: ANN001
        seen.clear()
        seen.update(req)
        return {"ok": True}

    client._request = fake_request
    try:
        assert client.lease("sess", "v1", runtime_identity={"package_id": "pkg"})["ok"]
        assert seen["op"] == "lease"
        assert seen["session"] == "sess"
        assert seen["version"] == "v1"
        assert seen["package_id"] == "pkg"

        for key in ("op", "session", "version", "token"):
            try:
                client.lease("sess", "v1", runtime_identity={key: "clobber"})
            except ValueError as exc:
                assert key in str(exc)
            else:
                raise AssertionError(f"reserved runtime_identity key was accepted: {key}")
    finally:
        client._request = old_request


def test_session_manager_spawn_log_is_private() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        sock = Path(td) / "manager.sock"
        old_env = {
            "NEEDLE_HOME": os.environ.get("NEEDLE_HOME"),
            "HAY_MANAGER_SOCKET": os.environ.get("HAY_MANAGER_SOCKET"),
        }
        old_socket_is_live = session_mod.naming.socket_is_live
        old_popen = session_mod.subprocess.Popen
        spawned = {"value": False}

        class FakePopen:
            def __init__(self, *_args, **_kwargs) -> None:
                spawned["value"] = True

        def fake_socket_is_live(_path: Path) -> bool:
            return spawned["value"]

        os.environ["NEEDLE_HOME"] = str(home)
        os.environ["HAY_MANAGER_SOCKET"] = str(sock)
        session_mod.naming.socket_is_live = fake_socket_is_live
        session_mod.subprocess.Popen = FakePopen
        try:
            assert session_mod._ensure_manager(timeout=0.5)
            log = home / "manager.log"
            assert home.stat().st_mode & 0o777 == 0o700
            assert log.stat().st_mode & 0o777 == 0o600
        finally:
            session_mod.naming.socket_is_live = old_socket_is_live
            session_mod.subprocess.Popen = old_popen
            for name, value in old_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_manage_refuses_live_manager_with_different_identity(tmp_sock: Path) -> None:
    ready = threading.Event()
    stop = threading.Event()
    mgr = threading.Thread(
        target=serve_manager,
        kwargs=dict(
            backend_factory=FakePruner,
            socket_path=str(tmp_sock),
            ready_cb=lambda _p: ready.set(),
            stop_event=stop,
            poll_interval=0.03,
            runtime_identity={
                "package_id": "e24z/mlx-pi-reference",
                "host_binding": "pi/native-tools",
                "backend_id": "e24z/code-pruner-mlx",
                "runtime_profile": "local_mlx_adaptive",
            },
        ),
        daemon=True,
    )
    mgr.start()
    assert ready.wait(5), "manager not ready"
    env = dict(
        os.environ,
        HAY_MANAGER_SOCKET=str(tmp_sock),
        HAY_NO_EVENTS="1",
        PYTHONPATH=_ROOT,
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "needle.runtime",
            "manage",
            "--package",
            "e24z/mlx-mcp-bash-reference",
            "--host-binding",
            "mcp/bash",
        ],
        cwd=_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        assert proc.returncode == 1, proc.stderr
        assert "identity mismatch" in proc.stderr
        assert "manager listening" not in proc.stderr
    finally:
        client.stop(socket_path=tmp_sock)
        stop.set()
        mgr.join(timeout=3)


def test_acquire_replaces_busy_stale_manager(tmp_sock: Path) -> None:
    old_socket = os.environ.get("HAY_MANAGER_SOCKET")
    old_ensure = session_mod._ensure_manager
    os.environ["HAY_MANAGER_SOCKET"] = str(tmp_sock)
    identity_a = session_mod._requested_runtime_identity(
        package_id="e24z/mlx-pi-reference",
        host_binding="pi/native-tools",
    )
    identity_b = session_mod._requested_runtime_identity(
        package_id="e24z/mlx-mcp-bash-reference",
        host_binding="mcp/bash",
    )
    version = naming.code_version()
    old_ready = threading.Event()
    old_stop = threading.Event()
    prune_started = threading.Event()
    prune_release = threading.Event()
    old_mgr = threading.Thread(
        target=serve_manager,
        kwargs=dict(
            backend_factory=lambda: BlockingBackend(prune_started, prune_release),
            socket_path=str(tmp_sock),
            ready_cb=lambda _p: old_ready.set(),
            stop_event=old_stop,
            poll_interval=0.03,
            runtime_identity=identity_a,
            version=version,
        ),
        daemon=True,
    )
    old_mgr.start()
    assert old_ready.wait(2), "old manager not ready"
    prune_thread = threading.Thread(
        target=lambda: client.prune(
            text="abcdef",
            query="q",
            socket_path=tmp_sock,
            timeout=5,
        ),
        daemon=True,
    )
    prune_thread.start()
    assert prune_started.wait(2), "prune did not enter blocking backend"

    new_ready = threading.Event()
    new_stop = threading.Event()
    new_mgr: list[threading.Thread] = []

    def fake_ensure_manager(timeout: float = 10.0, **_kwargs) -> bool:
        if naming.socket_is_live(tmp_sock):
            return True
        if not new_mgr:
            thread = threading.Thread(
                target=serve_manager,
                kwargs=dict(
                    backend_factory=FakePruner,
                    socket_path=str(tmp_sock),
                    ready_cb=lambda _p: new_ready.set(),
                    stop_event=new_stop,
                    poll_interval=0.03,
                    runtime_identity=identity_b,
                    version=version,
                ),
                daemon=True,
            )
            new_mgr.append(thread)
            thread.start()
        return new_ready.wait(timeout)

    session_mod._ensure_manager = fake_ensure_manager
    try:
        assert session_mod._acquire(
            "sess-b",
            version,
            attempts=4,
            package_id="e24z/mlx-mcp-bash-reference",
            host_binding="mcp/bash",
        )
        stats = client.stats(socket_path=tmp_sock)
        assert stats["package_id"] == identity_b["package_id"], stats
        assert stats["host_binding"] == identity_b["host_binding"], stats
        assert stats["sessions"] == 1, stats
    finally:
        prune_release.set()
        old_stop.set()
        new_stop.set()
        session_mod._ensure_manager = old_ensure
        if old_socket is None:
            os.environ.pop("HAY_MANAGER_SOCKET", None)
        else:
            os.environ["HAY_MANAGER_SOCKET"] = old_socket
        prune_thread.join(timeout=3)
        old_mgr.join(timeout=3)
        for thread in new_mgr:
            thread.join(timeout=3)


def test_session_failure_is_visible() -> None:
    old_ensure = session_mod._ensure_manager
    session_mod._ensure_manager = lambda timeout=10.0, **_kwargs: False
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
    test_session_manager_spawn_command_carries_package_context()
    test_client_lease_rejects_reserved_runtime_identity_fields()
    test_session_manager_spawn_log_is_private()
    test_manage_refuses_live_manager_with_different_identity(Path(tempfile.mkdtemp()) / "m4.sock")
    test_acquire_replaces_busy_stale_manager(Path(tempfile.mkdtemp()) / "m5.sock")
    test_session_failure_is_visible()
    test_prune_without_manager_has_recovery_text(Path(tempfile.mkdtemp()) / "m3.sock")
    print("test_session OK")
