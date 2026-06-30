"""Backbone resolution for the code-pruner MLX backend.

This stays import-light so tests can exercise the model-closure policy without
requiring MLX on the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import first_env

BACKBONE_REVISION_ENV_NAMES = ("NEEDLE_BACKBONE_REVISION", "HAY_BACKBONE_REVISION")


@dataclass(frozen=True)
class ResolvedBackbone:
    repo: str
    requested_revision: str
    resolved_revision: str
    path: str | None = None


def _requested_backbone_revision(config: dict[str, object]) -> str | None:
    revision = first_env(BACKBONE_REVISION_ENV_NAMES)
    if revision is None:
        raw = config.get("backbone_revision") or config.get("backbone_model_revision")
        revision = str(raw) if raw is not None else None
    revision = revision.strip() if revision else None
    return revision or None


def _explicit_local_path(value: str) -> bool:
    if value in {".", ".."}:
        return True
    if value.startswith(("./", "../", "~")):
        return True
    return Path(value).expanduser().is_absolute()


def resolve_backbone_reference(
    backbone_name: str,
    config: dict[str, object],
) -> ResolvedBackbone:
    if _explicit_local_path(backbone_name):
        local_path = Path(backbone_name).expanduser()
        if not local_path.is_dir():
            raise FileNotFoundError(f"local backbone path does not exist: {local_path}")
        return ResolvedBackbone(
            repo=backbone_name,
            requested_revision="local",
            resolved_revision="local",
            path=str(local_path),
        )

    from needle.model_download import resolve_model_revision

    revision = _requested_backbone_revision(config)
    return ResolvedBackbone(
        repo=backbone_name,
        requested_revision=revision or "default",
        resolved_revision=resolve_model_revision(backbone_name, revision),
    )


def download_backbone_snapshot(backbone: ResolvedBackbone) -> str:
    if backbone.path:
        return backbone.path

    from needle.model_download import download_model_snapshot

    result = download_model_snapshot(
        repo=backbone.repo,
        revision=backbone.resolved_revision,
        caller="runtime-backbone",
        force=False,
    )
    return result.path
