#!/usr/bin/env python3
"""Live Streamlit dashboard for Hay SWE-bench runs.

Built around one question: does Hay hold resolve-rate parity while cutting the
agent's end-to-end consumption, without spending it back on extra turns?

Layout follows that: gate (resolve parity) · win (cost/instance) · trap
(turns/instance) · trust (prune-outcome funnel). The harness records cost and
api_calls per instance (no raw token counts), so cost is the end-to-end proxy.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = Path(os.environ.get("HAY_BENCH_RUNS_ROOT", ROOT / "benchmarks" / "runs"))
sys.path.insert(0, str(ROOT))

from pruner import sysmem as hay_sysmem  # noqa: E402
from pruner.cli import _read_status, _status_payload  # noqa: E402

# Palette: baseline is the neutral control, Hay is the one Viridis accent. The
# full Viridis gradient is reserved for genuinely continuous data (the curve).
BASELINE_COLOR = "#888780"  # neutral slate
HAY_COLOR = "#1f9e89"  # Viridis teal-green
WARN_COLOR = "#BA7517"  # amber — passthrough / contaminated
MUTED_COLOR = "#b4b2a9"  # gray — skipped / no opportunity
_MODE_SCALE = alt.Scale(domain=["baseline", "hay"], range=[BASELINE_COLOR, HAY_COLOR])

_TOOL_DIRS = [
    Path.home() / ".local" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
]


def _tool_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for directory in _TOOL_DIRS:
        candidate = directory / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    searched = ", ".join(str(path) for path in _TOOL_DIRS)
    raise FileNotFoundError(f"Required command {name!r} not found. Searched PATH and {searched}.")


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    path_parts = [str(path) for path in _TOOL_DIRS]
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_parts)
    return env
_CHART_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
_PRESSURE = {1: "normal", 2: "warning", 4: "critical"}
_PROCESS_FILE = "dashboard-process.json"

# CSS covers DOM elements. Chart-internal text (axis labels, legend, titles) is
# rendered by Vega and is unreachable from CSS — use _configure_chart_font() for that.
# Never use `.stApp *` — it clobbers Streamlit's Material Symbols icon font and
# glyphs render as ligature text ("double_arrow_right", "arrow_drop_down").
_FONT_CSS = """
<style>
:root { --hay-font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
html, body, .stApp { font-family: var(--hay-font) !important; }
h1, h2, h3, h4, h5, h6,
[data-testid="stHeading"],
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] *,
[data-testid="stMetricValue"], [data-testid="stMetricLabel"], [data-testid="stMetricDelta"],
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] *,
[data-testid="stExpanderDetails"] *,
[data-testid="stSidebar"] p, [data-testid="stSidebar"] label,
.ag-cell, .ag-header-cell-text, .ag-header-cell-label,
button, input, select, textarea,
[data-baseweb="tab"] {
  font-family: var(--hay-font) !important;
}
[data-testid="stMetricValue"] { font-weight: 500; }
</style>
"""


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


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _runs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    runs = [
        p
        for p in root.iterdir()
        if (p / "manifest.json").exists() or (p / _PROCESS_FILE).exists()
    ]

    def mtime(path: Path) -> float:
        marker = path / "manifest.json"
        if not marker.exists():
            marker = path / _PROCESS_FILE
        return marker.stat().st_mtime

    return sorted(runs, key=mtime, reverse=True)


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _run_process(run_dir: Path) -> dict[str, Any]:
    meta = _read_json(run_dir / _PROCESS_FILE)
    pid = meta.get("pid")
    meta["running"] = _pid_alive(pid if isinstance(pid, int) else None)
    return meta


def _stop_process(meta: dict[str, Any]) -> str:
    pid = meta.get("pid")
    pgid = meta.get("pgid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        return "No live benchmark process found for this run."
    try:
        if isinstance(pgid, int) and pgid > 0:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return f"Stop signal failed: {exc}"
    return f"Sent SIGTERM to benchmark PID {pid}."


def _slug(value: str, *, fallback: str = "run") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def _auto_run_id(
    *,
    runs_root: Path,
    subset: str,
    split: str,
    slice_spec: str,
    backend: str,
    modes: str,
) -> str:
    mode_names = [name.strip() for name in modes.split(",") if name.strip()]
    mode_part = "paired" if set(mode_names) == {"baseline", "hay"} else "-".join(mode_names)
    base = "-".join(
        [
            _slug(backend),
            _slug(mode_part, fallback="modes"),
            _slug(subset),
            _slug(split),
            _slug(slice_spec, fallback="slice"),
            time.strftime("%Y%m%d-%H%M%S"),
        ]
    )
    candidate = base
    suffix = 2
    while (runs_root / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _launch_run(
    *,
    runs_root: Path,
    run_id: str,
    subset: str,
    split: str,
    slice_spec: str,
    workers: int,
    model: str,
    backend: str,
    modes: str,
    vibeproxy_url: str,
    submit: bool,
    hay_query: str,
    min_free_mb: str,
) -> tuple[bool, str]:
    requested_run_id = run_id.strip()
    run_id = requested_run_id or _auto_run_id(
        runs_root=runs_root,
        subset=subset,
        split=split,
        slice_spec=slice_spec,
        backend=backend,
        modes=modes,
    )
    run_dir = runs_root / run_id
    if (run_dir / _PROCESS_FILE).exists() and _run_process(run_dir).get("running"):
        return False, f"Run {run_id} already has a live benchmark process."
    if requested_run_id and run_dir.exists() and any(run_dir.iterdir()):
        return False, f"Run {run_id} already exists. Leave Run ID blank to auto-create a fresh one."

    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "dashboard-run.log"
    cmd = [
        _tool_path("uv"),
        "run",
        "--extra",
        "bench",
        "python",
        "benchmarks/swebench/run.py",
        "--subset",
        subset,
        "--split",
        split,
        "--slice",
        slice_spec,
        "--workers",
        str(workers),
        "--model",
        model,
        "--backend",
        backend,
        "--modes",
        modes,
        "--output",
        str(runs_root),
        "--run-id",
        run_id,
        "--vibeproxy-url",
        vibeproxy_url,
        "--hay-query",
        hay_query,
        "--submit" if submit else "--no-submit",
    ]
    env = _child_env()
    if min_free_mb.strip():
        env["HAY_MIN_FREE_MB"] = min_free_mb.strip()

    log = log_path.open("a", buffering=1)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    pgid = None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pass
    _write_json(
        run_dir / _PROCESS_FILE,
        {
            "pid": proc.pid,
            "pgid": pgid,
            "cmd": cmd,
            "run_id": run_id,
            "started_at": time.time(),
            "log_path": str(log_path),
            "env_overrides": {"HAY_MIN_FREE_MB": env.get("HAY_MIN_FREE_MB", "")},
        },
    )
    return True, f"Started {run_id} as PID {proc.pid}."


def _memory_snapshot() -> dict[str, Any]:
    pressure, available_mb = hay_sysmem.memstat()
    return {
        "pressure": pressure,
        "pressure_label": _PRESSURE.get(pressure, "?"),
        "available_mb": available_mb,
    }


def _fmt_gb(mb: Any) -> str:
    if (
        not isinstance(mb, (int, float))
        or mb <= 0
        or mb >= hay_sysmem._UNKNOWN_AVAIL_MB
    ):
        return "?"
    return f"{mb / 1024:.1f} GB"


def _observed_process_rows() -> list[dict[str, Any]]:
    pattern = (
        "streamlit run benchmarks/dashboard.py|benchmarks/swebench/run.py|"
        "pruner session|caffeinate -dimsu"
    )
    try:
        out = subprocess.run(
            ["pgrep", "-fl", pattern],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        pid, _, command = line.partition(" ")
        rows.append({"pid": pid, "command": command})
    return rows


def _takeover_commands(run_dir: Path, process: dict[str, Any]) -> str:
    rel_run = run_dir.relative_to(ROOT) if run_dir.is_relative_to(ROOT) else run_dir
    pgid = process.get("pgid")
    pid = process.get("pid")
    lines = [
        f"cd {ROOT}",
        "pgrep -fl 'streamlit run benchmarks/dashboard.py|benchmarks/swebench/run.py|pruner session|caffeinate -dimsu'",
        f"cat {rel_run / _PROCESS_FILE}",
        f"tail -f {rel_run / 'dashboard-run.log'}",
    ]
    if (run_dir / "hay" / "minisweagent.log").exists():
        lines.append(f"tail -f {rel_run / 'hay' / 'minisweagent.log'}")
    if (run_dir / "baseline" / "minisweagent.log").exists():
        lines.append(f"tail -f {rel_run / 'baseline' / 'minisweagent.log'}")
    if isinstance(pgid, int) and pgid > 0:
        lines.append(f"kill -TERM -{pgid}")
    elif isinstance(pid, int) and pid > 0:
        lines.append(f"kill -TERM {pid}")
    return "\n".join(lines)


def _configure_chart_font(chart: Any) -> Any:
    """Apply the OS system font to Altair chart text (axis, legend, title, header).

    CSS cannot reach Vega-rendered text; this is the correct path for chart typography.
    """
    return (
        chart
        .configure_axis(labelFont=_CHART_FONT, titleFont=_CHART_FONT)
        .configure_legend(labelFont=_CHART_FONT, titleFont=_CHART_FONT)
        .configure_title(font=_CHART_FONT)
        .configure_header(labelFont=_CHART_FONT, titleFont=_CHART_FONT)
    )


def _mode_rows(run_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    modes = manifest.get("modes") or {}
    for mode, meta in modes.items():
        mode_dir = run_dir / mode
        telemetry = _read_jsonl(mode_dir / "hay_telemetry.jsonl")
        preds = _read_json(mode_dir / "preds.json")
        traj = list(mode_dir.glob("*/*.traj.json"))
        traj_data = [_read_json(path) for path in traj]
        accepted = [r for r in telemetry if r.get("accepted")]
        expanded = [
            r
            for r in telemetry
            if int(r.get("candidate_saved_chars", r.get("saved_chars", 0)) or 0) < 0
        ]
        original_chars = sum(int(r.get("original_chars") or 0) for r in accepted)
        saved_chars = sum(int(r.get("saved_chars") or 0) for r in accepted)
        model_cost = sum(
            float(((data.get("info") or {}).get("model_stats") or {}).get("instance_cost") or 0.0)
            for data in traj_data
        )
        api_calls = sum(
            int(((data.get("info") or {}).get("model_stats") or {}).get("api_calls") or 0)
            for data in traj_data
        )
        instances = len(preds)
        local = meta.get("local_validation") if isinstance(meta.get("local_validation"), dict) else {}
        status = "failed" if meta.get("error") else "generated"
        if local.get("enabled") and local.get("submitted_instances"):
            status = "validated"
        elif meta.get("sbcli_run_id"):
            status = "sbcli-submitted"
        rows.append(
            {
                "mode": mode,
                "status": status,
                "instances": instances,
                "local_resolved": local.get("resolved_instances", ""),
                "local_unresolved": local.get("unresolved_instances", ""),
                "local_errors": local.get("error_instances", ""),
                "trajectories": len(traj),
                "api_calls": api_calls,
                "turns_per_inst": round(api_calls / instances, 2) if instances else 0.0,
                "model_cost": round(model_cost, 6),
                "cost_per_inst": round(model_cost / instances, 6) if instances else 0.0,
                "hay_calls": len(telemetry),
                "hay_accepted": len(accepted),
                "hay_expanded": len(expanded),
                "saved_chars": saved_chars,
                "savings_pct": round(100 * saved_chars / original_chars, 1) if original_chars else 0.0,
                "elapsed_s": meta.get("elapsed_s", 0),
                "sbcli_run_id": meta.get("sbcli_run_id", ""),
                "error": meta.get("error", ""),
            }
        )
    return rows


def _validation_rows(run_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for mode, meta in (manifest.get("modes") or {}).items():
        local = meta.get("local_validation") if isinstance(meta.get("local_validation"), dict) else {}
        if not local:
            report = _read_json(run_dir / mode / "local-validation.json")
            local = report.get("summary") if isinstance(report.get("summary"), dict) else {}
            if local:
                local = {"enabled": True, **local}
        if not local.get("enabled"):
            continue
        rows.append(
            {
                "mode": mode,
                "source": "local-modal",
                "resolved": local.get("resolved_instances", 0),
                "total": local.get("submitted_instances", 0),
                "completed": local.get("completed_instances", 0),
                "unresolved": local.get("unresolved_instances", 0),
                "errors": local.get("error_instances", 0),
                "report_path": local.get("report_path", str(run_dir / mode / "local-validation.json")),
            }
        )
    return rows


def _trajectory_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in run_dir.glob("*/*/*.traj.json"):
        data = _read_json(path)
        info = data.get("info") or {}
        stats = info.get("model_stats") or {}
        rows.append(
            {
                "mode": path.parts[-3],
                "instance_id": data.get("instance_id") or path.stem.replace(".traj", ""),
                "exit_status": info.get("exit_status", ""),
                "api_calls": stats.get("api_calls", 0),
                "instance_cost": stats.get("instance_cost", 0.0),
                "submission_chars": len(str(info.get("submission") or "")),
            }
        )
    return rows


def _telemetry_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in run_dir.glob("*/hay_telemetry.jsonl"):
        mode = path.parent.name
        for rec in _read_jsonl(path):
            rows.append({"mode": mode, **rec})
    return rows


def _token_turns(run_dir: Path) -> list[dict[str, Any]]:
    """Per-API-call token counts extracted from trajectory message metadata.

    Each assistant message carries exact prompt/completion/cached token counts
    from the LiteLLM response object. No approximation needed.
    """
    rows: list[dict[str, Any]] = []
    for path in run_dir.glob("*/*/*.traj.json"):
        data = _read_json(path)
        mode = path.parts[-3]
        instance_id = data.get("instance_id") or path.stem.replace(".traj", "")
        for msg in data.get("messages", []):
            if msg.get("role") != "assistant":
                continue
            extra = msg.get("extra") or {}
            resp = extra.get("response") or {}
            usage = resp.get("usage") or {}
            if not usage:
                continue
            prompt_details = usage.get("prompt_tokens_details") or {}
            rows.append({
                "mode": mode,
                "instance_id": instance_id,
                "ts": extra.get("timestamp") or resp.get("created"),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cached_tokens": int(prompt_details.get("cached_tokens") or 0),
                "cost": float(extra.get("cost") or 0.0),
            })
    return rows


def _report_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in run_dir.glob("*/sb-cli-reports/**/*.json"):
        data = _read_json(path)
        mode = path.relative_to(run_dir).parts[0]
        flat = {
            "mode": mode,
            "source": "sbcli-cloud-job",
            "file": path.name,
            "completed_jobs": data.get("completed_instances", ""),
            "resolved_jobs": data.get("resolved_instances", ""),
            "unresolved_jobs": data.get("unresolved_instances", ""),
            "failed_jobs": data.get("failed_instances", ""),
            "error_jobs": data.get("error_instances", ""),
            "submitted_jobs": data.get("submitted_instances", ""),
            "total_dataset_instances": data.get("total_instances", ""),
            "path": str(path),
        }
        rows.append(flat)
    return rows


def _status_snapshot(events_n: int = 20) -> dict[str, Any]:
    stats, recent = _read_status(events_n)
    return _status_payload(stats, recent)


def _first(df: pd.DataFrame, mode: str) -> dict[str, Any]:
    """First row for a mode as a dict, or {} when absent."""
    if df.empty or "mode" not in df.columns:
        return {}
    sub = df[df["mode"] == mode]
    return sub.iloc[0].to_dict() if not sub.empty else {}


def _resolve_counts(reports: pd.DataFrame, mode: str) -> tuple[int | None, int | None]:
    row = _first(reports, mode)
    if not row:
        return None, None
    try:
        return int(row.get("resolved")), int(row.get("total"))
    except (TypeError, ValueError):
        return None, None


def _outcome_bucket(rec: dict[str, Any]) -> str:
    """Collapse a prune record into a trust bucket."""
    if rec.get("accepted"):
        return "accepted"
    try:
        if int(rec.get("candidate_saved_chars", rec.get("saved_chars", 0)) or 0) < 0:
            return "expanded"
    except (TypeError, ValueError):
        pass
    blob = f"{rec.get('reason') or ''} {rec.get('backend') or ''}".lower()
    if "memory" in blob or "passthrough" in blob:
        return "passthrough"
    return "skipped"


def _mode_scale(modes_present: Any) -> alt.Scale:
    """Color scale over only the modes actually present, so a single-mode run
    doesn't render a phantom 'baseline' entry in the legend."""
    present = sorted(set(modes_present))
    return alt.Scale(
        domain=present,
        range=[BASELINE_COLOR if m == "baseline" else HAY_COLOR for m in present],
    )


