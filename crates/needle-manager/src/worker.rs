use crate::backend::BackendStatus;
use crate::protocol::{PruneDecision, PruneResult, WorkerError, WorkerRequest, WorkerResponse};
use std::ffi::OsString;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

const WORKER_OP_TIMEOUT_ENV: &str = "NEEDLE_WORKER_OP_TIMEOUT_SECS";
const DEFAULT_WORKER_OP_TIMEOUT_SECS: u64 = 600;

#[derive(Clone, Debug)]
pub(crate) struct WorkerCommand {
    program: OsString,
    args: Vec<OsString>,
    envs: Vec<(OsString, OsString)>,
    enforce_cold_load_memory_gate: bool,
}

impl WorkerCommand {
    pub(crate) fn new(program: impl Into<OsString>) -> Self {
        Self {
            program: program.into(),
            args: Vec::new(),
            envs: Vec::new(),
            enforce_cold_load_memory_gate: false,
        }
    }

    pub(crate) fn arg(mut self, arg: impl Into<OsString>) -> Self {
        self.args.push(arg.into());
        self
    }

    pub(crate) fn env(mut self, key: impl Into<OsString>, value: impl Into<OsString>) -> Self {
        self.envs.push((key.into(), value.into()));
        self
    }

    fn with_cold_load_memory_gate(mut self) -> Self {
        self.enforce_cold_load_memory_gate = true;
        self
    }

    fn needle_worker() -> Self {
        let config = crate::config::load().unwrap_or_default();
        // Env overrides beat the installed config; the dev checkout fallback
        // only applies when neither names a provisioned interpreter.
        let configured_python = std::env::var_os("NEEDLE_PYTHON").or_else(|| {
            config
                .worker_python
                .as_ref()
                .filter(|path| path.exists())
                .map(|path| path.as_os_str().to_os_string())
        });
        let dev_fallback = configured_python.is_none();
        let python = configured_python.unwrap_or_else(|| OsString::from("python3"));
        let mut command = Self::new(python).arg("-m").arg("needle_worker");
        if dev_fallback {
            if let Some(python_path) = dev_python_path() {
                command = command.env("PYTHONPATH", python_path);
            }
        }
        if std::env::var_os("NEEDLE_MODEL_DIR").is_none() {
            if let Some(model_dir) = &config.model_dir {
                command = command.env("NEEDLE_MODEL_DIR", model_dir.as_os_str());
            }
        }
        command.with_cold_load_memory_gate()
    }

    fn spawn(&self) -> Result<WorkerProcess, WorkerError> {
        let mut command = Command::new(&self.program);
        command
            .args(&self.args)
            .envs(self.envs.iter().cloned())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());

        let mut child = command.spawn().map_err(WorkerError::Spawn)?;
        let stdin = child
            .stdin
            .take()
            .ok_or(WorkerError::MissingPipe("stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or(WorkerError::MissingPipe("stdout"))?;
        let (responses, reader) = spawn_stdout_reader(stdout);
        Ok(WorkerProcess {
            child,
            stdin,
            responses,
            reader: Some(reader),
        })
    }
}

fn dev_python_path() -> Option<OsString> {
    let repo_python = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()?
        .parent()?
        .join("python");
    if !repo_python.is_dir() {
        return None;
    }

    // Caller-provided PYTHONPATH takes precedence; the repo checkout is only
    // the development fallback for finding `needle_worker`.
    let mut paths = Vec::new();
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        paths.extend(std::env::split_paths(&existing));
    }
    paths.push(repo_python);
    std::env::join_paths(paths).ok()
}

struct WorkerProcess {
    child: Child,
    stdin: ChildStdin,
    responses: Receiver<WorkerRead>,
    reader: Option<JoinHandle<()>>,
}

enum WorkerRead {
    Line(String),
    Eof,
    Io(std::io::Error),
}

fn spawn_stdout_reader(stdout: ChildStdout) -> (Receiver<WorkerRead>, JoinHandle<()>) {
    let (tx, rx) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut stdout = BufReader::new(stdout);
        loop {
            let mut line = String::new();
            match stdout.read_line(&mut line) {
                Ok(0) => {
                    let _ = tx.send(WorkerRead::Eof);
                    break;
                }
                Ok(_) => {
                    if tx.send(WorkerRead::Line(line)).is_err() {
                        break;
                    }
                }
                Err(error) => {
                    let _ = tx.send(WorkerRead::Io(error));
                    break;
                }
            }
        }
    });
    (rx, reader)
}

