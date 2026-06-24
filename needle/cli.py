"""Needle's public CLI.

The pruner module is the runtime engine. Needle owns package/registry selection
because a package composes protocol, capability, backend, host binding, docs,
privacy, accounting, and evidence.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
import typer

from . import __version__
from .registry import (
    PackageConfigError,
    active_package_selection,
    load_active_package,
    package_config_path,
    package_summaries,
    runtime_launch_plan,
    set_configured_package_id,
)
from .runtime import client, events, naming

app = typer.Typer(
    name="needle",
    help="Needle package and runtime control plane.",
    no_args_is_help=True,
    invoke_without_command=True,
)
package_app = typer.Typer(help="Inspect and select Needle packages.", no_args_is_help=True)
evidence_app = typer.Typer(help="Inspect package evidence fixtures.", no_args_is_help=True)
model_app = typer.Typer(help="Inspect, download, or remove local model files.", no_args_is_help=True)
runtime_app = typer.Typer(help="Low-level runtime commands used by host adapters.", no_args_is_help=True)
mcp_app = typer.Typer(help="Run Needle MCP servers used by host adapters.", no_args_is_help=True)
setup_app = typer.Typer(help="Set up Needle inside supported host agents.", no_args_is_help=True)

app.add_typer(package_app, name="package")
app.add_typer(evidence_app, name="evidence")
app.add_typer(model_app, name="model")
app.add_typer(runtime_app, name="runtime")
app.add_typer(mcp_app, name="mcp")
app.add_typer(setup_app, name="setup")


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show Needle's version and exit.",
        is_eager=True,
    ),
) -> None:
    """Needle package and runtime control plane."""
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


def _package_list(args: argparse.Namespace) -> int:
    try:
        summaries = package_summaries(host_binding=args.host_binding)
    except PackageConfigError as exc:
        _print_error(exc)
        return 1
    if not summaries:
        print("no packages found")
        return 0
    for item in summaries:
        marker = "*" if item.get("active") else "-"
        if not item.get("valid"):
            print(f"{marker} {item['id']}  INVALID  {item.get('error', '')}")
            continue
        capabilities = ",".join(item.get("capabilities", []))
        print(
            f"{marker} {item['id']}  "
            f"implements={capabilities}  "
            f"protocol={item.get('protocol', '?')}  "
            f"uses={item.get('backend', '?')}  "
            f"host={item.get('host_binding', '?')}"
        )
    return 0


def _package_current(args: argparse.Namespace) -> int:
    package_id, source = active_package_selection(host_binding=args.host_binding or None)
    print(package_id)
    print(f"source: {source}")
    if args.host_binding:
        print(f"host binding: {args.host_binding}")
    return 0


def _package_use(args: argparse.Namespace) -> int:
    try:
        loaded = set_configured_package_id(args.package_id, host_binding=args.host_binding or None)
    except PackageConfigError as exc:
        _print_error(exc)
        return 1
    print(f"selected package: {loaded.package_id}")
    print(f"config: {package_config_path()}")
    print(f"host binding: {args.host_binding or loaded.binding_id}")
    print(f"implements: {', '.join(loaded.capability_ids)}")
    print(f"protocol: {loaded.protocol['id']}")
    print(f"uses backend: {loaded.backend_id}")
    plan = runtime_launch_plan(package_id=loaded.package_id, host_binding=args.host_binding or None)
    print(f"runtime command: {' '.join(plan.command)}")
    print("restart the resident runtime for running sessions: needle stop")
    return 0


def _backend_readiness_notes(loaded: object) -> list[str]:
    backend = getattr(loaded, "backend", {}) or {}
    compute = backend.get("compute") if isinstance(backend, dict) else {}
    requires = compute.get("requires", []) if isinstance(compute, dict) else []
    requirements = ", ".join(str(item) for item in requires) if requires else "none declared"
    notes = [
        "package graph: ok (registry only; backend is not imported and model is not loaded)",
        f"backend requirements: {requirements}",
    ]
    if "mlx" in requires:
        notes.append(
            "backend readiness: install the MLX backend dependencies and model before expecting real pruning"
        )
    elif "explicit_endpoint" in requires:
        notes.append("backend readiness: set the endpoint env var before expecting remote pruning")
    else:
        notes.append("backend readiness: not checked by package doctor")
    return notes


def _package_doctor(args: argparse.Namespace) -> int:
    try:
        loaded = load_active_package(
            package_id=args.package_id or None,
            host_binding=args.host_binding or None,
        )
    except PackageConfigError as exc:
        _print_error(exc)
        return 1
    selected, source = active_package_selection(host_binding=args.host_binding or None)
    plan = runtime_launch_plan(package_id=loaded.package_id, host_binding=args.host_binding or None)
    lines = [
        f"package: {loaded.package_id}",
        f"active selection: {selected} ({source})",
        f"protocol: {loaded.protocol['id']}",
        f"implements: {', '.join(loaded.capability_ids)}",
        f"uses backend: {loaded.backend_id}",
        f"runtime launcher: {plan.kind}",
        f"runtime command: {' '.join(plan.command)}",
        *_backend_readiness_notes(loaded),
        f"host binding: {loaded.binding_id}",
        f"claim card: {loaded.claim_card['id']}",
        f"package card: {loaded.package_card_path}",
    ]
    for ref, path in loaded.evidence_paths.items():
        lines.append(f"evidence: {ref} -> {path}")
    print("\n".join(lines))
    return 0


def _evidence_check(args: argparse.Namespace) -> int:
    try:
        loaded = load_active_package(
            package_id=args.package_id or None,
            host_binding=args.host_binding or None,
        )
    except PackageConfigError as exc:
        _print_error(exc)
        return 1

    print(f"package: {loaded.package_id}")
    print(f"claim card: {loaded.claim_card['id']}")
    print("evidence: ok (fixture manifests only)")
    print(
        "proof level: local fixtures; does not prove MLX model quality, "
        "SWE-bench acceptance, token savings, or dollar savings"
    )
    for ref, path in loaded.evidence_paths.items():
        print(f"- {ref}")
        print(f"  manifest: {path}")
        with path.open("r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        for case in manifest.get("cases", []):
            print(
                f"  case: {case['id']}  "
                f"tool={case['tool']}  "
                f"behavior={case['expected_behavior']}  "
                f"file={path.parent / case['file']}"
            )
    return 0


_PRESSURE = {1: "normal", 2: "warning", 4: "critical"}


def _fmt_ts(ts: object) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _render_status(stats: dict | None, recent: list[dict]) -> str:
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
    except OSError:
        stats = None
    print(_render_status(stats, events.tail(args.events)))
    return 0


def _stop(args: argparse.Namespace) -> int:
    try:
        resp = client.stop(timeout=0.5)
    except OSError as exc:
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
    config_path = package_config_path()
    paths = [home]
    if not _is_relative_to(model_root, home):
        paths.append(model_root)
    if not _is_relative_to(config_path, home):
        paths.append(config_path)

    existing = [path for path in paths if path.exists()]
    if not args.yes:
        print("Needle-owned local state that would be removed:")
        if existing:
            for path in existing:
                print(f"  {path}")
        else:
            print("  nothing found")
        print("")
        print("Host extension removal stays host-native:")
        print("  Pi:     needle setup pi --uninstall")
        print("  Claude: needle setup claude-code --uninstall")
        print("")
        print("Run `needle uninstall --yes` to stop the runtime and remove these files.")
        return 0

    try:
        client.stop(timeout=0.5)
        print("Needle: runtime stopping", file=sys.stderr)
    except OSError:
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
    print("Remove host integrations with their native commands:")
    print("  Pi:     needle setup pi --uninstall")
    print("  Claude: needle setup claude-code --uninstall")
    print("Remove the CLI entrypoint with your package manager, for example:")
    print("  brew uninstall needle")
    print("  pipx uninstall needle")
    print("  uv tool uninstall needle")
    return 0


def _pi_package_dir() -> Path:
    return Path(__file__).resolve().parent / "hosts" / "pi"


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


def _setup_pi(args: argparse.Namespace) -> int:
    package_dir = _pi_package_dir()
    manifest = package_dir / "package.json"
    canary = package_dir / "demo-canary.mjs"
    if not manifest.exists():
        _print_error(f"Needle Pi package manifest is missing at {manifest}")
        return 1

    pi_command = ["pi", "uninstall" if args.uninstall else "install", str(package_dir)]
    print(f"Needle Pi package: {package_dir}")
    print(f"Pi command: {_format_command(pi_command)}")

    if args.dry_run:
        if not args.uninstall and not args.skip_canary:
            print(f"Canary command: node {canary}")
        print("dry run: no changes made")
        if args.uninstall:
            print("next: run `needle setup pi --uninstall` without `--dry-run` to remove the Pi adapter.")
        else:
            print("next: run `needle setup pi`, then open Pi and run `/needle doctor`.")
        return 0

    if shutil.which("pi") is None:
        _print_error("Pi CLI was not found on PATH; install or open Pi before running setup.")
        print("tip: `pi --help` should work before Needle can install its Pi adapter.", file=sys.stderr)
        return 1

    code = _run_visible(pi_command)
    if code:
        return code

    if args.uninstall:
        print("Needle Pi adapter removed through Pi's native package command.")
        return 0

    if not args.skip_canary:
        if shutil.which("node") is None:
            _print_error("Node.js was not found on PATH; Pi adapter installed, but the canary could not run.")
            return 1
        code = _run_visible(["node", str(canary)])
        if code:
            return code

    print("Needle Pi adapter installed. Open Pi and run `/needle doctor`.")
    return 0


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
    loaded = None
    try:
        loaded = load_active_package(package_id="e24z/mcp-bash-local", host_binding="mcp/bash")
    except PackageConfigError as exc:
        _print_error(exc)
        return 1

    print("Needle Claude Code MCP setup")
    print(f"package: {loaded.package_id}")
    print(f"host binding: {loaded.binding_id}")
    print(f"server: needle-bash")
    print(f"server command: needle mcp serve")
    print(f"Claude scope: {scope}")
    print(f"Claude command: {_format_command(command)}")
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


def _default_model_repo() -> str:
    return os.environ.get("NEEDLE_MODEL") or os.environ.get("HAY_MODEL", "ayanami-kitasan/code-pruner")


def _model_dir(args: argparse.Namespace) -> int:
    repo = args.repo or _default_model_repo()
    print(naming.model_dir_for_repo(repo))
    return 0


def _model_download(args: argparse.Namespace) -> int:
    repo = args.repo or _default_model_repo()
    root = naming.model_root()
    local_dir = naming.model_dir_for_repo(repo)
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "error: this Needle install does not include the MLX backend dependencies; "
            "developer preview path: `uv tool install --editable '.[backend-code-pruner-mlx]'`",
            file=sys.stderr,
        )
        return 1
    root.mkdir(parents=True, exist_ok=True)
    path = snapshot_download(
        repo,
        local_dir=str(local_dir),
        cache_dir=str(root / ".hf-cache"),
    )
    print(path)
    return 0


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


@package_app.command("list")
def package_list(
    host_binding: str = typer.Option(
        "",
        "--host-binding",
        help="Optional host binding filter, for example pi/native-tools.",
    ),
) -> None:
    """List registry packages."""
    _exit_with(_package_list(_ns(host_binding=host_binding)))


@package_app.command("current")
def package_current(
    host_binding: str = typer.Option(
        "",
        "--host-binding",
        help="Optional host binding scope, for example pi/native-tools.",
    ),
) -> None:
    """Show the active package id."""
    _exit_with(_package_current(_ns(host_binding=host_binding)))


@package_app.command("use")
def package_use(
    package_id: str = typer.Argument(..., help="Package id, for example e24z/pi-local-mac."),
    host_binding: str = typer.Option(
        "",
        "--host-binding",
        help="Optional required host binding; defaults to the selected package's binding.",
    ),
) -> None:
    """Persist a package selection."""
    _exit_with(_package_use(_ns(package_id=package_id, host_binding=host_binding)))


@package_app.command("doctor")
def package_doctor(
    package_id: str = typer.Argument("", help="Package id to inspect; defaults to active."),
    host_binding: str = typer.Option(
        "",
        "--host-binding",
        help="Optional host binding scope, for example pi/native-tools.",
    ),
) -> None:
    """Validate and explain a package."""
    _exit_with(_package_doctor(_ns(package_id=package_id, host_binding=host_binding)))


@evidence_app.command("check")
def evidence_check(
    package_id: str = typer.Argument("", help="Package id to inspect; defaults to active."),
    host_binding: str = typer.Option(
        "",
        "--host-binding",
        help="Optional host binding scope, for example pi/native-tools.",
    ),
) -> None:
    """Validate and list package evidence."""
    _exit_with(_evidence_check(_ns(package_id=package_id, host_binding=host_binding)))


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
        help="Actually remove local runtime/config/model files.",
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
) -> None:
    """Download the configured model repo."""
    _exit_with(_model_download(_ns(repo=repo)))


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
def runtime_manage() -> None:
    """Run the machine-wide model residency manager."""
    from .runtime import cli as runtime_cli

    _exit_with(runtime_cli.main(["manage"]))


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


@setup_app.command("pi")
def setup_pi(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the Pi package command without changing Pi.",
    ),
    uninstall_adapter: bool = typer.Option(
        False,
        "--uninstall",
        help="Remove the Pi adapter through Pi's native package command.",
    ),
    skip_canary: bool = typer.Option(
        False,
        "--skip-canary",
        help="Skip the local Pi adapter canary after install.",
    ),
) -> None:
    """Install or remove Needle's Pi adapter using Pi's native package flow."""
    _exit_with(_setup_pi(_ns(dry_run=dry_run, uninstall=uninstall_adapter, skip_canary=skip_canary)))


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
    _exit_with(_setup_claude_code(_ns(dry_run=dry_run, uninstall=uninstall_adapter, scope=scope)))


def main(argv: list[str] | None = None) -> int:
    try:
        result = app(args=argv, prog_name="needle", standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return int(exc.exit_code)
    except click.Abort:
        print("Aborted!", file=sys.stderr)
        return 1
    except Exception as exc:
        # Typer 0.26 uses a vendored Click implementation for console scripts.
        # Catch those CLI-usage exceptions structurally so user mistakes do not
        # render as Python tracebacks from installed artifacts.
        show = getattr(exc, "show", None)
        exit_code = getattr(exc, "exit_code", None)
        if callable(show) and isinstance(exit_code, int):
            show(file=sys.stderr)
            return exit_code
        raise
    if isinstance(result, int):
        return result
    return 0
