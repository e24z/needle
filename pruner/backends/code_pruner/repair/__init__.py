"""Optional structural repair for the code-pruner mask.

NOT a str->str post-processor: repair is an alternative *renderer* of the model's
kept-line mask (it needs the line numbers, not the rendered string), so it lives
inside the backend and is applied only when `HAY_REPAIR` is set. Off by default —
the model is meant to stand on its own; this is the eval-toggleable structural
axis (LAMR-style) for when raw output is too gappy to parse.

Python only today (`python.py`); other languages would add siblings here.
"""

from __future__ import annotations

from .python import RepairResult, repair_python_mask

__all__ = ["RepairResult", "repair_python_mask"]
