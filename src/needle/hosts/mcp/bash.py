"""Bash-minimal MCP observation tool.

The portable MCP package owns observation, not mutation. It executes one bash
command, renders a stable text observation, and optionally asks the resident
Needle runtime to prune that observation toward an explicit focus question.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import socket
import subprocess
from typing import Any, Callable

from needle.runtime import client, events


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
    emit_fn: Callable[..., None] | None = events.emit,
) -> str:
    """Execute bash, then prune only when an explicit focus is supplied."""
    observation = run_bash_command(command, timeout_secs=timeout_secs)
    original = observation.text
    context = _observation_context(observation, original)
    focus = (context_focus_question or "").strip()
    if not focus:
        _emit_mcp_diagnostic(emit_fn, "mcp_bash_passthrough", reason="missing_focus", **context)
        return original
    if len(original) < min_chars:
        _emit_mcp_diagnostic(
            emit_fn,
            "mcp_bash_passthrough",
            reason="below_min_chars",
            min_chars=min_chars,
            **context,
        )
        return original

    try:
        resp = (prune_fn or _manager_prune)(original, focus)
    except socket.timeout:
        _emit_mcp_diagnostic(emit_fn, "mcp_bash_passthrough", reason="manager_timeout", **context)
        return original
    except OSError:
        _emit_mcp_diagnostic(
            emit_fn,
            "mcp_bash_passthrough",
            reason="manager_unavailable",
            **context,
        )
        return original
    except Exception:
        _emit_mcp_diagnostic(emit_fn, "mcp_bash_passthrough", reason="pruner_error", **context)
        return original
    if not resp.get("ok"):
        _emit_mcp_diagnostic(
            emit_fn,
            "mcp_bash_passthrough",
            reason="manager_error",
            error=str(resp.get("error", ""))[:200],
            **context,
        )
        return original

    pruned = str(resp.get("text", ""))
    if not pruned:
        _emit_mcp_diagnostic(emit_fn, "mcp_bash_passthrough", reason="empty_prune", **context)
        return original
    if len(pruned) >= len(original):
        _emit_mcp_diagnostic(
            emit_fn,
            "mcp_bash_passthrough",
            reason="no_savings",
            pruned_chars=len(pruned),
            **context,
        )
        return original
    _emit_mcp_diagnostic(
        emit_fn,
        "mcp_bash_prune",
        pruned_chars=len(pruned),
        saved_chars=len(original) - len(pruned),
        **_response_stats(resp),
        **context,
    )
    return pruned


def _manager_prune(text: str, query: str) -> dict[str, Any]:
    return client.prune(text=text, query=query)


def _observation_context(observation: BashObservation, rendered: str) -> dict[str, object]:
    return {
        "chars": len(rendered),
        "stdout_chars": len(observation.stdout),
        "stderr_chars": len(observation.stderr),
        "exit_code": observation.exit_code if observation.exit_code is not None else "timeout",
        "command_timeout": observation.timed_out,
    }


def _response_stats(resp: dict[str, Any]) -> dict[str, object]:
    raw = resp.get("stats")
    if not isinstance(raw, dict):
        return {}
    fields: dict[str, object] = {}
    for key in (
        "chunks",
        "batches",
        "batch_sizes",
        "max_length",
        "padding_waste_ratio",
        "truncated_code_tokens",
        "total_ms",
    ):
        value = raw.get(key)
        if value is None or isinstance(value, (bool, int, float, str, list)):
            fields[key] = value
    return fields


def _emit_mcp_diagnostic(
    emit_fn: Callable[..., None] | None,
    event: str,
    **fields: object,
) -> None:
    if emit_fn is None:
        return
    try:
        emit_fn(event, **fields)
    except Exception:  # noqa: BLE001 - diagnostics must not alter tool output.
        pass


def _decode_timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
