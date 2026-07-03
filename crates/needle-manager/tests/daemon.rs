//! Scripted daemon scenarios over the real unix socket, with a fake
//! `needle_worker` package standing in for MLX.

use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const FAKE_WORKER: &str = r#"
import json
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    op = request.get("op")
    if op == "prune":
        text = request["text"]
        if "SLOW" in text:
            time.sleep(1.5)
        pruned = text.replace(" drop", "")
        response = {
            "id": request_id,
            "ok": True,
            "status": "resident",
            "backend": "fake-soft-lamr",
            "decision": "pruned" if pruned != text else "unchanged",
            "reason": "model" if pruned != text else "no-lines-removed",
            "text": pruned,
            "stats": {},
        }
    elif op == "load":
        response = {"id": request_id, "ok": True, "status": "resident", "backend": "fake-soft-lamr"}
    elif op in ("status", "unload", "exit"):
        response = {"id": request_id, "ok": True, "status": "cold"}
    else:
        response = {"id": request_id, "ok": False, "status": "failed", "error": "bad op"}
    print(json.dumps(response, separators=(",", ":")), flush=True)
    if op == "exit":
        break
"#;

struct DaemonUnderTest {
    child: Child,
    socket: PathBuf,
}

impl DaemonUnderTest {
    fn start(label: &str, lease_ttl_secs: u64) -> Self {
        let dir =
            std::env::temp_dir().join(format!("needle-daemon-test-{}-{label}", std::process::id()));
        let package = dir.join("pythonpath").join("needle_worker");
        std::fs::create_dir_all(&package).expect("create fake package");
        std::fs::write(package.join("__init__.py"), "").expect("write __init__");
        std::fs::write(package.join("__main__.py"), FAKE_WORKER).expect("write __main__");
        let socket = dir.join("needle.sock");

        let child = Command::new(env!("CARGO_BIN_EXE_needle"))
            .args([
                "daemon",
                "--socket",
                socket.to_str().expect("socket path utf8"),
                "--lease-ttl-secs",
                &lease_ttl_secs.to_string(),
            ])
            .env("PYTHONPATH", dir.join("pythonpath"))
            .env_remove("NEEDLE_PYTHON")
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("spawn daemon");

        let daemon = Self { child, socket };
        daemon.wait_for_socket();
        daemon
    }

    fn wait_for_socket(&self) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if UnixStream::connect(&self.socket).is_ok() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("daemon socket never came up at {}", self.socket.display());
    }

    fn connect(&self) -> Connection {
        Connection {
            stream: UnixStream::connect(&self.socket).expect("connect to daemon"),
        }
    }

    fn wait_for_exit(&mut self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if let Ok(Some(_)) = self.child.try_wait() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        false
    }
}

