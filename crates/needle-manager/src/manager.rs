use crate::backend::BackendStatus;
use crate::lease::Lease;
use crate::protocol::{PruneResult, WorkerError};
use crate::worker::Worker;
use std::collections::HashMap;

pub struct Manager {
    leases: HashMap<String, Lease>,
    worker: Worker,
}

impl Manager {
    pub fn new() -> Self {
        Self {
            leases: HashMap::new(),
            worker: Worker::new(),
        }
    }

    pub fn backend_status(&self) -> BackendStatus {
        self.worker.status()
    }

    pub fn refresh_backend_status(&mut self) -> Result<BackendStatus, WorkerError> {
        self.worker.refresh_status()
    }

    pub fn load_backend(&mut self) -> Result<(), WorkerError> {
        self.worker.load()
    }

    pub fn prune(&mut self, text: &str, query: &str) -> Result<PruneResult, WorkerError> {
        self.worker.prune(text, query)
    }

    pub fn unload_backend(&mut self) -> Result<(), WorkerError> {
        self.worker.unload()
    }

    pub fn has_leases(&self) -> bool {
        !self.leases.is_empty()
    }

    pub fn add_lease(&mut self, session_id: String) {
        self.leases.insert(session_id, Lease::new());
    }

    pub fn remove_lease(&mut self, session_id: &str) {
        self.leases.remove(session_id);
    }

    pub fn heartbeat(&mut self, session_id: &str) -> bool {
        if let Some(lease) = self.leases.get_mut(session_id) {
            lease.refresh();
            true
        } else {
            false
        }
    }
}