def _completed_ids(run_dir: Path, mode: str) -> set[str]:
    """Instance ids a mode has finished — preds.json gets an entry on completion."""
    return set(_read_json(run_dir / mode / "preds.json").keys())


def _instance_summary(token_turns: pd.DataFrame, run_dir: Path) -> pd.DataFrame:
    """Per (mode, instance) token totals + completion flag — the atom for WIN/PULSE.

    uncached = prompt - cached, the billable cost driver. Completion comes from
    preds.json (written only when an instance finishes), so in-flight instances are
    flagged incomplete and excluded from the 'total saved' running line.
    """
    if token_turns.empty:
        return pd.DataFrame()
    df = token_turns.dropna(subset=["ts"]).copy()
    df["uncached"] = (df["prompt_tokens"] - df["cached_tokens"]).clip(lower=0)
    summary = (
        df.groupby(["mode", "instance_id"])
        .agg(
            total_uncached=("uncached", "sum"),
            total_prompt=("prompt_tokens", "sum"),
            total_cost=("cost", "sum"),
            last_ts=("ts", "max"),
            n_turns=("ts", "count"),
        )
        .reset_index()
    )
    completed = {m: _completed_ids(run_dir, m) for m in summary["mode"].unique()}
    summary["complete"] = summary.apply(
        lambda r: r["instance_id"] in completed.get(r["mode"], set()), axis=1
    )
    return summary


