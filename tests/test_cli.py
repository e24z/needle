"""Needle thin-spine CLI surface.

Run: PYTHONPATH=src python3 tests/test_cli.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str]) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["COLUMNS"] = "120"
    proc = subprocess.run(
        ["uv", "run", "needle", *args],
        cwd=ROOT,
        env=env,
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
    assert "package" not in out
    assert "evidence" not in out


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


def test_uninstall_dry_run_and_yes_use_needle_owned_paths() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        home = root / "home"
        models = root / "models"
        home.mkdir()
        models.mkdir()
        (home / "events.jsonl").write_text("", encoding="utf-8")
        old_env = {
            name: os.environ.get(name)
            for name in (
                "NEEDLE_HOME",
                "NEEDLE_MODEL_ROOT",
                "NEEDLE_MANAGER_SOCKET",
            )
        }
        os.environ["NEEDLE_HOME"] = str(home)
        os.environ["NEEDLE_MODEL_ROOT"] = str(models)
        os.environ["NEEDLE_MANAGER_SOCKET"] = str(root / "missing.sock")
        try:
            code, out, err = _run(["uninstall"])
            assert code == 0, err
            assert str(home) in out
            assert str(models) in out
            assert home.exists()
            assert models.exists()

            code, out, err = _run(["uninstall", "--yes"])
            assert code == 0, err
            assert "removed Needle-owned local state" in out
            assert "needle setup claude-code --uninstall" in out
            assert "Codex:  needle setup codex --uninstall" in out
            assert "setup pi" not in out
            assert "brew uninstall needle" in out
            assert "pipx uninstall needle" in out
            assert "uv tool uninstall needle" in out
            assert not home.exists()
            assert not models.exists()
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


def test_model_provenance_records_resolved_revision() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        script = """
import json
import sys
from pathlib import Path

import needle.cli as cli

target = Path(sys.argv[1])
cli._write_model_provenance(
    target,
    repo="org/model",
    requested_revision="main",
    resolved_revision="abc123",
)
print((target / "needle-model.json").read_text(encoding="utf-8"))
"""
        proc = subprocess.run(
            ["uv", "run", "python", "-c", script, str(target)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
        data = json.loads(proc.stdout)
        assert data["repo"] == "org/model"
        assert data["requested_revision"] == "main"
        assert data["resolved_revision"] == "abc123"
        assert data["caller"] == "cli"
        assert "downloaded_at" in data


def test_setup_root_dry_run_lists_hosts_without_mutating() -> None:
    code, out, err = _run(["setup", "--dry-run"])
    assert code == 0, err
    assert "Needle setup" in out
    assert "Runtime: mlx-soft-lamr (code-pruner-mlx, local_mlx_adaptive)" in out
    assert "Tool surface: needle_bash through MCP" in out
    assert "claude-code: Claude Code (MCP tool named needle_bash)" in out
    assert "codex: Codex CLI (MCP tool named needle_bash)" in out
    assert "setup:   needle setup claude-code" in out
    assert "setup:   needle setup codex" in out
    assert "command: claude mcp add --transport stdio --scope local needle-bash -- needle mcp serve" in out
    assert "command: codex mcp add needle-bash -- needle mcp serve" in out
    assert "Needle will not change Claude Code or Codex until you confirm setup." in out
    assert "pi" not in out.lower()
    assert "package:" not in out
    assert "dry run: no changes made" in out


def test_setup_homebrew_entrypoint_defers_in_noninteractive_shell() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_home = os.environ.get("NEEDLE_HOME")
        os.environ["NEEDLE_HOME"] = str(Path(td) / "needle-home")
        try:
            code, out, err = _run(["setup", "--from-homebrew"])
            assert code == 0, err
            assert "Homebrew installed Needle" in out
            assert "pending setup marker:" in out
            marker = Path(os.environ["NEEDLE_HOME"]) / "setup-pending.json"
            assert marker.exists()
            data = json.loads(marker.read_text(encoding="utf-8"))
            assert data["source"] == "homebrew"
            assert data["resume"] == "needle setup"
            assert data["runtime"] == "mlx-soft-lamr"
            assert set(data["hosts"]) == {"claude-code", "codex"}
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
    assert "Needle for Claude Code" in out
    assert "Tool name: needle_bash" in out
    assert "Tool command: needle mcp serve" in out
    assert "Runtime command: needle runtime manage" in out
    assert "Runtime: mlx-soft-lamr (code-pruner-mlx, local_mlx_adaptive)" in out
    assert "package" not in out.lower()
    assert "--host-binding" not in out
    assert "Statusline: needle statusline claude-code" in out
    assert "Status: needle status --events 20" in out
    assert "start the Needle runtime before expecting pruning." in out
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
    assert "Needle for Codex CLI" in out
    assert "Tool name: needle_bash" in out
    assert "Tool command: needle mcp serve" in out
    assert "Runtime command: needle runtime manage" in out
    assert "Runtime: mlx-soft-lamr (code-pruner-mlx, local_mlx_adaptive)" in out
    assert "package" not in out.lower()
    assert "--host-binding" not in out
    assert "Statusline: needle statusline codex" in out
    assert "Status: needle status --events 20" in out
    assert "Codex support is experimental" in out
    assert "Codex command: codex mcp add needle-bash -- needle mcp serve" in out
    assert "Project .codex/config.toml shape" in out
    assert "[mcp_servers.needle-bash]" in out
    assert "Codex note:" in out
    assert "Needle does not transparently rewrite Codex's built-in Bash output." in out
    assert "dry run: no changes made" in out


def test_setup_claude_code_rejects_unknown_scope() -> None:
    code, out, err = _run(["setup", "claude-code", "--dry-run", "--scope", "workspace"])
    assert code == 1
    assert out == ""
    assert "scope must be one of" in err


def test_statusline_plain_reports_runtime_health() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_socket = os.environ.get("NEEDLE_MANAGER_SOCKET")
        os.environ["NEEDLE_MANAGER_SOCKET"] = str(Path(td) / "missing.sock")
        try:
            code, out, err = _run(["statusline", "claude-code", "--plain"])
            assert code == 0, err
            assert out.strip() == "- needle · down"

            code, out, err = _run(["statusline", "codex", "--plain"])
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


def test_runtime_manage_help_exposes_only_raw_context_option() -> None:
    code, out, err = _run(["runtime", "manage", "--help"])
    assert code == 0, err
    assert "Usage:" in out

    script = """
