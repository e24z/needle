"""The code-pruner backend: the real SWE-pruner / code-pruner relevance model.

Self-contained package — the model (model.py), runtime config helpers
(config.py), its line-mask helpers (lines.py), and the structural repair layer
(repair/). The rest of Needle only ever sees
`CodePrunerBackend.prune(text, query) -> str`; how the mask is rendered (plain vs
AST-repaired) is this package's private business.

`model.py` imports mlx/numpy/transformers, so it only imports cleanly in the
manager's uv-provisioned interpreter. This package __init__ stays import-light on
purpose: importing it (or the stdlib `repair` subpackage) must NOT drag in mlx.
`needle.backends.get_backend` resolves the model lazily via `code_pruner.model`, and
degrades loudly to a named fake if it can't load.
"""

from __future__ import annotations
