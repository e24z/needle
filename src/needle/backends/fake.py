"""The pass-through backend: returns text unchanged. Proves the pipe works
without a model, and is the fail-open fallback (see backends.get_backend ->
_degraded) when the real model can't load.

Siblings: debug.py (halve — the debug shrinker) and code_pruner/ (the real
SWE-pruner / code-pruner model, sealed behind prune(text, query) -> str, with
optional structural repair under code_pruner/repair/). Selected by name via
get_backend / NEEDLE_BACKEND.
"""

from __future__ import annotations


class FakePruner:
    name = "fake"

    def prune(self, *, text: str, query: str) -> str:
        return text
