"""Thin client for the machine-wide manager. The hook prunes through here, the
session process leases through here, the statusline reads stats through here.
Everything targets the manager socket unless an explicit socket_path is given."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from . import naming
from .protocol import decode, encode


def _request(
    req: dict, socket_path: str | Path | None = None, timeout: float = 30.0
) -> dict[str, Any]:
    sock_path = Path(socket_path) if socket_path else naming.manager_socket_path()
    wire_req = dict(req)
    try:
        wire_req["token"] = naming.read_manager_token(sock_path)
    except FileNotFoundError as exc:
        if not naming.socket_is_live(sock_path):
            raise
        token_path = naming.manager_token_path(sock_path)
        raise RuntimeError(
            f"manager token is missing at {token_path}; restart the Needle runtime manager"
        ) from exc
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    try:
        s.sendall(encode(wire_req))
        with s.makefile("rb") as f:
            line = f.readline()
        if not line:
            raise ConnectionError("no response from manager")
        return decode(line)
    finally:
        s.close()


def prune(
    text: str,
    query: str = "",
    socket_path: str | Path | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    return _request({"op": "prune", "text": text, "query": query}, socket_path, timeout)


def lease(
    session: str,
    version: str = "",
    socket_path: str | Path | None = None,
    timeout: float = 5.0,
    runtime_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    req = {"op": "lease", "session": session, "version": version}
    if runtime_identity:
        req.update(runtime_identity)
    return _request(req, socket_path, timeout)


def heartbeat(
    session: str, socket_path: str | Path | None = None, timeout: float = 5.0
) -> dict[str, Any]:
    return _request({"op": "heartbeat", "session": session}, socket_path, timeout)


def release(
    session: str, socket_path: str | Path | None = None, timeout: float = 5.0
) -> dict[str, Any]:
    return _request({"op": "release", "session": session}, socket_path, timeout)


def stats(socket_path: str | Path | None = None, timeout: float = 5.0) -> dict[str, Any]:
    return _request({"op": "stats"}, socket_path, timeout)


def stop(socket_path: str | Path | None = None, timeout: float = 5.0) -> dict[str, Any]:
    return _request({"op": "stop"}, socket_path, timeout)
