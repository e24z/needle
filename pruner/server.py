"""Unix-socket server. Loads one backend, answers prune requests, lives until
told to stop. Knows nothing about who started it."""

from __future__ import annotations

import os
import signal
import socket
import threading
from pathlib import Path
from typing import Callable

from . import naming
from .backends import FakePruner, PrunerBackend
from .protocol import decode, encode


def _handle(conn: socket.socket, backend: PrunerBackend) -> None:
    conn.settimeout(60)  # a silent client must not hang the (serial) server
    try:
        _serve_conn(conn, backend)
    except OSError:  # includes socket.timeout; never crash the accept loop
        try:
            conn.close()
        except OSError:
            pass


def _serve_conn(conn: socket.socket, backend: PrunerBackend) -> None:
    with conn, conn.makefile("rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                req = decode(line)
                text = req.get("text", "")
                query = req.get("query", "")
                pruned = backend.prune(text=text, query=query)
                resp = {
                    "ok": True,
                    "text": pruned,
                    "original_len": len(text),
                    "pruned_len": len(pruned),
                    "backend": getattr(backend, "name", "unknown"),
                }
            except Exception as exc:  # never let one bad request kill the conn
                resp = {"ok": False, "error": str(exc)}
            conn.sendall(encode(resp))


def _socket_is_live(path: Path) -> bool:
    """True if something is already accepting connections on this socket."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def serve_forever(
    backend: PrunerBackend | None = None,
    socket_path: str | Path | None = None,
    ready_cb: Callable[[Path], None] | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    backend = backend or FakePruner()
    sock_path = Path(socket_path) if socket_path else naming.socket_path()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        if _socket_is_live(sock_path):
            # Another session in this project already runs a server here; defer
            # to it rather than clobbering its socket.
            if ready_cb:
                ready_cb(sock_path)
            return
        sock_path.unlink()  # stale socket left by an unclean exit

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(16)
    srv.settimeout(0.5)  # so we can notice stop_event between accepts

    if ready_cb:
        ready_cb(sock_path)

    stop_event = stop_event or threading.Event()

    # Clean teardown when the session ends. The monitor kills our parent shell;
    # SIGTERM handles a direct signal, and the getppid()==1 check below catches
    # being orphaned (parent gone) so the socket never lingers stale.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    try:
        while not stop_event.is_set():
            if os.getppid() == 1:
                break
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            # Handle inline (serial): one session's tool calls are sequential, and
            # MLX's Metal stream is thread-bound, so inference must stay on this
            # thread. Per-project sockets give each session its own server.
            _handle(conn, backend)
    finally:
        srv.close()
        if sock_path.exists():
            sock_path.unlink()
