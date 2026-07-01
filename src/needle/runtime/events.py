"""Local event log: the foundation for telemetry, but LOCAL ONLY.

Append-only newline-delimited JSON, one manager lifecycle event per line, in
~/.needle/events.jsonl. Nothing here transmits anything off the machine -- a future
remote sink (Cloudflare/Supabase) would plug into `emit()` behind an explicit,
default-off opt-in. This file is just a local log, like manager.log.

On by default so a tester's box is diagnosable out of the box;
`NEEDLE_NO_EVENTS=1` disables it. `HAY_NO_EVENTS=1` remains a legacy
compatibility alias. Fail-silent (logging must never break the manager) and
stdlib-only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import naming

def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


# Rotate at ~1 MB to one backup (.jsonl.1) so the log can't grow unbounded.
MAX_BYTES = int(_env("NEEDLE_EVENTS_MAX_BYTES", "HAY_EVENTS_MAX_BYTES") or str(1 << 20))


def _path() -> Path:
    env = _env("NEEDLE_EVENTS", "HAY_EVENTS")
    if env:
        return Path(env).expanduser()
    return naming.app_home() / "events.jsonl"


def enabled() -> bool:
    return (_env("NEEDLE_NO_EVENTS", "HAY_NO_EVENTS") or "").lower() not in {"1", "true", "yes"}


def emit(event: str, **fields: object) -> None:
    """Append one event. Never raises; a logging failure is not the agent's problem."""
    if not enabled():
        return
    try:
        rec = {"ts": round(time.time(), 3), "event": event, **fields}
        line = json.dumps(rec, default=str)
        path = _path()
        naming.ensure_runtime_parent(path.parent)
        _rotate(path)
        with naming.open_private_append(path) as f:
            f.write(line + "\n")
    except Exception:
        pass


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            rotated = path.with_name(path.name + ".1")
            os.replace(path, rotated)  # events.jsonl -> events.jsonl.1
            naming.ensure_private_file(rotated)
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
