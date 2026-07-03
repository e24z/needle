use crate::backend::BackendStatus;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::io;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum WorkerStatus {
    Cold,
    Loading,
    Resident,
    Failed,
}

impl From<WorkerStatus> for BackendStatus {
    fn from(status: WorkerStatus) -> Self {
        match status {
            WorkerStatus::Cold => BackendStatus::Cold,
            WorkerStatus::Loading => BackendStatus::Loading,
            WorkerStatus::Resident => BackendStatus::Resident,
            WorkerStatus::Failed => BackendStatus::Failed,
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum PruneDecision {
    Pruned,
    Unchanged,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PruneResult {
    pub text: String,
    pub decision: PruneDecision,
    pub reason: Option<String>,
    pub backend: Option<String>,
    pub stats: BTreeMap<String, Value>,
}

#[derive(Debug)]
pub enum WorkerError {
    Spawn(io::Error),
    MissingPipe(&'static str),
    Io(io::Error),
    Json(serde_json::Error),
    Protocol(String),
    Worker(String),
}

impl fmt::Display for WorkerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WorkerError::Spawn(error) => write!(f, "failed to start worker: {error}"),
            WorkerError::MissingPipe(pipe) => write!(f, "worker child missing {pipe} pipe"),
            WorkerError::Io(error) => write!(f, "worker I/O failed: {error}"),
            WorkerError::Json(error) => write!(f, "worker JSON failed: {error}"),
            WorkerError::Protocol(message) => write!(f, "worker protocol error: {message}"),
            WorkerError::Worker(message) => write!(f, "worker failed: {message}"),
        }
    }
}

impl Error for WorkerError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            WorkerError::Spawn(error) | WorkerError::Io(error) => Some(error),
            WorkerError::Json(error) => Some(error),
            WorkerError::MissingPipe(_) | WorkerError::Protocol(_) | WorkerError::Worker(_) => None,
        }
    }
}

/// Ops Rust actually sends. The Python worker also answers a `status` op,
/// kept for humans poking the protocol by hand; Rust reads its own cache.
#[derive(Debug, Serialize)]
#[serde(tag = "op", rename_all = "lowercase")]
pub(crate) enum WorkerRequest {
    Load {
        id: u64,
    },
    Prune {
        id: u64,
        text: String,
        query: String,
    },
    Unload {
        id: u64,
    },
    Exit {
        id: u64,
    },
}

impl WorkerRequest {
    pub(crate) fn id(&self) -> u64 {
        match self {
            WorkerRequest::Load { id }
            | WorkerRequest::Prune { id, .. }
            | WorkerRequest::Unload { id }
            | WorkerRequest::Exit { id } => *id,
        }
    }
}

#[derive(Debug, Deserialize)]
pub(crate) struct WorkerResponse {
    pub(crate) id: Option<u64>,
    pub(crate) ok: bool,
    pub(crate) status: Option<WorkerStatus>,
    pub(crate) backend: Option<String>,
    pub(crate) decision: Option<PruneDecision>,
    pub(crate) reason: Option<String>,
    pub(crate) text: Option<String>,
    #[serde(default)]
    pub(crate) stats: BTreeMap<String, Value>,
    pub(crate) error: Option<String>,
}

impl WorkerResponse {
    pub(crate) fn worker_error(&self) -> WorkerError {
        WorkerError::Worker(
            self.error
                .clone()
                .unwrap_or_else(|| "worker returned ok=false".to_string()),
        )
    }
}
