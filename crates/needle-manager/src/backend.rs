#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BackendStatus {
    Cold,
    Loading,
    Resident,
    Failed,
}
