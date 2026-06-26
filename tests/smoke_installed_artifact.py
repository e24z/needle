"""Build and install Needle, then verify packaged runtime assets.

Run:
    python3 tests/smoke_installed_artifact.py

This is intentionally not named ``test_*.py`` because it builds an artifact,
creates a temporary virtualenv, and installs dependencies. Use it as a release
smoke, not as part of the tiny pure test loop.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    uv = shutil.which("uv")
    if not uv:
        print("error: uv is required for installed artifact smoke", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="needle-install-smoke-") as td:
        tmp = Path(td)
        dist = tmp / "dist"
        venv = tmp / "venv"

        run([uv, "build", "--out-dir", str(dist)])
        wheel = one(dist.glob("needle-*.whl"), "wheel")
        sdist = one(dist.glob("needle-*.tar.gz"), "sdist")
        check_wheel_contents(wheel)
        check_sdist_contents(sdist)

        run([sys.executable, "-m", "venv", str(venv)])
        python = venv / "bin" / "python"
        needle = venv / "bin" / "needle"
        run([uv, "pip", "install", "--python", str(python), str(wheel)])

        env = {**os.environ, "NEEDLE_HOME": str(tmp / "home")}
        commands = [
            [str(needle), "--help"],
            [str(needle), "package", "doctor", "--host-binding", "pi/native-tools"],
            [str(needle), "evidence", "check", "--host-binding", "pi/native-tools"],
            [str(needle), "setup", "pi", "--dry-run"],
            [str(needle), "setup", "claude-code", "--dry-run"],
            [str(needle), "setup", "codex", "--dry-run"],
        ]
        for command in commands:
            run(command, env=env)

        check_installed_files(python, env)

    print("smoke_installed_artifact OK")
    return 0


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    display = " ".join(command)
    if "\n" in display or len(display) > 220:
        display = f"{command[0]} ... {command[-1].splitlines()[0][:80]}"
    print("+", display, flush=True)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode == 0:
        return
    if completed.stdout:
        print(completed.stdout, file=sys.stdout)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr)
    raise subprocess.CalledProcessError(completed.returncode, command)


def one(paths, label: str) -> Path:
    matches = sorted(paths)
    if len(matches) != 1:
        raise AssertionError(f"expected one {label}, found {matches}")
    return matches[0]


def check_wheel_contents(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    required = {
        "needle/cli.py",
        "needle/registry.py",
        "needle/hosts/pi/extension.js",
        "needle/hosts/pi/client.mjs",
        "needle/hosts/mcp/server.py",
        "needle/hosts/mcp/README.md",
        "needle/registry_data/packages/e24z/mlx-pi-soft-lamr.yaml",
        "needle/registry_data/package-cards/e24z/mlx-pi-soft-lamr.md",
    }
    missing = sorted(required - names)
    if missing:
        raise AssertionError(f"wheel is missing packaged files: {missing}")
    blocked = sorted(
        name for name in names if name.startswith(("docs/", "tools/", "pruner/", "src/"))
    )
    if blocked:
        raise AssertionError(f"wheel includes source-only paths: {blocked[:10]}")


def check_sdist_contents(sdist: Path) -> None:
    names = set(
        subprocess.check_output(["tar", "-tzf", str(sdist)], text=True).splitlines()
    )
    required_suffixes = {
        "/pyproject.toml",
        "/README.md",
        "/src/needle/cli.py",
        "/src/needle/hosts/pi/extension.js",
        "/src/needle/registry_data/packages/e24z/mlx-pi-soft-lamr.yaml",
    }
    missing = [
        suffix for suffix in required_suffixes if not any(name.endswith(suffix) for name in names)
    ]
    if missing:
        raise AssertionError(f"sdist is missing source files: {missing}")


def check_installed_files(python: Path, env: dict[str, str]) -> None:
    script = """
from pathlib import Path
import needle

root = Path(needle.__file__).resolve().parent
required = [
    root / "cli.py",
    root / "hosts/pi/extension.js",
    root / "hosts/pi/package.json",
    root / "hosts/mcp/server.py",
    root / "registry_data/packages/e24z/mlx-pi-soft-lamr.yaml",
    root / "registry_data/evidence/fixture-packs/mlx-pi-soft-lamr/manifest.json",
]
missing = [str(path) for path in required if not path.exists()]
if missing:
    raise SystemExit(f"missing installed files: {missing}")
print(root)
"""
    run([str(python), "-c", script], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
