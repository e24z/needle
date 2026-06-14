"""The only backend that exists today. Returns text unchanged: proves the pipe
works before any model or heuristic exists.

Planned siblings in this package:
  ast.py  -- model-free heuristic: query-driven line selection repaired to be
             AST-valid (the structural axis of LAMR; reuses needle's
             pruning/ast_repair.py). Real enough to ship, no ML.
  mlx.py  -- the SWE-pruner / code-pruner model on MLX. Sealed black box.
"""

from __future__ import annotations


class FakePruner:
    name = "fake"

    def prune(self, *, text: str, query: str) -> str:
        return text
