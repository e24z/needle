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
from pruner.backends.code_pruner.config import (  # noqa: E402
    repair_enabled_for_active_package,
    repair_enabled_for_capabilities,
)
from pruner.backends.code_pruner.lines import prune_code_lines  # noqa: E402
from pruner.backends.code_pruner.repair import repair_python_mask  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent


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


def test_reference_capability_leaves_repair_off() -> None:
    assert not repair_enabled_for_capabilities(
        ["swe-pruner/reference"],
        {},
    )


def test_soft_lamr_capability_opts_into_repair() -> None:
    assert repair_enabled_for_capabilities(
        ["swe-pruner/reference", "e24z/soft-lamr"],
        {},
    )


def test_repair_env_override_wins() -> None:
    assert repair_enabled_for_capabilities(
        ["swe-pruner/reference"],
        {"HAY_REPAIR": "1"},
    )
    assert not repair_enabled_for_capabilities(
        ["e24z/soft-lamr"],
        {"HAY_REPAIR": "0"},
    )
    assert repair_enabled_for_capabilities(
        ["swe-pruner/reference"],
        {"NEEDLE_REPAIR": "true"},
    )
    assert not repair_enabled_for_capabilities(
        ["e24z/soft-lamr"],
        {"NEEDLE_REPAIR": "false"},
    )


def test_default_active_package_is_reference_without_repair() -> None:
    old_env = {
        name: os.environ.get(name)
        for name in (
            "HAY_PACKAGE",
            "NEEDLE_PACKAGE",
            "HAY_REGISTRY_ROOT",
            "NEEDLE_REGISTRY_ROOT",
            "HAY_REPAIR",
            "NEEDLE_REPAIR",
        )
    }
    os.environ["HAY_REGISTRY_ROOT"] = str(ROOT)
    os.environ["HAY_PACKAGE"] = "e24z/pi-local-mac"
    os.environ.pop("NEEDLE_PACKAGE", None)
    os.environ.pop("NEEDLE_REGISTRY_ROOT", None)
    os.environ.pop("HAY_REPAIR", None)
    os.environ.pop("NEEDLE_REPAIR", None)
    try:
        assert not repair_enabled_for_active_package()
    finally:
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_soft_lamr_active_package_enables_repair() -> None:
    old_env = {
        name: os.environ.get(name)
        for name in (
            "HAY_PACKAGE",
            "NEEDLE_PACKAGE",
            "HAY_REGISTRY_ROOT",
            "NEEDLE_REGISTRY_ROOT",
            "HAY_REPAIR",
            "NEEDLE_REPAIR",
        )
    }
    os.environ["HAY_REGISTRY_ROOT"] = str(ROOT)
    os.environ["HAY_PACKAGE"] = "e24z/pi-local-mac-soft-lamr"
    os.environ.pop("NEEDLE_PACKAGE", None)
    os.environ.pop("NEEDLE_REGISTRY_ROOT", None)
    os.environ.pop("HAY_REPAIR", None)
    os.environ.pop("NEEDLE_REPAIR", None)
    try:
        assert repair_enabled_for_active_package()
    finally:
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_repair_expands_enclosing_scope() -> None:
    """Repair is an alternative render of the mask: given only an inner line, it
    pulls in the enclosing def so the output still parses."""
    code = "import os\n\n\ndef helper(x):\n    y = x + 1\n    return y\n"
    repaired = repair_python_mask(code, [5]).repaired_code  # only the body line
    assert "def helper(x):" in repaired  # enclosing signature pulled in (line 4)
    assert "y = x + 1" in repaired


def test_plain_renderer_uses_upstream_filtered_marker() -> None:
    code = "keep\nfiltered one\nfiltered two\nkeep again\n"
    pruned, kept = prune_code_lines(code, {1: 1.0, 4: 1.0}, 0.5)
    assert kept == [1, 4]
    assert pruned == "keep\n(filtered 2 lines)\nkeep again"


def test_plain_renderer_keeps_tiny_gaps_when_marker_is_longer() -> None:
    code = "keep\nx\ny\nkeep again\n"
    pruned, kept = prune_code_lines(code, {1: 1.0, 4: 1.0}, 0.5)
    assert kept == [1, 4]
    assert pruned == code.rstrip("\n")


def main() -> int:
    test_routing()
    test_degraded_is_loud()
    test_reference_capability_leaves_repair_off()
    test_soft_lamr_capability_opts_into_repair()
    test_repair_env_override_wins()
    test_default_active_package_is_reference_without_repair()
    test_soft_lamr_active_package_enables_repair()
    test_repair_expands_enclosing_scope()
    test_plain_renderer_uses_upstream_filtered_marker()
    test_plain_renderer_keeps_tiny_gaps_when_marker_is_longer()
    print("test_backends OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
