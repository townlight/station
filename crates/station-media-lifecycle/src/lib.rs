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
    pub next_attempt_at_unix_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WatchFolderConfig {
    pub config_id: String,
    pub display_name: String,
    pub monitor_path: String,
    pub extensions: Vec<String>,
    pub settle_seconds: u32,
    pub enabled: bool,
    pub last_scan_at_unix_ms: Option<i64>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WatchFileObservation {
    pub config_id: String,
    pub source_path: String,
    pub size_bytes: u64,
    pub modified_at_unix_ms: i64,
    pub unchanged_since_unix_ms: i64,
    pub observation_count: u32,
    pub queued_job_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LoudnessGateStatus {
    Ok,
    Failed,
    NotChecked,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AssetLoudnessAnalysis {
    pub asset_id: String,
    pub status: LoudnessGateStatus,
    pub measured_lufs_milli: Option<i32>,
    pub target_lufs_milli: i32,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadinessState {
    Ready,
    Processing,
    MissingFile,
    Rejected,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AssetReadinessView {
    pub asset_id: String,
    pub readiness_state: ReadinessState,
    pub readiness_reason: Option<String>,
    pub media_path: String,
    pub duration_ms: u64,
    pub loudness_status: LoudnessGateStatus,
    pub measured_lufs: Option<f64>,
    pub target_lufs: f64,
    pub loudness_detail: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReadinessDashboard {
    pub total_assets: usize,
    pub ready_count: usize,
    pub processing_count: usize,
    pub missing_count: usize,
    pub rejected_count: usize,
    pub pending_ingest_count: usize,
    pub running_ingest_count: usize,
    pub failed_ingest_count: usize,
    pub by_asset: Vec<AssetReadinessView>,
    pub ingest_jobs: Vec<MediaIngestJob>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MissingMediaAlert {
    pub schedule_item_id: String,
    pub channel_id: String,
    pub asset_id: String,
    pub title: String,
    pub starts_at_unix_ms: i64,
    pub readiness_state: ReadinessState,
    pub readiness_reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IngestJobError {
    Invalid(&'static str),
}

impl WatchFolderConfig {
    pub fn validate(&self) -> Result<(), IngestJobError> {
        if !is_uuid(&self.config_id) {
            return Err(IngestJobError::Invalid(
                "config_id must be a canonical UUID",
            ));
        }
        if !visible_bounded(&self.display_name, 120) {
            return Err(IngestJobError::Invalid(
                "display_name must be visible and at most 120 characters",
            ));
        }
        if !visible_bounded(&self.monitor_path, 2_000) {
            return Err(IngestJobError::Invalid(
                "monitor_path must be a visible path",
            ));
        }
        if self.extensions.is_empty()
            || self.extensions.len() > 32
            || self.extensions.iter().any(|extension| {
                !(2..=13).contains(&extension.len())
                    || !extension.starts_with('.')
                    || !extension
                        .bytes()
                        .skip(1)
                        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
            })
        {
            return Err(IngestJobError::Invalid(
                "extensions must be unique lowercase dot-prefixed file extensions",
            ));
        }
        let mut sorted = self.extensions.clone();
        sorted.sort();
        sorted.dedup();
        if sorted.len() != self.extensions.len() {
            return Err(IngestJobError::Invalid(
                "extensions must be unique lowercase dot-prefixed file extensions",
            ));
        }
        if !(1..=3_600).contains(&self.settle_seconds) {
            return Err(IngestJobError::Invalid(
                "settle_seconds must be between 1 and 3600",
            ));
        }
        if self.last_scan_at_unix_ms.is_some_and(|value| value < 0)
            || self
                .last_error
                .as_ref()
                .is_some_and(|value| !visible_bounded(value, 4_000))
        {
            return Err(IngestJobError::Invalid(
                "watch-folder scan state is invalid",
            ));
        }
        Ok(())
    }
}

impl AssetLoudnessAnalysis {
    pub fn validate(&self) -> Result<(), IngestJobError> {
        if !is_asset_id(&self.asset_id) {
            return Err(IngestJobError::Invalid(
                "loudness asset_id must be a lowercase SHA-256 digest",
            ));
        }
        if !(-70_000..=-5_000).contains(&self.target_lufs_milli)
            || self
                .measured_lufs_milli
                .is_some_and(|value| !(-200_000..=20_000).contains(&value))
            || self
                .detail
                .as_ref()
                .is_some_and(|value| !visible_bounded(value, 4_000))
        {
            return Err(IngestJobError::Invalid(
                "loudness measurement or detail is invalid",
            ));
        }
        match self.status {
            LoudnessGateStatus::Ok | LoudnessGateStatus::Failed
                if self.measured_lufs_milli.is_none() =>
            {
                Err(IngestJobError::Invalid(
                    "a completed loudness gate requires a measurement",
                ))
            }
            LoudnessGateStatus::NotChecked if self.measured_lufs_milli.is_some() => Err(
                IngestJobError::Invalid("an unchecked loudness gate cannot contain a measurement"),
            ),
            _ => Ok(()),
        }
    }
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
            || self.next_attempt_at_unix_ms.is_some_and(|value| value < 0)
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

fn visible_bounded(value: &str, maximum: usize) -> bool {
    !value.trim().is_empty() && value.len() <= maximum && !value.chars().any(char::is_control)
}

#[cfg(test)]
mod tests {
    use super::{IngestJobError, WatchFolderConfig};

    fn config() -> WatchFolderConfig {
        WatchFolderConfig {
            config_id: "11111111-2222-4333-8444-555555555555".into(),
            display_name: "Council drop folder".into(),
            monitor_path: r"\\station-nas\incoming".into(),
            extensions: vec![".mov".into(), ".mp4".into(), ".ts".into()],
            settle_seconds: 10,
            enabled: true,
            last_scan_at_unix_ms: None,
            last_error: None,
        }
    }

    #[test]
    fn validates_operator_configurable_watch_folders() {
        config().validate().unwrap();
        let mut invalid = config();
        invalid.extensions = vec![".MP4".into()];
        assert!(matches!(
            invalid.validate(),
            Err(IngestJobError::Invalid(_))
        ));
        let mut duplicate = config();
        duplicate.extensions = vec![".ts".into(), ".ts".into()];
        assert!(duplicate.validate().is_err());
        let mut unsafe_error = config();
        unsafe_error.last_error = Some("bad\nstate".into());
        assert!(unsafe_error.validate().is_err());
    }
}
