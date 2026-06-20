"""Backend factory routing + the two pieces of the code-pruner ontology that
don't need the model: the LOUD degraded fallback, and the optional repair layer.

Both run under bare python3 (no mlx) on purpose — that's exactly the environment
where the real backend can't load, which is what we're pinning down.

Run: PYTHONPATH=. python3 tests/test_backends.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pruner.backends import FakePruner, _degraded, get_backend  # noqa: E402
from pruner.backends.code_pruner.lines import prune_code_lines  # noqa: E402
from pruner.backends.code_pruner.repair import repair_python_mask  # noqa: E402


def test_routing() -> None:
    assert get_backend("fake").prune(text="abcd", query="") == "abcd"
    # halve is the debug shrinker: visibly shorter, proves the replacement path
    assert len(get_backend("halve").prune(text="abcdefgh", query="")) < 8


def test_degraded_is_loud() -> None:
    """When the model can't load we pass through (fail-open for the agent) but the
    reason rides in .name (fail-loud for the operator) — never a bare 'fake'."""
    fb = _degraded(RuntimeError("No module named 'mlx'"))
    assert fb.name != FakePruner.name, "degraded backend must not look like a healthy fake"
    assert fb.name.startswith("fake (code-pruner unavailable:")
    assert "mlx" in fb.name
    assert fb.prune(text="untouched", query="q") == "untouched"  # still pass-through


def test_repair_expands_enclosing_scope() -> None:
    """Repair is an alternative render of the mask: given only an inner line, it
    pulls in the enclosing def so the output still parses."""
    code = "import os\n\n\ndef helper(x):\n    y = x + 1\n    return y\n"
    repaired = repair_python_mask(code, [5]).repaired_code  # only the body line
    assert "def helper(x):" in repaired  # enclosing signature pulled in (line 4)
    assert "y = x + 1" in repaired


def test_silent_prune_removes_line_renderer_markers() -> None:
    old = os.environ.get("HAY_SILENT_PRUNE")
    os.environ["HAY_SILENT_PRUNE"] = "1"
    try:
        code = "keep_one()\ndrop_one()\ndrop_two()\nkeep_two()\n"
        pruned, _kept = prune_code_lines(code, {1: 1.0, 4: 1.0}, 0.5)
    finally:
        if old is None:
            os.environ.pop("HAY_SILENT_PRUNE", None)
        else:
            os.environ["HAY_SILENT_PRUNE"] = old
    assert "[pruned" not in pruned
    assert "drop_one" not in pruned
    assert "drop_two" not in pruned
    assert "keep_one" in pruned
    assert "keep_two" in pruned


def test_silent_prune_removes_repair_renderer_markers() -> None:
    old = os.environ.get("HAY_SILENT_PRUNE")
    os.environ["HAY_SILENT_PRUNE"] = "1"
    try:
        code = "import os\n\n\ndef helper(x):\n    y = x + 1\n    return y\n"
        repaired = repair_python_mask(code, [5]).repaired_code
    finally:
        if old is None:
            os.environ.pop("HAY_SILENT_PRUNE", None)
        else:
            os.environ["HAY_SILENT_PRUNE"] = old
    assert "[pruned" not in repaired
    assert "def helper(x):" in repaired
    assert "y = x + 1" in repaired


def main() -> int:
    test_routing()
    test_degraded_is_loud()
    test_repair_expands_enclosing_scope()
    test_silent_prune_removes_line_renderer_markers()
    test_silent_prune_removes_repair_renderer_markers()
    print("test_backends OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