impl Drop for DaemonUnderTest {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct Connection {
    stream: UnixStream,
}

impl Connection {
    fn request(&mut self, request: Value) -> Value {
        let mut line = request.to_string();
        line.push('\n');
        self.stream
            .write_all(line.as_bytes())
            .expect("write request");
        let mut reader = BufReader::new(self.stream.try_clone().expect("clone stream"));
        let mut response = String::new();
        reader.read_line(&mut response).expect("read response");
        serde_json::from_str(&response).expect("response is JSON")
    }
}

#[test]
fn campfire_lifecycle_two_sessions() {
    let mut daemon = DaemonUnderTest::start("campfire", 90);
    let mut conn = daemon.connect();

    let enabled = conn.request(json!({"op": "enable", "session": "s1"}));
    assert_eq!(enabled["ok"], true, "enable: {enabled}");
    assert_eq!(enabled["backend_status"], "resident");

    let enabled_two = conn.request(json!({"op": "enable", "session": "s2"}));
    assert_eq!(enabled_two["ok"], true);

    let pruned = conn.request(json!({
        "op": "prune", "session": "s1",
        "text": "keep drop", "query": "keep relevant code",
    }));
    assert_eq!(pruned["ok"], true, "prune: {pruned}");
    assert_eq!(pruned["text"], "keep");
    assert_eq!(pruned["decision"], "pruned");
    assert_eq!(pruned["reason"], "model");

    let original = conn.request(json!({"op": "original", "session": "s1"}));
    assert_eq!(original["text"], "keep drop");

    let status = conn.request(json!({"op": "status"}));
    assert_eq!(status["mode"], "on");
    assert_eq!(status["backend_status"], "resident");
    assert_eq!(status["sessions"], 2);

    let first_out = conn.request(json!({"op": "disable", "session": "s1"}));
    assert_eq!(first_out["shutdown"], false);
    assert!(!daemon.wait_for_exit(Duration::from_millis(300)));

    let last_out = conn.request(json!({"op": "disable", "session": "s2"}));
    assert_eq!(last_out["shutdown"], true);
    assert!(
        daemon.wait_for_exit(Duration::from_secs(5)),
        "daemon should exit after the last lease drops"
    );
    assert!(!daemon.socket.exists(), "socket removed on shutdown");
}

#[test]
fn expired_lease_puts_the_campfire_out() {
    let mut daemon = DaemonUnderTest::start("ttl", 1);
    let mut conn = daemon.connect();

    let enabled = conn.request(json!({"op": "enable", "session": "s1"}));
    assert_eq!(enabled["ok"], true);

    // No heartbeats: the lease expires and the daemon exits on its own.
    assert!(
        daemon.wait_for_exit(Duration::from_secs(6)),
        "daemon should exit after lease TTL expiry"
    );
}

#[test]
fn control_ops_answer_during_slow_prune() {
    let daemon = DaemonUnderTest::start("liveness", 90);
    let mut conn = daemon.connect();
    assert_eq!(
        conn.request(json!({"op": "enable", "session": "s1"}))["ok"],
        true
    );

    let socket = daemon.socket.clone();
    let slow = std::thread::spawn(move || {
        let mut conn = Connection {
            stream: UnixStream::connect(&socket).expect("connect"),
        };
        conn.request(json!({
            "op": "prune", "session": "s1",
            "text": "SLOW keep drop", "query": "keep relevant code",
        }))
    });

    std::thread::sleep(Duration::from_millis(300));
    let started = Instant::now();
    let heartbeat = conn.request(json!({"op": "heartbeat", "session": "s1"}));
    let status = conn.request(json!({"op": "backend_status"}));
    let elapsed = started.elapsed();

    assert_eq!(heartbeat["ok"], true);
    assert_eq!(status["backend_status"], "resident");
    assert!(
        elapsed < Duration::from_millis(500),
        "control ops queued behind prune: {elapsed:?}"
    );

    let pruned = slow.join().expect("slow prune thread");
    assert_eq!(pruned["text"], "SLOW keep");
}

#[test]
fn socket_is_owner_only() {
    let daemon = DaemonUnderTest::start("perms", 90);

    let mode = std::fs::metadata(&daemon.socket)
        .expect("socket metadata")
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600, "socket mode: {mode:o}");

    let dir_mode = std::fs::metadata(daemon.socket.parent().expect("socket dir"))
        .expect("dir metadata")
        .permissions()
        .mode();
    assert_eq!(dir_mode & 0o777, 0o700, "dir mode: {dir_mode:o}");
}

#[test]
fn oversized_frames_are_rejected_and_daemon_survives() {
    let daemon = DaemonUnderTest::start("frames", 90);
    let mut stream = UnixStream::connect(&daemon.socket).expect("connect");

    // The daemon stops reading at the frame cap, answers with an error, and
    // closes the connection. Our blocked write then fails with EPIPE and the
    // reset may eat the response — both are the rejection working. What must
    // hold: the daemon itself survives and keeps serving new connections.
    let oversized = vec![b'x'; 17 * 1024 * 1024];
    let write_result = stream
        .write_all(&oversized)
        .and_then(|()| stream.write_all(b"\n"));
    let mut reader = BufReader::new(stream);
    let mut response = String::new();
    let read_result = reader.read_line(&mut response);
    let got_rejection = match (&write_result, &read_result) {
        (_, Ok(bytes)) if *bytes > 0 => {
            let response: Value = serde_json::from_str(&response).expect("JSON response");
            response["ok"] == false
                && response["error"]
                    .as_str()
                    .unwrap_or("")
                    .contains("too large")
        }
        _ => write_result.is_err() || read_result.is_err(),
    };
    assert!(got_rejection, "oversized frame was not rejected");

    let mut conn = daemon.connect();
    let status = conn.request(json!({"op": "status"}));
    assert_eq!(status["ok"], true, "daemon died after oversized frame");
}

#[test]
fn status_cli_reports_running_and_absent_daemons() {
    let daemon = DaemonUnderTest::start("status-cli", 90);
    let mut conn = daemon.connect();
    assert_eq!(
        conn.request(json!({"op": "enable", "session": "s1"}))["ok"],
        true
    );

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .args(["status", "--socket", daemon.socket.to_str().expect("utf8")])
        .output()
        .expect("run needle status");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(output.status.success());
    assert!(stdout.contains("on"), "stdout: {stdout}");
    assert!(stdout.contains("backend resident"), "stdout: {stdout}");
    assert!(stdout.contains("1 session"), "stdout: {stdout}");

    let missing = std::env::temp_dir().join("needle-status-cli-no-daemon.sock");
    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .args(["status", "--socket", missing.to_str().expect("utf8")])
        .output()
        .expect("run needle status");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(output.status.success());
    assert!(stdout.contains("no daemon running"), "stdout: {stdout}");
}
