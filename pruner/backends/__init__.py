"""Pruner backends. Import the contract and the fake; real backends register here."""

from __future__ import annotations

import os

from .base import PrunerBackend
from .fake import FakePruner

__all__ = ["PrunerBackend", "FakePruner", "get_backend"]


def get_backend(name: str | None = None) -> PrunerBackend:
    """Resolve a backend by name (or HAY_BACKEND env, default 'fake')."""
    name = (name or os.environ.get("HAY_BACKEND") or "fake").lower()
    if name == "halve":
        from .debug import HalvePruner

        return HalvePruner()
    if name == "mlx":
        try:
            from .mlx import MLXBackend

            return MLXBackend()
        except Exception as exc:  # mlx not installed / model unavailable
            import sys

            from ..naming import APP_NAME

            print(f"{APP_NAME}: mlx backend unavailable ({exc}); using fake", file=sys.stderr)
            return FakePruner()
    return FakePruner()
