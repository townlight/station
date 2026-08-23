use std::collections::{BTreeSet, HashMap};
use std::io::{Read, Write};
use std::net::UdpSocket;
use std::net::{TcpListener, TcpStream};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::process::Stdio;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::time::{Duration, Instant};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use station_alerting::probe_system_resources;
use station_cg::{
    CgAuditEvent, CgBoard, CgBulletin, CgFeedItem, CgFeedItemApproval, CgFeedSource, CgZoneConfig,
    built_in_templates,
};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertEventState, AlertRule, AlertSeverity,
    ChannelRuntimeState, SafeToAirColor, SelfTestKind, SelfTestSchedule, SelfTestTrigger,
    StationProfile, derive_runtime_safe_to_air, redact_alert_channel_target,
    validate_alert_channel_target,
};
use station_live::{LiveSource, LiveSourceWriteError, TakeoverSession, TakeoverState};
use station_maintenance::{read_active_slot_receipt, verify_active_application};
use station_media_lifecycle::{
    AssetLoudnessAnalysis, AssetReadinessView, IngestJobStatus, IngestSourceKind,
    LoudnessGateStatus, MediaIngestJob, MissingMediaAlert, ReadinessDashboard, ReadinessState,
    TranscodeJobStatus, WatchFolderConfig,
};
use station_readiness::{
    BackupProbeInput, CandidateIdentity, CapabilitySupport, ChannelOutputRuntimeProbe,
    ClockProbeInput, ClockSyncStatus, EffectiveLoudnessStatus, EngineProbeInput,
    ExternalReceiverState, HardwareProbeInput, HeadendChannelProbeInput, HeadendProbeInput,
    HeadendProfileProbeInput, HeadendSinkKind, HeadendSinkProbeInput, NetworkProbeInput,
    ReadinessState as StationReadinessState, ReceiverAcceptance, ReleaseProbeInput,
    StationBoxProbeInput, StationBoxProfile, derive_headend_readiness, derive_station_box_profile,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, DispatchStatus, HEADEND_PROFILES,
    HeadendProfileApplyRequest, MediaAsset, OUTPUT_PROFILES, ScheduleItem, ScheduleState,
};
use station_storage::{
    AlertChannelSecret, CgSnapshotReadError, CgWriteError, ChannelWriteError, CommitWriteError,
    HeadendProfileWriteError, ProfileWriteError, StationStore, StoredSinkLoudnessProof,
};
use station_update::UpdateCheckTrigger;
use station_update_coordinator::CoordinatorError;

const OPERATOR_HTML: &[u8] = include_bytes!("../../../web/operator/dist/index.html");
const OPERATOR_SCRIPT: &[u8] = include_bytes!("../../../web/operator/dist/operator.js");
const OPERATOR_STYLES: &[u8] = include_bytes!("../../../web/operator/dist/operator.css");
const MAX_CONCURRENT_REQUESTS: usize = 16;
const MAX_SMALL_REQUEST_BODY: usize = 65_536;
const MAX_CG_IMAGE_BODY: usize = 8 * 1024 * 1024;
const STATION_BOX_CACHE_TTL: Duration = Duration::from_secs(60);
const LOUDNESS_PROOF_MAX_AGE_MS: i64 = 24 * 60 * 60 * 1_000;

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

#[derive(Debug, Clone, Serialize)]
struct ChannelLoudnessProofResponse {
    channel_id: String,
    sinks: Vec<SinkLoudnessProofResponse>,
}

