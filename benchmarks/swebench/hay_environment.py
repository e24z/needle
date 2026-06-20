"""Mini-SWE environment wrapper that prunes observations through Hay.

This is intentionally a thin adapter around Mini-SWE's stock Docker environment:
the agent, runner, dataset handling, and `preds.json` format stay upstream.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from minisweagent.environments.docker import DockerEnvironment, DockerEnvironmentConfig
from minisweagent.environments.extra.swerex_modal import (
    SwerexModalEnvironment,
    SwerexModalEnvironmentConfig,
)
from pydantic import Field
from swerex.deployment.modal import ModalDeployment
from swerex.runtime.abstract import Command as RexCommand

from pruner import client


_READ_COMMAND_RE = re.compile(r"(^|[;&|]\s*)(cat|nl|sed|grep|rg|find|head|tail)\b")
_BLOCKED_COMMAND_RE = re.compile(
    r"("
    r"\bpython\b|"
    r"\bpytest\b|"
    r"\bruntests\.py\b|"
    r"\bmanage\.py\s+test\b|"
    r"\btox\b|"
    r"\bgit\s+(diff|show|status)\b|"
    r"\bapply_patch\b|"
    r"\bpatch\.txt\b|"
    r"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT|"
    r"(^|[;&|]\s*)(cat|tee)\s+[^;&|]*>|"
    r">"
    r")",
    re.IGNORECASE,
)


def _env_int(names: tuple[str, ...], default: int) -> int:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return int(value)
    return default


def _env_float(names: tuple[str, ...], default: float) -> float:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return float(value)
    return default


def _env_optional_float(names: tuple[str, ...]) -> float | None:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return float(value)
    return None


def _env_bool(names: tuple[str, ...], default: bool) -> bool:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


class BenchmarkAbortError(RuntimeError):
    """Abort a benchmark run instead of emitting contaminated evidence."""


def _is_low_memory_passthrough(resp: dict[str, Any]) -> bool:
    stats = resp.get("stats") if isinstance(resp.get("stats"), dict) else {}
    return (
        resp.get("backend") == "passthrough:low-memory"
        or stats.get("passthrough_reason") == "low-memory"
    )


class HayDockerEnvironmentConfig(DockerEnvironmentConfig):
    hay_enabled: bool = True
    hay_min_chars: int = Field(
        default_factory=lambda: _env_int(("HAY_BENCH_MIN_CHARS", "HAY_MIN_CHARS"), 500)
    )
    hay_min_savings_ratio: float = Field(
        default_factory=lambda: _env_float(
            ("HAY_BENCH_MIN_SAVINGS_RATIO", "HAY_MIN_SAVINGS_RATIO"), 0.0
        )
    )
    hay_query_env: str = "HAY_BENCH_QUERY"
    hay_telemetry_path: str = Field(
        default_factory=lambda: os.getenv("HAY_BENCH_TELEMETRY", "")
    )
    hay_prune_timeout: float | None = Field(
        default_factory=lambda: _env_optional_float(("HAY_BENCH_PRUNE_TIMEOUT",))
    )
    hay_abort_on_low_memory: bool = Field(
        default_factory=lambda: _env_bool(("HAY_BENCH_ABORT_ON_LOW_MEMORY",), True)
    )


class _HayObservationMixin:
    def _maybe_prune_output(self, output: dict[str, Any], action: dict) -> dict[str, Any]:
        cfg = self.config
        if not cfg.hay_enabled:
            return output
        original = output.get("output", "")
        query, query_source = self._query_for(action)
        if not query:
            self._record(
                action,
                original if isinstance(original, str) else "",
                original if isinstance(original, str) else "",
                accepted=False,
                reason="no-query",
                query=query,
                query_source=query_source,
            )
            return output
        allowed, policy_reason = self._command_can_be_pruned(action)
        if not allowed:
            self._record(
                action,
                original if isinstance(original, str) else "",
                original if isinstance(original, str) else "",
                accepted=False,
                reason=policy_reason,
                query=query,
                query_source=query_source,
            )
            return output
        if not isinstance(original, str) or len(original) < cfg.hay_min_chars:
            self._record(
                action,
                original,
                original,
                accepted=False,
                reason="too-short",
                query=query,
                query_source=query_source,
            )
            return output

        started = time.perf_counter()
        try:
            resp = client.prune(
                text=original, query=query, timeout=cfg.hay_prune_timeout
            )
        except OSError as exc:
            self._record(
                action,
                original,
                original,
                accepted=False,
                reason=type(exc).__name__,
                query=query,
                query_source=query_source,
            )
            return output
        elapsed = time.perf_counter() - started

        if not resp.get("ok"):
            self._record(
                action,
                original,
                original,
                accepted=False,
                reason=str(resp.get("error", "not-ok")),
                query=query,
                query_source=query_source,
            )
            return output

        pruner_stats = resp.get("stats") if isinstance(resp.get("stats"), dict) else None
        if _is_low_memory_passthrough(resp):
            self._record(
                action,
                original,
                original,
                accepted=False,
                reason="low-memory",
                backend=str(resp.get("backend", "")),
                elapsed_s=round(elapsed, 3),
                query=query,
                query_source=query_source,
                pruner_stats=pruner_stats,
            )
            if cfg.hay_abort_on_low_memory:
                raise BenchmarkAbortError(
                    "Hay benchmark aborted: low-memory passthrough would "
                    "contaminate the run. Close memory-heavy apps and retry."
                )
            return output

        pruned = str(resp.get("text", ""))
        saved = len(original) - len(pruned)
        ratio = saved / len(original) if original else 0.0
        saved_tokens = None
        if pruner_stats and isinstance(pruner_stats.get("saved_tokens"), (int, float)):
            saved_tokens = int(pruner_stats["saved_tokens"])
        saved_for_acceptance = saved_tokens if saved_tokens is not None else saved
        accepted = saved_for_acceptance > 0 and ratio >= cfg.hay_min_savings_ratio
        reason = (
            "accepted"
            if accepted
            else ("expanded" if saved_for_acceptance < 0 else "below-threshold")
        )
        self._record(
            action,
            original,
            pruned if accepted else original,
            accepted=accepted,
            reason=reason,
            backend=str(resp.get("backend", "")),
            elapsed_s=round(elapsed, 3),
            query=query,
            query_source=query_source,
            candidate=pruned,
            pruner_stats=pruner_stats,
        )
        if accepted:
            output["output"] = pruned
        return output

    def _query_for(self, action: dict) -> tuple[str, str]:
        forced = os.getenv(self.config.hay_query_env, "").strip()
        if forced:
            return forced, "env"
        for key in ("context_focus_question", "hay_query", "query"):
            value = action.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), key
        return "", "missing"

    def _command_can_be_pruned(self, action: dict) -> tuple[bool, str]:
        command = str(action.get("command", ""))
        if _BLOCKED_COMMAND_RE.search(command):
            return False, "blocked-command"
        if _READ_COMMAND_RE.search(command):
            return True, "read-command"
        return False, "non-read-command"

    def _record(
        self,
        action: dict,
        original: str,
        pruned: str,
        *,
        accepted: bool,
        reason: str,
        backend: str = "",
        elapsed_s: float = 0.0,
        query: str = "",
        query_source: str = "",
        candidate: str | None = None,
        pruner_stats: dict[str, Any] | None = None,
    ) -> None:
        path = getattr(self.config, "hay_telemetry_path", "")
        if not path:
            return
        candidate = pruned if candidate is None else candidate
        rec = {
            "ts": round(time.time(), 3),
            "accepted": accepted,
            "reason": reason,
            "backend": backend,
            "elapsed_s": elapsed_s,
            "command": str(action.get("command", ""))[:500],
            "query_source": query_source,
            "query": query[:500],
            "query_chars": len(query),
            "original_chars": len(original),
            "pruned_chars": len(pruned),
            "saved_chars": len(original) - len(pruned),
            "candidate_chars": len(candidate),
            "candidate_saved_chars": len(original) - len(candidate),
        }
        if pruner_stats:
            rec["pruner"] = pruner_stats
            for key in (
                "original_tokens",
                "raw_pruned_tokens",
                "pruned_tokens",
                "saved_tokens",
                "model_input_tokens",
                "chunks",
                "scored_tokens",
                "kept_lines",
                "code_token_budget",
                "chunk_overlap_tokens",
                "chunked",
                "repair",
                "floor_enabled",
                "floor_applied",
                "passthrough_reason",
            ):
                if key in pruner_stats:
                    rec[key] = pruner_stats[key]
        try:
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError:
            pass


class HayDockerEnvironment(_HayObservationMixin, DockerEnvironment):
    """Prune local Docker command output before Mini-SWE observes it."""

    def __init__(self, *, config_class: type = HayDockerEnvironmentConfig, **kwargs):
        super().__init__(config_class=config_class, **kwargs)

    def execute(
        self, action: dict, cwd: str = "", *, timeout: int | None = None
    ) -> dict[str, Any]:
        return self._maybe_prune_output(
            super().execute(action, cwd=cwd, timeout=timeout), action
        )


class BenchmarkModalEnvironmentConfig(SwerexModalEnvironmentConfig):
    interpreter: list[str] = Field(default_factory=lambda: ["bash", "-c"])


class BenchmarkModalEnvironment(SwerexModalEnvironment):
    """Modal/SWE-ReX environment that preserves Mini-SWE's interpreter contract.

    The upstream Modal wrapper uses SWE-ReX ``Command(shell=True)``, which runs
    through ``/bin/sh`` and bypasses SWE-bench images' ``BASH_ENV=/root/.bashrc``
    activation path. Docker runs use ``interpreter`` explicitly, so keep Modal on
    the same contract here.
    """

    config: BenchmarkModalEnvironmentConfig

    def __init__(self, **kwargs):
        self.config = BenchmarkModalEnvironmentConfig(**kwargs)
        self.deployment = ModalDeployment(
            image=self.config.image,
            startup_timeout=self.config.startup_timeout,
            runtime_timeout=self.config.runtime_timeout,
            deployment_timeout=self.config.deployment_timeout,
            install_pipx=self.config.install_pipx,
            modal_sandbox_kwargs=self.config.modal_sandbox_kwargs,
        )
        asyncio.run(self.deployment.start())

    def execute(
        self, action: dict, cwd: str = "", *, timeout: int | None = None
    ) -> dict[str, Any]:
        command = action.get("command", "") if isinstance(action, dict) else action
        try:
            result = asyncio.run(
                self.deployment.runtime.execute(
                    RexCommand(
                        command=[*self.config.interpreter, str(command)],
                        shell=False,
                        check=False,
                        cwd=cwd or self.config.cwd,
                        timeout=timeout or self.config.timeout,
                        merge_output_streams=True,
                        env=self.config.env if self.config.env else None,
                    )
                )
            )
            output = {
                "output": result.stdout,
                "returncode": result.exit_code,
                "exception_info": "",
            }
        except Exception as exc:
            output = {
                "output": str(exc) if str(exc) else "",
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {exc}",
                "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
            }
        self._check_finished(output)
        return output

    async def _stop_deployment(self) -> None:
        runtime = getattr(self.deployment, "_runtime", None)
        sandbox = getattr(self.deployment, "_sandbox", None)
        if runtime is not None:
            try:
                await runtime.close()
            finally:
                self.deployment._runtime = None
        if sandbox is not None:
            try:
                await sandbox.terminate.aio()
            finally:
                self.deployment._sandbox = None
        self.deployment._app = None

    def stop(self) -> None:
        asyncio.run(self._stop_deployment())


class HayModalEnvironmentConfig(BenchmarkModalEnvironmentConfig):
    hay_enabled: bool = True
    hay_min_chars: int = Field(
        default_factory=lambda: _env_int(("HAY_BENCH_MIN_CHARS", "HAY_MIN_CHARS"), 500)
    )
    hay_min_savings_ratio: float = Field(
        default_factory=lambda: _env_float(
            ("HAY_BENCH_MIN_SAVINGS_RATIO", "HAY_MIN_SAVINGS_RATIO"), 0.0
        )
    )
    hay_query_env: str = "HAY_BENCH_QUERY"
    hay_telemetry_path: str = Field(
        default_factory=lambda: os.getenv("HAY_BENCH_TELEMETRY", "")
    )
    hay_prune_timeout: float | None = Field(
        default_factory=lambda: _env_optional_float(("HAY_BENCH_PRUNE_TIMEOUT",))
    )
    hay_abort_on_low_memory: bool = Field(
        default_factory=lambda: _env_bool(("HAY_BENCH_ABORT_ON_LOW_MEMORY",), True)
    )


class HayModalEnvironment(_HayObservationMixin, BenchmarkModalEnvironment):
    """Prune Modal/SWE-ReX command output before Mini-SWE observes it."""

    def __init__(self, **kwargs):
        self.config = HayModalEnvironmentConfig(**kwargs)
        self.deployment = ModalDeployment(
            image=self.config.image,
            startup_timeout=self.config.startup_timeout,
            runtime_timeout=self.config.runtime_timeout,
            deployment_timeout=self.config.deployment_timeout,
            install_pipx=self.config.install_pipx,
            modal_sandbox_kwargs=self.config.modal_sandbox_kwargs,
        )
        asyncio.run(self.deployment.start())

    def execute(
        self, action: dict, cwd: str = "", *, timeout: int | None = None
    ) -> dict[str, Any]:
        return self._maybe_prune_output(
            super().execute(action, cwd=cwd, timeout=timeout), action
        )
