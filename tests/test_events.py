"""events.py round-trip + kill-switch, and the manager emitting the lifecycle
events the status surface relies on. The manager test injects a capturing emit,
so it never touches disk.

Run: PYTHONPATH=src python3 tests/test_events.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.runtime import events  # noqa: E402
from needle.runtime.manager import Manager  # noqa: E402


def test_emit_tail_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        os.environ["NEEDLE_HOME"] = d
        os.environ.pop("HAY_NO_EVENTS", None)
        os.environ.pop("NEEDLE_NO_EVENTS", None)
        events.emit("alpha", n=1)
        events.emit("beta", reason="x")
        got = events.tail(10)
        assert [e["event"] for e in got] == ["alpha", "beta"], got
        assert got[0]["n"] == 1 and got[1]["reason"] == "x"
        assert all("ts" in e for e in got)
    os.environ.pop("NEEDLE_HOME", None)


def test_emit_creates_private_event_log_under_permissive_umask() -> None:
    old_umask = os.umask(0)
    try:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir(mode=0o777)
            os.chmod(home, 0o777)
            os.environ["NEEDLE_HOME"] = str(home)
            os.environ.pop("HAY_NO_EVENTS", None)
            os.environ.pop("NEEDLE_NO_EVENTS", None)

            events.emit("alpha", n=1)
            path = home / "events.jsonl"
            assert home.stat().st_mode & 0o777 == 0o700
            assert path.stat().st_mode & 0o777 == 0o600

            os.chmod(path, 0o666)
            events.emit("beta", n=2)
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_umask)
        os.environ.pop("NEEDLE_HOME", None)


def test_event_rotation_repairs_backup_permissions() -> None:
    old_umask = os.umask(0)
    old_max = events.MAX_BYTES
    try:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            path = home / "events.jsonl"
            path.write_text("x" * 32, encoding="utf-8")
            os.chmod(path, 0o644)
            os.environ["NEEDLE_HOME"] = str(home)
            os.environ.pop("HAY_NO_EVENTS", None)
            os.environ.pop("NEEDLE_NO_EVENTS", None)
            events.MAX_BYTES = 1

            events.emit("rotated", n=1)

            rotated = home / "events.jsonl.1"
            assert rotated.exists()
            assert path.exists()
            assert rotated.stat().st_mode & 0o777 == 0o600
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        events.MAX_BYTES = old_max
        os.umask(old_umask)
        os.environ.pop("NEEDLE_HOME", None)


def test_kill_switch() -> None:
    with tempfile.TemporaryDirectory() as d:
        os.environ["NEEDLE_HOME"] = d
        os.environ["NEEDLE_NO_EVENTS"] = "1"
        events.emit("should_not_write")
        assert events.tail(10) == [], "NEEDLE_NO_EVENTS=1 must suppress the local log"
    os.environ.pop("NEEDLE_NO_EVENTS", None)
    os.environ.pop("NEEDLE_HOME", None)


def test_legacy_event_env_aliases_still_work() -> None:
    with tempfile.TemporaryDirectory() as d:
        os.environ["HAY_HOME"] = d
        os.environ["HAY_NO_EVENTS"] = "1"
        events.emit("should_not_write")
        assert events.tail(10) == [], "HAY_NO_EVENTS=1 must remain a legacy compatibility alias"
    os.environ.pop("HAY_NO_EVENTS", None)
    os.environ.pop("HAY_HOME", None)


class _Spy:
    name = "spy"

    def prune(self, *, text: str, query: str) -> str:
        return text[:1]

    def evict(self) -> None:
        pass


def test_manager_emits_lifecycle() -> None:
    seen: list[tuple[str, dict]] = []
    cap = lambda event, **f: seen.append((event, f))  # noqa: E731

    m = Manager(lambda: _Spy(), emit=cap, heavy=False, idle_timeout=0.0)
    m.handle({"op": "lease", "session": "s1"})
    m.handle({"op": "prune", "text": "hello", "query": "q"})  # cold load -> model_load
    m.handle({"op": "release", "session": "s1"})
    m.maintain()  # records empty-since
    m.maintain()  # idle + resident -> evict
    names = [e for e, _ in seen]
    for expected in ("lease", "model_load", "release", "model_evict"):
        assert expected in names, (expected, names)

    seen.clear()
    m2 = Manager(lambda: _Spy(), emit=cap, heavy=False, max_prune_chars=3)
    m2.handle({"op": "prune", "text": "toolong", "query": "q"})  # 7 > 3 -> oversize
    assert ("passthrough", {"reason": "oversize", "chars": 7}) in seen, seen


def main() -> int:
    test_emit_tail_roundtrip()
    test_emit_creates_private_event_log_under_permissive_umask()
    test_event_rotation_repairs_backup_permissions()
    test_kill_switch()
    test_legacy_event_env_aliases_still_work()
    test_manager_emits_lifecycle()
    print("test_events OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
