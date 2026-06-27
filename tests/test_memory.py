"""Memory-aware residency: the manager refuses to cold-load the heavy model when
the machine can't take it, evicts under critical pressure, and caps huge inputs.

Drives Manager.handle/maintain directly with an injected memstat -- no sockets,
no real model. Run: PYTHONPATH=src python3 tests/test_memory.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.runtime import sysmem  # noqa: E402
from needle.runtime.manager import Manager  # noqa: E402


class Spy:
    name = "spy"

    def __init__(self) -> None:
        self.evicted = 0

    def prune(self, *, text: str, query: str) -> str:
        return text[: len(text) // 2]

    def evict(self) -> None:
        self.evicted += 1


def _mgr(builds: list, mem, **kw):
    def factory():
        b = Spy()
        builds.append(b)
        return b

    kw.setdefault("emit", lambda *a, **k: None)  # don't write the real event log in tests
    return Manager(factory, heavy=True, memstat=lambda: mem, min_free_mb=3072, **kw)


def test_refuses_cold_load_under_critical_pressure() -> None:
    builds: list = []
    m = _mgr(builds, (sysmem.PRESSURE_CRITICAL, 99999))
    r = m.handle({"op": "prune", "text": "x" * 1000, "query": "q"})
    assert r["ok"] and r["text"] == "x" * 1000, r       # passed through unchanged
    assert builds == [], "model was loaded under critical pressure"
    assert r["backend"] == "passthrough:low-memory", r


def test_refuses_cold_load_below_free_floor() -> None:
    builds: list = []
    m = _mgr(builds, (sysmem.PRESSURE_NORMAL, 1870))     # like the real 8 GB box
    r = m.handle({"op": "prune", "text": "x" * 1000, "query": "q"})
    assert builds == [] and r["backend"] == "passthrough:low-memory", r


def test_loads_and_prunes_when_memory_is_fine() -> None:
    builds: list = []
    m = _mgr(builds, (sysmem.PRESSURE_NORMAL, 9000))
    r = m.handle({"op": "prune", "text": "abcdefghij", "query": "q"})
    assert r["ok"] and r["pruned_len"] < r["original_len"], r
    assert len(builds) == 1 and m.resident, r


def test_oversize_input_passes_through() -> None:
    builds: list = []
    m = _mgr(builds, (sysmem.PRESSURE_NORMAL, 9000), max_prune_chars=100)
    r = m.handle({"op": "prune", "text": "x" * 500, "query": "q"})
    assert builds == [] and r["backend"] == "passthrough:oversize", r


def test_evicts_under_critical_pressure_even_if_leased() -> None:
    builds: list = []
    m = _mgr(builds, (sysmem.PRESSURE_NORMAL, 9000), mem_poll=0.0)
    m.handle({"op": "lease", "session": "s1"})            # held lease
    m.handle({"op": "prune", "text": "abcdefghij", "query": "q"})
    assert m.resident and len(builds) == 1
    m.memstat = lambda: (sysmem.PRESSURE_CRITICAL, 100)   # pressure spikes
    m.maintain()  # real clock so the throttled memstat actually refreshes
    assert not m.resident and builds[0].evicted == 1, "did not evict under pressure"


if __name__ == "__main__":
    test_refuses_cold_load_under_critical_pressure()
    test_refuses_cold_load_below_free_floor()
    test_loads_and_prunes_when_memory_is_fine()
    test_oversize_input_passes_through()
    test_evicts_under_critical_pressure_even_if_leased()
    print("test_memory OK")
