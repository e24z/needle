"""Bash-minimal MCP observation tool.

The portable MCP package owns observation, not mutation. It executes one bash
command, renders a stable text observation, and optionally asks the resident
Needle runtime to prune that observation toward an explicit focus question.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
from typing import Any, Callable

from needle.runtime import client


def _float_env(primary: str, fallback: str, default: str) -> float:
    return float(os.environ.get(primary) or os.environ.get(fallback, default))


def _int_env(primary: str, fallback: str, default: str) -> int:
    return int(os.environ.get(primary) or os.environ.get(fallback, default))


DEFAULT_TIMEOUT_SECS = _float_env("NEEDLE_MCP_BASH_TIMEOUT_SECS", "HAY_MCP_BASH_TIMEOUT_SECS", "30")
DEFAULT_MIN_CHARS = _int_env("NEEDLE_MCP_MIN_CHARS", "HAY_MCP_MIN_CHARS", "500")


@dataclass(frozen=True)
class BashObservation:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    timeout_secs: float | None = None

    @property
    def text(self) -> str:
        return render_observation(self)


def run_bash_command(command: str, timeout_secs: float = DEFAULT_TIMEOUT_SECS) -> BashObservation:
    """Run one command in a fresh non-login bash process."""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")

    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )
    except subprocess.TimeoutExpired as exc:
        return BashObservation(
            command=command,
            exit_code=None,
            stdout=_decode_timeout_output(exc.stdout),
            stderr=_decode_timeout_output(exc.stderr),
            timed_out=True,
            timeout_secs=timeout_secs,
        )

    return BashObservation(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def render_observation(observation: BashObservation) -> str:
    """Render command output in a stable, prunable text shape."""
    parts = [f"exit_code: {observation.exit_code if observation.exit_code is not None else 'timeout'}"]
    if observation.timed_out:
        parts.append(f"timed_out_after_secs: {observation.timeout_secs:g}")
    if observation.stdout:
        parts.append("stdout:")
        parts.append(observation.stdout.rstrip("\n"))
    if observation.stderr:
        parts.append("stderr:")
        parts.append(observation.stderr.rstrip("\n"))
    if len(parts) == 1:
        parts.append("stdout:")
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def needle_bash_observation(
    command: str,
    context_focus_question: str | None = None,
    *,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    min_chars: int = DEFAULT_MIN_CHARS,
    prune_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> str:
    """Execute bash, then prune only when an explicit focus is supplied."""
    observation = run_bash_command(command, timeout_secs=timeout_secs)
    original = observation.text
    focus = (context_focus_question or "").strip()
    if not focus or len(original) < min_chars:
        return original

    try:
        resp = (prune_fn or _manager_prune)(original, focus)
    except Exception:
        return original
    if not resp.get("ok"):
        return original

    pruned = str(resp.get("text", ""))
    if not pruned:
        return original
    if len(pruned) >= len(original):
        return original
    return pruned


def _manager_prune(text: str, query: str) -> dict[str, Any]:
    return client.prune(text=text, query=query)


def _decode_timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

