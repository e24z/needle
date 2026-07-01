"""Needle's public CLI for the thin-spine runtime."""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import click
import typer

from . import __version__
from .runtime import client, events, naming
from .runtime.config import DEFAULT_RUNTIME, runtime_manage_command


app = typer.Typer(
    name="needle",
    help="Needle runtime and MCP control plane.",
    no_args_is_help=True,
    invoke_without_command=True,
)
model_app = typer.Typer(help="Inspect, download, or remove local model files.", no_args_is_help=True)
runtime_app = typer.Typer(help="Low-level runtime commands used by MCP connections.", no_args_is_help=True)
mcp_app = typer.Typer(help="Run Needle MCP servers used by Claude Code, Codex, and other MCP clients.", no_args_is_help=True)
setup_app = typer.Typer(
    help="Connect Needle's MCP server to supported coding agents.",
    invoke_without_command=True,
)
statusline_app = typer.Typer(help="Render compact Needle statusline output.", no_args_is_help=True)

app.add_typer(model_app, name="model")
app.add_typer(runtime_app, name="runtime")
app.add_typer(mcp_app, name="mcp")
app.add_typer(setup_app, name="setup")
app.add_typer(statusline_app, name="statusline")


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show Needle's version and exit.",
        is_eager=True,
    ),
) -> None:
    """Needle runtime and MCP control plane."""
    if version:
        print(f"needle {__version__}")
        raise typer.Exit()


def _print_error(message: object) -> None:
    print(f"error: {message}", file=sys.stderr)


def _exit_with(code: int) -> None:
    if code:
        raise typer.Exit(code=code)


def _ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


_PRESSURE = {1: "normal", 2: "warning", 4: "critical"}


def _fmt_ts(ts: object) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _render_status(stats: dict | None, recent: list[dict]) -> str:
    from .runtime.cli import _render_prune_summary

    lines: list[str] = []
    if not stats or not stats.get("ok"):
        lines.append("Needle runtime: down (not running)")
    else:
        backend = stats.get("backend")
        if not stats.get("resident"):
            state = "cold (model not loaded)"
        elif isinstance(backend, str) and backend.startswith("fake ("):
            state = f"DEGRADED ({backend})"
        else:
            state = f"ready ({backend} resident)"
        avail = stats.get("available_mb")
        free = f"{avail / 1024:.1f} GB" if isinstance(avail, (int, float)) else "?"
        lines.append(f"Needle runtime: {state}")
        lines.append(
            f"  sessions {stats.get('sessions', 0)}"
            f"  ·  version {str(stats.get('version', ''))[:12]}"
            f"  ·  pressure {_PRESSURE.get(stats.get('pressure'), '?')}"
            f"  ·  free {free}"
        )
        identity_bits = [
            f"runtime {stats.get('runtime_id')}" if stats.get("runtime_id") else None,
            f"surface {stats.get('tool_surface')}" if stats.get("tool_surface") else None,
            f"profile {stats.get('runtime_profile')}" if stats.get("runtime_profile") else None,
            f"backend-id {stats.get('backend_id')}" if stats.get("backend_id") else None,
        ]
        identity_line = "  " + "  ·  ".join(bit for bit in identity_bits if bit)
        if identity_line.strip():
            lines.append(identity_line)
        last_prune = stats.get("last_prune")
        detail = _render_prune_summary(last_prune)
        if detail:
            backend = last_prune.get("backend") if isinstance(last_prune, dict) else None
            prefix = f"{backend} · " if isinstance(backend, str) and backend else ""
            lines.append(f"  last prune: {prefix}{detail}")
    if recent:
        lines.append("")
        lines.append("recent events:")
        for event in recent:
            extra = " ".join(
                f"{key}={value}"
                for key, value in event.items()
                if key not in {"ts", "event"}
            )
            lines.append(f"  {_fmt_ts(event.get('ts'))}  {str(event.get('event', '?')):<16} {extra}")
    return "\n".join(lines)


def _status(args: argparse.Namespace) -> int:
    try:
        stats = client.stats(timeout=0.5)
    except (OSError, RuntimeError):
        stats = None
    print(_render_status(stats, events.tail(args.events)))
    return 0


_ACTIVE_SECS = 3.0
_SEP = " · "
_SPIN_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_PULSE_FRAMES = ["⠤", "⠶", "⠿", "⠶"]
_STATUSLINE_COLORS = {
    "down": "38;5;240",
    "cold": "38;5;67",
    "loading": "38;5;179",
    "degraded": "38;5;196",
    "ready": "38;5;35",
    "active": "38;5;87",
}


