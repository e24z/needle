use crate::backend::BackendStatus;
use crate::protocol::{PruneDecision, PruneResult, WorkerError, WorkerRequest, WorkerResponse};
use std::ffi::OsString;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

#[derive(Clone, Debug)]
pub(crate) struct WorkerCommand {
    program: OsString,
    args: Vec<OsString>,
    envs: Vec<(OsString, OsString)>,
}

impl WorkerCommand {
    pub(crate) fn new(program: impl Into<OsString>) -> Self {
        Self {
            program: program.into(),
            args: Vec::new(),
            envs: Vec::new(),
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

    fn needle_worker() -> Self {
        let config = crate::config::load().unwrap_or_default();
        // Env overrides beat the installed config; the dev checkout fallback
        // only applies when neither names a provisioned interpreter.
        let configured_python = std::env::var_os("NEEDLE_PYTHON")
            .or_else(|| std::env::var_os("PYTHON"))
            .or_else(|| {
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
        command
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
        Ok(WorkerProcess {
            child,
            stdin,
            stdout: BufReader::new(stdout),
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
    stdout: BufReader<ChildStdout>,
}

pub(crate) struct Worker {
    status: BackendStatus,
    next_id: u64,
    command: WorkerCommand,
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
            let _ = process.child.kill();
            let _ = process.child.wait();
        }
        self.status = BackendStatus::Cold;
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
            let _ = process.child.wait();
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
        let process = self.process()?;
        serde_json::to_writer(&mut process.stdin, &request).map_err(WorkerError::Json)?;
        process.stdin.write_all(b"\n").map_err(WorkerError::Io)?;
        process.stdin.flush().map_err(WorkerError::Io)?;

        let mut line = String::new();
        let bytes = process
            .stdout
            .read_line(&mut line)
            .map_err(WorkerError::Io)?;
        if bytes == 0 {
            return Err(WorkerError::Protocol(
                "worker exited before sending a response".to_string(),
            ));
        }

        let response: WorkerResponse = serde_json::from_str(&line).map_err(WorkerError::Json)?;
        if response.id != Some(expected_id) {
            return Err(WorkerError::Protocol(format!(
                "response id {:?} did not match request id {expected_id}",
                response.id
            )));
        }
        Ok(response)
    }

    fn apply_status(&mut self, response: &WorkerResponse) {
        if let Some(status) = response.status {
            self.status = BackendStatus::from(status);
        } else if !response.ok {
            self.status = BackendStatus::Failed;
        }
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.shutdown();
        if let Some(mut process) = self.process.take() {
            let _ = process.child.kill();
            let _ = process.child.wait();
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
    let path = std::env::temp_dir().join(format!(
        "needle-fake-worker-{}-{label}.py",
        std::process::id()
    ));
    std::fs::write(&path, script).expect("write fake worker script");
    WorkerCommand::new(
        std::env::var_os("PYTHON")
            .or_else(|| std::env::var_os("NEEDLE_PYTHON"))
            .unwrap_or_else(|| "python3".into()),
    )
    .arg(path.into_os_string())
}

#[cfg(test)]
mod tests {
    use super::{Worker, fake_worker_command};
    use crate::backend::BackendStatus;
    use crate::protocol::PruneDecision;

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
}
