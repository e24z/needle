//! The concurrent core of the Needle daemon.
//!
//! Ownership follows the product notes: this layer owns the authoritative
//! Needle mode, session leases, and the worker lifecycle. The locking is
//! deliberately split so control operations (`mode`, `backend_status`,
//! `heartbeat`) never queue behind model work: long prunes hold only the
//! worker lock, while status reads come from a cache the worker ops update.

use crate::backend::BackendStatus;
use crate::lease::Lease;
use crate::protocol::{PruneResult, WorkerError};
use crate::worker::Worker;
use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, RwLock};
use std::time::Duration;

/// The product switch. Machinery states live in [`BackendStatus`].
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NeedleMode {
    Off,
    On,
}

/// Largest pre-prune original kept for recovery. Observations beyond this are
/// not cached; re-running the command is the fallback.
const MAX_CACHED_ORIGINAL_BYTES: usize = 4 * 1024 * 1024;

pub struct Runtime {
    sessions: Mutex<HashMap<String, Lease>>,
    status: RwLock<BackendStatus>,
    worker: Mutex<Worker>,
    /// Last pre-prune text per session: the recovery path for over-pruned
    /// observations of non-idempotent commands.
    originals: Mutex<HashMap<String, String>>,
}

impl Runtime {
    pub fn new() -> Self {
        Self::with_worker(Worker::new())
    }

    pub(crate) fn with_worker(worker: Worker) -> Self {
        Self {
            sessions: Mutex::new(HashMap::new()),
            status: RwLock::new(worker.status()),
            worker: Mutex::new(worker),
            originals: Mutex::new(HashMap::new()),
        }
    }

    pub fn mode(&self) -> NeedleMode {
        if self.lock_sessions().is_empty() {
            NeedleMode::Off
        } else {
            NeedleMode::On
        }
    }

    /// Cached backend status; never blocks on the worker.
    pub fn backend_status(&self) -> BackendStatus {
        *self.status.read().expect("status lock poisoned")
    }

    pub fn session_count(&self) -> usize {
        self.lock_sessions().len()
    }

    /// Register (or refresh) a lease and block until the model is resident.
    /// Failure is loud: the lease stays, the status reads Failed, and the
    /// caller decides whether to retry or disable.
    pub fn enable(&self, session: &str) -> Result<BackendStatus, WorkerError> {
        self.lock_sessions()
            .entry(session.to_string())
            .and_modify(Lease::refresh)
            .or_insert_with(Lease::new);
        if self.backend_status() != BackendStatus::Resident {
            self.set_status(BackendStatus::Loading);
        }
        let mut worker = self.lock_worker();
        let result = worker.load();
        self.set_status(worker.status());
        result.map(|_| self.backend_status())
    }

    /// Drop a session's lease. Returns true when it was the last one: the
    /// worker is unloaded and the caller should shut the daemon down.
    pub fn disable(&self, session: &str) -> Result<bool, WorkerError> {
        let last = {
            let mut sessions = self.lock_sessions();
            let removed = sessions.remove(session).is_some();
            removed && sessions.is_empty()
        };
        self.lock_originals().remove(session);
        if last {
            self.unload()?;
            return Ok(true);
        }
        Ok(false)
    }

    /// Refresh a session's lease. False when the session has no lease.
    pub fn heartbeat(&self, session: &str) -> bool {
        match self.lock_sessions().get_mut(session) {
            Some(lease) => {
                lease.refresh();
                true
            }
            None => false,
        }
    }

    /// Remove leases that have missed their heartbeats. Returns true when
    /// this emptied a previously non-empty table (campfire: last one out).
    pub fn reap_expired(&self, ttl: Duration) -> Result<bool, WorkerError> {
        let emptied = {
            let mut sessions = self.lock_sessions();
            let before = sessions.len();
            sessions.retain(|_, lease| !lease.expired(ttl));
            before > 0 && sessions.is_empty()
        };
        if emptied {
            self.lock_originals().clear();
            self.unload()?;
        }
        Ok(emptied)
    }

    /// Refresh the cached status if the child died outside a worker op.
    pub fn reap_worker_exit(&self) -> bool {
        let mut worker = self.lock_worker();
        let changed = worker.reap_exited_child();
        if changed {
            self.set_status(worker.status());
        }
        changed
    }

