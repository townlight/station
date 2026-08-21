use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertEventState, AlertRule, AlertSeverity,
    SelfTestKind, SelfTestSchedule, SelfTestTrigger, StationProfile, derive_runtime_safe_to_air,
    redact_alert_channel_target, validate_alert_channel_target,
};
use station_media_lifecycle::{
    AssetLoudnessAnalysis, AssetReadinessView, IngestJobStatus, IngestSourceKind,
    LoudnessGateStatus, MediaIngestJob, MissingMediaAlert, ReadinessDashboard, ReadinessState,
    TranscodeJobStatus, WatchFolderConfig,
};
use station_schedule::{AssetReadiness, ChannelConfiguration, MediaAsset, ScheduleItem};
use station_storage::{AlertChannelSecret, CommitWriteError, ProfileWriteError, StationStore};
use station_update::UpdateCheckTrigger;
use station_update_coordinator::CoordinatorError;

const OPERATOR_HTML: &[u8] = include_bytes!("../../../web/operator/dist/index.html");
const OPERATOR_SCRIPT: &[u8] = include_bytes!("../../../web/operator/dist/operator.js");
const OPERATOR_STYLES: &[u8] = include_bytes!("../../../web/operator/dist/operator.css");

#[derive(Deserialize)]
struct PutStationProfile {
    #[serde(flatten)]
    profile: StationProfile,
    #[serde(default)]
    expected_revision: u64,
}

#[derive(Deserialize)]
struct PrepareScheduleCommit {
    plan_id: String,
    schedule_item_id: String,
}