def _running_totals(summary: pd.DataFrame) -> pd.DataFrame:
    """Completed instances only, in completion order, cumulative tokens per mode.

    Same series feeds the WIN sum (x = completion index) and the PULSE stepped line
    (x = wall-clock). Each instance contributes its full total once, when it finishes,
    so varying turn counts never enter the math.
    """
    if summary.empty or "complete" not in summary.columns:
        return pd.DataFrame()
    df = summary[summary["complete"]].sort_values(["mode", "last_ts"]).copy()
    if df.empty:
        return df
    df["running_uncached"] = df.groupby("mode")["total_uncached"].cumsum()
    df["completion_index"] = df.groupby("mode").cumcount() + 1
    df["ts"] = pd.to_datetime(pd.to_numeric(df["last_ts"], errors="coerce"), unit="s")
    return df


def _turn_band(token_turns: pd.DataFrame, min_alive: int = 5) -> pd.DataFrame:
    """Median + IQR of cumulative uncached tokens by turn index, per mode.

    Cut where fewer than `min_alive` instances remain so the thinning tail can't
    fake a trend. Median (not mean) keeps one runaway instance from dominating.
    """
    if token_turns.empty:
        return pd.DataFrame()
    df = token_turns.dropna(subset=["ts"]).copy()
    df["uncached"] = (df["prompt_tokens"] - df["cached_tokens"]).clip(lower=0)
    df = df.sort_values(["mode", "instance_id", "ts"])
    df["turn"] = df.groupby(["mode", "instance_id"]).cumcount() + 1
    df["cum_uncached"] = df.groupby(["mode", "instance_id"])["uncached"].cumsum()
    rows = [
        {
            "mode": mode,
            "turn": turn,
            "median": s.median(),
            "q1": s.quantile(0.25),
            "q3": s.quantile(0.75),
            "n": len(s),
        }
        for (mode, turn), s in df.groupby(["mode", "turn"])["cum_uncached"]
    ]
    agg = pd.DataFrame(rows)
    if agg.empty:
        return agg
    kept = []
    for _, grp in agg.groupby("mode"):
        grp = grp.sort_values("turn")
        mask = (grp["n"] >= min_alive).cumprod().astype(bool)  # contiguous from turn 1
        kept.append(grp[mask])
    return pd.concat(kept) if kept else pd.DataFrame()


