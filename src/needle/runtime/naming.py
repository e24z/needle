"""Single source of truth for the product name and where runtime files live.

The product name is deliberately confined to this one module. NEEDLE_* env vars
are canonical; HAY_* remains as a legacy compatibility alias for early installs.
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import socket
from pathlib import Path

# Product name. The ONLY place the default product name appears in runtime code.
APP_NAME = os.environ.get("NEEDLE_APP_NAME") or os.environ.get("HAY_APP_NAME", "needle")

def app_home() -> Path:
    """Directory for runtime state (socket, logs). Override with NEEDLE_HOME."""
    env = os.environ.get("NEEDLE_HOME") or os.environ.get("HAY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / f".{APP_NAME}"


def model_root() -> Path:
    """Directory for Needle-owned model files. Override with NEEDLE_MODEL_ROOT."""
    env = os.environ.get("NEEDLE_MODEL_ROOT") or os.environ.get("HAY_MODEL_ROOT")
    if env:
        return Path(env).expanduser()
    return app_home() / "models"


def model_dir_for_repo(repo: str) -> Path:
    """Stable local directory for a Hugging Face repo under Needle's model root."""
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "-"
        for ch in repo.replace("/", "--")
    ).strip("-")
    return model_root() / (safe or "model")


def manager_socket_path() -> Path:
    """The machine-wide manager socket. One per machine (per NEEDLE_HOME), NOT keyed
    by project: the whole point of the manager is a single resident model shared
    by every session. NEEDLE_MANAGER_SOCKET overrides (tests, manual runs)."""
    env = os.environ.get("NEEDLE_MANAGER_SOCKET") or os.environ.get("HAY_MANAGER_SOCKET")
    if env:
        return Path(env).expanduser()
    return app_home() / "manager.sock"


def manager_token_path(socket_path: str | Path | None = None) -> Path:
    """Path for the per-install manager capability token.

    The default token lives beside the default socket under NEEDLE_HOME. Tests
    and manual runs that override the socket get an adjacent token file instead
    of touching the user's real runtime directory.
    """
    env = os.environ.get("NEEDLE_MANAGER_TOKEN_FILE") or os.environ.get("HAY_MANAGER_TOKEN_FILE")
    if env:
        return Path(env).expanduser()
    sock = Path(socket_path).expanduser() if socket_path is not None else manager_socket_path()
    if sock == app_home() / "manager.sock":
        return app_home() / "manager.token"
    return sock.with_name(f"{sock.name}.token")


def _is_owned_by_current_user(path: Path) -> bool:
    if not hasattr(os, "getuid"):
        return True
    try:
        return path.stat().st_uid == os.getuid()
    except OSError:
        return False


def ensure_private_dir(path: Path) -> None:
    """Create/chmod a Needle-owned runtime directory to user-only access."""
    # Centralized runtime mkdir; callers must route runtime state through this helper.
    path.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "getuid") and path.exists() and path.stat().st_uid != os.getuid():
        raise PermissionError(f"runtime directory is not owned by the current user: {path}")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def ensure_runtime_parent(path: Path) -> None:
    """Create a runtime parent without changing permissions on shared dirs."""
    if path == app_home():
        ensure_private_dir(path)
        return
    existed = path.exists()
    # Centralized runtime mkdir; callers must route runtime state through this helper.
    path.mkdir(parents=True, exist_ok=True)
    if not existed:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    if hasattr(os, "getuid") and path.exists() and path.stat().st_uid != os.getuid():
        raise PermissionError(f"runtime directory is not owned by the current user: {path}")


def _validate_owned_file(path: Path) -> None:
    if hasattr(os, "getuid") and path.exists() and path.stat().st_uid != os.getuid():
        raise PermissionError(f"runtime file is not owned by the current user: {path}")


def ensure_private_file(path: Path, mode: int = 0o600) -> None:
    """Create or repair a current-user runtime file to user-only access."""
    ensure_runtime_parent(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        _validate_owned_file(path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        return
    with os.fdopen(fd, "w", encoding="utf-8"):
        pass


def open_private_append(path: Path) -> io.TextIOWrapper:
    """Open a current-user runtime file for append with mode 0600."""
    ensure_runtime_parent(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if hasattr(os, "getuid") and os.fstat(fd).st_uid != os.getuid():
            raise PermissionError(f"runtime file is not owned by the current user: {path}")
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        return os.fdopen(fd, "a", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def write_private_text(path: Path, text: str) -> None:
    """Write a current-user local state file with mode 0600."""
    ensure_runtime_parent(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "getuid") and os.fstat(fd).st_uid != os.getuid():
            raise PermissionError(f"runtime file is not owned by the current user: {path}")
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    with fh:
        fh.write(text)


def read_manager_token(socket_path: str | Path | None = None) -> str:
    path = manager_token_path(socket_path)
    if not path.exists():
        raise FileNotFoundError(path)
    if not _is_owned_by_current_user(path):
        raise PermissionError(f"manager token is not owned by the current user: {path}")
    _validate_owned_file(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise PermissionError(f"manager token is empty: {path}")
    return token


def get_or_create_manager_token(socket_path: str | Path | None = None) -> str:
    """Return a stable random capability token stored mode 0600."""
    path = manager_token_path(socket_path)
    ensure_runtime_parent(path.parent)
    if path.exists():
        token = read_manager_token(socket_path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return token

    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return token


def socket_owner_is_current_user(path: Path) -> bool:
    """True when an existing socket path is safe to contact as this user."""
    return _is_owned_by_current_user(path)


def socket_is_live(path: Path) -> bool:
    """True if something is already accepting connections on this unix socket.
    The basis for first-writer-wins binding: a later starter defers to it."""
    if path.exists() and not socket_owner_is_current_user(path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _iter_code_version_files(package_root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in ("runtime", "backends", "hosts/mcp"):
        root = package_root / rel
        if root.exists():
            files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files, key=lambda path: path.relative_to(package_root).as_posix())


def code_version(package_root: Path | None = None) -> str:
    """Short hash of the runtime package source. A detached manager records the
    version it started on; a session announces its version when it leases. A
    mismatch means the code was edited since the manager launched -- so the old
    manager steps aside and a fresh one starts on the new code. Without this, a
    long-lived manager would silently keep running stale code after an edit."""
    pkg = package_root or Path(__file__).resolve().parent.parent
    h = hashlib.sha1()
    for p in _iter_code_version_files(pkg):
        h.update(p.relative_to(pkg).as_posix().encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]
