//! End-to-end CLI tests against a fake `needle_worker` package.
//!
//! The fake shadows the real worker via PYTHONPATH (caller-provided entries
//! take precedence over the dev fallback), so these tests exercise the real
//! binary, the real child-process protocol, and no MLX.

use std::fs;
use std::path::PathBuf;
use std::process::Command;

const FAKE_WORKER: &str = r#"
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    op = request.get("op")
    if op == "prune":
        text = request["text"]
        pruned = text.replace(" drop", "")
        decision = "pruned" if pruned != text else "unchanged"
        response = {
            "id": request_id,
            "ok": True,
            "status": "resident",
            "backend": "fake-soft-lamr",
            "decision": decision,
            "reason": "model" if decision == "pruned" else "no-lines-removed",
            "text": pruned,
            "stats": {"input_chars": len(text), "output_chars": len(pruned)},
        }
    elif op in ("status", "load", "unload", "exit"):
        status = "resident" if op == "load" else "cold"
        response = {"id": request_id, "ok": True, "status": status}
    else:
        response = {"id": request_id, "ok": False, "status": "failed", "error": "bad op"}
    print(json.dumps(response, separators=(",", ":")), flush=True)
    if op == "exit":
        break
"#;

fn fake_worker_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("needle-cli-fake-{}-{label}", std::process::id()));
    let package = dir.join("needle_worker");
    fs::create_dir_all(&package).expect("create fake package dir");
    fs::write(package.join("__init__.py"), "").expect("write __init__");
    fs::write(package.join("__main__.py"), FAKE_WORKER).expect("write __main__");
    dir
}

fn needle_command(label: &str) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_needle"));
    command.env("PYTHONPATH", fake_worker_dir(label));
    command.env_remove("NEEDLE_PYTHON");
    command
}

#[test]
fn prune_file_prints_pruned_text_and_summary() {
    let input = std::env::temp_dir().join(format!("needle-cli-input-{}.txt", std::process::id()));
    fs::write(&input, "keep drop").expect("write input");

    let output = needle_command("file")
        .args(["prune", "--query", "keep relevant code"])
        .arg(&input)
        .output()
        .expect("run needle");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "stderr: {stderr}");
    assert_eq!(stdout, "keep\n");
    assert!(stderr.contains("pruned (model)"), "stderr: {stderr}");
    assert!(stderr.contains("9 -> 4 chars"), "stderr: {stderr}");
    assert!(
        stderr.contains("backend fake-soft-lamr"),
        "stderr: {stderr}"
    );
}

#[test]
fn prune_stdin_json_envelope() {
    use std::io::Write;

    let mut child = needle_command("json")
        .args(["prune", "--query", "keep relevant code", "--json"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("spawn needle");
    child
        .stdin
        .take()
        .expect("stdin")
        .write_all(b"keep drop")
        .expect("write stdin");
    let output = child.wait_with_output().expect("wait for needle");

    assert!(output.status.success());
    let envelope: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("stdout is JSON");
    assert_eq!(envelope["decision"], "pruned");
    assert_eq!(envelope["reason"], "model");
    assert_eq!(envelope["backend"], "fake-soft-lamr");
    assert_eq!(envelope["text"], "keep");
    assert_eq!(envelope["stats"]["input_chars"], 9);
}

#[test]
fn broken_worker_fails_loudly_with_nonzero_exit() {
    let output = needle_command("broken")
        .env("NEEDLE_PYTHON", "/nonexistent/python3")
        .args(["prune", "--query", "anything", "-"])
        .stdin(std::process::Stdio::null())
        .output()
        .expect("run needle");

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(!output.status.success());
    assert!(stderr.contains("needle: prune failed"), "stderr: {stderr}");
}
