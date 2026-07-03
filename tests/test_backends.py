"""Soft-LaMR policy and repair tests that do not need MLX.

Run: PYTHONPATH=python python3 tests/test_backends.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from needle_worker.soft_lamr.config import (  # noqa: E402
    CHUNK_OVERLAP_ENV_NAMES,
    MAX_BATCH_SIZE_ENV_NAMES,
    MAX_LENGTH_ENV_NAMES,
    MAX_LENGTH_RATIO_ENV_NAMES,
    MLX_CACHE_LIMIT_ENV_NAMES,
    MLX_CLEAR_CACHE_ENV_NAMES,
    MLX_LIGHT_ENV_NAMES,
    MLX_PROFILE_ENV_NAMES,
    MLX_WIRED_LIMIT_ENV_NAMES,
    PROFILE_MLX_ENV_NAMES,
    REPAIR_ENV_NAMES,
    THRESHOLD_ENV_NAMES,
    choose_mlx_max_length,
    configured_max_length,
    first_env,
    repair_enabled_for_builtin_runtime,
)
from needle_worker.soft_lamr.lines import prune_code_lines  # noqa: E402
from needle_worker.soft_lamr.repair import repair_python_mask  # noqa: E402


def test_code_pruner_env_tuples_prefer_needle_names() -> None:
    tuples = [
        REPAIR_ENV_NAMES,
        MLX_PROFILE_ENV_NAMES,
        MAX_LENGTH_ENV_NAMES,
        MLX_LIGHT_ENV_NAMES,
        PROFILE_MLX_ENV_NAMES,
        CHUNK_OVERLAP_ENV_NAMES,
        MAX_BATCH_SIZE_ENV_NAMES,
        MAX_LENGTH_RATIO_ENV_NAMES,
        MLX_CACHE_LIMIT_ENV_NAMES,
        MLX_WIRED_LIMIT_ENV_NAMES,
        MLX_CLEAR_CACHE_ENV_NAMES,
        THRESHOLD_ENV_NAMES,
    ]
    for names in tuples:
        assert names[0].startswith("NEEDLE_"), names
        assert all(not name.startswith("HAY_") for name in names), names
        assert first_env(names, {names[0]: "configured"}) == "configured"


def test_adaptive_mlx_profile_uses_2048_for_small_observations() -> None:
    assert choose_mlx_max_length(
        original_tokens=1200,
        prompt_tokens=82,
        min_code_tokens=100,
        environ={"NEEDLE_MLX_PROFILE": "local_adaptive"},
    ) == (2048, "local_adaptive")


def test_adaptive_mlx_profile_uses_1024_for_large_observations() -> None:
    assert choose_mlx_max_length(
        original_tokens=2600,
        prompt_tokens=82,
        min_code_tokens=100,
        environ={"NEEDLE_MLX_PROFILE": "local_adaptive"},
    ) == (1024, "local_adaptive")


def test_explicit_mlx_max_length_overrides_adaptive_profile() -> None:
    assert configured_max_length({"NEEDLE_MLX_MAX_LENGTH": "1536"}) == 1536
    assert configured_max_length(
        {"NEEDLE_MLX_MAX_LENGTH": "1536", "NEEDLE_MAX_LENGTH": "1024"}
    ) == 1536
    assert configured_max_length({"NEEDLE_MAX_LENGTH": "1024"}) == 1024
    assert choose_mlx_max_length(
        original_tokens=2600,
        prompt_tokens=82,
        min_code_tokens=100,
        environ={
            "NEEDLE_MLX_PROFILE": "local_adaptive",
            "NEEDLE_MLX_MAX_LENGTH": "1536",
        },
    ) == (1536, "explicit")


def test_builtin_runtime_enables_repair_by_default() -> None:
    old_env = {
        name: os.environ.get(name)
        for name in (
            "NEEDLE_REPAIR",
        )
    }
    os.environ.pop("NEEDLE_REPAIR", None)
    try:
        assert repair_enabled_for_builtin_runtime()
    finally:
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_repair_env_override_controls_builtin_default() -> None:
    old_env = {
        name: os.environ.get(name)
        for name in (
            "NEEDLE_REPAIR",
        )
    }
    try:
        os.environ["NEEDLE_REPAIR"] = "0"
        assert not repair_enabled_for_builtin_runtime()

        os.environ["NEEDLE_REPAIR"] = "1"
        assert repair_enabled_for_builtin_runtime()
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
    test_code_pruner_env_tuples_prefer_needle_names()
    test_adaptive_mlx_profile_uses_2048_for_small_observations()
    test_adaptive_mlx_profile_uses_1024_for_large_observations()
    test_explicit_mlx_max_length_overrides_adaptive_profile()
    test_builtin_runtime_enables_repair_by_default()
    test_repair_env_override_controls_builtin_default()
    test_repair_expands_enclosing_scope()
    test_plain_renderer_uses_upstream_filtered_marker()
    test_plain_renderer_keeps_tiny_gaps_when_marker_is_longer()
    print("test_backends OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