def _header_strip(
    run_dir: Path,
    manifest: dict[str, Any],
    status: dict[str, Any],
    memory: dict[str, Any],
    process: dict[str, Any],
    refresh_s: float,
) -> None:
    """One responsive line: run identity + per-mode progress on the left, health on
    the right. Designed to stay readable narrow (the phone glance)."""
    subset = manifest.get("subset", "?")
    modes_meta = manifest.get("modes") or {}
    parts = []
    for mode in ("baseline", "hay"):
        if not (run_dir / mode).exists():
            continue
        done = len(_completed_ids(run_dir, mode))
        total = (modes_meta.get(mode) or {}).get("instances_selected")
        parts.append(f"{mode} {done}/{total}" if total else f"{mode} {done}")
    progress = " · ".join(parts) if parts else "no instances yet"

    run_state = "running" if process.get("running") else ("done" if process else "—")
    manager = status.get("manager") or {}
    mgr = "ready" if (status.get("ok") and manager.get("resident")) else ("up" if status.get("ok") else "down")
    pressure = str(memory.get("pressure_label", "?"))
    free = _fmt_gb(memory.get("available_mb"))

    st.markdown(
        "<div style='display:flex;justify-content:space-between;flex-wrap:wrap;"
        "gap:0.3rem 1.5rem;align-items:baseline;margin:-0.3rem 0 1rem;font-size:0.9rem'>"
        f"<span><strong>{run_dir.name}</strong> &nbsp;·&nbsp; SWE-bench {subset} &nbsp;·&nbsp; {progress}</span>"
        f"<span style='opacity:0.65'>{run_state} · mgr {mgr} · {pressure} ({free} free) · {refresh_s:g}s</span>"
        "</div>",
        unsafe_allow_html=True,
    )


