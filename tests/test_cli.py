"""Stdlib CLI package management.

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

from pruner.cli import main as cli_main  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str]) -> tuple[int, str, str]:
    out = StringIO()
    err = StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_main(args)
    return code, out.getvalue(), err.getvalue()


def test_package_cli_lists_and_selects_packages() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_config = os.environ.get("HAY_CONFIG")
        old_package = os.environ.get("HAY_PACKAGE")
        old_needle_package = os.environ.get("NEEDLE_PACKAGE")
        old_registry_root = os.environ.get("HAY_REGISTRY_ROOT")
        old_needle_registry_root = os.environ.get("NEEDLE_REGISTRY_ROOT")
        os.environ["HAY_CONFIG"] = str(Path(td) / "config.json")
        os.environ["HAY_REGISTRY_ROOT"] = str(ROOT)
        os.environ.pop("HAY_PACKAGE", None)
        os.environ.pop("NEEDLE_PACKAGE", None)
        os.environ.pop("NEEDLE_REGISTRY_ROOT", None)
        try:
            code, out, err = _run(["package", "list", "--host-binding", "pi/native-tools"])
            assert code == 0, err
            assert "e24z/pi-local-mac" in out
            assert "e24z/pi-local-mac-soft-lamr" in out

            code, out, err = _run(["package", "use", "e24z/pi-local-mac-soft-lamr"])
            assert code == 0, err
            assert "selected package: e24z/pi-local-mac-soft-lamr" in out
            assert "restart the manager" in out

            code, out, err = _run(["package", "current"])
            assert code == 0, err
            assert out.splitlines()[0] == "e24z/pi-local-mac-soft-lamr"
            assert "source: config:" in out

            code, out, err = _run(["package", "doctor"])
            assert code == 0, err
            assert "package: e24z/pi-local-mac-soft-lamr" in out
            assert "protocol: needle/text-transform" in out
            assert "capability: e24z/soft-lamr" in out
            assert "backend: e24z/code-pruner-mlx" in out
        finally:
            if old_config is None:
                os.environ.pop("HAY_CONFIG", None)
            else:
                os.environ["HAY_CONFIG"] = old_config
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


def main() -> int:
    test_package_cli_lists_and_selects_packages()
    test_package_cli_rejects_unknown_package()
    print("test_cli OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
