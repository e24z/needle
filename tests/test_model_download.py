"""Model download provenance without network access."""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.model_download import (  # noqa: E402
    download_model_snapshot,
    model_snapshot_dir,
    read_model_provenance,
    write_model_provenance,
)
from needle.runtime import naming  # noqa: E402


class _Info:
    def __init__(self, sha: str) -> None:
        self.sha = sha


class _Api:
    def __init__(self, sha: str = "commit-sha-123") -> None:
        self.sha = sha
        self.calls: list[tuple[str, str | None]] = []

    def model_info(self, repo: str, revision: str | None = None) -> _Info:
        self.calls.append((repo, revision))
        return _Info(self.sha)


def _snapshot_recorder(calls: list[dict[str, str | None]]):
    def snapshot_download(repo: str, **kwargs) -> str:
        calls.append({"repo": repo, **kwargs})
        local_dir = Path(str(kwargs["local_dir"]))
        local_dir.mkdir(parents=True, exist_ok=True)
        return str(local_dir)

    return snapshot_download


def _with_model_root(root: Path):
    old = {name: os.environ.get(name) for name in ("NEEDLE_MODEL_ROOT", "HAY_MODEL_ROOT")}
    os.environ["NEEDLE_MODEL_ROOT"] = str(root)
    os.environ.pop("HAY_MODEL_ROOT", None)
    return old


def _restore_env(old: dict[str, str | None]) -> None:
    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _with_default_home(home: Path):
    names = ("NEEDLE_HOME", "HAY_HOME", "NEEDLE_MODEL_ROOT", "HAY_MODEL_ROOT")
    old = {name: os.environ.get(name) for name in names}
    os.environ["NEEDLE_HOME"] = str(home)
    for name in ("HAY_HOME", "NEEDLE_MODEL_ROOT", "HAY_MODEL_ROOT"):
        os.environ.pop(name, None)
    return old


def test_model_download_uses_resolved_commit_and_writes_provenance() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        calls: list[dict[str, str | None]] = []
        try:
            result = download_model_snapshot(
                repo="org/model",
                revision="main",
                caller="cli",
                force=True,
                hf_api=_Api(),
                snapshot_download_fn=_snapshot_recorder(calls),
            )
        finally:
            _restore_env(old_env)

        assert calls[0]["revision"] == "commit-sha-123", calls
        local_dir = Path(str(calls[0]["local_dir"]))
        assert result.path == str(local_dir)
        data = json.loads((local_dir / "needle-model.json").read_text(encoding="utf-8"))
        assert data["repo"] == "org/model"
        assert data["requested_revision"] == "main"
        assert data["resolved_revision"] == "commit-sha-123"
        assert data["caller"] == "cli"
        assert "downloaded_at" in data


def test_runtime_lazy_download_uses_same_revision_and_provenance_schema() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        api = _Api()
        calls: list[dict[str, str | None]] = []
        try:
            result = download_model_snapshot(
                repo="org/model",
                revision="main",
                caller="runtime",
                force=False,
                hf_api=api,
                snapshot_download_fn=_snapshot_recorder(calls),
            )
        finally:
            _restore_env(old_env)

        assert result.downloaded is True
        assert calls[0]["revision"] == "commit-sha-123", calls
        assert result.resolved_revision == "commit-sha-123"
        assert result.provenance is not None
        assert result.provenance["caller"] == "runtime"
        assert set(result.provenance) >= {
            "repo",
            "requested_revision",
            "resolved_revision",
            "downloaded_at",
            "caller",
        }


def test_default_model_download_creates_private_app_home_under_permissive_umask() -> None:
    old_umask = os.umask(0)
    try:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            old_env = _with_default_home(home)
            calls: list[dict[str, str | None]] = []
            try:
                result = download_model_snapshot(
                    repo="org/model",
                    revision="abc123def",
                    caller="runtime",
                    snapshot_download_fn=_snapshot_recorder(calls),
                )
                assert result.downloaded is True
                assert home.stat().st_mode & 0o777 == 0o700
                local_dir = Path(result.path)
                assert local_dir.stat().st_mode & 0o777 == 0o700
                assert (local_dir / "needle-model.json").stat().st_mode & 0o777 == 0o600
            finally:
                _restore_env(old_env)
    finally:
        os.umask(old_umask)


def test_explicit_sha_revision_is_recorded_without_api_resolution() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        calls: list[dict[str, str | None]] = []

        class NoApi:
            def model_info(self, repo: str, revision: str | None = None) -> _Info:
                raise AssertionError("explicit SHA should not call model_info")

        try:
            result = download_model_snapshot(
                repo="org/model",
                revision="abc123def",
                caller="runtime",
                hf_api=NoApi(),
                snapshot_download_fn=_snapshot_recorder(calls),
            )
        finally:
            _restore_env(old_env)

        assert calls[0]["revision"] == "abc123def", calls
        assert result.provenance is not None
        assert result.provenance["requested_revision"] == "abc123def"
        assert result.provenance["resolved_revision"] == "abc123def"