#[derive(Deserialize)]
struct CommitSchedule {
    report_id: String,
    plan_id: String,
    schedule_item_id: String,
    approved_by: String,
    #[serde(default)]
    operator_notes: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct IngestMedia {
    job_id: String,
    source_path: PathBuf,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AcknowledgeAlert {
    acknowledged_by: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PutAlertChannel {
    channel_id: String,
    kind: AlertChannelKind,
    label: String,
    enabled: bool,
    target: String,
    #[serde(default)]
    endpoint: Option<String>,
    #[serde(default)]
    sender: Option<String>,
    #[serde(default)]
    identity: Option<String>,
    credential: String,
    #[serde(default)]
    quiet_hours_start_utc: Option<String>,
    #[serde(default)]
    quiet_hours_end_utc: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PutAlertRule {
    condition: AlertConditionKind,
    enabled: bool,
    severity: AlertSeverity,
    channel_ids: Vec<String>,
    dedupe_window_seconds: u32,
    re_alert_after_seconds: u32,
    #[serde(default)]
    scope_channel_id: Option<String>,
    notify_on_resolve: bool,
    updated_by: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PutSelfTestSchedule {
    enabled: bool,
    local_hour: u8,
    local_minute: u8,
    #[serde(default)]
    weekday: Option<u8>,
    updated_by: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiResponse {
    pub status: u16,
    pub content_type: &'static str,
    pub attachment_name: Option<String>,
    pub body: Vec<u8>,
}

pub struct Api {
    storage: StationStore,
    database_path: PathBuf,
    application_directory: PathBuf,
}

impl Api {
    pub fn open(database_path: impl AsRef<Path>) -> Result<Self, String> {
        let application_directory = std::env::current_exe()
            .ok()
            .and_then(|path| path.parent().map(Path::to_path_buf))
            .ok_or_else(|| "the station application directory is unavailable".to_string())?;
        Self::open_with_application_directory(database_path, application_directory)
    }

    pub fn open_with_application_directory(
        database_path: impl AsRef<Path>,
        application_directory: impl AsRef<Path>,
    ) -> Result<Self, String> {
        let database_path = database_path.as_ref().to_path_buf();
        Ok(Self {
            storage: StationStore::open(&database_path)?,
            database_path,
            application_directory: application_directory.as_ref().to_path_buf(),
        })
    }

    pub fn handle(&self, method: &str, path: &str, body: Option<&[u8]>) -> ApiResponse {
        match (method, path) {
            ("GET", path) if path == "/" || path.starts_with("/?") => {
                static_response(200, "text/html; charset=utf-8", OPERATOR_HTML)
            }
            ("GET", "/operator.js") => {
                static_response(200, "text/javascript; charset=utf-8", OPERATOR_SCRIPT)
            }
            ("GET", "/operator.css") => {
                static_response(200, "text/css; charset=utf-8", OPERATOR_STYLES)
            }
            ("GET", "/health") => {
                json_response(200, br#"{"database":"ready","status":"ready"}"#.to_vec())
            }
            ("GET", "/api/v1/station") => match self.storage.read_profile() {
                Ok(Some(profile)) => json_response(
                    200,
                    serde_json::to_vec(&profile).expect("serializing a profile cannot fail"),
                ),
                Ok(None) => error_response(
                    404,
                    "not_commissioned",
                    "The station profile has not been configured.",
                ),
                Err(message) => error_response(500, "storage_error", &message),
            },
            ("PUT", "/api/v1/station") => self.put_profile(body),
            ("POST", "/api/v1/assets/ingest") => self.ingest_media(body),
            ("GET", "/api/v1/assets/readiness-dashboard") => self.readiness_dashboard(),
            ("GET", path) if path.starts_with("/api/v1/assets/missing-scheduled?") => {
                self.missing_scheduled(path)
            }
            ("PUT", "/api/v1/channels") => self.put_channel(body),
            ("GET", "/api/v1/channels") => self.list_channels(),
            ("GET", "/api/v1/runtime-safe-to-air") => self.runtime_safe_to_air(),
            ("GET", "/api/v1/system-resources/latest") => self.latest_system_resources(),
            ("GET", "/api/v1/self-tests") => self.list_self_tests(),
            ("POST", path) if path.starts_with("/api/v1/self-tests/run?") => {
                self.run_self_test(path)
            }
            ("GET", "/api/v1/self-test-schedules") => self.list_self_test_schedules(),
            ("PUT", path) if path.starts_with("/api/v1/self-test-schedules/") => {
                self.put_self_test_schedule(path, body)
            }
            ("POST", "/api/v1/support-bundles") => self.create_support_bundle(),
            ("GET", path)
                if path.starts_with("/api/v1/support-bundles/") && path.ends_with("/download") =>
            {
                self.download_support_bundle(path)
            }
            ("GET", "/api/v1/updates/status") => self.update_status(),
            ("POST", "/api/v1/updates/check") => self.check_updates(),
            ("GET", path) if path.starts_with("/api/v1/updates/release-notes?") => {
                self.update_release_notes(path)
            }
            ("PUT", "/api/v1/alert-channels") => self.put_alert_channel(body),
            ("GET", "/api/v1/alert-channels") => self.list_alert_channels(),
            ("GET", "/api/v1/alert-rules") => self.list_alert_rules(),
            ("PUT", path) if path.starts_with("/api/v1/alert-rules/") => {
                self.put_alert_rule(path, body)
            }
            ("GET", "/api/v1/alert-deliveries") => self.list_alert_deliveries(),
            ("POST", path)
                if path.starts_with("/api/v1/alert-channels/") && path.ends_with("/test") =>
            {
                self.test_alert_channel(path)
            }
            ("GET", "/api/v1/alerts") => self.list_alerts(),
            ("POST", path)
                if path.starts_with("/api/v1/alerts/") && path.ends_with("/acknowledge") =>
            {
                self.acknowledge_alert(path, body)
            }
            ("PUT", "/api/v1/watch-folders") => self.put_watch_folder(body),
            ("GET", "/api/v1/watch-folders") => self.list_watch_folders(),
            ("PUT", "/api/v1/schedule/items") => self.put_schedule_item(body),
            ("POST", "/api/v1/schedule/prepare") => self.prepare_schedule_commit(body),
            ("POST", "/api/v1/schedule/commit") => self.commit_schedule(body),
            ("GET", path) if path.starts_with("/api/v1/schedule/items?") => {
                self.list_schedule(path)
            }
            ("GET", path) if path.starts_with("/api/v1/schedule/commits/") => {
                self.read_commit_report(path)
            }
            ("GET", path) if path.starts_with("/api/v1/assets/ingest/jobs/") => {
                self.read_ingest_job(path)
            }
            ("GET", path) if path.starts_with("/api/v1/assets/transcode/jobs/") => {
                self.read_transcode_job(path)
            }
            ("GET", path)
                if path.starts_with("/api/v1/assets/") && path.ends_with("/readiness") =>
            {
                self.read_asset_readiness(path)
            }
            ("GET", path) if path.starts_with("/api/v1/assets/") => self.read_media_asset(path),
            _ => error_response(404, "not_found", "The requested route does not exist."),
        }
    }

    fn put_profile(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(body) = body else {
            return error_response(400, "invalid_json", "A JSON request body is required.");
        };
        let command: PutStationProfile = match serde_json::from_slice(body) {
            Ok(command) => command,
            Err(_) => {
                return error_response(
                    400,
                    "invalid_json",
                    "The request body is not a station profile.",
                );
            }
        };
        if let Err(message) = command.profile.validate() {
            return error_response(422, "validation_failed", message);
        }
        match self
            .storage
            .write_profile(&command.profile, command.expected_revision)
        {
            Ok(document) => json_response(
                200,
                serde_json::to_vec(&document).expect("serializing a profile cannot fail"),
            ),
            Err(ProfileWriteError::Conflict(current_revision)) => error_response(
                409,
                "revision_conflict",
                &format!(
                    "The station profile changed; expected revision {}, current revision {}.",
                    command.expected_revision, current_revision
                ),
            ),
            Err(ProfileWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn ingest_media(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<IngestMedia>(body) else {
            return error_response(400, "invalid_json", "A media ingest body is required.");
        };
        if !command.source_path.is_absolute() {
            return error_response(
                422,
                "invalid_source_path",
                "The media source path must be absolute.",
            );
        }
        match std::fs::metadata(&command.source_path) {
            Ok(source) if source.is_file() && source.len() > 0 => {}
            Ok(_) => {
                return error_response(
                    422,
                    "invalid_source_path",
                    "The media source must be a non-empty regular file.",
                );
            }
            Err(error) => return error_response(422, "invalid_source_path", &error.to_string()),
        }
        let created_at = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let job = MediaIngestJob {
            job_id: command.job_id,
            source_kind: IngestSourceKind::OperatorPath,
            source_path: command.source_path.to_string_lossy().into_owned(),
            status: IngestJobStatus::Pending,
            progress_percent: 0,
            created_at_unix_ms: created_at,
            started_at_unix_ms: None,
            completed_at_unix_ms: None,
            error_detail: None,
            asset_id: None,
            attempts: 0,
            next_attempt_at_unix_ms: None,
        };
        if let Err(error) = job.validate() {
            return error_response(422, "validation_failed", &format!("{error:?}"));
        }
        match self.storage.create_ingest_job(&job) {
            Ok(true) => json_response(
                202,
                serde_json::to_vec(&job).expect("serializing an ingest job cannot fail"),
            ),
            Ok(false) => error_response(
                409,
                "ingest_job_conflict",
                "An ingest job with this identity already exists.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_ingest_job(&self, path: &str) -> ApiResponse {
        let Some(job_id) = path.strip_prefix("/api/v1/assets/ingest/jobs/") else {
            unreachable!("the route prefix was checked")
        };
        if job_id.is_empty() || job_id.contains('/') {
            return error_response(400, "invalid_path", "The ingest job identity is invalid.");
        }
        match self.storage.read_ingest_job(job_id) {
            Ok(Some(job)) => json_response(
                200,
                serde_json::to_vec(&job).expect("serializing an ingest job cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "ingest_job_not_found",
                "The media ingest job does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_transcode_job(&self, path: &str) -> ApiResponse {
        let Some(job_id) = path.strip_prefix("/api/v1/assets/transcode/jobs/") else {
            unreachable!("the route prefix was checked")
        };
        if job_id.is_empty() || job_id.contains('/') {
            return error_response(
                400,
                "invalid_path",
                "The transcode job identity is invalid.",
            );
        }
        match self.storage.read_transcode_job(job_id) {
            Ok(Some(job)) => json_response(
                200,
                serde_json::to_vec(&job).expect("serializing a transcode job cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "transcode_job_not_found",
                "The media transcode job does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn readiness_dashboard(&self) -> ApiResponse {
        let assets = match self.storage.list_media_assets() {
            Ok(assets) => assets,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let ingest_jobs = match self.storage.list_ingest_jobs() {
            Ok(jobs) => jobs,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let transcode_jobs = match self.storage.list_transcode_jobs() {
            Ok(jobs) => jobs,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let analyses: HashMap<_, _> = match self.storage.list_asset_loudness() {
            Ok(analyses) => analyses
                .into_iter()
                .map(|analysis| (analysis.asset_id.clone(), analysis))
                .collect(),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let by_asset: Vec<_> = assets
            .iter()
            .map(|asset| asset_readiness_view(asset, analyses.get(&asset.asset_id)))
            .collect();
        let dashboard = ReadinessDashboard {
            total_assets: by_asset.len(),
            ready_count: count_readiness(&by_asset, ReadinessState::Ready),
            processing_count: count_readiness(&by_asset, ReadinessState::Processing),
            missing_count: count_readiness(&by_asset, ReadinessState::MissingFile),
            rejected_count: count_readiness(&by_asset, ReadinessState::Rejected),
            pending_ingest_count: ingest_jobs
                .iter()
                .filter(|job| job.status == IngestJobStatus::Pending)
                .count(),
            running_ingest_count: ingest_jobs
                .iter()
                .filter(|job| job.status == IngestJobStatus::Running)
                .count(),
            awaiting_transcode_ingest_count: ingest_jobs
                .iter()
                .filter(|job| job.status == IngestJobStatus::AwaitingTranscode)
                .count(),
            failed_ingest_count: ingest_jobs
                .iter()
                .filter(|job| job.status == IngestJobStatus::Failed)
                .count(),
            pending_transcode_count: transcode_jobs
                .iter()
                .filter(|job| job.status == TranscodeJobStatus::Pending)
                .count(),
            running_transcode_count: transcode_jobs
                .iter()
                .filter(|job| job.status == TranscodeJobStatus::Running)
                .count(),
            failed_transcode_count: transcode_jobs
                .iter()
                .filter(|job| job.status == TranscodeJobStatus::Failed)
                .count(),
            by_asset,
            ingest_jobs,
            transcode_jobs,
        };
        json_response(
            200,
            serde_json::to_vec(&dashboard).expect("serializing readiness cannot fail"),
        )
    }

    fn missing_scheduled(&self, path: &str) -> ApiResponse {
        let Some(query) = path.strip_prefix("/api/v1/assets/missing-scheduled?") else {
            unreachable!("the route prefix was checked")
        };
        let Some((now, horizon)) = parse_window_query(query) else {
            return error_response(
                400,
                "invalid_query",
                "now_unix_ms and horizon_ms are required positive integers.",
            );
        };
        if horizon == 0 || horizon > 31 * 24 * 60 * 60 * 1_000 {
            return error_response(
                422,
                "invalid_horizon",
                "The missing-media horizon must be between 1 ms and 31 days.",
            );
        }
        let Some(end) = now.checked_add(horizon) else {
            return error_response(422, "invalid_horizon", "The schedule window overflows.");
        };
        let items = match self.storage.list_schedule_window(now, end) {
            Ok(items) => items,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let mut alerts = Vec::new();
        for item in items {
            let asset = match self.storage.read_media_asset(&item.asset_id) {
                Ok(asset) => asset,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            let loudness = match self.storage.read_asset_loudness(&item.asset_id) {
                Ok(analysis) => analysis,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            let view = asset
                .as_ref()
                .map(|asset| asset_readiness_view(asset, loudness.as_ref()))
                .unwrap_or_else(|| AssetReadinessView {
                    asset_id: item.asset_id.clone(),
                    readiness_state: ReadinessState::MissingFile,
                    readiness_reason: Some("The asset record does not exist.".into()),
                    media_path: String::new(),
                    duration_ms: 0,
                    loudness_status: LoudnessGateStatus::NotChecked,
                    measured_lufs: None,
                    target_lufs: -24.0,
                    loudness_detail: Some("Loudness has not been checked.".into()),
                });
            if view.readiness_state != ReadinessState::Ready {
                alerts.push(MissingMediaAlert {
                    schedule_item_id: item.item_id,
                    channel_id: item.channel_id,
                    asset_id: item.asset_id,
                    title: item.title,
                    starts_at_unix_ms: item.starts_at_unix_ms,
                    readiness_state: view.readiness_state,
                    readiness_reason: view
                        .readiness_reason
                        .unwrap_or_else(|| "The asset is not ready.".into()),
                });
            }
        }
        json_response(
            200,
            serde_json::to_vec(&alerts).expect("serializing missing-media alerts cannot fail"),
        )
    }

    fn read_media_asset(&self, path: &str) -> ApiResponse {
        let Some(asset_id) = path.strip_prefix("/api/v1/assets/") else {
            unreachable!("the route prefix was checked")
        };
        if asset_id.len() != 64
            || !asset_id
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return error_response(400, "invalid_path", "The asset identity is invalid.");
        }
        match self.storage.read_media_asset(asset_id) {
            Ok(Some(asset)) => json_response(
                200,
                serde_json::to_vec(&asset).expect("serializing a media asset cannot fail"),
            ),
            Ok(None) => error_response(404, "asset_not_found", "The media asset does not exist."),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_asset_readiness(&self, path: &str) -> ApiResponse {
        let Some(asset_id) = path
            .strip_prefix("/api/v1/assets/")
            .and_then(|value| value.strip_suffix("/readiness"))
        else {
            return error_response(400, "invalid_path", "The asset identity is invalid.");
        };
        if asset_id.len() != 64
            || !asset_id
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return error_response(400, "invalid_path", "The asset identity is invalid.");
        }
        let asset = match self.storage.read_media_asset(asset_id) {
            Ok(Some(asset)) => asset,
            Ok(None) => {
                return error_response(404, "asset_not_found", "The media asset does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let loudness = match self.storage.read_asset_loudness(asset_id) {
            Ok(analysis) => analysis,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        json_response(
            200,
            serde_json::to_vec(&asset_readiness_view(&asset, loudness.as_ref()))
                .expect("serializing asset readiness cannot fail"),
        )
    }

    fn put_channel(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(channel) = parse_body::<ChannelConfiguration>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A channel configuration body is required.",
            );
        };
        if let Err(error) = channel.validate() {
            return error_response(422, "validation_failed", &format!("{error:?}"));
        }
        match self.storage.put_channel(&channel) {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&channel)
                    .expect("serializing a channel configuration cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_channels(&self) -> ApiResponse {
        match self.storage.list_channels() {
            Ok(channels) => json_response(
                200,
                serde_json::to_vec(&channels)
                    .expect("serializing channel configurations cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn runtime_safe_to_air(&self) -> ApiResponse {
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let channels = match self.storage.list_channels() {
            Ok(channels) => channels,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let mut runtime_channels = Vec::new();
        for channel in channels.into_iter().filter(|channel| channel.enabled) {
            let observation = match self.storage.read_channel_runtime(&channel.channel_id) {
                Ok(observation) => observation,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            runtime_channels.push((channel.channel_id, channel.display_name, observation));
        }
        let alerts = match self.storage.list_alert_events() {
            Ok(alerts) => alerts,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let active_critical = alerts
            .iter()
            .filter(|event| {
                event.state == AlertEventState::Firing && event.severity == AlertSeverity::Critical
            })
            .count();
        let active_warning = alerts
            .iter()
            .filter(|event| {
                event.state == AlertEventState::Firing && event.severity == AlertSeverity::Warning
            })
            .count();
        match derive_runtime_safe_to_air(now, &runtime_channels, active_critical, active_warning) {
            Ok(status) => json_response(
                200,
                serde_json::to_vec(&status).expect("serializing runtime status cannot fail"),
            ),
            Err(message) => error_response(500, "runtime_state_invalid", message),
        }
    }

    fn list_alerts(&self) -> ApiResponse {
        match self.storage.list_alert_events() {
            Ok(events) => json_response(
                200,
                serde_json::to_vec(&events).expect("serializing alert events cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn latest_system_resources(&self) -> ApiResponse {
        match self.storage.latest_system_resource_sample() {
            Ok(sample) => json_response(
                200,
                serde_json::to_vec(&sample)
                    .expect("serializing the latest system resource sample cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_self_tests(&self) -> ApiResponse {
        match self.storage.list_system_self_tests(100) {
            Ok(records) => json_response(
                200,
                serde_json::to_vec(&records).expect("serializing self-test history cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn run_self_test(&self, path: &str) -> ApiResponse {
        let Some(kind_value) = path.strip_prefix("/api/v1/self-tests/run?kind=") else {
            return error_response(400, "invalid_query", "A self-test kind is required.");
        };
        let kind = match kind_value {
            "daily" => SelfTestKind::Daily,
            "weekly" => SelfTestKind::Weekly,
            _ => {
                return error_response(
                    422,
                    "validation_failed",
                    "Self-test kind must be daily or weekly.",
                );
            }
        };
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match station_alerting::run_system_self_test(
            &self.database_path,
            kind,
            SelfTestTrigger::Manual,
            None,
            now,
        ) {
            Ok(record) => json_response(
                201,
                serde_json::to_vec(&record).expect("serializing a self-test cannot fail"),
            ),
            Err(message) => error_response(500, "self_test_failed", &message),
        }
    }

    fn list_self_test_schedules(&self) -> ApiResponse {
        match self.storage.list_self_test_schedules() {
            Ok(schedules) => json_response(
                200,
                serde_json::to_vec(&schedules)
                    .expect("serializing self-test schedules cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_self_test_schedule(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(kind_value) = path.strip_prefix("/api/v1/self-test-schedules/") else {
            unreachable!("the self-test schedule route was checked")
        };
        let kind = match kind_value {
            "daily" => SelfTestKind::Daily,
            "weekly" => SelfTestKind::Weekly,
            _ => {
                return error_response(
                    404,
                    "self_test_schedule_not_found",
                    "The self-test schedule does not exist.",
                );
            }
        };
        let Some(command) = parse_body::<PutSelfTestSchedule>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A self-test schedule body is required.",
            );
        };
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let schedule = SelfTestSchedule {
            kind,
            enabled: command.enabled,
            local_hour: command.local_hour,
            local_minute: command.local_minute,
            weekday: command.weekday,
            updated_at_unix_ms: now,
            updated_by: command.updated_by,
        };
        if let Err(message) = schedule.validate() {
            return error_response(422, "validation_failed", message);
        }
        match self.storage.put_self_test_schedule(&schedule) {
            Ok(stored) => json_response(
                200,
                serde_json::to_vec(&stored).expect("serializing a self-test schedule cannot fail"),
            ),
            Err(message) => error_response(422, "validation_failed", &message),
        }
    }

    fn create_support_bundle(&self) -> ApiResponse {
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match station_support::create_support_bundle(
            &self.database_path,
            &self.application_directory,
            now,
        ) {
            Ok(record) => json_response(
                201,
                serde_json::to_vec(&record).expect("serializing a support bundle cannot fail"),
            ),
            Err(message) => error_response(500, "support_bundle_failed", &message),
        }
    }

    fn update_status(&self) -> ApiResponse {
        match station_update_coordinator::read_status(
            &self.database_path,
            &self.application_directory,
        ) {
            Ok(status) => json_response(
                200,
                serde_json::to_vec(&status).expect("serializing update status cannot fail"),
            ),
            Err(error) => update_error_response(error),
        }
    }

    fn check_updates(&self) -> ApiResponse {
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match station_update_coordinator::check_now(
            &self.database_path,
            &self.application_directory,
            UpdateCheckTrigger::Manual,
            now,
        ) {
            Ok(record) => json_response(
                200,
                serde_json::to_vec(&record).expect("serializing an update check cannot fail"),
            ),
            Err(error) => update_error_response(error),
        }
    }

    fn update_release_notes(&self, path: &str) -> ApiResponse {
        let Some(version) = path.strip_prefix("/api/v1/updates/release-notes?version=") else {
            return error_response(400, "invalid_query", "A release version is required.");
        };
        if version.contains('&') {
            return error_response(400, "invalid_query", "The release version is invalid.");
        }
        match station_update_coordinator::release_notes(&self.database_path, version) {
            Ok(Some(markdown)) => {
                dynamic_response(200, "text/markdown; charset=utf-8", markdown.into_bytes())
            }
            Ok(None) => error_response(
                404,
                "release_notes_not_found",
                "No verified release notes exist for that version.",
            ),
            Err(error) => update_error_response(error),
        }
    }

    fn download_support_bundle(&self, path: &str) -> ApiResponse {
        let Some(bundle_id) = path
            .strip_prefix("/api/v1/support-bundles/")
            .and_then(|value| value.strip_suffix("/download"))
        else {
            unreachable!("the support bundle download route was checked")
        };
        match station_support::read_support_bundle(&self.database_path, bundle_id) {
            Ok(bytes) => {
                attachment_response(200, "application/zip", format!("{bundle_id}.zip"), bytes)
            }
            Err(message) => error_response(404, "support_bundle_not_found", &message),
        }
    }

    fn put_alert_channel(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<PutAlertChannel>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An alert destination configuration body is required.",
            );
        };
        if let Err(message) = validate_alert_channel_target(command.kind, &command.target) {
            return error_response(422, "validation_failed", message);
        }
        if command.credential.trim() != command.credential
            || command.credential.len() < 8
            || command.credential.len() > 2_000
            || command.credential.chars().any(char::is_control)
        {
            return error_response(
                422,
                "validation_failed",
                "The alert destination credential must contain 8 to 2000 visible characters.",
            );
        }
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let created_at = match self.storage.read_alert_channel(&command.channel_id) {
            Ok(Some(existing)) => existing.created_at_unix_ms,
            Ok(None) => now,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let target_redacted = match redact_alert_channel_target(command.kind, &command.target) {
            Ok(value) => value,
            Err(message) => return error_response(422, "validation_failed", message),
        };
        let channel = AlertChannel {
            channel_id: command.channel_id,
            kind: command.kind,
            label: command.label,
            enabled: command.enabled,
            target_redacted,
            credential_configured: true,
            quiet_hours_start_utc: command.quiet_hours_start_utc,
            quiet_hours_end_utc: command.quiet_hours_end_utc,
            created_at_unix_ms: created_at,
            updated_at_unix_ms: now,
        };
        if let Err(message) = channel.validate() {
            return error_response(422, "validation_failed", message);
        }
        let secret = AlertChannelSecret {
            target: command.target,
            endpoint: command.endpoint,
            sender: command.sender,
            identity: command.identity,
            credential: command.credential,
        };
        if let Err(message) = secret.validate(channel.kind) {
            return error_response(422, "validation_failed", &message);
        }
        match self.storage.put_alert_channel(&channel, &secret) {
            Ok(stored) => json_response(
                200,
                serde_json::to_vec(&stored).expect("serializing an alert channel cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_alert_channels(&self) -> ApiResponse {
        match self.storage.list_alert_channels() {
            Ok(channels) => json_response(
                200,
                serde_json::to_vec(&channels).expect("serializing alert channels cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_alert_rules(&self) -> ApiResponse {
        match self.storage.list_alert_rules() {
            Ok(rules) => json_response(
                200,
                serde_json::to_vec(&rules).expect("serializing alert rules cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_alert_rule(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(rule_id) = path.strip_prefix("/api/v1/alert-rules/") else {
            unreachable!("the alert-rule route was checked")
        };
        if rule_id.is_empty() || rule_id.contains('/') {
            return error_response(400, "invalid_path", "The alert rule identity is invalid.");
        }
        let Some(command) = parse_body::<PutAlertRule>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An alert rule configuration body is required.",
            );
        };
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let rule = AlertRule {
            rule_id: rule_id.into(),
            condition: command.condition,
            enabled: command.enabled,
            severity: command.severity,
            channel_ids: command.channel_ids,
            dedupe_window_seconds: command.dedupe_window_seconds,
            re_alert_after_seconds: command.re_alert_after_seconds,
            scope_channel_id: command.scope_channel_id,
            notify_on_resolve: command.notify_on_resolve,
            updated_at_unix_ms: now,
            updated_by: command.updated_by,
        };
        if let Err(message) = rule.validate() {
            return error_response(422, "validation_failed", message);
        }
        match self.storage.put_alert_rule(&rule) {
            Ok(stored) => json_response(
                200,
                serde_json::to_vec(&stored).expect("serializing an alert rule cannot fail"),
            ),
            Err(message) => error_response(422, "validation_failed", &message),
        }
    }

    fn list_alert_deliveries(&self) -> ApiResponse {
        match self.storage.list_alert_deliveries() {
            Ok(deliveries) => json_response(
                200,
                serde_json::to_vec(&deliveries).expect("serializing alert deliveries cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn test_alert_channel(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path
            .strip_prefix("/api/v1/alert-channels/")
            .and_then(|value| value.strip_suffix("/test"))
        else {
            unreachable!("the alert-channel test route was checked")
        };
        if channel_id.is_empty() || channel_id.contains('/') {
            return error_response(
                400,
                "invalid_path",
                "The alert destination identity is invalid.",
            );
        }
        match self.storage.read_alert_channel(channel_id) {
            Ok(Some(_)) => {}
            Ok(None) => {
                return error_response(
                    404,
                    "alert_channel_not_found",
                    "The alert destination does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let nonce = match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(value) => value.as_nanos(),
            Err(_) => {
                return error_response(
                    500,
                    "clock_error",
                    "The system clock is before the Unix epoch.",
                );
            }
        };
        let event_id = format!("alert-channel-test-{channel_id}-{nonce:x}");
        let delivery_id = format!("alert-delivery-test-{channel_id}-{nonce:x}");
        match self
            .storage
            .enqueue_alert_channel_test(&event_id, &delivery_id, channel_id, now)
        {
            Ok(delivery) => json_response(
                202,
                serde_json::to_vec(&delivery).expect("serializing an alert delivery cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn acknowledge_alert(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(event_id) = path
            .strip_prefix("/api/v1/alerts/")
            .and_then(|value| value.strip_suffix("/acknowledge"))
        else {
            unreachable!("the alert acknowledgment route was checked")
        };
        if event_id.is_empty() || event_id.contains('/') {
            return error_response(400, "invalid_path", "The alert identity is invalid.");
        }
        let Some(command) = parse_body::<AcknowledgeAlert>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An alert acknowledgment body is required.",
            );
        };
        if command.acknowledged_by.trim().is_empty() || command.acknowledged_by.len() > 120 {
            return error_response(
                422,
                "validation_failed",
                "acknowledged_by must contain 1 to 120 visible characters.",
            );
        }
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self
            .storage
            .acknowledge_alert(event_id, now, &command.acknowledged_by)
        {
            Ok(Some(event)) => json_response(
                200,
                serde_json::to_vec(&event).expect("serializing an alert cannot fail"),
            ),
            Ok(None) => error_response(404, "alert_not_found", "The alert event does not exist."),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_watch_folder(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(config) = parse_body::<WatchFolderConfig>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A watch-folder configuration body is required.",
            );
        };
        if !Path::new(&config.monitor_path).is_absolute() {
            return error_response(
                422,
                "invalid_watch_folder",
                "The watch-folder path must be absolute.",
            );
        }
        if let Err(error) = config.validate() {
            return error_response(422, "validation_failed", &format!("{error:?}"));
        }
        if config.last_scan_at_unix_ms.is_some() || config.last_error.is_some() {
            return error_response(
                422,
                "read_only_scan_state",
                "Watch-folder scan status is written only by the station service.",
            );
        }
        match self.storage.put_watch_folder(&config) {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&config)
                    .expect("serializing a watch-folder configuration cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_watch_folders(&self) -> ApiResponse {
        match self.storage.list_watch_folders() {
            Ok(configs) => json_response(
                200,
                serde_json::to_vec(&configs)
                    .expect("serializing watch-folder configurations cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_schedule_item(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(item) = parse_body::<ScheduleItem>(body) else {
            return error_response(400, "invalid_json", "A schedule item body is required.");
        };
        if let Err(error) = item.validate() {
            return error_response(422, "validation_failed", &format!("{error:?}"));
        }
        match self.storage.put_schedule_item(&item) {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&item).expect("serializing a schedule item cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn prepare_schedule_commit(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<PrepareScheduleCommit>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A commit preparation body is required.",
            );
        };
        match self
            .storage
            .prepare_schedule_commit(&command.plan_id, &command.schedule_item_id)
        {
            Ok(plan) => json_response(
                200,
                serde_json::to_vec(&plan).expect("serializing a commit plan cannot fail"),
            ),
            Err(CommitWriteError::NotFound) => error_response(
                404,
                "schedule_item_not_found",
                "The schedule item does not exist.",
            ),
            Err(CommitWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", message)
            }
            Err(CommitWriteError::GateFailed(_)) => unreachable!("prepare returns a plan"),
            Err(CommitWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn commit_schedule(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<CommitSchedule>(body) else {
            return error_response(400, "invalid_json", "A schedule commit body is required.");
        };
        let approved_at = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.commit_schedule(
            &command.report_id,
            &command.plan_id,
            &command.schedule_item_id,
            &command.approved_by,
            approved_at,
            &command.operator_notes,
        ) {
            Ok(report) => json_response(
                201,
                serde_json::to_vec(&report).expect("serializing a commit report cannot fail"),
            ),
            Err(CommitWriteError::NotFound) => error_response(
                404,
                "schedule_item_not_found",
                "The schedule item does not exist.",
            ),
            Err(CommitWriteError::GateFailed(plan)) => {
                let status = if plan.missing_media_detail.is_some() {
                    422
                } else {
                    409
                };
                json_response(
                    status,
                    serde_json::to_vec(&plan).expect("serializing a failed gate cannot fail"),
                )
            }
            Err(CommitWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", message)
            }
            Err(CommitWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn list_schedule(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path.strip_prefix("/api/v1/schedule/items?channel_id=") else {
            return error_response(400, "invalid_query", "channel_id is required.");
        };
        if channel_id.is_empty() || channel_id.contains('&') {
            return error_response(400, "invalid_query", "channel_id is invalid.");
        }
        match self.storage.list_channel_schedule(channel_id) {
            Ok(items) => json_response(
                200,
                serde_json::to_vec(&items).expect("serializing schedule items cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_commit_report(&self, path: &str) -> ApiResponse {
        let Some(report_id) = path.strip_prefix("/api/v1/schedule/commits/") else {
            unreachable!("the route prefix was checked")
        };
        if report_id.is_empty() || report_id.contains('/') {
            return error_response(400, "invalid_path", "The report identity is invalid.");
        }
        match self.storage.read_commit_report(report_id) {
            Ok(Some(report)) => json_response(
                200,
                serde_json::to_vec(&report).expect("serializing a commit report cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "commit_report_not_found",
                "The commit report does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }
}

fn asset_readiness_view(
    asset: &MediaAsset,
    loudness: Option<&AssetLoudnessAnalysis>,
) -> AssetReadinessView {
    let (readiness_state, readiness_reason) = match asset.readiness {
        AssetReadiness::Ready if Path::new(&asset.media_path).is_file() => {
            (ReadinessState::Ready, None)
        }
        AssetReadiness::Ready | AssetReadiness::Missing => (
            ReadinessState::MissingFile,
            Some(format!(
                "The admitted media file is missing: {}",
                asset.media_path
            )),
        ),
        AssetReadiness::Processing => (
            ReadinessState::Processing,
            Some("Media processing has not completed.".into()),
        ),
        AssetReadiness::Rejected => (
            ReadinessState::Rejected,
            Some("Media admission was rejected.".into()),
        ),
    };
    let (loudness_status, measured_lufs, target_lufs, loudness_detail) = loudness.map_or_else(
        || {
            (
                LoudnessGateStatus::NotChecked,
                None,
                -24.0,
                Some("Loudness has not been checked.".into()),
            )
        },
        |analysis| {
            (
                analysis.status,
                analysis
                    .measured_lufs_milli
                    .map(|value| f64::from(value) / 1_000.0),
                f64::from(analysis.target_lufs_milli) / 1_000.0,
                analysis.detail.clone(),
            )
        },
    );
    AssetReadinessView {
        asset_id: asset.asset_id.clone(),
        readiness_state,
        readiness_reason,
        media_path: asset.media_path.clone(),
        duration_ms: asset.duration_ms,
        loudness_status,
        measured_lufs,
        target_lufs,
        loudness_detail,
    }
}

fn count_readiness(assets: &[AssetReadinessView], state: ReadinessState) -> usize {
    assets
        .iter()
        .filter(|asset| asset.readiness_state == state)
        .count()
}

fn parse_window_query(query: &str) -> Option<(i64, i64)> {
    let mut now = None;
    let mut horizon = None;
    for pair in query.split('&') {
        let (name, value) = pair.split_once('=')?;
        let value = value.parse::<i64>().ok()?;
        match name {
            "now_unix_ms" if now.is_none() => now = Some(value),
            "horizon_ms" if horizon.is_none() => horizon = Some(value),
            _ => return None,
        }
    }
    let (now, horizon) = (now?, horizon?);
    (now >= 0 && horizon > 0).then_some((now, horizon))
}

fn parse_body<T: serde::de::DeserializeOwned>(body: Option<&[u8]>) -> Option<T> {
    body.and_then(|body| serde_json::from_slice(body).ok())
}

fn system_time_millis() -> Result<i64, String> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis();
    i64::try_from(millis).map_err(|_| "system time exceeds the supported range".into())
}

pub fn serve_one(listener: TcpListener, database_path: impl AsRef<Path>) -> Result<(), String> {
    let api = Api::open(database_path)?;
    let (mut stream, _) = listener.accept().map_err(|error| error.to_string())?;
    serve_stream(&mut stream, &api)
}

pub fn serve(listener: TcpListener, database_path: impl AsRef<Path>) -> Result<(), String> {
    serve_until(listener, database_path, Arc::new(AtomicBool::new(false)))
}

pub fn serve_until(
    listener: TcpListener,
    database_path: impl AsRef<Path>,
    stop: Arc<AtomicBool>,
) -> Result<(), String> {
    let api = Api::open(database_path)?;
    listener
        .set_nonblocking(true)
        .map_err(|error| error.to_string())?;
    while !stop.load(Ordering::Acquire) {
        match listener.accept() {
            Ok((mut stream, _)) => {
                stream
                    .set_nonblocking(false)
                    .map_err(|error| error.to_string())?;
                serve_stream(&mut stream, &api)?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(error) => return Err(error.to_string()),
        }
    }
    Ok(())
}

fn serve_stream(stream: &mut TcpStream, api: &Api) -> Result<(), String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|error| error.to_string())?;
    let response = match read_http_request(stream) {
        Ok(request) => dispatch_http(api, &request),
        Err(response) => response,
    };
    let reason = match response.status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Content Too Large",
        422 => "Unprocessable Content",
        _ => "Internal Server Error",
    };
    let attachment_header = response
        .attachment_name
        .as_ref()
        .map_or_else(String::new, |name| {
            format!(
                "Content-Disposition: attachment; filename=\"{name}\"\r\n\
                 Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
            )
        });
    let headers = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\n{}Content-Length: {}\r\nConnection: close\r\n\r\n",
        response.status,
        reason,
        response.content_type,
        attachment_header,
        response.body.len()
    );
    stream
        .write_all(headers.as_bytes())
        .map_err(|error| error.to_string())?;
    stream
        .write_all(&response.body)
        .map_err(|error| error.to_string())
}

fn read_http_request(stream: &mut TcpStream) -> Result<Vec<u8>, ApiResponse> {
    let mut request = Vec::new();
    let mut chunk = [0_u8; 4096];
    let mut continue_sent = false;
    loop {
        let count = match stream.read(&mut chunk) {
            Ok(0) => {
                return Err(error_response(
                    400,
                    "malformed_request",
                    "The HTTP request ended before it was complete.",
                ));
            }
            Ok(count) => count,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                ) =>
            {
                return Err(error_response(
                    400,
                    "request_timeout",
                    "The HTTP request was not completed within five seconds.",
                ));
            }
            Err(error) => return Err(error_response(400, "read_failed", &error.to_string())),
        };
        request.extend_from_slice(&chunk[..count]);
        if request.len() > 65_536 {
            return Err(error_response(
                413,
                "request_too_large",
                "Requests may not exceed 64 KiB.",
            ));
        }

        let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") else {
            continue;
        };
        let headers = std::str::from_utf8(&request[..header_end])
            .map_err(|_| error_response(400, "malformed_request", "HTTP headers must be UTF-8."))?;
        let mut content_length = None;
        let mut expects_continue = false;
        for line in headers.split("\r\n").skip(1) {
            let Some((name, value)) = line.split_once(':') else {
                return Err(error_response(
                    400,
                    "malformed_request",
                    "An HTTP header is invalid.",
                ));
            };
            if name.eq_ignore_ascii_case("content-length") {
                if content_length.is_some() {
                    return Err(error_response(
                        400,
                        "malformed_request",
                        "Content-Length may appear only once.",
                    ));
                }
                content_length = Some(value.trim().parse::<usize>().map_err(|_| {
                    error_response(400, "malformed_request", "Content-Length is invalid.")
                })?);
            }
            if name.eq_ignore_ascii_case("expect") {
                if value.trim().eq_ignore_ascii_case("100-continue") {
                    expects_continue = true;
                } else {
                    return Err(error_response(
                        400,
                        "unsupported_expectation",
                        "Only Expect: 100-continue is supported.",
                    ));
                }
            }
        }
        let expected_length = header_end + 4 + content_length.unwrap_or(0);
        if expected_length > 65_536 {
            return Err(error_response(
                413,
                "request_too_large",
                "Requests may not exceed 64 KiB.",
            ));
        }
        if expects_continue && !continue_sent && request.len() < expected_length {
            stream
                .write_all(b"HTTP/1.1 100 Continue\r\n\r\n")
                .map_err(|error| error_response(400, "write_failed", &error.to_string()))?;
            stream
                .flush()
                .map_err(|error| error_response(400, "write_failed", &error.to_string()))?;
            continue_sent = true;
        }
        if request.len() >= expected_length {
            request.truncate(expected_length);
            return Ok(request);
        }
    }
}

fn dispatch_http(api: &Api, request: &[u8]) -> ApiResponse {
    let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") else {
        return error_response(400, "malformed_request", "The HTTP headers are incomplete.");
    };
    let headers = match std::str::from_utf8(&request[..header_end]) {
        Ok(headers) => headers,
        Err(_) => return error_response(400, "malformed_request", "HTTP headers must be UTF-8."),
    };
    let mut lines = headers.split("\r\n");
    let Some(request_line) = lines.next() else {
        return error_response(400, "malformed_request", "The request line is missing.");
    };
    let mut request_parts = request_line.split_whitespace();
    let (Some(method), Some(path), Some(version), None) = (
        request_parts.next(),
        request_parts.next(),
        request_parts.next(),
        request_parts.next(),
    ) else {
        return error_response(400, "malformed_request", "The request line is invalid.");
    };
    if version != "HTTP/1.1" {
        return error_response(400, "malformed_request", "Only HTTP/1.1 is supported.");
    }

    let mut content_length = 0usize;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return error_response(400, "malformed_request", "An HTTP header is invalid.");
        };
        if name.eq_ignore_ascii_case("content-length") {
            content_length = match value.trim().parse() {
                Ok(length) => length,
                Err(_) => {
                    return error_response(400, "malformed_request", "Content-Length is invalid.");
                }
            };
        }
    }
    let body = &request[header_end + 4..];
    if body.len() != content_length {
        return error_response(
            400,
            "malformed_request",
            "Content-Length does not match the request body.",
        );
    }
    api.handle(method, path, (!body.is_empty()).then_some(body))
}

fn json_response(status: u16, body: Vec<u8>) -> ApiResponse {
    ApiResponse {
        status,
        content_type: "application/json",
        attachment_name: None,
        body,
    }
}

fn static_response(status: u16, content_type: &'static str, body: &[u8]) -> ApiResponse {
    ApiResponse {
        status,
        content_type,
        attachment_name: None,
        body: body.to_vec(),
    }
}

fn dynamic_response(status: u16, content_type: &'static str, body: Vec<u8>) -> ApiResponse {
    ApiResponse {
        status,
        content_type,
        attachment_name: None,
        body,
    }
}

fn update_error_response(error: CoordinatorError) -> ApiResponse {
    match error {
        CoordinatorError::NotConfigured => error_response(
            409,
            "updates_not_configured",
            "No signed TownLight Station release source is installed on this station.",
        ),
        CoordinatorError::Fault(message) => error_response(500, "update_status_failed", &message),
    }
}

fn attachment_response(
    status: u16,
    content_type: &'static str,
    attachment_name: String,
    body: Vec<u8>,
) -> ApiResponse {
    debug_assert!(
        !attachment_name.is_empty()
            && attachment_name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
    );
    ApiResponse {
        status,
        content_type,
        attachment_name: Some(attachment_name),
        body,
    }
}

fn error_response(status: u16, code: &str, message: &str) -> ApiResponse {
    json_response(
        status,
        serde_json::to_vec(&serde_json::json!({ "error": { "code": code, "message": message } }))
            .expect("serializing an error response cannot fail"),
    )
}
