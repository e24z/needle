"""Manager socket naming: NEEDLE_MANAGER_SOCKET overrides; otherwise a single
machine-wide socket under NEEDLE_HOME (NOT keyed by project, unlike the old
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
    for k in (
        "NEEDLE_APP_NAME",
        "HAY_APP_NAME",
        "NEEDLE_MANAGER_SOCKET",
        "HAY_MANAGER_SOCKET",
        "CLAUDE_PROJECT_DIR",
        "NEEDLE_HOME",
        "HAY_HOME",
        "NEEDLE_MODEL_ROOT",
        "HAY_MODEL_ROOT",
    ):
        os.environ.pop(k, None)
    os.environ.update(env)
    import needle.runtime.naming as naming

    importlib.reload(naming)
    return naming


def test_manager_socket_overrides() -> None:
    naming = _reload_naming(NEEDLE_MANAGER_SOCKET="/tmp/explicit-manager.sock")
    assert str(naming.manager_socket_path()) == "/tmp/explicit-manager.sock"


def test_manager_socket_is_machine_wide() -> None:
    # Same socket regardless of project: one resident model per machine.
    a = _reload_naming(NEEDLE_HOME="/tmp/needlehome", CLAUDE_PROJECT_DIR="/repos/alpha").manager_socket_path()
    b = _reload_naming(NEEDLE_HOME="/tmp/needlehome", CLAUDE_PROJECT_DIR="/repos/beta").manager_socket_path()
    assert a == b
    assert a.name == "manager.sock"


def test_default_runtime_state_is_needle_owned() -> None:
    naming = _reload_naming()
    assert naming.APP_NAME == "needle"
    assert naming.app_home() == Path.home() / ".needle"
    assert naming.model_root() == Path.home() / ".needle/models"
    assert naming.manager_socket_path() == Path.home() / ".needle/manager.sock"


def test_hay_env_names_remain_compatibility_aliases() -> None:
    naming = _reload_naming(HAY_MANAGER_SOCKET="/tmp/legacy-manager.sock")
    assert str(naming.manager_socket_path()) == "/tmp/legacy-manager.sock"

    naming = _reload_naming(HAY_HOME="/tmp/hayhome")
    assert naming.app_home() == Path("/tmp/hayhome")
    assert naming.model_root() == Path("/tmp/hayhome/models")


def test_socket_is_live_false_when_absent() -> None:
    naming = _reload_naming(NEEDLE_HOME=tempfile.mkdtemp())
    assert naming.socket_is_live(naming.manager_socket_path()) is False


def test_model_dir_is_needle_owned_and_sanitized() -> None:
    naming = _reload_naming(NEEDLE_HOME="/tmp/needlehome")
    assert naming.model_root() == Path("/tmp/needlehome/models")
    assert naming.model_dir_for_repo("ayanami-kitasan/code-pruner") == Path(
        "/tmp/needlehome/models/ayanami-kitasan--code-pruner"
    )

    naming = _reload_naming(NEEDLE_MODEL_ROOT="/tmp/needle-models")
    assert naming.model_dir_for_repo("org/model:v1") == Path("/tmp/needle-models/org--model-v1")


if __name__ == "__main__":
    test_manager_socket_overrides()
    test_manager_socket_is_machine_wide()
    test_default_runtime_state_is_needle_owned()
    test_hay_env_names_remain_compatibility_aliases()
    test_socket_is_live_false_when_absent()
    test_model_dir_is_needle_owned_and_sanitized()
    for k in (
        "NEEDLE_APP_NAME",
        "HAY_APP_NAME",
        "NEEDLE_MANAGER_SOCKET",
        "HAY_MANAGER_SOCKET",
        "CLAUDE_PROJECT_DIR",
        "NEEDLE_HOME",
        "HAY_HOME",
        "NEEDLE_MODEL_ROOT",
        "HAY_MODEL_ROOT",
    ):
        os.environ.pop(k, None)
    print("ok: manager socket overridable, machine-wide, liveness probe works")
