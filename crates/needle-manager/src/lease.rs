use std::time::Instant;

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
}
