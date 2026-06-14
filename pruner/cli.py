"""Command line. `serve` runs the server in the foreground; `prune` pipes
stdin through it. This is where `hay serve` will come from once we add a
console-script alias; for now it is `python3 -m pruner serve`."""

from __future__ import annotations

import argparse
import sys

from . import client, naming
from .backends import get_backend
from .server import serve_forever


def _serve(args: argparse.Namespace) -> int:
    backend = get_backend()

    def ready(path) -> None:
        # stderr only: a monitor surfaces STDOUT lines to the agent as
        # notifications, and we don't want the agent narrating routine startup.
        print(
            f"{naming.APP_NAME}: listening on {path} (backend={backend.name})",
            file=sys.stderr,
            flush=True,
        )

    try:
        serve_forever(backend=backend, ready_cb=ready)
    except KeyboardInterrupt:
        print(f"\n{naming.APP_NAME}: stopped", file=sys.stderr)
    return 0


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
        description=f"context-pruning server (codename: {naming.APP_NAME})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the pruning server in the foreground")
    sp.set_defaults(func=_serve)

    pp = sub.add_parser("prune", help="send stdin to the server, print the result")
    pp.add_argument("--query", "-q", default="", help="relevance query / goal")
    pp.set_defaults(func=_prune)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
