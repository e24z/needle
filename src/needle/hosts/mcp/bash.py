"""Bash-minimal MCP observation tool.

The portable MCP package owns observation, not mutation. It executes one bash
command, renders a stable text observation, and optionally asks the resident
Needle runtime to prune that observation toward an explicit focus question.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import selectors
import signal
import socket
import subprocess
import time
from typing import Any, Callable

from needle.runtime import client, events


def _float_env(primary: str, fallback: str, default: str) -> float:
    return float(os.environ.get(primary) or os.environ.get(fallback, default))


def _int_env(primary: str, fallback: str, default: str) -> int:
    return int(os.environ.get(primary) or os.environ.get(fallback, default))


DEFAULT_TIMEOUT_SECS = _float_env("NEEDLE_MCP_BASH_TIMEOUT_SECS", "HAY_MCP_BASH_TIMEOUT_SECS", "30")
DEFAULT_PRUNE_TIMEOUT_SECS = _float_env(
    "NEEDLE_MCP_PRUNE_TIMEOUT_SECS",
    "HAY_MCP_PRUNE_TIMEOUT_SECS",
    "120",
)
DEFAULT_MIN_CHARS = _int_env("NEEDLE_MCP_MIN_CHARS", "HAY_MCP_MIN_CHARS", "500")
DEFAULT_STDOUT_LIMIT_BYTES = _int_env(
    "NEEDLE_MCP_STDOUT_LIMIT_BYTES",
    "HAY_MCP_STDOUT_LIMIT_BYTES",
    "200000",
)
DEFAULT_STDERR_LIMIT_BYTES = _int_env(
    "NEEDLE_MCP_STDERR_LIMIT_BYTES",
    "HAY_MCP_STDERR_LIMIT_BYTES",
    "100000",
)
_READ_CHUNK_BYTES = 8192


@dataclass(frozen=True)
class BashObservation:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    timeout_secs: float | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_limit_bytes: int | None = None
    stderr_limit_bytes: int | None = None
    stdout_omitted_bytes: int = 0
    stderr_omitted_bytes: int = 0

    @property
    def text(self) -> str:
        return render_observation(self)


def run_bash_command(
    command: str,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    *,
    stdout_limit_bytes: int = DEFAULT_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_STDERR_LIMIT_BYTES,
) -> BashObservation:
    """Run one command in a fresh non-login bash process."""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    _validate_limit("stdout_limit_bytes", stdout_limit_bytes)
    _validate_limit("stderr_limit_bytes", stderr_limit_bytes)
    if timeout_secs < 0:
        raise ValueError("timeout_secs must be non-negative")

    proc = subprocess.Popen(
        ["bash", "-c", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    result = _communicate_bounded(
        proc,
        timeout_secs=timeout_secs,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
    )

    return BashObservation(
        command=command,
        exit_code=result["exit_code"],
        stdout=_decode_captured_output(
            result["stdout"],
            stream="stdout",
            head_bytes=int(result["stdout_head_bytes"]),
            omitted_bytes=int(result["stdout_omitted_bytes"]),
        ),
        stderr=_decode_captured_output(
            result["stderr"],
            stream="stderr",
            head_bytes=int(result["stderr_head_bytes"]),
            omitted_bytes=int(result["stderr_omitted_bytes"]),
        ),
        timed_out=bool(result["timed_out"]),
        timeout_secs=timeout_secs if result["timed_out"] else None,
        stdout_truncated=bool(result["stdout_truncated"]),
        stderr_truncated=bool(result["stderr_truncated"]),
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        stdout_omitted_bytes=int(result["stdout_omitted_bytes"]),
        stderr_omitted_bytes=int(result["stderr_omitted_bytes"]),
    )


def render_observation(observation: BashObservation) -> str:
    """Render command output in a stable, prunable text shape."""
    parts = [f"exit_code: {observation.exit_code if observation.exit_code is not None else 'timeout'}"]
    if observation.timed_out:
        parts.append(f"timed_out_after_secs: {observation.timeout_secs:g}")
    if observation.stdout:
        parts.append("stdout:")
        parts.append(observation.stdout.rstrip("\n"))
    if observation.stdout_truncated:
        parts.append(
            f"[needle: stdout truncated to head+tail within {observation.stdout_limit_bytes} bytes; "
            f"omitted {observation.stdout_omitted_bytes} bytes]"
        )
    if observation.stderr:
        parts.append("stderr:")
        parts.append(observation.stderr.rstrip("\n"))
    if observation.stderr_truncated:
        parts.append(
            f"[needle: stderr truncated to head+tail within {observation.stderr_limit_bytes} bytes; "
            f"omitted {observation.stderr_omitted_bytes} bytes]"
        )
    if len(parts) == 1:
        parts.append("stdout:")
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def needle_bash_observation(
    command: str,
    context_focus_question: str | None = None,
    *,
    timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    prune_timeout_secs: float = DEFAULT_PRUNE_TIMEOUT_SECS,
    min_chars: int = DEFAULT_MIN_CHARS,
    stdout_limit_bytes: int = DEFAULT_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_STDERR_LIMIT_BYTES,
    prune_fn: Callable[[str, str], dict[str, Any]] | None = None,
    emit_fn: Callable[..., None] | None = events.emit,
) -> str:
    """Execute bash, then prune only when an explicit focus is supplied."""
    observation = run_bash_command(
        command,
        timeout_secs=timeout_secs,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
    )
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
        resp = (
            prune_fn(original, focus)
            if prune_fn is not None
            else _manager_prune(original, focus, timeout_secs=prune_timeout_secs)
        )
    except socket.timeout:
        _emit_mcp_diagnostic(
            emit_fn,
            "mcp_bash_passthrough",
            reason="manager_timeout",
            prune_timeout_secs=prune_timeout_secs,
            **context,
        )
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


def _manager_prune(
    text: str,
    query: str,
    *,
    timeout_secs: float = DEFAULT_PRUNE_TIMEOUT_SECS,
) -> dict[str, Any]:
    return client.prune(text=text, query=query, timeout=timeout_secs)


def _observation_context(observation: BashObservation, rendered: str) -> dict[str, object]:
    return {
        "chars": len(rendered),
        "stdout_chars": len(observation.stdout),
        "stderr_chars": len(observation.stderr),
        "stdout_truncated": observation.stdout_truncated,
        "stderr_truncated": observation.stderr_truncated,
        "stdout_limit_bytes": observation.stdout_limit_bytes,
        "stderr_limit_bytes": observation.stderr_limit_bytes,
        "stdout_omitted_bytes": observation.stdout_omitted_bytes,
        "stderr_omitted_bytes": observation.stderr_omitted_bytes,
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


def _validate_limit(name: str, value: int) -> None:
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _communicate_bounded(
    proc: subprocess.Popen[bytes],
    *,
    timeout_secs: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> dict[str, object]:
    selector = selectors.DefaultSelector()
    captures = {
        "stdout": _BoundedCapture(stdout_limit_bytes),
        "stderr": _BoundedCapture(stderr_limit_bytes),
    }

    if proc.stdout is not None:
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    if proc.stderr is not None:
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")

    timed_out = False
    deadline = time.monotonic() + timeout_secs
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        _drain_ready(selector, captures, timeout=min(0.1, remaining))

    if timed_out:
        _kill_process_group(proc)
        drain_deadline = time.monotonic() + 1.0
        while selector.get_map() and time.monotonic() < drain_deadline:
            _drain_ready(selector, captures, timeout=0.05)

    selector.close()
    try:
        proc_exit_code = proc.wait(timeout=1.0)
        exit_code = None if timed_out else proc_exit_code
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        exit_code = None

    return {
        "exit_code": exit_code,
        "stdout": captures["stdout"].value,
        "stderr": captures["stderr"].value,
        "timed_out": timed_out,
        "stdout_truncated": captures["stdout"].truncated,
        "stderr_truncated": captures["stderr"].truncated,
        "stdout_head_bytes": captures["stdout"].head_len,
        "stderr_head_bytes": captures["stderr"].head_len,
        "stdout_omitted_bytes": captures["stdout"].omitted_bytes,
        "stderr_omitted_bytes": captures["stderr"].omitted_bytes,
    }


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._head_limit = limit // 3
        self._tail_limit = limit - self._head_limit
        self._full = bytearray()
        self._head = bytearray()
        self._tail = bytearray()
        self._seen = 0
        self.truncated = False

    def append(self, chunk: bytes) -> bool:
        if not chunk:
            return self.truncated
        if not self.truncated:
            if len(self._full) + len(chunk) <= self.limit:
                self._full.extend(chunk)
                self._seen += len(chunk)
                return False
            combined = bytes(self._full) + chunk
            self._seen += len(chunk)
            self.truncated = True
            self._full.clear()
            if self._head_limit:
                self._head.extend(combined[: self._head_limit])
            if self._tail_limit:
                self._tail.extend(combined[-self._tail_limit :])
            return True

        self._seen += len(chunk)
        if self._tail_limit:
            self._tail.extend(chunk)
            if len(self._tail) > self._tail_limit:
                del self._tail[: len(self._tail) - self._tail_limit]
        return True

    @property
    def value(self) -> bytes:
        if not self.truncated:
            return bytes(self._full)
        return bytes(self._head + self._tail)

    @property
    def head_len(self) -> int:
        if not self.truncated:
            return len(self._full)
        return len(self._head)

    @property
    def omitted_bytes(self) -> int:
        if not self.truncated:
            return 0
        return max(0, self._seen - len(self._head) - len(self._tail))


def _drain_ready(
    selector: selectors.BaseSelector,
    captures: dict[str, _BoundedCapture],
    *,
    timeout: float,
) -> None:
    for key, _mask in selector.select(timeout):
        stream = str(key.data)
        try:
            chunk = os.read(key.fileobj.fileno(), _READ_CHUNK_BYTES)
        except OSError:
            chunk = b""
        if not chunk:
            try:
                selector.unregister(key.fileobj)
            except (KeyError, ValueError):
                pass
            try:
                key.fileobj.close()
            except OSError:
                pass
            continue
        captures[stream].append(chunk)


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _decode_captured_output(
    value: bytes,
    *,
    stream: str,
    head_bytes: int,
    omitted_bytes: int,
) -> str:
    if omitted_bytes <= 0:
        return value.decode("utf-8", errors="replace")
    head = value[:head_bytes].decode("utf-8", errors="replace")
    tail = value[head_bytes:].decode("utf-8", errors="replace")
    marker = f"\n[needle: {stream} omitted {omitted_bytes} bytes between head and tail]\n"
    return f"{head}{marker}{tail}"
