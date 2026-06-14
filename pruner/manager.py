"""Machine-wide model residency manager.

One per machine, not per session. Like the per-project server it answers prune
requests, but its lifetime is decoupled from any single session: sessions hold
*leases* (acquire -> heartbeat... -> release) and the manager keeps the model
resident only while at least one lease is live.

The backend (the model) is built LAZILY on the first prune and DROPPED on idle
eviction, so an idle machine actually gives the memory back; it reloads on the
next prune. When the last lease drops, an idle clock starts; if it expires the
model is evicted. A session that crashes without releasing can't pin memory
forever -- a lease with no heartbeat for `lease_ttl` is reaped.

Deliberately NO getppid()==1 self-teardown: unlike the session-owned server, the
manager must OUTLIVE the process that launched it.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import naming
from .backends import PrunerBackend, get_backend
from .protocol import decode, encode

LEASE_TTL = float(os.environ.get("HAY_LEASE_TTL", "90"))
IDLE_TIMEOUT = float(os.environ.get("HAY_IDLE_TIMEOUT", "300"))


class Manager:
    """Lease bookkeeping + lazy model lifecycle. Single-threaded: every method
    here runs on the serve loop's thread (MLX inference is thread-bound), so no
    locks are needed."""

    def __init__(
        self,
        backend_factory: Callable[[], PrunerBackend],
        *,
        lease_ttl: float = LEASE_TTL,
        idle_timeout: float = IDLE_TIMEOUT,
    ) -> None:
        self._make = backend_factory
        self._backend: PrunerBackend | None = None  # built on first prune, dropped on evict
        self.lease_ttl = lease_ttl
        self.idle_timeout = idle_timeout
        self._beats: dict[str, float] = {}      # session id -> last heartbeat (monotonic)
        self._empty_since: float | None = None  # when leases last fell to zero

    @property
    def resident(self) -> bool:
        """True iff the model is currently loaded in memory."""
        return self._backend is not None

    def _ensure_backend(self) -> PrunerBackend:
        if self._backend is None:
            self._backend = self._make()  # cold load (blocks this prune; serial by design)
        return self._backend

    # -- request handling -------------------------------------------------
    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op", "prune")
        now = time.monotonic()
        if op == "prune":
            backend = self._ensure_backend()
            text = req.get("text", "")
            pruned = backend.prune(text=text, query=req.get("query", ""))
            return {
                "ok": True,
                "text": pruned,
                "original_len": len(text),
                "pruned_len": len(pruned),
                "backend": getattr(backend, "name", "unknown"),
            }
        if op in ("lease", "heartbeat"):
            self._beats[req.get("session", "")] = now
            return {"ok": True}
        if op == "release":
            self._beats.pop(req.get("session", ""), None)
            return {"ok": True}
        if op == "stats":
            return {
                "ok": True,
                "resident": self.resident,
                "sessions": len(self._beats),
                "backend": getattr(self._backend, "name", None),
            }
        return {"ok": False, "error": f"unknown op: {op!r}"}

    # -- periodic maintenance (called between accepts) --------------------
    def maintain(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        # Reap leases whose session stopped heartbeating (crashed / killed).
        dead = [s for s, ts in self._beats.items() if now - ts > self.lease_ttl]
        for s in dead:
            del self._beats[s]
        # Idle eviction: no live leases for idle_timeout -> drop the model.
        if self._beats:
            self._empty_since = None
        elif self._empty_since is None:
            self._empty_since = now
        elif self.resident and now - self._empty_since >= self.idle_timeout:
            self._evict()

    def _evict(self) -> None:
        backend, self._backend = self._backend, None  # drop the ref -> model memory freed
        evict = getattr(backend, "evict", None)
        if callable(evict):
            evict()  # let the backend release device caches too
        self._empty_since = None  # evicted; don't fire again until reloaded


def _serve_conn(conn: socket.socket, mgr: Manager) -> None:
    conn.settimeout(60)  # a silent client must not hang the serial loop
    try:
        with conn, conn.makefile("rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    resp = mgr.handle(decode(line))
                except Exception as exc:  # never let one bad request kill the conn
                    resp = {"ok": False, "error": str(exc)}
                conn.sendall(encode(resp))
    except OSError:  # includes socket.timeout
        try:
            conn.close()
        except OSError:
            pass


def serve_manager(
    backend_factory: Callable[[], PrunerBackend] | None = None,
    socket_path: str | Path | None = None,
    ready_cb: Callable[[Path], None] | None = None,
    stop_event: threading.Event | None = None,
    *,
    lease_ttl: float = LEASE_TTL,
    idle_timeout: float = IDLE_TIMEOUT,
    poll_interval: float = 0.5,
) -> None:
    """Bind the machine-wide socket and serve until stopped. First writer wins:
    if a manager is already live on the socket, defer to it and return."""
    backend_factory = backend_factory or get_backend
    sock_path = Path(socket_path) if socket_path else naming.manager_socket_path()
    sock_path.parent.mkdir(parents=True, exist_ok=True)

    if sock_path.exists():
        if naming.socket_is_live(sock_path):
            if ready_cb:  # another manager already owns this machine; defer to it
                ready_cb(sock_path)
            return
        sock_path.unlink()  # stale socket from an unclean exit

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
    except OSError:  # lost the bind race to another manager; defer to it
        srv.close()
        if ready_cb:
            ready_cb(sock_path)
        return
    srv.listen(16)
    srv.settimeout(poll_interval)  # wake periodically to maintain + check stop

    mgr = Manager(backend_factory, lease_ttl=lease_ttl, idle_timeout=idle_timeout)

    if ready_cb:
        ready_cb(sock_path)

    stop_event = stop_event or threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    try:
        while not stop_event.is_set():
            mgr.maintain()
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            _serve_conn(conn, mgr)  # serial: one model, one Metal thread
    finally:
        srv.close()
        if sock_path.exists():
            sock_path.unlink()
