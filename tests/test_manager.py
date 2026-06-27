"""Manager: lazy model lifecycle, leases, idle eviction, first-writer-wins bind.

Fake spy backends + tiny timeouts -- no real model, no Claude.
Run: PYTHONPATH=src python3 tests/test_manager.py
"""

from __future__ import annotations

import os

os.environ["HAY_NO_EVENTS"] = "1"  # legacy compatibility alias; don't write the real local event log

import socket  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # runnable bare, like its siblings

from needle.runtime.manager import MANAGER_CONFIG_ENVS, Manager, _env, serve_manager  # noqa: E402
from needle.runtime import naming  # noqa: E402
from needle.runtime.protocol import decode, encode  # noqa: E402


class SpyBackend:
    name = "spy"

    def __init__(self) -> None:
        self.evicted = 0

    def prune(self, *, text: str, query: str) -> str:
        return text[: len(text) // 2]  # visibly shorter so we can assert pruning

    def evict(self) -> None:
        self.evicted += 1


class StatsBackend:
    name = "stats"

    def __init__(self) -> None:
        self.last_stats: dict[str, object] = {}

    def prune(self, *, text: str, query: str) -> str:
        self.last_stats = {
            "chunks": 3,
            "batches": 2,
            "batch_sizes": [2, 1],
            "max_length": 1024,
            "padding_waste_ratio": 0.125,
            "truncated_code_tokens": 7,
            "forward_eval_ms": 11.5,
            "total_ms": 15.25,
            "huge_internal": {"token_scores": [0.1, 0.2]},
        }
        return text[:4]


def _call(sock_path: Path, req: dict) -> dict:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(2)
    c.connect(str(sock_path))
    try:
        c.sendall(encode(req))
        with c.makefile("rb") as f:
            return decode(f.readline())
    finally:
        c.close()


def _wait_until(pred, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_manager_config_env_prefers_needle_names() -> None:
    env_names = [name for names in MANAGER_CONFIG_ENVS.values() for name in names]
    old = {name: os.environ.get(name) for name in env_names}
    try:
        for idx, names in enumerate(MANAGER_CONFIG_ENVS.values(), start=1):
            needle, legacy = names
            os.environ[legacy] = f"legacy-{idx}"
            os.environ[needle] = f"needle-{idx}"
            assert _env(names, "default") == f"needle-{idx}"
            os.environ.pop(needle)
            assert _env(names, "default") == f"legacy-{idx}"
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_manager_surfaces_bounded_backend_stats() -> None:
    seen: list[tuple[str, dict[str, object]]] = []
    manager = Manager(
        lambda: StatsBackend(),
        emit=lambda event, **fields: seen.append((event, fields)),
    )

    resp = manager.handle({"op": "prune", "text": "abcdefghij", "query": "q"})
    assert resp["ok"], resp
    assert resp["stats"]["chunks"] == 3, resp
    assert resp["stats"]["batches"] == 2, resp
    assert resp["stats"]["batch_sizes"] == [2, 1], resp
    assert resp["stats"]["padding_waste_ratio"] == 0.125, resp
    assert "huge_internal" not in resp["stats"], resp

    prune_events = [(name, fields) for name, fields in seen if name == "prune"]
    assert len(prune_events) == 1, seen
    event_fields = prune_events[0][1]
    assert event_fields["backend"] == "stats", event_fields
    assert event_fields["chunks"] == 3, event_fields
    assert event_fields["batch_sizes"] == [2, 1], event_fields
    assert event_fields["saved_chars"] == 6, event_fields

    stats = manager.handle({"op": "stats"})
    assert stats["last_prune"]["backend"] == "stats", stats
    assert stats["last_prune"]["chunks"] == 3, stats


def test_manager_stats_expose_runtime_identity() -> None:
    manager = Manager(
        lambda: StatsBackend(),
        runtime_identity={
            "package_id": "e24z/mlx-pi-soft-lamr",
            "host_binding": "pi/native-tools",
            "runtime_profile": "local_mlx_adaptive",
            "backend_id": "e24z/code-pruner-mlx",
        },
    )

    stats = manager.handle({"op": "stats"})

    assert stats["package_id"] == "e24z/mlx-pi-soft-lamr", stats
    assert stats["host_binding"] == "pi/native-tools", stats
    assert stats["runtime_profile"] == "local_mlx_adaptive", stats
    assert stats["backend_id"] == "e24z/code-pruner-mlx", stats


def test_code_version_changes_for_backend_affecting_files() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "needle"
        (root / "runtime").mkdir(parents=True)
        (root / "backends").mkdir()
        (root / "registry_data/backends/e24z").mkdir(parents=True)
        (root / "registry_data/packages/e24z").mkdir(parents=True)
        (root / "runtime/manager.py").write_text("runtime = 1\n", encoding="utf-8")
        (root / "backends/fake.py").write_text("backend = 1\n", encoding="utf-8")
        (root / "registry.py").write_text("registry = 1\n", encoding="utf-8")
        package_path = root / "registry_data/packages/e24z/pkg.yaml"
        backend_path = root / "registry_data/backends/e24z/backend.yaml"
        package_path.write_text('{"id":"pkg"}\n', encoding="utf-8")
        backend_path.write_text('{"id":"backend","launcher":{"command":["needle"]}}\n', encoding="utf-8")

        before = naming.code_version(root)
        backend_path.write_text('{"id":"backend","launcher":{"command":["needle","runtime"]}}\n', encoding="utf-8")
        after_backend = naming.code_version(root)
        (root / "backends/fake.py").write_text("backend = 2\n", encoding="utf-8")
        after_source = naming.code_version(root)

    assert before != after_backend
    assert after_backend != after_source


def main() -> int:
    test_manager_config_env_prefers_needle_names()
    test_manager_surfaces_bounded_backend_stats()
    test_manager_stats_expose_runtime_identity()
    test_code_version_changes_for_backend_affecting_files()
    tmp = Path(tempfile.mkdtemp()) / "manager.sock"
    builds: list[SpyBackend] = []

    def factory() -> SpyBackend:
        b = SpyBackend()
        builds.append(b)
        return b

    ready = threading.Event()
    stop = threading.Event()
    t = threading.Thread(
        target=serve_manager,
        kwargs=dict(
            backend_factory=factory,
            socket_path=tmp,
            ready_cb=lambda _p: ready.set(),
            stop_event=stop,
            lease_ttl=0.5,
            idle_timeout=0.25,
            poll_interval=0.03,
        ),
        daemon=True,
    )
    t.start()
    assert ready.wait(2), "manager never signalled ready"

    try:
        # Leasing does NOT load the model (lazy): nothing built yet.
        assert _call(tmp, {"op": "lease", "session": "s1"})["ok"]
        s = _call(tmp, {"op": "stats"})
        assert s["sessions"] == 1 and s["resident"] is False, s
        assert builds == [], "model loaded before any prune"

        # First prune loads the model.
        r = _call(tmp, {"op": "prune", "text": "abcdefghij", "query": "x"})
        assert r["ok"] and r["pruned_len"] < r["original_len"], r
        assert len(builds) == 1, builds
        assert _call(tmp, {"op": "stats"})["resident"] is True

        # Heartbeat holds the lease; no eviction while leased.
        time.sleep(0.12)
        assert _call(tmp, {"op": "heartbeat", "session": "s1"})["ok"]
        assert _call(tmp, {"op": "stats"})["sessions"] == 1
        assert builds[0].evicted == 0

        # Release -> idle -> model evicted AND dropped (memory freed).
        assert _call(tmp, {"op": "release", "session": "s1"})["ok"]
        assert _wait_until(lambda: builds[0].evicted >= 1), "model not evicted when idle"
        assert _call(tmp, {"op": "stats"})["resident"] is False

        # Next prune reloads (a fresh backend is built).
        assert _call(tmp, {"op": "prune", "text": "abcdefghij", "query": "x"})["ok"]
        assert len(builds) == 2, "model did not reload after eviction"

        # A crashed session (leases, never heartbeats) is reaped after lease_ttl.
        assert _call(tmp, {"op": "lease", "session": "s2"})["ok"]
        assert _wait_until(
            lambda: _call(tmp, {"op": "stats"})["sessions"] == 0
        ), "stale lease was not reaped"

        # First-writer-wins: a second manager on the same socket defers and returns.
        second = threading.Event()
        threading.Thread(
            target=lambda: (
                serve_manager(backend_factory=factory, socket_path=tmp, ready_cb=lambda _p: None),
                second.set(),
            ),
            daemon=True,
        ).start()
        assert second.wait(2), "second manager did not defer to the first"
        assert _call(tmp, {"op": "stats"})["ok"], "first manager stopped serving"

        assert _call(tmp, {"op": "stop"})["ok"], "manager did not accept stop"
        assert _wait_until(lambda: not tmp.exists()), "manager socket was not removed"

        ready_on_failed_bind = threading.Event()
        too_long = tmp.parent / ("x" * 200)
        try:
            serve_manager(
                backend_factory=factory,
                socket_path=too_long,
                ready_cb=lambda _p: ready_on_failed_bind.set(),
            )
        except RuntimeError as exc:
            assert "could not bind manager socket" in str(exc)
        else:
            raise AssertionError("bind failure should not be treated as a live manager")
        assert not ready_on_failed_bind.is_set(), "failed bind must not signal ready"

        print("test_manager OK")
        return 0
    finally:
        stop.set()
        t.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
