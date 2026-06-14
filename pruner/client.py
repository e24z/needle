"""Thin client. The PostToolUse hook will be a caller of this later; for now
the CLI and the test use it."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from . import naming
from .protocol import decode, encode


def prune(
    text: str,
    query: str = "",
    socket_path: str | Path | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    sock_path = Path(socket_path) if socket_path else naming.socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    try:
        s.sendall(encode({"text": text, "query": query}))
        with s.makefile("rb") as f:
            line = f.readline()
        if not line:
            raise ConnectionError("no response from server")
        return decode(line)
    finally:
        s.close()