    /// Prune on behalf of a leased session. Blocks for model residency; a
    /// pruning session is a live session, so its lease is refreshed.
    pub fn prune(
        &self,
        session: &str,
        text: &str,
        query: &str,
    ) -> Result<PruneResult, WorkerError> {
        if !self.heartbeat(session) {
            return Err(WorkerError::Protocol(format!(
                "session {session} has no lease; enable first"
            )));
        }
        if text.len() <= MAX_CACHED_ORIGINAL_BYTES {
            self.lock_originals()
                .insert(session.to_string(), text.to_string());
        } else {
            self.lock_originals().remove(session);
        }
        if self.backend_status() == BackendStatus::Cold
            || self.backend_status() == BackendStatus::Failed
        {
            self.set_status(BackendStatus::Loading);
        }
        let mut worker = self.lock_worker();
        let result = worker.prune(text, query);
        self.set_status(worker.status());
        result
    }

    /// The pre-prune text of the session's most recent prune, if cached.
    pub fn last_original(&self, session: &str) -> Option<String> {
        self.lock_originals().get(session).cloned()
    }

    /// Unload the model but keep the worker process contactable.
    fn unload(&self) -> Result<(), WorkerError> {
        let mut worker = self.lock_worker();
        if worker.status() == BackendStatus::Cold {
            self.set_status(BackendStatus::Cold);
            return Ok(());
        }
        let result = worker.unload();
        self.set_status(worker.status());
        result
    }

    /// Stop the worker child entirely. For daemon shutdown paths that exit
    /// the process (bypassing Drop).
    pub fn shutdown(&self) {
        self.lock_worker().stop();
        self.set_status(BackendStatus::Cold);
    }

    fn set_status(&self, status: BackendStatus) {
        *self.status.write().expect("status lock poisoned") = status;
    }

    fn lock_sessions(&self) -> MutexGuard<'_, HashMap<String, Lease>> {
        self.sessions.lock().expect("sessions lock poisoned")
    }

    fn lock_worker(&self) -> MutexGuard<'_, Worker> {
        self.worker.lock().expect("worker lock poisoned")
    }

    fn lock_originals(&self) -> MutexGuard<'_, HashMap<String, String>> {
        self.originals.lock().expect("originals lock poisoned")
    }
}

impl Default for Runtime {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::{NeedleMode, Runtime};
    use crate::backend::BackendStatus;
    use crate::worker::{
        Worker, counting_unload_worker_command, fake_worker_command, self_exiting_worker_command,
    };
    use std::sync::{Arc, Barrier};
    use std::time::{Duration, Instant};

    fn runtime(label: &str) -> Runtime {
        Runtime::with_worker(Worker::with_command(fake_worker_command(label)))
    }

    #[test]
    fn enable_blocks_until_resident_and_switches_mode_on() {
        let runtime = runtime("enable");
        assert_eq!(runtime.mode(), NeedleMode::Off);

        let status = runtime.enable("s1").expect("enable succeeds");

        assert_eq!(status, BackendStatus::Resident);
        assert_eq!(runtime.mode(), NeedleMode::On);
    }

    #[test]
    fn last_disable_unloads_and_reports_shutdown() {
        let runtime = runtime("disable");
        runtime.enable("s1").expect("enable s1");
        runtime.enable("s2").expect("enable s2");

        assert!(!runtime.disable("s1").expect("disable s1"));
        assert_eq!(runtime.mode(), NeedleMode::On);

        assert!(runtime.disable("s2").expect("disable s2"));
        assert_eq!(runtime.mode(), NeedleMode::Off);
        assert_eq!(runtime.backend_status(), BackendStatus::Cold);
    }

    #[test]
    fn failed_enable_reads_failed_not_loading() {
        use crate::worker::WorkerCommand;
        let runtime =
            Runtime::with_worker(Worker::with_command(WorkerCommand::new("/nonexistent")));

        let error = runtime.enable("s1").expect_err("enable fails");

        assert!(error.to_string().contains("failed to start worker"));
        assert_eq!(runtime.backend_status(), BackendStatus::Failed);
        // Needle stays on-but-broken: the lease exists, the failure is loud.
        assert_eq!(runtime.mode(), NeedleMode::On);
    }

