"""Command line for the pruner.

  manage   run the machine-wide model residency manager (one per machine)
  session  hold a session lease against the manager (what the monitor runs)
  prune    pipe stdin through the manager, print the result
  status   operator snapshot: live residency + recent events (stdlib; works broken)

For now invoked as `python3 -m pruner <cmd>`; a `hay` console-script alias can
come later."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time

from . import client, events, naming
from .manager import serve_manager
from .session import run_session


def _manage(args: argparse.Namespace) -> int:
    def ready(path) -> None:
        # stderr only: a monitor surfaces STDOUT lines to the agent as
        # notifications, and we don't want it narrating routine startup.
        print(
            f"{naming.APP_NAME}: manager listening on {path} "
            f"(backend={os.environ.get('HAY_BACKEND', 'code-pruner')}, lazy-load on first prune)",
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
    bits = [
        f"[{naming.APP_NAME}] backend={resp['backend']}",
        f"in={resp['original_len']}",
        f"out={resp['pruned_len']}",
        f"saved={saved}",
    ]
    stats = resp.get("stats") if isinstance(resp.get("stats"), dict) else {}
    if stats:
        bits.extend(
            [
                f"tokens={stats.get('original_tokens', '?')}->{stats.get('pruned_tokens', '?')}",
                f"token_saved={stats.get('saved_tokens', '?')}",
                f"chunks={stats.get('chunks', '?')}",
                f"pruner_input_tokens={stats.get('model_input_tokens', '?')}",
            ]
        )
    print(" ".join(bits), file=sys.stderr)
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


def _status_payload(stats: dict | None, recent: list[dict]) -> dict:
    """Structured status for dashboards and other Unix-y wrappers."""
    manager = stats if stats and stats.get("ok") else {"ok": False, "state": "down"}
    return {
        "ok": bool(stats and stats.get("ok")),
        "app": naming.APP_NAME,
        "generated_at": time.time(),
        "socket": str(naming.manager_socket_path()),
        "manager": manager,
        "events": recent,
    }


def _read_status(events_n: int) -> tuple[dict | None, list[dict]]:
    try:
        stats = client.stats(timeout=0.5)
    except OSError:
        stats = None  # no manager / unreachable -> "down", still show recent events
    return stats, events.tail(events_n)


def _status(args: argparse.Namespace) -> int:
    def emit_once(*, clear: bool = False) -> None:
        stats, recent = _read_status(args.events)
        if args.json:
            print(json.dumps(_status_payload(stats, recent), sort_keys=True), flush=True)
            return
        if clear:
            sys.stdout.write("\033[2J\033[H")
        print(_render_status(stats, recent), flush=True)

    if not args.watch:
        emit_once()
        return 0

    if args.interval <= 0:
        print("error: --interval must be > 0", file=sys.stderr)
        return 2

    try:
        while True:
            emit_once(clear=not args.json)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
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
    stp.add_argument("--json", action="store_true", help="emit structured JSON")
    stp.add_argument("--watch", action="store_true", help="refresh until interrupted")
    stp.add_argument(
        "--interval", type=float, default=1.0,
        help="seconds between refreshes with --watch (default: 1.0)",
    )
    stp.set_defaults(func=_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
