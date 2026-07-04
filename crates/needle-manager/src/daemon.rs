//! The Needle daemon: NDJSON over a unix socket.
//!
//! Campfire lifecycle: the first `enable` lights it, the last `disable` (or
//! lease expiry) puts it out — the worker is unloaded, the socket removed,
//! and the process exits. Security posture matches the old Python manager:
//! same-UID peers only, 0600 socket under NEEDLE_HOME/runtime, bounded frames.

use crate::runtime::{NeedleMode, Runtime};
use serde::Deserialize;
use serde_json::{Value, json};
use signal_hook::consts::signal::{SIGINT, SIGTERM};
use signal_hook::iterator::Signals;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

/// Upper bound on one NDJSON request line. Large observations are expected;
/// unbounded frames are not.
pub const MAX_FRAME_BYTES: u64 = 16 * 1024 * 1024;

pub fn needle_home() -> PathBuf {
    if let Some(home) = std::env::var_os("NEEDLE_HOME") {
        return PathBuf::from(home);
    }
    let user_home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    if cfg!(target_os = "macos") {
        user_home
            .join("Library")
            .join("Application Support")
            .join("Needle")
    } else {
        user_home.join(".local").join("share").join("needle")
    }
}

pub fn default_socket_path() -> PathBuf {
    if let Some(path) = std::env::var_os("NEEDLE_SOCKET") {
        return PathBuf::from(path);
    }
    needle_home().join("runtime").join("needle.sock")
}

pub struct DaemonConfig {
    pub socket: PathBuf,
    pub lease_ttl: Duration,
}

impl Default for DaemonConfig {
    fn default() -> Self {
        Self {
            socket: default_socket_path(),
            lease_ttl: Duration::from_secs(90),
        }
    }
}

pub fn run(config: DaemonConfig) -> io::Result<()> {
    let socket_path = config.socket;
    let listener = bind(&socket_path)?;
    let runtime = Arc::new(Runtime::new());
    if let Err(error) = install_shutdown_signals(Arc::clone(&runtime), socket_path.clone()) {
        let _ = std::fs::remove_file(&socket_path);
        return Err(error);
    }

    let reaper_runtime = Arc::clone(&runtime);
    let reaper_socket = socket_path.clone();
    let ttl = config.lease_ttl;
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(ttl.min(Duration::from_secs(30)) / 3);
            reaper_runtime.reap_worker_exit();
            if matches!(reaper_runtime.reap_expired(ttl), Ok(true)) {
                shutdown_daemon(&reaper_runtime, &reaper_socket);
            }
        }
    });

    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        if !peer_is_same_uid(&stream) {
            continue;
        }
        let runtime = Arc::clone(&runtime);
        let socket_path = socket_path.clone();
        std::thread::spawn(move || handle_connection(stream, runtime, socket_path));
    }
    Ok(())
}

/// One-shot status query used by `needle status` and the Pi extension.
pub fn query(socket: &Path, request: &Value) -> io::Result<Value> {
    let mut stream = UnixStream::connect(socket)?;
    let mut line = serde_json::to_string(request).map_err(io::Error::other)?;
    line.push('\n');
    stream.write_all(line.as_bytes())?;
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    reader.read_line(&mut response)?;
    serde_json::from_str(&response).map_err(io::Error::other)
}

fn bind(socket_path: &Path) -> io::Result<UnixListener> {
    if let Some(dir) = socket_path.parent() {
        // Lock down directories we create (the default NEEDLE_HOME/runtime),
        // but never chmod a pre-existing dir: an explicit --socket may point
        // into /tmp or another shared location we do not own. The socket
        // file's own 0600 mode and the peer-UID check still apply there.
        if !dir.exists() {
            std::fs::create_dir_all(dir)?;
            std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
        }
    }
    if socket_path.exists() {
        // Only replace the socket if nothing answers on it: a live daemon's
        // socket must never be unlinked out from under it.
        match UnixStream::connect(socket_path) {
            Ok(_) => {
                return Err(io::Error::new(
                    io::ErrorKind::AddrInUse,
                    format!(
                        "a needle daemon is already running on {}",
                        socket_path.display()
                    ),
                ));
            }
            Err(_) => std::fs::remove_file(socket_path)?,
        }
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o600))?;
    Ok(listener)
}

fn shutdown_daemon(runtime: &Runtime, socket_path: &Path) -> ! {
    runtime.shutdown();
    let _ = std::fs::remove_file(socket_path);
    std::process::exit(0)
}

fn install_shutdown_signals(runtime: Arc<Runtime>, socket_path: PathBuf) -> io::Result<()> {
    let mut signals = Signals::new([SIGTERM, SIGINT])?;
    std::thread::spawn(move || {
        if signals.forever().next().is_some() {
            shutdown_daemon(&runtime, &socket_path);
        }
    });
    Ok(())
}

