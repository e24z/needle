"""Static Needle package registry and loader.

Run: PYTHONPATH=. python3 tests/test_package_config.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def test_default_package_graph_loads() -> None:
    loaded = load_active_package(REGISTRY_ROOT, "e24z/pi-local-mac")
    assert loaded.package_id == "e24z/pi-local-mac"
    assert loaded.protocol["id"] == "needle/text-transform"
    assert loaded.capability_ids == ["swe-pruner/reference"]
    assert loaded.backend_id == "e24z/code-pruner-mlx"
    assert loaded.binding_id == "pi/native-tools"
    assert loaded.claim_card["capability"] == "swe-pruner/reference"
    assert loaded.package_card_path.exists()
    assert loaded.evidence_refs == ["fixture_pack:swe-pruner-reference"]
    assert loaded.evidence_paths["fixture_pack:swe-pruner-reference"].exists()


def test_default_package_resolves_runtime_launch_plan() -> None:
    plan = runtime_launch_plan(REGISTRY_ROOT, "e24z/pi-local-mac")
    assert plan.package_id == "e24z/pi-local-mac"
    assert plan.backend_id == "e24z/code-pruner-mlx"
    assert plan.kind == "needle-cli"
    assert plan.command == [
        "needle",
        "runtime",
        "manage",
    ]
    assert plan.env["NEEDLE_BACKEND"] == "e24z/code-pruner-mlx"
    assert plan.env["HAY_BACKEND"] == "code-pruner"


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
    loaded = load_active_package(REGISTRY_ROOT, "e24z/pi-local-mac")
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
    loaded = load_active_package(REGISTRY_ROOT, "e24z/pi-local-mac-soft-lamr")
    assert loaded.package_id == "e24z/pi-local-mac-soft-lamr"
    assert loaded.protocol["id"] == "needle/text-transform"
    assert loaded.capability_ids == ["e24z/soft-lamr"]
    assert loaded.capabilities["e24z/soft-lamr"]["extends"] == "swe-pruner/reference"
    assert loaded.backend_id == "e24z/code-pruner-mlx"
    assert loaded.binding_id == "pi/native-tools"
    assert loaded.claim_card["capability"] == "e24z/soft-lamr"
    assert loaded.package_card_path.exists()
    assert loaded.evidence_refs == ["fixture_pack:needle-soft-lamr"]
    assert loaded.evidence_paths["fixture_pack:needle-soft-lamr"].exists()


def test_missing_backend_reference_fails_clearly() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)

        package_path = tmp / "packages/e24z/pi-local-mac.yaml"
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
        backend["supports"] = ["e24z/soft-lamr"]
        backend_path.write_text(json.dumps(backend, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected unsupported capability to fail")

    assert "does not support package capabilities" in msg
    assert "swe-pruner/reference" in msg


def test_registry_root_and_package_can_come_from_environment() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)

        old_root = os.environ.get("HAY_REGISTRY_ROOT")
        old_package = os.environ.get("HAY_PACKAGE")
        os.environ["HAY_REGISTRY_ROOT"] = str(tmp)
        os.environ["HAY_PACKAGE"] = "e24z/pi-local-mac"
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

    assert loaded.package_id == "e24z/pi-local-mac"
    assert loaded.backend_id == "e24z/code-pruner-mlx"


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
            selected = set_configured_package_id("e24z/pi-local-mac-soft-lamr", root=REGISTRY_ROOT)
            assert selected.package_id == "e24z/pi-local-mac-soft-lamr"
            assert configured_package_id() == "e24z/pi-local-mac-soft-lamr"
            assert configured_package_id(host_binding="pi/native-tools") == "e24z/pi-local-mac-soft-lamr"
            assert active_package_selection()[0] == "e24z/pi-local-mac-soft-lamr"
            assert active_package_selection(host_binding="pi/native-tools")[0] == "e24z/pi-local-mac-soft-lamr"
            assert load_active_package(REGISTRY_ROOT, host_binding="pi/native-tools").package_id == "e24z/pi-local-mac-soft-lamr"

            os.environ["HAY_PACKAGE"] = "e24z/pi-local-mac"
            os.environ["NEEDLE_PACKAGE"] = "e24z/pi-local-mac-soft-lamr"
            package_id, source = active_package_selection()
            assert package_id == "e24z/pi-local-mac-soft-lamr"
            assert source == "env:NEEDLE_PACKAGE"

            os.environ.pop("NEEDLE_PACKAGE", None)
            package_id, source = active_package_selection()
            assert package_id == "e24z/pi-local-mac"
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
    assert "e24z/pi-local-mac" in ids
    assert "e24z/pi-local-mac-soft-lamr" in ids
    assert all(item["host_binding"] == "pi/native-tools" for item in summaries)


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
        package_path = tmp / "packages/e24z/pi-local-mac-soft-lamr.yaml"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["host_binding"] = "example/other"
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")

        try:
            load_active_package(tmp, "e24z/pi-local-mac-soft-lamr", host_binding="pi/native-tools")
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected host binding mismatch to fail")

    assert "not requested host binding" in msg


def test_package_requires_focus_contract() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        package_path = tmp / "packages/e24z/pi-local-mac.yaml"
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
        package_path = tmp / "packages/e24z/pi-local-mac.yaml"
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
        evidence_path = tmp / "evidence/fixture-packs/swe-pruner-reference/manifest.json"
        evidence_path.unlink()

        try:
            load_active_package(tmp)
        except PackageConfigError as exc:
            msg = str(exc)
        else:
            raise AssertionError("expected missing evidence manifest to fail")

    assert "missing evidence reference 'fixture_pack:swe-pruner-reference'" in msg


def test_fixture_pack_must_cover_required_pi_cases() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        manifest_path = tmp / "evidence/fixture-packs/swe-pruner-reference/manifest.json"
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


def test_claim_card_tested_capability_must_match_claim() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _copy_registry(tmp)
        claim_path = tmp / "claims/pi-local-mac-swe-pruner-reference.yaml"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim["tested"]["capability"] = "e24z/soft-lamr"
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
    test_default_package_graph_loads()
    test_default_package_resolves_runtime_launch_plan()
    test_http_backend_contract_validates_without_server()
    test_reference_capability_has_no_ast_repair()
    test_soft_lamr_is_separate_capability()
    test_soft_lamr_package_resolves_parent_protocol()
    test_missing_backend_reference_fails_clearly()
    test_backend_must_support_package_capabilities()
    test_registry_root_and_package_can_come_from_environment()
    test_package_selection_can_come_from_user_config()
    test_package_summaries_can_filter_by_host_binding()
    test_host_scoped_load_rejects_wrong_binding()
    test_package_requires_focus_contract()
    test_binding_tool_mapping_must_use_known_artifact_kind()
    test_package_rejects_unknown_evidence_reference()
    test_package_rejects_missing_evidence_reference()
    test_fixture_pack_must_cover_required_pi_cases()
    test_claim_card_tested_capability_must_match_claim()
    test_backend_requires_text_interface()
    test_http_backend_requires_explicit_endpoint()
    test_http_backend_must_fail_open()
    print("test_package_config OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
