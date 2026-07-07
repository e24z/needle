//! Needle's installed configuration: one JSON file under NEEDLE_HOME.
//!
//! Its existence is what "configured" means — the bare `needle` command runs
//! the setup wizard when it is absent. Values here are the wizard's output
//! and the daemon's input (which Python owns the worker, where the model is).

use crate::daemon::needle_home;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::io;
use std::path::PathBuf;

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct Config {
    /// Python interpreter of the private worker venv.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub worker_python: Option<PathBuf>,
    /// Directory holding the model snapshot (needle-model.json inside).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_dir: Option<PathBuf>,
    /// Whether Pi integration was registered via `pi install`.
    #[serde(default)]
    pub pi_integrated: bool,
    /// Legacy single cli-spinners entry. New config uses per-state spinners.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status_spinner: Option<String>,
    /// Transitional per-state spinner map from early dev builds.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub status_spinners: BTreeMap<String, String>,
    /// Pi statusline appearance, keyed by visual state.
    #[serde(default, skip_serializing_if = "StatuslineConfig::is_empty")]
    pub statusline: StatuslineConfig,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub needle_version: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct StatuslineConfig {
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub states: BTreeMap<String, StatuslineStateConfig>,
}

impl StatuslineConfig {
    pub fn is_empty(&self) -> bool {
        self.states.is_empty()
    }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct StatuslineStateConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub spinner: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub color: Option<String>,
    pub interval_ms: Option<u64>,
}

pub fn config_path() -> PathBuf {
    needle_home().join("config.json")
}

pub fn load() -> Option<Config> {
    let text = std::fs::read_to_string(config_path()).ok()?;
    serde_json::from_str(&text).ok()
}

pub fn save(config: &Config) -> io::Result<()> {
    let path = config_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let text = serde_json::to_string_pretty(config).map_err(io::Error::other)?;
    std::fs::write(path, text + "\n")
}

pub fn is_configured() -> bool {
    config_path().exists()
}