fn handle_connection(stream: UnixStream, runtime: Arc<Runtime>, socket_path: PathBuf) {
    let Ok(read_half) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(read_half);
    let mut writer = stream;
    loop {
        match read_frame(&mut reader) {
            Ok(None) => return,
            Ok(Some(line)) => {
                let (response, shutdown) = dispatch(&line, &runtime);
                if write_json(&mut writer, &response).is_err() {
                    return;
                }
                if shutdown {
                    shutdown_daemon(&runtime, &socket_path);
                }
            }
            Err(FrameError::TooLarge) => {
                let _ = write_json(
                    &mut writer,
                    &json!({"ok": false, "error": "frame too large"}),
                );
                return;
            }
            Err(FrameError::Io) => return,
        }
    }
}

enum FrameError {
    TooLarge,
    Io,
}

fn read_frame(reader: &mut BufReader<UnixStream>) -> Result<Option<String>, FrameError> {
    let mut buf = Vec::new();
    let bytes = reader
        .take(MAX_FRAME_BYTES + 1)
        .read_until(b'\n', &mut buf)
        .map_err(|_| FrameError::Io)?;
    if bytes == 0 {
        return Ok(None);
    }
    if buf.len() as u64 > MAX_FRAME_BYTES {
        return Err(FrameError::TooLarge);
    }
    Ok(Some(String::from_utf8_lossy(&buf).into_owned()))
}

fn write_json(writer: &mut UnixStream, value: &Value) -> io::Result<()> {
    let mut line = serde_json::to_string(value).map_err(io::Error::other)?;
    line.push('\n');
    writer.write_all(line.as_bytes())
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Request {
    Enable {
        session: String,
    },
    Disable {
        session: String,
    },
    Heartbeat {
        session: String,
    },
    Prune {
        session: String,
        text: String,
        query: String,
    },
    Mode,
    BackendStatus,
    Status,
    Original {
        session: String,
    },
    Shutdown,
}

fn dispatch(line: &str, runtime: &Runtime) -> (Value, bool) {
    let request: Request = match serde_json::from_str(line) {
        Ok(request) => request,
        Err(error) => {
            return (
                json!({"ok": false, "error": format!("invalid request: {error}")}),
                false,
            );
        }
    };

    match request {
        Request::Enable { session } => match runtime.enable(&session) {
            Ok(status) => (
                json!({"ok": true, "backend_status": status.as_str()}),
                false,
            ),
            Err(error) => (
                json!({
                    "ok": false,
                    "error": error.to_string(),
                    "backend_status": runtime.backend_status().as_str(),
                }),
                false,
            ),
        },
        Request::Disable { session } => match runtime.disable(&session) {
            Ok(last) => (json!({"ok": true, "shutdown": last}), last),
            Err(error) => (
                json!({"ok": false, "error": error.to_string(), "shutdown": true}),
                // A failed unload still means the last lease is gone: put the
                // campfire out rather than leaving a session-less daemon.
                true,
            ),
        },
        Request::Heartbeat { session } => {
            if runtime.heartbeat(&session) {
                (json!({"ok": true}), false)
            } else {
                (
                    json!({"ok": false, "error": format!("session {session} has no lease")}),
                    false,
                )
            }
        }
        Request::Prune {
            session,
            text,
            query,
        } => match runtime.prune(&session, &text, &query) {
            Ok(result) => (
                json!({
                    "ok": true,
                    "backend_status": runtime.backend_status().as_str(),
                    "decision": match result.decision {
                        crate::protocol::PruneDecision::Pruned => "pruned",
                        crate::protocol::PruneDecision::Unchanged => "unchanged",
                    },
                    "reason": result.reason,
                    "backend": result.backend,
                    "text": result.text,
                    "stats": result.stats,
                }),
                false,
            ),
            Err(error) => (
                json!({
                    "ok": false,
                    "error": error.to_string(),
                    "backend_status": runtime.backend_status().as_str(),
                }),
                false,
            ),
        },
        Request::Mode => (json!({"ok": true, "mode": mode_str(runtime.mode())}), false),
        Request::BackendStatus => (
            json!({"ok": true, "backend_status": runtime.backend_status().as_str()}),
            false,
        ),
        Request::Status => (
            json!({
                "ok": true,
                "mode": mode_str(runtime.mode()),
                "backend_status": runtime.backend_status().as_str(),
                "sessions": runtime.session_count(),
            }),
            false,
        ),
        Request::Original { session } => match runtime.last_original(&session) {
            Some(text) => (json!({"ok": true, "text": text}), false),
            None => (
                json!({"ok": false, "error": "no original cached for session"}),
                false,
            ),
        },
        Request::Shutdown => (json!({"ok": true, "shutdown": true}), true),
    }
}

fn mode_str(mode: NeedleMode) -> &'static str {
    match mode {
        NeedleMode::Off => "off",
        NeedleMode::On => "on",
    }
}

fn peer_is_same_uid(stream: &UnixStream) -> bool {
    #[cfg(target_os = "macos")]
    {
        return peer_uid(stream)
            .map(|uid| uid == unsafe { libc::getuid() })
            .unwrap_or(false);
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = stream;
        false
    }
}

#[cfg(target_os = "macos")]
fn peer_uid(stream: &UnixStream) -> io::Result<libc::uid_t> {
    use std::os::fd::AsRawFd;
    let mut uid: libc::uid_t = 0;
    let mut gid: libc::gid_t = 0;
    let rc = unsafe { libc::getpeereid(stream.as_raw_fd(), &mut uid, &mut gid) };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(uid)
}
