"""DEBUG-ONLY backends. Never for production pruning -- they exist to exercise
the replacement path end-to-end before a real model arrives."""

from __future__ import annotations


class HalvePruner:
    """Keeps the first half of the text. Deterministic, no ML -- used to prove
    that real savings produce a correct `updatedToolOutput`."""

    name = "halve"

    def prune(self, *, text: str, query: str) -> str:
        return text[: len(text) // 2]
