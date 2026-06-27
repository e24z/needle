"""Runtime policy knobs for the code-pruner backend.

This module must stay import-light: tests import it under plain python3 on
machines with no MLX runtime.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping


REFERENCE_CAPABILITY = "swe-pruner/reference"
SOFT_LAMR_CAPABILITY = "e24z/soft-lamr"
# NEEDLE_* names are canonical; HAY_* entries remain legacy compatibility
# aliases for early installs and local experiment scripts.
REPAIR_ENV_NAMES = ("NEEDLE_REPAIR", "HAY_REPAIR")
MLX_PROFILE_ENV_NAMES = ("NEEDLE_MLX_PROFILE", "HAY_MLX_PROFILE")
MAX_LENGTH_ENV_NAMES = ("NEEDLE_MLX_MAX_LENGTH", "NEEDLE_MAX_LENGTH", "HAY_MAX_LENGTH")
MLX_LIGHT_ENV_NAMES = ("NEEDLE_MLX_LIGHT", "HAY_MLX_LIGHT")
PROFILE_MLX_ENV_NAMES = ("NEEDLE_PROFILE_MLX", "HAY_PROFILE_MLX")
CHUNK_OVERLAP_ENV_NAMES = ("NEEDLE_CHUNK_OVERLAP_TOKENS", "HAY_CHUNK_OVERLAP_TOKENS")
MAX_BATCH_SIZE_ENV_NAMES = ("NEEDLE_MLX_MAX_BATCH_SIZE", "HAY_MLX_MAX_BATCH_SIZE")
MAX_BATCH_TOKENS_ENV_NAMES = ("NEEDLE_MLX_MAX_BATCH_TOKENS", "HAY_MLX_MAX_BATCH_TOKENS")
MAX_LENGTH_RATIO_ENV_NAMES = ("NEEDLE_MLX_MAX_LENGTH_RATIO", "HAY_MLX_MAX_LENGTH_RATIO")
MLX_CACHE_LIMIT_ENV_NAMES = ("NEEDLE_MLX_CACHE_LIMIT_MB", "HAY_MLX_CACHE_LIMIT_MB")
MLX_WIRED_LIMIT_ENV_NAMES = ("NEEDLE_MLX_WIRED_LIMIT_MB", "HAY_MLX_WIRED_LIMIT_MB")
MLX_CLEAR_CACHE_ENV_NAMES = (
    "NEEDLE_MLX_CLEAR_CACHE_AFTER_PRUNE",
    "HAY_MLX_CLEAR_CACHE_AFTER_PRUNE",
)
THRESHOLD_ENV_NAMES = ("NEEDLE_THRESHOLD", "HAY_THRESHOLD")
ADAPTIVE_MLX_PROFILES = {"local_adaptive", "local-mlx-adaptive", "local_mlx_adaptive"}


def repair_enabled_for_active_package() -> bool:
    """Return whether the active package should apply AST repair.

    Explicit env flags win for local experiments. Otherwise the package's named
    capability decides: SWE-Pruner reference behavior is repair-free, while the
    Soft-LaMR capability opts into Python AST mask expansion.
    """
    return repair_enabled_for_capabilities(_active_capability_ids(), os.environ)


def repair_enabled_for_capabilities(
    capability_ids: Iterable[str],
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    explicit = _first_env(REPAIR_ENV_NAMES, env)
    if explicit is not None:
        return _parse_bool(explicit)
    return SOFT_LAMR_CAPABILITY in set(capability_ids)


def configured_max_length(environ: Mapping[str, str] | None = None) -> int | None:
    """Explicit max-length override for local experiments.

    Package runtime profiles should not set this. They should set
    NEEDLE_MLX_PROFILE so the backend can choose per input.
    """
    env = os.environ if environ is None else environ
    value = _first_env(MAX_LENGTH_ENV_NAMES, env)
    if value is None:
        return None
    return _parse_positive_int(value, "max length")


def active_mlx_profile(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return (_first_env(MLX_PROFILE_ENV_NAMES, env) or "").strip().lower()


def configured_max_batch_tokens(environ: Mapping[str, str] | None = None) -> int | None:
    env = os.environ if environ is None else environ
    value = _first_env(MAX_BATCH_TOKENS_ENV_NAMES, env)
    if value is None:
        return None
    return _parse_positive_int(value, "max batch tokens")


def first_env(
    names: tuple[str, ...],
    environ: Mapping[str, str] | None = None,
    default: str | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    value = _first_env(names, env)
    return default if value is None else value


def choose_mlx_max_length(
    *,
    original_tokens: int,
    prompt_tokens: int,
    min_code_tokens: int,
    default_max_length: int = 4096,
    environ: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """Choose the sequence length for one prune request.

    This is a runtime profile, not a capability rule: it changes local MLX
    performance shape while leaving the text->text pruning contract intact.
    """
    env = os.environ if environ is None else environ
    explicit = configured_max_length(env)
    if explicit is not None:
        return max(explicit, prompt_tokens + min_code_tokens), "explicit"

    profile = active_mlx_profile(env)
    if profile not in ADAPTIVE_MLX_PROFILES:
        return default_max_length, "fixed-default"

    single_chunk_until = _parse_positive_int(
        env.get("NEEDLE_MLX_ADAPTIVE_SINGLE_CHUNK_UNTIL_TOKENS", "1500"),
        "single chunk token threshold",
    )
    small_max_length = _parse_positive_int(
        env.get("NEEDLE_MLX_ADAPTIVE_SMALL_MAX_LENGTH", "2048"),
        "small adaptive max length",
    )
    large_max_length = _parse_positive_int(
        env.get("NEEDLE_MLX_ADAPTIVE_LARGE_MAX_LENGTH", "1024"),
        "large adaptive max length",
    )
    selected = small_max_length if original_tokens <= single_chunk_until else large_max_length
    return max(selected, prompt_tokens + min_code_tokens), profile


def _active_capability_ids() -> list[str]:
    try:
        from ...registry import load_active_package

        return load_active_package().capability_ids
    except Exception:  # noqa: BLE001
        return [REFERENCE_CAPABILITY]


def _first_env(names: tuple[str, ...], env: Mapping[str, str]) -> str | None:
    for name in names:
        value = env.get(name)
        if value is not None:
            return value
    return None


def _parse_bool(value: str) -> bool:
    return value.lower() not in {"0", "false", "no", "off"}


def _parse_positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed
