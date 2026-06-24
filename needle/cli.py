"""Needle's public CLI.

The pruner module is the runtime engine. Needle owns package/registry selection
because a package composes protocol, capability, backend, host binding, docs,
privacy, accounting, and evidence.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
from pathlib import Path

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


def _print_error(message: object) -> None:
    print(f"error: {message}", file=sys.stderr)


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
    print(f"runtime extra: {plan.extra}")
    print(f"runtime command: {' '.join(plan.command)}")
    print("restart the resident runtime for running sessions: needle stop")
    return 0


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
        f"runtime extra: {plan.extra}",
        f"runtime module: {plan.module}",
        f"runtime command: {' '.join(plan.command)}",
        f"host binding: {loaded.binding_id}",
        f"claim card: {loaded.claim_card['id']}",
        f"package card: {loaded.package_card_path}",
    ]
    for ref, path in loaded.evidence_paths.items():
        lines.append(f"evidence: {ref} -> {path}")
    print("\n".join(lines))
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
        print(f"Needle: runtime not running ({exc})", file=sys.stderr)
        return 1
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
        print("  Pi:     pi uninstall .")
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
    print("  Pi:     pi uninstall .")
    print("Remove the CLI entrypoint with your Python tool installer, for example:")
    print("  uv tool uninstall needle")
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
            "error: model download needs the MLX backend extra; "
            "run `uv run --extra backend-code-pruner-mlx needle model download`",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="needle",
        description="Needle package and runtime control plane",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    package = sub.add_parser("package", help="inspect and select Needle packages")
    package_sub = package.add_subparsers(dest="package_cmd", required=True)

    package_list = package_sub.add_parser("list", help="list registry packages")
    package_list.add_argument(
        "--host-binding",
        default="",
        help="optional host binding filter, for example pi/native-tools",
    )
    package_list.set_defaults(func=_package_list)

    package_current = package_sub.add_parser("current", help="show the active package id")
    package_current.add_argument(
        "--host-binding",
        default="",
        help="optional host binding scope, for example pi/native-tools",
    )
    package_current.set_defaults(func=_package_current)

    package_use = package_sub.add_parser("use", help="persist a package selection")
    package_use.add_argument("package_id", help="package id, for example e24z/pi-local-mac")
    package_use.add_argument(
        "--host-binding",
        default="",
        help="optional required host binding; defaults to the selected package's binding",
    )
    package_use.set_defaults(func=_package_use)

    package_doctor = package_sub.add_parser("doctor", help="validate and explain a package")
    package_doctor.add_argument("package_id", nargs="?", default="", help="package id to inspect; defaults to active")
    package_doctor.add_argument(
        "--host-binding",
        default="",
        help="optional host binding scope, for example pi/native-tools",
    )
    package_doctor.set_defaults(func=_package_doctor)

    status = sub.add_parser("status", help="operator snapshot: residency + recent events")
    status.add_argument("--events", "-n", type=int, default=12, help="recent events to show")
    status.set_defaults(func=_status)

    stop = sub.add_parser("stop", help="ask the resident runtime to shut down cleanly")
    stop.set_defaults(func=_stop)

    uninstall = sub.add_parser(
        "uninstall",
        help="stop Needle and remove Needle-owned local state",
    )
    uninstall.add_argument(
        "--yes",
        action="store_true",
        help="actually remove local runtime/config/model files",
    )
    uninstall.set_defaults(func=_uninstall)

    model = sub.add_parser("model", help="inspect, download, or remove local model files")
    model_sub = model.add_subparsers(dest="model_cmd", required=True)

    model_dir = model_sub.add_parser("dir", help="show the local directory for a model repo")
    model_dir.add_argument("--repo", default="", help="Hugging Face repo id; defaults to code-pruner")
    model_dir.set_defaults(func=_model_dir)

    model_download = model_sub.add_parser("download", help="download the configured model repo")
    model_download.add_argument("--repo", default="", help="Hugging Face repo id; defaults to code-pruner")
    model_download.set_defaults(func=_model_download)

    model_clean = model_sub.add_parser("clean", help="remove the local model directory")
    model_clean.add_argument("--yes", action="store_true", help="actually remove the model directory")
    model_clean.set_defaults(func=_model_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
