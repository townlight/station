use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use station_media_assets::{AssetError, ingest_media};
use station_media_lifecycle::MediaIngestJob;
use station_schedule::{AssetReadiness, MediaAsset};
use station_storage::StationStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IngestOutcome {
    Completed { job_id: String, asset_id: String },
    Failed { job_id: String, detail: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IngestWorkerError {
    Storage(String),
    Clock(String),
}

pub struct IngestWorker {
    database_path: PathBuf,
    media_library: PathBuf,
}

impl IngestWorker {
    pub fn new(database_path: impl Into<PathBuf>, media_library: impl Into<PathBuf>) -> Self {
        Self {
            database_path: database_path.into(),
            media_library: media_library.into(),
        }
    }

    pub fn recover_interrupted(&self) -> Result<u64, IngestWorkerError> {
        StationStore::open(&self.database_path)
            .and_then(|store| store.recover_interrupted_ingest_jobs())
            .map_err(IngestWorkerError::Storage)
    }

    pub fn run_once(&self) -> Result<Option<IngestOutcome>, IngestWorkerError> {
        let started_at = system_time_millis()?;
        let store = StationStore::open(&self.database_path).map_err(IngestWorkerError::Storage)?;
        let Some(job) = store
            .claim_next_ingest_job(started_at)
            .map_err(IngestWorkerError::Storage)?
        else {
            return Ok(None);
        };
        drop(store);
        let result = ingest_media(&job.source_path, &self.media_library);
        let completed_at = system_time_millis()?;
        match result {
            Ok(ingested) => {
                let asset = MediaAsset {
                    asset_id: ingested.asset_id.clone(),
                    media_path: ingested.stored_path.to_string_lossy().into_owned(),
                    duration_ms: (ingested.metadata.duration_ns / 1_000_000).max(1),
                    readiness: AssetReadiness::Ready,
                };
                StationStore::open(&self.database_path)
                    .and_then(|store| store.complete_ingest_job(&job.job_id, &asset, completed_at))
                    .map_err(IngestWorkerError::Storage)?;
                Ok(Some(IngestOutcome::Completed {
                    job_id: job.job_id,
                    asset_id: ingested.asset_id,
                }))
            }
            Err(error) => {
                let detail = operator_error(&error);
                StationStore::open(&self.database_path)
                    .and_then(|store| store.fail_ingest_job(&job.job_id, completed_at, &detail))
                    .map_err(IngestWorkerError::Storage)?;
                Ok(Some(IngestOutcome::Failed {
                    job_id: job.job_id,
                    detail,
                }))
            }
        }
    }
}

pub fn run_until(
    database_path: &Path,
    media_library: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), IngestWorkerError> {
    let worker = IngestWorker::new(database_path, media_library);
    worker.recover_interrupted()?;
    while !stop.load(Ordering::Acquire) {
        if worker.run_once()?.is_none() {
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    Ok(())
}

fn operator_error(error: &AssetError) -> String {
    let raw = match error {
        AssetError::InvalidSource(message) => format!("Invalid media source: {message}"),
        AssetError::UnsupportedMedia(message) => format!("Unsupported media: {message}"),
        AssetError::Io(message) => format!("Media file operation failed: {message}"),
        AssetError::Discovery(message) => format!("Media analysis failed: {message}"),
    };
    raw.chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .take(4_000)
        .collect()
}

fn system_time_millis() -> Result<i64, IngestWorkerError> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| IngestWorkerError::Clock(error.to_string()))?
        .as_millis();
    i64::try_from(millis).map_err(|_| IngestWorkerError::Clock("system time exceeds i64".into()))
}

pub fn pending_job(job_id: String, source_path: String, created_at_unix_ms: i64) -> MediaIngestJob {
    MediaIngestJob {
        job_id,
        source_kind: station_media_lifecycle::IngestSourceKind::OperatorPath,
        source_path,
        status: station_media_lifecycle::IngestJobStatus::Pending,
        progress_percent: 0,
        created_at_unix_ms,
        started_at_unix_ms: None,
        completed_at_unix_ms: None,
        error_detail: None,
        asset_id: None,
        attempts: 0,
    }
}
