use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use station_cg::{
    CgAuditEvent, CgBoard, CgBulletin, CgFeedItem, CgFeedItemApproval, CgFeedSource, CgZoneConfig,
    built_in_templates,
};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertEventState, AlertRule, AlertSeverity,
    SafeToAirColor, SelfTestKind, SelfTestSchedule, SelfTestTrigger, StationProfile,
    derive_runtime_safe_to_air, redact_alert_channel_target, validate_alert_channel_target,
};
use station_media_lifecycle::{
    AssetLoudnessAnalysis, AssetReadinessView, IngestJobStatus, IngestSourceKind,
    LoudnessGateStatus, MediaIngestJob, MissingMediaAlert, ReadinessDashboard, ReadinessState,
    TranscodeJobStatus, WatchFolderConfig,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, DispatchStatus, MediaAsset, OUTPUT_PROFILES,
    ScheduleItem, ScheduleState,
};
use station_storage::{
    AlertChannelSecret, CgSnapshotReadError, CgWriteError, CommitWriteError, ProfileWriteError,
    StationStore,
};
use station_update::UpdateCheckTrigger;
use station_update_coordinator::CoordinatorError;

const OPERATOR_HTML: &[u8] = include_bytes!("../../../web/operator/dist/index.html");
const OPERATOR_SCRIPT: &[u8] = include_bytes!("../../../web/operator/dist/operator.js");
const OPERATOR_STYLES: &[u8] = include_bytes!("../../../web/operator/dist/operator.css");
const MAX_CONCURRENT_REQUESTS: usize = 16;
const MAX_SMALL_REQUEST_BODY: usize = 65_536;
const MAX_CG_IMAGE_BODY: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum CommissioningCheckStatus {
    Pass,
    Warning,
    Blocked,
    NotVerified,
}

#[derive(Debug, Clone, Serialize)]
struct CommissioningCheck {
    id: &'static str,
    label: &'static str,
    status: CommissioningCheckStatus,
    blocking: bool,
    detail: String,
    evidence: String,
}

#[derive(Debug, Clone, Serialize)]
struct CommissioningReport {
    generated_at_unix_ms: i64,
    overall_status: CommissioningCheckStatus,
    checks: Vec<CommissioningCheck>,
}

#[derive(Deserialize)]
struct PutStationProfile {
    #[serde(flatten)]
    profile: StationProfile,
    #[serde(default)]
    expected_revision: u64,
}

