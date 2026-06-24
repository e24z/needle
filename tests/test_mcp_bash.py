"""Bash-minimal MCP tool behavior without importing the MCP SDK.

Run: PYTHONPATH=. python3 tests/test_mcp_bash.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from needle.hosts.mcp.bash import needle_bash_observation  # noqa: E402


def test_needle_bash_returns_raw_output_without_focus() -> None:
    out = needle_bash_observation("printf 'alpha\\n'", timeout_secs=5)
    assert "exit_code: 0" in out
    assert "stdout:\nalpha" in out


def test_needle_bash_uses_non_login_bash() -> None:
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs

        class Completed:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Completed()

    with patch("subprocess.run", fake_run):
        out = needle_bash_observation("printf ok", timeout_secs=5)

    assert out == "exit_code: 0\nstdout:\nok\n"
    assert seen["argv"] == ["bash", "-c", "printf ok"]


def test_needle_bash_focus_is_optional_and_blank_passes_through() -> None:
    calls: list[tuple[str, str]] = []

    def prune_fn(text: str, query: str) -> dict:
        calls.append((text, query))
        return {"ok": True, "text": "PRUNED"}

    out = needle_bash_observation(
        "printf 'alpha\\n'",
        "",
        timeout_secs=5,
        min_chars=1,
        prune_fn=prune_fn,
    )
    assert "stdout:\nalpha" in out
    assert calls == []


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
    def prune_fn(_text: str, _query: str) -> dict:
        raise OSError("manager unavailable")

    out = needle_bash_observation(
        "printf 'alpha\\n'",
        "What matters?",
        timeout_secs=5,
        min_chars=1,
        prune_fn=prune_fn,
    )
    assert "stdout:\nalpha" in out


def test_needle_bash_reports_nonzero_exit_without_throwing() -> None:
    out = needle_bash_observation("printf 'bad\\n' >&2; exit 7", timeout_secs=5)
    assert "exit_code: 7" in out
    assert "stderr:\nbad" in out


def main() -> int:
    test_needle_bash_returns_raw_output_without_focus()
    test_needle_bash_uses_non_login_bash()
    test_needle_bash_focus_is_optional_and_blank_passes_through()
    test_needle_bash_prunes_only_with_explicit_focus()
    test_needle_bash_fails_open_when_pruner_errors()
    test_needle_bash_reports_nonzero_exit_without_throwing()
    print("test_mcp_bash OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

