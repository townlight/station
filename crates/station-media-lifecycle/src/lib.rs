use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IngestSourceKind {
    OperatorPath,
    WatchFolder,
    LiveFinalization,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IngestJobStatus {
    Pending,
    Running,
    Completed,
    Failed,
    Cancelled,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MediaIngestJob {
    pub job_id: String,
    pub source_kind: IngestSourceKind,
    pub source_path: String,
    pub status: IngestJobStatus,
    pub progress_percent: u8,
    pub created_at_unix_ms: i64,
    pub started_at_unix_ms: Option<i64>,
    pub completed_at_unix_ms: Option<i64>,
    pub error_detail: Option<String>,
    pub asset_id: Option<String>,
    pub attempts: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IngestJobError {
    Invalid(&'static str),
}

impl MediaIngestJob {
    pub fn validate(&self) -> Result<(), IngestJobError> {
        if !is_uuid(&self.job_id) {
            return Err(IngestJobError::Invalid("job_id must be a canonical UUID"));
        }
        if self.source_path.trim().is_empty() || self.source_path.chars().any(char::is_control) {
            return Err(IngestJobError::Invalid(
                "source_path must be a visible path",
            ));
        }
        if self.progress_percent > 100 || self.created_at_unix_ms < 0 {
            return Err(IngestJobError::Invalid(
                "progress and creation time are invalid",
            ));
        }
        if self.started_at_unix_ms.is_some_and(|value| value < 0)
            || self.completed_at_unix_ms.is_some_and(|value| value < 0)
            || self
                .error_detail
                .as_ref()
                .is_some_and(|value| value.len() > 4_000 || value.chars().any(char::is_control))
        {
            return Err(IngestJobError::Invalid(
                "job timestamps or error detail are invalid",
            ));
        }
        if self
            .asset_id
            .as_ref()
            .is_some_and(|value| !is_asset_id(value))
        {
            return Err(IngestJobError::Invalid(
                "asset_id must be a lowercase SHA-256 digest",
            ));
        }
        Ok(())
    }
}

fn is_asset_id(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
}
