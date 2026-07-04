"""Python model worker entrypoint for the Rust runtime.

This process owns Python/MLX model memory. Rust owns when this process is
started, stopped, and considered part of Needle's runtime state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from needle_worker.soft_lamr.decision import prune_decision_reason

JsonObject = dict[str, Any]


@dataclass
class WorkerState:
    backend: Any | None = None
    last_error: str | None = None


def _write_json(payload: JsonObject, output: TextIO = sys.stdout) -> None:
    output.write(json.dumps(payload, separators=(",", ":")) + "\n")
    output.flush()


def load_backend() -> Any:
    """Load the concrete MLX backend for the worker process.

    The Rust-owned runtime should fail visibly if the model backend cannot load,
    not degrade to a fake/pass-through backend from the older MCP path.
    """
    from needle_worker.soft_lamr.model import CodePrunerBackend

    return CodePrunerBackend()


def _backend_name(backend: Any) -> str:
    name = getattr(backend, "name", "unknown")
    return name if isinstance(name, str) else "unknown"


def _backend_stats(backend: Any) -> JsonObject:
    stats = getattr(backend, "last_stats", {})
    return dict(stats) if isinstance(stats, dict) else {}


def _backend_decision(stats: JsonObject, *, original: str, pruned: str) -> tuple[str, str]:
    decision = stats.get("decision")
    reason = stats.get("reason")
    if isinstance(decision, str) and isinstance(reason, str):
        return decision, reason
    return prune_decision_reason(
        original=original,
        pruned=pruned,
        passthrough_reason=stats.get("passthrough_reason"),
    )


def _status_response(state: WorkerState) -> JsonObject:
    if state.last_error is not None:
        return {"ok": True, "status": "failed", "error": state.last_error}
    if state.backend is None:
        return {"ok": True, "status": "cold"}
    return {
        "ok": True,
        "status": "resident",
        "backend": _backend_name(state.backend),
    }


def _with_id(request: JsonObject, response: JsonObject) -> JsonObject:
    if "id" not in request:
        return response
    return {"id": request["id"], **response}


def _failed(request: JsonObject, error: str) -> JsonObject:
    return _with_id(request, {"ok": False, "status": "failed", "error": error})


def _ensure_loaded(
    state: WorkerState,
    load: Callable[[], Any],
) -> None:
    if state.backend is None:
        state.backend = load()
    state.last_error = None


def _unload(state: WorkerState) -> None:
    backend = state.backend
    state.backend = None
    if backend is None:
        state.last_error = None
        return
    evict = getattr(backend, "evict", None)
    if callable(evict):
        evict()
    state.last_error = None


def handle_request(
    request: JsonObject,
    state: WorkerState,
    *,
    load: Callable[[], Any] = load_backend,
) -> tuple[JsonObject, bool]:
    op = request.get("op")

    if op == "status":
        return _with_id(request, _status_response(state)), False

    if op == "load":
        try:
            _ensure_loaded(state, load)
        except Exception as exc:  # noqa: BLE001 - startup errors cross the process boundary.
            state.backend = None
            state.last_error = str(exc)
            return _failed(request, str(exc)), False
        return _with_id(request, _status_response(state)), False

    if op == "prune":
        text = request.get("text")
        query = request.get("query")
        if not isinstance(text, str):
            return _failed(request, "prune requires string field: text"), False
        if not isinstance(query, str) or not query.strip():
            return _failed(request, "prune requires non-empty string field: query"), False
        try:
            _ensure_loaded(state, load)
            pruned = state.backend.prune(text=text, query=query)
            state.last_error = None
        except Exception as exc:  # noqa: BLE001 - model errors must cross the process boundary.
            state.last_error = str(exc)
            return _failed(request, str(exc)), False
        stats = _backend_stats(state.backend)
        decision, reason = _backend_decision(stats, original=text, pruned=pruned)
        return (
            _with_id(
                request,
                {
                    "ok": True,
                    "status": "resident",
                    "backend": _backend_name(state.backend),
                    "decision": decision,
                    "reason": reason,
                    "text": pruned,
                    "stats": stats,
                },
            ),
            False,
        )

    if op == "unload":
        try:
            _unload(state)
        except Exception as exc:  # noqa: BLE001 - unload errors must cross the process boundary.
            state.last_error = str(exc)
            return _failed(request, str(exc)), False
        return _with_id(request, _status_response(state)), False

    if op == "exit":
        try:
            _unload(state)
            response = _with_id(request, _status_response(state))
        except Exception as exc:  # noqa: BLE001 - still exit after reporting unload failure.
            state.last_error = str(exc)
            response = _failed(request, str(exc))
        return response, True

    return _failed(request, f"unknown op: {op}"), False


def run_loop(
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    *,
    load: Callable[[], Any] = load_backend,
) -> int:
    state = WorkerState()
    for line in input_stream:
        if not line.strip():
            continue
        try:
            raw_request = json.loads(line)
        except json.JSONDecodeError as exc:
            _write_json({"ok": False, "status": "failed", "error": str(exc)}, output_stream)
            continue
        if not isinstance(raw_request, dict):
            _write_json(
                {"ok": False, "status": "failed", "error": "request must be a JSON object"},
                output_stream,
            )
            continue
        response, should_exit = handle_request(raw_request, state, load=load)
        _write_json(response, output_stream)
        if should_exit:
            return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Needle's Python model worker.")
    parser.parse_args(argv)
    return run_loop()


if __name__ == "__main__":
    raise SystemExit(main())
