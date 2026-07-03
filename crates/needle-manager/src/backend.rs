use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum BackendStatus {
    Cold,
    Loading,
    Resident,
    Failed,
}

impl BackendStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            BackendStatus::Cold => "cold",
            BackendStatus::Loading => "loading",
            BackendStatus::Resident => "resident",
            BackendStatus::Failed => "failed",
        }
    }
}