import json
import typer
import needle.cli as cli

command = typer.main.get_command(cli.app).commands["runtime"].commands["manage"]
options = sorted({opt for param in command.params for opt in param.opts})
print(json.dumps(options))
"""
    proc = subprocess.run(
        ["uv", "run", "python", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    options = set(json.loads(proc.stdout))
    assert "--raw" in options
    assert "--package" not in options
    assert "--host-binding" not in options


def test_runtime_manage_wrapper_forwards_raw_without_starting_manager() -> None:
    script = """
import json

from typer.testing import CliRunner

import needle.cli as cli
import needle.runtime.cli as runtime_cli

seen = []

def fake_main(argv):
    seen.append(argv)
    return 0

runtime_cli.main = fake_main
result = CliRunner().invoke(cli.app, ["runtime", "manage", "--raw"])
if result.exit_code != 0:
    raise SystemExit(result.output)
print(json.dumps(seen))
"""
    proc = subprocess.run(
        ["uv", "run", "python", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert json.loads(proc.stdout) == [["manage", "--raw"]]


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
    test_uninstall_dry_run_and_yes_use_needle_owned_paths()
    test_model_dir_command_reports_needle_model_path()
    test_model_provenance_records_resolved_revision()
    test_setup_root_dry_run_lists_hosts_without_mutating()
    test_setup_homebrew_entrypoint_defers_in_noninteractive_shell()
    test_setup_root_rejects_unknown_host()
    test_setup_claude_code_dry_run_prints_native_mcp_setup()
    test_setup_codex_dry_run_prints_native_mcp_setup()
    test_setup_claude_code_rejects_unknown_scope()
    test_statusline_plain_reports_runtime_health()
    test_runtime_status_wrapper_is_available()
    test_runtime_manage_help_exposes_only_raw_context_option()
    test_runtime_manage_wrapper_forwards_raw_without_starting_manager()
    test_stop_is_idempotent_when_runtime_is_down()
    print("test_cli OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
