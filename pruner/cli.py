"""Command line for the pruner.

  manage   run the machine-wide model residency manager (one per machine)
  session  hold a session lease against the manager (what the monitor runs)
  prune    pipe stdin through the manager, print the result

For now invoked as `python3 -m pruner <cmd>`; a `hay` console-script alias can
come later."""

from __future__ import annotations

import argparse
import os
import sys

from . import client, naming
from .manager import serve_manager
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
    return run_session()


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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=naming.APP_NAME,
        description=f"context-pruning manager (codename: {naming.APP_NAME})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    mp = sub.add_parser("manage", help="run the machine-wide model residency manager")
    mp.set_defaults(func=_manage)

    ssp = sub.add_parser("session", help="hold a session lease against the manager")
    ssp.set_defaults(func=_session)

    pp = sub.add_parser("prune", help="send stdin to the manager, print the result")
    pp.add_argument("--query", "-q", default="", help="relevance query / goal")
    pp.set_defaults(func=_prune)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
