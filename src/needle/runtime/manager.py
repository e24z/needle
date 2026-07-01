"""Machine-wide model residency manager.

One per machine, not per session. Like the per-project server it answers prune
requests, but its lifetime is decoupled from any single session: sessions hold
*leases* (acquire -> heartbeat... -> release) and the manager keeps the model
resident only while at least one lease is live.

The backend (the model) is built LAZILY on the first prune and DROPPED on idle
eviction, so an idle machine actually gives the memory back; it reloads on the
next prune. When the last lease drops, an idle clock starts; if it expires the
model is evicted. A session that crashes without releasing can't pin memory
forever -- a lease with no heartbeat for `lease_ttl` is reaped.

Memory safety (the model is ~1.5 GB; an 8 GB laptop can't always take it):
cold-loading the heavy model is GATED on real memory -- if the machine is under
critical pressure or below a free-memory floor, the prune passes through
unchanged instead of loading. And if pressure goes critical while resident, the
model is evicted even mid-lease. So Needle does nothing rather than tipping the box.

Because the manager OUTLIVES the sessions that use it (deliberately NO
getppid()==1 self-teardown), a lease carries the session's code version; on a
mismatch the manager steps aside (stops) and the session starts a fresh one.
"""

from __future__ import annotations

import os
import signal
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import events, naming, sysmem
from .backends import PrunerBackend, get_backend, is_code_pruner_backend_name
from .protocol import decode, encode


