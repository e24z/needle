"""Manager socket naming: HAY_MANAGER_SOCKET overrides; otherwise a single
machine-wide socket under HAY_HOME (NOT keyed by project, unlike the old
per-project server). Run directly: python3 tests/test_paths.py
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _reload_naming(**env):
    for k in ("HAY_MANAGER_SOCKET", "CLAUDE_PROJECT_DIR", "HAY_HOME"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import pruner.naming as naming

    importlib.reload(naming)
    return naming


def test_manager_socket_overrides() -> None:
    naming = _reload_naming(HAY_MANAGER_SOCKET="/tmp/explicit-manager.sock")
    assert str(naming.manager_socket_path()) == "/tmp/explicit-manager.sock"


def test_manager_socket_is_machine_wide() -> None:
    # Same socket regardless of project: one resident model per machine.
    a = _reload_naming(HAY_HOME="/tmp/hayhome", CLAUDE_PROJECT_DIR="/repos/alpha").manager_socket_path()
    b = _reload_naming(HAY_HOME="/tmp/hayhome", CLAUDE_PROJECT_DIR="/repos/beta").manager_socket_path()
    assert a == b
    assert a.name == "manager.sock"


def test_socket_is_live_false_when_absent() -> None:
    naming = _reload_naming(HAY_HOME=tempfile.mkdtemp())
    assert naming.socket_is_live(naming.manager_socket_path()) is False


if __name__ == "__main__":
    test_manager_socket_overrides()
    test_manager_socket_is_machine_wide()
    test_socket_is_live_false_when_absent()
    for k in ("HAY_MANAGER_SOCKET", "CLAUDE_PROJECT_DIR", "HAY_HOME"):
        os.environ.pop(k, None)
    print("ok: manager socket overridable, machine-wide, liveness probe works")
