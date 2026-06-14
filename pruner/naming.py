"""Single source of truth for the codename and where runtime files live.

The product name is deliberately confined to this one module. Rename the
project by changing APP_NAME here (or setting HAY_APP_NAME); paths, the socket,
and CLI display all follow. Do not hardcode "hay" anywhere else.
"""

from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

# Codename. The ONLY place the product name appears in code.
APP_NAME = os.environ.get("HAY_APP_NAME", "hay")


def app_home() -> Path:
    """Directory for runtime state (socket, logs later). Override with HAY_HOME."""
    env = os.environ.get("HAY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / f".{APP_NAME}"


def socket_path() -> Path:
    """Unix domain socket the server listens on.

    Keyed by CLAUDE_PROJECT_DIR so concurrent sessions in different projects get
    distinct sockets and never clobber each other's daemon. Both the server (run
    by the monitor) and the hook see the same CLAUDE_PROJECT_DIR, so they agree.
    HAY_SOCKET overrides everything (tests, manual runs)."""
    env = os.environ.get("HAY_SOCKET")
    if env:
        return Path(env).expanduser()
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        key = hashlib.sha1(project.encode("utf-8")).hexdigest()[:12]
        return app_home() / "sockets" / f"{APP_NAME}-{key}.sock"
    return app_home() / f"{APP_NAME}.sock"


def manager_socket_path() -> Path:
    """The machine-wide manager socket. One per machine (per HAY_HOME), NOT keyed
    by project: the whole point of the manager is a single resident model shared
    by every session. HAY_MANAGER_SOCKET overrides (tests, manual runs)."""
    env = os.environ.get("HAY_MANAGER_SOCKET")
    if env:
        return Path(env).expanduser()
    return app_home() / "manager.sock"


def socket_is_live(path: Path) -> bool:
    """True if something is already accepting connections on this unix socket.
    The basis for first-writer-wins binding: a later starter defers to it."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()