#[derive(Debug, Clone, Serialize)]
struct SinkLoudnessProofResponse {
    sink_id: String,
    label: String,
    kind: station_schedule::OutputSinkKind,
    configured_target_lufs: f64,
    measured_lufs: Option<f64>,
    measurement_status: EffectiveLoudnessStatus,
    receiver_acceptance: ReceiverAcceptance,
    proof_id: Option<String>,
    captured_at_unix_ms: Option<i64>,
    sample_duration_ms: Option<u64>,
    capture_bytes: Option<u64>,
    capture_sha256: Option<String>,
    candidate_version: Option<String>,
    source_commit: Option<String>,
    configuration_hash: Option<String>,
    measurement_engine: Option<String>,
    measurement_method: Option<String>,
    detail: Option<String>,
    artifact_relative_path: Option<String>,
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
#[serde(deny_unknown_fields)]
struct PutLiveSource {
    #[serde(flatten)]
    source: LiveSource,
    expected_revision: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestLiveTakeover {
    session_id: String,
    channel_id: String,
    source_id: String,
    operator_id: String,
    #[serde(default)]
    reason: Option<String>,
    expires_at_unix_ms: i64,
    expected_authority_revision: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfirmLiveTransition {
    actor_id: String,
    expected_authority_revision: u64,
}

#[derive(Debug, Clone, Serialize)]
struct LiveAuthorityRead {
    channel_id: String,
    authority_revision: u64,
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
    station_box_probe_override: Option<StationBoxProbeInput>,
    station_box_cache: Arc<Mutex<Option<(Instant, StationBoxProfile)>>>,
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
        Self::open_with_application_directory_and_cache(
            database_path,
            application_directory,
            Arc::new(Mutex::new(None)),
        )
    }

    fn open_with_application_directory_and_cache(
        database_path: impl AsRef<Path>,
        application_directory: impl AsRef<Path>,
        station_box_cache: Arc<Mutex<Option<(Instant, StationBoxProfile)>>>,
    ) -> Result<Self, String> {
        let database_path = database_path.as_ref().to_path_buf();
        Ok(Self {
            storage: StationStore::open(&database_path)?,
            database_path,
            application_directory: application_directory.as_ref().to_path_buf(),
            station_box_probe_override: None,
            station_box_cache,
        })
    }

    #[cfg(test)]
    fn open_with_station_box_probe(
        database_path: impl AsRef<Path>,
        application_directory: impl AsRef<Path>,
        probe: StationBoxProbeInput,
    ) -> Result<Self, String> {
        let database_path = database_path.as_ref().to_path_buf();
        Ok(Self {
            storage: StationStore::open(&database_path)?,
            database_path,
            application_directory: application_directory.as_ref().to_path_buf(),
            station_box_probe_override: Some(probe),
            station_box_cache: Arc::new(Mutex::new(None)),
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
            ("GET", "/api/v1/station-box-profile") => self.station_box_profile(false),
            ("GET", "/api/v1/station-box-profile/readiness") => self.station_box_profile(true),
            ("GET", "/api/v1/headend-readiness") => self.headend_readiness(None),
            ("GET", "/api/v1/headend-profiles") => json_response(
                200,
                serde_json::to_vec(&HEADEND_PROFILES)
                    .expect("serializing the static headend profile catalog cannot fail"),
            ),
            ("PUT", "/api/v1/station") => self.put_profile(body),
            ("POST", "/api/v1/assets/ingest") => self.ingest_media(body),
            ("GET", "/api/v1/assets/readiness-dashboard") => self.readiness_dashboard(),
            ("GET", path) if path.starts_with("/api/v1/assets/missing-scheduled?") => {
                self.missing_scheduled(path)
            }
            ("PUT", "/api/v1/channels") => self.put_channel(body),
            ("GET", "/api/v1/channels") => self.list_channels(),
            ("GET", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/live-sources") =>
            {
                self.list_live_sources(path)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/")
                    && path.ends_with("/headend-readiness") =>
            {
                self.channel_headend_readiness(path)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/loudness-proof") =>
            {
                self.channel_loudness_proof(path)
            }
            ("POST", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/headend-profile") =>
            {
                self.apply_channel_headend_profile(path, body)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/live-authority") =>
            {
                self.read_live_authority(path)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/")
                    && path.ends_with("/active-live-takeover") =>
            {
                self.read_active_live_takeover(path)
            }
            ("PUT", path) if path.starts_with("/api/v1/live-sources/") => {
                self.put_live_source(path, body)
            }
            ("POST", "/api/v1/live-takeovers") => self.request_live_takeover(body),
            ("GET", path)
                if path.starts_with("/api/v1/live-takeovers/")
                    && !path.ends_with("/take")
                    && !path.ends_with("/return")
                    && !path.ends_with("/audit") =>
            {
                self.read_live_takeover(path)
            }
            ("POST", path)
                if path.starts_with("/api/v1/live-takeovers/") && path.ends_with("/take") =>
            {
                self.confirm_live_take(path, body)
            }
            ("POST", path)
                if path.starts_with("/api/v1/live-takeovers/") && path.ends_with("/return") =>
            {
                self.request_live_return(path, body)
            }
            ("GET", path)
                if path.starts_with("/api/v1/live-takeovers/") && path.ends_with("/audit") =>
            {
                self.list_live_audit(path)
            }
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
            Ok(document) => {
                if let Ok(mut cache) = self.station_box_cache.lock() {
                    *cache = None;
                }
                json_response(
                    200,
                    serde_json::to_vec(&document).expect("serializing a profile cannot fail"),
                )
            }
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
        let headend_contract_changed = existing.as_ref().is_some_and(|existing| {
            let prior = existing
                .output_sinks
                .iter()
                .filter(|sink| sink.headend.is_some())
                .collect::<Vec<_>>();
            let proposed = command
                .channel
                .output_sinks
                .iter()
                .filter(|sink| sink.headend.is_some())
                .collect::<Vec<_>>();
            prior != proposed
        }) || (existing.is_none()
            && command
                .channel
                .output_sinks
                .iter()
                .any(|sink| sink.headend.is_some()));
        if headend_contract_changed {
            return error_response(
                422,
                "headend_profile_route_required",
                "Create or change a headend sink through the headend profile application route.",
            );
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
        match self.storage.put_channel_with_srt_passphrases_if_current(
            &channel,
            &command.srt_passphrases,
            existing.as_ref(),
        ) {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&channel)
                    .expect("serializing a channel configuration cannot fail"),
            ),
            Err(ChannelWriteError::Conflict) => error_response(
                409,
                "channel_configuration_changed",
                "The channel changed while this update was being prepared. Reload and try again.",
            ),
            Err(ChannelWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
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

    fn list_live_sources(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/live-sources") else {
            return error_response(400, "invalid_path", "The live channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The live channel identity is invalid.");
        }
        match self.storage.list_channels() {
            Ok(channels)
                if !channels
                    .iter()
                    .any(|channel| channel.channel_id == channel_id) =>
            {
                error_response(404, "channel_not_found", "The channel does not exist.")
            }
            Ok(_) => match self.storage.list_channel_live_sources(channel_id) {
                Ok(sources) => json_response(
                    200,
                    serde_json::to_vec(&sources).expect("serializing live sources cannot fail"),
                ),
                Err(message) => error_response(500, "storage_error", &message),
            },
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_live_authority(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/live-authority") else {
            return error_response(400, "invalid_path", "The live channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The live channel identity is invalid.");
        }
        match self.storage.list_channels() {
            Ok(channels)
                if !channels
                    .iter()
                    .any(|channel| channel.channel_id == channel_id) =>
            {
                error_response(404, "channel_not_found", "The channel does not exist.")
            }
            Ok(_) => match self.storage.read_takeover_authority_revision(channel_id) {
                Ok(authority_revision) => json_response(
                    200,
                    serde_json::to_vec(&LiveAuthorityRead {
                        channel_id: channel_id.to_string(),
                        authority_revision,
                    })
                    .expect("serializing live authority cannot fail"),
                ),
                Err(message) => error_response(500, "storage_error", &message),
            },
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_active_live_takeover(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/active-live-takeover")
        else {
            return error_response(400, "invalid_path", "The live channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The live channel identity is invalid.");
        }
        match self.storage.list_channels() {
            Ok(channels)
                if !channels
                    .iter()
                    .any(|channel| channel.channel_id == channel_id) =>
            {
                error_response(404, "channel_not_found", "The channel does not exist.")
            }
            Ok(_) => match self.storage.read_active_takeover(channel_id) {
                Ok(session) => json_response(
                    200,
                    serde_json::to_vec(&session).expect("serializing active takeover cannot fail"),
                ),
                Err(message) => error_response(500, "storage_error", &message),
            },
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_live_source(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(source_id) = path.strip_prefix("/api/v1/live-sources/") else {
            return error_response(400, "invalid_path", "The live source identity is invalid.");
        };
        if !is_api_uuid(source_id) {
            return error_response(400, "invalid_path", "The live source identity is invalid.");
        }
        let Some(command) = parse_body::<PutLiveSource>(body) else {
            return error_response(400, "invalid_json", "A live source body is required.");
        };
        if command.source.source_id != source_id {
            return error_response(
                422,
                "identity_mismatch",
                "The live source path identity must match the body identity.",
            );
        }
        if let Err(message) = command.source.validate() {
            return error_response(422, "validation_failed", &message);
        }
        match self.storage.list_channels() {
            Ok(channels)
                if !channels
                    .iter()
                    .any(|channel| channel.channel_id == command.source.channel_id) =>
            {
                return error_response(
                    404,
                    "channel_not_found",
                    "The source channel does not exist.",
                );
            }
            Ok(_) => {}
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self
            .storage
            .write_live_source(&command.source, command.expected_revision)
        {
            Ok(source) => json_response(
                200,
                serde_json::to_vec(&source).expect("serializing live source cannot fail"),
            ),
            Err(LiveSourceWriteError::Conflict(current_revision)) => error_response(
                409,
                "revision_conflict",
                &format!(
                    "The live source changed; expected revision {}, current revision {}.",
                    command.expected_revision, current_revision
                ),
            ),
            Err(LiveSourceWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", &message)
            }
            Err(LiveSourceWriteError::Storage(message)) => live_storage_error(&message),
        }
    }

    fn request_live_takeover(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<RequestLiveTakeover>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A live takeover request body is required.",
            );
        };
        for (value, field) in [
            (&command.session_id, "session"),
            (&command.channel_id, "channel"),
            (&command.source_id, "source"),
        ] {
            if !is_api_uuid(value) {
                return error_response(
                    422,
                    "validation_failed",
                    &format!("The live {field} identity is invalid."),
                );
            }
        }
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let source = match self.storage.read_live_source(&command.source_id) {
            Ok(Some(source)) => source,
            Ok(None) => {
                return error_response(
                    404,
                    "live_source_not_found",
                    "The live source does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if source.channel_id != command.channel_id {
            return error_response(
                422,
                "identity_mismatch",
                "The takeover channel must match the source channel.",
            );
        }
        let session = TakeoverSession {
            session_id: command.session_id,
            channel_id: command.channel_id,
            source_id: command.source_id,
            source_revision: source.revision,
            authority_revision: command.expected_authority_revision.saturating_add(1),
            operator_id: command.operator_id,
            reason: command.reason,
            requested_at_unix_ms: now,
            expires_at_unix_ms: command.expires_at_unix_ms,
            taken_at_unix_ms: None,
            returned_at_unix_ms: None,
            state: TakeoverState::Arming,
            failure_detail: None,
        };
        if let Err(message) = session.validate() {
            return error_response(422, "validation_failed", &message);
        }
        match self
            .storage
            .request_takeover(&session, command.expected_authority_revision)
        {
            Ok(()) => json_response(
                201,
                serde_json::to_vec(&session).expect("serializing takeover session cannot fail"),
            ),
            Err(message) => live_storage_error(&message),
        }
    }

    fn read_live_takeover(&self, path: &str) -> ApiResponse {
        let Some(session_id) = path.strip_prefix("/api/v1/live-takeovers/") else {
            return error_response(
                400,
                "invalid_path",
                "The takeover session identity is invalid.",
            );
        };
        if !is_api_uuid(session_id) {
            return error_response(
                400,
                "invalid_path",
                "The takeover session identity is invalid.",
            );
        }
        match self.storage.read_takeover_session(session_id) {
            Ok(Some(session)) => json_response(
                200,
                serde_json::to_vec(&session).expect("serializing takeover session cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "takeover_not_found",
                "The takeover session does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn confirm_live_take(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        self.transition_live_takeover(
            path,
            body,
            TakeoverState::Armed,
            TakeoverState::TakeRequested,
        )
    }

    fn request_live_return(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        self.transition_live_takeover(
            path,
            body,
            TakeoverState::OnAir,
            TakeoverState::ReturnRequested,
        )
    }

    fn transition_live_takeover(
        &self,
        path: &str,
        body: Option<&[u8]>,
        expected: TakeoverState,
        next: TakeoverState,
    ) -> ApiResponse {
        let Some(session_id) = path
            .strip_prefix("/api/v1/live-takeovers/")
            .and_then(|value| {
                value.strip_suffix(if next == TakeoverState::TakeRequested {
                    "/take"
                } else {
                    "/return"
                })
            })
        else {
            return error_response(
                400,
                "invalid_path",
                "The takeover session identity is invalid.",
            );
        };
        if !is_api_uuid(session_id) {
            return error_response(
                400,
                "invalid_path",
                "The takeover session identity is invalid.",
            );
        }
        let Some(command) = parse_body::<ConfirmLiveTransition>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A takeover transition body is required.",
            );
        };
        if command.actor_id.trim().is_empty()
            || command.actor_id.trim() != command.actor_id
            || command.actor_id.chars().any(char::is_control)
            || command.actor_id.len() > 120
        {
            return error_response(
                422,
                "validation_failed",
                "The takeover actor identity is invalid.",
            );
        }
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.transition_takeover(
            session_id,
            expected,
            next,
            now,
            None,
            &command.actor_id,
            command.expected_authority_revision,
        ) {
            Ok(session) => json_response(
                200,
                serde_json::to_vec(&session).expect("serializing takeover session cannot fail"),
            ),
            Err(message) => live_storage_error(&message),
        }
    }

    fn list_live_audit(&self, path: &str) -> ApiResponse {
        let Some(session_id) = path
            .strip_prefix("/api/v1/live-takeovers/")
            .and_then(|value| value.strip_suffix("/audit"))
        else {
            return error_response(
                400,
                "invalid_path",
                "The takeover session identity is invalid.",
            );
        };
        if !is_api_uuid(session_id) {
            return error_response(
                400,
                "invalid_path",
                "The takeover session identity is invalid.",
            );
        }
        match self.storage.read_takeover_session(session_id) {
            Ok(Some(_)) => {}
            Ok(None) => {
                return error_response(
                    404,
                    "takeover_not_found",
                    "The takeover session does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self.storage.list_takeover_audit(session_id) {
            Ok(events) => json_response(
                200,
                serde_json::to_vec(&events).expect("serializing takeover audit cannot fail"),
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

    fn station_box_profile(&self, readiness_only: bool) -> ApiResponse {
        let profile = match self.cached_station_box_profile() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        if readiness_only {
            json_response(
                200,
                serde_json::to_vec(&profile.peg_readiness)
                    .expect("serializing StationBox readiness cannot fail"),
            )
        } else {
            json_response(
                200,
                serde_json::to_vec(&profile)
                    .expect("serializing the StationBox profile cannot fail"),
            )
        }
    }

    fn channel_headend_readiness(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/headend-readiness")
        else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        self.headend_readiness(Some(channel_id))
    }

    fn channel_loudness_proof(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/loudness-proof") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let snapshot = match self
            .storage
            .read_channel_loudness_proof_snapshot(channel_id)
        {
            Ok(Some(snapshot)) => snapshot,
            Ok(None) => {
                return error_response(404, "channel_not_found", "The channel does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let mut latest_by_sink = HashMap::new();
        for stored in snapshot.latest_proofs {
            latest_by_sink.insert(stored.proof.sink_id.clone(), stored);
        }
        let candidate = if latest_by_sink.is_empty() {
            None
        } else {
            match self.station_box_profile_uncached() {
                Ok(profile) if profile.release.candidate_verified => Some(CandidateIdentity {
                    version: profile.product_version,
                    source_commit: profile.source_commit,
                }),
                Ok(_) => None,
                Err(message) => return error_response(500, "readiness_probe_failed", &message),
            }
        };
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let sinks = snapshot
            .output_sinks
            .into_iter()
            .map(|sink| {
                let stored = latest_by_sink.remove(&sink.sink_id);
                loudness_proof_response(
                    sink,
                    stored,
                    candidate.as_ref(),
                    &snapshot.configuration_hash,
                    now,
                )
            })
            .collect();
        json_response(
            200,
            serde_json::to_vec(&ChannelLoudnessProofResponse {
                channel_id: channel_id.to_string(),
                sinks,
            })
            .expect("serializing loudness proof response cannot fail"),
        )
    }

    fn apply_channel_headend_profile(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/headend-profile") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let Some(request) = parse_body::<HeadendProfileApplyRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A headend profile application body is required.",
            );
        };
        let application = match self.storage.stage_headend_profile(channel_id, &request) {
            Ok(application) => application,
            Err(HeadendProfileWriteError::NotFound) => {
                return error_response(404, "channel_not_found", "The channel does not exist.");
            }
            Err(HeadendProfileWriteError::MustBeStopped) => {
                return error_response(
                    409,
                    "channel_must_be_stopped",
                    "Stop this output before applying a headend profile.",
                );
            }
            Err(HeadendProfileWriteError::Invalid(message)) => {
                return error_response(422, "validation_failed", &message);
            }
            Err(HeadendProfileWriteError::Storage(message)) => {
                return error_response(500, "storage_error", &message);
            }
        };
        json_response(
            200,
            serde_json::to_vec(&application)
                .expect("serializing a headend profile application cannot fail"),
        )
    }

    fn headend_readiness(&self, channel_id: Option<&str>) -> ApiResponse {
        let generated_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let station_box = match self.cached_station_box_profile() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        let probe = match collect_headend_probe(&self.storage, generated_at_unix_ms, channel_id) {
            Ok(Some(probe)) => probe,
            Ok(None) => {
                return error_response(404, "channel_not_found", "The channel does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        match derive_headend_readiness(&station_box, probe) {
            Ok(readiness) => json_response(
                200,
                serde_json::to_vec(&readiness).expect("serializing headend readiness cannot fail"),
            ),
            Err(error) => error_response(500, "headend_readiness_invalid", &error.to_string()),
        }
    }

    fn cached_station_box_profile(&self) -> Result<StationBoxProfile, String> {
        let cache_checked_at = Instant::now();
        let mut cache = self
            .station_box_cache
            .lock()
            .map_err(|_| "the StationBox readiness cache is unavailable".to_string())?;
        if let Some((_, profile)) = cache.as_ref().filter(|(cached_at, _)| {
            cache_checked_at.duration_since(*cached_at) <= STATION_BOX_CACHE_TTL
        }) {
            return Ok(profile.clone());
        }
        let profile = self.station_box_profile_uncached()?;
        *cache = Some((cache_checked_at, profile.clone()));
        Ok(profile)
    }

    fn station_box_profile_uncached(&self) -> Result<StationBoxProfile, String> {
        let generated_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return Err(format!("station clock is unavailable: {message}")),
        };
        let probe = match &self.station_box_probe_override {
            Some(probe) => probe.clone(),
            None => collect_station_box_probe(
                &self.storage,
                &self.database_path,
                &self.application_directory,
                generated_at_unix_ms,
            )?,
        };
        derive_station_box_profile(probe)
            .map_err(|error| format!("readiness probe is invalid: {error}"))
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

        let station_box = match self.cached_station_box_profile() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        let headend_probe = match collect_headend_probe(&self.storage, generated_at_unix_ms, None) {
            Ok(Some(probe)) => probe,
            Ok(None) => unreachable!("the aggregate headend probe is always present"),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let headend = match derive_headend_readiness(&station_box, headend_probe) {
            Ok(readiness) => readiness,
            Err(error) => {
                return error_response(500, "headend_readiness_invalid", &error.to_string());
            }
        };
        let headend_status = match headend.rollup.color {
            station_readiness::PegReadinessColor::Green => CommissioningCheckStatus::Pass,
            station_readiness::PegReadinessColor::Yellow => CommissioningCheckStatus::Warning,
            station_readiness::PegReadinessColor::Red => CommissioningCheckStatus::Blocked,
        };
        checks.push(CommissioningCheck {
            id: "headend_output_setup",
            label: "Headend output setup and local transport",
            status: headend_status,
            blocking: headend_status == CommissioningCheckStatus::Blocked,
            detail: headend.rollup.detail,
            evidence: "current StationBox capability, channel configuration, and worker heartbeat authority; external receiver acceptance remains separate".into(),
        });

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

fn path_identity<'a>(path: &'a str, prefix: &str, suffix: &str) -> Option<&'a str> {
    let identity = path.strip_prefix(prefix)?.strip_suffix(suffix)?;
    (!identity.is_empty() && !identity.contains('/') && !identity.contains(['?', '%', '\\']))
        .then_some(identity)
}

fn is_api_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 8 | 13 | 18 | 23) || byte.is_ascii_hexdigit())
}

fn live_storage_error(message: &str) -> ApiResponse {
    if message.contains("does not exist") {
        return error_response(404, "not_found", message);
    }
    if message.contains("conflict")
        || message.contains("already")
        || message.contains("stale")
        || message.contains("disabled")
        || message.contains("expired")
        || message.contains("constraint")
    {
        return error_response(409, "revision_conflict", message);
    }
    if message.contains("invalid")
        || message.contains("must ")
        || message.contains("not enabled")
        || message.contains("contains invalid")
    {
        return error_response(422, "validation_failed", message);
    }
    error_response(500, "storage_error", message)
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

const REQUIRED_MEDIA_FACTORIES: [&str; 22] = [
    "input-selector",
    "audiotestsrc",
    "audioconvert",
    "audioresample",
    "audiorate",
    "ebur128level",
    "videorate",
    "clocksync",
    "voaacenc",
    "aacparse",
    "uridecodebin",
    "tsdemux",
    "openh264dec",
    "avdec_aac",
    "openh264enc",
    "h264parse",
    "mpegtsmux",
    "udpsink",
    "srtsink",
    "gdkpixbufoverlay",
    "pngdec",
    "jpegdec",
];

fn loudness_proof_response(
    sink: station_schedule::OutputSinkConfiguration,
    stored: Option<StoredSinkLoudnessProof>,
    candidate: Option<&CandidateIdentity>,
    current_configuration_hash: &str,
    now_unix_ms: i64,
) -> SinkLoudnessProofResponse {
    let configured_target_lufs = f64::from(sink.loudness_target_lufs);
    let Some(stored) = stored else {
        return SinkLoudnessProofResponse {
            sink_id: sink.sink_id,
            label: sink.label,
            kind: sink.kind,
            configured_target_lufs,
            measured_lufs: None,
            measurement_status: EffectiveLoudnessStatus::NotRun,
            receiver_acceptance: ReceiverAcceptance::Unknown,
            proof_id: None,
            captured_at_unix_ms: None,
            sample_duration_ms: None,
            capture_bytes: None,
            capture_sha256: None,
            candidate_version: None,
            source_commit: None,
            configuration_hash: None,
            measurement_engine: None,
            measurement_method: None,
            detail: None,
            artifact_relative_path: None,
        };
    };
    let proof = stored.proof;
    let measurement_status = candidate.map_or(EffectiveLoudnessStatus::Expired, |candidate| {
        proof.effective_status(
            candidate,
            current_configuration_hash,
            now_unix_ms,
            LOUDNESS_PROOF_MAX_AGE_MS,
        )
    });
    SinkLoudnessProofResponse {
        sink_id: sink.sink_id,
        label: sink.label,
        kind: sink.kind,
        configured_target_lufs,
        measured_lufs: proof
            .measured_lufs_milli
            .map(|value| f64::from(value) / 1_000.0),
        measurement_status,
        receiver_acceptance: proof.receiver_acceptance,
        proof_id: Some(proof.proof_id),
        captured_at_unix_ms: Some(proof.captured_at_unix_ms),
        sample_duration_ms: Some(proof.sample_duration_ms),
        capture_bytes: Some(proof.capture_bytes),
        capture_sha256: Some(proof.capture_sha256),
        candidate_version: Some(proof.candidate_version),
        source_commit: proof.source_commit,
        configuration_hash: Some(proof.configuration_hash),
        measurement_engine: Some(proof.measurement_engine),
        measurement_method: Some(proof.measurement_method),
        detail: Some(proof.detail),
        artifact_relative_path: stored.artifact_relative_path,
    }
}

fn collect_headend_probe(
    storage: &StationStore,
    generated_at_unix_ms: i64,
    channel_id: Option<&str>,
) -> Result<Option<HeadendProbeInput>, String> {
    let mut channels = storage.list_channels()?;
    if let Some(channel_id) = channel_id {
        channels.retain(|channel| channel.channel_id == channel_id);
        if channels.is_empty() {
            return Ok(None);
        }
    }
    let mut probes = Vec::with_capacity(channels.len());
    for channel in channels {
        let profile = channel.output_profile.profile();
        let runtime = storage.read_channel_runtime(&channel.channel_id)?;
        let runtime = match runtime {
            Some(runtime) => {
                let heartbeat_age_ms = generated_at_unix_ms
                    .checked_sub(runtime.heartbeat_at_unix_ms)
                    .ok_or_else(|| "channel heartbeat age overflowed".to_string())?;
                if heartbeat_age_ms < 0 {
                    return Err("channel heartbeat cannot be in the future".into());
                }
                ChannelOutputRuntimeProbe {
                    state: match runtime.state {
                        ChannelRuntimeState::HealthyFallback
                        | ChannelRuntimeState::OnAir
                        | ChannelRuntimeState::Live => StationReadinessState::Pass,
                        ChannelRuntimeState::Starting => StationReadinessState::Unknown,
                        ChannelRuntimeState::Error | ChannelRuntimeState::Stopped => {
                            StationReadinessState::Blocked
                        }
                    },
                    heartbeat_age_ms: Some(heartbeat_age_ms),
                    evidence: format!(
                        "authoritative worker state {:?}, heartbeat age {heartbeat_age_ms} ms",
                        runtime.state
                    ),
                }
            }
            None => ChannelOutputRuntimeProbe {
                state: StationReadinessState::Unknown,
                heartbeat_age_ms: None,
                evidence: "no authoritative worker runtime observation exists".into(),
            },
        };
        probes.push(HeadendChannelProbeInput {
            channel_id: channel.channel_id,
            display_name: channel.display_name,
            enabled: channel.enabled,
            profile: HeadendProfileProbeInput {
                profile_id: profile.profile_id.storage_name().into(),
                width: profile.width,
                height: profile.height,
                frame_rate_numerator: profile.frame_rate_numerator,
                frame_rate_denominator: profile.frame_rate_denominator,
                interlaced: profile.interlaced,
                video_bitrate_bps: profile.video_bitrate_bps,
                audio_bitrate_bps: profile.audio_bitrate_bps,
                transport_bitrate_bps: profile.transport_bitrate_bps,
                audio_sample_rate_hz: profile.audio_sample_rate_hz,
                loudness_target_lufs: profile.loudness_target_lufs,
            },
            sinks: channel
                .output_sinks
                .into_iter()
                .map(|sink| HeadendSinkProbeInput {
                    sink_id: sink.sink_id,
                    label: sink.label,
                    kind: match sink.kind {
                        station_schedule::OutputSinkKind::UdpTs => HeadendSinkKind::UdpTs,
                        station_schedule::OutputSinkKind::Srt => HeadendSinkKind::Srt,
                    },
                    required: sink.required,
                    enabled: sink.enabled,
                    // Local worker progress is not cable-operator decode or
                    // receiver acceptance. Only an external evidence receipt
                    // may change this in a later proof slice.
                    external_receiver: ExternalReceiverState::Unknown,
                })
                .collect(),
            runtime,
        });
    }
    Ok(Some(HeadendProbeInput {
        generated_at_unix_ms,
        channels: probes,
    }))
}

fn collect_station_box_probe(
    storage: &StationStore,
    database_path: &Path,
    application_directory: &Path,
    generated_at_unix_ms: i64,
) -> Result<StationBoxProbeInput, String> {
    let resources = probe_system_resources(database_path, generated_at_unix_ms)?;
    let station = storage.read_profile()?;
    let engine = probe_installed_engine(application_directory);
    let network_up = UdpSocket::bind("0.0.0.0:0")
        .and_then(|socket| {
            socket.connect("192.0.2.1:9")?;
            socket.local_addr()
        })
        .is_ok_and(|address| !address.ip().is_loopback() && !address.ip().is_unspecified());
    let data_directory = database_path
        .parent()
        .ok_or_else(|| "station database has no parent directory".to_string())?;
    let (receipt_version, receipt_commit, receipt_verified, receipt_detail) =
        read_release_receipts(data_directory, application_directory);
    let backup_writable = resources.backup_volume_writable;
    let clock_sync = probe_windows_clock_sync();
    Ok(StationBoxProbeInput {
        generated_at_unix_ms,
        probe_collected_at_unix_ms: generated_at_unix_ms,
        max_probe_age_ms: 300_000,
        product_version: receipt_version.unwrap_or_else(|| env!("CARGO_PKG_VERSION").into()),
        source_commit: receipt_commit,
        station_id: station
            .as_ref()
            .map(|value| value.profile.station_id.clone()),
        station_name: station
            .as_ref()
            .map(|value| value.profile.display_name.clone()),
        timezone: station.as_ref().map(|value| value.profile.timezone.clone()),
        hardware: HardwareProbeInput {
            logical_cpus: std::thread::available_parallelism()
                .map(|value| value.get() as u32)
                .unwrap_or(1),
            ram_total_gb: resources
                .ram_total_gb
                .ok_or_else(|| "live RAM total is unavailable".to_string())?,
            media_disk_free_gb: resources
                .media_volume_free_gb
                .ok_or_else(|| "live media free space is unavailable".to_string())?,
        },
        clock: ClockProbeInput {
            configured_timezone: station.as_ref().map(|value| value.profile.timezone.clone()),
            utc_offset_seconds: None,
            sync_status: clock_sync,
        },
        network: NetworkProbeInput {
            state: if network_up {
                StationReadinessState::Unknown
            } else {
                StationReadinessState::Blocked
            },
            hostname: std::env::var("COMPUTERNAME")
                .ok()
                .filter(|value| !value.is_empty()),
            interface_up: network_up,
            dns_working: None,
            // A UDP connect proves route selection only; it does not prove that
            // packets can reach a headend or remote receiver.
            outbound_udp: CapabilitySupport::Unknown,
            outbound_srt: CapabilitySupport::Unknown,
        },
        backup: BackupProbeInput {
            state: if backup_writable {
                StationReadinessState::Pass
            } else {
                StationReadinessState::Blocked
            },
            destination_configured: true,
            writable: Some(backup_writable),
            detail: Some(if backup_writable {
                "The managed backup directory passed a synchronized write/read/delete probe.".into()
            } else {
                "The managed backup directory failed its write/read/delete probe.".into()
            }),
        },
        release: ReleaseProbeInput {
            state: if receipt_verified {
                StationReadinessState::Pass
            } else {
                StationReadinessState::Unknown
            },
            candidate_verified: receipt_verified,
            update_available: false,
            detail: Some(receipt_detail),
        },
        engine,
    })
}

fn probe_installed_engine(application_directory: &Path) -> EngineProbeInput {
    let runtime = application_directory.join("runtime").join("gstreamer");
    let executable = runtime.join("bin").join("gst-inspect-1.0.exe");
    let required_factories = REQUIRED_MEDIA_FACTORIES
        .iter()
        .map(|name| (*name).to_string())
        .collect::<Vec<_>>();
    let mut available = BTreeSet::new();
    let mut version = None;
    if executable.is_file() {
        let mut inventory = hidden_command(&executable);
        configure_gstreamer_command(&mut inventory, &runtime);
        inventory.arg("--print-plugin-auto-install-info");
        if let Ok(output) = run_bounded_command(inventory, Duration::from_secs(5), 4 * 1024 * 1024)
            && output.success
        {
            for line in String::from_utf8_lossy(&output.stdout).lines() {
                if let Some(factory) = line.strip_prefix("element-") {
                    available.insert(factory.trim().to_string());
                }
            }
        }
        let mut version_command = hidden_command(&executable);
        configure_gstreamer_command(&mut version_command, &runtime);
        version_command.arg("--version");
        if let Ok(output) = run_bounded_command(version_command, Duration::from_secs(5), 64 * 1024)
            && output.success
        {
            version = String::from_utf8_lossy(&output.stdout)
                .lines()
                .find_map(|line| line.trim().strip_prefix("GStreamer ").map(str::to_string));
        }
    }
    let available_factories = required_factories
        .iter()
        .filter(|factory| available.contains(factory.as_str()))
        .cloned()
        .collect::<Vec<_>>();
    let available_encoders = ["openh264enc", "mfh264enc", "nvh264enc", "amfh264enc"]
        .into_iter()
        .filter(|factory| available.contains(*factory))
        .map(str::to_string)
        .collect::<Vec<_>>();
    let base_ready = version.is_some() && available_factories.len() == required_factories.len();
    let state = if base_ready {
        StationReadinessState::Pass
    } else {
        StationReadinessState::Blocked
    };
    EngineProbeInput {
        gstreamer_version: version,
        required_factories,
        available_factories,
        available_encoders,
        playout: state.clone(),
        runtime: state.clone(),
        output: state,
        udp: if base_ready {
            CapabilitySupport::Supported
        } else {
            CapabilitySupport::Unknown
        },
        srt: if available.contains("srtsink") {
            CapabilitySupport::Supported
        } else {
            CapabilitySupport::Unknown
        },
        sdi: CapabilitySupport::Unimplemented,
        ndi: CapabilitySupport::Unimplemented,
        interlaced: CapabilitySupport::Unimplemented,
    }
}

fn configure_gstreamer_command(command: &mut Command, runtime: &Path) {
    let plugin_path = runtime.join("lib").join("gstreamer-1.0");
    let runtime_bin = runtime.join("bin");
    let mut path = runtime_bin.into_os_string();
    path.push(";");
    path.push(std::env::var_os("PATH").unwrap_or_default());
    command
        .env("PATH", path)
        .env("GST_PLUGIN_PATH_1_0", &plugin_path)
        .env("GST_PLUGIN_SYSTEM_PATH_1_0", plugin_path);
}

fn probe_windows_clock_sync() -> ClockSyncStatus {
    let mut command = hidden_command("w32tm.exe");
    command.args(["/query", "/status"]);
    // A successful w32tm query means only that status was returned. Its text is
    // localized and can describe an unsynchronized clock, so do not infer green.
    let _ = run_bounded_command(command, Duration::from_secs(5), 64 * 1024);
    ClockSyncStatus::Unknown
}

fn hidden_command(program: impl AsRef<std::ffi::OsStr>) -> Command {
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let mut command = Command::new(program);
    command.creation_flags(CREATE_NO_WINDOW);
    command
}

#[derive(Debug)]
struct BoundedCommandOutput {
    success: bool,
    stdout: Vec<u8>,
}

fn run_bounded_command(
    mut command: Command,
    timeout: Duration,
    max_output_bytes: u64,
) -> Result<BoundedCommandOutput, String> {
    command.stdout(Stdio::piped()).stderr(Stdio::null());
    let mut child = command
        .spawn()
        .map_err(|error| format!("could not start readiness command: {error}"))?;
    let Some(mut stdout) = child.stdout.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err("readiness command stdout was not captured".into());
    };
    let output_limit = usize::try_from(max_output_bytes).unwrap_or(usize::MAX);
    let (output_sender, output_receiver) = std::sync::mpsc::sync_channel(1);
    let reader = std::thread::spawn(move || {
        let mut collected = Vec::new();
        let mut chunk = [0_u8; 8 * 1024];
        let result = loop {
            match stdout.read(&mut chunk) {
                Ok(0) => break Ok(collected),
                Ok(read) if read <= output_limit.saturating_sub(collected.len()) => {
                    collected.extend_from_slice(&chunk[..read]);
                }
                Ok(_) => break Err("readiness command output exceeded its byte limit".to_string()),
                Err(error) => {
                    break Err(format!("could not read readiness command output: {error}"));
                }
            }
        };
        let _ = output_sender.send(result);
    });
    let started = Instant::now();
    let mut captured_output = None;
    let status = loop {
        match output_receiver.try_recv() {
            Ok(Ok(output)) => captured_output = Some(output),
            Ok(Err(message)) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = reader.join();
                return Err(message);
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => {}
            Err(std::sync::mpsc::TryRecvError::Disconnected) if captured_output.is_some() => {}
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = reader.join();
                return Err("readiness command output reader stopped unexpectedly".into());
            }
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() < timeout => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                // A descendant may have inherited stdout. Detach the bounded
                // reader instead of allowing that inherited handle to defeat
                // this request's deadline.
                drop(reader);
                return Err("readiness command exceeded its bounded deadline".into());
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                drop(reader);
                return Err(format!("could not poll readiness command: {error}"));
            }
        }
    };
    let stdout = match captured_output {
        Some(output) => output,
        None => output_receiver
            .recv_timeout(Duration::from_secs(1))
            .map_err(|_| "readiness command output reader did not finish".to_string())??,
    };
    reader
        .join()
        .map_err(|_| "readiness command output reader panicked".to_string())?;
    Ok(BoundedCommandOutput {
        success: status.success(),
        stdout,
    })
}

fn read_release_receipts(
    data_directory: &Path,
    application_directory: &Path,
) -> (Option<String>, Option<String>, bool, String) {
    let install = read_bounded_json(&data_directory.join("install-receipt.json"));
    let install_version = install
        .as_ref()
        .and_then(|value| value.get("version"))
        .and_then(serde_json::Value::as_str)
        .map(str::to_string);
    let install_commit = install
        .as_ref()
        .and_then(|value| value.get("source_commit"))
        .and_then(serde_json::Value::as_str)
        .map(str::to_string);
    let active = read_active_slot_receipt(&data_directory.join("active-slot.json")).ok();
    let active_version = active
        .as_ref()
        .map(|receipt| receipt.active.package_manifest.version.as_str());
    let active_commit = active
        .as_ref()
        .map(|receipt| receipt.active.package_manifest.source_commit.as_str());
    let application_verified = install_version.as_deref() == active_version
        && install_commit.as_deref() == active_commit
        && active.as_ref().is_some_and(|receipt| {
            verify_active_application(application_directory, &receipt.active).is_ok()
        });
    (
        install_version,
        install_commit,
        false,
        if application_verified {
            "The install and active-slot receipts agree and both active binaries match their manifest hashes; the private media-runtime hash is not yet live-verified.".into()
        } else {
            "A matching install/active-slot receipt pair is not available.".into()
        },
    )
}

fn read_bounded_json(path: &Path) -> Option<serde_json::Value> {
    let metadata = std::fs::symlink_metadata(path).ok()?;
    if !metadata.file_type().is_file() || metadata.len() > 65_536 {
        return None;
    }
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
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
    let station_box_cache = Arc::new(Mutex::new(None));
    Api::open_with_application_directory_and_cache(
        &database_path,
        &application_directory,
        Arc::clone(&station_box_cache),
    )?;
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
                let request_station_box_cache = Arc::clone(&station_box_cache);
                let request_count = Arc::clone(&active_requests);
                std::thread::spawn(move || {
                    let _guard = ActiveRequestGuard(request_count);
                    let result = Api::open_with_application_directory_and_cache(
                        &request_database,
                        &request_application,
                        request_station_box_cache,
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

    fn station_box_probe() -> StationBoxProbeInput {
        StationBoxProbeInput {
            generated_at_unix_ms: 1_777_000_000_000,
            probe_collected_at_unix_ms: 1_777_000_000_000,
            max_probe_age_ms: 300_000,
            product_version: "0.1.0".into(),
            source_commit: Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into()),
            station_id: Some("3f5f721f-96c7-48b1-b061-1bf1ad1e62c2".into()),
            station_name: Some("KTLT".into()),
            timezone: Some("America/Denver".into()),
            hardware: HardwareProbeInput {
                logical_cpus: 8,
                ram_total_gb: 16.0,
                media_disk_free_gb: 200.0,
            },
            engine: EngineProbeInput {
                gstreamer_version: Some("1.28.6".into()),
                required_factories: vec![
                    "input-selector".into(),
                    "mpegtsmux".into(),
                    "udpsink".into(),
                ],
                available_factories: vec![
                    "input-selector".into(),
                    "mpegtsmux".into(),
                    "udpsink".into(),
                ],
                available_encoders: vec!["openh264enc".into()],
                playout: StationReadinessState::Pass,
                runtime: StationReadinessState::Pass,
                output: StationReadinessState::Pass,
                udp: CapabilitySupport::Supported,
                srt: CapabilitySupport::Supported,
                sdi: CapabilitySupport::Unimplemented,
                ndi: CapabilitySupport::Unimplemented,
                interlaced: CapabilitySupport::Unimplemented,
            },
            clock: ClockProbeInput {
                configured_timezone: Some("America/Denver".into()),
                utc_offset_seconds: None,
                sync_status: ClockSyncStatus::Synchronized,
            },
            network: NetworkProbeInput {
                state: StationReadinessState::Pass,
                hostname: Some("station-one".into()),
                interface_up: true,
                dns_working: Some(true),
                outbound_udp: CapabilitySupport::Supported,
                outbound_srt: CapabilitySupport::Supported,
            },
            backup: BackupProbeInput {
                state: StationReadinessState::Pass,
                destination_configured: true,
                writable: Some(true),
                detail: Some("Backup probe passed.".into()),
            },
            release: ReleaseProbeInput {
                state: StationReadinessState::Pass,
                candidate_verified: true,
                update_available: false,
                detail: Some("Receipts match.".into()),
            },
        }
    }

    #[test]
    fn station_box_routes_share_one_cached_authority_without_public_fact_injection() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-station-box-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();

        let full = api.handle("GET", "/api/v1/station-box-profile", None);
        assert_eq!(full.status, 200);
        let full: serde_json::Value = serde_json::from_slice(&full.body).unwrap();
        assert_eq!(full["schema_version"], 1);
        assert_eq!(full["peg_readiness"]["color"], "yellow");
        assert_eq!(full["engine"]["sdi"], "unimplemented");
        assert!(api.station_box_cache.lock().unwrap().is_some());

        let rollup = api.handle("GET", "/api/v1/station-box-profile/readiness", None);
        assert_eq!(rollup.status, 200);
        let rollup: serde_json::Value = serde_json::from_slice(&rollup.body).unwrap();
        assert_eq!(rollup, full["peg_readiness"]);

        let profile = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#;
        assert_eq!(
            api.handle("PUT", "/api/v1/station", Some(profile)).status,
            200
        );
        assert!(api.station_box_cache.lock().unwrap().is_none());

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn readiness_subprocess_output_is_rejected_at_the_configured_byte_limit() {
        let mut command = hidden_command("cmd.exe");
        command.args(["/D", "/S", "/C", "echo 0123456789"]);
        let error = run_bounded_command(command, Duration::from_secs(2), 4).unwrap_err();
        assert!(error.contains("output exceeded its byte limit"));
    }

    #[test]
    fn headend_routes_report_fresh_local_progress_without_claiming_receiver_acceptance() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-headend-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Government".into(),
                output_profile: station_schedule::OutputProfileId::Atsc720p5994,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "22222222-3333-4444-8555-666666666666",
                    "Cable primary",
                    "127.0.0.1:5500",
                    -24.0,
                )],
                enabled: true,
            })
            .unwrap();
        let now = system_time_millis().unwrap();
        api.storage
            .put_channel_runtime(&station_domain::ChannelRuntimeObservation {
                channel_id: channel_id.into(),
                state: ChannelRuntimeState::HealthyFallback,
                on_air_asset_id: None,
                state_started_at_unix_ms: now,
                heartbeat_at_unix_ms: now,
                detail: Some("all required branches advancing".into()),
            })
            .unwrap();

        let aggregate = api.handle("GET", "/api/v1/headend-readiness", None);
        assert_eq!(aggregate.status, 200);
        let aggregate: serde_json::Value = serde_json::from_slice(&aggregate.body).unwrap();
        assert_eq!(aggregate["rollup"]["color"], "yellow");
        assert_eq!(
            aggregate["channels"][0]["sinks"][0]["local_transport"],
            "pass"
        );
        assert_eq!(
            aggregate["channels"][0]["sinks"][0]["external_receiver"],
            "unknown"
        );

        let one = api.handle(
            "GET",
            &format!("/api/v1/channels/{channel_id}/headend-readiness"),
            None,
        );
        assert_eq!(one.status, 200);
        let one: serde_json::Value = serde_json::from_slice(&one.body).unwrap();
        assert_eq!(
            one["channels"][0]["channel_id"],
            aggregate["channels"][0]["channel_id"]
        );
        assert_eq!(
            one["channels"][0]["sinks"][0]["state"],
            aggregate["channels"][0]["sinks"][0]["state"]
        );

        api.storage
            .put_channel_runtime(&station_domain::ChannelRuntimeObservation {
                channel_id: channel_id.into(),
                state: ChannelRuntimeState::Stopped,
                on_air_asset_id: None,
                state_started_at_unix_ms: now,
                heartbeat_at_unix_ms: now + 1,
                detail: Some("operator stopped output".into()),
            })
            .unwrap();
        let stopped = api.handle("GET", "/api/v1/headend-readiness", None);
        let stopped: serde_json::Value = serde_json::from_slice(&stopped.body).unwrap();
        assert_eq!(stopped["rollup"]["color"], "red");

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn loudness_proof_route_expires_historical_pass_after_output_reconfiguration() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-loudness-proof-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let mut api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        let mut channel = ChannelConfiguration {
            channel_id: channel_id.into(),
            display_name: "Measured output".into(),
            output_profile: station_schedule::OutputProfileId::Atsc720p5994,
            output_sinks: vec![
                station_schedule::OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Cable primary",
                    "127.0.0.1:5500",
                    -24.0,
                ),
                station_schedule::OutputSinkConfiguration::required_udp(
                    "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "Streaming primary",
                    "127.0.0.1:5501",
                    -16.0,
                ),
            ],
            enabled: true,
        };
        api.storage.put_channel(&channel).unwrap();
        let configuration_hash = api
            .storage
            .current_channel_output_configuration_hash(channel_id)
            .unwrap();
        let captured_at_unix_ms = system_time_millis().unwrap();
        api.storage
            .append_sink_loudness_proof(
                &station_readiness::SinkLoudnessProof {
                    proof_id: "33333333-4444-4555-8666-777777777777".into(),
                    channel_id: channel_id.into(),
                    sink_id: sink_id.into(),
                    target_lufs_milli: -24_000,
                    measured_lufs_milli: Some(-23_600),
                    tolerance_lufs_milli: 2_000,
                    outcome: station_readiness::LoudnessProofOutcome::Pass,
                    receiver_acceptance: ReceiverAcceptance::Unknown,
                    captured_at_unix_ms: captured_at_unix_ms - 2_000,
                    sample_duration_ms: 30_000,
                    capture_bytes: 1_000_000,
                    capture_sha256:
                        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
                    candidate_version: "0.1.0".into(),
                    source_commit: Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into()),
                    configuration_hash,
                    measurement_engine: "GStreamer 1.28.6".into(),
                    measurement_method: "decoded post-mux MPEG-TS ebur128".into(),
                    detail: "local target-group proof; receiver acceptance not attempted".into(),
                },
                Some("proofs/run-1/cable.ts"),
            )
            .unwrap();
        for index in 0_i64..1_000 {
            api.storage
                .append_sink_loudness_proof(
                    &station_readiness::SinkLoudnessProof {
                        proof_id: format!("{index:08x}-0000-4000-8000-000000000000"),
                        channel_id: channel_id.into(),
                        sink_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
                        target_lufs_milli: -16_000,
                        measured_lufs_milli: Some(-15_900),
                        tolerance_lufs_milli: 2_000,
                        outcome: station_readiness::LoudnessProofOutcome::Pass,
                        receiver_acceptance: ReceiverAcceptance::Unknown,
                        captured_at_unix_ms: captured_at_unix_ms - 1_000 + index,
                        sample_duration_ms: 30_000,
                        capture_bytes: 1_000_000,
                        capture_sha256:
                            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                                .into(),
                        candidate_version: "0.1.0".into(),
                        source_commit: Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into()),
                        configuration_hash: api
                            .storage
                            .current_channel_output_configuration_hash(channel_id)
                            .unwrap(),
                        measurement_engine: "GStreamer 1.28.6".into(),
                        measurement_method: "decoded post-mux MPEG-TS ebur128".into(),
                        detail: "bounded per-sink history regression fixture".into(),
                    },
                    None,
                )
                .unwrap();
        }

        let current = api.handle(
            "GET",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            None,
        );
        assert_eq!(current.status, 200);
        let current: serde_json::Value = serde_json::from_slice(&current.body).unwrap();
        assert_eq!(current["sinks"][0]["measurement_status"], "pass");
        assert_eq!(current["sinks"][0]["measured_lufs"], -23.6);
        assert_eq!(current["sinks"][0]["receiver_acceptance"], "unknown");

        api.cached_station_box_profile().unwrap();
        let mut unverified_probe = station_box_probe();
        unverified_probe.release.state = StationReadinessState::Blocked;
        unverified_probe.release.candidate_verified = false;
        api.station_box_probe_override = Some(unverified_probe);
        let unverified = api.handle(
            "GET",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            None,
        );
        assert_eq!(
            unverified.status,
            200,
            "{}",
            String::from_utf8_lossy(&unverified.body)
        );
        let unverified: serde_json::Value = serde_json::from_slice(&unverified.body).unwrap();
        assert_eq!(unverified["sinks"][0]["measurement_status"], "expired");

        api.station_box_probe_override = Some(station_box_probe());
        channel.output_sinks[0].destination = "127.0.0.1:5510".into();
        api.storage.put_channel(&channel).unwrap();
        let expired = api.handle(
            "GET",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            None,
        );
        let expired: serde_json::Value = serde_json::from_slice(&expired.body).unwrap();
        assert_eq!(expired["sinks"][0]["measurement_status"], "expired");
        assert_eq!(expired["sinks"][0]["receiver_acceptance"], "unknown");

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
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
