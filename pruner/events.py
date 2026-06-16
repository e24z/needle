"""Local event log: the foundation for telemetry, but LOCAL ONLY.

Append-only newline-delimited JSON, one manager lifecycle event per line, in
~/.hay/events.jsonl. Nothing here transmits anything off the machine -- a future
remote sink (Cloudflare/Supabase) would plug into `emit()` behind an explicit,
default-off opt-in. This file is just a local log, like manager.log.

On by default so a tester's box is diagnosable out of the box; `HAY_NO_EVENTS=1`
disables it. Fail-silent (logging must never break the manager) and stdlib-only
(so readers like `pruner status` work under bare python3).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import naming

# Rotate at ~1 MB to one backup (.jsonl.1) so the log can't grow unbounded.
MAX_BYTES = int(os.environ.get("HAY_EVENTS_MAX_BYTES", str(1 << 20)))


def _path() -> Path:
    return naming.app_home() / "events.jsonl"


def enabled() -> bool:
    return os.environ.get("HAY_NO_EVENTS", "").lower() not in {"1", "true", "yes"}


def emit(event: str, **fields: object) -> None:
    """Append one event. Never raises; a logging failure is not the agent's problem."""
    if not enabled():
        return
    try:
        rec = {"ts": round(time.time(), 3), "event": event, **fields}
        line = json.dumps(rec, default=str)
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))  # events.jsonl -> events.jsonl.1
    except OSError:
        pass


def tail(n: int = 20) -> list[dict]:
    """The last n events (current file only). [] on any error."""
    try:
        with open(_path()) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
