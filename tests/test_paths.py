"""Socket keying: HAY_SOCKET overrides; otherwise per-project (CLAUDE_PROJECT_DIR)
so concurrent sessions in different projects don't share a socket.
Run directly: python3 tests/test_paths.py
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _socket_path(**env):
    # Reload naming with a controlled environment.
    for k in ("HAY_SOCKET", "CLAUDE_PROJECT_DIR", "HAY_HOME"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import pruner.naming as naming

    importlib.reload(naming)
    return naming.socket_path()


def test_hay_socket_overrides() -> None:
    p = _socket_path(HAY_SOCKET="/tmp/explicit.sock", CLAUDE_PROJECT_DIR="/whatever")
    assert str(p) == "/tmp/explicit.sock"


def test_distinct_projects_get_distinct_sockets() -> None:
    a = _socket_path(HAY_HOME="/tmp/hayhome", CLAUDE_PROJECT_DIR="/repos/alpha")
    b = _socket_path(HAY_HOME="/tmp/hayhome", CLAUDE_PROJECT_DIR="/repos/beta")
    assert a != b
    assert a.parent.name == "sockets"


def test_same_project_is_stable() -> None:
    a = _socket_path(HAY_HOME="/tmp/hayhome", CLAUDE_PROJECT_DIR="/repos/alpha")
    b = _socket_path(HAY_HOME="/tmp/hayhome", CLAUDE_PROJECT_DIR="/repos/alpha")
    assert a == b


if __name__ == "__main__":
    test_hay_socket_overrides()
    test_distinct_projects_get_distinct_sockets()
    test_same_project_is_stable()
    # Leave the environment clean for any later import.
    for k in ("HAY_SOCKET", "CLAUDE_PROJECT_DIR", "HAY_HOME"):
        os.environ.pop(k, None)
    print("ok: socket keyed per-project, HAY_SOCKET overrides, stable within a project")
