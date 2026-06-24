"""Optional structural repair for the code-pruner mask.

NOT a str->str post-processor: repair is an alternative *renderer* of the model's
kept-line mask (it needs the line numbers, not the rendered string), so it lives
inside the backend. The active package capability controls the default:
`swe-pruner/reference` keeps it off, while `e24z/soft-lamr` opts in. `HAY_REPAIR`
or `NEEDLE_REPAIR` can still override that for experiments.

Python only today (`python.py`); other languages would add siblings here.
"""

from __future__ import annotations

from .python import RepairResult, repair_python_mask

__all__ = ["RepairResult", "repair_python_mask"]
