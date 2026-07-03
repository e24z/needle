"""Shared Hugging Face model download and provenance helpers."""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import paths as naming

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True)
class ModelDownloadResult:
    path: str
    repo: str
    requested_revision: str
    resolved_revision: str
    downloaded: bool
    provenance: dict[str, Any] | None = None


def provenance_path(local_dir: Path) -> Path:
    return local_dir / "needle-model.json"


def _safe_path_segment(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-")
    return safe or "snapshot"


def model_snapshot_dir(repo: str, resolved_revision: str) -> Path:
    """Revision-scoped local directory for a resolved Hugging Face snapshot."""
    return naming.model_dir_for_repo(repo) / _safe_path_segment(resolved_revision)


def read_model_provenance(local_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(provenance_path(local_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _ensure_model_dir(path: Path) -> None:
    app_home = naming.app_home()
    if path == app_home or path.is_relative_to(app_home):
        naming.ensure_private_dir(app_home)
    naming.ensure_runtime_parent(path)


def write_model_provenance(
    local_dir: Path,
    *,
    repo: str,
    requested_revision: str,
    resolved_revision: str,
    caller: str,
    downloaded_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = {
        "repo": repo,
        "requested_revision": requested_revision,
        "resolved_revision": resolved_revision,
        "downloaded_at": downloaded_at
        or datetime.datetime.now(datetime.UTC).isoformat(),
        "caller": caller,
    }
    if extra:
        data.update(extra)
    _ensure_model_dir(local_dir)
    naming.write_private_text(
        provenance_path(local_dir),
        json.dumps(data, indent=2) + "\n",
    )
    return data


def augment_model_provenance(local_dir: Path, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Merge extra resolved-runtime metadata into an existing provenance file."""
    existing = read_model_provenance(local_dir)
    if existing is None:
        return None
    data = {**existing, **updates}
    _ensure_model_dir(local_dir)
    naming.write_private_text(
        provenance_path(local_dir),
        json.dumps(data, indent=2) + "\n",
    )
    return data


def _looks_like_commit_sha(revision: str | None) -> bool:
    return bool(revision and _SHA_RE.match(revision))


def _existing_matches_resolved_commit(
    existing: dict[str, Any],
    *,
    repo: str,
    resolved_revision: str,
) -> bool:
    if str(existing.get("repo") or "") != repo:
        return False
    existing_resolved = str(existing.get("resolved_revision") or "")
    return bool(existing_resolved and existing_resolved == resolved_revision)


def resolve_model_revision(
    repo: str,
    revision: str | None,
    *,
    hf_api: Any | None = None,
) -> str:
    if _looks_like_commit_sha(revision):
        return str(revision)
    if hf_api is None:
        from huggingface_hub import HfApi

        hf_api = HfApi()
    info = hf_api.model_info(repo, revision=revision or None)
    resolved = str(getattr(info, "sha", "") or "")
    if not resolved:
        requested = revision or "default"
        raise RuntimeError(f"could not resolve model revision {requested!r} for {repo!r}")
    return resolved


def download_model_snapshot(
    *,
    repo: str,
    revision: str | None = None,
    caller: str,
    force: bool = True,
    hf_api: Any | None = None,
    snapshot_download_fn: Callable[..., str] | None = None,
) -> ModelDownloadResult:
    requested_revision = revision or "default"
    root = naming.model_root()
    if _looks_like_commit_sha(revision):
        resolved_revision = str(revision)
    else:
        resolved_revision = resolve_model_revision(repo, revision, hf_api=hf_api)
    repo_dir = naming.model_dir_for_repo(repo)
    local_dir = model_snapshot_dir(repo, resolved_revision)
    existing = read_model_provenance(local_dir)

    if (
        existing
        and not force
        and _existing_matches_resolved_commit(
            existing,
            repo=repo,
            resolved_revision=resolved_revision,
        )
    ):
        return ModelDownloadResult(
            path=str(local_dir),
            repo=repo,
            requested_revision=requested_revision,
            resolved_revision=resolved_revision,
            downloaded=False,
            provenance=existing,
        )

    if snapshot_download_fn is None:
        from huggingface_hub import snapshot_download

        snapshot_download_fn = snapshot_download

    _ensure_model_dir(root)
    _ensure_model_dir(repo_dir)
    _ensure_model_dir(local_dir)
    path = snapshot_download_fn(
        repo,
        revision=resolved_revision,
        local_dir=str(local_dir),
        cache_dir=str(root / ".hf-cache"),
    )
    provenance = write_model_provenance(
        local_dir,
        repo=repo,
        requested_revision=requested_revision,
        resolved_revision=resolved_revision,
        caller=caller,
    )
    return ModelDownloadResult(
        path=str(path),
        repo=repo,
        requested_revision=requested_revision,
        resolved_revision=resolved_revision,
        downloaded=True,
        provenance=provenance,
    )