def _ansi(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _recent_runtime_activity(recent: list[dict], *, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    for event in recent:
        if event.get("event") not in {"model_load", "passthrough", "model_evict"}:
            continue
        try:
            ts = float(event.get("ts", 0.0))
        except (TypeError, ValueError):
            continue
        if current - ts < _ACTIVE_SECS:
            return True
    return False


def _statusline_decide(stats: object, recent: bool) -> str:
    if stats is None:
        return "down"
    if stats == "loading":
        return "loading"
    if not isinstance(stats, dict) or not stats.get("ok"):
        return "down"
    if not stats.get("resident"):
        return "cold"
    backend = stats.get("backend")
    if isinstance(backend, str) and backend.startswith("fake ("):
        return "degraded"
    return "active" if recent else "ready"


def _statusline_indicator(state: str, *, plain: bool = False) -> str:
    if plain:
        return {
            "down": "-",
            "cold": ".",
            "loading": "*",
            "degraded": "x",
            "ready": "+",
            "active": "*",
        }.get(state, "?")
    frame = int(time.time())
    if state in {"loading", "active"}:
        glyph = _SPIN_FRAMES[frame % len(_SPIN_FRAMES)]
    elif state == "ready":
        glyph = _PULSE_FRAMES[frame % len(_PULSE_FRAMES)]
    elif state == "cold":
        glyph = "·"
    elif state == "degraded":
        glyph = "x"
    else:
        glyph = "-"
    return _ansi(_STATUSLINE_COLORS.get(state, _STATUSLINE_COLORS["down"]), glyph)


def _statusline_query() -> object:
    try:
        return client.stats(timeout=0.25)
    except socket.timeout:
        return "loading"
    except OSError:
        return None


def _statusline(args: argparse.Namespace) -> int:
    stats = _statusline_query()
    recent = _recent_runtime_activity(events.tail(6))
    state = _statusline_decide(stats, recent)
    print(f"{_statusline_indicator(state, plain=args.plain)} needle{_SEP}{state}")
    return 0


def _stop(args: argparse.Namespace) -> int:
    try:
        resp = client.stop(timeout=0.5)
    except (OSError, RuntimeError) as exc:
        print(f"Needle: runtime already stopped ({exc})", file=sys.stderr)
        return 0
    if not resp.get("ok"):
        _print_error(resp.get("error"))
        return 1
    print("Needle: runtime stopping", file=sys.stderr)
    return 0


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _uninstall(args: argparse.Namespace) -> int:
    home = naming.app_home()
    model_root = naming.model_root()
    paths = [home]
    if not _is_relative_to(model_root, home):
        paths.append(model_root)

    existing = [path for path in paths if path.exists()]
    if not args.yes:
        print("Needle-owned local state that would be removed:")
        if existing:
            for path in existing:
                print(f"  {path}")
        else:
            print("  nothing found")
        print("")
        print("Agent cleanup uses each agent's own MCP command:")
        print("  Claude: needle setup claude-code --uninstall")
        print("  Codex:  needle setup codex --uninstall")
        print("")
        print("Run `needle uninstall --yes` to stop the runtime and remove these files.")
        return 0

    try:
        client.stop(timeout=0.5)
        print("Needle: runtime stopping", file=sys.stderr)
    except (OSError, RuntimeError):
        pass

    removed: list[Path] = []
    for path in existing:
        try:
            _remove_path(path)
            removed.append(path)
        except OSError as exc:
            print(f"warning: could not remove {path}: {exc}", file=sys.stderr)

    if removed:
        print("removed Needle-owned local state:")
        for path in removed:
            print(f"  {path}")
    else:
        print("no Needle-owned local state found")
    print("")
    print("Remove agent connections with their setup commands:")
    print("  Claude: needle setup claude-code --uninstall")
    print("  Codex:  needle setup codex --uninstall")
    print("Remove the CLI entrypoint with your package manager, for example:")
    print("  brew uninstall needle")
    print("  pipx uninstall needle")
    print("  uv tool uninstall needle")
    return 0


def _format_command(command: list[str]) -> str:
    return " ".join(command)


def _run_visible(command: list[str]) -> int:
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode:
        _print_error(f"command failed ({proc.returncode}): {_format_command(command)}")
    return int(proc.returncode)


_SETUP_HOSTS = {
    "claude-code": {
        "label": "Claude Code",
        "summary": "MCP tool named needle_bash",
        "setup": "needle setup claude-code",
        "verify": "Open Claude Code and run `/mcp`.",
        "uninstall": "needle setup claude-code --uninstall",
    },
    "codex": {
        "label": "Codex CLI",
        "summary": "MCP tool named needle_bash",
        "setup": "needle setup codex",
        "verify": "Open Codex and run `/mcp`.",
        "uninstall": "needle setup codex --uninstall",
    },
}


def _is_interactive() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _setup_pending_path() -> Path:
    return naming.app_home() / "setup-pending.json"


def _record_setup_pending(source: str) -> None:
    path = _setup_pending_path()
    data = {
        "source": source,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "resume": "needle setup",
        "runtime": DEFAULT_RUNTIME.runtime_id,
        "hosts": {
            host: {
                "setup": meta["setup"],
                "verify": meta["verify"],
                "uninstall": meta["uninstall"],
            }
            for host, meta in _SETUP_HOSTS.items()
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not write setup pending marker at {path}: {exc}", file=sys.stderr)


def _setup_host(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    aliases = {
        "claude": "claude-code",
        "claude_code": "claude-code",
        "mcp": "claude-code",
        "codex-cli": "codex",
        "codex_app": "codex",
        "codex-app": "codex",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _SETUP_HOSTS:
        raise ValueError("host must be one of: claude-code, codex")
    return normalized


def _print_setup_checklist(
    *,
    host: str | None,
    from_homebrew: bool,
    dry_run: bool,
) -> None:
    print("Needle setup")
    if dry_run:
        print("Dry run: Needle is showing the setup checklist.")
    elif from_homebrew:
        print("Homebrew installed Needle, but this shell cannot ask setup questions.")
    else:
        print("This shell is not interactive, so Needle is showing the setup checklist.")
    print("")
    print(f"Runtime: {DEFAULT_RUNTIME.runtime_id} ({DEFAULT_RUNTIME.backend_id}, {DEFAULT_RUNTIME.runtime_profile})")
    print("Tool surface: needle_bash through MCP")
    print("")
    print("Available agents:")
    for key, meta in _SETUP_HOSTS.items():
        marker = "*" if key == (host or "claude-code") else "-"
        print(f"  {marker} {key}: {meta['label']} ({meta['summary']})")
        print(f"      setup:   {meta['setup']}")
        print(f"      command: {_format_command(_setup_native_command(key, 'local'))}")
        print(f"      verify:  {meta['verify']}")
    print("")
    print("Needle will not change Claude Code or Codex until you confirm setup.")
    if dry_run:
        print("dry run: no changes made")
    else:
        print(f"pending setup marker: {_setup_pending_path()}")
    print("next: run `needle setup` in an interactive terminal, or run one of the setup commands above.")


def _setup_native_command(host: str, scope: str) -> list[str]:
    if host == "codex":
        return _codex_add_command()
    return _claude_code_add_command(scope)


def _parse_setup_hosts(value: str) -> list[str]:
    raw_parts = value.replace(";", ",").split(",")
    choices = list(_SETUP_HOSTS)
    selected: list[str] = []
    for raw in raw_parts:
        part = raw.strip().lower()
        if not part:
            continue
        if part in {"all", "*"}:
            for choice in choices:
                if choice not in selected:
                    selected.append(choice)
            continue
        if part.isdigit():
            idx = int(part)
            if idx < 1 or idx > len(choices):
                raise ValueError(f"agent number must be between 1 and {len(choices)}")
            host = choices[idx - 1]
        else:
            parsed = _setup_host(part)
            if parsed is None:
                continue
            host = parsed
        if host not in selected:
            selected.append(host)
    if not selected:
        raise ValueError("select at least one agent")
    return selected


def _prompt_setup_hosts(default: str = "1") -> list[str]:
    print("Choose agents to connect:")
    for idx, (host, meta) in enumerate(_SETUP_HOSTS.items(), start=1):
        print(f"  {idx}. {meta['label']} - {meta['summary']}")
    print("")
    raw = typer.prompt("Agents", default=default)
    try:
        hosts = _parse_setup_hosts(raw)
    except ValueError as exc:
        _print_error(exc)
        raise typer.Exit(code=1) from exc
    return hosts


def _print_setup_plan(hosts: list[str], scope: str) -> None:
    print("Needle will connect:")
    for host in hosts:
        meta = _SETUP_HOSTS[host]
        print(f"  - {meta['label']}: {meta['summary']}")
        print(f"    runtime: {DEFAULT_RUNTIME.runtime_id}")
        print(f"    command: {_format_command(_setup_native_command(host, scope))}")
        print(f"    verify:  {meta['verify']}")
        print("    note:    use needle_bash for large read-only observations")
    print("")


def _run_setup_host(*, host: str, dry_run: bool, scope: str) -> int:
    if host == "codex":
        return _setup_codex(_ns(dry_run=dry_run, uninstall=False))
    return _setup_claude_code(_ns(dry_run=dry_run, uninstall=False, scope=scope))


def _run_setup_hosts(*, hosts: list[str], dry_run: bool, scope: str) -> int:
    for index, host in enumerate(hosts, start=1):
        if len(hosts) > 1:
            print("")
            print(f"[{index}/{len(hosts)}] {_SETUP_HOSTS[host]['label']}")
        code = _run_setup_host(host=host, dry_run=dry_run, scope=scope)
        if code:
            return code
    return 0


def _setup_wizard(args: argparse.Namespace) -> int:
    try:
        host = _setup_host(args.host)
        _claude_scope(args.scope)
    except ValueError as exc:
        _print_error(exc)
        return 1

    if args.from_homebrew and not _is_interactive() and not args.yes and not args.dry_run:
        _record_setup_pending("homebrew")
        _print_setup_checklist(host=host, from_homebrew=True, dry_run=False)
        return 0

    if args.dry_run or (not _is_interactive() and not args.yes):
        _print_setup_checklist(host=host, from_homebrew=args.from_homebrew, dry_run=args.dry_run)
        return 0

    if args.yes:
        return _run_setup_host(host=host or "claude-code", dry_run=False, scope=args.scope)

    print("Needle setup")
    if args.from_homebrew:
        print("Homebrew installed Needle. Let's connect it to your coding agent.")
    else:
        print("Connect Needle to your coding agent.")
    print("")
    chosen_hosts = [host] if host else _prompt_setup_hosts()
    _print_setup_plan(chosen_hosts, args.scope)
    if not typer.confirm("Connect selected agent(s) now?", default=True):
        _record_setup_pending("user-deferred")
        print("No changes made.")
        if len(chosen_hosts) == 1:
            print(f"resume: {_SETUP_HOSTS[chosen_hosts[0]]['setup']}")
        else:
            print("resume: needle setup")
        return 0
    return _run_setup_hosts(hosts=chosen_hosts, dry_run=False, scope=args.scope)


def _mcp_serve(args: argparse.Namespace) -> int:
    try:
        from .hosts.mcp.server import main as serve

        serve()
    except RuntimeError as exc:
        _print_error(exc)
        return 1
    return 0


def _claude_scope(value: str) -> str:
    if value not in {"local", "project", "user"}:
        raise ValueError("scope must be one of: local, project, user")
    return value


def _claude_code_add_command(scope: str) -> list[str]:
    return [
        "claude",
        "mcp",
        "add",
        "--transport",
        "stdio",
        "--scope",
        scope,
        "needle-bash",
        "--",
        "needle",
        "mcp",
        "serve",
    ]


def _claude_code_remove_command() -> list[str]:
    return ["claude", "mcp", "remove", "needle-bash"]


def _claude_code_mcp_json() -> dict[str, object]:
    return {
        "mcpServers": {
            "needle-bash": {
                "type": "stdio",
                "command": "needle",
                "args": ["mcp", "serve"],
                "env": {},
            }
        }
    }


def _codex_add_command() -> list[str]:
    return ["codex", "mcp", "add", "needle-bash", "--", "needle", "mcp", "serve"]


def _codex_remove_command() -> list[str]:
    return ["codex", "mcp", "remove", "needle-bash"]


def _codex_config_toml() -> str:
    return "\n".join(
        [
            "[mcp_servers.needle-bash]",
            'command = "needle"',
            'args = ["mcp", "serve"]',
        ]
    )


def _setup_claude_code(args: argparse.Namespace) -> int:
    try:
        scope = _claude_scope(args.scope)
    except ValueError as exc:
        _print_error(exc)
        return 1

    if args.uninstall:
        command = _claude_code_remove_command()
        print("Needle Claude Code MCP server: needle-bash")
        print(f"Claude command: {_format_command(command)}")
        if args.dry_run:
            print("dry run: no changes made")
            print("next: run `needle setup claude-code --uninstall` without `--dry-run` to remove it.")
            return 0
        if shutil.which("claude") is None:
            _print_error("Claude Code CLI was not found on PATH; `claude --help` should work first.")
            return 1
        code = _run_visible(command)
        if code:
            return code
        print("Needle Claude Code MCP server removed through Claude's native MCP command.")
        return 0

    command = _claude_code_add_command(scope)
    print("Needle for Claude Code")
    print("Tool name: needle_bash")
    print("Tool command: needle mcp serve")
    print(f"Runtime command: {_format_command(runtime_manage_command())}")
    print(f"Runtime: {DEFAULT_RUNTIME.runtime_id} ({DEFAULT_RUNTIME.backend_id}, {DEFAULT_RUNTIME.runtime_profile})")
    print(f"Claude scope: {scope}")
    print(f"Claude command: {_format_command(command)}")
    print("Statusline: needle statusline claude-code")
    print("Status: needle status --events 20")
    print("Note: start the Needle runtime before expecting pruning.")
    print("")
    print("Project .mcp.json shape, if you choose --scope project:")
    print(json.dumps(_claude_code_mcp_json(), indent=2))

    if args.dry_run:
        print("dry run: no changes made")
        print("next: run `needle setup claude-code`, then open Claude Code and run `/mcp`.")
        return 0

    if shutil.which("claude") is None:
        _print_error("Claude Code CLI was not found on PATH; `claude --help` should work first.")
        return 1

    code = _run_visible(command)
    if code:
        return code
    print("Needle Claude Code MCP server installed. Open Claude Code and run `/mcp`.")
    print("Agent contract: use `needle_bash` for observation; keep edits on native tools.")
    return 0


def _setup_codex(args: argparse.Namespace) -> int:
    if args.uninstall:
        command = _codex_remove_command()
        print("Needle Codex CLI MCP server: needle-bash")
        print(f"Codex command: {_format_command(command)}")
        if args.dry_run:
            print("dry run: no changes made")
            print("next: run `needle setup codex --uninstall` without `--dry-run` to remove it.")
            return 0
        if shutil.which("codex") is None:
            _print_error("Codex CLI was not found on PATH; `codex --help` should work first.")
            return 1
        code = _run_visible(command)
        if code:
            return code
        print("Needle Codex CLI MCP server removed through Codex's native MCP command.")
        return 0

    command = _codex_add_command()
    print("Needle for Codex CLI")
    print("Tool name: needle_bash")
    print("Tool command: needle mcp serve")
    print(f"Runtime command: {_format_command(runtime_manage_command())}")
    print(f"Runtime: {DEFAULT_RUNTIME.runtime_id} ({DEFAULT_RUNTIME.backend_id}, {DEFAULT_RUNTIME.runtime_profile})")
    print(f"Codex command: {_format_command(command)}")
    print("Statusline: needle statusline codex")
    print("Status: needle status --events 20")
    print("Note: Codex support is experimental; start the Needle runtime before expecting pruning.")
    print("")
    print("Project .codex/config.toml shape, if you prefer project-scoped setup:")
    print(_codex_config_toml())
    print("")
    print("Codex note: ask Codex to use `needle_bash` for large read-only observations.")
    print("Needle does not transparently rewrite Codex's built-in Bash output.")

    if args.dry_run:
        print("dry run: no changes made")
        print("next: run `needle setup codex`, then open Codex and run `/mcp`.")
        return 0

    if shutil.which("codex") is None:
        _print_error("Codex CLI was not found on PATH; `codex --help` should work first.")
        return 1

    code = _run_visible(command)
    if code:
        return code
    print("Needle Codex CLI MCP server installed. Start a fresh Codex thread and run `/mcp`.")
    print("Agent contract: use `needle_bash` for observation; keep edits on native tools.")
    return 0


def _default_model_repo() -> str:
    return os.environ.get("NEEDLE_MODEL") or os.environ.get("HAY_MODEL", "ayanami-kitasan/code-pruner")


def _model_dir(args: argparse.Namespace) -> int:
    repo = args.repo or _default_model_repo()
    print(naming.model_dir_for_repo(repo))
    return 0


def _model_download(args: argparse.Namespace) -> int:
    repo = args.repo or _default_model_repo()
    revision = args.revision or os.environ.get("NEEDLE_MODEL_REVISION") or os.environ.get("HAY_MODEL_REVISION")
    try:
        from .model_download import download_model_snapshot
    except ImportError:
        print(
            "error: this Needle install can set up MCP, but it does not include "
            "the MLX backend/model download dependencies needed for real pruning.",
            file=sys.stderr,
        )
        print(
            "developer preview path: `uv tool install --editable '.[backend-code-pruner-mlx]'`, "
            "then run `needle model download`",
            file=sys.stderr,
        )
        return 1
    try:
        result = download_model_snapshot(
            repo=repo,
            revision=revision,
            caller="cli",
            force=True,
        )
    except ImportError:
        print(
            "error: this Needle install can set up MCP, but it does not include "
            "the MLX backend/model download dependencies needed for real pruning.",
            file=sys.stderr,
        )
        print(
            "developer preview path: `uv tool install --editable '.[backend-code-pruner-mlx]'`, "
            "then run `needle model download`",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - surface model resolution/download errors without a traceback.
        print(f"error: could not download model: {exc}", file=sys.stderr)
        return 1
    print(result.path)
    if result.resolved_revision:
        print(f"revision: {result.resolved_revision}")
    return 0


def _write_model_provenance(
    local_dir: Path,
    *,
    repo: str,
    requested_revision: str,
    resolved_revision: str,
    caller: str = "cli",
) -> None:
    from .model_download import write_model_provenance

    write_model_provenance(
        local_dir,
        repo=repo,
        requested_revision=requested_revision,
        resolved_revision=resolved_revision,
        caller=caller,
    )


def _model_clean(args: argparse.Namespace) -> int:
    root = naming.model_root()
    if not args.yes:
        print(f"Needle model directory: {root}")
        print("Run `needle model clean --yes` to remove it.")
        return 0
    if root.exists():
        shutil.rmtree(root)
        print(f"removed {root}")
    else:
        print(f"no model directory found at {root}")
    return 0


@app.command("status")
def status(
    events_count: int = typer.Option(
        12,
        "--events",
        "-n",
        help="Recent events to show.",
    ),
) -> None:
    """Operator snapshot: residency and recent events."""
    _exit_with(_status(_ns(events=events_count)))


@app.command("stop")
def stop() -> None:
    """Ask the resident runtime to shut down cleanly."""
    _exit_with(_stop(_ns()))


@app.command("uninstall")
def uninstall(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Actually remove local runtime/model files.",
    ),
) -> None:
    """Stop Needle and remove Needle-owned local state."""
    _exit_with(_uninstall(_ns(yes=yes)))


@model_app.command("dir")
def model_dir(
    repo: str = typer.Option(
        "",
        "--repo",
        help="Hugging Face repo id; defaults to code-pruner.",
    ),
) -> None:
    """Show the local directory for a model repo."""
    _exit_with(_model_dir(_ns(repo=repo)))


@model_app.command("download")
def model_download(
    repo: str = typer.Option(
        "",
        "--repo",
        help="Hugging Face repo id; defaults to code-pruner.",
    ),
    revision: str = typer.Option(
        "",
        "--revision",
        help="Hugging Face revision, tag, branch, or commit to download.",
    ),
) -> None:
    """Download the configured model repo."""
    _exit_with(_model_download(_ns(repo=repo, revision=revision)))


@model_app.command("clean")
def model_clean(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Actually remove the model directory.",
    ),
) -> None:
    """Remove the local model directory."""
    _exit_with(_model_clean(_ns(yes=yes)))


@runtime_app.command("manage")
def runtime_manage(
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Debug mode: start from inherited environment instead of the built-in runtime config.",
    ),
) -> None:
    """Run the machine-wide model residency manager."""
    from .runtime import cli as runtime_cli

    argv = ["manage"]
    if raw:
        argv.append("--raw")
    _exit_with(runtime_cli.main(argv))


@runtime_app.command("session")
def runtime_session(
    session: str = typer.Option(
        "",
        "--session",
        help="Optional host session id to lease under.",
    ),
) -> None:
    """Hold a session lease against the manager."""
    from .runtime import cli as runtime_cli

    argv = ["session"]
    if session:
        argv.extend(["--session", session])
    _exit_with(runtime_cli.main(argv))


@runtime_app.command("prune")
def runtime_prune(
    query: str = typer.Option(
        "",
        "--query",
        "-q",
        help="Relevance query / goal.",
    ),
) -> None:
    """Pipe stdin through the manager and print the returned text."""
    from .runtime import cli as runtime_cli

    argv = ["prune"]
    if query:
        argv.extend(["--query", query])
    _exit_with(runtime_cli.main(argv))


@runtime_app.command("status")
def runtime_status(
    events_count: int = typer.Option(
        12,
        "--events",
        "-n",
        help="Recent events to show.",
    ),
) -> None:
    """Show the low-level runtime residency snapshot."""
    from .runtime import cli as runtime_cli

    _exit_with(runtime_cli.main(["status", "--events", str(events_count)]))


@runtime_app.command("stop")
def runtime_stop() -> None:
    """Ask the resident manager to shut down cleanly."""
    from .runtime import cli as runtime_cli

    _exit_with(runtime_cli.main(["stop"]))


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the bash-minimal stdio MCP server."""
    _exit_with(_mcp_serve(_ns()))


@setup_app.callback(invoke_without_command=True)
def setup(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the guided setup plan without changing Claude Code or Codex.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Use the safest default answers and run setup for the selected agent.",
    ),
    host: str | None = typer.Option(
        None,
        "--host",
        help="Agent to connect: claude-code or codex.",
    ),
    from_homebrew: bool = typer.Option(
        False,
        "--from-homebrew",
        help="Entry point used by the Homebrew post-install hook.",
    ),
    scope: str = typer.Option(
        "local",
        "--scope",
        help="Claude MCP scope if the wizard installs Claude Code.",
    ),
) -> None:
    """Run Needle's guided MCP setup wizard."""
    if ctx.invoked_subcommand is not None:
        return
    _exit_with(
        _setup_wizard(
            _ns(
                dry_run=dry_run,
                yes=yes,
                host=host,
                from_homebrew=from_homebrew,
                scope=scope,
            )
        )
    )


@setup_app.command("claude-code")
def setup_claude_code(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print Claude Code MCP setup without changing Claude Code.",
    ),
    uninstall_adapter: bool = typer.Option(
        False,
        "--uninstall",
        help="Remove Needle's MCP server through Claude Code's native MCP command.",
    ),
    scope: str = typer.Option(
        "local",
        "--scope",
        help="Claude MCP scope: local, project, or user.",
    ),
) -> None:
    """Install or remove Needle's Claude Code MCP server."""
    if uninstall_adapter:
        _exit_with(_setup_claude_code(_ns(dry_run=dry_run, uninstall=True, scope=scope)))
    _exit_with(_setup_claude_code(_ns(dry_run=dry_run, uninstall=False, scope=scope)))


@setup_app.command("codex")
def setup_codex(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print experimental Codex MCP setup without changing Codex.",
    ),
    uninstall_adapter: bool = typer.Option(
        False,
        "--uninstall",
        help="Remove Needle's experimental Codex MCP server through Codex's native MCP command.",
    ),
) -> None:
    """Install or remove Needle's experimental Codex MCP server."""
    if uninstall_adapter:
        _exit_with(_setup_codex(_ns(dry_run=dry_run, uninstall=True)))
    _exit_with(_setup_codex(_ns(dry_run=dry_run, uninstall=False)))


@statusline_app.command("claude-code")
def statusline_claude_code(
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Disable ANSI color and animation glyphs for tests or plain terminals.",
    ),
) -> None:
    """Render a compact Claude Code statusline from Needle runtime health."""
    _exit_with(_statusline(_ns(plain=plain)))


@statusline_app.command("codex")
def statusline_codex(
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Disable ANSI color and animation glyphs for tests or plain terminals.",
    ),
) -> None:
    """Render a compact Codex CLI statusline from Needle runtime health."""
    _exit_with(_statusline(_ns(plain=plain)))


def main(argv: list[str] | None = None) -> int:
    try:
        result = app(args=argv, prog_name="needle", standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return int(exc.exit_code)
    except EOFError:
        print("Aborted: setup needs an interactive answer. Re-run with `--dry-run` to inspect first.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Aborted.", file=sys.stderr)
        return 1
    except click.Abort:
        print("Aborted!", file=sys.stderr)
        return 1
    except Exception as exc:
        show = getattr(exc, "show", None)
        exit_code = getattr(exc, "exit_code", None)
        if callable(show) and isinstance(exit_code, int):
            show(file=sys.stderr)
            return exit_code
        raise
    if isinstance(result, int):
        return result
    return 0
