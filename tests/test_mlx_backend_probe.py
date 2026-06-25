"""Probe CLI env wiring stays on canonical NEEDLE_* names first.

Run: PYTHONPATH=. python3 tests/test_mlx_backend_probe.py
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import mlx_backend_probe  # noqa: E402


def _restore_env(old: dict[str, str | None]) -> None:
    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_probe_writes_canonical_env_names() -> None:
    names = (
        "NEEDLE_MLX_MAX_LENGTH",
        "HAY_MAX_LENGTH",
        "NEEDLE_PROFILE_MLX",
        "HAY_PROFILE_MLX",
    )
    old = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        mlx_backend_probe._configure_env(max_length=1536)
        assert os.environ["NEEDLE_MLX_MAX_LENGTH"] == "1536"
        assert os.environ["NEEDLE_PROFILE_MLX"] == "1"
        assert "HAY_MAX_LENGTH" not in os.environ
        assert "HAY_PROFILE_MLX" not in os.environ
    finally:
        _restore_env(old)


def test_probe_reads_needle_batch_size_before_legacy_hay_alias() -> None:
    names = ("NEEDLE_MLX_MAX_BATCH_SIZE", "HAY_MLX_MAX_BATCH_SIZE")
    old = {name: os.environ.get(name) for name in names}
    try:
        os.environ["HAY_MLX_MAX_BATCH_SIZE"] = "2"
        os.environ["NEEDLE_MLX_MAX_BATCH_SIZE"] = "4"
        assert mlx_backend_probe._batch_sizes(None) == [4]
        os.environ.pop("NEEDLE_MLX_MAX_BATCH_SIZE")
        assert mlx_backend_probe._batch_sizes(None) == [2]
    finally:
        _restore_env(old)


def main() -> int:
    test_probe_writes_canonical_env_names()
    test_probe_reads_needle_batch_size_before_legacy_hay_alias()
    print("test_mlx_backend_probe OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
