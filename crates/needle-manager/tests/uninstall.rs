//! Uninstall scenarios against throwaway NEEDLE_HOME trees.

use serde_json::json;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const FAKE_WORKER: &str = r#"
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    op = request.get("op")
    response = {"id": request.get("id"), "ok": True, "status": "resident"}
    if op in ("unload", "exit", "status"):
        response["status"] = "cold"
    print(json.dumps(response), flush=True)
    if op == "exit":
        break
"#;

fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("nu-{}-{label}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create scratch");
    dir
}

fn fake_pi(dir: &Path) -> PathBuf {
    let script = dir.join("fake-pi.sh");
    std::fs::write(
        &script,
        r#"#!/bin/sh
echo "$@" >> "$PI_CALLS"
exit 0
"#,
    )
    .expect("write fake pi");
    let mut perms = std::fs::metadata(&script).unwrap().permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&script, perms).unwrap();
    script
}

fn write_config(home: &Path, pi_integrated: bool) {
    std::fs::create_dir_all(home).expect("create home");
    std::fs::write(
        home.join("config.json"),
        serde_json::to_string_pretty(&json!({
            "worker_python": home.join("python/venv/bin/python"),
            "model_dir": home.join("models/model"),
            "pi_integrated": pi_integrated,
            "created_at": "test",
            "needle_version": "test",
        }))
        .unwrap(),
    )
    .expect("write config");
}

fn populate_home(home: &Path, pi_integrated: bool) {
    write_config(home, pi_integrated);
    for path in ["runtime", "pi", "python/venv", "models/model", "logs"] {
        std::fs::create_dir_all(home.join(path)).expect("create home child");
    }
}

#[test]
fn uninstall_keeps_heavy_state_by_default() {
    let dir = scratch("keep");
    let home = dir.join("home");
    let calls = dir.join("pi-calls.txt");
    populate_home(&home, true);

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .args(["uninstall", "--yes"])
        .env("NEEDLE_HOME", &home)
        .env("NEEDLE_DEV_PI_BIN", fake_pi(&dir))
        .env("PI_CALLS", &calls)
        .output()
        .expect("run uninstall");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "stdout: {stdout}\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    assert!(!home.join("config.json").exists(), "config retained");
    assert!(!home.join("runtime").exists(), "runtime retained");
    assert!(!home.join("pi").exists(), "pi package retained");
    assert!(home.join("python/venv").exists(), "venv removed by default");
    assert!(
        home.join("models/model").exists(),
        "model removed by default"
    );
    assert!(home.join("logs").exists(), "logs removed by default");
    let pi_calls = std::fs::read_to_string(calls).expect("pi call log");
    assert!(
        pi_calls.contains("uninstall"),
        "pi not uninstalled: {pi_calls}"
    );
}

#[test]
fn purge_removes_the_entire_home() {
    let dir = scratch("purge");
    let home = dir.join("home");
    populate_home(&home, false);

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .args(["uninstall", "--purge", "--yes"])
        .env("NEEDLE_HOME", &home)
        .env("NEEDLE_DEV_PI_BIN", fake_pi(&dir))
        .env("PI_CALLS", dir.join("pi-calls.txt"))
        .output()
        .expect("run uninstall");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(output.status.success(), "stdout: {stdout}");
    assert!(!home.exists(), "home survived purge");
}

#[test]
fn uninstall_stops_a_running_daemon() {
    let dir = scratch("daemon");
    let home = dir.join("home");
    write_config(&home, false);
    let package = dir.join("pythonpath").join("needle_worker");
    std::fs::create_dir_all(&package).expect("create fake worker");
    std::fs::write(package.join("__init__.py"), "").expect("write init");
    std::fs::write(package.join("__main__.py"), FAKE_WORKER).expect("write worker");

    let mut daemon = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("daemon")
        .env("NEEDLE_HOME", &home)
        .env("PYTHONPATH", dir.join("pythonpath"))
        .env_remove("NEEDLE_PYTHON")
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("spawn daemon");
    wait_for_socket(&home.join("runtime/needle.sock"));

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .args(["uninstall", "--yes"])
        .env("NEEDLE_HOME", &home)
        .env("NEEDLE_DEV_PI_BIN", fake_pi(&dir))
        .env("PI_CALLS", dir.join("pi-calls.txt"))
        .output()
        .expect("run uninstall");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(output.status.success(), "stdout: {stdout}");
    assert!(wait_for_exit(&mut daemon, Duration::from_secs(5)));
}

fn wait_for_socket(socket: &Path) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if UnixStream::connect(socket).is_ok() {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("socket never came up: {}", socket.display());
}

fn wait_for_exit(child: &mut Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    let _ = child.wait();
    false
}
