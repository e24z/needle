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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


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
        "NEEDLE_MANAGER_TOKEN_FILE",
        "HAY_MANAGER_TOKEN_FILE",
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
    assert naming.manager_token_path() == Path.home() / ".needle/manager.token"


def test_hay_env_names_remain_compatibility_aliases() -> None:
    naming = _reload_naming(HAY_MANAGER_SOCKET="/tmp/legacy-manager.sock")
    assert str(naming.manager_socket_path()) == "/tmp/legacy-manager.sock"

    naming = _reload_naming(HAY_HOME="/tmp/hayhome")
    assert naming.app_home() == Path("/tmp/hayhome")
    assert naming.model_root() == Path("/tmp/hayhome/models")


def test_socket_is_live_false_when_absent() -> None:
    naming = _reload_naming(NEEDLE_HOME=tempfile.mkdtemp())
    assert naming.socket_is_live(naming.manager_socket_path()) is False


def test_manager_token_is_private_and_socket_scoped() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        naming = _reload_naming(NEEDLE_HOME=str(home))
        token = naming.get_or_create_manager_token()
        token_path = home / "manager.token"
        assert token == token_path.read_text(encoding="utf-8").strip()
        assert token_path.stat().st_mode & 0o777 == 0o600
        assert home.stat().st_mode & 0o777 == 0o700

        explicit = Path(td) / "explicit.sock"
        assert naming.manager_token_path(explicit) == Path(td) / "explicit.sock.token"
        explicit_token = naming.get_or_create_manager_token(explicit)
        assert explicit_token == naming.read_manager_token(explicit)


def test_private_runtime_helpers_repair_permissive_modes() -> None:
    old_umask = os.umask(0)
    try:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir(mode=0o777)
            os.chmod(home, 0o777)
            naming = _reload_naming(NEEDLE_HOME=str(home))

            log_path = home / "manager.log"
            log_path.write_text("", encoding="utf-8")
            os.chmod(log_path, 0o666)
            with naming.open_private_append(log_path) as fh:
                fh.write("hello\n")

            assert home.stat().st_mode & 0o777 == 0o700
            assert log_path.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_umask)


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
    test_manager_token_is_private_and_socket_scoped()
    test_private_runtime_helpers_repair_permissive_modes()
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
        "NEEDLE_MANAGER_TOKEN_FILE",
        "HAY_MANAGER_TOKEN_FILE",
    ):
        os.environ.pop(k, None)
    print("ok: manager socket overridable, machine-wide, liveness probe works")
