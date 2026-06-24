"""Session presence: the per-session process the monitor runs.

This replaces "the session owns the daemon". The session now owns only its
LEASE: this process makes sure the machine-wide manager is up, acquires a lease,
heartbeats while the session is alive, and releases on exit. Model residency is
the manager's decision, driven by whether any lease is live.

The lease carries this session's code version. If the running manager started on
older code, it steps aside and we start a fresh one (see _acquire), so an edit to
the source actually takes effect on the next session.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import client, naming

HEARTBEAT_INTERVAL = float(
    os.environ.get("NEEDLE_HEARTBEAT_INTERVAL")
    or os.environ.get("HAY_HEARTBEAT_INTERVAL", "30")
)
_REPO_ROOT = str(Path(__file__).resolve().parents[2])


def _ensure_manager(timeout: float = 10.0) -> bool:
    """Make sure a manager is accepting connections, spawning a DETACHED one if
    not. start_new_session puts it in its own session/process group so it
    OUTLIVES this session -- the monitor kills our group when the session ends,
    and the manager must survive that to serve the next session."""
    sock = naming.manager_socket_path()
    if naming.socket_is_live(sock):
        return True
    home = naming.app_home()
    home.mkdir(parents=True, exist_ok=True)
    log = open(home / "manager.log", "a")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "needle.runtime", "manage"],
            start_new_session=True,
            stdout=log,
            stderr=log,
            cwd=_REPO_ROOT,
        )
    finally:
        log.close()  # the child inherited its own fd; the parent holds nothing open
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if naming.socket_is_live(sock):
            return True
        time.sleep(0.1)
    return False


def _acquire(session_id: str, version: str, attempts: int = 4) -> bool:
    """Lease, handling a stale manager: if it reports our code is newer than what
    it started on, it steps aside -- we wait for the socket to free, start a
    fresh manager on the current code, and retry."""
    for _ in range(attempts):
        try:
            resp = client.lease(session_id, version)
        except OSError:
            if not _ensure_manager():
                return False
            continue
        if resp.get("ok"):
            return True
        if resp.get("stale"):
            # TODO(takeover-race): we wait for the old manager to free the socket,
            # then _ensure_manager() — but if it's stuck in a long prune past this
            # 10s window, socket_is_live() stays true, _ensure_manager() declines to
            # spawn, and we can burn the retry budget re-hitting the stale manager
            # (ending leaseless). Fix: force a respawn here rather than trusting
            # socket_is_live (or have the stepped-aside manager unlink before draining).
            sock = naming.manager_socket_path()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and naming.socket_is_live(sock):
                time.sleep(0.1)
            if not _ensure_manager():
                return False
            continue
        return False  # refused for some other reason
    return False


def run_session(
    stop_event: threading.Event | None = None,
    session_id: str | None = None,
) -> int:
    # The engine is agent-agnostic: it never reads CLAUDE_* itself. A caller
    # (the CLI's --session, set by an adapter) may pass the host's session id so
    # leases/logs correlate; absent that, a uuid is fine -- the lease only needs
    # a unique id for presence-counting, not the host's identity.
    session_id = session_id or uuid.uuid4().hex
    version = naming.code_version()
    stop_event = stop_event or threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    if not _ensure_manager():
        return 0  # couldn't start a manager: the hook fails open, nothing to hold
    if not _acquire(session_id, version):
        return 0

    last_beat = time.monotonic()
    try:
        while not stop_event.is_set():
            if os.getppid() == 1:  # orphaned (parent gone) -> release and exit
                break
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_INTERVAL:
                try:
                    client.heartbeat(session_id)
                except OSError:
                    _acquire(session_id, version)  # manager died/replaced; re-lease
                last_beat = now
            stop_event.wait(1.0)  # stay responsive to SIGTERM / orphaning
    finally:
        try:
            client.release(session_id)
        except OSError:
            pass
    return 0
