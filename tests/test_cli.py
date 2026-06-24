"""Needle CLI package management.

Run: PYTHONPATH=. python3 tests/test_cli.py
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from needle.cli import main as needle_main  # noqa: E402
from pruner.cli import main as pruner_main  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str]) -> tuple[int, str, str]:
    out = StringIO()
    err = StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = needle_main(args)
    return code, out.getvalue(), err.getvalue()


def test_package_cli_lists_and_selects_packages() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_config = os.environ.get("HAY_CONFIG")
        old_needle_config = os.environ.get("NEEDLE_CONFIG")
        old_package = os.environ.get("HAY_PACKAGE")
        old_needle_package = os.environ.get("NEEDLE_PACKAGE")
        old_registry_root = os.environ.get("HAY_REGISTRY_ROOT")
        old_needle_registry_root = os.environ.get("NEEDLE_REGISTRY_ROOT")
        os.environ["NEEDLE_CONFIG"] = str(Path(td) / "config.json")
        os.environ["NEEDLE_REGISTRY_ROOT"] = str(ROOT)
        os.environ.pop("HAY_CONFIG", None)
        os.environ.pop("HAY_REGISTRY_ROOT", None)
        os.environ.pop("HAY_PACKAGE", None)
        os.environ.pop("NEEDLE_PACKAGE", None)
        try:
            code, out, err = _run(["package", "list", "--host-binding", "pi/native-tools"])
            assert code == 0, err
            assert "e24z/pi-local-mac" in out
            assert "e24z/pi-local-mac-soft-lamr" in out
            assert "implements=swe-pruner/reference" in out
            assert "protocol=needle/text-transform" in out
            assert "uses=e24z/code-pruner-mlx" in out

            code, out, err = _run(["package", "use", "e24z/pi-local-mac-soft-lamr"])
            assert code == 0, err
            assert "selected package: e24z/pi-local-mac-soft-lamr" in out
            assert "runtime extra: backend-code-pruner-mlx" in out
            assert "runtime command: uv run --extra backend-code-pruner-mlx -m needle.runtime manage" in out
            assert "restart the resident runtime" in out

            code, out, err = _run(["package", "current", "--host-binding", "pi/native-tools"])
            assert code == 0, err
            assert out.splitlines()[0] == "e24z/pi-local-mac-soft-lamr"
            assert "source: config:" in out
            assert "host binding: pi/native-tools" in out

            code, out, err = _run(["package", "doctor", "--host-binding", "pi/native-tools"])
            assert code == 0, err
            assert "package: e24z/pi-local-mac-soft-lamr" in out
            assert "protocol: needle/text-transform" in out
            assert "implements: e24z/soft-lamr" in out
            assert "uses backend: e24z/code-pruner-mlx" in out
            assert "runtime extra: backend-code-pruner-mlx" in out
            assert "runtime command: uv run --extra backend-code-pruner-mlx -m needle.runtime manage" in out
            assert "evidence: fixture_pack:needle-soft-lamr" in out
            assert "evidence/fixture-packs/needle-soft-lamr/manifest.json" in out
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
            if old_registry_root is None:
                os.environ.pop("HAY_REGISTRY_ROOT", None)
            else:
                os.environ["HAY_REGISTRY_ROOT"] = old_registry_root
            if old_needle_registry_root is None:
                os.environ.pop("NEEDLE_REGISTRY_ROOT", None)
            else:
                os.environ["NEEDLE_REGISTRY_ROOT"] = old_needle_registry_root


def test_package_cli_rejects_unknown_package() -> None:
    code, out, err = _run(["package", "use", "e24z/nope"])
    assert code == 1
    assert out == ""
    assert "missing package object" in err


def test_uninstall_dry_run_and_yes_use_needle_owned_paths() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        models = root / "models"
        config = root / "config.json"
        home.mkdir()
        models.mkdir()
        config.write_text('{"package": "e24z/pi-local-mac"}\n', encoding="utf-8")
        (home / "events.jsonl").write_text("", encoding="utf-8")
        old_env = {
            name: os.environ.get(name)
            for name in (
                "NEEDLE_HOME",
                "NEEDLE_MODEL_ROOT",
                "NEEDLE_CONFIG",
                "NEEDLE_MANAGER_SOCKET",
            )
        }
        os.environ["NEEDLE_HOME"] = str(home)
        os.environ["NEEDLE_MODEL_ROOT"] = str(models)
        os.environ["NEEDLE_CONFIG"] = str(config)
        os.environ["NEEDLE_MANAGER_SOCKET"] = str(root / "missing.sock")
        try:
            code, out, err = _run(["uninstall"])
            assert code == 0, err
            assert str(home) in out
            assert str(models) in out
            assert str(config) in out
            assert home.exists()
            assert models.exists()
            assert config.exists()

            code, out, err = _run(["uninstall", "--yes"])
            assert code == 0, err
            assert "removed Needle-owned local state" in out
            assert "pi uninstall ." in out
            assert "uv tool uninstall needle" in out
            assert "Claude" not in out
            assert not home.exists()
            assert not models.exists()
            assert not config.exists()
        finally:
            for name, value in old_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_model_dir_command_reports_needle_model_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_root = os.environ.get("NEEDLE_MODEL_ROOT")
        os.environ["NEEDLE_MODEL_ROOT"] = str(Path(td) / "models")
        try:
            code, out, err = _run(["model", "dir"])
            assert code == 0, err
            assert "ayanami-kitasan--code-pruner" in out
            assert str(Path(td) / "models") in out
        finally:
            if old_root is None:
                os.environ.pop("NEEDLE_MODEL_ROOT", None)
            else:
                os.environ["NEEDLE_MODEL_ROOT"] = old_root


def test_pruner_cli_does_not_own_packages() -> None:
    out = StringIO()
    err = StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            pruner_main(["package", "list"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("pruner package list should not exist")
    assert "invalid choice" in err.getvalue()


def main() -> int:
    test_package_cli_lists_and_selects_packages()
    test_package_cli_rejects_unknown_package()
    test_uninstall_dry_run_and_yes_use_needle_owned_paths()
    test_model_dir_command_reports_needle_model_path()
    test_pruner_cli_does_not_own_packages()
    print("test_cli OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
