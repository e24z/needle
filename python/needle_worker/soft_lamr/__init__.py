"""Needle's private Soft-LaMR / code-pruner relevance model package.

Self-contained package: the model (model.py), runtime config helpers
(config.py), line-mask helpers (lines.py), and structural repair layer
(repair/). The rest of Needle should only ever see
`CodePrunerBackend.prune(text, query) -> str`; how the mask is rendered (plain vs
AST-repaired) is this package's private business.

`model.py` imports mlx/numpy/transformers, so it only imports cleanly in the
worker's provisioned interpreter. This package __init__ stays import-light on
purpose: importing it (or the stdlib `repair` subpackage) must NOT drag in mlx.
"""

from __future__ import annotations
