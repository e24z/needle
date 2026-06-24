"""Pruner backends. Import the contract and the fake; real backends register here."""

from __future__ import annotations

import os

from .base import PrunerBackend
from .fake import FakePruner

__all__ = ["PrunerBackend", "FakePruner", "get_backend", "is_code_pruner_backend_name"]

CODE_PRUNER_BACKEND_NAMES = {
    "e24z/code-pruner-mlx",
    "code-pruner-mlx",
    "code-pruner",
    "code_pruner",
}


def _configured_backend_name(name: str | None = None) -> str:
    return (
        name
        or os.environ.get("NEEDLE_BACKEND")
        or os.environ.get("HAY_BACKEND")
        or "e24z/code-pruner-mlx"
    ).lower()


def is_code_pruner_backend_name(name: str | None = None) -> bool:
    return _configured_backend_name(name) in CODE_PRUNER_BACKEND_NAMES


def get_backend(name: str | None = None) -> PrunerBackend:
    """Resolve a backend by canonical id or legacy env alias."""
    name = _configured_backend_name(name)
    if name == "halve":
        from .debug import HalvePruner

        return HalvePruner()
    if name in CODE_PRUNER_BACKEND_NAMES:
        try:
            from .code_pruner.model import CodePrunerBackend

            return CodePrunerBackend()
        except Exception as exc:  # deps/model unavailable: degrade, but LOUDLY
            return _degraded(exc)
    return FakePruner()


def _degraded(exc: Exception) -> PrunerBackend:
    """Pass-through fallback when the real model can't load. Unlike a plain fake,
    it REPORTS why: the reason rides in `.name`, so stats/statusline/logs show a
    distinct degraded state instead of a healthy-looking 'fake'. Fail-open for
    the agent (text passes through), fail-loud for the operator."""
    import sys

    from ..naming import APP_NAME

    reason = str(exc).strip().splitlines()[0][:120] or exc.__class__.__name__
    print(f"{APP_NAME}: code-pruner unavailable ({reason}); using fake", file=sys.stderr)
    fb = FakePruner()
    fb.name = f"fake (code-pruner unavailable: {reason})"  # honest, not just 'fake'
    return fb
