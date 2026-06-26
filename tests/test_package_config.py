"""Static Needle package registry and loader.

Run: PYTHONPATH=src python3 tests/test_package_config.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.registry import (  # noqa: E402
    BUILTIN_REGISTRY_ROOT,
    PackageConfigError,
    active_package_selection,
    configured_package_id,
    load_backend,
    load_active_package,
    package_summaries,
    runtime_launch_plan,
    set_configured_package_id,
)
from needle.runtime import cli as runtime_cli  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
REGISTRY_ROOT = BUILTIN_REGISTRY_ROOT


def _copy_registry(tmp: Path) -> None:
    for name in (
        "protocols",
        "capabilities",
        "backends",
        "bindings",
        "packages",
        "claims",
        "package-cards",
        "evidence",
    ):
        src = REGISTRY_ROOT / name
        if src.exists():
            shutil.copytree(src, tmp / name)


def test_default_package_graph_loads_soft_lamr() -> None:
    loaded = load_active_package(REGISTRY_ROOT)
    assert loaded.package_id == "e24z/mlx-pi-soft-lamr"
    assert loaded.protocol["id"] == "needle/text-transform"
    assert loaded.capability_ids == ["e24z/soft-lamr"]
    assert loaded.capabilities["e24z/soft-lamr"]["extends"] == "swe-pruner/reference"
    assert loaded.backend_id == "e24z/code-pruner-mlx"
    assert loaded.binding_id == "pi/native-tools"
    assert loaded.claim_card["capability"] == "e24z/soft-lamr"
    assert loaded.package_card_path.exists()
    assert loaded.evidence_refs == ["fixture_pack:mlx-pi-soft-lamr"]
    assert loaded.evidence_paths["fixture_pack:mlx-pi-soft-lamr"].exists()


def test_reference_package_graph_loads() -> None:
    loaded = load_active_package(REGISTRY_ROOT, "e24z/mlx-pi-reference")
    assert loaded.package_id == "e24z/mlx-pi-reference"
    assert loaded.protocol["id"] == "needle/text-transform"
    assert loaded.capability_ids == ["swe-pruner/reference"]
    assert loaded.backend_id == "e24z/code-pruner-mlx"
    assert loaded.binding_id == "pi/native-tools"
    assert loaded.claim_card["capability"] == "swe-pruner/reference"
    assert loaded.package_card_path.exists()
    assert loaded.evidence_refs == ["fixture_pack:mlx-pi-reference"]
    assert loaded.evidence_paths["fixture_pack:mlx-pi-reference"].exists()


def test_default_package_resolves_runtime_launch_plan() -> None:
    plan = runtime_launch_plan(REGISTRY_ROOT)
    assert plan.package_id == "e24z/mlx-pi-soft-lamr"
    assert plan.backend_id == "e24z/code-pruner-mlx"
    assert plan.kind == "needle-cli"
    assert plan.command == [
        "needle",
        "runtime",
        "manage",
    ]
    assert plan.env["NEEDLE_BACKEND"] == "e24z/code-pruner-mlx"
    assert plan.env["HAY_BACKEND"] == "code-pruner"
    assert plan.runtime_profile == "local_mlx_adaptive"
    assert plan.env["NEEDLE_MLX_PROFILE"] == "local_adaptive"
    assert plan.env["NEEDLE_MLX_MAX_BATCH_SIZE"] == "1"


def test_runtime_manage_applies_package_runtime_profile_env() -> None:
    names = [
        "NEEDLE_REGISTRY_ROOT",
        "HAY_REGISTRY_ROOT",
        "NEEDLE_PACKAGE",
        "HAY_PACKAGE",
        "NEEDLE_BACKEND",
        "HAY_BACKEND",
        "NEEDLE_MLX_PROFILE",
        "NEEDLE_MLX_MAX_BATCH_SIZE",
    ]
    old = {name: os.environ.get(name) for name in names}
    try:
        os.environ["NEEDLE_REGISTRY_ROOT"] = str(REGISTRY_ROOT)
        os.environ.pop("HAY_REGISTRY_ROOT", None)
        os.environ.pop("NEEDLE_PACKAGE", None)
        os.environ.pop("HAY_PACKAGE", None)
        os.environ.pop("NEEDLE_BACKEND", None)
        os.environ.pop("HAY_BACKEND", None)
        os.environ.pop("NEEDLE_MLX_PROFILE", None)
        os.environ.pop("NEEDLE_MLX_MAX_BATCH_SIZE", None)

        plan = runtime_cli._apply_runtime_launch_env(
            package_id="e24z/mlx-pi-soft-lamr",
            host_binding="pi/native-tools",
        )

        assert plan is not None
        assert plan.package_id == "e24z/mlx-pi-soft-lamr"
        assert os.environ["NEEDLE_BACKEND"] == "e24z/code-pruner-mlx"
        assert os.environ["HAY_BACKEND"] == "code-pruner"
        assert os.environ["NEEDLE_MLX_PROFILE"] == "local_adaptive"
        assert os.environ["NEEDLE_MLX_MAX_BATCH_SIZE"] == "1"
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_runtime_manage_raw_mode_skips_package_runtime_profile_env() -> None:
    old = os.environ.get("NEEDLE_MLX_PROFILE")
    try:
        os.environ.pop("NEEDLE_MLX_PROFILE", None)
        plan = runtime_cli._apply_runtime_launch_env(raw=True)
        assert plan is None
        assert "NEEDLE_MLX_PROFILE" not in os.environ
    finally:
        if old is None:
            os.environ.pop("NEEDLE_MLX_PROFILE", None)
        else:
            os.environ["NEEDLE_MLX_PROFILE"] = old


def test_http_backend_contract_validates_without_server() -> None:
    backend = load_backend(REGISTRY_ROOT, "e24z/code-pruner-http")
    assert backend["id"] == "e24z/code-pruner-http"
    assert backend["compute"]["default"] == "remote_http"
    assert backend["compute"]["requires"] == ["explicit_endpoint"]
    assert backend["runtime"] == "local_manager"
    assert backend["launcher"]["kind"] == "needle-cli"
    assert backend["launcher"]["command"] == ["needle", "runtime", "manage"]
    assert backend["transport"]["kind"] == "http_json"
    assert backend["transport"]["endpoint_env"] == "NEEDLE_HTTP_PRUNER_URL"
    assert backend["transport"]["endpoint_required"] is True
    assert backend["transport"]["failure_behavior"] == "passthrough_original"
    assert backend["transport"]["request"]["body"]["text"] == "string"
    assert backend["transport"]["response"]["body"]["text"] == "string"


def test_reference_capability_has_no_ast_repair() -> None:
    loaded = load_active_package(REGISTRY_ROOT, "e24z/mlx-pi-reference")
    ref = loaded.capabilities["swe-pruner/reference"]
    assert ref["claim_scope"]["ast_repair"] == "absent"
    assert ref["focus"]["field"] == "context_focus_question"
    assert ref["focus"]["missing"] == "passthrough_original"
    assert ref["rendering"]["marker_format"] == "(filtered {line_count} lines)"


def test_soft_lamr_is_separate_capability() -> None:
    path = REGISTRY_ROOT / "capabilities/e24z/soft-lamr.yaml"
    soft = json.loads(path.read_text(encoding="utf-8"))
    assert soft["extends"] == "swe-pruner/reference"
    assert soft["overrides"]["ast_repair"] == "python"


def test_soft_lamr_package_resolves_parent_protocol() -> None:
    loaded = load_active_package(REGISTRY_ROOT, "e24z/mlx-pi-soft-lamr")
    assert loaded.package_id == "e24z/mlx-pi-soft-lamr"
    assert loaded.protocol["id"] == "needle/text-transform"
    assert loaded.capability_ids == ["e24z/soft-lamr"]
    assert loaded.capabilities["e24z/soft-lamr"]["extends"] == "swe-pruner/reference"
    assert loaded.backend_id == "e24z/code-pruner-mlx"
    assert loaded.binding_id == "pi/native-tools"
    assert loaded.claim_card["capability"] == "e24z/soft-lamr"
    assert loaded.package_card_path.exists()
    assert loaded.evidence_refs == ["fixture_pack:mlx-pi-soft-lamr"]
    assert loaded.evidence_paths["fixture_pack:mlx-pi-soft-lamr"].exists()


def test_mcp_bash_package_loads_as_reference_host_binding() -> None:
    loaded = load_active_package(REGISTRY_ROOT, "e24z/mlx-mcp-bash-reference", host_binding="mcp/bash")
    assert loaded.package_id == "e24z/mlx-mcp-bash-reference"
    assert loaded.protocol["id"] == "needle/text-transform"
    assert loaded.capability_ids == ["swe-pruner/reference"]
    assert loaded.backend_id == "e24z/code-pruner-mlx"
    assert loaded.binding_id == "mcp/bash"
    assert set(loaded.binding["tools"]) == {"needle_bash"}
    assert loaded.claim_card["capability"] == "swe-pruner/reference"
    assert loaded.claim_card["tested"]["host"] == "mcp"
    assert loaded.package_card_path.exists()
    assert loaded.evidence_refs == ["fixture_pack:mlx-mcp-bash-reference"]
    assert loaded.evidence_paths["fixture_pack:mlx-mcp-bash-reference"].exists()


def test_mlx_package_family_has_explicit_surface_parity() -> None:
    reference = load_active_package(REGISTRY_ROOT, "e24z/mlx-pi-reference", host_binding="pi/native-tools")
    soft_lamr = load_active_package(REGISTRY_ROOT, "e24z/mlx-pi-soft-lamr", host_binding="pi/native-tools")
    mcp_bash = load_active_package(REGISTRY_ROOT, "e24z/mlx-mcp-bash-reference", host_binding="mcp/bash")

    old_config = os.environ.get("HAY_CONFIG")
    old_needle_config = os.environ.get("NEEDLE_CONFIG")
    old_package = os.environ.get("HAY_PACKAGE")
    old_needle_package = os.environ.get("NEEDLE_PACKAGE")
    with tempfile.TemporaryDirectory() as td:
        os.environ["NEEDLE_CONFIG"] = str(Path(td) / "missing.json")
        os.environ.pop("HAY_CONFIG", None)
        os.environ.pop("HAY_PACKAGE", None)
        os.environ.pop("NEEDLE_PACKAGE", None)
        try:
            assert active_package_selection(host_binding="pi/native-tools") == ("e24z/mlx-pi-soft-lamr", "default")
        finally:
            if old_config is None:
                os.environ.pop("HAY_CONFIG", None)
            else:
                os.environ["HAY_CONFIG"] = old_config
            if old_needle_config is None:
                os.environ.pop("NEEDLE_CONFIG", None)
            else:
                os.environ["NEEDLE_CONFIG"] = old_needle_config
            if old_package is None:
                os.environ.pop("HAY_PACKAGE", None)
            else:
                os.environ["HAY_PACKAGE"] = old_package
            if old_needle_package is None:
                os.environ.pop("NEEDLE_PACKAGE", None)
            else:
                os.environ["NEEDLE_PACKAGE"] = old_needle_package
    assert reference.backend_id == soft_lamr.backend_id == mcp_bash.backend_id == "e24z/code-pruner-mlx"
    assert set(reference.binding["tools"]) == {"read", "bash"}
    assert set(soft_lamr.binding["tools"]) == {"read", "bash"}
    assert set(mcp_bash.binding["tools"]) == {"needle_bash"}
    assert reference.package["focus_contract"]["missing_focus_behavior"] == "passthrough_original"
    assert soft_lamr.package["focus_contract"]["missing_focus_behavior"] == "passthrough_original"
    assert mcp_bash.package["focus_contract"]["missing_focus_behavior"] == "passthrough_original"
    assert reference.capability_ids == ["swe-pruner/reference"]
    assert soft_lamr.capability_ids == ["e24z/soft-lamr"]
    assert mcp_bash.capability_ids == ["swe-pruner/reference"]
    assert reference.package["runtime_profile"]["id"] == "local_mlx_adaptive"
    assert soft_lamr.package["runtime_profile"]["id"] == "local_mlx_adaptive"
    assert mcp_bash.package["runtime_profile"]["id"] == "local_mlx_adaptive"


def test_missing_backend_reference_fails_clearly() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)

        package_path = tmp / "packages/e24z/mlx-pi-soft-lamr.yaml"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["uses"]["backend"] = "e24z/missing-backend"
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected missing backend to fail")

    assert "missing backend object" in msg
    assert "e24z/missing-backend" in msg


def test_backend_must_support_package_capabilities() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)

        backend_path = tmp / "backends/e24z/code-pruner-mlx.yaml"
        backend = json.loads(backend_path.read_text(encoding="utf-8"))
        backend["supports"] = ["swe-pruner/reference"]
        backend_path.write_text(json.dumps(backend, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected unsupported capability to fail")

    assert "does not support package capabilities" in msg
    assert "e24z/soft-lamr" in msg


def test_registry_root_and_package_can_come_from_environment() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)

        old_root = os.environ.get("HAY_REGISTRY_ROOT")
        old_package = os.environ.get("HAY_PACKAGE")
        os.environ["HAY_REGISTRY_ROOT"] = str(tmp)
        os.environ["HAY_PACKAGE"] = "e24z/mlx-pi-reference"
        try:
            loaded = load_active_package()
        finally:
            if old_root is None:
                os.environ.pop("HAY_REGISTRY_ROOT", None)
            else:
                os.environ["HAY_REGISTRY_ROOT"] = old_root
            if old_package is None:
                os.environ.pop("HAY_PACKAGE", None)
            else:
                os.environ["HAY_PACKAGE"] = old_package

    assert loaded.package_id == "e24z/mlx-pi-reference"
    assert loaded.backend_id == "e24z/code-pruner-mlx"


def test_legacy_package_ids_resolve_to_canonical_names() -> None:
    assert load_active_package(REGISTRY_ROOT, "e24z/pi-local-mac").package_id == "e24z/mlx-pi-reference"
    assert load_active_package(REGISTRY_ROOT, "e24z/pi-local-mac-soft-lamr").package_id == "e24z/mlx-pi-soft-lamr"
    assert (
        load_active_package(REGISTRY_ROOT, "e24z/mcp-bash-local", host_binding="mcp/bash").package_id
        == "e24z/mlx-mcp-bash-reference"
    )


def test_package_selection_can_come_from_user_config() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.json"
        old_config = os.environ.get("HAY_CONFIG")
        old_needle_config = os.environ.get("NEEDLE_CONFIG")
        old_package = os.environ.get("HAY_PACKAGE")
        old_needle_package = os.environ.get("NEEDLE_PACKAGE")
        os.environ["NEEDLE_CONFIG"] = str(path)
        os.environ.pop("HAY_CONFIG", None)
        os.environ.pop("HAY_PACKAGE", None)
        os.environ.pop("NEEDLE_PACKAGE", None)
        try:
            selected = set_configured_package_id("e24z/mlx-pi-soft-lamr", root=REGISTRY_ROOT)
            assert selected.package_id == "e24z/mlx-pi-soft-lamr"
            assert configured_package_id() == "e24z/mlx-pi-soft-lamr"
            assert configured_package_id(host_binding="pi/native-tools") == "e24z/mlx-pi-soft-lamr"
            assert active_package_selection()[0] == "e24z/mlx-pi-soft-lamr"
            assert active_package_selection(host_binding="pi/native-tools")[0] == "e24z/mlx-pi-soft-lamr"
            assert load_active_package(REGISTRY_ROOT, host_binding="pi/native-tools").package_id == "e24z/mlx-pi-soft-lamr"

            os.environ["HAY_PACKAGE"] = "e24z/mlx-pi-reference"
            os.environ["NEEDLE_PACKAGE"] = "e24z/mlx-pi-soft-lamr"
            package_id, source = active_package_selection()
            assert package_id == "e24z/mlx-pi-soft-lamr"
            assert source == "env:NEEDLE_PACKAGE"

            os.environ.pop("NEEDLE_PACKAGE", None)
            package_id, source = active_package_selection()
            assert package_id == "e24z/mlx-pi-reference"
            assert source == "env:HAY_PACKAGE"
        finally:
            if old_config is None:
                os.environ.pop("HAY_CONFIG", None)
            else:
                os.environ["HAY_CONFIG"] = old_config
            if old_needle_config is None:
                os.environ.pop("NEEDLE_CONFIG", None)
            else:
                os.environ["NEEDLE_CONFIG"] = old_needle_config
            if old_package is None:
                os.environ.pop("HAY_PACKAGE", None)
            else:
                os.environ["HAY_PACKAGE"] = old_package
            if old_needle_package is None:
                os.environ.pop("NEEDLE_PACKAGE", None)
            else:
                os.environ["NEEDLE_PACKAGE"] = old_needle_package


def test_package_summaries_can_filter_by_host_binding() -> None:
    summaries = package_summaries(REGISTRY_ROOT, host_binding="pi/native-tools")
    ids = {item["id"] for item in summaries}
    assert "e24z/mlx-pi-reference" in ids
    assert "e24z/mlx-pi-soft-lamr" in ids
    assert all(item["host_binding"] == "pi/native-tools" for item in summaries)
    assert all(item["runtime_profile"] == "local_mlx_adaptive" for item in summaries)


def test_package_summaries_can_filter_mcp_binding() -> None:
    summaries = package_summaries(REGISTRY_ROOT, host_binding="mcp/bash")
    assert [item["id"] for item in summaries] == ["e24z/mlx-mcp-bash-reference"]
    assert summaries[0]["host_binding"] == "mcp/bash"
    assert summaries[0]["capabilities"] == ["swe-pruner/reference"]


def test_host_scoped_load_rejects_wrong_binding() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)

        binding_path = tmp / "bindings/example/other.yaml"
        binding_path.parent.mkdir(parents=True, exist_ok=True)
        binding_path.write_text(
            json.dumps(
                {
                    "schema": "needle.host_binding.v1",
                    "id": "example/other",
                    "host": "example",
                    "tools": {
                        "read": {
                            "artifact_kind": "file_text",
                            "focus_param": "context_focus_question",
                            "text_extract": "example_text",
                            "text_patch": "replace_example_text",
                        }
                    },
                    "fallbacks": {
                        "missing_focus": "passthrough_original",
                        "unsupported_result_shape": "passthrough_original",
                    },
                }
            ),
            encoding="utf-8",
        )
        package_path = tmp / "packages/e24z/mlx-pi-soft-lamr.yaml"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["host_binding"] = "example/other"
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp, "e24z/mlx-pi-soft-lamr", host_binding="pi/native-tools")
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected host binding mismatch to fail")

    assert "not requested host binding" in msg


def test_package_requires_focus_contract() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        package_path = tmp / "packages/e24z/mlx-pi-soft-lamr.yaml"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        del package["focus_contract"]
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected missing focus_contract to fail")

    assert "requires mapping field 'focus_contract'" in msg


def test_package_runtime_profile_env_is_public_needle_scoped() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        package_path = tmp / "packages/e24z/mlx-pi-soft-lamr.yaml"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["runtime_profile"]["env"] = {"HAY_MAX_LENGTH": "1024"}
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected non-NEEDLE runtime profile env to fail")

    assert "runtime_profile.env must map NEEDLE_* keys to strings" in msg


def test_package_runtime_profile_rejects_unknown_env_keys() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        package_path = tmp / "packages/e24z/mlx-pi-soft-lamr.yaml"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["runtime_profile"]["env"] = {"NEEDLE_SOMETHING_SQUISHY": "1"}
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected unknown runtime profile env to fail")

    assert "runtime_profile.env key 'NEEDLE_SOMETHING_SQUISHY' is unknown" in msg


def test_package_runtime_profile_rejects_invalid_env_values() -> None:
    cases = [
        ("NEEDLE_MLX_MAX_BATCH_SIZE", "0", "positive integer"),
        ("NEEDLE_CHUNK_OVERLAP_TOKENS", "-1", "non-negative integer"),
        ("NEEDLE_MLX_MAX_LENGTH_RATIO", "0.5", "at least 1"),
        ("NEEDLE_MLX_MAX_LENGTH_RATIO", "nan", "must be finite"),
        ("NEEDLE_MLX_MAX_LENGTH_RATIO", "inf", "must be finite"),
        ("NEEDLE_MLX_MAX_LENGTH_RATIO", "-inf", "must be finite"),
        ("NEEDLE_THRESHOLD", "nan", "must be finite"),
        ("NEEDLE_THRESHOLD", "inf", "must be finite"),
        ("NEEDLE_THRESHOLD", "-inf", "must be finite"),
        ("NEEDLE_REPAIR", "maybe", "must be a boolean"),
        ("NEEDLE_MLX_PROFILE", "fast-ish", "must be one of"),
    ]
    for key, value, expected in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _copy_registry(tmp)
            package_path = tmp / "packages/e24z/mlx-pi-soft-lamr.yaml"
            package = json.loads(package_path.read_text(encoding="utf-8"))
            package["runtime_profile"]["env"] = {key: value}
            package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

            try:
                load_active_package(tmp)
            except PackageConfigError as exc:
                msg = str(exc)
            else:
                raise AssertionError(f"expected invalid runtime profile env {key}={value!r} to fail")

        assert expected in msg


def test_binding_tool_mapping_must_use_known_artifact_kind() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        binding_path = tmp / "bindings/pi/native-tools.yaml"
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding["tools"]["read"]["artifact_kind"] = "mystery_blob"
        binding_path.write_text(json.dumps(binding, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected invalid artifact kind to fail")

    assert "artifact_kind 'mystery_blob' is unknown" in msg


def test_package_rejects_unknown_evidence_reference() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        package_path = tmp / "packages/e24z/mlx-pi-soft-lamr.yaml"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["evidence"] = ["somewhere:squishy"]
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected invalid evidence reference to fail")

    assert "evidence reference 'somewhere:squishy'" in msg


def test_package_rejects_missing_evidence_reference() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        evidence_path = tmp / "evidence/fixture-packs/mlx-pi-soft-lamr/manifest.json"
        evidence_path.unlink()

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected missing evidence manifest to fail")

    assert "missing evidence reference 'fixture_pack:mlx-pi-soft-lamr'" in msg


def test_fixture_pack_must_cover_required_pi_cases() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        manifest_path = tmp / "evidence/fixture-packs/mlx-pi-soft-lamr/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cases"] = [case for case in manifest["cases"] if case["tool"] != "bash"]
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected incomplete fixture pack to fail")

    assert "missing required cases" in msg
    assert "bash:visible_prune" in msg


def test_fixture_pack_must_match_package_binding() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        manifest_path = tmp / "evidence/fixture-packs/mlx-mcp-bash-reference/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["host_binding"] = "pi/native-tools"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp, "e24z/mlx-mcp-bash-reference", host_binding="mcp/bash")
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected fixture host binding mismatch to fail")

    assert "host_binding must be 'mcp/bash'" in msg


def test_mcp_fixture_pack_must_cover_needle_bash_cases() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        manifest_path = tmp / "evidence/fixture-packs/mlx-mcp-bash-reference/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cases"] = [
            case for case in manifest["cases"] if case["expected_behavior"] != "passthrough_original"
        ]
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp, "e24z/mlx-mcp-bash-reference", host_binding="mcp/bash")
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected incomplete MCP fixture pack to fail")

    assert "missing required cases" in msg
    assert "needle_bash:passthrough_original" in msg


def test_claim_card_tested_capability_must_match_claim() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        claim_path = tmp / "claims/mlx-pi-soft-lamr.yaml"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim["tested"]["capability"] = "swe-pruner/reference"
        claim_path.write_text(json.dumps(claim, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected claim/tested capability mismatch to fail")

    assert "does not match tested.capability" in msg


def test_backend_requires_text_interface() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        backend_path = tmp / "backends/e24z/code-pruner-mlx.yaml"
        backend = json.loads(backend_path.read_text(encoding="utf-8"))
        backend["interface"]["returns"] = ["scores"]
        backend_path.write_text(json.dumps(backend, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected invalid backend interface to fail")

    assert "interface must accept and return text" in msg


def test_http_backend_requires_explicit_endpoint() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        backend_path = tmp / "backends/e24z/code-pruner-http.yaml"
        backend = json.loads(backend_path.read_text(encoding="utf-8"))
        backend["transport"]["endpoint_required"] = False
        backend_path.write_text(json.dumps(backend, indent=2), encoding="utf-8")

        try:
            load_backend(tmp, "e24z/code-pruner-http")
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected implicit HTTP endpoint to fail")

    assert "transport.endpoint_required must be true" in msg


def test_http_backend_must_fail_open() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        backend_path = tmp / "backends/e24z/code-pruner-http.yaml"
        backend = json.loads(backend_path.read_text(encoding="utf-8"))
        backend["transport"]["failure_behavior"] = "raise_error"
        backend_path.write_text(json.dumps(backend, indent=2), encoding="utf-8")

        try:
            load_backend(tmp, "e24z/code-pruner-http")
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected non fail-open HTTP contract to fail")

    assert "transport.failure_behavior must be 'passthrough_original'" in msg


def main() -> int:
    test_default_package_graph_loads_soft_lamr()
    test_reference_package_graph_loads()
    test_default_package_resolves_runtime_launch_plan()
    test_runtime_manage_applies_package_runtime_profile_env()
    test_runtime_manage_raw_mode_skips_package_runtime_profile_env()
    test_http_backend_contract_validates_without_server()
    test_reference_capability_has_no_ast_repair()
    test_soft_lamr_is_separate_capability()
    test_soft_lamr_package_resolves_parent_protocol()
    test_mcp_bash_package_loads_as_reference_host_binding()
    test_mlx_package_family_has_explicit_surface_parity()
    test_missing_backend_reference_fails_clearly()
    test_backend_must_support_package_capabilities()
    test_registry_root_and_package_can_come_from_environment()
    test_legacy_package_ids_resolve_to_canonical_names()
    test_package_selection_can_come_from_user_config()
    test_package_summaries_can_filter_by_host_binding()
    test_package_summaries_can_filter_mcp_binding()
    test_host_scoped_load_rejects_wrong_binding()
    test_package_requires_focus_contract()
    test_package_runtime_profile_env_is_public_needle_scoped()
    test_package_runtime_profile_rejects_unknown_env_keys()
    test_package_runtime_profile_rejects_invalid_env_values()
    test_binding_tool_mapping_must_use_known_artifact_kind()
    test_package_rejects_unknown_evidence_reference()
    test_package_rejects_missing_evidence_reference()
    test_fixture_pack_must_cover_required_pi_cases()
    test_fixture_pack_must_match_package_binding()
    test_mcp_fixture_pack_must_cover_needle_bash_cases()
    test_claim_card_tested_capability_must_match_claim()
    test_backend_requires_text_interface()
    test_http_backend_requires_explicit_endpoint()
    test_http_backend_must_fail_open()
    print("test_package_config OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
