"""Command line for the pruner.

  manage   run the machine-wide model residency manager (one per machine)
  session  hold a session lease against the manager (what the monitor runs)
  prune    pipe stdin through the manager, print the result
  status   operator snapshot: live residency + recent events (stdlib; works broken)
  stop     ask the resident manager to shut down cleanly
  package  inspect and select Needle runtime packages

For now invoked as `python3 -m pruner <cmd>`; a `hay` console-script alias can
come later."""

from __future__ import annotations

import argparse
import datetime
import os
import sys

from . import client, events, naming
from .manager import serve_manager
from .package_config import (
    PackageConfigError,
    active_package_selection,
    load_active_package,
    package_config_path,
    package_summaries,
    set_configured_package_id,
)
from .session import run_session


def _manage(args: argparse.Namespace) -> int:
    def ready(path) -> None:
        # stderr only: a monitor surfaces STDOUT lines to the agent as
        # notifications, and we don't want it narrating routine startup.
        print(
            f"{naming.APP_NAME}: manager listening on {path} "
            f"(backend={os.environ.get('HAY_BACKEND', 'fake')}, lazy-load on first prune)",
            file=sys.stderr,
            flush=True,
        )

    try:
        serve_manager(ready_cb=ready)
    except KeyboardInterrupt:
        print(f"\n{naming.APP_NAME}: manager stopped", file=sys.stderr)
    return 0


def _session(args: argparse.Namespace) -> int:
    return run_session(session_id=args.session or None)


def _prune(args: argparse.Namespace) -> int:
    text = sys.stdin.read()
    resp = client.prune(text=text, query=args.query)
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    sys.stdout.write(resp["text"])
    saved = resp["original_len"] - resp["pruned_len"]
    print(
        f"[{naming.APP_NAME}] backend={resp['backend']} "
        f"in={resp['original_len']} out={resp['pruned_len']} saved={saved}",
        file=sys.stderr,
    )
    return 0


_PRESSURE = {1: "normal", 2: "warning", 4: "critical"}


def _fmt_ts(ts: object) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _render_status(stats: dict | None, recent: list[dict]) -> str:
    """Pure: render an operator snapshot from (live stats, recent events).
    Honest about a degraded backend -- never prints 'ready' for a fake."""
    lines: list[str] = []
    if not stats or not stats.get("ok"):
        lines.append(f"{naming.APP_NAME} manager: down (not running)")
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
        lines.append(f"{naming.APP_NAME} manager: {state}")
        lines.append(
            f"  sessions {stats.get('sessions', 0)}"
            f"  ·  version {str(stats.get('version', ''))[:12]}"
            f"  ·  pressure {_PRESSURE.get(stats.get('pressure'), '?')}"
            f"  ·  free {free}"
        )
    if recent:
        lines.append("")
        lines.append("recent events:")
        for e in recent:
            extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in {"ts", "event"})
            lines.append(f"  {_fmt_ts(e.get('ts'))}  {str(e.get('event', '?')):<16} {extra}")
    return "\n".join(lines)


def _status(args: argparse.Namespace) -> int:
    try:
        stats = client.stats(timeout=0.5)
    except OSError:
        stats = None  # no manager / unreachable -> "down", still show recent events
    print(_render_status(stats, events.tail(args.events)))
    return 0


def _stop(args: argparse.Namespace) -> int:
    try:
        resp = client.stop(timeout=0.5)
    except OSError as exc:
        print(f"{naming.APP_NAME}: manager not running ({exc})", file=sys.stderr)
        return 1
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"{naming.APP_NAME}: manager stopping", file=sys.stderr)
    return 0


def _package_list(args: argparse.Namespace) -> int:
    try:
        summaries = package_summaries(host_binding=args.host_binding)
    except PackageConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
            f"capability={capabilities}  "
            f"backend={item.get('backend', '?')}  "
            f"host={item.get('host_binding', '?')}"
        )
    return 0


def _package_current(args: argparse.Namespace) -> int:
    package_id, source = active_package_selection()
    print(package_id)
    print(f"source: {source}")
    return 0


def _package_use(args: argparse.Namespace) -> int:
    try:
        loaded = set_configured_package_id(args.package_id)
    except PackageConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"selected package: {loaded.package_id}")
    print(f"config: {package_config_path()}")
    print(f"capability: {', '.join(loaded.capability_ids)}")
    print(f"backend: {loaded.backend_id}")
    print("restart the manager for running sessions: python3 -m pruner stop")
    return 0


def _package_doctor(args: argparse.Namespace) -> int:
    try:
        loaded = load_active_package(package_id=args.package_id or None)
    except PackageConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    selected, source = active_package_selection()
    lines = [
        f"package: {loaded.package_id}",
        f"active selection: {selected} ({source})",
        f"protocol: {loaded.protocol['id']}",
        f"capability: {', '.join(loaded.capability_ids)}",
        f"backend: {loaded.backend_id}",
        f"host binding: {loaded.binding_id}",
        f"claim card: {loaded.claim_card['id']}",
        f"package card: {loaded.package_card_path}",
    ]
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=naming.APP_NAME,
        description=f"context-pruning manager (codename: {naming.APP_NAME})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    mp = sub.add_parser("manage", help="run the machine-wide model residency manager")
    mp.set_defaults(func=_manage)

    ssp = sub.add_parser("session", help="hold a session lease against the manager")
    ssp.add_argument(
        "--session", default="",
        help="host session id to lease under (an adapter passes its agent's id)",
    )
    ssp.set_defaults(func=_session)

    pp = sub.add_parser("prune", help="send stdin to the manager, print the result")
    pp.add_argument("--query", "-q", default="", help="relevance query / goal")
    pp.set_defaults(func=_prune)

    stp = sub.add_parser("status", help="operator snapshot: residency + recent events")
    stp.add_argument("--events", "-n", type=int, default=12, help="recent events to show")
    stp.set_defaults(func=_status)

    stop_p = sub.add_parser("stop", help="ask the resident manager to shut down cleanly")
    stop_p.set_defaults(func=_stop)

    pkg = sub.add_parser("package", help="inspect and select Needle runtime packages")
    pkg_sub = pkg.add_subparsers(dest="package_cmd", required=True)

    pkg_list = pkg_sub.add_parser("list", help="list registry packages")
    pkg_list.add_argument(
        "--host-binding",
        default="",
        help="optional host binding filter, for example pi/native-tools",
    )
    pkg_list.set_defaults(func=_package_list)

    pkg_current = pkg_sub.add_parser("current", help="show the active package id")
    pkg_current.set_defaults(func=_package_current)

    pkg_use = pkg_sub.add_parser("use", help="persist a package selection")
    pkg_use.add_argument("package_id", help="package id, for example e24z/pi-local-mac")
    pkg_use.set_defaults(func=_package_use)

    pkg_doctor = pkg_sub.add_parser("doctor", help="validate and explain a package")
    pkg_doctor.add_argument("package_id", nargs="?", default="", help="package id to inspect; defaults to active")
    pkg_doctor.set_defaults(func=_package_doctor)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
