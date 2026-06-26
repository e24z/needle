"""Command line for the Needle runtime.

  manage   run the machine-wide model residency manager
  session  hold a session lease against the manager
  prune    pipe stdin through the manager
  status   show residency and recent events
  stop     ask the resident manager to shut down cleanly
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

from . import client, events, naming
from .manager import serve_manager
from .session import run_session


def _apply_runtime_launch_env(
    *,
    package_id: str = "",
    host_binding: str = "",
    raw: bool = False,
):
    """Apply package-derived runtime env for manager startup.

    Raw/debug starts deliberately skip the package graph; all normal starts use
    the same launch plan shape as host adapters.
    """
    if raw:
        return None
    from needle.registry import runtime_launch_plan

    plan = runtime_launch_plan(
        package_id=package_id or None,
        host_binding=host_binding or None,
    )
    os.environ.update(plan.env)
    return plan


def _manage(args: argparse.Namespace) -> int:
    try:
        plan = _apply_runtime_launch_env(
            package_id=args.package,
            host_binding=args.host_binding,
            raw=args.raw,
        )
    except ValueError as exc:
        print(f"{naming.APP_NAME}: could not resolve runtime package: {exc}", file=sys.stderr)
        return 1

    def ready(path) -> None:
        # stderr only: a monitor surfaces STDOUT lines to the agent as
        # notifications, and we don't want it narrating routine startup.
        profile = (
            f", package={plan.package_id}, profile={plan.runtime_profile}"
            if plan is not None
            else ", raw runtime env"
        )
        print(
            f"{naming.APP_NAME}: manager listening on {path} "
            f"(backend={os.environ.get('NEEDLE_BACKEND') or os.environ.get('HAY_BACKEND', 'fake')}, "
            f"lazy-load on first prune{profile})",
            file=sys.stderr,
            flush=True,
        )

    try:
        serve_manager(ready_cb=ready)
    except (OSError, RuntimeError) as exc:
        print(f"{naming.APP_NAME}: manager failed to start: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\n{naming.APP_NAME}: manager stopped", file=sys.stderr)
    return 0


def _session(args: argparse.Namespace) -> int:
    return run_session(session_id=args.session or None)


def _prune(args: argparse.Namespace) -> int:
    text = sys.stdin.read()
    try:
        resp = client.prune(text=text, query=args.query)
    except OSError as exc:
        print(
            f"error: {naming.APP_NAME} manager is not reachable at {naming.manager_socket_path()}: {exc}",
            file=sys.stderr,
        )
        print(f"hint: run `python -m needle.runtime status` to inspect it", file=sys.stderr)
        print(f"hint: run `python -m needle.runtime manage` to start it", file=sys.stderr)
        return 1
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
    detail = _render_prune_summary(resp.get("stats"))
    if detail:
        print(f"[{naming.APP_NAME}] {detail}", file=sys.stderr)
    return 0


_PRESSURE = {1: "normal", 2: "warning", 4: "critical"}


def _fmt_ts(ts: object) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _fmt_number(value: object) -> str | None:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.1f}"
    if isinstance(value, str) and value:
        return value
    return None


def _fmt_ms(value: object) -> str | None:
    if isinstance(value, (int, float)):
        return f"{float(value):.1f}ms"
    return None


def _fmt_percent(value: object) -> str | None:
    if isinstance(value, (int, float)):
        return f"{float(value) * 100:.1f}%"
    return None


def _render_prune_summary(stats: object) -> str:
    if not isinstance(stats, dict):
        return ""
    parts: list[str] = []
    reason = _fmt_number(stats.get("passthrough_reason"))
    if reason:
        parts.append(f"passthrough {reason}")
    saved = _fmt_number(stats.get("saved_chars"))
    if saved is not None:
        parts.append(f"saved {saved} chars")
    chunks = _fmt_number(stats.get("chunks"))
    if chunks is not None:
        parts.append(f"chunks {chunks}")
    batches = _fmt_number(stats.get("batches"))
    if batches is not None:
        parts.append(f"batches {batches}")
    batch_sizes = stats.get("batch_sizes")
    if isinstance(batch_sizes, list) and batch_sizes:
        shown = ",".join(str(item) for item in batch_sizes[:8])
        suffix = ",..." if len(batch_sizes) > 8 else ""
        parts.append(f"batch_sizes [{shown}{suffix}]")
    max_length = _fmt_number(stats.get("max_length"))
    if max_length is not None:
        parts.append(f"max_len {max_length}")
    padding = _fmt_percent(stats.get("padding_waste_ratio"))
    if padding:
        parts.append(f"padding {padding}")
    truncated = _fmt_number(stats.get("truncated_code_tokens"))
    if truncated is not None:
        parts.append(f"truncated {truncated} tokens")
    forward = _fmt_ms(stats.get("forward_eval_ms"))
    if forward:
        parts.append(f"forward {forward}")
    total = _fmt_ms(stats.get("total_ms"))
    if total:
        parts.append(f"total {total}")
    return " · ".join(parts)


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
        last_prune = stats.get("last_prune")
        detail = _render_prune_summary(last_prune)
        if detail:
            backend = last_prune.get("backend") if isinstance(last_prune, dict) else None
            prefix = f"{backend} · " if isinstance(backend, str) and backend else ""
            lines.append(f"  last prune: {prefix}{detail}")
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
        print(f"{naming.APP_NAME}: manager already stopped ({exc})", file=sys.stderr)
        return 0
    if not resp.get("ok"):
        print(f"error: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"{naming.APP_NAME}: manager stopping", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=naming.APP_NAME,
        description=f"context-pruning manager (codename: {naming.APP_NAME})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    mp = sub.add_parser("manage", help="run the machine-wide model residency manager")
    mp.add_argument("--package", default="", help="package id used to derive runtime env")
    mp.add_argument("--host-binding", default="", help="host binding used for package selection")
    mp.add_argument(
        "--raw",
        action="store_true",
        help="debug mode: start from the inherited environment instead of the package graph",
    )
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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