    #[test]
    fn prune_requires_a_lease() {
        let runtime = runtime("no-lease");

        let error = runtime
            .prune("ghost", "text", "query")
            .expect_err("prune without lease fails");

        assert!(error.to_string().contains("no lease"));
    }

    #[test]
    fn prune_caches_the_original_for_recovery() {
        let runtime = runtime("original");
        runtime.enable("s1").expect("enable");

        let result = runtime
            .prune("s1", "keep drop", "keep relevant code")
            .expect("prune succeeds");

        assert_eq!(result.text, "keep");
        assert_eq!(runtime.last_original("s1").as_deref(), Some("keep drop"));
    }

    #[test]
    fn expired_leases_are_reaped_and_last_one_unloads() {
        let runtime = runtime("reap");
        runtime.enable("s1").expect("enable");

        assert!(
            !runtime
                .reap_expired(Duration::from_secs(60))
                .expect("fresh lease survives")
        );

        std::thread::sleep(Duration::from_millis(30));
        assert!(
            runtime
                .reap_expired(Duration::from_millis(10))
                .expect("expired lease reaped")
        );
        assert_eq!(runtime.mode(), NeedleMode::Off);
    }

    #[test]
    fn control_ops_answer_while_a_prune_is_in_flight() {
        let runtime = Arc::new(runtime("liveness"));
        runtime.enable("s1").expect("enable");

        let background = Arc::clone(&runtime);
        let slow = std::thread::spawn(move || {
            background
                .prune("s1", "SLOW keep drop", "keep relevant code")
                .expect("slow prune succeeds")
        });

        // Give the slow prune time to take the worker lock.
        std::thread::sleep(Duration::from_millis(300));
        let started = Instant::now();
        assert_eq!(runtime.backend_status(), BackendStatus::Resident);
        assert!(runtime.heartbeat("s1"));
        assert_eq!(runtime.mode(), NeedleMode::On);
        assert!(
            started.elapsed() < Duration::from_millis(200),
            "control ops queued behind the prune: {:?}",
            started.elapsed()
        );

        let result = slow.join().expect("prune thread");
        assert_eq!(result.text, "SLOW keep");
    }

    #[test]
    fn reaper_marks_silently_exited_worker_failed() {
        let runtime = Runtime::with_worker(Worker::with_command(self_exiting_worker_command(
            "self-exit",
        )));
        runtime.enable("s1").expect("enable");
        assert_eq!(runtime.backend_status(), BackendStatus::Resident);

        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if runtime.reap_worker_exit() {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }

        assert_eq!(runtime.backend_status(), BackendStatus::Failed);
        assert_eq!(runtime.mode(), NeedleMode::On);
    }

    #[test]
    fn concurrent_last_lease_paths_unload_once() {
        let log_path =
            std::env::temp_dir().join(format!("needle-unload-count-{}.log", std::process::id()));
        let _ = std::fs::remove_file(&log_path);
        let runtime = Arc::new(Runtime::with_worker(Worker::with_command(
            counting_unload_worker_command("race", &log_path),
        )));
        runtime.enable("s1").expect("enable s1");
        runtime.enable("s2").expect("enable s2");

        let barrier = Arc::new(Barrier::new(3));
        let disable_runtime = Arc::clone(&runtime);
        let disable_barrier = Arc::clone(&barrier);
        let disable = std::thread::spawn(move || {
            disable_barrier.wait();
            disable_runtime.disable("s1").expect("disable s1")
        });
        let reap_runtime = Arc::clone(&runtime);
        let reap_barrier = Arc::clone(&barrier);
        let reap = std::thread::spawn(move || {
            reap_barrier.wait();
            reap_runtime
                .reap_expired(Duration::ZERO)
                .expect("reap expired")
        });

        barrier.wait();
        let disable_last = disable.join().expect("disable thread");
        let reap_emptied = reap.join().expect("reap thread");

        assert!(
            disable_last || reap_emptied,
            "one path should observe the last lease"
        );
        assert_eq!(runtime.mode(), NeedleMode::Off);
        assert_eq!(runtime.backend_status(), BackendStatus::Cold);
        let unload_log = std::fs::read_to_string(&log_path).expect("read unload log");
        assert_eq!(unload_log.lines().count(), 1, "unload log: {unload_log}");
        let _ = std::fs::remove_file(&log_path);
    }
}