impl WorkerProcess {
    fn kill_and_wait(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        self.join_reader();
    }

    fn wait_and_join(&mut self) {
        let _ = self.child.wait();
        self.join_reader();
    }

    fn join_reader(&mut self) {
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

pub(crate) struct Worker {
    status: BackendStatus,
    next_id: u64,
    command: WorkerCommand,
    response_timeout: Duration,
    process: Option<WorkerProcess>,
}

impl Worker {
    pub(crate) fn new() -> Self {
        Self::with_command(WorkerCommand::needle_worker())
    }

    pub(crate) fn with_command(command: WorkerCommand) -> Self {
        Self {
            status: BackendStatus::Cold,
            next_id: 1,
            command,
            response_timeout: worker_response_timeout(),
            process: None,
        }
    }

    #[cfg(test)]
    fn with_command_and_response_timeout(
        command: WorkerCommand,
        response_timeout: Duration,
    ) -> Self {
        Self {
            status: BackendStatus::Cold,
            next_id: 1,
            command,
            response_timeout,
            process: None,
        }
    }

    pub(crate) fn status(&self) -> BackendStatus {
        self.status
    }

    pub(crate) fn load(&mut self) -> Result<(), WorkerError> {
        let id = self.next_request_id();
        if self.status != BackendStatus::Resident {
            self.status = BackendStatus::Loading;
        }
        let response = self.send(WorkerRequest::Load { id })?;
        self.apply_status(&response);
        if response.ok {
            Ok(())
        } else {
            Err(response.worker_error())
        }
    }

    pub(crate) fn prune(&mut self, text: &str, query: &str) -> Result<PruneResult, WorkerError> {
        let id = self.next_request_id();
        if self.status != BackendStatus::Resident {
            self.status = BackendStatus::Loading;
        }
        let response = self.send(WorkerRequest::Prune {
            id,
            text: text.to_string(),
            query: query.to_string(),
        })?;
        self.apply_status(&response);
        if !response.ok {
            return Err(response.worker_error());
        }
        let pruned_text = response
            .text
            .ok_or_else(|| WorkerError::Protocol("prune response missing text".to_string()))?;
        let decision = response.decision.unwrap_or_else(|| {
            if pruned_text == text {
                PruneDecision::Unchanged
            } else {
                PruneDecision::Pruned
            }
        });
        Ok(PruneResult {
            text: pruned_text,
            decision,
            reason: response.reason,
            backend: response.backend,
            stats: response.stats,
        })
    }

    pub(crate) fn unload(&mut self) -> Result<(), WorkerError> {
        if self.process.is_none() {
            self.status = BackendStatus::Cold;
            return Ok(());
        }
        if self.status == BackendStatus::Cold {
            return Ok(());
        }
        let id = self.next_request_id();
        let response = self.send(WorkerRequest::Unload { id })?;
        self.apply_status(&response);
        if response.ok {
            Ok(())
        } else {
            Err(response.worker_error())
        }
    }

    /// Exit the child protocol-first, then reap it. For shutdown paths that
    /// bypass Drop (e.g. the daemon calling std::process::exit).
    pub(crate) fn stop(&mut self) {
        let _ = self.shutdown();
        if let Some(mut process) = self.process.take() {
            process.kill_and_wait();
        }
        self.status = BackendStatus::Cold;
    }

    pub(crate) fn reap_exited_child(&mut self) -> bool {
        let Some(process) = self.process.as_mut() else {
            return false;
        };
        match process.child.try_wait() {
            Ok(Some(_)) => {
                if let Some(mut process) = self.process.take() {
                    process.join_reader();
                }
                self.status = BackendStatus::Failed;
                true
            }
            Ok(None) => false,
            Err(_) => {
                self.status = BackendStatus::Failed;
                true
            }
        }
    }

    fn shutdown(&mut self) -> Result<(), WorkerError> {
        if self.process.is_none() {
            self.status = BackendStatus::Cold;
            return Ok(());
        }
        let id = self.next_request_id();
        let response = self.send(WorkerRequest::Exit { id })?;
        self.apply_status(&response);
        if let Some(mut process) = self.process.take() {
            process.wait_and_join();
        }
        if response.ok {
            Ok(())
        } else {
            Err(response.worker_error())
        }
    }

    fn next_request_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        id
    }

    fn process(&mut self) -> Result<&mut WorkerProcess, WorkerError> {
        if self.process.is_none() {
            if self.command.enforce_cold_load_memory_gate {
                if let Some(refusal) = crate::memory::cold_load_refusal() {
                    return Err(WorkerError::MemoryPressure(refusal.message()));
                }
            }
            self.process = Some(self.command.spawn()?);
        }
        Ok(self.process.as_mut().expect("process exists after spawn"))
    }

    /// Any transport failure — spawn, I/O, malformed or mismatched response —
    /// leaves the backend visibly Failed, never stuck at Loading.
    fn send(&mut self, request: WorkerRequest) -> Result<WorkerResponse, WorkerError> {
        let result = self.send_inner(request);
        if result.is_err() {
            self.status = BackendStatus::Failed;
        }
        result
    }

    fn send_inner(&mut self, request: WorkerRequest) -> Result<WorkerResponse, WorkerError> {
        let expected_id = request.id();
        let timeout = self.response_timeout;
        let read = {
            let process = self.process()?;
            serde_json::to_writer(&mut process.stdin, &request).map_err(WorkerError::Json)?;
            process.stdin.write_all(b"\n").map_err(WorkerError::Io)?;
            process.stdin.flush().map_err(WorkerError::Io)?;
            process.responses.recv_timeout(timeout)
        };

        let line = match read {
            Ok(WorkerRead::Line(line)) => line,
            Ok(WorkerRead::Eof) => {
                self.kill_process();
                return Err(WorkerError::Protocol(
                    "worker exited before sending a response".to_string(),
                ));
            }
            Ok(WorkerRead::Io(error)) => {
                self.kill_process();
                return Err(WorkerError::Io(error));
            }
            Err(RecvTimeoutError::Timeout) => {
                self.kill_process();
                return Err(WorkerError::Timeout(timeout));
            }
            Err(RecvTimeoutError::Disconnected) => {
                self.kill_process();
                return Err(WorkerError::Protocol(
                    "worker stdout reader stopped before sending a response".to_string(),
                ));
            }
        };

        let response: WorkerResponse = serde_json::from_str(&line).map_err(WorkerError::Json)?;
        if response.id != Some(expected_id) {
            return Err(WorkerError::Protocol(format!(
                "response id {:?} did not match request id {expected_id}",
                response.id
            )));
        }
        Ok(response)
    }

    fn kill_process(&mut self) {
        if let Some(mut process) = self.process.take() {
            process.kill_and_wait();
        }
    }

    fn apply_status(&mut self, response: &WorkerResponse) {
        if let Some(status) = response.status {
            self.status = BackendStatus::from(status);
        } else if !response.ok {
            self.status = BackendStatus::Failed;
        }
    }
}

fn worker_response_timeout() -> Duration {
    std::env::var(WORKER_OP_TIMEOUT_ENV)
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|seconds| *seconds > 0)
        .map(Duration::from_secs)
        .unwrap_or_else(|| Duration::from_secs(DEFAULT_WORKER_OP_TIMEOUT_SECS))
}

impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.shutdown();
        if let Some(mut process) = self.process.take() {
            process.kill_and_wait();
        }
    }
}

/// A protocol-faithful fake worker for tests: prunes by dropping " drop",
/// sleeps when the text contains "SLOW" so tests can hold the worker busy.
#[cfg(test)]
pub(crate) fn fake_worker_command(label: &str) -> WorkerCommand {
    let script = r#"
import json
import sys
import time

loaded = False

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    op = request.get("op")
    if op == "status":
        status = "resident" if loaded else "cold"
        response = {"id": request_id, "ok": True, "status": status}
    elif op == "load":
        loaded = True
        response = {"id": request_id, "ok": True, "status": "resident", "backend": "fake"}
    elif op == "prune":
        loaded = True
        text = request["text"]
        if "SLOW" in text:
            time.sleep(1.5)
        pruned = text.replace(" drop", "")
        response = {
            "id": request_id,
            "ok": True,
            "status": "resident",
            "backend": "fake",
            "decision": "pruned" if pruned != text else "unchanged",
            "reason": "model" if pruned != text else "no-lines-removed",
            "text": pruned,
            "stats": {"saved_chars": len(text) - len(pruned)},
        }
    elif op == "unload":
        loaded = False
        response = {"id": request_id, "ok": True, "status": "cold"}
    elif op == "exit":
        loaded = False
        response = {"id": request_id, "ok": True, "status": "cold"}
        print(json.dumps(response, separators=(",", ":")), flush=True)
        break
    else:
        response = {"id": request_id, "ok": False, "status": "failed", "error": "bad op"}
    print(json.dumps(response, separators=(",", ":")), flush=True)
"#;
    python_script_command("fake", label, script)
}

