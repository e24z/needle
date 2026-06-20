#!/usr/bin/env python3
"""Terminal observer for Hay SWE-bench runs.

This is intentionally read-only: it polls run artifacts under benchmarks/runs,
local process state, and Modal's container logs. It does not start, stop, or
submit anything.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_ROOT = ROOT / "benchmarks" / "runs"
MODES = ("baseline", "hay")
SPARK = "▁▂▃▄▅▆▇█"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _run(cmd: list[str], *, timeout: float = 4.0) -> str:
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return proc.stdout.strip() or proc.stderr.strip()


def _latest_run(runs_root: Path) -> Path | None:
    runs = [p for p in runs_root.iterdir() if p.is_dir()] if runs_root.exists() else []
    if not runs:
        return None
    return max(runs, key=lambda p: p.stat().st_mtime)


def _fmt_time(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ").replace("Z", " UTC")


def _sparkline(values: deque[float], width: int = 24) -> str:
    if not values:
        return ""
    vals = list(values)[-width:]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return SPARK[0] * len(vals)
    return "".join(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in vals)


def _short(value: Any, n: int = 100) -> str:
    text = str(value or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _tail(path: Path, n: int = 12) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _trajectory_summary(mode_dir: Path) -> dict[str, Any]:
    trajs = sorted(mode_dir.glob("*/*.traj.json"), key=lambda p: p.stat().st_mtime)
    latest = trajs[-1] if trajs else None
    data = _read_json(latest) if latest else {}
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    model_stats = info.get("model_stats") if isinstance(info.get("model_stats"), dict) else {}
    last_command = ""
    last_thought = ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if not last_thought and message.get("role") == "assistant":
            last_thought = _short(message.get("content"), 130)
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                fn = (tool_calls[0] or {}).get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                last_command = _short(args.get("command"), 130)
        if not last_command:
            extra = message.get("extra") if isinstance(message.get("extra"), dict) else {}
            actions = extra.get("actions") if isinstance(extra.get("actions"), list) else []
            if actions and isinstance(actions[-1], dict):
                last_command = _short(actions[-1].get("command"), 130)
        if last_command and last_thought:
            break
    return {
        "trajs": len(trajs),
        "messages": len(messages),
        "assistant": sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant"),
        "tools": sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool"),
        "exit": info.get("exit_status", "-"),
        "cost": float(model_stats.get("instance_cost") or 0),
        "api_calls": int(model_stats.get("api_calls") or 0),
        "instance": data.get("instance_id") or (latest.parent.name if latest else "-"),
        "latest_mtime": latest.stat().st_mtime if latest else None,
        "last_command": last_command,
        "last_thought": last_thought,
    }


def _telemetry_summary(mode_dir: Path) -> dict[str, Any]:
    rows = _read_jsonl(mode_dir / "hay_telemetry.jsonl")
    return {
        "rows": rows,
        "n": len(rows),
        "accepted": sum(1 for r in rows if r.get("accepted")),
        "passthrough": sum(1 for r in rows if "passthrough" in str(r.get("backend", ""))),
        "expanded": sum(1 for r in rows if int(r.get("candidate_saved_chars") or 0) < 0),
        "saved": sum(int(r.get("saved_chars") or 0) for r in rows),
        "saved_tokens": sum(int(r.get("saved_tokens") or 0) for r in rows),
        "model_input_tokens": sum(int(r.get("model_input_tokens") or 0) for r in rows),
        "candidate_saved": sum(int(r.get("candidate_saved_chars") or 0) for r in rows),
        "last_backend": rows[-1].get("backend", "-") if rows else "-",
        "last_rows": rows[-5:],
    }


def _validation_summary(mode_dir: Path) -> dict[str, Any]:
    report = _read_json(mode_dir / "local-validation.json")
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return {
        "exists": bool(summary),
        "resolved": int(summary.get("resolved_instances") or 0),
        "submitted": int(summary.get("submitted_instances") or 0),
        "unresolved": int(summary.get("unresolved_instances") or 0),
        "errors": int(summary.get("error_instances") or 0),
    }


def _mode_state(run_dir: Path, mode: str) -> dict[str, Any]:
    mode_dir = run_dir / mode
    preds = _read_json(mode_dir / "preds.json")
    traj = _trajectory_summary(mode_dir)
    telemetry = _telemetry_summary(mode_dir) if mode == "hay" else {
        "n": 0,
        "accepted": 0,
        "passthrough": 0,
        "expanded": 0,
        "saved": 0,
        "saved_tokens": 0,
        "model_input_tokens": 0,
        "candidate_saved": 0,
        "last_backend": "-",
        "last_rows": [],
    }
    return {
        "exists": mode_dir.exists(),
        "preds": len(preds),
        "traj": traj,
        "telemetry": telemetry,
        "validation": _validation_summary(mode_dir),
    }


def _process_rows() -> list[tuple[str, str, str]]:
    out = _run(["pgrep", "-fl", "benchmarks/swebench/run.py|pruner session|pruner manage|caffeinate"], timeout=2)
    rows: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        pid, _, command = line.partition(" ")
        role = "process"
        if "benchmarks/swebench/run.py" in command:
            role = "runner"
        elif "pruner session" in command:
            role = "hay session"
        elif "pruner manage" in command:
            role = "hay manager"
        elif "caffeinate" in command:
            role = "caffeinate"
        rows.append((role, pid, _short(command, 110)))
    return rows


def _modal() -> dict[str, Any]:
    if shutil.which("modal") is None:
        return {"containers": [], "logs": "modal not on PATH"}
    raw = _run(["modal", "container", "list", "--json"], timeout=6)
    try:
        containers = json.loads(raw)
    except ValueError:
        return {"containers": [], "logs": raw}
    logs: list[str] = []
    for container in containers[:2]:
        cid = container.get("container_id")
        if cid:
            text = _run(["modal", "container", "logs", cid, "--tail", "8", "--timestamps"], timeout=6)
            if text:
                logs.append(f"{cid}\n{text}")
    return {"containers": containers, "logs": "\n\n".join(logs)}


def _header(run_dir: Path | None, manifest: dict[str, Any]) -> Panel:
    if run_dir is None:
        return Panel("No run directories found.", title="Hay SWE-bench", border_style="red")
    bits = [
        f"run: {run_dir.name}",
        f"backend: {manifest.get('backend', '-')}",
        f"subset: {manifest.get('subset', '-')}/{manifest.get('split', '-')}",
        f"slice: {manifest.get('slice', '-')}",
        f"model: {manifest.get('model', '-')}",
        f"started: {_fmt_time(manifest.get('started_at'))}",
        f"finished: {_fmt_time(manifest.get('finished_at'))}",
    ]
    return Panel("\n".join(bits), title="Hay SWE-bench", border_style="cyan")


def _mode_panel(mode: str, state: dict[str, Any], history: dict[str, deque[float]]) -> Panel:
    if not state.get("exists"):
        return Panel("not present", title=mode, border_style="dim")
    traj = state["traj"]
    tel = state["telemetry"]
    history[f"{mode}:messages"].append(float(traj["messages"]))
    history[f"{mode}:cost"].append(float(traj["cost"]))
    history[f"{mode}:prunes"].append(float(tel["n"]))

    table = Table(box=box.SIMPLE, expand=True, show_header=False)
    table.add_column("metric", style="bold")
    table.add_column("value")
    table.add_row("instance", _short(traj["instance"], 44))
    table.add_row("exit", str(traj["exit"]))
    table.add_row("preds", str(state["preds"]))
    validation = state.get("validation") or {}
    if validation.get("exists"):
        table.add_row(
            "validated",
            f"{validation['resolved']}/{validation['submitted']} resolved; "
            f"{validation['unresolved']} unresolved; {validation['errors']} errors",
        )
    table.add_row("messages", f'{traj["messages"]}  {_sparkline(history[f"{mode}:messages"])}')
    table.add_row("api/cost", f'{traj["api_calls"]} / ${traj["cost"]:.4f}  {_sparkline(history[f"{mode}:cost"])}')
    if mode == "hay":
        table.add_row("prunes", f'{tel["n"]}  accepted={tel["accepted"]} passthru={tel["passthrough"]}  {_sparkline(history[f"{mode}:prunes"])}')
        table.add_row("saved chars", f'{tel["saved"]} candidate={tel["candidate_saved"]}')
        if tel.get("saved_tokens") or tel.get("model_input_tokens"):
            table.add_row(
                "saved tokens",
                f'{tel["saved_tokens"]} pruner_input={tel["model_input_tokens"]}',
            )
        table.add_row("backend", str(tel["last_backend"]))
    table.add_row("last command", traj["last_command"] or "-")
    table.add_row("last thought", traj["last_thought"] or "-")
    return Panel(table, title=mode, border_style="green" if mode == "hay" else "blue")


def _process_panel(rows: list[tuple[str, str, str]]) -> Panel:
    table = Table(box=box.SIMPLE, expand=True)
    table.add_column("role")
    table.add_column("pid", justify="right")
    table.add_column("command")
    for role, pid, command in rows:
        table.add_row(role, pid, command)
    if not rows:
        table.add_row("-", "-", "no local benchmark/pruner processes")
    return Panel(table, title="local processes")


def _modal_panel(modal: dict[str, Any]) -> Panel:
    containers = modal.get("containers") or []
    table = Table(box=box.SIMPLE, expand=True)
    table.add_column("container")
    table.add_column("app")
    table.add_column("started")
    for c in containers:
        table.add_row(
            str(c.get("container_id", "-")),
            str(c.get("app_name", c.get("app_id", "-"))),
            str(c.get("start_time", "-")),
        )
    if not containers:
        table.add_row("-", "-", "no active Modal containers")
    logs = _short(modal.get("logs") or "", 1400)
    return Panel(Group(table, Text("\n" + logs if logs else "")), title="modal containers/log tail")


def _telemetry_panel(run_dir: Path, state: dict[str, Any]) -> Panel:
    rows = state.get("hay", {}).get("telemetry", {}).get("last_rows", [])
    table = Table(box=box.SIMPLE, expand=True)
    table.add_column("#", justify="right")
    table.add_column("backend")
    table.add_column("save", justify="right")
    table.add_column("tok", justify="right")
    table.add_column("accepted")
    table.add_column("command")
    start = max(0, state.get("hay", {}).get("telemetry", {}).get("n", 0) - len(rows))
    for i, row in enumerate(rows, start + 1):
        table.add_row(
            str(i),
            _short(row.get("backend"), 22),
            str(row.get("saved_chars", 0)),
            str(row.get("saved_tokens", "-")),
            "yes" if row.get("accepted") else str(row.get("reason", "no")),
            _short(row.get("command"), 95),
        )
    if not rows:
        table.add_row("-", "-", "-", "-", "-", "no Hay telemetry yet")
    return Panel(table, title="latest Hay prune telemetry")


def _log_panel(run_dir: Path) -> Panel:
    candidates = [
        run_dir / "terminal.log",
        run_dir / "dashboard-run.log",
        run_dir / "hay" / "minisweagent.log",
        run_dir / "baseline" / "minisweagent.log",
    ]
    for path in candidates:
        text = _tail(path, 12)
        if text:
            return Panel(text, title=f"log tail: {path.relative_to(run_dir)}")
    return Panel("no log yet", title="log tail")


def render(run_dir: Path | None, history: dict[str, deque[float]], include_modal: bool) -> Group:
    manifest = _read_json(run_dir / "manifest.json") if run_dir else {}
    states = {mode: _mode_state(run_dir, mode) for mode in MODES} if run_dir else {}
    modal = _modal() if include_modal else {"containers": [], "logs": "disabled"}
    return Group(
        _header(run_dir, manifest),
        Columns([_mode_panel("baseline", states.get("baseline", {}), history), _mode_panel("hay", states.get("hay", {}), history)], equal=True),
        Columns([_process_panel(_process_rows()), _modal_panel(modal)], equal=True),
        _telemetry_panel(run_dir, states) if run_dir else Panel("no run", title="latest Hay prune telemetry"),
        _log_panel(run_dir) if run_dir else Panel("no run", title="log tail"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?", help="run directory to watch; defaults to latest under benchmarks/runs")
    parser.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-modal", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runs_root = Path(args.runs_root)
    run_dir = Path(args.run_dir) if args.run_dir else _latest_run(runs_root)
    history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=80))
    if args.once:
        from rich.console import Console

        Console().print(render(run_dir, history, not args.no_modal))
        return 0
    with Live(
        render(run_dir, history, not args.no_modal),
        refresh_per_second=max(1, int(1 / max(args.interval, 0.5))),
        screen=True,
    ) as live:
        while True:
            if not args.run_dir:
                run_dir = _latest_run(runs_root)
            live.update(render(run_dir, history, not args.no_modal))
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
