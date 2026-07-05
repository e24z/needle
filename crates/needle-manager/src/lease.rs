use std::time::{Duration, Instant};

pub(crate) struct Lease {
    last_heartbeat: Instant,
}

impl Lease {
    pub(crate) fn new() -> Self {
        Self {
            last_heartbeat: Instant::now(),
        }
    }

    pub(crate) fn refresh(&mut self) {
        self.last_heartbeat = Instant::now();
    }

    pub(crate) fn expired(&self, ttl: Duration) -> bool {
        self.last_heartbeat.elapsed() > ttl
    }
}
