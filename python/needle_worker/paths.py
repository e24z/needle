"""Needle-owned local paths used by the private Python worker.

Rust owns the product layout and should pass explicit directories during normal
operation. These defaults keep local worker runs predictable without importing
the old Python runtime package.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Needle"


def app_home() -> Path:
    env = os.environ.get("NEEDLE_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".local" / "share" / "needle"


def model_root() -> Path:
    env = os.environ.get("NEEDLE_MODEL_ROOT")
    if env:
        return Path(env).expanduser()
    return app_home() / "models"


def model_dir_for_repo(repo: str) -> Path:
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "-"
        for ch in repo.replace("/", "--")
    ).strip("-")
    return model_root() / (safe or "model")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "getuid") and path.exists() and path.stat().st_uid != os.getuid():
        raise PermissionError(f"directory is not owned by the current user: {path}")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def ensure_runtime_parent(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    if hasattr(os, "getuid") and path.exists() and path.stat().st_uid != os.getuid():
        raise PermissionError(f"directory is not owned by the current user: {path}")


def write_private_text(path: Path, text: str) -> None:
    ensure_runtime_parent(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "getuid") and os.fstat(fd).st_uid != os.getuid():
            raise PermissionError(f"file is not owned by the current user: {path}")
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
    except Exception:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
