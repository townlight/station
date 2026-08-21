use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use station_media_assets::{AssetError, ingest_media, measure_loudness};
use station_media_lifecycle::{
    AssetLoudnessAnalysis, IngestSourceKind, LoudnessGateStatus, MediaIngestJob, WatchFolderConfig,
};
use station_schedule::{AssetReadiness, MediaAsset};
use station_storage::StationStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IngestOutcome {
    Completed {
        job_id: String,
        asset_id: String,
    },
    RetryScheduled {
        job_id: String,
        next_attempt_at_unix_ms: i64,
        detail: String,
    },
    Failed {
        job_id: String,
        detail: String,
    },
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

pub struct WatchFolderMonitor {
    database_path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WatchScanOutcome {
    pub scanned_files: usize,
    pub queued_job_ids: Vec<String>,
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
                let loudness = analyze_loudness(&asset);
                StationStore::open(&self.database_path)
                    .and_then(|store| {
                        store.complete_ingest_job(&job.job_id, &asset, &loudness, completed_at)
                    })
                    .map_err(IngestWorkerError::Storage)?;
                Ok(Some(IngestOutcome::Completed {
                    job_id: job.job_id,
                    asset_id: ingested.asset_id,
                }))
            }
            Err(error) => {
                let detail = operator_error(&error);
                if job.source_kind == IngestSourceKind::WatchFolder
                    && transient_watch_error(&error)
                    && job.attempts < 5
                {
                    let next_attempt_at_unix_ms =
                        completed_at.saturating_add(retry_delay(job.attempts));
                    StationStore::open(&self.database_path)
                        .and_then(|store| {
                            store.retry_ingest_job(&job.job_id, next_attempt_at_unix_ms, &detail)
                        })
                        .map_err(IngestWorkerError::Storage)?;
                    return Ok(Some(IngestOutcome::RetryScheduled {
                        job_id: job.job_id,
                        next_attempt_at_unix_ms,
                        detail,
                    }));
                }
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

impl WatchFolderMonitor {
    pub fn new(database_path: impl Into<PathBuf>) -> Self {
        Self {
            database_path: database_path.into(),
        }
    }

    pub fn scan_once_at(&self, observed_at_unix_ms: i64) -> Result<WatchScanOutcome, String> {
        if observed_at_unix_ms < 0 {
            return Err("watch-folder scan time must be nonnegative".into());
        }
        let configs = StationStore::open(&self.database_path)?.list_watch_folders()?;
        let mut outcome = WatchScanOutcome {
            scanned_files: 0,
            queued_job_ids: Vec::new(),
        };
        for config in configs.into_iter().filter(|config| config.enabled) {
            match self.scan_configuration(&config, observed_at_unix_ms) {
                Ok(config_outcome) => {
                    outcome.scanned_files += config_outcome.scanned_files;
                    outcome.queued_job_ids.extend(config_outcome.queued_job_ids);
                    StationStore::open(&self.database_path)?.record_watch_folder_scan(
                        &config.config_id,
                        observed_at_unix_ms,
                        None,
                    )?;
                }
                Err(error) => {
                    let detail = bounded_detail(&error);
                    StationStore::open(&self.database_path)?.record_watch_folder_scan(
                        &config.config_id,
                        observed_at_unix_ms,
                        Some(&detail),
                    )?;
                }
            }
        }
        Ok(outcome)
    }

    fn scan_configuration(
        &self,
        config: &WatchFolderConfig,
        observed_at_unix_ms: i64,
    ) -> Result<WatchScanOutcome, String> {
        let directory = fs::read_dir(&config.monitor_path).map_err(|error| {
            format!(
                "Watch folder '{}' is unavailable: {error}",
                config.display_name
            )
        })?;
        let store = StationStore::open(&self.database_path)?;
        let mut outcome = WatchScanOutcome {
            scanned_files: 0,
            queued_job_ids: Vec::new(),
        };
        for entry in directory {
            let entry =
                entry.map_err(|error| format!("Could not list a watch-folder entry: {error}"))?;
            let file_type = entry
                .file_type()
                .map_err(|error| format!("Could not inspect a watch-folder entry: {error}"))?;
            if !file_type.is_file() || !matches_extension(&entry.path(), &config.extensions) {
                continue;
            }
            let metadata = entry
                .metadata()
                .map_err(|error| format!("Could not read watch-file metadata: {error}"))?;
            if metadata.len() == 0 {
                continue;
            }
            let modified_at_unix_ms =
                system_time_to_millis(metadata.modified().map_err(|error| {
                    format!("Could not read watch-file modification time: {error}")
                })?)?;
            let source_path = entry.path().to_string_lossy().into_owned();
            let observation = store.observe_watch_file(
                &config.config_id,
                &source_path,
                metadata.len(),
                modified_at_unix_ms,
                observed_at_unix_ms,
            )?;
            outcome.scanned_files += 1;
            let settled_for =
                observed_at_unix_ms.saturating_sub(observation.unchanged_since_unix_ms);
            if observation.observation_count < 2
                || settled_for < i64::from(config.settle_seconds) * 1_000
                || observation.queued_job_id.is_some()
            {
                continue;
            }
            let job_id = deterministic_watch_job_id(
                &config.config_id,
                &source_path,
                metadata.len(),
                modified_at_unix_ms,
            );
            let job = pending_job_with_source(
                job_id.clone(),
                source_path.clone(),
                observed_at_unix_ms,
                IngestSourceKind::WatchFolder,
            );
            let _ = store.create_ingest_job(&job)?;
            store.mark_watch_file_queued(&config.config_id, &source_path, &job_id)?;
            outcome.queued_job_ids.push(job_id);
        }
        Ok(outcome)
    }
}

pub fn run_until(
    database_path: &Path,
    media_library: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), IngestWorkerError> {
    let worker = IngestWorker::new(database_path, media_library);
    let watch_folders = WatchFolderMonitor::new(database_path);
    worker.recover_interrupted()?;
    let mut next_watch_scan = Instant::now();
    while !stop.load(Ordering::Acquire) {
        if Instant::now() >= next_watch_scan {
            watch_folders
                .scan_once_at(system_time_millis()?)
                .map_err(IngestWorkerError::Storage)?;
            next_watch_scan = Instant::now() + Duration::from_secs(5);
        }
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

fn transient_watch_error(error: &AssetError) -> bool {
    matches!(error, AssetError::InvalidSource(_) | AssetError::Io(_))
}

fn analyze_loudness(asset: &MediaAsset) -> AssetLoudnessAnalysis {
    const TARGET_MILLI: i32 = -24_000;
    const TOLERANCE_MILLI: i32 = 2_000;
    match measure_loudness(&asset.media_path) {
        Ok(measurement) => {
            let measured = (measurement.integrated_lufs * 1_000.0).round();
            if !measured.is_finite()
                || measured < f64::from(i32::MIN)
                || measured > f64::from(i32::MAX)
            {
                return AssetLoudnessAnalysis {
                    asset_id: asset.asset_id.clone(),
                    status: LoudnessGateStatus::NotChecked,
                    measured_lufs_milli: None,
                    target_lufs_milli: TARGET_MILLI,
                    detail: Some("Loudness analysis returned an invalid measurement.".into()),
                };
            }
            let measured = measured as i32;
            let passed = measured.abs_diff(TARGET_MILLI) <= TOLERANCE_MILLI as u32;
            AssetLoudnessAnalysis {
                asset_id: asset.asset_id.clone(),
                status: if passed {
                    LoudnessGateStatus::Ok
                } else {
                    LoudnessGateStatus::Failed
                },
                measured_lufs_milli: Some(measured),
                target_lufs_milli: TARGET_MILLI,
                detail: (!passed).then(|| {
                    format!(
                        "Measured {:.2} LUFS; ingest target is -24.00 LUFS ±2.00 LU. Normalize before air.",
                        f64::from(measured) / 1_000.0
                    )
                }),
            }
        }
        Err(error) => AssetLoudnessAnalysis {
            asset_id: asset.asset_id.clone(),
            status: LoudnessGateStatus::NotChecked,
            measured_lufs_milli: None,
            target_lufs_milli: TARGET_MILLI,
            detail: Some(bounded_detail(&format!(
                "Loudness could not be measured: {}",
                operator_error(&error)
            ))),
        },
    }
}

fn retry_delay(attempts: u32) -> i64 {
    match attempts {
        0 | 1 => 1_000,
        2 => 5_000,
        3 => 15_000,
        _ => 60_000,
    }
}

fn system_time_millis() -> Result<i64, IngestWorkerError> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| IngestWorkerError::Clock(error.to_string()))?
        .as_millis();
    i64::try_from(millis).map_err(|_| IngestWorkerError::Clock("system time exceeds i64".into()))
}

pub fn pending_job(job_id: String, source_path: String, created_at_unix_ms: i64) -> MediaIngestJob {
    pending_job_with_source(
        job_id,
        source_path,
        created_at_unix_ms,
        IngestSourceKind::OperatorPath,
    )
}

pub fn pending_job_with_source(
    job_id: String,
    source_path: String,
    created_at_unix_ms: i64,
    source_kind: IngestSourceKind,
) -> MediaIngestJob {
    MediaIngestJob {
        job_id,
        source_kind,
        source_path,
        status: station_media_lifecycle::IngestJobStatus::Pending,
        progress_percent: 0,
        created_at_unix_ms,
        started_at_unix_ms: None,
        completed_at_unix_ms: None,
        error_detail: None,
        asset_id: None,
        attempts: 0,
        next_attempt_at_unix_ms: None,
    }
}

fn matches_extension(path: &Path, extensions: &[String]) -> bool {
    let Some(extension) = path.extension().and_then(|value| value.to_str()) else {
        return false;
    };
    let extension = format!(".{}", extension.to_ascii_lowercase());
    extensions.iter().any(|allowed| allowed == &extension)
}

fn deterministic_watch_job_id(
    config_id: &str,
    source_path: &str,
    size_bytes: u64,
    modified_at_unix_ms: i64,
) -> String {
    let mut digest = Sha256::new();
    for value in [
        config_id.as_bytes(),
        source_path.as_bytes(),
        &size_bytes.to_le_bytes(),
        &modified_at_unix_ms.to_le_bytes(),
    ] {
        digest.update((value.len() as u64).to_le_bytes());
        digest.update(value);
    }
    let mut bytes: [u8; 16] = digest.finalize()[..16]
        .try_into()
        .expect("a SHA-256 digest contains sixteen bytes");
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15]
    )
}

fn system_time_to_millis(value: SystemTime) -> Result<i64, String> {
    let millis = value
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("watch-file modification time precedes the Unix epoch: {error}"))?
        .as_millis();
    i64::try_from(millis).map_err(|_| "watch-file modification time exceeds i64".into())
}

fn bounded_detail(value: &str) -> String {
    value
        .chars()
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
