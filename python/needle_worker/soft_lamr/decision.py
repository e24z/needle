"""Decision/reason policy for one Soft-LaMR prune."""

from __future__ import annotations


def prune_decision_reason(
    *,
    original: str,
    pruned: str,
    passthrough_reason: object | None = None,
) -> tuple[str, str]:
    """Return the wire decision/reason for a completed prune.

    This preserves the historical worker contract: explicit passthrough reasons
    win, unchanged output means no lines were removed, and every changed output
    is reported as model-pruned.
    """
    if passthrough_reason:
        return "unchanged", str(passthrough_reason)
    if pruned == original:
        return "unchanged", "no-lines-removed"
    return "pruned", "model"
