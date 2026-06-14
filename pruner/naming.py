"""Single source of truth for the codename and where runtime files live.

The product name is deliberately confined to this one module. Rename the
project by changing APP_NAME here (or setting HAY_APP_NAME); paths, the socket,
and CLI display all follow. Do not hardcode "hay" anywhere else.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

# Codename. The ONLY place the product name appears in code.
APP_NAME = os.environ.get("HAY_APP_NAME", "hay")


def app_home() -> Path:
    """Directory for runtime state (socket, logs). Override with HAY_HOME."""
    env = os.environ.get("HAY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / f".{APP_NAME}"


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
