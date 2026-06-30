"""Needle's public CLI.

Needle owns package selection, setup, and runtime control because a package
composes protocol, capability, backend, host binding, privacy, accounting, and
evidence.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import click
import typer

from . import __version__
from .registry import (
    BUILTIN_REGISTRY_ROOT,
    LoadedPackage,
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
runtime_app = typer.Typer(help="Low-level runtime commands used by agent connections.", no_args_is_help=True)
mcp_app = typer.Typer(help="Run Needle MCP servers used by Claude Code, Codex, and other MCP clients.", no_args_is_help=True)
setup_app = typer.Typer(
    help="Connect Needle to supported coding agents.",
    invoke_without_command=True,
)
statusline_app = typer.Typer(help="Render compact Needle statusline output.", no_args_is_help=True)

app.add_typer(package_app, name="package")
app.add_typer(evidence_app, name="evidence")
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


_HOST_LABELS = {
    "pi/native-tools": "Pi native tools",
    "mcp/bash": "MCP bash",
}

_CAPABILITY_LABELS = {
    "e24z/soft-lamr": "Soft LAMR (SWE-Pruner plus Python AST repair)",
    "swe-pruner/reference": "SWE-Pruner reference (no AST repair)",
}

_BACKEND_LABELS = {
    "e24z/code-pruner-mlx": "local MLX backend",
    "e24z/code-pruner-http": "HTTP contract (future/declarative)",
}


def _package_label(value: object, labels: dict[str, str]) -> str:
    text = str(value or "?")
    return labels.get(text, text)


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
        if args.verbose:
            print(
                f"{marker} {item['id']}  "
                f"implements={capabilities}  "
                f"protocol={item.get('protocol', '?')}  "
                f"uses={item.get('backend', '?')}  "
                f"host={item.get('host_binding', '?')}  "
                f"runtime_profile={item.get('runtime_profile') or '?'}"
            )
            continue
        capability_labels = ", ".join(
            _package_label(capability, _CAPABILITY_LABELS)
            for capability in item.get("capabilities", [])
        )
        print(f"{marker} {item['id']} - {item.get('display_name') or item['id']}")
        print(f"    host:     {_package_label(item.get('host_binding'), _HOST_LABELS)}")
        print(f"    behavior: {capability_labels or 'unknown'}")
        print(f"    backend:  {_package_label(item.get('backend'), _BACKEND_LABELS)}")
        if item.get("runtime_profile"):
            print(f"    profile:  {item['runtime_profile']}")
    if not args.verbose:
        print("")
        print("* = active package for this host/config. Use --verbose for registry ids.")
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
    print(f"runtime profile: {plan.runtime_profile or 'none'}")
    print(
        "runtime command: "
        f"{_runtime_command_for_display(plan.command, package_id=loaded.package_id, host_binding=args.host_binding or '')}"
    )
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


def _package_field_audit() -> list[str]:
    return [
        "field audit:",
        f"  full audit: {BUILTIN_REGISTRY_ROOT / 'FIELD-AUDIT.md'}",
        "  uses.backend: drives the runtime launcher and backend env",
        "  host_binding: constrains host-scoped package selection",
        "  runtime_profile.env: applied to the resident manager process",
        "  focus_contract: adapter/tool contract; manager does not enforce goal hints",
        "  compute/privacy/accounting/evidence: public contract metadata, not runtime switches",
        "  http_pruner: not advertised as a usable runtime alternative in built-in packages",
    ]


def _runtime_command_for_display(
    command: list[str],
    *,
    package_id: str = "",
    host_binding: str = "",
) -> str:
    display = list(command)
    if package_id and "--package" not in display:
        display.extend(["--package", package_id])
    if host_binding and "--host-binding" not in display:
        display.extend(["--host-binding", host_binding])
    return " ".join(display)


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
        f"runtime profile: {plan.runtime_profile or 'none'}",
        "runtime command: "
        f"{_runtime_command_for_display(plan.command, package_id=loaded.package_id, host_binding=args.host_binding or '')}",
        *_package_field_audit(),
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
        package_bits = [
            f"package {stats.get('package_id')}" if stats.get("package_id") else None,
            f"host {stats.get('host_binding')}" if stats.get("host_binding") else None,
            f"profile {stats.get('runtime_profile')}" if stats.get("runtime_profile") else None,
            f"backend-id {stats.get('backend_id')}" if stats.get("backend_id") else None,
        ]
        package_line = "  " + "  ·  ".join(bit for bit in package_bits if bit)
        if package_line.strip():
            lines.append(package_line)
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
    except OSError:
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
    if state == "loading" or state == "active":
        glyph = _SPIN_FRAMES[frame % len(_SPIN_FRAMES)]
    elif state == "ready":
        glyph = _PULSE_FRAMES[frame % len(_PULSE_FRAMES)]
    elif state == "cold":
        glyph = "·"
    elif state == "degraded":
        glyph = "✗"
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


def _statusline_claude_code(args: argparse.Namespace) -> int:
    stats = _statusline_query()
    recent = _recent_runtime_activity(events.tail(6))
    state = _statusline_decide(stats, recent)
    print(f"{_statusline_indicator(state, plain=args.plain)} needle{_SEP}{state}")
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
        print("Agent cleanup uses each agent's own setup command:")
        print("  Pi:     needle setup pi --uninstall")
        print("  Claude: needle setup claude-code --uninstall")
        print("  Codex:  needle setup codex --uninstall")
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
    print("Remove agent connections with their setup commands:")
    print("  Pi:     needle setup pi --uninstall")
    print("  Claude: needle setup claude-code --uninstall")
    print("  Codex:  needle setup codex --uninstall")
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


_SETUP_HOSTS = {
    "pi": {
        "label": "Pi",
        "summary": "native read/bash tools, visible through /needle doctor",
        "binding": "pi/native-tools",
        "package": naming.DEFAULT_PACKAGE_ID,
        "setup": "needle setup pi",
        "verify": "Open Pi and run `/needle doctor`.",
        "uninstall": "needle setup pi --uninstall",
    },
    "claude-code": {
        "label": "Claude Code",
        "summary": "MCP tool named needle_bash",
        "binding": "mcp/bash",
        "package": "e24z/mlx-mcp-bash-reference",
        "setup": "needle setup claude-code",
        "verify": "Open Claude Code and run `/mcp`.",
        "uninstall": "needle setup claude-code --uninstall",
    },
    "codex": {
        "label": "Codex CLI",
        "summary": "experimental MCP tool named needle_bash",
        "binding": "mcp/bash",
        "package": "e24z/mlx-mcp-bash-reference",
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
        raise ValueError("host must be one of: pi, claude-code, codex")
    return normalized


def _print_setup_checklist(
    *,
    host: str | None,
    package_id: str | None,
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
    print("Available agents:")
    for key, meta in _SETUP_HOSTS.items():
        marker = "*" if key == (host or "pi") else "-"
        print(f"  {marker} {key}: {meta['label']} ({meta['summary']})")
        print(f"      package: {package_id if key == host and package_id else meta['package']}")
        print(f"      setup:   {meta['setup']}")
        print(f"      command: {_format_command(_setup_native_command(key, 'local'))}")
        print(f"      verify:  {meta['verify']}")
        print("      model:   run `needle model dir`; use `needle model download` after installing backend deps")
    print("")
    print("Needle will not change Pi, Claude Code, or Codex until you confirm setup.")
    if dry_run:
        print("dry run: no changes made")
    else:
        print(f"pending setup marker: {_setup_pending_path()}")
    print("next: run `needle setup` in an interactive terminal, or run one of the setup commands above.")


def _select_setup_package(
    host: str, package_id: str | None, *, dry_run: bool
) -> tuple[int, LoadedPackage | None]:
    meta = _SETUP_HOSTS[host]
    selected = package_id or str(meta["package"])
    binding = str(meta["binding"])
    try:
        loaded = load_active_package(package_id=selected, host_binding=binding)
    except PackageConfigError as exc:
        _print_error(exc)
        return 1, None

    print(f"Using Needle package: {loaded.package_id}")
    if dry_run:
        return 0, loaded

    try:
        configured = set_configured_package_id(loaded.package_id, host_binding=binding)
    except PackageConfigError as exc:
        _print_error(exc)
        return 1, None
    print(f"Selected Needle package: {configured.package_id}")
    return 0, loaded


def _run_setup_host(
    *,
    host: str,
    package_id: str | None,
    dry_run: bool,
    skip_canary: bool,
    scope: str,
) -> int:
    if host == "claude-code":
        try:
            _claude_scope(scope)
        except ValueError as exc:
            _print_error(exc)
            return 1
    code, loaded = _select_setup_package(host, package_id, dry_run=dry_run)
    if code:
        return code
    print("")
    if host == "pi":
        return _setup_pi(_ns(dry_run=dry_run, uninstall=False, skip_canary=skip_canary))
    if host == "codex":
        return _setup_codex(_ns(dry_run=dry_run, uninstall=False), loaded)
    return _setup_claude_code(_ns(dry_run=dry_run, uninstall=False, scope=scope), loaded)


def _setup_native_command(host: str, scope: str) -> list[str]:
    if host == "pi":
        return ["pi", "install", str(_pi_package_dir())]
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


def _print_setup_plan(hosts: list[str], package_id: str | None, scope: str) -> None:
    print("Needle will connect:")
    for host in hosts:
        meta = _SETUP_HOSTS[host]
        selected_package = package_id or str(meta["package"])
        print(f"  - {meta['label']}: {meta['summary']}")
        print(f"    package: {selected_package}")
        print(f"    verify:  {meta['verify']}")
        if host != "pi":
            print("    note:    use needle_bash for large read-only observations")
    print("")


def _run_setup_hosts(
    *,
    hosts: list[str],
    package_id: str | None,
    dry_run: bool,
    skip_canary: bool,
    scope: str,
) -> int:
    for index, host in enumerate(hosts, start=1):
        if len(hosts) > 1:
            print("")
            print(f"[{index}/{len(hosts)}] {_SETUP_HOSTS[host]['label']}")
        code = _run_setup_host(
            host=host,
            package_id=package_id,
            dry_run=dry_run,
            skip_canary=skip_canary,
            scope=scope,
        )
        if code:
            return code
    return 0


def _setup_wizard(args: argparse.Namespace) -> int:
    try:
        host = _setup_host(args.host)
    except ValueError as exc:
        _print_error(exc)
        return 1

    if args.from_homebrew and not _is_interactive() and not args.yes and not args.dry_run:
        _record_setup_pending("homebrew")
        _print_setup_checklist(
            host=host,
            package_id=args.package_id,
            from_homebrew=True,
            dry_run=False,
        )
        return 0

    if args.dry_run or (not _is_interactive() and not args.yes):
        _print_setup_checklist(
            host=host,
            package_id=args.package_id,
            from_homebrew=args.from_homebrew,
            dry_run=args.dry_run,
        )
        return 0

    if args.yes:
        return _run_setup_host(
            host=host or "pi",
            package_id=args.package_id,
            dry_run=False,
            skip_canary=args.skip_canary,
            scope=args.scope,
        )

    print("Needle setup")
    if args.from_homebrew:
        print("Homebrew installed Needle. Let's connect it to your coding agent.")
    else:
        print("Connect Needle to your coding agent.")
    print("")
    chosen_hosts = [host] if host else _prompt_setup_hosts()
    _print_setup_plan(chosen_hosts, args.package_id, args.scope)
    if not typer.confirm("Connect selected agent(s) now?", default=True):
        _record_setup_pending("user-deferred")
        print("No changes made.")
        if len(chosen_hosts) == 1:
            print(f"resume: {_SETUP_HOSTS[chosen_hosts[0]]['setup']}")
        else:
            print("resume: needle setup")
        return 0
    return _run_setup_hosts(
        hosts=chosen_hosts,
        package_id=args.package_id,
        dry_run=False,
        skip_canary=args.skip_canary,
        scope=args.scope,
    )


def _setup_pi(args: argparse.Namespace) -> int:
    package_dir = _pi_package_dir()
    manifest = package_dir / "package.json"
    canary = package_dir / "demo-canary.mjs"
    if not manifest.exists():
        _print_error(f"Needle Pi package manifest is missing at {manifest}")
        return 1

    pi_command = ["pi", "uninstall" if args.uninstall else "install", str(package_dir)]
    print(f"Pi package: {package_dir}")
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
        proc = subprocess.run(["node", str(canary)], text=True, capture_output=True, check=False)
        if proc.returncode:
            if proc.stdout:
                print(proc.stdout, end="")
            if proc.stderr:
                print(proc.stderr, end="", file=sys.stderr)
            _print_error(f"Pi canary failed ({proc.returncode})")
            return int(proc.returncode)
        print("Pi canary passed.")

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


def _runtime_manage_command(loaded: LoadedPackage) -> list[str]:
    return [
        "needle",
        "runtime",
        "manage",
        "--package",
        loaded.package_id,
        "--host-binding",
        loaded.binding_id,
    ]


def _setup_claude_code(
    args: argparse.Namespace,
    loaded_package: LoadedPackage | None = None,
) -> int:
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
    loaded = loaded_package
    if loaded is None:
        try:
            loaded = load_active_package(host_binding="mcp/bash")
        except PackageConfigError as exc:
            _print_error(exc)
            return 1

    print("Needle for Claude Code")
    print("Tool name: needle_bash")
    print("Tool command: needle mcp serve")
    print(f"Runtime command: {_format_command(_runtime_manage_command(loaded))}")
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


def _setup_codex(
    args: argparse.Namespace,
    loaded_package: LoadedPackage | None = None,
) -> int:
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

    loaded = loaded_package
    if loaded is None:
        try:
            loaded = load_active_package(host_binding="mcp/bash")
        except PackageConfigError as exc:
            _print_error(exc)
            return 1

    command = _codex_add_command()
    print("Needle for Codex CLI")
    print("Tool name: needle_bash")
    print("Tool command: needle mcp serve")
    print(f"Runtime command: {_format_command(_runtime_manage_command(loaded))}")
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
    root = naming.model_root()
    local_dir = naming.model_dir_for_repo(repo)
    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError:
        print(
            "error: this Needle install can set up host adapters, but it does not include "
            "the MLX backend/model download dependencies needed for real pruning.",
            file=sys.stderr,
        )
        print(
            "developer preview path: `uv tool install --editable '.[backend-code-pruner-mlx]'`, "
            "then run `needle model download`",
            file=sys.stderr,
        )
        return 1
    resolved_revision = ""
    try:
        info = HfApi().model_info(repo, revision=revision or None)
        resolved_revision = str(getattr(info, "sha", "") or "")
    except Exception as exc:  # noqa: BLE001 - download may still produce a useful local error.
        print(f"warning: could not resolve model revision before download: {exc}", file=sys.stderr)
    root.mkdir(parents=True, exist_ok=True)
    path = snapshot_download(
        repo,
        revision=revision or None,
        local_dir=str(local_dir),
        cache_dir=str(root / ".hf-cache"),
    )
    print(path)
    if resolved_revision:
        _write_model_provenance(
            local_dir,
            repo=repo,
            requested_revision=revision or "default",
            resolved_revision=resolved_revision,
        )
        print(f"revision: {resolved_revision}")
    return 0


def _write_model_provenance(
    local_dir: Path,
    *,
    repo: str,
    requested_revision: str,
    resolved_revision: str,
) -> None:
    data = {
        "repo": repo,
        "requested_revision": requested_revision,
        "resolved_revision": resolved_revision,
    }
    (local_dir / "needle-model.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show raw registry protocol, capability, backend, and host ids.",
    ),
) -> None:
    """List registry packages."""
    _exit_with(_package_list(_ns(host_binding=host_binding, verbose=verbose)))


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
    package_id: str = typer.Argument(..., help="Package id, for example e24z/mlx-pi-soft-lamr."),
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
    """Stop Needle and remove Needle-owned local state.

    Host adapters stay host-native: preview with `needle uninstall`, then run
    `needle setup pi --uninstall` or `needle setup claude-code --uninstall`.
    """
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
    package_id: str | None = typer.Option(
        None,
        "--package",
        help="Package id used to derive runtime environment.",
    ),
    host_binding: str | None = typer.Option(
        None,
        "--host-binding",
        help="Host binding used for default package selection.",
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Debug mode: start from inherited environment instead of package graph.",
    ),
) -> None:
    """Run the machine-wide model residency manager."""
    from .runtime import cli as runtime_cli

    argv = ["manage"]
    if package_id:
        argv.extend(["--package", package_id])
    if host_binding:
        argv.extend(["--host-binding", host_binding])
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
    package_id: str | None = typer.Option(
        None,
        "--package",
        help="Package id used to derive manager runtime environment.",
    ),
    host_binding: str | None = typer.Option(
        None,
        "--host-binding",
        help="Host binding used for manager package selection.",
    ),
) -> None:
    """Hold a session lease against the manager."""
    from .runtime import cli as runtime_cli

    argv = ["session"]
    if session:
        argv.extend(["--session", session])
    if package_id:
        argv.extend(["--package", package_id])
    if host_binding:
        argv.extend(["--host-binding", host_binding])
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
        help="Show the guided setup plan without changing Pi, Claude Code, or Codex.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Use the safest default answers and run setup for the selected agent.",
    ),
    host: str | None = typer.Option(
        None,
        "--host",
        help="Agent to connect: pi, claude-code, or codex.",
    ),
    package_id: str | None = typer.Option(
        None,
        "--package",
        help="Needle package id to use for the selected agent.",
    ),
    from_homebrew: bool = typer.Option(
        False,
        "--from-homebrew",
        help="Entry point used by the Homebrew post-install hook.",
    ),
    skip_canary: bool = typer.Option(
        False,
        "--skip-canary",
        help="Skip the Pi canary if the wizard installs Pi.",
    ),
    scope: str = typer.Option(
        "local",
        "--scope",
        help="Claude MCP scope if the wizard installs Claude Code.",
    ),
) -> None:
    """Run Needle's guided setup wizard."""
    if ctx.invoked_subcommand is not None:
        return
    _exit_with(
        _setup_wizard(
            _ns(
                dry_run=dry_run,
                yes=yes,
                host=host,
                package_id=package_id,
                from_homebrew=from_homebrew,
                skip_canary=skip_canary,
                scope=scope,
            )
        )
    )


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
    package_id: str | None = typer.Option(
        None,
        "--package",
        help="Package id to select before installing the Pi adapter.",
    ),
) -> None:
    """Install or remove Needle's Pi adapter using Pi's native package flow."""
    if uninstall_adapter:
        _exit_with(_setup_pi(_ns(dry_run=dry_run, uninstall=True, skip_canary=skip_canary)))
    _exit_with(
        _run_setup_host(
            host="pi",
            package_id=package_id,
            dry_run=dry_run,
            skip_canary=skip_canary,
            scope="local",
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
    package_id: str | None = typer.Option(
        None,
        "--package",
        help="Package id to select before installing the Claude Code MCP server.",
    ),
) -> None:
    """Install or remove Needle's Claude Code MCP server."""
    if uninstall_adapter:
        _exit_with(_setup_claude_code(_ns(dry_run=dry_run, uninstall=True, scope=scope)))
    _exit_with(
        _run_setup_host(
            host="claude-code",
            package_id=package_id,
            dry_run=dry_run,
            skip_canary=False,
            scope=scope,
        )
    )


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
    package_id: str | None = typer.Option(
        None,
        "--package",
        help="Package id to select before installing the Codex MCP server.",
    ),
) -> None:
    """Install or remove Needle's experimental Codex MCP server."""
    if uninstall_adapter:
        _exit_with(_setup_codex(_ns(dry_run=dry_run, uninstall=True)))
    _exit_with(
        _run_setup_host(
            host="codex",
            package_id=package_id,
            dry_run=dry_run,
            skip_canary=False,
            scope="local",
        )
    )


@statusline_app.command("claude-code")
def statusline_claude_code(
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Disable ANSI color and animation glyphs for tests or plain terminals.",
    ),
) -> None:
    """Render a compact Claude Code statusline from Needle runtime health."""
    _exit_with(_statusline_claude_code(_ns(plain=plain)))


@statusline_app.command("codex")
def statusline_codex(
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Disable ANSI color and animation glyphs for tests or plain terminals.",
    ),
) -> None:
    """Render a compact Codex CLI statusline from Needle runtime health."""
    _exit_with(_statusline_claude_code(_ns(plain=plain)))


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
