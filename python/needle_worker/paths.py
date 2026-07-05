"""Needle-owned local paths used by the private Python worker.

Rust owns the product layout and passes NEEDLE_HOME to managed worker children.
Python intentionally has no platform-default branch here: standalone/dev runs
must set NEEDLE_HOME explicitly so layout drift fails loudly.
"""

from __future__ import annotations

import os
from pathlib import Path


def app_home() -> Path:
    env = os.environ.get("NEEDLE_HOME")
    if env:
        return Path(env).expanduser()
    raise RuntimeError("NEEDLE_HOME is required; the Rust needle binary owns path layout")


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