#[derive(Deserialize)]
struct PutChannel {
    #[serde(flatten)]
    channel: ChannelConfiguration,
    #[serde(default)]
    srt_passphrases: HashMap<String, String>,
    #[serde(default)]
    clear_srt_passphrases: Vec<String>,
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

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ApplyUpdate {
    requested_by: String,
    confirmation: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WriteCgBoard {
    board: CgBoard,
    expected_revision: u64,
    audit: CgAuditEvent,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WriteCgZone {
    zone: CgZoneConfig,
    expected_revision: u64,
    audit: CgAuditEvent,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeleteCgResource {
    expected_revision: u64,
    audit: CgAuditEvent,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WriteCgFeedSource {
    feed: CgFeedSource,
    expected_revision: u64,
    audit: CgAuditEvent,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WriteCgBulletin {
    bulletin: CgBulletin,
    expected_revision: u64,
    audit: CgAuditEvent,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExpireCgBulletins {
    channel_id: String,
    now_unix_ms: i64,
    operator_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ApproveCgFeedItem {
    approval: CgFeedItemApproval,
    audit: CgAuditEvent,
}

#[derive(Debug, Clone, Serialize)]
struct CgFeedItemRead {
    item_id: String,
    title: String,
    summary: Option<String>,
    starts_at_unix_ms: Option<i64>,
    url: Option<String>,
    tags: Vec<String>,
    approved: bool,
    approved_by: Option<String>,
    approved_at_unix_ms: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
struct CgFeedRead {
    feed_source_id: String,
    label: String,
    kind: station_cg::CgFeedKind,
    source_url: String,
    trust_tier: station_cg::CgFeedTrustTier,
    tags: Vec<String>,
    enabled: bool,
    refresh_seconds: u32,
    last_fetched_at_unix_ms: Option<i64>,
    last_fetch_error: Option<String>,
    cache_fetched_at_unix_ms: Option<i64>,
    cache_expires_at_unix_ms: Option<i64>,
    cache_last_error: Option<String>,
    stale: bool,
    never_fetched: bool,
    items: Vec<CgFeedItemRead>,
}

#[derive(Debug, Clone, Serialize)]
struct CgFeedItemsRead {
    channel_id: String,
    as_of_unix_ms: i64,
    feeds: Vec<CgFeedRead>,
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
            ("GET", "/api/v1/commissioning/report") => self.commissioning_report(),
            ("PUT", "/api/v1/station") => self.put_profile(body),
            ("POST", "/api/v1/assets/ingest") => self.ingest_media(body),
            ("GET", "/api/v1/assets/readiness-dashboard") => self.readiness_dashboard(),
            ("GET", path) if path.starts_with("/api/v1/assets/missing-scheduled?") => {
                self.missing_scheduled(path)
            }
            ("PUT", "/api/v1/channels") => self.put_channel(body),
            ("GET", "/api/v1/channels") => self.list_channels(),
            ("GET", "/api/v1/output-profiles") => json_response(
                200,
                serde_json::to_vec(&OUTPUT_PROFILES)
                    .expect("serializing output profiles cannot fail"),
            ),
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
            ("POST", "/api/v1/updates/stage") => self.stage_update(),
            ("POST", "/api/v1/updates/prepare") => self.prepare_update(),
            ("POST", "/api/v1/updates/apply") => self.apply_update(body),
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
            ("GET", "/api/v1/cg/templates") => json_response(
                200,
                serde_json::to_vec(&built_in_templates())
                    .expect("serializing built-in CG templates cannot fail"),
            ),
            ("GET", "/api/v1/cg/images") => self.list_cg_images(),
            ("POST", "/api/v1/cg/images") => self.upload_cg_image(body),
            ("GET", path) if path.starts_with("/api/v1/cg/board?") => self.read_cg_board(path),
            ("GET", path)
                if path == "/api/v1/cg/preview" || path.starts_with("/api/v1/cg/preview?") =>
            {
                self.read_cg_snapshot(path, "/api/v1/cg/preview", true)
            }
            ("GET", path)
                if path == "/api/v1/cg/snapshot" || path.starts_with("/api/v1/cg/snapshot?") =>
            {
                self.read_cg_snapshot(path, "/api/v1/cg/snapshot", false)
            }
            ("PUT", "/api/v1/cg/board") => self.write_cg_board(body),
            ("GET", path) if path.starts_with("/api/v1/cg/zones?") => self.list_cg_zones(path),
            ("PUT", "/api/v1/cg/zones") => self.write_cg_zone(body),
            ("DELETE", path) if path.starts_with("/api/v1/cg/zones/") => {
                self.delete_cg_zone(path, body)
            }
            ("GET", path) if path.starts_with("/api/v1/cg/feeds?") => self.list_cg_feeds(path),
            ("PUT", "/api/v1/cg/feeds") => self.write_cg_feed(body),
            ("DELETE", path) if path.starts_with("/api/v1/cg/feeds/") => {
                self.delete_cg_feed(path, body)
            }
            ("GET", path) if path.starts_with("/api/v1/cg/feed-items?") => {
                self.list_cg_feed_items(path)
            }
            ("POST", "/api/v1/cg/feed-items/approve") => self.approve_cg_feed_item(body),
            ("GET", path) if path.starts_with("/api/v1/cg/bulletins?") => {
                self.list_cg_bulletins(path)
            }
            ("PUT", "/api/v1/cg/bulletins") => self.write_cg_bulletin(body),
            ("POST", "/api/v1/cg/bulletins/expire") => self.expire_cg_bulletins(body),
            ("GET", path) if path.starts_with("/api/v1/cg/audit?") => self.list_cg_audit(path),
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
        let Some(mut command) = parse_body::<PutChannel>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A channel configuration body is required.",
            );
        };
        for (sink_id, passphrase) in &command.srt_passphrases {
            if !(10..=79).contains(&passphrase.len())
                || !passphrase.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
            {
                return error_response(
                    422,
                    "validation_failed",
                    "SRT passphrases must contain 10 to 79 printable ASCII characters without spaces.",
                );
            }
            let Some(sink) = command.channel.output_sinks.iter_mut().find(|sink| {
                sink.sink_id == *sink_id && sink.kind == station_schedule::OutputSinkKind::Srt
            }) else {
                return error_response(
                    422,
                    "validation_failed",
                    "Every SRT passphrase must identify an SRT output sink in this channel.",
                );
            };
            sink.encryption_configured = true;
        }
        let existing = match self.storage.list_channels() {
            Ok(channels) => channels
                .into_iter()
                .find(|existing| existing.channel_id == command.channel.channel_id),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        for sink_id in &command.clear_srt_passphrases {
            if command.srt_passphrases.contains_key(sink_id) {
                return error_response(
                    422,
                    "validation_failed",
                    "An SRT passphrase cannot be replaced and cleared in the same request.",
                );
            }
            let Some(sink) = command.channel.output_sinks.iter_mut().find(|sink| {
                sink.sink_id == *sink_id && sink.kind == station_schedule::OutputSinkKind::Srt
            }) else {
                return error_response(
                    422,
                    "validation_failed",
                    "Every cleared SRT passphrase must identify an SRT output sink.",
                );
            };
            sink.encryption_configured = false;
        }
        if let Some(existing) = &existing {
            for sink in &mut command.channel.output_sinks {
                if !command.clear_srt_passphrases.contains(&sink.sink_id)
                    && existing
                        .output_sinks
                        .iter()
                        .any(|prior| prior.sink_id == sink.sink_id && prior.encryption_configured)
                {
                    sink.encryption_configured = true;
                }
            }
        }
        let channel = command.channel;
        if let Err(error) = channel.validate() {
            return error_response(422, "validation_failed", &format!("{error:?}"));
        }
        if existing.as_ref().is_some_and(|existing| {
            existing.enabled
                && (existing.output_sinks != channel.output_sinks
                    || existing.output_profile != channel.output_profile
                    || !command.srt_passphrases.is_empty()
                    || !command.clear_srt_passphrases.is_empty())
        }) {
            return error_response(
                409,
                "channel_must_be_stopped",
                "Stop this output before changing its destination or output profile.",
            );
        }
        if channel.output_sinks.iter().any(|sink| {
            sink.encryption_configured
                && !command.srt_passphrases.contains_key(&sink.sink_id)
                && !existing.as_ref().is_some_and(|existing| {
                    existing
                        .output_sinks
                        .iter()
                        .any(|prior| prior.sink_id == sink.sink_id && prior.encryption_configured)
                })
        }) {
            return error_response(
                422,
                "srt_passphrase_required",
                "A new encrypted SRT output requires its write-only passphrase.",
            );
        }
        match self
            .storage
            .put_channel_with_srt_passphrases(&channel, &command.srt_passphrases)
        {
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

    fn commissioning_report(&self) -> ApiResponse {
        let generated_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let mut checks = Vec::new();

        let profile = match self.storage.read_profile() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        checks.push(match profile {
            Some(profile) => CommissioningCheck {
                id: "station_profile",
                label: "Station profile commissioned",
                status: CommissioningCheckStatus::Pass,
                blocking: false,
                detail: format!(
                    "{} (revision {})",
                    profile.profile.display_name, profile.revision
                ),
                evidence: "authoritative station profile in SQLite".into(),
            },
            None => CommissioningCheck {
                id: "station_profile",
                label: "Station profile commissioned",
                status: CommissioningCheckStatus::Blocked,
                blocking: true,
                detail: "No station profile has been commissioned.".into(),
                evidence: "GET /api/v1/station returned no profile".into(),
            },
        });

        checks.push(CommissioningCheck {
            id: "runtime_api",
            label: "Station runtime is serving authoritative state",
            status: CommissioningCheckStatus::Pass,
            blocking: false,
            detail: "The station API and SQLite authority are serving this report.".into(),
            evidence: "current stationd request".into(),
        });

        let channels = match self.storage.list_channels() {
            Ok(channels) => channels,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let enabled: Vec<_> = channels.iter().filter(|channel| channel.enabled).collect();
        if enabled.is_empty() {
            checks.push(CommissioningCheck {
                id: "enabled_channel",
                label: "Enabled channel reaches safe-to-air runtime",
                status: CommissioningCheckStatus::Blocked,
                blocking: true,
                detail: "No enabled output channel is configured.".into(),
                evidence: "authoritative channel configuration".into(),
            });
        } else {
            let mut runtime_channels = Vec::with_capacity(enabled.len());
            for channel in enabled {
                let observation = match self.storage.read_channel_runtime(&channel.channel_id) {
                    Ok(observation) => observation,
                    Err(message) => return error_response(500, "storage_error", &message),
                };
                runtime_channels.push((
                    channel.channel_id.clone(),
                    channel.display_name.clone(),
                    observation,
                ));
            }
            let alerts = match self.storage.list_alert_events() {
                Ok(alerts) => alerts,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            let active_critical = alerts
                .iter()
                .filter(|event| {
                    event.state == AlertEventState::Firing
                        && event.severity == AlertSeverity::Critical
                })
                .count();
            let active_warning = alerts
                .iter()
                .filter(|event| {
                    event.state == AlertEventState::Firing
                        && event.severity == AlertSeverity::Warning
                })
                .count();
            let safe_to_air = match derive_runtime_safe_to_air(
                generated_at_unix_ms,
                &runtime_channels,
                active_critical,
                active_warning,
            ) {
                Ok(status) => status,
                Err(message) => return error_response(500, "runtime_state_invalid", message),
            };
            let status = match safe_to_air.color {
                SafeToAirColor::Green => CommissioningCheckStatus::Pass,
                SafeToAirColor::Yellow => CommissioningCheckStatus::Warning,
                SafeToAirColor::Red => CommissioningCheckStatus::Blocked,
            };
            checks.push(CommissioningCheck {
                id: "enabled_channel",
                label: "Enabled channel reaches safe-to-air runtime",
                blocking: status == CommissioningCheckStatus::Blocked,
                status,
                detail: safe_to_air.operator_message,
                evidence: format!(
                    "current heartbeat-derived runtime status; {} critical and {} warning alert(s)",
                    safe_to_air.active_critical_alerts, safe_to_air.active_warning_alerts
                ),
            });
        }

        let assets = match self.storage.list_media_assets() {
            Ok(assets) => assets,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let ready_assets = assets
            .iter()
            .filter(|asset| {
                asset.readiness == AssetReadiness::Ready && Path::new(&asset.media_path).is_file()
            })
            .count();
        checks.push(if ready_assets > 0 {
            CommissioningCheck {
                id: "ready_media",
                label: "At least one air-ready media asset exists",
                status: CommissioningCheckStatus::Pass,
                blocking: false,
                detail: format!("{ready_assets} ready asset(s) have present files."),
                evidence: "current media catalog and filesystem presence".into(),
            }
        } else {
            CommissioningCheck {
                id: "ready_media",
                label: "At least one air-ready media asset exists",
                status: CommissioningCheckStatus::Blocked,
                blocking: true,
                detail: "No ready media asset with a present file is available.".into(),
                evidence: "current media catalog and filesystem presence".into(),
            }
        });

        let mut committed = 0usize;
        let mut reports = Vec::new();
        for channel in &channels {
            let schedule = match self.storage.list_channel_schedule(&channel.channel_id) {
                Ok(schedule) => schedule,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            committed += schedule
                .iter()
                .filter(|item| item.state == ScheduleState::Committed)
                .count();
            let channel_reports = match self.storage.list_commit_reports(&channel.channel_id) {
                Ok(reports) => reports,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            reports.extend(channel_reports);
        }
        checks.push(if committed > 0 {
            CommissioningCheck {
                id: "schedule_authority",
                label: "Committed scheduling authority exists",
                status: CommissioningCheckStatus::Pass,
                blocking: false,
                detail: format!("{committed} committed schedule item(s) are recorded."),
                evidence: "authoritative channel schedule rows".into(),
            }
        } else {
            CommissioningCheck {
                id: "schedule_authority",
                label: "Committed scheduling authority exists",
                status: CommissioningCheckStatus::Warning,
                blocking: false,
                detail: "No committed schedule item is recorded yet.".into(),
                evidence: "authoritative channel schedule rows".into(),
            }
        });

        let completed = reports
            .iter()
            .filter(|report| report.dispatch_status == DispatchStatus::Completed)
            .count();
        let acknowledged = reports
            .iter()
            .filter(|report| report.dispatch_status == DispatchStatus::Acknowledged)
            .count();
        checks.push(CommissioningCheck {
            id: "dispatch_execution",
            label: "Dispatch execution evidence exists",
            status: if completed > 0 {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Warning
            },
            blocking: false,
            detail: if reports.is_empty() {
                "No commit report is recorded; dispatch execution is not verified.".into()
            } else {
                format!(
                    "{} commit report(s), including {} acknowledged and {} completed.",
                    reports.len(),
                    acknowledged,
                    completed
                )
            },
            evidence: "authoritative commit_reports rows, distinct from schedule authority".into(),
        });

        checks.push(CommissioningCheck {
            id: "installed_field_proof",
            label: "Installed and field proof",
            status: CommissioningCheckStatus::NotVerified,
            blocking: false,
            detail: "This report does not claim clean-machine installation, external receiver proof, or one-/three-station field operation.".into(),
            evidence: "no authoritative field-proof record is stored by the current control plane".into(),
        });

        let overall_status = if checks.iter().any(|check| check.blocking) {
            CommissioningCheckStatus::Blocked
        } else if checks.iter().any(|check| {
            matches!(
                check.status,
                CommissioningCheckStatus::Warning | CommissioningCheckStatus::NotVerified
            )
        }) {
            CommissioningCheckStatus::Warning
        } else {
            CommissioningCheckStatus::Pass
        };
        json_response(
            200,
            serde_json::to_vec(&CommissioningReport {
                generated_at_unix_ms,
                overall_status,
                checks,
            })
            .expect("serializing commissioning report cannot fail"),
        )
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

    fn stage_update(&self) -> ApiResponse {
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match station_update_coordinator::stage_available_release(
            &self.database_path,
            &self.application_directory,
            now,
        ) {
            Ok(record) => json_response(
                201,
                serde_json::to_vec(&record).expect("serializing a staged update cannot fail"),
            ),
            Err(error) => update_error_response(error),
        }
    }

    fn prepare_update(&self) -> ApiResponse {
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match station_update_coordinator::prepare_staged_release(
            &self.database_path,
            &self.application_directory,
            now,
        ) {
            Ok(record) => json_response(
                201,
                serde_json::to_vec(&record).expect("serializing a prepared slot cannot fail"),
            ),
            Err(error) => update_error_response(error),
        }
    }

    fn apply_update(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<ApplyUpdate>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An update-application authorization body is required.",
            );
        };
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match station_update_coordinator::request_maintenance_activation(
            &self.database_path,
            &self.application_directory,
            &command.requested_by,
            &command.confirmation,
            now,
        ) {
            Ok(request) => json_response(
                202,
                serde_json::to_vec(&request)
                    .expect("serializing a maintenance request cannot fail"),
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

    fn read_cg_board(&self, path: &str) -> ApiResponse {
        let Some(query) = cg_query(path, "/api/v1/cg/board?") else {
            return error_response(400, "invalid_query", "The CG board query is invalid.");
        };
        let Some(channel_id) = exact_cg_query_parameter(&query, "channel_id") else {
            return error_response(400, "invalid_query", "channel_id is required.");
        };
        match self.storage.read_cg_board(channel_id) {
            Ok(Some(board)) => json_response(
                200,
                serde_json::to_vec(&board).expect("serializing a CG board cannot fail"),
            ),
            Ok(None) => error_response(404, "cg_board_not_found", "The CG board does not exist."),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_cg_snapshot(&self, path: &str, prefix: &str, require_at: bool) -> ApiResponse {
        let query_prefix = format!("{prefix}?");
        let Some(query) = cg_query(path, &query_prefix) else {
            return error_response(400, "invalid_query", "The CG snapshot query is invalid.");
        };
        let expected_parameter_count = if query.contains_key("at_unix_ms") {
            2
        } else {
            1
        };
        if query.len() != expected_parameter_count
            || (require_at && !query.contains_key("at_unix_ms"))
        {
            return error_response(400, "invalid_query", "channel_id is required.");
        }
        let Some(channel_id) = query.get("channel_id").map(String::as_str) else {
            return error_response(400, "invalid_query", "channel_id is required.");
        };
        let now = match query.get("at_unix_ms") {
            Some(value) => match value.parse::<i64>() {
                Ok(value) if value >= 0 => value,
                _ => {
                    return error_response(
                        400,
                        "invalid_query",
                        "at_unix_ms must be a nonnegative integer.",
                    );
                }
            },
            None => match system_time_millis() {
                Ok(value) => value,
                Err(message) => return error_response(500, "clock_error", &message),
            },
        };
        match self.storage.load_cg_snapshot_inputs(channel_id, now) {
            Ok(inputs) => match inputs.resolve() {
                Ok(snapshot) => json_response(
                    200,
                    serde_json::to_vec(&snapshot).expect("serializing a CG snapshot cannot fail"),
                ),
                Err(error) => error_response(422, "cg_snapshot_invalid", &format!("{error:?}")),
            },
            Err(CgSnapshotReadError::InvalidChannel | CgSnapshotReadError::InvalidTimestamp) => {
                error_response(400, "invalid_query", "The CG snapshot query is invalid.")
            }
            Err(CgSnapshotReadError::ChannelNotFound) => {
                error_response(404, "cg_channel_not_found", "The channel does not exist.")
            }
            Err(CgSnapshotReadError::BoardNotFound) => {
                error_response(404, "cg_board_not_found", "The CG board does not exist.")
            }
            Err(CgSnapshotReadError::BoardInactive) => {
                error_response(409, "cg_board_inactive", "The CG board is inactive.")
            }
            Err(CgSnapshotReadError::InvalidData(message)) => {
                error_response(422, "cg_snapshot_invalid", &message)
            }
            Err(CgSnapshotReadError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn write_cg_board(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<WriteCgBoard>(body) else {
            return error_response(400, "invalid_json", "A CG board write body is required.");
        };
        cg_write_response(self.storage.write_cg_board(
            &command.board,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn list_cg_zones(&self, path: &str) -> ApiResponse {
        let Some(query) = cg_query(path, "/api/v1/cg/zones?") else {
            return error_response(400, "invalid_query", "The CG zone query is invalid.");
        };
        let Some(board_id) = exact_cg_query_parameter(&query, "board_id") else {
            return error_response(400, "invalid_query", "board_id is required.");
        };
        match self.storage.list_cg_zones(board_id) {
            Ok(zones) => json_response(
                200,
                serde_json::to_vec(&zones).expect("serializing CG zones cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn write_cg_zone(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<WriteCgZone>(body) else {
            return error_response(400, "invalid_json", "A CG zone write body is required.");
        };
        cg_write_response(self.storage.write_cg_zone(
            &command.zone,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn delete_cg_zone(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(zone_id) = cg_resource_id(path, "/api/v1/cg/zones/") else {
            return error_response(400, "invalid_path", "The CG zone identity is invalid.");
        };
        let Some(command) = parse_body::<DeleteCgResource>(body) else {
            return error_response(400, "invalid_json", "A CG zone deletion body is required.");
        };
        cg_delete_response(self.storage.delete_cg_zone(
            zone_id,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn list_cg_feeds(&self, path: &str) -> ApiResponse {
        let Some(query) = cg_query(path, "/api/v1/cg/feeds?") else {
            return error_response(400, "invalid_query", "The CG feed query is invalid.");
        };
        let Some(channel_id) = exact_cg_query_parameter(&query, "channel_id") else {
            return error_response(400, "invalid_query", "channel_id is required.");
        };
        match self.storage.list_cg_feed_sources(channel_id) {
            Ok(feeds) => json_response(
                200,
                serde_json::to_vec(&feeds).expect("serializing CG feeds cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_cg_feed_items(&self, path: &str) -> ApiResponse {
        let Some(query) = cg_query(path, "/api/v1/cg/feed-items?") else {
            return error_response(400, "invalid_query", "The CG feed-item query is invalid.");
        };
        if query.len() > 2 || !query.contains_key("channel_id") {
            return error_response(400, "invalid_query", "channel_id is required.");
        }
        let Some(channel_id) = query.get("channel_id").map(String::as_str) else {
            return error_response(400, "invalid_query", "channel_id is required.");
        };
        let as_of_unix_ms = match query.get("at_unix_ms") {
            Some(value) => match value.parse::<i64>() {
                Ok(value) if value >= 0 => value,
                _ => {
                    return error_response(
                        400,
                        "invalid_query",
                        "at_unix_ms must be a nonnegative integer.",
                    );
                }
            },
            None => match system_time_millis() {
                Ok(value) => value,
                Err(message) => return error_response(500, "clock_error", &message),
            },
        };
        if query
            .keys()
            .any(|key| key != "channel_id" && key != "at_unix_ms")
        {
            return error_response(400, "invalid_query", "The CG feed-item query is invalid.");
        }

        let channel_exists = match self.storage.list_channels() {
            Ok(channels) => channels
                .iter()
                .any(|channel| channel.channel_id == channel_id),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if !channel_exists {
            return error_response(404, "cg_channel_not_found", "The channel does not exist.");
        }

        let feeds = match self.storage.list_cg_feed_sources(channel_id) {
            Ok(feeds) => feeds,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let mut read_feeds = Vec::with_capacity(feeds.len());
        for feed in feeds {
            let cache = match self.storage.read_cg_feed_cache(&feed.feed_source_id) {
                Ok(cache) => cache,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            let approvals = match self
                .storage
                .list_cg_feed_item_approvals(&feed.feed_source_id)
            {
                Ok(approvals) => approvals,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            let approvals = approvals
                .into_iter()
                .map(|approval| (approval.item_id.clone(), approval))
                .collect::<std::collections::BTreeMap<_, _>>();
            let stale = cache
                .as_ref()
                .is_some_and(|cache| cache.is_stale_at(as_of_unix_ms));
            let never_fetched = cache.is_none() && feed.last_fetched_at_unix_ms.is_none();
            let items = cache
                .as_ref()
                .map(|cache| {
                    cache
                        .items
                        .iter()
                        .map(|item: &CgFeedItem| {
                            let approval = approvals.get(&item.item_id);
                            CgFeedItemRead {
                                item_id: item.item_id.clone(),
                                title: item.title.clone(),
                                summary: item.summary.clone(),
                                starts_at_unix_ms: item.starts_at_unix_ms,
                                url: item.url.clone(),
                                tags: item.tags.clone(),
                                approved: approval.is_some(),
                                approved_by: approval.map(|value| value.approved_by.clone()),
                                approved_at_unix_ms: approval
                                    .map(|value| value.approved_at_unix_ms),
                            }
                        })
                        .collect()
                })
                .unwrap_or_default();
            read_feeds.push(CgFeedRead {
                feed_source_id: feed.feed_source_id,
                label: feed.label,
                kind: feed.kind,
                source_url: feed.source_url,
                trust_tier: feed.trust_tier,
                tags: feed.tags,
                enabled: feed.enabled,
                refresh_seconds: feed.refresh_seconds,
                last_fetched_at_unix_ms: feed.last_fetched_at_unix_ms,
                last_fetch_error: feed.last_fetch_error,
                cache_fetched_at_unix_ms: cache.as_ref().map(|value| value.fetched_at_unix_ms),
                cache_expires_at_unix_ms: cache.as_ref().map(|value| value.expires_at_unix_ms),
                cache_last_error: cache.and_then(|value| value.last_error),
                stale,
                never_fetched,
                items,
            });
        }
        json_response(
            200,
            serde_json::to_vec(&CgFeedItemsRead {
                channel_id: channel_id.to_string(),
                as_of_unix_ms,
                feeds: read_feeds,
            })
            .expect("serializing CG feed-item reads cannot fail"),
        )
    }

    fn write_cg_feed(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<WriteCgFeedSource>(body) else {
            return error_response(400, "invalid_json", "A CG feed write body is required.");
        };
        cg_write_response(self.storage.write_cg_feed_source(
            &command.feed,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn delete_cg_feed(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(feed_id) = cg_resource_id(path, "/api/v1/cg/feeds/") else {
            return error_response(400, "invalid_path", "The CG feed identity is invalid.");
        };
        let Some(command) = parse_body::<DeleteCgResource>(body) else {
            return error_response(400, "invalid_json", "A CG feed deletion body is required.");
        };
        cg_delete_response(self.storage.delete_cg_feed_source(
            feed_id,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn approve_cg_feed_item(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<ApproveCgFeedItem>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A CG feed-item approval body is required.",
            );
        };
        cg_write_response(
            self.storage
                .approve_cg_feed_item(&command.approval, &command.audit),
        )
    }

    fn list_cg_bulletins(&self, path: &str) -> ApiResponse {
        let Some(query) = cg_query(path, "/api/v1/cg/bulletins?") else {
            return error_response(400, "invalid_query", "The CG bulletin query is invalid.");
        };
        if query
            .keys()
            .any(|key| !matches!(key.as_str(), "channel_id" | "airable_at_unix_ms"))
        {
            return error_response(400, "invalid_query", "The CG bulletin query is invalid.");
        }
        let Some(channel_id) = query.get("channel_id").map(String::as_str) else {
            return error_response(400, "invalid_query", "channel_id is required.");
        };
        let result = match query.get("airable_at_unix_ms") {
            Some(value) => match value.parse::<i64>() {
                Ok(now) => self.storage.list_airable_cg_bulletins(channel_id, now),
                Err(_) => {
                    return error_response(
                        400,
                        "invalid_query",
                        "airable_at_unix_ms must be an integer.",
                    );
                }
            },
            None => self.storage.list_cg_bulletins(channel_id),
        };
        match result {
            Ok(bulletins) => json_response(
                200,
                serde_json::to_vec(&bulletins).expect("serializing CG bulletins cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn write_cg_bulletin(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<WriteCgBulletin>(body) else {
            return error_response(400, "invalid_json", "A CG bulletin write body is required.");
        };
        cg_write_response(self.storage.write_cg_bulletin(
            &command.bulletin,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn expire_cg_bulletins(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<ExpireCgBulletins>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A CG bulletin-expiration command body is required.",
            );
        };
        cg_write_response(self.storage.expire_due_cg_bulletins(
            &command.channel_id,
            command.now_unix_ms,
            &command.operator_id,
        ))
    }

    fn list_cg_audit(&self, path: &str) -> ApiResponse {
        let Some(query) = cg_query(path, "/api/v1/cg/audit?") else {
            return error_response(400, "invalid_query", "The CG audit query is invalid.");
        };
        if query
            .keys()
            .any(|key| !matches!(key.as_str(), "channel_id" | "limit"))
        {
            return error_response(400, "invalid_query", "The CG audit query is invalid.");
        }
        let Some(channel_id) = query.get("channel_id").map(String::as_str) else {
            return error_response(400, "invalid_query", "channel_id is required.");
        };
        let limit = match query.get("limit") {
            Some(value) => match value.parse::<u32>() {
                Ok(limit) => limit,
                Err(_) => {
                    return error_response(400, "invalid_query", "limit must be an integer.");
                }
            },
            None => 100,
        };
        match self.storage.list_cg_audit_events(channel_id, limit) {
            Ok(events) => json_response(
                200,
                serde_json::to_vec(&events).expect("serializing CG audit events cannot fail"),
            ),
            Err(message) if message.contains("limit") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_cg_images(&self) -> ApiResponse {
        match self.storage.list_verified_cg_image_assets() {
            Ok(images) => json_response(
                200,
                serde_json::to_vec(&images).expect("serializing CG image assets cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn upload_cg_image(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(body) = body else {
            return error_response(
                400,
                "cg_image_invalid",
                "A PNG or JPEG image body is required.",
            );
        };
        if body.len() > MAX_CG_IMAGE_BODY {
            return error_response(
                413,
                "request_too_large",
                "CG image uploads may not exceed 8 MiB.",
            );
        }
        match self.storage.admit_cg_image_asset(body) {
            Ok(image) => json_response(
                200,
                serde_json::to_vec(&image).expect("serializing a CG image asset cannot fail"),
            ),
            Err(message) => error_response(400, "cg_image_invalid", &message),
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

fn cg_query(path: &str, prefix: &str) -> Option<HashMap<String, String>> {
    let query = path.strip_prefix(prefix)?;
    if query.is_empty() || query.len() > 1_000 {
        return None;
    }
    let mut values = HashMap::new();
    for pair in query.split('&') {
        let (name, value) = pair.split_once('=')?;
        if name.is_empty()
            || value.is_empty()
            || name.len() > 80
            || value.len() > 500
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte == b'_')
            || value
                .bytes()
                .any(|byte| byte.is_ascii_control() || matches!(byte, b'/' | b'%' | b'\\'))
            || values.insert(name.to_string(), value.to_string()).is_some()
        {
            return None;
        }
    }
    Some(values)
}

fn exact_cg_query_parameter<'a>(query: &'a HashMap<String, String>, name: &str) -> Option<&'a str> {
    (query.len() == 1)
        .then(|| query.get(name).map(String::as_str))
        .flatten()
}

fn cg_resource_id<'a>(path: &'a str, prefix: &str) -> Option<&'a str> {
    let identity = path.strip_prefix(prefix)?;
    (!identity.is_empty()
        && identity.len() <= 128
        && !identity.contains('/')
        && !identity
            .bytes()
            .any(|byte| byte.is_ascii_control() || byte == b'%'))
    .then_some(identity)
}

fn cg_write_response<T: serde::Serialize>(result: Result<T, CgWriteError>) -> ApiResponse {
    match result {
        Ok(value) => json_response(
            200,
            serde_json::to_vec(&value).expect("serializing a CG write result cannot fail"),
        ),
        Err(CgWriteError::NotFound) => error_response(
            404,
            "cg_dependency_not_found",
            "A required CG resource does not exist.",
        ),
        Err(CgWriteError::Conflict(current_revision)) => error_response(
            409,
            "revision_conflict",
            &format!("The CG resource changed; current revision is {current_revision}."),
        ),
        Err(CgWriteError::Invalid(message)) => error_response(422, "validation_failed", message),
        Err(CgWriteError::Storage(message)) => error_response(500, "storage_error", &message),
    }
}

fn cg_delete_response(result: Result<(), CgWriteError>) -> ApiResponse {
    match result {
        Ok(()) => ApiResponse {
            status: 204,
            content_type: "application/json",
            attachment_name: None,
            body: Vec::new(),
        },
        Err(CgWriteError::NotFound) => error_response(
            404,
            "cg_dependency_not_found",
            "The requested CG resource does not exist.",
        ),
        Err(CgWriteError::Conflict(current_revision)) => error_response(
            409,
            "revision_conflict",
            &format!("The CG resource changed; current revision is {current_revision}."),
        ),
        Err(CgWriteError::Invalid(message)) => error_response(422, "validation_failed", message),
        Err(CgWriteError::Storage(message)) => error_response(500, "storage_error", &message),
    }
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
    let application_directory = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .ok_or_else(|| "the station application directory is unavailable".to_string())?;
    serve_until_with_application_directory(listener, database_path, application_directory, stop)
}

pub fn serve_until_with_application_directory(
    listener: TcpListener,
    database_path: impl AsRef<Path>,
    application_directory: impl AsRef<Path>,
    stop: Arc<AtomicBool>,
) -> Result<(), String> {
    let database_path = database_path.as_ref().to_path_buf();
    let application_directory = application_directory.as_ref().to_path_buf();
    Api::open_with_application_directory(&database_path, &application_directory)?;
    let active_requests = Arc::new(AtomicUsize::new(0));
    listener
        .set_nonblocking(true)
        .map_err(|error| error.to_string())?;
    while !stop.load(Ordering::Acquire) {
        match listener.accept() {
            Ok((mut stream, _)) => {
                stream
                    .set_nonblocking(false)
                    .map_err(|error| error.to_string())?;
                let active = active_requests.fetch_add(1, Ordering::AcqRel);
                if active >= MAX_CONCURRENT_REQUESTS {
                    active_requests.fetch_sub(1, Ordering::AcqRel);
                    write_response(
                        &mut stream,
                        error_response(
                            503,
                            "server_busy",
                            "The station is handling the maximum number of concurrent requests.",
                        ),
                    )?;
                    continue;
                }
                let request_database = database_path.clone();
                let request_application = application_directory.clone();
                let request_count = Arc::clone(&active_requests);
                std::thread::spawn(move || {
                    let _guard = ActiveRequestGuard(request_count);
                    let result = Api::open_with_application_directory(
                        &request_database,
                        &request_application,
                    )
                    .and_then(|api| serve_stream(&mut stream, &api));
                    if let Err(error) = result {
                        eprintln!("TownLight Station request failed: {error}");
                    }
                });
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
    write_response(stream, response)
}

fn write_response(stream: &mut TcpStream, response: ApiResponse) -> Result<(), String> {
    let reason = match response.status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Content Too Large",
        422 => "Unprocessable Content",
        503 => "Service Unavailable",
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

struct ActiveRequestGuard(Arc<AtomicUsize>);

impl Drop for ActiveRequestGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
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
        if request.len() > MAX_SMALL_REQUEST_BODY
            && !request.windows(4).any(|window| window == b"\r\n\r\n")
        {
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
        let header_bytes = header_end + 4;
        if header_bytes > MAX_SMALL_REQUEST_BODY {
            return Err(error_response(
                413,
                "request_too_large",
                "HTTP headers may not exceed 64 KiB.",
            ));
        }
        let mut request_parts = headers
            .split("\r\n")
            .next()
            .unwrap_or_default()
            .split_whitespace();
        let (Some(method), Some(path), Some(version), None) = (
            request_parts.next(),
            request_parts.next(),
            request_parts.next(),
            request_parts.next(),
        ) else {
            return Err(error_response(
                400,
                "malformed_request",
                "The request line is invalid.",
            ));
        };
        if version != "HTTP/1.1" {
            return Err(error_response(
                400,
                "malformed_request",
                "Only HTTP/1.1 is supported.",
            ));
        }
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
        let content_length = content_length.unwrap_or(0);
        let body_limit = request_body_limit(method, path);
        if content_length > body_limit {
            return Err(error_response(
                413,
                "request_too_large",
                request_too_large_message(method, path),
            ));
        }
        let Some(expected_length) = header_bytes.checked_add(content_length) else {
            return Err(error_response(
                400,
                "malformed_request",
                "Content-Length is invalid.",
            ));
        };
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
    let body_limit = request_body_limit(method, path);
    if body.len() > body_limit {
        return error_response(
            413,
            "request_too_large",
            request_too_large_message(method, path),
        );
    }
    api.handle(method, path, (!body.is_empty()).then_some(body))
}

fn request_body_limit(method: &str, path: &str) -> usize {
    if method == "POST" && path == "/api/v1/cg/images" {
        MAX_CG_IMAGE_BODY
    } else {
        MAX_SMALL_REQUEST_BODY
    }
}

fn request_too_large_message(method: &str, path: &str) -> &'static str {
    if request_body_limit(method, path) == MAX_CG_IMAGE_BODY {
        "CG image uploads may not exceed 8 MiB."
    } else {
        "Requests may not exceed 64 KiB."
    }
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
        CoordinatorError::NoAvailableRelease => error_response(
            409,
            "no_available_update",
            "No current verified release is available to stage on this station.",
        ),
        CoordinatorError::NoStagedRelease => error_response(
            409,
            "no_staged_update",
            "No current verified package is staged and available to prepare on this station.",
        ),
        CoordinatorError::NoPreparedRelease => error_response(
            409,
            "no_prepared_update",
            "No current verified application slot is prepared for maintenance activation.",
        ),
        CoordinatorError::MaintenanceAlreadyQueued => error_response(
            409,
            "maintenance_already_queued",
            "A maintenance activation request is already pending.",
        ),
        CoordinatorError::MaintenanceGate(message) => {
            error_response(409, "maintenance_gate_failed", &message)
        }
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    const PNG_1X1: &[u8] = &[
        137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 6,
        0, 0, 0, 31, 21, 196, 137, 0, 0, 0, 13, 73, 68, 65, 84, 120, 156, 99, 248, 207, 192, 240,
        31, 0, 5, 0, 1, 255, 137, 153, 61, 29, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130,
    ];

    fn read_request_over_tcp(request: Vec<u8>) -> Result<Vec<u8>, ApiResponse> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let sender = thread::spawn(move || {
            let mut stream = TcpStream::connect(address).unwrap();
            stream.write_all(&request).unwrap();
        });
        let (mut stream, _) = listener.accept().unwrap();
        let result = read_http_request(&mut stream);
        sender.join().unwrap();
        result
    }

    #[test]
    fn http_parser_allows_large_body_only_for_the_image_upload_route() {
        let mut image = PNG_1X1.to_vec();
        image.resize(MAX_SMALL_REQUEST_BODY + 1, 0);
        let request = format!(
            "POST /api/v1/cg/images HTTP/1.1\r\nHost: localhost\r\nContent-Length: {}\r\n\r\n",
            image.len()
        )
        .into_bytes();
        let mut image_request = request;
        image_request.extend_from_slice(&image);
        let parsed = read_request_over_tcp(image_request).expect("image request should parse");
        assert!(parsed.len() > MAX_SMALL_REQUEST_BODY);

        let database =
            std::env::temp_dir().join(format!("townlight-api-parser-{}.db", std::process::id()));
        let api = Api::open(&database).unwrap();
        let image_response = dispatch_http(&api, &parsed);
        assert_ne!(image_response.status, 413);

        let mut unrelated = format!(
            "POST /api/v1/station HTTP/1.1\r\nHost: localhost\r\nContent-Length: {}\r\n\r\n",
            image.len()
        )
        .into_bytes();
        unrelated.extend_from_slice(&image);
        let rejection = read_request_over_tcp(unrelated).unwrap_err();
        assert_eq!(rejection.status, 413);
        let _ = std::fs::remove_file(&database);
    }
}
