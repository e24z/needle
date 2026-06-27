"""Single source of truth for the product name and where runtime files live.

The product name is deliberately confined to this one module. NEEDLE_* env vars
are canonical; HAY_* remains as a legacy compatibility alias for early installs.
"""

from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

# Product name. The ONLY place the default product name appears in runtime code.
APP_NAME = os.environ.get("NEEDLE_APP_NAME") or os.environ.get("HAY_APP_NAME", "needle")

# Built-in package aliases for legacy alpha package ids. Long-term, these aliases
# should disappear once old alpha configs and env vars no longer exist.
DEFAULT_PACKAGE_ID = "e24z/mlx-pi-soft-lamr"
PACKAGE_ALIASES = {
    "e24z/pi-local-mac": "e24z/mlx-pi-reference",
    "e24z/pi-local-mac-soft-lamr": "e24z/mlx-pi-soft-lamr",
    "e24z/mcp-bash-local": "e24z/mlx-mcp-bash-reference",
}


def canonical_package_id(package_id: str) -> str:
    """Map early alpha package ids to their current public names."""
    return PACKAGE_ALIASES.get(package_id, package_id)


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


def _iter_code_version_files(package_root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in ("runtime", "backends"):
        root = package_root / rel
        if root.exists():
            files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    registry = package_root / "registry.py"
    if registry.exists():
        files.append(registry)
    for rel in ("registry_data/packages", "registry_data/backends"):
        root = package_root / rel
        if root.exists():
            files.extend(
                path for path in root.rglob("*")
                if path.is_file() and path.suffix in {".yaml", ".json"}
            )
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
