"""Code-pruner backbone provenance policy without requiring MLX."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle import model_download  # noqa: E402
from needle.backends.code_pruner import backbone as backbone_mod  # noqa: E402


def _restore_env(old: dict[str, str | None]) -> None:
    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_default_backbone_repo_is_not_shadowed_by_workspace_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_cwd = os.getcwd()
        old_resolve = model_download.resolve_model_revision
        calls: list[tuple[str, str | None]] = []

        def fake_resolve(repo: str, revision: str | None) -> str:
            calls.append((repo, revision))
            return "resolved-backbone-sha"

        Path(td, "Qwen/Qwen3-Reranker-0.6B").mkdir(parents=True)
        os.chdir(td)
        model_download.resolve_model_revision = fake_resolve
        try:
            ref = backbone_mod.resolve_backbone_reference(
                "Qwen/Qwen3-Reranker-0.6B",
                {},
            )
        finally:
            model_download.resolve_model_revision = old_resolve
            os.chdir(old_cwd)

    assert calls == [("Qwen/Qwen3-Reranker-0.6B", None)]
    assert ref.repo == "Qwen/Qwen3-Reranker-0.6B"
    assert ref.requested_revision == "default"
    assert ref.resolved_revision == "resolved-backbone-sha"
    assert ref.path is None


def test_explicit_local_backbone_path_is_allowed_and_missing_paths_fail() -> None:
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "local-backbone"
        local.mkdir()
        ref = backbone_mod.resolve_backbone_reference(str(local), {})
        assert ref.repo == str(local)
        assert ref.requested_revision == "local"
        assert ref.resolved_revision == "local"
        assert ref.path == str(local)

        try:
            backbone_mod.resolve_backbone_reference(str(Path(td) / "missing"), {})
        except FileNotFoundError as exc:
            assert "local backbone path does not exist" in str(exc)
        else:
            raise AssertionError("missing explicit local backbone path was accepted")


def test_backbone_revision_and_download_use_resolved_revision() -> None:
    old_env = {
        "NEEDLE_BACKBONE_REVISION": os.environ.get("NEEDLE_BACKBONE_REVISION"),
        "HAY_BACKBONE_REVISION": os.environ.get("HAY_BACKBONE_REVISION"),
    }
    old_resolve = model_download.resolve_model_revision
    old_download = model_download.download_model_snapshot
    resolve_calls: list[tuple[str, str | None]] = []
    download_calls: list[dict[str, object]] = []

    def fake_resolve(repo: str, revision: str | None) -> str:
        resolve_calls.append((repo, revision))
        return "resolved-backbone-sha"

    def fake_download_model_snapshot(**kwargs):  # noqa: ANN001
        download_calls.append(kwargs)
        return SimpleNamespace(path="/tmp/resolved-backbone")

    os.environ.pop("NEEDLE_BACKBONE_REVISION", None)
    os.environ.pop("HAY_BACKBONE_REVISION", None)
    model_download.resolve_model_revision = fake_resolve
    model_download.download_model_snapshot = fake_download_model_snapshot
    try:
        ref = backbone_mod.resolve_backbone_reference(
            "Qwen/Qwen3-Reranker-0.6B",
            {"backbone_revision": "config-revision"},
        )
        path = backbone_mod.download_backbone_snapshot(ref)
    finally:
        model_download.resolve_model_revision = old_resolve
        model_download.download_model_snapshot = old_download
        _restore_env(old_env)

    assert resolve_calls == [("Qwen/Qwen3-Reranker-0.6B", "config-revision")]
    assert ref.requested_revision == "config-revision"
    assert ref.resolved_revision == "resolved-backbone-sha"
    assert path == "/tmp/resolved-backbone"
    assert download_calls == [
        {
            "repo": "Qwen/Qwen3-Reranker-0.6B",
            "revision": "resolved-backbone-sha",
            "caller": "runtime-backbone",
            "force": False,
        }
    ]


def test_light_backbone_config_fetch_uses_resolved_revision() -> None:
    calls: list[tuple[str, str, str | None]] = []
    module_names = [
        "huggingface_hub",
        "mlx",
        "mlx.core",
        "mlx.nn",
        "mlx_lm",
        "mlx_lm.models",
        "mlx_lm.models.qwen3",
        "numpy",
        "transformers",
        "needle.backends.code_pruner.model",
    ]
    old_modules = {name: sys.modules.get(name) for name in module_names}

    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "config.json"
        config_path.write_text('{"hidden_size": 8}\n', encoding="utf-8")

        fake_hf = types.ModuleType("huggingface_hub")

        def fake_hf_hub_download(repo: str, filename: str, *, revision: str | None = None) -> str:
            calls.append((repo, filename, revision))
            return str(config_path)

        fake_hf.hf_hub_download = fake_hf_hub_download

        fake_mlx = types.ModuleType("mlx")
        fake_mx = types.ModuleType("mlx.core")
        fake_mx.array = object
        fake_mx.zeros = lambda *_args, **_kwargs: None
        fake_nn = types.ModuleType("mlx.nn")
        fake_nn.Module = object

        fake_mlx_lm = types.ModuleType("mlx_lm")
        fake_mlx_lm.load = lambda *_args, **_kwargs: None
        fake_models = types.ModuleType("mlx_lm.models")
        fake_qwen3 = types.ModuleType("mlx_lm.models.qwen3")
        fake_numpy = types.ModuleType("numpy")
        fake_numpy.ndarray = object
        fake_numpy.int64 = "int64"

        class FakeModelArgs:
            @classmethod
            def from_dict(cls, data):  # noqa: ANN001
                return data

        class FakeModel:
            def __init__(self, args):  # noqa: ANN001
                self.args = args

        fake_qwen3.ModelArgs = FakeModelArgs
        fake_qwen3.Model = FakeModel

        fake_transformers = types.ModuleType("transformers")

        class FakeAutoTokenizer:
            @classmethod
            def from_pretrained(cls, model_dir):  # noqa: ANN001
                return {"model_dir": model_dir}

        fake_transformers.AutoTokenizer = FakeAutoTokenizer

        sys.modules["huggingface_hub"] = fake_hf
        sys.modules["mlx"] = fake_mlx
        sys.modules["mlx.core"] = fake_mx
        sys.modules["mlx.nn"] = fake_nn
        sys.modules["mlx_lm"] = fake_mlx_lm
        sys.modules["mlx_lm.models"] = fake_models
        sys.modules["mlx_lm.models.qwen3"] = fake_qwen3
        sys.modules["numpy"] = fake_numpy
        sys.modules["transformers"] = fake_transformers
        sys.modules.pop("needle.backends.code_pruner.model", None)
        try:
            model_mod = importlib.import_module("needle.backends.code_pruner.model")
            ref = backbone_mod.ResolvedBackbone(
                repo="Qwen/Qwen3-Reranker-0.6B",
                requested_revision="default",
                resolved_revision="resolved-backbone-sha",
            )
            owner = SimpleNamespace(model_dir="/tmp/main-model")
            model_obj, tokenizer = model_mod.MLXSwePrunerBackend._build_backbone_light(owner, ref)
        finally:
            sys.modules.pop("needle.backends.code_pruner.model", None)
            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    assert calls == [
        ("Qwen/Qwen3-Reranker-0.6B", "config.json", "resolved-backbone-sha")
    ]
    assert model_obj.args == {"hidden_size": 8}
    assert tokenizer == {"model_dir": "/tmp/main-model"}


def main() -> int:
    test_default_backbone_repo_is_not_shadowed_by_workspace_path()
    test_explicit_local_backbone_path_is_allowed_and_missing_paths_fail()
    test_backbone_revision_and_download_use_resolved_revision()
    test_light_backbone_config_fetch_uses_resolved_revision()
    print("test_code_pruner_backbone OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