def test_existing_local_model_with_provenance_is_reused_without_download() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        api = _Api()
        try:
            local_dir = model_snapshot_dir("org/model", "commit-sha-123")
            original = write_model_provenance(
                local_dir,
                repo="org/model",
                requested_revision="main",
                resolved_revision="commit-sha-123",
                caller="cli",
                downloaded_at="2026-01-01T00:00:00+00:00",
            )

            def fail_download(*_args, **_kwargs) -> str:
                raise AssertionError("existing provenance should avoid lazy download")

            result = download_model_snapshot(
                repo="org/model",
                revision="main",
                caller="runtime",
                force=False,
                hf_api=api,
                snapshot_download_fn=fail_download,
            )
        finally:
            _restore_env(old_env)

        assert result.downloaded is False
        assert result.path == str(local_dir)
        assert read_model_provenance(local_dir) == original
        assert api.calls == [("org/model", "main")]


def test_existing_provenance_for_colliding_repo_is_not_reused() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        calls: list[dict[str, str | None]] = []
        try:
            local_dir = model_snapshot_dir("org/model", "abc123def")
            assert local_dir == model_snapshot_dir("org--model", "abc123def")
            write_model_provenance(
                local_dir,
                repo="org/model",
                requested_revision="abc123def",
                resolved_revision="abc123def",
                caller="cli",
                downloaded_at="2026-01-01T00:00:00+00:00",
            )
            result = download_model_snapshot(
                repo="org--model",
                revision="abc123def",
                caller="runtime",
                force=False,
                snapshot_download_fn=_snapshot_recorder(calls),
            )
        finally:
            _restore_env(old_env)

        assert result.downloaded is True
        assert calls[0]["repo"] == "org--model", calls
        assert calls[0]["revision"] == "abc123def", calls
        assert result.provenance is not None
        assert result.provenance["repo"] == "org--model"


def test_existing_branch_snapshot_downloads_when_resolved_commit_changes() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        api = _Api("newsha")
        calls: list[dict[str, str | None]] = []
        try:
            local_dir = model_snapshot_dir("org/model", "oldsha")
            write_model_provenance(
                local_dir,
                repo="org/model",
                requested_revision="main",
                resolved_revision="oldsha",
                caller="cli",
                downloaded_at="2026-01-01T00:00:00+00:00",
            )
            result = download_model_snapshot(
                repo="org/model",
                revision="main",
                caller="runtime",
                force=False,
                hf_api=api,
                snapshot_download_fn=_snapshot_recorder(calls),
            )
        finally:
            _restore_env(old_env)

        assert api.calls == [("org/model", "main")]
        assert calls[0]["revision"] == "newsha", calls
        assert result.provenance is not None
        assert result.provenance["resolved_revision"] == "newsha"


def test_existing_default_snapshot_downloads_when_resolved_commit_changes() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        api = _Api("newsha")
        calls: list[dict[str, str | None]] = []
        try:
            local_dir = model_snapshot_dir("org/model", "oldsha")
            write_model_provenance(
                local_dir,
                repo="org/model",
                requested_revision="default",
                resolved_revision="oldsha",
                caller="cli",
                downloaded_at="2026-01-01T00:00:00+00:00",
            )
            result = download_model_snapshot(
                repo="org/model",
                revision=None,
                caller="runtime",
                force=False,
                hf_api=api,
                snapshot_download_fn=_snapshot_recorder(calls),
            )
        finally:
            _restore_env(old_env)

        assert api.calls == [("org/model", None)]
        assert calls[0]["revision"] == "newsha", calls
        assert result.provenance is not None
        assert result.provenance["requested_revision"] == "default"
        assert result.provenance["resolved_revision"] == "newsha"


def test_existing_mutable_snapshot_reuses_after_resolving_same_commit() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        api = _Api("same-sha")
        try:
            local_dir = model_snapshot_dir("org/model", "same-sha")
            write_model_provenance(
                local_dir,
                repo="org/model",
                requested_revision="main",
                resolved_revision="same-sha",
                caller="cli",
                downloaded_at="2026-01-01T00:00:00+00:00",
            )

            def fail_download(*_args, **_kwargs) -> str:
                raise AssertionError("same resolved commit should reuse local snapshot")

            result = download_model_snapshot(
                repo="org/model",
                revision="main",
                caller="runtime",
                force=False,
                hf_api=api,
                snapshot_download_fn=fail_download,
            )
        finally:
            _restore_env(old_env)

        assert api.calls == [("org/model", "main")]
        assert result.downloaded is False
        assert result.resolved_revision == "same-sha"