def _gate(validations: pd.DataFrame) -> bool | None:
    """Local official-test resolve parity — the kill switch. Returns parity."""
    b_res, b_tot = _resolve_counts(validations, "baseline")
    h_res, h_tot = _resolve_counts(validations, "hay")
    parity: bool | None = None
    if b_res is not None and h_res is not None:
        parity = h_res >= b_res

    c1, c2 = st.columns(2)
    c1.metric("Baseline resolved", f"{b_res}/{b_tot}" if b_res is not None else "—")
    c2.metric("Hay resolved", f"{h_res}/{h_tot}" if h_res is not None else "—")
    if parity is True:
        st.success("Parity held — Hay resolved at least as many as baseline. The savings below count.")
    elif parity is False:
        st.error("Parity FAILED — Hay resolved fewer than baseline. The savings below do not count as a win.")
    else:
        st.caption("Awaiting local official-test validation — the gate fills after `local-validation.json` is written.")
    return parity


def _pulse_smooth(telemetry: pd.DataFrame) -> None:
    """Cumulative characters Hay strips, live over wall-clock — the smooth 'it's
    working right now' signal. Exact char count (Hay-only), not a token estimate."""
    if telemetry.empty or "saved_chars" not in telemetry.columns or "ts" not in telemetry.columns:
        st.caption("No prunes recorded yet — fills live as Hay accepts prunes.")
        return
    df = telemetry.copy()
    if "accepted" in df.columns:
        df = df[df["accepted"] == True]  # noqa: E712
    df = df.dropna(subset=["ts"])
    if df.empty:
        st.caption("No accepted prunes yet.")
        return
    df = df.sort_values("ts")
    df["t"] = pd.to_datetime(pd.to_numeric(df["ts"], errors="coerce"), unit="s")
    df["cum_saved"] = pd.to_numeric(df["saved_chars"], errors="coerce").fillna(0).cumsum()
    area = (
        alt.Chart(df)
        .mark_area(opacity=0.18, color=HAY_COLOR, line={"color": HAY_COLOR, "strokeWidth": 2.5})
        .encode(
            x=alt.X("t:T", title="wall-clock", axis=alt.Axis(format="%H:%M:%S")),
            y=alt.Y("cum_saved:Q", title="cumulative chars stripped"),
            tooltip=[alt.Tooltip("t:T", title="time", format="%H:%M:%S"), alt.Tooltip("cum_saved:Q", title="chars", format=",")],
        )
        .properties(height=200)
    )
    st.altair_chart(_configure_chart_font(area), width="stretch")


