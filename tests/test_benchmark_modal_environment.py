"""Benchmark Modal wrapper preserves the SWE-bench bash/testbed contract.

Run:
    PYTHONPATH=. python3 tests/test_benchmark_modal_environment.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.swebench.hay_environment import (  # noqa: E402
    BenchmarkAbortError,
    BenchmarkModalEnvironment,
    BenchmarkModalEnvironmentConfig,
    _HayObservationMixin,
)
from benchmarks.swebench import hay_environment  # noqa: E402


class _Runtime:
    def __init__(self) -> None:
        self.last_command = None

    async def execute(self, command):
        self.last_command = command
        return SimpleNamespace(stdout="ok", exit_code=0)


class _Deployment:
    def __init__(self) -> None:
        self.runtime = _Runtime()


class _PolicyEnv(_HayObservationMixin):
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            hay_enabled=True,
            hay_min_chars=5,
            hay_min_savings_ratio=0.0,
            hay_query_env="HAY_BENCH_QUERY",
            hay_telemetry_path="",
            hay_prune_timeout=None,
            hay_abort_on_low_memory=True,
        )


def test_modal_execute_uses_configured_interpreter() -> None:
    env = BenchmarkModalEnvironment.__new__(BenchmarkModalEnvironment)
    env.config = BenchmarkModalEnvironmentConfig(
        image="example:latest",
        cwd="/testbed",
        timeout=17,
        interpreter=["bash", "-c"],
        env={"BASH_ENV": "/root/.bashrc"},
    )
    env.deployment = _Deployment()

    out = env.execute({"command": "which python"})

    assert out == {"output": "ok", "returncode": 0, "exception_info": ""}
    sent = env.deployment.runtime.last_command
    assert sent.command == ["bash", "-c", "which python"]
    assert sent.shell is False
    assert sent.cwd == "/testbed"
    assert sent.timeout == 17
    assert sent.env == {"BASH_ENV": "/root/.bashrc"}


def test_hay_policy_prunes_read_commands_with_goal_hint() -> None:
    env = _PolicyEnv()
    calls = []
    old = hay_environment.client.prune
    hay_environment.client.prune = lambda **kw: calls.append(kw) or {
        "ok": True,
        "text": "kept",
        "backend": "fake",
    }
    try:
        out = env._maybe_prune_output(
            {"output": "0123456789", "returncode": 0},
            {
                "command": "cd /testbed && nl -ba django/forms/widgets.py",
                "context_focus_question": "Find media merging behavior.",
            },
        )
    finally:
        hay_environment.client.prune = old

    assert out["output"] == "kept"
    assert len(calls) == 1
    assert calls[0]["query"] == "Find media merging behavior."


def test_hay_policy_requires_goal_hint() -> None:
    env = _PolicyEnv()
    calls = []
    old = hay_environment.client.prune
    hay_environment.client.prune = lambda **kw: calls.append(kw) or {
        "ok": True,
        "text": "kept",
        "backend": "fake",
    }
    try:
        out = env._maybe_prune_output(
            {"output": "0123456789", "returncode": 0},
            {"command": "cd /testbed && nl -ba django/forms/widgets.py"},
        )
    finally:
        hay_environment.client.prune = old

    assert out["output"] == "0123456789"
    assert calls == []


def test_hay_policy_does_not_prune_execution_or_diff_outputs() -> None:
    env = _PolicyEnv()
    calls = []
    old = hay_environment.client.prune
    hay_environment.client.prune = lambda **kw: calls.append(kw) or {
        "ok": True,
        "text": "kept",
        "backend": "fake",
    }
    try:
        for command in (
            "cd /testbed && python tests/runtests.py forms_tests",
            "cd /testbed && git diff -- django/forms/widgets.py",
            "cd /testbed && cat patch.txt",
        ):
            out = env._maybe_prune_output(
                {"output": "0123456789", "returncode": 0},
                {
                    "command": command,
                    "context_focus_question": "Find media merging behavior.",
                },
            )
            assert out["output"] == "0123456789"
    finally:
        hay_environment.client.prune = old

    assert calls == []


def test_hay_telemetry_records_pruner_token_stats() -> None:
    env = _PolicyEnv()
    calls = []
    old = hay_environment.client.prune
    hay_environment.client.prune = lambda **kw: calls.append(kw) or {
        "ok": True,
        "text": "kept",
        "backend": "fake",
        "stats": {
            "original_tokens": 10,
            "pruned_tokens": 4,
            "saved_tokens": 6,
            "model_input_tokens": 25,
            "chunks": 3,
            "chunked": True,
        },
    }
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "telemetry.jsonl"
            env.config.hay_telemetry_path = str(path)
            env._maybe_prune_output(
                {"output": "0123456789", "returncode": 0},
                {
                    "command": "cd /testbed && nl -ba django/forms/widgets.py",
                    "context_focus_question": "Find media merging behavior.",
                },
            )
            rec = json.loads(path.read_text().splitlines()[0])
    finally:
        hay_environment.client.prune = old

    assert rec["accepted"] is True
    assert rec["saved_tokens"] == 6
    assert rec["model_input_tokens"] == 25
    assert rec["chunks"] == 3
    assert rec["chunked"] is True
    assert rec["pruner"]["pruned_tokens"] == 4


def test_hay_policy_requires_positive_token_savings_when_available() -> None:
    env = _PolicyEnv()
    old = hay_environment.client.prune
    hay_environment.client.prune = lambda **kw: {
        "ok": True,
        "text": "012345678",
        "backend": "fake",
        "stats": {
            "original_tokens": 3,
            "pruned_tokens": 3,
            "saved_tokens": 0,
        },
    }
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "telemetry.jsonl"
            env.config.hay_telemetry_path = str(path)
            out = env._maybe_prune_output(
                {"output": "0123456789", "returncode": 0},
                {
                    "command": "cd /testbed && nl -ba django/forms/widgets.py",
                    "context_focus_question": "Find media merging behavior.",
                },
            )
            rec = json.loads(path.read_text().splitlines()[0])
    finally:
        hay_environment.client.prune = old

    assert out["output"] == "0123456789"
    assert rec["accepted"] is False
    assert rec["reason"] == "below-threshold"
    assert rec["candidate_saved_chars"] == 1
    assert rec["saved_tokens"] == 0


def test_hay_policy_aborts_on_low_memory_passthrough() -> None:
    env = _PolicyEnv()
    old = hay_environment.client.prune
    hay_environment.client.prune = lambda **kw: {
        "ok": True,
        "text": kw["text"],
        "backend": "passthrough:low-memory",
        "stats": {
            "passthrough_reason": "low-memory",
            "original_chars": len(kw["text"]),
            "pruned_chars": len(kw["text"]),
            "saved_chars": 0,
        },
    }
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "telemetry.jsonl"
            env.config.hay_telemetry_path = str(path)
            try:
                env._maybe_prune_output(
                    {"output": "0123456789", "returncode": 0},
                    {
                        "command": "cd /testbed && nl -ba django/forms/widgets.py",
                        "context_focus_question": "Find media merging behavior.",
                    },
                )
            except BenchmarkAbortError as exc:
                assert "low-memory passthrough" in str(exc)
            else:
                raise AssertionError("low-memory passthrough did not abort")
            rec = json.loads(path.read_text().splitlines()[0])
    finally:
        hay_environment.client.prune = old

    assert rec["accepted"] is False
    assert rec["reason"] == "low-memory"
    assert rec["backend"] == "passthrough:low-memory"
    assert rec["passthrough_reason"] == "low-memory"


if __name__ == "__main__":
    test_modal_execute_uses_configured_interpreter()
    test_hay_policy_prunes_read_commands_with_goal_hint()
    test_hay_policy_requires_goal_hint()
    test_hay_policy_does_not_prune_execution_or_diff_outputs()
    test_hay_telemetry_records_pruner_token_stats()
    test_hay_policy_requires_positive_token_savings_when_available()
    test_hay_policy_aborts_on_low_memory_passthrough()
    print("test_benchmark_modal_environment OK")
