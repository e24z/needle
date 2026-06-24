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
    PackageConfigError,
    active_package_selection,
    configured_package_id,
    load_active_package,
    package_summaries,
    runtime_launch_plan,
    set_configured_package_id,
)


ROOT = Path(__file__).resolve().parent.parent


def _copy_registry(tmp: Path) -> None:
    for name in ("protocols", "capabilities", "backends", "bindings", "packages", "claims", "package-cards"):
        src = ROOT / name
        if src.exists():
            shutil.copytree(src, tmp / name)


def test_default_package_graph_loads() -> None:
    loaded = load_active_package(ROOT, "e24z/pi-local-mac")
    assert loaded.package_id == "e24z/pi-local-mac"
    assert loaded.protocol["id"] == "needle/text-transform"
    assert loaded.capability_ids == ["swe-pruner/reference"]
    assert loaded.backend_id == "e24z/code-pruner-mlx"
    assert loaded.binding_id == "pi/native-tools"
    assert loaded.claim_card["capability"] == "swe-pruner/reference"
    assert loaded.package_card_path.exists()


def test_default_package_resolves_runtime_launch_plan() -> None:
    plan = runtime_launch_plan(ROOT, "e24z/pi-local-mac")
    assert plan.package_id == "e24z/pi-local-mac"
    assert plan.backend_id == "e24z/code-pruner-mlx"
    assert plan.kind == "uv-python-module"
    assert plan.extra == "backend-code-pruner-mlx"
    assert plan.module == "needle.runtime"
    assert plan.args == ["manage"]
    assert plan.command == [
        "uv",
        "run",
        "--extra",
        "backend-code-pruner-mlx",
        "-m",
        "needle.runtime",
        "manage",
    ]
    assert plan.env["NEEDLE_BACKEND"] == "e24z/code-pruner-mlx"
    assert plan.env["HAY_BACKEND"] == "code-pruner"


def test_reference_capability_has_no_ast_repair() -> None:
    loaded = load_active_package(ROOT, "e24z/pi-local-mac")
    ref = loaded.capabilities["swe-pruner/reference"]
    assert ref["claim_scope"]["ast_repair"] == "absent"
    assert ref["focus"]["field"] == "context_focus_question"
    assert ref["focus"]["missing"] == "passthrough_original"
    assert ref["rendering"]["marker_format"] == "(filtered {line_count} lines)"


def test_soft_lamr_is_separate_capability() -> None:
    path = ROOT / "capabilities/e24z/soft-lamr.yaml"
    soft = json.loads(path.read_text(encoding="utf-8"))
    assert soft["extends"] == "swe-pruner/reference"
    assert soft["overrides"]["ast_repair"] == "python"


def test_soft_lamr_package_resolves_parent_protocol() -> None:
    loaded = load_active_package(ROOT, "e24z/pi-local-mac-soft-lamr")
    assert loaded.package_id == "e24z/pi-local-mac-soft-lamr"
    assert loaded.protocol["id"] == "needle/text-transform"
    assert loaded.capability_ids == ["e24z/soft-lamr"]
    assert loaded.capabilities["e24z/soft-lamr"]["extends"] == "swe-pruner/reference"
    assert loaded.backend_id == "e24z/code-pruner-mlx"
    assert loaded.binding_id == "pi/native-tools"
    assert loaded.claim_card["capability"] == "e24z/soft-lamr"
    assert loaded.package_card_path.exists()


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
            selected = set_configured_package_id("e24z/pi-local-mac-soft-lamr", root=ROOT)
            assert selected.package_id == "e24z/pi-local-mac-soft-lamr"
            assert configured_package_id(host_binding="pi/native-tools") == "e24z/pi-local-mac-soft-lamr"
            assert active_package_selection(host_binding="pi/native-tools")[0] == "e24z/pi-local-mac-soft-lamr"
            assert load_active_package(ROOT, host_binding="pi/native-tools").package_id == "e24z/pi-local-mac-soft-lamr"

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
    summaries = package_summaries(ROOT, host_binding="pi/native-tools")
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


def main() -> int:
    test_default_package_graph_loads()
    test_default_package_resolves_runtime_launch_plan()
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
    test_claim_card_tested_capability_must_match_claim()
    test_backend_requires_text_interface()
    print("test_package_config OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