def _pulse_stepped(running: pd.DataFrame) -> None:
    """Real billable tokens, stepping up as each instance finishes (wall-clock x).
    Flat between completions — the live real-token counterpart to the char pulse."""
    if running.empty:
        st.caption("Real-token totals appear as each instance finishes.")
        return
    line = (
        alt.Chart(running)
        .mark_line(strokeWidth=2.5, interpolate="step-after", point=alt.OverlayMarkDef(size=30))
        .encode(
            x=alt.X("ts:T", title="wall-clock", axis=alt.Axis(format="%H:%M:%S")),
            y=alt.Y("running_uncached:Q", title="cumulative billable tokens"),
            color=alt.Color("mode:N", scale=_mode_scale(running["mode"]), legend=alt.Legend(title=None, orient="top-left")),
            tooltip=["mode", "instance_id", alt.Tooltip("ts:T", title="finished", format="%H:%M:%S"), alt.Tooltip("running_uncached:Q", title="cumul. tokens", format=",")],
        )
        .properties(height=200)
    )
    st.altair_chart(_configure_chart_font(line), width="stretch")


def _win_sum(running: pd.DataFrame) -> None:
    """THE win: cumulative billable tokens summed across completed instances, baseline
    vs Hay, x = instances completed. The vertical gap is the total saving."""
    if running.empty:
        st.caption("Fills as instances complete — each finished task adds its full total.")
        return
    line = (
        alt.Chart(running)
        .mark_line(strokeWidth=2.5, point=alt.OverlayMarkDef(size=35))
        .encode(
            x=alt.X("completion_index:Q", title="instances completed", axis=alt.Axis(tickMinStep=1, format="d")),
            y=alt.Y("running_uncached:Q", title="cumulative billable tokens"),
            color=alt.Color("mode:N", scale=_mode_scale(running["mode"]), legend=alt.Legend(title=None, orient="top-left")),
            tooltip=[
                "mode",
                "instance_id",
                alt.Tooltip("completion_index:Q", title="#"),
                alt.Tooltip("running_uncached:Q", title="cumul. tokens", format=","),
                alt.Tooltip("total_uncached:Q", title="this instance", format=","),
            ],
        )
        .properties(height=300)
    )
    st.altair_chart(_configure_chart_font(line), width="stretch")


def _win_mechanism(band: pd.DataFrame) -> None:
    """The mechanism: median cumulative tokens per turn-index with an IQR band,
    cut where too few instances remain. Shows context staying lean per turn."""
    if band.empty:
        st.caption("Fills once instances log enough turns to compare.")
        return
    scale = _mode_scale(band["mode"])
    base = alt.Chart(band)
    area = base.mark_area(opacity=0.15).encode(
        x=alt.X("turn:Q", title="turn index"),
        y=alt.Y("q1:Q", title="cumulative tokens (median)"),
        y2="q3:Q",
        color=alt.Color("mode:N", scale=scale, legend=None),
    )
    line = base.mark_line(strokeWidth=2.5).encode(
        x="turn:Q",
        y="median:Q",
        color=alt.Color("mode:N", scale=scale, legend=alt.Legend(title=None, orient="top-left")),
        tooltip=["mode", "turn", alt.Tooltip("median:Q", format=","), alt.Tooltip("q1:Q", format=","), alt.Tooltip("q3:Q", format=","), alt.Tooltip("n:Q", title="instances")],
    )
    st.altair_chart(_configure_chart_font((area + line).properties(height=300)), width="stretch")


