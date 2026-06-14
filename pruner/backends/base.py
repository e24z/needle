"""The pruner contract. Everything above this line is plumbing the user owns;
everything a real backend hides is ML the user does not need to learn to ship."""

from __future__ import annotations

from typing import Protocol


class PrunerBackend(Protocol):
    def prune(self, *, text: str, query: str) -> str:
        """Return a relevance-pruned version of `text` for `query`, or `text`
        unchanged when there is nothing worth removing."""
        ...
