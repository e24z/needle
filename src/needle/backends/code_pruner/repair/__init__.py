"""Optional structural repair for the code-pruner mask.

NOT a str->str post-processor: repair is an alternative *renderer* of the model's
kept-line mask (it needs the line numbers, not the rendered string), so it lives
inside the backend. The thin-spine product enables this Soft-LaMR behavior by
default. `NEEDLE_REPAIR` is canonical; legacy `HAY_REPAIR` remains a
compatibility override for experiments.

Python only today (`python.py`); other languages would add siblings here.
"""

from __future__ import annotations

from .python import RepairResult, repair_python_mask

__all__ = ["RepairResult", "repair_python_mask"]
