"""Bash-minimal MCP tool behavior without importing the MCP SDK.

Run: PYTHONPATH=src python3 tests/test_mcp_bash.py
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

os.environ["NEEDLE_NO_EVENTS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from needle.hosts.mcp.bash import needle_bash_observation, run_bash_command  # noqa: E402


def test_needle_bash_returns_raw_output_without_focus() -> None:
    out = needle_bash_observation("printf 'alpha\\n'", timeout_secs=5)
    assert "exit_code: 0" in out
    assert "stdout:\nalpha" in out


def test_needle_bash_uses_non_login_bash() -> None:
    out = needle_bash_observation(
        "if shopt -q login_shell; then printf login; else printf non-login; fi",
        timeout_secs=5,
    )

    assert out == "exit_code: 0\nstdout:\nnon-login\n"


def test_needle_bash_focus_is_optional_and_blank_passes_through() -> None:
    calls: list[tuple[str, str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def prune_fn(text: str, query: str) -> dict:
        calls.append((text, query))
        return {"ok": True, "text": "PRUNED"}

    out = needle_bash_observation(
        "printf 'alpha\\n'",
        "",
        timeout_secs=5,
        min_chars=1,
        prune_fn=prune_fn,
        emit_fn=lambda event, **fields: events.append((event, fields)),
    )
    assert "stdout:\nalpha" in out
    assert calls == []
    assert events and events[0][0] == "mcp_bash_passthrough"
    assert events[0][1]["reason"] == "missing_focus"


def test_needle_bash_prunes_only_with_explicit_focus() -> None:
    calls: list[tuple[str, str]] = []

    def prune_fn(text: str, query: str) -> dict:
        calls.append((text, query))
        return {"ok": True, "text": "exit_code: 0\nstdout:\nkept\n"}

    out = needle_bash_observation(
        "printf 'alpha\\nbeta\\n'",
        "Which line mentions beta?",
        timeout_secs=5,
        min_chars=1,
        prune_fn=prune_fn,
    )
    assert out == "exit_code: 0\nstdout:\nkept\n"
    assert calls
    assert calls[0][1] == "Which line mentions beta?"


def test_needle_bash_fails_open_when_pruner_errors() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def prune_fn(_text: str, _query: str) -> dict:
        raise OSError("manager unavailable")

    out = needle_bash_observation(
        "printf 'alpha\\n'",
        "What matters?",
        timeout_secs=5,
        min_chars=1,
        prune_fn=prune_fn,
        emit_fn=lambda event, **fields: events.append((event, fields)),
    )
    assert "stdout:\nalpha" in out
    assert events and events[0][1]["reason"] == "manager_unavailable"


def test_needle_bash_reports_manager_timeout_without_changing_output() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def prune_fn(_text: str, _query: str) -> dict:
        raise socket.timeout("slow manager")

    out = needle_bash_observation(
        "printf 'alpha\\n'",
        "What matters?",
        timeout_secs=5,
        min_chars=1,
        prune_fn=prune_fn,
        emit_fn=lambda event, **fields: events.append((event, fields)),
    )
    assert "stdout:\nalpha" in out
    assert events and events[0][1]["reason"] == "manager_timeout"


def test_needle_bash_reports_no_savings_without_changing_output() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def prune_fn(text: str, query: str) -> dict:
        return {"ok": True, "text": text}

    out = needle_bash_observation(
        "printf 'alpha\\n'",
        "What matters?",
        timeout_secs=5,
        min_chars=1,
        prune_fn=prune_fn,
        emit_fn=lambda event, **fields: events.append((event, fields)),
    )
    assert "stdout:\nalpha" in out
    assert events and events[0][1]["reason"] == "no_savings"


def test_needle_bash_reports_success_with_response_stats() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def prune_fn(text: str, query: str) -> dict:
        return {
            "ok": True,
            "text": "exit_code: 0\nstdout:\nkept\n",
            "stats": {
                "chunks": 2,
                "batches": 1,
                "batch_sizes": [2],
                "max_length": 1024,
                "padding_waste_ratio": 0.25,
            },
        }

    out = needle_bash_observation(
        "printf 'alpha\\nbeta\\n'",
        "Which line mentions beta?",
        timeout_secs=5,
        min_chars=1,
        prune_fn=prune_fn,
        emit_fn=lambda event, **fields: events.append((event, fields)),
    )
    assert out == "exit_code: 0\nstdout:\nkept\n"
    assert events and events[0][0] == "mcp_bash_prune"
    assert events[0][1]["chunks"] == 2
    assert events[0][1]["batch_sizes"] == [2]


def test_needle_bash_uses_configurable_prune_timeout() -> None:
    seen: dict[str, object] = {}

    def fake_prune(**kwargs) -> dict:
        seen.update(kwargs)
        return {"ok": True, "text": "exit_code: 0\nstdout:\nkept\n"}

    from needle.hosts.mcp import bash as bash_mod

    old_prune = bash_mod.client.prune
    try:
        bash_mod.client.prune = fake_prune
        out = needle_bash_observation(
            "printf 'alpha\\nbeta\\n'",
            "Which line mentions beta?",
            timeout_secs=5,
            prune_timeout_secs=3.5,
            min_chars=1,
        )
    finally:
        bash_mod.client.prune = old_prune

    assert out == "exit_code: 0\nstdout:\nkept\n"
    assert seen["timeout"] == 3.5


def test_needle_bash_reports_nonzero_exit_without_throwing() -> None:
    out = needle_bash_observation("printf 'bad\\n' >&2; exit 7", timeout_secs=5)
    assert "exit_code: 7" in out
    assert "stderr:\nbad" in out


def test_needle_bash_caps_large_stdout_before_rendering() -> None:
    obs = run_bash_command(
        f"{sys.executable} -c \"import sys; sys.stdout.write('a' * 64)\"",
        timeout_secs=5,
        stdout_limit_bytes=10,
    )
    assert obs.stdout == "a" * 10
    assert obs.stdout_truncated is True
    assert "[needle: stdout truncated at 10 bytes]" in obs.text


def test_needle_bash_caps_large_stderr_before_rendering() -> None:
    obs = run_bash_command(
        f"{sys.executable} -c \"import sys; sys.stderr.write('e' * 64)\"",
        timeout_secs=5,
        stderr_limit_bytes=7,
    )
    assert obs.stderr == "e" * 7
    assert obs.stderr_truncated is True
    assert "[needle: stderr truncated at 7 bytes]" in obs.text


def test_needle_bash_timeout_preserves_bounded_partial_output() -> None:
    obs = run_bash_command(
        f"{sys.executable} -c \"import sys, time; sys.stdout.write('x' * 64); "
        "sys.stdout.flush(); time.sleep(2)\"",
        timeout_secs=0.2,
        stdout_limit_bytes=8,
    )
    assert obs.timed_out is True
    assert obs.exit_code is None
    assert obs.stdout == "x" * 8
    assert obs.stdout_truncated is True
    assert "exit_code: timeout" in obs.text


def test_needle_bash_commands_read_from_devnull() -> None:
    from needle.hosts.mcp import bash as bash_mod

    seen: dict[str, object] = {}

    class FakeProc:
        pass

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProc()

    def fake_communicate(_proc, **_kwargs):
        return {
            "exit_code": 0,
            "stdout": b"",
            "stderr": b"",
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    old_popen = bash_mod.subprocess.Popen
    old_communicate = bash_mod._communicate_bounded
    try:
        bash_mod.subprocess.Popen = fake_popen
        bash_mod._communicate_bounded = fake_communicate
        run_bash_command("cat", timeout_secs=1)
    finally:
        bash_mod.subprocess.Popen = old_popen
        bash_mod._communicate_bounded = old_communicate

    assert seen["argv"] == ["bash", "-c", "cat"]
    assert seen["kwargs"]["stdin"] == bash_mod.subprocess.DEVNULL


def test_needle_bash_stdin_reader_exits_on_eof() -> None:
    obs = run_bash_command("cat", timeout_secs=1)
    assert obs.timed_out is False
    assert obs.exit_code == 0


def main() -> int:
    test_needle_bash_returns_raw_output_without_focus()
    test_needle_bash_uses_non_login_bash()
    test_needle_bash_focus_is_optional_and_blank_passes_through()
    test_needle_bash_prunes_only_with_explicit_focus()
    test_needle_bash_fails_open_when_pruner_errors()
    test_needle_bash_reports_manager_timeout_without_changing_output()
    test_needle_bash_reports_no_savings_without_changing_output()
    test_needle_bash_reports_success_with_response_stats()
    test_needle_bash_uses_configurable_prune_timeout()
    test_needle_bash_reports_nonzero_exit_without_throwing()
    test_needle_bash_caps_large_stdout_before_rendering()
    test_needle_bash_caps_large_stderr_before_rendering()
    test_needle_bash_timeout_preserves_bounded_partial_output()
    test_needle_bash_commands_read_from_devnull()
    test_needle_bash_stdin_reader_exits_on_eof()
    print("test_mcp_bash OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
