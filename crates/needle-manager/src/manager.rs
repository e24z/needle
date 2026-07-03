use crate::backend::BackendStatus;
use crate::lease::Lease;
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
