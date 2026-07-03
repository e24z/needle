use crate::backend::BackendStatus;

pub(crate) struct Worker {
    status: BackendStatus,
}

impl Worker {
    pub(crate) fn new() -> Self {
        Self {
            status: BackendStatus::Cold,
        }
    }

    pub(crate) fn status(&self) -> BackendStatus {
        self.status
    }
}
