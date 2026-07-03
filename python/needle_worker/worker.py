"""Python model worker entrypoint for the Rust manager.

This process owns Python/MLX model memory. The Rust manager owns when this
process is started, stopped, and considered part of Needle's runtime state.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _write_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def load_backend() -> Any:
    """Load the concrete MLX backend for the worker process.

    The Rust-owned runtime should fail visibly if the model backend cannot load,
    not degrade to a fake/pass-through backend from the older MCP path.
    """
    from needle_worker.soft_lamr.model import CodePrunerBackend

    return CodePrunerBackend()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load Needle's Python model worker.")
    parser.add_argument(
        "--load-only",
        action="store_true",
        help="Load the configured backend, report status, then exit.",
    )
    args = parser.parse_args(argv)

    try:
        backend = load_backend()
    except Exception as exc:  # noqa: BLE001 - startup errors must cross the process boundary.
        _write_json({"ok": False, "status": "failed", "error": str(exc)})
        return 1

    _write_json(
        {
            "ok": True,
            "status": "resident",
            "backend": getattr(backend, "name", "unknown"),
        }
    )
    if args.load_only:
        evict = getattr(backend, "evict", None)
        if callable(evict):
            evict()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