def _outcomes_chart(telemetry: pd.DataFrame) -> None:
    """The trust funnel: of every prune attempt, how many were accepted, skipped
    (no opportunity), or passthrough (memory-gated → that instance is suspect)."""
    if telemetry.empty:
        st.caption("No prune attempts recorded yet.")
        return
    df = telemetry.copy()
    df["outcome"] = df.apply(_outcome_bucket, axis=1)
    counts = df["outcome"].value_counts().reset_index()
    counts.columns = ["outcome", "count"]
    order = ["accepted", "expanded", "passthrough", "skipped"]
    palette = {
        "accepted": HAY_COLOR,
        "expanded": "#6f4eb2",
        "passthrough": WARN_COLOR,
        "skipped": MUTED_COLOR,
    }
    present = [o for o in order if o in set(counts["outcome"])]
    chart = (
        alt.Chart(counts)
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
        .encode(
            y=alt.Y("outcome:N", sort=present, title=None),
            x=alt.X("count:Q", title="Prune attempts"),
            color=alt.Color(
                "outcome:N",
                scale=alt.Scale(domain=present, range=[palette[o] for o in present]),
                legend=None,
            ),
            tooltip=["outcome", "count"],
        )
        .properties(height=150)
    )
    st.altair_chart(_configure_chart_font(chart), width="stretch")


def render(run_dir: Path, refresh_s: float) -> None:
    manifest = _read_json(run_dir / "manifest.json")
    status = _status_snapshot()
    memory = _memory_snapshot()
    process = _run_process(run_dir)
    modes = pd.DataFrame(_mode_rows(run_dir, manifest))
    validations = pd.DataFrame(_validation_rows(run_dir, manifest))
    telemetry = pd.DataFrame(_telemetry_rows(run_dir))
    trajectories = pd.DataFrame(_trajectory_rows(run_dir))
    reports = pd.DataFrame(_report_rows(run_dir))
    token_turns = pd.DataFrame(_token_turns(run_dir))
    summary = _instance_summary(token_turns, run_dir)
    running = _running_totals(summary)

    _header_strip(run_dir, manifest, status, memory, process, refresh_s)
    with st.expander("Processes and takeover", expanded=False):
        process_rows = _observed_process_rows()
        if process_rows:
            st.dataframe(pd.DataFrame(process_rows), width="stretch", hide_index=True)
        else:
            st.caption("No matching dashboard, benchmark, Hay session, or caffeinate processes found.")

        if process:
            st.markdown("**Selected run process metadata**")
            st.json(process, expanded=False)
            st.markdown("**Terminal takeover commands**")
            st.code(_takeover_commands(run_dir, process), language="bash")
        else:
            st.caption("The selected run was not launched by this dashboard, so no PID/PGID metadata is recorded.")

    st.subheader("The pulse — is it working, right now?")
    p1, p2 = st.columns(2)
    with p1:
        st.caption("Context Hay is stripping, live (exact characters).")
        _pulse_smooth(telemetry)
    with p2:
        st.caption("Real billable tokens, stepping up as instances finish.")
        _pulse_stepped(running)

    st.subheader("The verdict — did the thesis hold?")
    _gate(validations)

    st.markdown("**The win — total billable tokens, baseline vs Hay**")
    view = st.radio(
        "win view",
        ["Total saved", "Per-turn growth"],
        horizontal=True,
        label_visibility="collapsed",
        key=f"winview-{run_dir.name}",
    )
    if view == "Total saved":
        st.caption("Sum across completed instances (x = instances done). The gap between the lines is the saving.")
        _win_sum(running)
    else:
        st.caption("Median + IQR per turn (x = turn index), cut where fewer than 5 instances remain.")
        _win_mechanism(_turn_band(token_turns))

    baseline_row, hay_row = _first(modes, "baseline"), _first(modes, "hay")
    if baseline_row and hay_row:
        b_turns = float(baseline_row.get("turns_per_inst") or 0.0)
        h_turns = float(hay_row.get("turns_per_inst") or 0.0)
        flag = "no turn inflation" if h_turns <= b_turns + 0.5 else "warning — Hay spent more turns; savings may be re-fetch"
        st.caption(f"Turns / instance (the falsifier) — baseline {b_turns:.1f} · Hay {h_turns:.1f} ({h_turns - b_turns:+.1f}) — {flag}.")
    else:
        st.caption("Turns / instance (the falsifier) — fills once both modes have run.")

    with st.expander("Drill-down — per-instance, prune outcomes, raw data", expanded=False):
        if not summary.empty:
            st.markdown("**Per-instance token totals**")
            st.dataframe(summary.sort_values(["mode", "instance_id"]), width="stretch", hide_index=True)
        st.markdown("**Prune outcomes**")
        pass_n = int((telemetry.apply(_outcome_bucket, axis=1) == "passthrough").sum()) if not telemetry.empty else 0
        if pass_n:
            st.warning(f"{pass_n} prunes were passthrough (memory-gated) — discount those instances.")
        _outcomes_chart(telemetry)
        tabs = st.tabs(["Modes", "Validation", "Trajectories", "SBCLI", "Events"])
        with tabs[0]:
            st.dataframe(modes, width="stretch", hide_index=True)
        with tabs[1]:
            st.dataframe(validations, width="stretch", hide_index=True)
        with tabs[2]:
            st.dataframe(trajectories, width="stretch", hide_index=True)
        with tabs[3]:
            st.dataframe(reports, width="stretch", hide_index=True)
        with tabs[4]:
            st.dataframe(pd.DataFrame(status.get("events") or []), width="stretch", hide_index=True)