def test_existing_exact_sha_snapshot_reuses_without_api_or_download() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        try:
            local_dir = model_snapshot_dir("org/model", "abc123def")
            write_model_provenance(
                local_dir,
                repo="org/model",
                requested_revision="abc123def",
                resolved_revision="abc123def",
                caller="cli",
                downloaded_at="2026-01-01T00:00:00+00:00",
            )

            class NoApi:
                def model_info(self, repo: str, revision: str | None = None) -> _Info:
                    raise AssertionError("matching exact SHA should not call model_info")

            def fail_download(*_args, **_kwargs) -> str:
                raise AssertionError("matching exact SHA should reuse local snapshot")

            result = download_model_snapshot(
                repo="org/model",
                revision="abc123def",
                caller="runtime",
                force=False,
                hf_api=NoApi(),
                snapshot_download_fn=fail_download,
            )
        finally:
            _restore_env(old_env)

        assert result.downloaded is False
        assert result.resolved_revision == "abc123def"


def test_existing_local_model_with_different_explicit_sha_downloads() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        calls: list[dict[str, str | None]] = []
        try:
            local_dir = model_snapshot_dir("org/model", "abc123def")
            write_model_provenance(
                local_dir,
                repo="org/model",
                requested_revision="abc123def",
                resolved_revision="abc123def",
                caller="cli",
                downloaded_at="2026-01-01T00:00:00+00:00",
            )
            result = download_model_snapshot(
                repo="org/model",
                revision="def456abc",
                caller="runtime",
                force=False,
                snapshot_download_fn=_snapshot_recorder(calls),
            )
        finally:
            _restore_env(old_env)

        assert result.downloaded is True
        assert calls[0]["revision"] == "def456abc", calls
        assert result.provenance is not None
        assert result.provenance["resolved_revision"] == "def456abc"


def test_revision_specific_snapshot_path_keeps_stale_files_out_of_new_revisions() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_env = _with_model_root(Path(td) / "models")
        api = _Api("newsha")
        calls: list[dict[str, str | None]] = []
        try:
            old_dir = model_snapshot_dir("org/model", "oldsha")
            write_model_provenance(
                old_dir,
                repo="org/model",
                requested_revision="main",
                resolved_revision="oldsha",
                caller="cli",
                downloaded_at="2026-01-01T00:00:00+00:00",
            )
            (old_dir / "removed-in-new-revision.safetensors").write_text("stale", encoding="utf-8")

            result = download_model_snapshot(
                repo="org/model",
                revision="main",
                caller="runtime",
                force=False,
                hf_api=api,
                snapshot_download_fn=_snapshot_recorder(calls),
            )
            new_dir = model_snapshot_dir("org/model", "newsha")
        finally:
            _restore_env(old_env)

        assert Path(result.path) == new_dir
        assert calls[0]["local_dir"] == str(new_dir), calls
        assert (old_dir / "removed-in-new-revision.safetensors").exists()
        assert not (new_dir / "removed-in-new-revision.safetensors").exists()


def test_code_pruner_backend_uses_resolved_backbone_snapshot_for_network_loads() -> None:
    root = Path(__file__).resolve().parent.parent
    source_paths = [
        root / "src/needle/backends/code_pruner/model.py",
        root / "src/needle/backends/code_pruner/backbone.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    tree = ast.parse(source)
    bad_calls: list[str] = []
    found_backbone_revision_resolution = False
    found_resolved_backbone_download = False

    def call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        if name == "hf_hub_download":
            if not any(keyword.arg == "revision" for keyword in node.keywords):
                bad_calls.append("hf_hub_download without revision")
        if (
            name == "load"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "backbone_name"
        ):
            bad_calls.append("load(backbone_name)")
        if name == "resolve_model_revision":
            found_backbone_revision_resolution = (
                found_backbone_revision_resolution
                or (
                    node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "backbone_name"
                )
            )
        if name == "download_model_snapshot":
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            repo = keywords.get("repo")
            caller = keywords.get("caller")
            found_resolved_backbone_download = found_resolved_backbone_download or (
                isinstance(repo, ast.Attribute)
                and isinstance(repo.value, ast.Name)
                and repo.value.id == "backbone"
                and repo.attr == "repo"
                and isinstance(caller, ast.Constant)
                and caller.value == "runtime-backbone"
            )

    assert bad_calls == []
    assert found_backbone_revision_resolution
    assert found_resolved_backbone_download
    assert "backbone_resolved_revision" in source


def main() -> int:
    test_model_download_uses_resolved_commit_and_writes_provenance()
    test_runtime_lazy_download_uses_same_revision_and_provenance_schema()
    test_default_model_download_creates_private_app_home_under_permissive_umask()
    test_explicit_sha_revision_is_recorded_without_api_resolution()
    test_existing_local_model_with_provenance_is_reused_without_download()
    test_existing_provenance_for_colliding_repo_is_not_reused()
    test_existing_branch_snapshot_downloads_when_resolved_commit_changes()
    test_existing_default_snapshot_downloads_when_resolved_commit_changes()
    test_existing_mutable_snapshot_reuses_after_resolving_same_commit()
    test_existing_exact_sha_snapshot_reuses_without_api_or_download()
    test_existing_local_model_with_different_explicit_sha_downloads()
    test_revision_specific_snapshot_path_keeps_stale_files_out_of_new_revisions()
    test_code_pruner_backend_uses_resolved_backbone_snapshot_for_network_loads()
    print("test_model_download OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
