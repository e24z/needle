"""Single built-in runtime path.

Needle has one built-in runtime path: local MLX Soft-LaMR pruning through the MCP
observation surface. Keep this module tiny so a future extension system can wrap
it later without being part of the runtime today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class RuntimeConfig:
    runtime_id: str
    tool_surface: str
    backend_id: str
    runtime_profile: str
    env: dict[str, str] = field(default_factory=dict)


DEFAULT_RUNTIME = RuntimeConfig(
    runtime_id="mlx-soft-lamr",
    tool_surface="mcp/bash",
    backend_id="code-pruner-mlx",
    runtime_profile="local_mlx_adaptive",
    env={
        "NEEDLE_BACKEND": "e24z/code-pruner-mlx",
        "NEEDLE_MLX_PROFILE": "local_adaptive",
        "NEEDLE_MLX_MAX_BATCH_SIZE": "1",
    },
)


def apply_runtime_env(config: RuntimeConfig = DEFAULT_RUNTIME) -> RuntimeConfig:
    for key, value in config.env.items():
        os.environ.setdefault(key, value)
    return config


def runtime_identity(config: RuntimeConfig = DEFAULT_RUNTIME) -> dict[str, str]:
    return {
        "runtime_id": config.runtime_id,
        "tool_surface": config.tool_surface,
        "backend_id": config.backend_id,
        "runtime_profile": config.runtime_profile,
    }


def runtime_manage_command() -> list[str]:
    return ["needle", "runtime", "manage"]