def main() -> None:
    st.set_page_config(page_title="Hay · SWE-bench", layout="wide")
    st.markdown(_FONT_CSS, unsafe_allow_html=True)
    st.title("Hay · SWE-bench")
    st.caption("Same agent, same instances — pruning off vs on.")

    with st.sidebar:
        runs_root = Path(st.text_input("Runs root", str(RUNS_ROOT))).expanduser()
        with st.expander("Start benchmark run", expanded=False):
            with st.form("start-benchmark"):
                run_id = st.text_input(
                    "Run ID",
                    "",
                    help="Optional. Leave blank to create a fresh name from the run settings.",
                )
                subset = st.selectbox("Subset", ["lite", "verified", "full"], index=0)
                split = st.text_input("Split", "test")
                slice_spec = st.text_input("Slice", "0:1")
                modes_selected = st.multiselect(
                    "Modes", ["baseline", "hay"], default=["baseline", "hay"]
                )
                backend = st.selectbox("Backend", ["modal", "docker"], index=0)
                workers = st.number_input("Workers", min_value=1, max_value=32, value=1)
                model = st.text_input("Model", "gpt-5.5")
                vibeproxy_url = st.text_input("VibeProxy URL", "http://localhost:8317/v1")
                submit = st.checkbox("Submit to SBCLI", value=False)
                hay_query = st.text_input("Force Hay query", "")
                min_free_mb = st.text_input(
                    "HAY_MIN_FREE_MB override",
                    "0",
                    help="Benchmark default disables the cold-load memory floor. Set 3072 to restore the manager's normal gate.",
                )
                launched = st.form_submit_button("Start run")

            if launched:
                if not modes_selected:
                    st.error("Choose at least one mode.")
                else:
                    ok, msg = _launch_run(
                        runs_root=runs_root,
                        run_id=run_id,
                        subset=subset,
                        split=split,
                        slice_spec=slice_spec,
                        workers=int(workers),
                        model=model,
                        backend=backend,
                        modes=",".join(modes_selected),
                        vibeproxy_url=vibeproxy_url,
                        submit=submit,
                        hay_query=hay_query,
                        min_free_mb=min_free_mb,
                    )
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

        runs = _runs(runs_root)
        names = [p.name for p in runs]
        # Default to the most-recent *active* run so a mid-batch glance lands on the
        # one that's still going; a key lets a manual pick stick across refreshes.
        default_idx = next(
            (i for i, p in enumerate(runs) if _run_process(p).get("running")), 0
        )
        selected = st.selectbox(
            "Run", names, index=default_idx if names else None, key="run_select"
        )
        if selected:
            selected_dir = runs[names.index(selected)]
            process = _run_process(selected_dir)
            if process:
                state = "running" if process.get("running") else "not running"
                st.caption(f"Process: {state} · pid {process.get('pid', '—')}")
                if process.get("running") and st.button("Stop selected run"):
                    st.warning(_stop_process(process))
                    st.rerun()
        live = st.toggle("Live", value=True)
        refresh_s = st.slider("Refresh seconds", min_value=1.0, max_value=10.0, value=2.0, step=0.5)

    if not runs:
        st.info("No benchmark runs yet.")
        return
    run_dir = runs[names.index(selected)] if selected else runs[0]

    if live and hasattr(st, "fragment"):

        @st.fragment(run_every=refresh_s)
        def live_body() -> None:
            render(run_dir, refresh_s)

        live_body()
    else:
        render(run_dir, refresh_s)


if __name__ == "__main__":
    main()