def _env(names: tuple[str, ...], default: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


MANAGER_CONFIG_ENVS = {
    "lease_ttl": ("NEEDLE_LEASE_TTL", "HAY_LEASE_TTL"),
    "idle_timeout": ("NEEDLE_IDLE_TIMEOUT", "HAY_IDLE_TIMEOUT"),
    "min_free_mb": ("NEEDLE_MIN_FREE_MB", "HAY_MIN_FREE_MB"),
    "max_prune_chars": ("NEEDLE_MAX_PRUNE_CHARS", "HAY_MAX_PRUNE_CHARS"),
    "max_request_bytes": ("NEEDLE_MANAGER_MAX_REQUEST_BYTES", "HAY_MANAGER_MAX_REQUEST_BYTES"),
    "request_read_timeout": ("NEEDLE_MANAGER_REQUEST_READ_TIMEOUT", "HAY_MANAGER_REQUEST_READ_TIMEOUT"),
    "max_connection_workers": ("NEEDLE_MANAGER_MAX_CONNECTION_WORKERS", "HAY_MANAGER_MAX_CONNECTION_WORKERS"),
    "mem_poll": ("NEEDLE_MEM_POLL", "HAY_MEM_POLL"),
}


LEASE_TTL = float(_env(MANAGER_CONFIG_ENVS["lease_ttl"], "90"))
IDLE_TIMEOUT = float(_env(MANAGER_CONFIG_ENVS["idle_timeout"], "300"))
MIN_FREE_MB = float(_env(MANAGER_CONFIG_ENVS["min_free_mb"], "3072"))
MAX_PRUNE_CHARS = int(_env(MANAGER_CONFIG_ENVS["max_prune_chars"], "1000000"))
MAX_REQUEST_BYTES = int(_env(MANAGER_CONFIG_ENVS["max_request_bytes"], "2500000"))
REQUEST_READ_TIMEOUT = float(_env(MANAGER_CONFIG_ENVS["request_read_timeout"], "2"))
MAX_CONNECTION_WORKERS = int(_env(MANAGER_CONFIG_ENVS["max_connection_workers"], "8"))
MEM_POLL = float(_env(MANAGER_CONFIG_ENVS["mem_poll"], "5"))

_STATS_LIST_LIMIT = 16
_BACKEND_STATS_KEYS = (
    "passthrough_reason",
    "input_chars",
    "output_chars",
    "saved_chars",
    "original_tokens",
    "original_code_tokens",
    "scored_code_tokens",
    "chunks",
    "batches",
    "batch_sizes",
    "max_batch_size",
    "max_batch_tokens",
    "max_length",
    "max_length_profile",
    "max_length_ratio",
    "batch_guardrail_splits",
    "batch_guardrail_singles_over_budget",
    "batch_retry_count",
    "batch_downgrade_reason",
    "batch_retry_from_sizes",
    "batch_error",
    "available_code_tokens",
    "chunk_overlap_tokens",
    "chunked",
    "batched",
    "real_tokens",
    "padded_tokens",
    "pad_tokens",
    "padding_waste_ratio",
    "truncated_code_tokens",
    "max_chunk_score",
    "tokenize_ms",
    "graph_build_ms",
    "forward_eval_ms",
    "decode_graph_ms",
    "host_sync_ms",
    "batch_total_ms",
    "line_aggregate_ms",
    "render_ms",
    "total_ms",
    "retained_hidden_states",
    "available_hidden_states",
    "mlx_active_mb_end",
    "mlx_cache_mb_end",
    "mlx_peak_mb_max",
    "profile_forced_eval",
)
_EVENT_STATS_KEYS = (
    "passthrough_reason",
    "chunks",
    "batches",
    "batch_sizes",
    "max_batch_size",
    "max_batch_tokens",
    "max_length",
    "max_length_profile",
    "batch_guardrail_splits",
    "batch_guardrail_singles_over_budget",
    "batch_retry_count",
    "batch_downgrade_reason",
    "padding_waste_ratio",
    "truncated_code_tokens",
    "forward_eval_ms",
    "host_sync_ms",
    "batch_total_ms",
    "total_ms",
    "chunked",
    "batched",
)
_IDENTITY_FIELDS = ("runtime_id", "tool_surface", "backend_id", "runtime_profile")


def _identity_value(value: object) -> str:
    return str(value) if value is not None else ""


def _identity_mismatches(
    requested: dict[str, object],
    actual: dict[str, object],
) -> dict[str, dict[str, str]]:
    mismatches: dict[str, dict[str, str]] = {}
    for field in _IDENTITY_FIELDS:
        req_value = _identity_value(requested.get(field))
        if not req_value:
            continue
        actual_value = _identity_value(actual.get(field))
        if req_value != actual_value:
            mismatches[field] = {"requested": req_value, "actual": actual_value}
    req_version = _identity_value(requested.get("version"))
    actual_version = _identity_value(actual.get("version"))
    if req_version and actual_version and req_version != actual_version:
        mismatches["version"] = {"requested": req_version, "actual": actual_version}
    return mismatches


def _bounded_stats_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 200 else f"{value[:197]}..."
    if isinstance(value, (list, tuple)):
        out: list[object] = []
        for item in value[:_STATS_LIST_LIMIT]:
            if item is None or isinstance(item, (bool, int, float, str)):
                out.append(_bounded_stats_value(item))
        return out
    return None


def _bounded_backend_stats(backend: PrunerBackend) -> dict[str, object]:
    try:
        raw = getattr(backend, "last_stats", None)
    except Exception:  # noqa: BLE001 - diagnostics must not break pruning.
        return {}
    if not isinstance(raw, dict):
        return {}
    stats: dict[str, object] = {}
    for key in _BACKEND_STATS_KEYS:
        if key not in raw:
            continue
        value = _bounded_stats_value(raw[key])
        if value is not None:
            stats[key] = value
    return stats


def _event_stats(stats: dict[str, object]) -> dict[str, object]:
    return {key: stats[key] for key in _EVENT_STATS_KEYS if key in stats}


class Manager:
    """Lease bookkeeping + lazy, memory-gated model lifecycle. Single-threaded:
    every method here runs on the serve loop's thread (MLX inference is
    thread-bound), so no locks are needed."""

    def __init__(
        self,
        backend_factory: Callable[[], PrunerBackend],
        *,
        version: str = "",
        stop_event: threading.Event | None = None,
        heavy: bool = False,
        lease_ttl: float = LEASE_TTL,
        idle_timeout: float = IDLE_TIMEOUT,
        min_free_mb: float = MIN_FREE_MB,
        max_prune_chars: int = MAX_PRUNE_CHARS,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        mem_poll: float = MEM_POLL,
        memstat: Callable[[], tuple[int, int]] = sysmem.memstat,
        emit: Callable[..., None] = events.emit,
        runtime_identity: dict[str, str] | None = None,
    ) -> None:
        self._make = backend_factory
        self._emit = emit  # structured event log (injected so tests can capture)
        self._backend: PrunerBackend | None = None  # built on first prune, dropped on evict
        self.version = version
        self._stop = stop_event
        self.heavy = heavy  # is the backend a big model whose cold load needs gating?
        self.lease_ttl = lease_ttl
        self.idle_timeout = idle_timeout
        self.min_free_mb = min_free_mb
        self.max_prune_chars = max_prune_chars
        self.max_request_bytes = max_request_bytes
        self.mem_poll = mem_poll
        self.memstat = memstat
        self._beats: dict[str, float] = {}      # session id -> last heartbeat (monotonic)
        self._empty_since: float | None = None  # when leases last fell to zero
        self._last_mem = -1e9                    # last memstat poll (monotonic)
        self._pressure = sysmem.PRESSURE_NORMAL
        self._avail = sysmem._UNKNOWN_AVAIL_MB
        self._last_prune: dict[str, object] | None = None
        self._runtime_identity = dict(runtime_identity or {})

    @property
    def resident(self) -> bool:
        """True iff the model is currently loaded in memory."""
        return self._backend is not None

    def _ensure_backend(self) -> PrunerBackend:
        if self._backend is None:
            self._backend = self._make()  # cold load (blocks this prune; serial by design)
            # name reveals a degraded fallback ("fake (code-pruner unavailable: ...)").
            self._emit("model_load", backend=getattr(self._backend, "name", "unknown"))
        return self._backend

    def _passthrough(self, text: str, reason: str) -> dict[str, Any]:
        """Return the text unchanged (Needle does nothing). saved==0, so the hook
        won't count it as a prune; the agent just gets the original."""
        self._emit("passthrough", reason=reason, chars=len(text))
        stats: dict[str, object] = {
            "passthrough_reason": reason,
            "input_chars": len(text),
            "output_chars": len(text),
            "saved_chars": 0,
        }
        self._last_prune = {"backend": f"passthrough:{reason}", **stats}
        return {
            "ok": True,
            "text": text,
            "original_len": len(text),
            "pruned_len": len(text),
            "backend": f"passthrough:{reason}",
            "stats": stats,
        }

    def identity(self) -> dict[str, str]:
        return {"version": self.version, **self._runtime_identity}

    def stale_lease_response(self, req: dict[str, Any]) -> dict[str, Any] | None:
        if req.get("op") != "lease":
            return None
        mismatches = _identity_mismatches(req, self.identity())
        if not mismatches:
            return None
        return self._stale_response(req, mismatches)

    def _stale_response(
        self,
        req: dict[str, Any],
        mismatches: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        self._emit(
            "stale_stepaside",
            mismatches=mismatches,
            their_version=req.get("version", ""),
            our_version=self.version,
        )
        if self._stop is not None:
            self._stop.set()
        return {
            "ok": False,
            "stale": True,
            "identity_mismatch": True,
            "version": self.version,
            "mismatches": mismatches,
        }

    # -- request handling -------------------------------------------------
    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op", "prune")
        now = time.monotonic()
        if op == "prune":
            text = req.get("text", "")
            if len(text) > self.max_prune_chars:
                return self._passthrough(text, "oversize")  # don't tokenize a monster
            if self._backend is None and self.heavy:
                # A cold load of the heavy model is the dangerous, ~GB event.
                # Refuse it when the machine can't take it.
                pressure, avail = self.memstat()
                self._pressure, self._avail, self._last_mem = pressure, avail, now
                if pressure >= sysmem.PRESSURE_CRITICAL or avail < self.min_free_mb:
                    return self._passthrough(text, "low-memory")
            backend = self._ensure_backend()
            pruned = backend.prune(text=text, query=req.get("query", ""))
            backend_name = getattr(backend, "name", "unknown")
            stats = _bounded_backend_stats(backend)
            stats.setdefault("input_chars", len(text))
            stats.setdefault("output_chars", len(pruned))
            stats.setdefault("saved_chars", max(0, len(text) - len(pruned)))
            self._last_prune = {"backend": backend_name, **stats}
            self._emit(
                "prune",
                backend=backend_name,
                original_len=len(text),
                pruned_len=len(pruned),
                saved_chars=max(0, len(text) - len(pruned)),
                **_event_stats(stats),
            )
            resp = {
                "ok": True,
                "text": pruned,
                "original_len": len(text),
                "pruned_len": len(pruned),
                "backend": backend_name,
            }
            if stats:
                resp["stats"] = stats
            return resp
        if op == "lease":
            mismatches = _identity_mismatches(req, self.identity())
            if mismatches:
                # Different runtime than the session requested: step aside for a fresh manager.
                return self._stale_response(req, mismatches)
            session = req.get("session", "")
            if session not in self._beats:
                self._emit("lease", session=session)  # new lease (re-leases stay quiet)
            self._beats[session] = now
            return {"ok": True}
        if op == "heartbeat":
            self._beats[req.get("session", "")] = now  # heartbeats are too frequent to log
            return {"ok": True}
        if op == "release":
            session = req.get("session", "")
            if self._beats.pop(session, None) is not None:
                self._emit("release", session=session)
            return {"ok": True}
        if op == "stats":
            return {
                "ok": True,
                "resident": self.resident,
                "sessions": len(self._beats),
                "backend": getattr(self._backend, "name", None),
                "version": self.version,
                "pressure": self._pressure,
                "available_mb": self._avail,
                "last_prune": dict(self._last_prune) if self._last_prune else None,
                **self.identity(),
            }
        if op == "stop":
            self._emit("stop")
            if self._stop is not None:
                self._stop.set()
            return {"ok": True}
        return {"ok": False, "error": f"unknown op: {op!r}"}

    # -- periodic maintenance (called between accepts) --------------------
    def maintain(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        # Reap leases whose session stopped heartbeating (crashed / killed).
        dead = [s for s, ts in self._beats.items() if now - ts > self.lease_ttl]
        for s in dead:
            del self._beats[s]
        # Throttled memory read (feeds pressure-eviction and the stats view).
        if now - self._last_mem >= self.mem_poll:
            self._last_mem = now
            self._pressure, self._avail = self.memstat()
        # Pressure eviction: free the model under critical pressure, even if leased.
        if self.heavy and self.resident and self._pressure >= sysmem.PRESSURE_CRITICAL:
            self._evict("pressure")
            return
        # Idle eviction: no live leases for idle_timeout -> drop the model.
        if self._beats:
            self._empty_since = None
        elif self._empty_since is None:
            self._empty_since = now
        elif self.resident and now - self._empty_since >= self.idle_timeout:
            self._evict("idle")

    def _evict(self, reason: str) -> None:
        backend, self._backend = self._backend, None  # drop the ref -> model memory freed
        self._emit("model_evict", reason=reason, backend=getattr(backend, "name", None))
        evict = getattr(backend, "evict", None)
        if callable(evict):
            evict()  # let the backend release device caches too
        self._empty_since = None  # evicted; don't fire again until reloaded


def _peer_uid(conn: socket.socket) -> int | None:
    if not hasattr(os, "getuid"):
        return None
    if hasattr(socket, "SO_PEERCRED"):
        try:
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return int(uid)
        except OSError:
            return None
    return None


def _read_request_frame(conn: socket.socket, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        chunk = conn.recv(min(8192, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            line, _sep, _rest = bytes(data).partition(b"\n")
            if len(line) > limit:
                raise ValueError(f"request too large: limit is {limit} bytes")
            return line.strip()
    if len(data) > limit:
        raise ValueError(f"request too large: limit is {limit} bytes")
    return bytes(data).strip()


def _manager_request(
    sock_path: Path,
    req: dict[str, object],
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    wire_req = dict(req)
    wire_req["token"] = naming.read_manager_token(sock_path)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    conn.connect(str(sock_path))
    try:
        conn.sendall(encode(wire_req))
        with conn.makefile("rb") as f:
            line = f.readline()
        if not line:
            raise ConnectionError("no response from manager")
        resp = decode(line)
        if not isinstance(resp, dict):
            raise ValueError("manager response must be a JSON object")
        return resp
    finally:
        conn.close()


def _assert_live_manager_compatible(
    sock_path: Path,
    runtime_identity: dict[str, str] | None,
    version: str,
) -> None:
    if not runtime_identity and not version:
        return
    try:
        stats = _manager_request(sock_path, {"op": "stats"}, timeout=1.0)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"live manager is missing its token at {naming.manager_token_path(sock_path)}; "
            "restart the runtime manager"
        ) from exc
    if not stats.get("ok"):
        raise RuntimeError(f"live manager refused identity check: {stats.get('error')}")
    requested = {"version": version, **(runtime_identity or {})}
    mismatches = _identity_mismatches(requested, stats)
    if mismatches:
        parts = [
            f"{field} requested={values['requested']!r} live={values['actual']!r}"
            for field, values in sorted(mismatches.items())
        ]
        raise RuntimeError("live manager identity mismatch: " + "; ".join(parts))


def _serve_conn(
    conn: socket.socket,
    mgr: Manager,
    *,
    auth_token: str,
    handle_lock: threading.Lock,
) -> None:
    conn.settimeout(REQUEST_READ_TIMEOUT)
    try:
        with conn:
            peer_uid = _peer_uid(conn)
            if peer_uid is not None and hasattr(os, "getuid") and peer_uid != os.getuid():
                conn.sendall(encode({"ok": False, "error": "unauthorized"}))
                return
            try:
                line = _read_request_frame(conn, mgr.max_request_bytes)
                if not line:
                    return
                req = decode(line)
                if not isinstance(req, dict):
                    raise ValueError("request must be a JSON object")
                if req.pop("token", None) != auth_token:
                    conn.sendall(encode({"ok": False, "error": "unauthorized"}))
                    return
                resp = mgr.stale_lease_response(req)
                if resp is not None:
                    conn.sendall(encode(resp))
                    return
                with handle_lock:
                    resp = mgr.handle(req)
            except Exception as exc:  # never let one bad request kill the manager
                resp = {"ok": False, "error": str(exc)}
            conn.sendall(encode(resp))
    except OSError:  # includes socket.timeout
        try:
            conn.close()
        except OSError:
            pass


def _serve_conn_with_release(
    conn: socket.socket,
    mgr: Manager,
    *,
    auth_token: str,
    handle_lock: threading.Lock,
    slots: threading.BoundedSemaphore,
) -> None:
    try:
        _serve_conn(conn, mgr, auth_token=auth_token, handle_lock=handle_lock)
    finally:
        slots.release()


def serve_manager(
    backend_factory: Callable[[], PrunerBackend] | None = None,
    socket_path: str | Path | None = None,
    ready_cb: Callable[[Path], None] | None = None,
    stop_event: threading.Event | None = None,
    *,
    version: str | None = None,
    heavy: bool | None = None,
    lease_ttl: float = LEASE_TTL,
    idle_timeout: float = IDLE_TIMEOUT,
    poll_interval: float = 0.5,
    max_connection_workers: int = MAX_CONNECTION_WORKERS,
    runtime_identity: dict[str, str] | None = None,
) -> None:
    """Bind the machine-wide socket and serve until stopped. First writer wins:
    if a manager is already live on the socket, defer to it and return."""
    explicit_backend = backend_factory is not None
    backend_factory = backend_factory or get_backend
    version = naming.code_version() if version is None else version
    # Only the heavy model needs memory gating; a free backend (fake/halve) does
    # not. An explicitly injected backend (tests, custom hosts) is presumed light
    # unless the caller passes heavy=True; only the default path reads the
    # configured backend id.
    if heavy is None:
        heavy = (not explicit_backend) and is_code_pruner_backend_name()
    sock_path = Path(socket_path) if socket_path else naming.manager_socket_path()
    naming.ensure_runtime_parent(sock_path.parent)

    if sock_path.exists():
        if not naming.socket_owner_is_current_user(sock_path):
            raise RuntimeError(f"manager socket is not owned by the current user: {sock_path}")
        if naming.socket_is_live(sock_path):
            _assert_live_manager_compatible(sock_path, runtime_identity, version)
            if ready_cb:  # another manager already owns this machine; defer to it
                ready_cb(sock_path)
            return
        sock_path.unlink()  # stale socket from an unclean exit

    auth_token = naming.get_or_create_manager_token(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        try:
            os.chmod(sock_path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        srv.close()
        if naming.socket_is_live(sock_path):
            _assert_live_manager_compatible(sock_path, runtime_identity, version)
            if ready_cb:  # lost the bind race to a manager that is actually live
                ready_cb(sock_path)
            return
        raise RuntimeError(f"could not bind manager socket at {sock_path}: {exc}") from exc
    srv.listen(16)
    srv.settimeout(poll_interval)  # wake periodically to maintain + check stop

    stop_event = stop_event or threading.Event()
    mgr = Manager(
        backend_factory,
        version=version,
        stop_event=stop_event,
        heavy=heavy,
        lease_ttl=lease_ttl,
        idle_timeout=idle_timeout,
        runtime_identity=runtime_identity,
    )
    handle_lock = threading.Lock()
    connection_slots = threading.BoundedSemaphore(max(1, max_connection_workers))

    if ready_cb:
        ready_cb(sock_path)

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    try:
        while not stop_event.is_set():
            if handle_lock.acquire(blocking=False):
                try:
                    mgr.maintain()
                finally:
                    handle_lock.release()
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            if not connection_slots.acquire(blocking=False):
                try:
                    conn.sendall(encode({"ok": False, "error": "manager busy"}))
                except OSError:
                    pass
                conn.close()
                continue
            threading.Thread(
                target=_serve_conn_with_release,
                args=(conn, mgr),
                kwargs={
                    "auth_token": auth_token,
                    "handle_lock": handle_lock,
                    "slots": connection_slots,
                },
                daemon=True,
            ).start()
    finally:
        srv.close()
        # Do not unlink the public socket path during shutdown. A replacement
        # manager may already have rebound the same pathname, and Unix-socket
        # inodes can be reused quickly enough to fool stat-based identity
        # checks. The next startup removes dead socket files before binding.