#[cfg(test)]
pub(crate) fn hanging_worker_command(label: &str) -> WorkerCommand {
    let script = r#"
import json
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    if request.get("op") == "load":
        time.sleep(60)
    elif request.get("op") == "exit":
        break
"#;
    python_script_command("hanging", label, script)
}

#[cfg(test)]
pub(crate) fn self_exiting_worker_command(label: &str) -> WorkerCommand {
    let script = r#"
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    op = request.get("op")
    if op == "load":
        response = {"id": request_id, "ok": True, "status": "resident", "backend": "fake"}
        print(json.dumps(response, separators=(",", ":")), flush=True)
        sys.exit(0)
    elif op == "exit":
        response = {"id": request_id, "ok": True, "status": "cold"}
        print(json.dumps(response, separators=(",", ":")), flush=True)
        break
    else:
        response = {"id": request_id, "ok": False, "status": "failed", "error": "unexpected op"}
        print(json.dumps(response, separators=(",", ":")), flush=True)
"#;
    python_script_command("self-exiting", label, script)
}

#[cfg(test)]
pub(crate) fn counting_unload_worker_command(
    label: &str,
    log_path: &std::path::Path,
) -> WorkerCommand {
    let script = r#"
import json
import os
import sys
import time

loaded = False

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    op = request.get("op")
    if op == "load":
        loaded = True
        response = {"id": request_id, "ok": True, "status": "resident", "backend": "fake"}
    elif op == "unload":
        with open(os.environ["NEEDLE_UNLOAD_LOG"], "a", encoding="utf-8") as handle:
            handle.write("unload\n")
        time.sleep(0.2)
        loaded = False
        response = {"id": request_id, "ok": True, "status": "cold"}
    elif op == "exit":
        loaded = False
        response = {"id": request_id, "ok": True, "status": "cold"}
        print(json.dumps(response, separators=(",", ":")), flush=True)
        break
    else:
        status = "resident" if loaded else "cold"
        response = {"id": request_id, "ok": True, "status": status}
    print(json.dumps(response, separators=(",", ":")), flush=True)
"#;
    python_script_command("counting-unload", label, script)
        .env("NEEDLE_UNLOAD_LOG", log_path.as_os_str())
}

#[cfg(test)]
fn python_script_command(kind: &str, label: &str, script: &str) -> WorkerCommand {
    let path = std::env::temp_dir().join(format!(
        "needle-{kind}-worker-{}-{label}.py",
        std::process::id()
    ));
    std::fs::write(&path, script).expect("write fake worker script");
    WorkerCommand::new(std::env::var_os("NEEDLE_PYTHON").unwrap_or_else(|| "python3".into()))
        .arg(path.into_os_string())
}

#[cfg(test)]
mod tests {
    use super::{Worker, fake_worker_command, hanging_worker_command};
    use crate::backend::BackendStatus;
    use crate::protocol::PruneDecision;
    use std::time::{Duration, Instant};

    #[test]
    fn prune_starts_worker_and_returns_result() {
        let mut worker = Worker::with_command(fake_worker_command("worker-prune"));

        let result = worker
            .prune("keep drop", "keep relevant code")
            .expect("prune succeeds");

        assert_eq!(worker.status(), BackendStatus::Resident);
        assert_eq!(result.text, "keep");
        assert_eq!(result.decision, PruneDecision::Pruned);
        assert_eq!(result.backend.as_deref(), Some("fake"));
        assert_eq!(
            result
                .stats
                .get("saved_chars")
                .and_then(serde_json::Value::as_i64),
            Some(5)
        );
    }

    #[test]
    fn unload_moves_worker_back_to_cold() {
        let mut worker = Worker::with_command(fake_worker_command("worker-unload"));

        worker.load().expect("load succeeds");
        assert_eq!(worker.status(), BackendStatus::Resident);

        worker.unload().expect("unload succeeds");
        assert_eq!(worker.status(), BackendStatus::Cold);
    }

    #[test]
    fn load_timeout_kills_child_and_marks_failed() {
        let mut worker = Worker::with_command_and_response_timeout(
            hanging_worker_command("worker-timeout"),
            Duration::from_millis(100),
        );

        let started = Instant::now();
        let error = worker.load().expect_err("load times out");

        assert!(
            started.elapsed() < Duration::from_secs(2),
            "load ignored response timeout"
        );
        assert!(error.to_string().contains("timed out"), "{error}");
        assert_eq!(worker.status(), BackendStatus::Failed);
    }
}
