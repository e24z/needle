"""NDJSON worker protocol tests without loading MLX.

Run: PYTHONPATH=python python3 tests/test_worker.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from needle_worker.worker import run_loop  # noqa: E402


class FakeBackend:
    name = "fake-soft-lamr"

    def __init__(self) -> None:
        self.evicted = False
        self.last_stats: dict[str, object] = {}

    def prune(self, *, text: str, query: str) -> str:
        del query
        pruned = text.replace(" drop", "")
        self.last_stats = {
            "input_chars": len(text),
            "output_chars": len(pruned),
            "saved_chars": max(0, len(text) - len(pruned)),
        }
        return pruned

    def evict(self) -> None:
        self.evicted = True


def _responses(output: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_worker_prune_loads_backend_and_returns_result() -> None:
    loaded: list[FakeBackend] = []

    def load() -> FakeBackend:
        backend = FakeBackend()
        loaded.append(backend)
        return backend

    input_stream = io.StringIO(
        "\n".join(
            [
                '{"id":1,"op":"status"}',
                '{"id":2,"op":"prune","text":"keep drop","query":"keep relevant code"}',
                '{"id":3,"op":"status"}',
                '{"id":4,"op":"unload"}',
                '{"id":5,"op":"exit"}',
            ]
        )
        + "\n"
    )
    output_stream = io.StringIO()

    assert run_loop(input_stream, output_stream, load=load) == 0

    responses = _responses(output_stream)
    assert responses[0] == {"id": 1, "ok": True, "status": "cold"}
    assert responses[1]["id"] == 2
    assert responses[1]["ok"] is True
    assert responses[1]["status"] == "resident"
    assert responses[1]["backend"] == "fake-soft-lamr"
    assert responses[1]["decision"] == "pruned"
    assert responses[1]["reason"] == "model"
    assert responses[1]["text"] == "keep"
    assert responses[1]["stats"] == {
        "input_chars": 9,
        "output_chars": 4,
        "saved_chars": 5,
    }
    assert responses[2] == {
        "id": 3,
        "ok": True,
        "status": "resident",
        "backend": "fake-soft-lamr",
    }
    assert responses[3] == {"id": 4, "ok": True, "status": "cold"}
    assert responses[4] == {"id": 5, "ok": True, "status": "cold"}
    assert len(loaded) == 1
    assert loaded[0].evicted


def test_worker_reports_explicit_unchanged_reason() -> None:
    class PassthroughBackend(FakeBackend):
        def prune(self, *, text: str, query: str) -> str:
            del query
            self.last_stats = {"passthrough_reason": "query-too-long"}
            return text

    input_stream = io.StringIO(
        '{"id":1,"op":"prune","text":"same","query":"very long question"}\n'
        '{"id":2,"op":"exit"}\n'
    )
    output_stream = io.StringIO()

    assert run_loop(input_stream, output_stream, load=PassthroughBackend) == 0

    responses = _responses(output_stream)
    assert responses[0]["decision"] == "unchanged"
    assert responses[0]["reason"] == "query-too-long"
    assert responses[0]["text"] == "same"


def test_worker_rejects_prune_without_query_before_loading() -> None:
    loads = 0

    def load() -> FakeBackend:
        nonlocal loads
        loads += 1
        return FakeBackend()

    input_stream = io.StringIO(
        '{"id":"missing-query","op":"prune","text":"keep"}\n'
        '{"id":"exit","op":"exit"}\n'
    )
    output_stream = io.StringIO()

    assert run_loop(input_stream, output_stream, load=load) == 0

    responses = _responses(output_stream)
    assert responses[0] == {
        "id": "missing-query",
        "ok": False,
        "status": "failed",
        "error": "prune requires non-empty string field: query",
    }
    assert responses[1] == {"id": "exit", "ok": True, "status": "cold"}
    assert loads == 0


def test_worker_reports_load_failure() -> None:
    def load() -> FakeBackend:
        raise RuntimeError("model unavailable")

    input_stream = io.StringIO(
        '{"id":1,"op":"load"}\n'
        '{"id":2,"op":"status"}\n'
        '{"id":3,"op":"exit"}\n'
    )
    output_stream = io.StringIO()

    assert run_loop(input_stream, output_stream, load=load) == 0

    responses = _responses(output_stream)
    assert responses[0] == {
        "id": 1,
        "ok": False,
        "status": "failed",
        "error": "model unavailable",
    }
    assert responses[1] == {
        "id": 2,
        "ok": True,
        "status": "failed",
        "error": "model unavailable",
    }
    assert responses[2] == {"id": 3, "ok": True, "status": "cold"}


def main() -> int:
    test_worker_prune_loads_backend_and_returns_result()
    test_worker_reports_explicit_unchanged_reason()
    test_worker_rejects_prune_without_query_before_loading()
    test_worker_reports_load_failure()
    print("test_worker OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
