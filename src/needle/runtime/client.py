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
    except FileNotFoundError:
        wire_req["token"] = naming.get_or_create_manager_token(sock_path)
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
) -> dict[str, Any]:
    return _request(
        {"op": "lease", "session": session, "version": version}, socket_path, timeout
    )


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
