"""Runtime policy knobs for the code-pruner backend.

This module must stay import-light: tests import it under plain python3 on
machines with no MLX runtime.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping


REFERENCE_CAPABILITY = "swe-pruner/reference"
SOFT_LAMR_CAPABILITY = "e24z/soft-lamr"
REPAIR_ENV_NAMES = ("HAY_REPAIR", "NEEDLE_REPAIR")


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


def _active_capability_ids() -> list[str]:
    try:
        from ...package_config import load_active_package

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
