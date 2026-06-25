"""Needle CLI package management.

Run: PYTHONPATH=. python3 tests/test_cli.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_ROOT = ROOT / "needle" / "registry_data"


def _run(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["uv", "run", "needle", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_typer_help_groups_public_commands() -> None:
    code, out, err = _run(["--help"])
    assert code == 0, err
    assert "Commands" in out
    for command in (
        "package",
        "evidence",
        "model",
        "mcp",
        "runtime",
        "setup",
        "status",
        "statusline",
        "stop",
        "uninstall",
    ):
        assert command in out


def test_cli_version_and_usage_errors_are_human_readable() -> None:
    code, out, err = _run(["--version"])
    assert code == 0, err
    assert out.strip().startswith("needle ")

    code, out, err = _run(["nope"])
    assert code != 0
    assert out == ""
    assert "No such command" in err
    assert "Traceback" not in err


def test_base_cli_dependency_does_not_pull_backend_runtime() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(data["project"].get("dependencies", [])).lower()
    assert "typer" in dependencies
    assert "click" in dependencies
    for backend_dep in ("mlx", "mlx-lm", "numpy", "huggingface-hub", "transformers"):
        assert backend_dep not in dependencies


def test_package_cli_lists_and_selects_packages() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_config = os.environ.get("HAY_CONFIG")
        old_needle_config = os.environ.get("NEEDLE_CONFIG")
        old_package = os.environ.get("HAY_PACKAGE")
        old_needle_package = os.environ.get("NEEDLE_PACKAGE")
        old_registry_root = os.environ.get("HAY_REGISTRY_ROOT")
        old_needle_registry_root = os.environ.get("NEEDLE_REGISTRY_ROOT")
        os.environ["NEEDLE_CONFIG"] = str(Path(td) / "config.json")
        os.environ["NEEDLE_REGISTRY_ROOT"] = str(REGISTRY_ROOT)
        os.environ.pop("HAY_CONFIG", None)
        os.environ.pop("HAY_REGISTRY_ROOT", None)
        os.environ.pop("HAY_PACKAGE", None)
        os.environ.pop("NEEDLE_PACKAGE", None)
        try:
            code, out, err = _run(["package", "list", "--host-binding", "pi/native-tools"])
            assert code == 0, err
            assert "e24z/mlx-pi-reference" in out
            assert "e24z/mlx-pi-soft-lamr" in out
            assert "MLX Pi Soft LAMR" in out
            assert "host:     Pi native tools" in out
            assert "behavior: Soft LAMR" in out
            assert "backend:  local MLX backend" in out
            assert "Use --verbose for registry ids" in out

            code, out, err = _run(["package", "list", "--host-binding", "pi/native-tools", "--verbose"])
            assert code == 0, err
            assert "implements=swe-pruner/reference" in out
            assert "protocol=needle/text-transform" in out
            assert "uses=e24z/code-pruner-mlx" in out

            code, out, err = _run(["package", "use", "e24z/mlx-pi-soft-lamr"])
            assert code == 0, err
            assert "selected package: e24z/mlx-pi-soft-lamr" in out
            assert "runtime command: needle runtime manage" in out
            assert "restart the resident runtime" in out

            code, out, err = _run(["package", "current"])
            assert code == 0, err
            assert out.splitlines()[0] == "e24z/mlx-pi-soft-lamr"
            assert "source: config:" in out

            code, out, err = _run(["package", "current", "--host-binding", "pi/native-tools"])
            assert code == 0, err
            assert out.splitlines()[0] == "e24z/mlx-pi-soft-lamr"
            assert "source: config:" in out
            assert "host binding: pi/native-tools" in out

            code, out, err = _run(["package", "doctor", "--host-binding", "pi/native-tools"])
            assert code == 0, err
            assert "package: e24z/mlx-pi-soft-lamr" in out
            assert "protocol: needle/text-transform" in out
            assert "implements: e24z/soft-lamr" in out
            assert "uses backend: e24z/code-pruner-mlx" in out
            assert "runtime launcher: needle-cli" in out
            assert "runtime command: needle runtime manage" in out
            assert "package graph: ok" in out
            assert "backend requirements: apple_silicon, mlx" in out
            assert "backend readiness:" in out
            assert "evidence: fixture_pack:mlx-pi-soft-lamr" in out
            assert "evidence/fixture-packs/mlx-pi-soft-lamr/manifest.json" in out

            code, out, err = _run(["package", "list", "--host-binding", "mcp/bash"])
            assert code == 0, err
            assert "e24z/mlx-mcp-bash-reference" in out
            assert "host:     MCP bash" in out
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


def test_evidence_check_lists_fixture_cases() -> None:
    code, out, err = _run(["evidence", "check", "e24z/mlx-pi-reference", "--host-binding", "pi/native-tools"])
    assert code == 0, err
    assert "package: e24z/mlx-pi-reference" in out
    assert "evidence: ok (fixture manifests only)" in out
    assert "proof level: local fixtures" in out
    assert "fixture_pack:mlx-pi-reference" in out
    assert "case: read-visible-prune  tool=read  behavior=visible_prune" in out
    assert "case: bash-visible-prune  tool=bash  behavior=visible_prune" in out
    assert "case: read-missing-focus-passthrough  tool=read  behavior=passthrough_original" in out


def test_evidence_check_lists_mcp_fixture_cases() -> None:
    code, out, err = _run(["evidence", "check", "e24z/mlx-mcp-bash-reference", "--host-binding", "mcp/bash"])
    assert code == 0, err
    assert "package: e24z/mlx-mcp-bash-reference" in out
    assert "fixture_pack:mlx-mcp-bash-reference" in out
    assert "case: needle-bash-visible-prune  tool=needle_bash  behavior=visible_prune" in out
    assert (
        "case: needle-bash-missing-focus-passthrough  "
        "tool=needle_bash  behavior=passthrough_original"
    ) in out


def test_uninstall_dry_run_and_yes_use_needle_owned_paths() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        models = root / "models"
        config = root / "config.json"
        home.mkdir()
        models.mkdir()
        config.write_text('{"package": "e24z/mlx-pi-reference"}\n', encoding="utf-8")
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
            assert "needle setup pi --uninstall" in out
            assert "needle setup claude-code --uninstall" in out
            assert "brew uninstall needle" in out
            assert "pipx uninstall needle" in out
            assert "uv tool uninstall needle" in out
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


def test_setup_pi_dry_run_uses_packaged_adapter() -> None:
    code, out, err = _run(["setup", "pi", "--dry-run"])
    assert code == 0, err
    assert "Needle Pi package:" in out
    assert "needle/hosts/pi" in out
    assert "Pi command: pi install" in out
    assert "Canary command: node" in out
    assert "dry run: no changes made" in out
    assert "next: run `needle setup pi`" in out


def test_setup_root_dry_run_lists_hosts_without_mutating() -> None:
    code, out, err = _run(["setup", "--dry-run"])
    assert code == 0, err
    assert "Needle setup" in out
    assert "Pi native adapter" in out
    assert "Claude Code MCP adapter" in out
    assert "Codex MCP dogfood adapter" in out
    assert "package: e24z/mlx-pi-soft-lamr" in out
    assert "setup:   needle setup pi" in out
    assert "setup:   needle setup claude-code" in out
    assert "setup:   needle setup codex" in out
    assert "native:  pi install" in out
    assert "native:  claude mcp add --transport stdio --scope local needle-bash -- needle mcp serve" in out
    assert "native:  codex mcp add needle-bash -- needle mcp serve" in out
    assert "needle model dir" in out
    assert "needle model download" in out
    assert "Needle will not change Pi, Claude Code, or Codex" in out
    assert "dry run: no changes made" in out


def test_setup_homebrew_entrypoint_defers_in_noninteractive_shell() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = os.environ.get("NEEDLE_HOME")
        os.environ["NEEDLE_HOME"] = str(Path(td) / "needle-home")
        try:
            code, out, err = _run(["setup", "--from-homebrew"])
            assert code == 0, err
            assert "Homebrew triggered setup" in out
            assert "pending setup marker:" in out
            marker = Path(os.environ["NEEDLE_HOME"]) / "setup-pending.json"
            assert marker.exists()
            data = json.loads(marker.read_text(encoding="utf-8"))
            assert data["source"] == "homebrew"
            assert data["resume"] == "needle setup"
            assert data["hosts"]["pi"]["setup"] == "needle setup pi"
            assert data["hosts"]["claude-code"]["setup"] == "needle setup claude-code"
            assert data["hosts"]["codex"]["setup"] == "needle setup codex"
        finally:
            if old_home is None:
                os.environ.pop("NEEDLE_HOME", None)
            else:
                os.environ["NEEDLE_HOME"] = old_home


def test_setup_root_rejects_unknown_host() -> None:
    code, out, err = _run(["setup", "--dry-run", "--host", "vibes"])
    assert code == 1
    assert out == ""
    assert "host must be one of" in err


def test_setup_claude_code_dry_run_prints_native_mcp_setup() -> None:
    code, out, err = _run(["setup", "claude-code", "--dry-run"])
    assert code == 0, err
    assert "Needle Claude Code MCP setup" in out
    assert "package: e24z/mlx-mcp-bash-reference" in out
    assert "host binding: mcp/bash" in out
    assert "server command: needle mcp serve" in out
    assert "Claude command: claude mcp add --transport stdio --scope local needle-bash -- needle mcp serve" in out
    assert '"needle-bash"' in out
    assert "dry run: no changes made" in out

    code, out, err = _run(["setup", "claude-code", "--dry-run", "--scope", "project"])
    assert code == 0, err
    assert "Claude scope: project" in out
    assert "Project .mcp.json shape" in out


def test_setup_codex_dry_run_prints_native_mcp_setup() -> None:
    code, out, err = _run(["setup", "codex", "--dry-run"])
    assert code == 0, err
    assert "Needle Codex MCP setup" in out
    assert "package: e24z/mlx-mcp-bash-reference" in out
    assert "host binding: mcp/bash" in out
    assert "server command: needle mcp serve" in out
    assert "Codex command: codex mcp add needle-bash -- needle mcp serve" in out
    assert "Project .codex/config.toml shape" in out
    assert "[mcp_servers.needle-bash]" in out
    assert "Needle does not transparently rewrite Codex's built-in Bash output." in out
    assert "dry run: no changes made" in out


def test_setup_claude_code_rejects_unknown_scope() -> None:
    code, out, err = _run(["setup", "claude-code", "--dry-run", "--scope", "workspace"])
    assert code == 1
    assert out == ""
    assert "scope must be one of" in err


def test_claude_code_statusline_plain_reports_runtime_health() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_socket = os.environ.get("NEEDLE_MANAGER_SOCKET")
        os.environ["NEEDLE_MANAGER_SOCKET"] = str(Path(td) / "missing.sock")
        try:
            code, out, err = _run(["statusline", "claude-code", "--plain"])
            assert code == 0, err
            assert out.strip() == "- needle · down"
        finally:
            if old_socket is None:
                os.environ.pop("NEEDLE_MANAGER_SOCKET", None)
            else:
                os.environ["NEEDLE_MANAGER_SOCKET"] = old_socket


def test_runtime_status_wrapper_is_available() -> None:
    code, out, err = _run(["runtime", "status"])
    assert code == 0, err
    assert "manager:" in out


def test_stop_is_idempotent_when_runtime_is_down() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_socket = os.environ.get("NEEDLE_MANAGER_SOCKET")
        os.environ["NEEDLE_MANAGER_SOCKET"] = str(Path(td) / "missing.sock")
        try:
            code, out, err = _run(["stop"])
            assert code == 0
            assert out == ""
            assert "already stopped" in err

            code, out, err = _run(["runtime", "stop"])
            assert code == 0
            assert out == ""
            assert "already stopped" in err
        finally:
            if old_socket is None:
                os.environ.pop("NEEDLE_MANAGER_SOCKET", None)
            else:
                os.environ["NEEDLE_MANAGER_SOCKET"] = old_socket


def main() -> int:
    test_typer_help_groups_public_commands()
    test_cli_version_and_usage_errors_are_human_readable()
    test_base_cli_dependency_does_not_pull_backend_runtime()
    test_package_cli_lists_and_selects_packages()
    test_package_cli_rejects_unknown_package()
    test_evidence_check_lists_fixture_cases()
    test_evidence_check_lists_mcp_fixture_cases()
    test_uninstall_dry_run_and_yes_use_needle_owned_paths()
    test_model_dir_command_reports_needle_model_path()
    test_setup_root_dry_run_lists_hosts_without_mutating()
    test_setup_homebrew_entrypoint_defers_in_noninteractive_shell()
    test_setup_root_rejects_unknown_host()
    test_setup_pi_dry_run_uses_packaged_adapter()
    test_setup_claude_code_dry_run_prints_native_mcp_setup()
    test_setup_codex_dry_run_prints_native_mcp_setup()
    test_setup_claude_code_rejects_unknown_scope()
    test_claude_code_statusline_plain_reports_runtime_health()
    test_runtime_status_wrapper_is_available()
    test_stop_is_idempotent_when_runtime_is_down()
    print("test_cli OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
