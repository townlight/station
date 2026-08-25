pub mod auth;
mod machine_inventory;

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
use sha2::{Digest, Sha256};
use station_alerting::probe_system_resources;
use station_auth::{OperatorAccount, SecretToken, digest_secret};
use station_cg::{
    CgAuditEvent, CgBoard, CgBulletin, CgBulletinState, CgFeedItem, CgFeedItemApproval,
    CgFeedSource, CgZoneConfig, built_in_templates,
};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertEventState, AlertRule, AlertSeverity,
    ChannelRuntimeState, SafeToAirColor, SelfTestKind, SelfTestSchedule, SelfTestTrigger,
    StationProfile, derive_runtime_safe_to_air, redact_alert_channel_target,
    validate_alert_channel_target,
};
use station_live::{LiveSource, LiveSourceWriteError, TakeoverSession, TakeoverState};
use station_maintenance::read_verified_candidate_identity;
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
    ReadinessState as StationReadinessState, ReceiverAcceptance, ReferenceStationTier,
    ReleaseProbeInput, StationBoxProbeInput, StationBoxProfile, derive_headend_readiness,
    derive_station_box_profile,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, CommitReport, DispatchStatus, HEADEND_PROFILES,
    HeadendProfileApplyRequest, MediaAsset, OUTPUT_PROFILES, ScheduleItem, ScheduleState,
};
use station_storage::{
    AlertChannelSecret, CgSnapshotReadError, CgWriteError, ChannelWriteError,
    CommissioningSupportBundleReceipt, CommitWriteError, HeadendProfileWriteError,
    OutputLoudnessProofJob, OutputLoudnessProofJobRecord, OutputLoudnessProofJobState,
    ProfileWriteError, ReferenceStationTierWrite, ReferenceStationTierWriteError, StationStore,
    StoredSinkLoudnessProof,
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
const LOUDNESS_PROOF_MIN_DURATION_MS: u64 = 3_000;
const LOUDNESS_PROOF_MAX_DURATION_MS: u64 = 300_000;
const LOUDNESS_PROOF_MAX_CAPTURE_BYTES: u64 = 256 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OrchestrationHealthStatus {
    Starting,
    Ready,
    Failed,
    Stale,
}

pub struct OrchestrationHealth {
    stale_after: Duration,
    inner: Mutex<OrchestrationHealthInner>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum OrchestrationLifecycle {
    Starting,
    Ready,
    Failed,
}

struct OrchestrationHealthInner {
    lifecycle: OrchestrationLifecycle,
    last_progress: Option<Instant>,
}

impl OrchestrationHealth {
    pub fn new(stale_after: Duration) -> Self {
        Self {
            stale_after,
            inner: Mutex::new(OrchestrationHealthInner {
                lifecycle: OrchestrationLifecycle::Starting,
                last_progress: None,
            }),
        }
    }

    fn ready_without_supervisor() -> Arc<Self> {
        let health = Arc::new(Self::new(Duration::MAX));
        health.mark_progress();
        health
    }

    pub fn status(&self) -> OrchestrationHealthStatus {
        self.status_at(Instant::now())
    }

    pub fn mark_progress(&self) {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if inner.lifecycle != OrchestrationLifecycle::Failed {
            inner.lifecycle = OrchestrationLifecycle::Ready;
            inner.last_progress = Some(Instant::now());
        }
    }

    pub fn mark_alive(&self) {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if inner.lifecycle == OrchestrationLifecycle::Ready {
            inner.last_progress = Some(Instant::now());
        }
    }

    pub fn mark_failed(&self) {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        inner.lifecycle = OrchestrationLifecycle::Failed;
    }

    fn status_at(&self, now: Instant) -> OrchestrationHealthStatus {
        let inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        match inner.lifecycle {
            OrchestrationLifecycle::Starting => OrchestrationHealthStatus::Starting,
            OrchestrationLifecycle::Failed => OrchestrationHealthStatus::Failed,
            OrchestrationLifecycle::Ready
                if inner.last_progress.is_some_and(|progress| {
                    now.saturating_duration_since(progress) > self.stale_after
                }) =>
            {
                OrchestrationHealthStatus::Stale
            }
            OrchestrationLifecycle::Ready => OrchestrationHealthStatus::Ready,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum CommissioningCheckStatus {
    Pass,
    Warning,
    Blocked,
    NotVerified,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum CommissioningPhase {
    Identity,
    StationBox,
    SourcesOutputs,
    GuidedAirProof,
    ExternalProof,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum CommissioningBoundary {
    LocalAuthority,
    ExternalEvidence,
}

#[derive(Debug, Clone, Serialize)]
struct CommissioningCheck {
    id: &'static str,
    label: &'static str,
    phase: CommissioningPhase,
    status: CommissioningCheckStatus,
    required: bool,
    blocking: bool,
    action: Option<&'static str>,
    detail: String,
    evidence: String,
    boundary: CommissioningBoundary,
}

#[derive(Debug, Clone, Serialize)]
struct CommissioningCoreProgress {
    passed: usize,
    total: usize,
}

#[derive(Debug, Clone, Serialize)]
struct CommissioningReport {
    schema_version: u32,
    generated_at_unix_ms: i64,
    overall_status: CommissioningCheckStatus,
    current_step_id: Option<&'static str>,
    core_progress: CommissioningCoreProgress,
    core_complete: bool,
    checks: Vec<CommissioningCheck>,
}

#[derive(Debug, Serialize)]
struct SupportBundleCreationResponse {
    #[serde(flatten)]
    bundle: station_support::SupportBundleRecord,
    commissioning_report_id: Option<String>,
}

// Keeping every serialized policy field visible at each ordered call site makes
// this safety checklist auditable; hiding them in positional defaults would be
// more error-prone than this deliberately explicit constructor.
#[allow(clippy::too_many_arguments)]
fn commissioning_check(
    id: &'static str,
    label: &'static str,
    phase: CommissioningPhase,
    status: CommissioningCheckStatus,
    required: bool,
    action: Option<&'static str>,
    detail: impl Into<String>,
    evidence: impl Into<String>,
    boundary: CommissioningBoundary,
) -> CommissioningCheck {
    CommissioningCheck {
        id,
        label,
        phase,
        status,
        required,
        blocking: required && status != CommissioningCheckStatus::Pass,
        action,
        detail: detail.into(),
        evidence: evidence.into(),
        boundary,
    }
}

fn headend_receiver_verified(headend: &station_readiness::HeadendReadiness) -> bool {
    headend.channels.iter().any(|channel| channel.enabled)
        && headend
            .channels
            .iter()
            .filter(|channel| channel.enabled)
            .all(|channel| {
                channel
                    .sinks
                    .iter()
                    .any(|sink| sink.enabled && sink.required)
                    && channel
                        .sinks
                        .iter()
                        .filter(|sink| sink.enabled && sink.required)
                        .all(|sink| sink.external_receiver == ExternalReceiverState::Verified)
            })
}

struct GuidedAirEvidence {
    ready_ingested_asset_ids: BTreeSet<String>,
    linked_reports: Vec<CommitReport>,
}

fn collect_guided_air_evidence(storage: &StationStore) -> Result<GuidedAirEvidence, String> {
    let ready_asset_ids = storage
        .list_media_assets()?
        .into_iter()
        .filter(|asset| {
            asset.readiness == AssetReadiness::Ready && Path::new(&asset.media_path).is_file()
        })
        .map(|asset| asset.asset_id)
        .collect::<BTreeSet<_>>();
    let guided_asset_completion = storage
        .list_ingest_jobs()?
        .into_iter()
        .filter(|job| job.status == IngestJobStatus::Completed)
        .filter_map(|job| Some((job.asset_id?, job.completed_at_unix_ms?)))
        .filter(|(asset_id, _)| ready_asset_ids.contains(asset_id))
        .fold(
            HashMap::<String, i64>::new(),
            |mut completed, (asset_id, at)| {
                completed
                    .entry(asset_id)
                    .and_modify(|existing| *existing = (*existing).min(at))
                    .or_insert(at);
                completed
            },
        );
    let ready_ingested_asset_ids = guided_asset_completion
        .keys()
        .cloned()
        .collect::<BTreeSet<_>>();

    let mut schedule_items = Vec::new();
    let mut reports = Vec::new();
    for channel in storage.list_channels()? {
        schedule_items.extend(storage.list_channel_schedule(&channel.channel_id)?);
        reports.extend(storage.list_commit_reports(&channel.channel_id)?);
    }
    let linked_reports = reports
        .into_iter()
        .filter(|report| {
            guided_asset_completion
                .get(&report.asset_id)
                .is_some_and(|completed_at| *completed_at <= report.approved_at_unix_ms)
        })
        .filter(|report| {
            schedule_items.iter().any(|item| {
                item.state == ScheduleState::Committed
                    && item.item_id == report.schedule_item_id
                    && item.channel_id == report.channel_id
                    && item.asset_id == report.asset_id
            })
        })
        .collect();
    Ok(GuidedAirEvidence {
        ready_ingested_asset_ids,
        linked_reports,
    })
}

fn verified_commissioning_bundle_receipt(
    storage: &StationStore,
    database_path: &Path,
    linked_reports: &[CommitReport],
) -> Result<Option<CommissioningSupportBundleReceipt>, String> {
    for receipt in storage.list_commissioning_support_bundle_receipts(1)? {
        let report_is_linked = linked_reports.iter().any(|report| {
            report.report_id == receipt.report_id
                && report.dispatch_status == DispatchStatus::Completed
                && receipt.created_at_unix_ms >= report.approved_at_unix_ms
        });
        if !report_is_linked {
            continue;
        }
        let Ok(bytes) = station_support::read_support_bundle(database_path, &receipt.bundle_id)
        else {
            continue;
        };
        if bytes.len() as u64 != receipt.archive_size_bytes {
            continue;
        }
        let actual_hash = format!("{:x}", Sha256::digest(&bytes));
        if actual_hash != receipt.archive_sha256 {
            continue;
        }
        return Ok(Some(receipt));
    }
    Ok(None)
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

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestLoudnessProofJob {
    sink_id: String,
    sample_duration_ms: u64,
    max_capture_bytes: u64,
}

#[derive(Debug, Serialize)]
struct LoudnessProofJobResponse {
    job_id: String,
    channel_id: String,
    target_group: String,
    sink_id: String,
    target_lufs_milli: i32,
    candidate_version: String,
    source_commit: String,
    configuration_hash: String,
    sample_duration_ms: u64,
    max_capture_bytes: u64,
    state: &'static str,
    created_at_unix_ms: i64,
    updated_at_unix_ms: i64,
    lease_expires_at_unix_ms: Option<i64>,
    capture: Option<LoudnessProofJobCaptureResponse>,
    completed_at_unix_ms: Option<i64>,
    failed_at_unix_ms: Option<i64>,
    failure_detail: Option<String>,
}

#[derive(Debug, Serialize)]
struct LoudnessProofJobCaptureResponse {
    artifact_relative_path: String,
    capture_bytes: u64,
    sample_duration_ms: u64,
    capture_sha256: String,
}

#[derive(Deserialize)]
struct PutStationProfile {
    #[serde(flatten)]
    profile: StationProfile,
    #[serde(default)]
    expected_revision: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PutReferenceStationTier {
    expected_revision: Option<u64>,
    catalog_version: u32,
    tier: ReferenceStationTier,
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

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BootstrapRequest {
    capability: String,
    username: String,
    display_name: String,
    password: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LoginRequest {
    username: String,
    password: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RecoverRequest {
    username: String,
    recovery_code: String,
    new_password: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChangePasswordRequest {
    current_password: String,
    new_password: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiResponse {
    pub status: u16,
    pub content_type: &'static str,
    pub attachment_name: Option<String>,
    pub headers: Vec<ApiResponseHeader>,
    pub body: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiResponseHeader {
    pub name: &'static str,
    pub value: String,
}

impl ApiResponse {
    pub fn with_header(
        mut self,
        name: &'static str,
        value: impl Into<String>,
    ) -> Result<Self, String> {
        const ALLOWED_RESPONSE_HEADERS: &[&str] = &[
            "Cache-Control",
            "Content-Security-Policy",
            "Pragma",
            "Referrer-Policy",
            "Retry-After",
            "Set-Cookie",
            "X-Content-Type-Options",
        ];
        let value = value.into();
        if !ALLOWED_RESPONSE_HEADERS.contains(&name) {
            return Err(format!("response header {name} is not allowed"));
        }
        if self.headers.len() >= 16 {
            return Err("response headers exceed the bounded count".into());
        }
        if value.is_empty()
            || value.len() > 4096
            || value
                .bytes()
                .any(|byte| byte == b'\r' || byte == b'\n' || byte == 0)
        {
            return Err(format!("response header {name} has an invalid value"));
        }
        self.headers.push(ApiResponseHeader { name, value });
        Ok(self)
    }
}

pub struct Api {
    storage: StationStore,
    database_path: PathBuf,
    application_directory: PathBuf,
    station_box_probe_override: Option<StationBoxProbeInput>,
    station_box_cache: Arc<Mutex<Option<(Instant, StationBoxProfile)>>>,
    orchestration_health: Arc<OrchestrationHealth>,
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
        Self::open_with_application_directory_and_health(
            database_path,
            application_directory,
            OrchestrationHealth::ready_without_supervisor(),
        )
    }

    pub fn open_with_application_directory_and_health(
        database_path: impl AsRef<Path>,
        application_directory: impl AsRef<Path>,
        orchestration_health: Arc<OrchestrationHealth>,
    ) -> Result<Self, String> {
        Self::open_with_application_directory_and_cache(
            database_path,
            application_directory,
            Arc::new(Mutex::new(None)),
            orchestration_health,
        )
    }

    fn open_with_application_directory_and_cache(
        database_path: impl AsRef<Path>,
        application_directory: impl AsRef<Path>,
        station_box_cache: Arc<Mutex<Option<(Instant, StationBoxProfile)>>>,
        orchestration_health: Arc<OrchestrationHealth>,
    ) -> Result<Self, String> {
        let database_path = database_path.as_ref().to_path_buf();
        Ok(Self {
            storage: StationStore::open(&database_path)?,
            database_path,
            application_directory: application_directory.as_ref().to_path_buf(),
            station_box_probe_override: None,
            station_box_cache,
            orchestration_health,
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
            orchestration_health: OrchestrationHealth::ready_without_supervisor(),
        })
    }

    /// Legacy alias for in-process tests and trusted embedding only.
    /// Production requests never call this entrypoint.
    pub fn handle(&self, method: &str, path: &str, body: Option<&[u8]>) -> ApiResponse {
        self.handle_in_process(method, path, body)
    }

    /// Bypasses HTTP authentication by explicit in-process authority.
    /// This exists for tests and trusted embedding; it is not used by the production dispatcher.
    pub fn handle_in_process(&self, method: &str, path: &str, body: Option<&[u8]>) -> ApiResponse {
        self.route(method, path, body, None)
    }

    fn handle_authenticated(
        &self,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
        actor: &OperatorAccount,
    ) -> ApiResponse {
        self.route(method, path, body, Some(actor))
    }

    fn route(
        &self,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
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
            ("GET", "/health") => self.health(),
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
            ("GET", "/api/v1/commissioning/report") => {
                self.commissioning_report(actor.is_some_and(|account| {
                    account.enabled()
                        && account
                            .roles()
                            .contains(&station_auth::OperatorRole::SetupAdmin)
                }))
            }
            ("GET", "/api/v1/station-box-profile") => self.station_box_profile(false),
            ("GET", "/api/v1/station-box-profile/readiness") => self.station_box_profile(true),
            ("PUT", "/api/v1/reference-station-tier") => {
                self.put_reference_station_tier(body, actor)
            }
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
                if path.starts_with("/api/v1/channels/") && path.ends_with("/loudness-proof") =>
            {
                self.request_channel_loudness_proof(path, body)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/")
                    && path.contains("/loudness-proof/jobs/") =>
            {
                self.channel_loudness_proof_job(path)
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
            ("POST", "/api/v1/live-takeovers") => self.request_live_takeover(body, actor),
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
                self.confirm_live_take(path, body, actor)
            }
            ("POST", path)
                if path.starts_with("/api/v1/live-takeovers/") && path.ends_with("/return") =>
            {
                self.request_live_return(path, body, actor)
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
                self.run_self_test(path, actor)
            }
            ("GET", "/api/v1/self-test-schedules") => self.list_self_test_schedules(),
            ("PUT", path) if path.starts_with("/api/v1/self-test-schedules/") => {
                self.put_self_test_schedule(path, body, actor)
            }
            ("POST", "/api/v1/support-bundles") => self.create_support_bundle(actor),
            ("GET", path)
                if path.starts_with("/api/v1/support-bundles/") && path.ends_with("/download") =>
            {
                self.download_support_bundle(path)
            }
            ("GET", "/api/v1/updates/status") => self.update_status(),
            ("POST", "/api/v1/updates/check") => self.check_updates(),
            ("POST", "/api/v1/updates/stage") => self.stage_update(),
            ("POST", "/api/v1/updates/prepare") => self.prepare_update(),
            ("POST", "/api/v1/updates/apply") => self.apply_update(body, actor),
            ("GET", path) if path.starts_with("/api/v1/updates/release-notes?") => {
                self.update_release_notes(path)
            }
            ("PUT", "/api/v1/alert-channels") => self.put_alert_channel(body),
            ("GET", "/api/v1/alert-channels") => self.list_alert_channels(),
            ("GET", "/api/v1/alert-rules") => self.list_alert_rules(),
            ("PUT", path) if path.starts_with("/api/v1/alert-rules/") => {
                self.put_alert_rule(path, body, actor)
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
                self.acknowledge_alert(path, body, actor)
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
            ("PUT", "/api/v1/cg/board") => self.write_cg_board(body, actor),
            ("GET", path) if path.starts_with("/api/v1/cg/zones?") => self.list_cg_zones(path),
            ("PUT", "/api/v1/cg/zones") => self.write_cg_zone(body, actor),
            ("DELETE", path) if path.starts_with("/api/v1/cg/zones/") => {
                self.delete_cg_zone(path, body, actor)
            }
            ("GET", path) if path.starts_with("/api/v1/cg/feeds?") => self.list_cg_feeds(path),
            ("PUT", "/api/v1/cg/feeds") => self.write_cg_feed(body, actor),
            ("DELETE", path) if path.starts_with("/api/v1/cg/feeds/") => {
                self.delete_cg_feed(path, body, actor)
            }
            ("GET", path) if path.starts_with("/api/v1/cg/feed-items?") => {
                self.list_cg_feed_items(path)
            }
            ("POST", "/api/v1/cg/feed-items/approve") => self.approve_cg_feed_item(body, actor),
            ("GET", path) if path.starts_with("/api/v1/cg/bulletins?") => {
                self.list_cg_bulletins(path)
            }
            ("PUT", "/api/v1/cg/bulletins") => self.write_cg_bulletin(body, actor),
            ("POST", "/api/v1/cg/bulletins/expire") => self.expire_cg_bulletins(body, actor),
            ("GET", path) if path.starts_with("/api/v1/cg/audit?") => self.list_cg_audit(path),
            ("PUT", "/api/v1/schedule/items") => self.put_schedule_item(body),
            ("POST", "/api/v1/schedule/prepare") => self.prepare_schedule_commit(body),
            ("POST", "/api/v1/schedule/commit") => self.commit_schedule(body, actor),
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

    fn health(&self) -> ApiResponse {
        let status = self.orchestration_health.status();
        let http_status = if status == OrchestrationHealthStatus::Ready {
            200
        } else {
            503
        };
        json_response(
            http_status,
            serde_json::to_vec(&serde_json::json!({
                "database": "ready",
                "orchestration": { "status": status },
                "status": status,
            }))
            .expect("serializing health cannot fail"),
        )
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

    fn put_reference_station_tier(
        &self,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(actor) = actor.filter(|account| {
            account.enabled()
                && account
                    .roles()
                    .contains(&station_auth::OperatorRole::SetupAdmin)
        }) else {
            return error_response(
                403,
                "setup_admin_required",
                "Selecting the reference station tier requires the Setup Admin role.",
            );
        };
        let Some(command) = parse_body::<PutReferenceStationTier>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A reference station tier selection body is required.",
            );
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let request = ReferenceStationTierWrite {
            expected_revision: command.expected_revision,
            catalog_version: command.catalog_version,
            tier: command.tier,
            actor: actor.account_id().as_str().into(),
            occurred_at_unix_ms: now,
        };
        match self
            .storage
            .write_reference_station_tier_selection(&request)
        {
            Ok(selection) => {
                if let Ok(mut cache) = self.station_box_cache.lock() {
                    *cache = None;
                }
                json_response(
                    if selection.revision == 1 { 201 } else { 200 },
                    serde_json::to_vec(&selection)
                        .expect("serializing a reference station selection cannot fail"),
                )
            }
            Err(ReferenceStationTierWriteError::Conflict { current_revision }) => error_response(
                409,
                "revision_conflict",
                &format!(
                    "The selected reference station tier changed; expected revision {:?}, current revision {:?}.",
                    command.expected_revision, current_revision
                ),
            ),
            Err(ReferenceStationTierWriteError::CatalogVersionMismatch { expected, actual }) => {
                error_response(
                    409,
                    "catalog_version_conflict",
                    &format!(
                        "The reference station catalog changed; submitted version {actual}, current version {expected}."
                    ),
                )
            }
            Err(ReferenceStationTierWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", &message)
            }
            Err(ReferenceStationTierWriteError::Storage(message)) => {
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

    fn request_live_takeover(
        &self,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(command) =
            parse_authenticated_body::<RequestLiveTakeover>(body, actor, &[&["operator_id"]])
        else {
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
            operator_id: mutation_actor(actor, &command.operator_id),
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

    fn confirm_live_take(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        self.transition_live_takeover(
            path,
            body,
            TakeoverState::Armed,
            TakeoverState::TakeRequested,
            actor,
        )
    }

    fn request_live_return(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        self.transition_live_takeover(
            path,
            body,
            TakeoverState::OnAir,
            TakeoverState::ReturnRequested,
            actor,
        )
    }

    fn transition_live_takeover(
        &self,
        path: &str,
        body: Option<&[u8]>,
        expected: TakeoverState,
        next: TakeoverState,
        actor: Option<&OperatorAccount>,
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
        let Some(command) =
            parse_authenticated_body::<ConfirmLiveTransition>(body, actor, &[&["actor_id"]])
        else {
            return error_response(
                400,
                "invalid_json",
                "A takeover transition body is required.",
            );
        };
        let actor_id = mutation_actor(actor, &command.actor_id);
        if actor_id.trim().is_empty()
            || actor_id.trim() != actor_id
            || actor_id.chars().any(char::is_control)
            || actor_id.len() > 120
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
            &actor_id,
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

    fn request_channel_loudness_proof(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/loudness-proof") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let Some(request) = parse_body::<RequestLoudnessProofJob>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A loudness proof job request body is required.",
            );
        };
        if !is_canonical_api_uuid(&request.sink_id)
            || !(LOUDNESS_PROOF_MIN_DURATION_MS..=LOUDNESS_PROOF_MAX_DURATION_MS)
                .contains(&request.sample_duration_ms)
            || !(1..=LOUDNESS_PROOF_MAX_CAPTURE_BYTES).contains(&request.max_capture_bytes)
        {
            return error_response(
                422,
                "validation_failed",
                "The sink identity, sample duration, or capture limit is invalid or unbounded.",
            );
        }

        let station_box = match self.station_box_profile_uncached() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        if !station_box.release.candidate_verified {
            return error_response(
                409,
                "candidate_not_verified",
                "A verified installed candidate is required before requesting loudness proof.",
            );
        }
        let Some(source_commit) = station_box.source_commit.as_deref() else {
            return error_response(
                409,
                "candidate_not_verified",
                "The verified candidate has no usable source commit identity.",
            );
        };
        if !is_api_source_commit(source_commit) {
            return error_response(
                409,
                "candidate_not_verified",
                "The verified candidate source commit identity is invalid.",
            );
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
        let Some(sink) = snapshot
            .output_sinks
            .iter()
            .find(|sink| sink.sink_id == request.sink_id)
        else {
            return error_response(
                409,
                "sink_not_enabled",
                "The requested sink is not an enabled UDP or SRT output.",
            );
        };
        let target_lufs_milli = (sink.loudness_target_lufs * 1_000.0).round() as i32;
        let target_group = match target_lufs_milli {
            -24_000 => "cable",
            -16_000 => "streaming",
            _ => {
                return error_response(
                    409,
                    "unsupported_loudness_target",
                    "The requested sink has no executable loudness target.",
                );
            }
        };
        let created_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let job_id = match secure_canonical_uuid() {
            Ok(value) => value,
            Err(message) => return error_response(500, "entropy_unavailable", &message),
        };
        let job = OutputLoudnessProofJob {
            job_id,
            channel_id: channel_id.into(),
            target_group: target_group.into(),
            sink_id: sink.sink_id.clone(),
            target_lufs_milli,
            candidate_version: station_box.product_version,
            source_commit: Some(source_commit.into()),
            configuration_hash: snapshot.configuration_hash,
            sample_duration_ms: request.sample_duration_ms,
            max_capture_bytes: request.max_capture_bytes,
            created_at_unix_ms,
        };
        let created = match self.storage.create_output_loudness_job(&job) {
            Ok(created) => created,
            Err(message) => return loudness_job_storage_error(&message),
        };
        if !created {
            return error_response(
                409,
                "job_already_active",
                "An active loudness proof job already exists for this target group.",
            );
        }
        let record = match self.storage.read_output_loudness_job(&job.job_id) {
            Ok(Some(record)) => record,
            Ok(None) => {
                return error_response(500, "storage_error", "The created job disappeared.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        json_response(
            202,
            serde_json::to_vec(&loudness_job_response(record))
                .expect("serializing loudness proof job response cannot fail"),
        )
    }

    fn channel_loudness_proof_job(&self, path: &str) -> ApiResponse {
        let Some((channel_id, job_id)) = loudness_job_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The channel or job identity is invalid.",
            );
        };
        if !is_api_uuid(channel_id) || !is_canonical_api_uuid(job_id) {
            return error_response(
                400,
                "invalid_path",
                "The channel or job identity is invalid.",
            );
        }
        let record = match self.storage.read_output_loudness_job(job_id) {
            Ok(Some(record)) => record,
            Ok(None) => {
                return error_response(
                    404,
                    "job_not_found",
                    "The loudness proof job does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if record.channel_id != channel_id {
            return error_response(
                404,
                "job_not_found",
                "The loudness proof job does not exist.",
            );
        }
        json_response(
            200,
            serde_json::to_vec(&loudness_job_response(record))
                .expect("serializing loudness proof job response cannot fail"),
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
        let station_box = match self.cached_station_box_profile() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        let generated_at_unix_ms = match system_time_millis() {
            Ok(value) => value.max(station_box.generated_at_unix_ms),
            Err(message) => return error_response(500, "clock_error", &message),
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
            Some(probe) => {
                let mut probe = probe.clone();
                // Test-injected hardware facts remain deterministic, but the
                // assembled profile is generated now. This preserves the same
                // temporal contract as the production collector when current
                // persisted authority (such as a tier selection) is attached.
                probe.generated_at_unix_ms = generated_at_unix_ms;
                probe.probe_collected_at_unix_ms = generated_at_unix_ms;
                probe.hardware.machine_inventory.collected_at_unix_ms = generated_at_unix_ms;
                probe.reference_station_tier_selection = self
                    .storage
                    .read_reference_station_tier_selection()
                    .map_err(|message| {
                        format!("read selected reference station tier: {message}")
                    })?;
                probe
            }
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

    fn commissioning_report(&self, authenticated_setup_admin: bool) -> ApiResponse {
        let station_box = match self.cached_station_box_profile() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        let generated_at_unix_ms = match system_time_millis() {
            Ok(value) => value.max(station_box.generated_at_unix_ms),
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let mut checks = Vec::new();

        let profile = match self.storage.read_profile() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        checks.push(match profile.as_ref() {
            Some(profile) => commissioning_check(
                "station_profile",
                "Station identity",
                CommissioningPhase::Identity,
                CommissioningCheckStatus::Pass,
                true,
                Some("station"),
                format!(
                    "{} · {} (revision {})",
                    profile.profile.display_name, profile.profile.timezone, profile.revision
                ),
                "authoritative station profile in SQLite",
                CommissioningBoundary::LocalAuthority,
            ),
            None => commissioning_check(
                "station_profile",
                "Station identity",
                CommissioningPhase::Identity,
                CommissioningCheckStatus::Blocked,
                true,
                Some("station"),
                "No station identity and IANA timezone have been commissioned.",
                "GET /api/v1/station returned no profile",
                CommissioningBoundary::LocalAuthority,
            ),
        });
        checks.push(commissioning_check(
            "operator_account_auth",
            "Operator account and authentication",
            CommissioningPhase::Identity,
            if authenticated_setup_admin {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            None,
            if authenticated_setup_admin {
                "An enabled setup administrator reached this report through a valid current-boot session."
            } else {
                "This report was not reached through an enabled setup administrator session."
            },
            if authenticated_setup_admin {
                "current authenticated setup_admin account and session authority"
            } else {
                "authenticated setup_admin report authority is absent"
            },
            CommissioningBoundary::LocalAuthority,
        ));
        checks.push(commissioning_check(
            "runtime_api",
            "Local runtime and database authority",
            CommissioningPhase::StationBox,
            CommissioningCheckStatus::Pass,
            true,
            None,
            "The station API and SQLite authority are serving this report.",
            "current stationd request and successful SQLite reads",
            CommissioningBoundary::LocalAuthority,
        ));

        let station_box_storage_pass = station_box.hardware.state == StationReadinessState::Pass
            && station_box.engine.state == StationReadinessState::Pass;
        checks.push(commissioning_check(
            "station_box_storage",
            "StationBox hardware, engine, and media storage",
            CommissioningPhase::StationBox,
            if station_box_storage_pass {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("health"),
            format!(
                "{} logical processors, {:.1} GB RAM, {:.1} GB media storage free; engine state {:?}.",
                station_box.hardware.logical_cpus,
                station_box.hardware.ram_total_gb,
                station_box.hardware.media_disk_free_gb,
                station_box.engine.state
            ),
            "current StationBox Windows resource and installed-engine probes",
            CommissioningBoundary::LocalAuthority,
        ));
        checks.push(commissioning_check(
            "station_time",
            "Station timezone and synchronized time",
            CommissioningPhase::StationBox,
            if station_box.clock.state == StationReadinessState::Pass {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("health"),
            format!(
                "Configured timezone: {}; clock synchronization: {:?}.",
                station_box
                    .clock
                    .configured_timezone
                    .as_deref()
                    .unwrap_or("not configured"),
                station_box.clock.sync_status
            ),
            "configured station timezone and current operating-system clock probe",
            CommissioningBoundary::LocalAuthority,
        ));
        checks.push(commissioning_check(
            "station_backup",
            "Writable station backup",
            CommissioningPhase::StationBox,
            if station_box.backup.state == StationReadinessState::Pass {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("health"),
            station_box
                .backup
                .detail
                .clone()
                .unwrap_or_else(|| "Backup readiness has no current evidence.".into()),
            "current synchronized backup write/read/delete probe",
            CommissioningBoundary::LocalAuthority,
        ));

        let watch_folders = match self.storage.list_watch_folders() {
            Ok(configs) => configs,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let ready_watch_folders = watch_folders
            .iter()
            .filter(|config| {
                config.enabled
                    && Path::new(&config.monitor_path).is_dir()
                    && config.last_scan_at_unix_ms.is_some()
                    && config.last_error.is_none()
            })
            .collect::<Vec<_>>();
        let ready_watch_folder_count = ready_watch_folders.len();
        checks.push(commissioning_check(
            "channel_watch_folder",
            "Channel watch folder",
            CommissioningPhase::SourcesOutputs,
            if ready_watch_folder_count > 0 {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("media"),
            if ready_watch_folder_count > 0 {
                format!("{ready_watch_folder_count} enabled watch folder(s) have a present directory and successful scan evidence.")
            } else if watch_folders.is_empty() {
                "No watch folder is configured.".into()
            } else {
                "No enabled watch folder has both a present directory and successful scan evidence.".into()
            },
            if ready_watch_folders.is_empty() {
                "no enabled watch-folder identity has current directory and scan evidence".into()
            } else {
                format!(
                    "watch folder(s) {} have authoritative configuration, filesystem presence, and durable successful scan results",
                    ready_watch_folders
                        .iter()
                        .map(|config| config.config_id.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            },
            CommissioningBoundary::LocalAuthority,
        ));

        let channels = match self.storage.list_channels() {
            Ok(channels) => channels,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let enabled: Vec<_> = channels.iter().filter(|channel| channel.enabled).collect();
        let safe_to_air = if enabled.is_empty() {
            None
        } else {
            let mut runtime_channels = Vec::with_capacity(enabled.len());
            for channel in &enabled {
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
            Some(safe_to_air)
        };
        checks.push(commissioning_check(
            "fallback_slate",
            "Fallback output and configured station slate",
            CommissioningPhase::SourcesOutputs,
            CommissioningCheckStatus::Blocked,
            true,
            Some("channels"),
            match safe_to_air.as_ref() {
                Some(status) if status.color == SafeToAirColor::Green => format!(
                    "{} No durable operator-selected fallback/slate authority exists yet.",
                    status.operator_message
                ),
                Some(status) => format!(
                    "{} A durable operator-selected fallback/slate authority is also missing.",
                    status.operator_message
                ),
                None => "No enabled channel or durable operator-selected fallback/slate authority exists.".into(),
            },
            "current heartbeat-derived runtime plus absence of a durable fallback/slate configuration record",
            CommissioningBoundary::LocalAuthority,
        ));
        checks.push(commissioning_check(
            "cea708_captions",
            "CEA-708 caption path",
            CommissioningPhase::SourcesOutputs,
            CommissioningCheckStatus::Blocked,
            true,
            Some("channels"),
            "No typed CEA-708 ingest, preservation, playout, or output-proof authority exists yet.",
            "CEA-708 is absent from current media, schedule, worker, and output contracts",
            CommissioningBoundary::LocalAuthority,
        ));

        checks.push(commissioning_check(
            "publish_targets",
            "Required publish targets",
            CommissioningPhase::SourcesOutputs,
            CommissioningCheckStatus::Blocked,
            true,
            None,
            "Generic channel output sinks and headend destinations are not publish-target account authority. No server-authoritative publish-target model exists yet.",
            "the current control plane has no publish-target credential/account model",
            CommissioningBoundary::LocalAuthority,
        ));
        checks.push(commissioning_check(
            "automation_model_policy",
            "Automation and model policy",
            CommissioningPhase::SourcesOutputs,
            CommissioningCheckStatus::Blocked,
            true,
            None,
            "No server-authoritative automation policy, model selection policy, or approval boundary exists yet.",
            "the current control plane has no automation/model policy authority",
            CommissioningBoundary::LocalAuthority,
        ));

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
        let named_headend_ready = !enabled.is_empty()
            && enabled.iter().all(|channel| {
                channel
                    .output_sinks
                    .iter()
                    .any(|sink| sink.enabled && sink.required && sink.headend.is_some())
            });
        let local_headend_ready = named_headend_ready
            && headend
                .channels
                .iter()
                .filter(|channel| channel.enabled)
                .all(|channel| {
                    channel.profile_state == StationReadinessState::Pass
                        && channel
                            .sinks
                            .iter()
                            .filter(|sink| sink.enabled && sink.required)
                            .all(|sink| sink.local_transport == StationReadinessState::Pass)
                });
        checks.push(commissioning_check(
            "headend_output_setup",
            "Named headend output and fresh local transport",
            CommissioningPhase::SourcesOutputs,
            if local_headend_ready {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("channels"),
            if local_headend_ready {
                String::from("Every enabled channel has a named headend contract and fresh required-sink local transport progress. Receiver acceptance remains separate.")
            } else if !named_headend_ready {
                String::from("Every enabled channel requires an enabled required sink with a named headend profile.")
            } else {
                String::from("Named headend configuration exists, but required local transport is missing, stale, unsupported, or failed.")
            },
            "current StationBox capability, named sink contract, and worker heartbeat authority",
            CommissioningBoundary::LocalAuthority,
        ));
        checks.push(commissioning_check(
            "cable_transport_validation",
            "Cable transport analysis and device reachability",
            CommissioningPhase::SourcesOutputs,
            CommissioningCheckStatus::Blocked,
            true,
            Some("health"),
            "No durable TSDuck transport-analysis result and no authenticated target-device reachability result are stored yet.",
            "the current control plane has no TSDuck/device-reachability evidence authority",
            CommissioningBoundary::LocalAuthority,
        ));

        let guided_air_evidence = match collect_guided_air_evidence(&self.storage) {
            Ok(evidence) => evidence,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let guided_asset_ids = &guided_air_evidence.ready_ingested_asset_ids;
        checks.push(commissioning_check(
            "guided_ingest",
            "Guided real media ingest",
            CommissioningPhase::GuidedAirProof,
            if guided_asset_ids.is_empty() {
                CommissioningCheckStatus::Blocked
            } else {
                CommissioningCheckStatus::Pass
            },
            true,
            Some("media"),
            if guided_asset_ids.is_empty() {
                "No completed ingest job resolves to a ready managed asset whose bytes are still present.".into()
            } else {
                format!("{} completed ingest chain(s) resolve to ready managed media with present bytes.", guided_asset_ids.len())
            },
            "durable ingest job linked to current media catalog and filesystem bytes",
            CommissioningBoundary::LocalAuthority,
        ));

        let linked_reports = &guided_air_evidence.linked_reports;
        checks.push(commissioning_check(
            "guided_schedule",
            "Guided schedule approval",
            CommissioningPhase::GuidedAirProof,
            if linked_reports.is_empty() {
                CommissioningCheckStatus::Blocked
            } else {
                CommissioningCheckStatus::Pass
            },
            true,
            Some("schedule"),
            if linked_reports.is_empty() {
                "No committed schedule report is linked to the guided ingested asset.".into()
            } else {
                format!(
                    "{} committed report(s) are linked to guided ingested media.",
                    linked_reports.len()
                )
            },
            "exact asset, schedule item, channel, and commit-report linkage in SQLite",
            CommissioningBoundary::LocalAuthority,
        ));
        let acknowledged = linked_reports
            .iter()
            .filter(|report| {
                matches!(
                    report.dispatch_status,
                    DispatchStatus::Acknowledged | DispatchStatus::Completed
                )
            })
            .count();
        let acknowledged_evidence = linked_reports
            .iter()
            .find(|report| {
                matches!(
                    report.dispatch_status,
                    DispatchStatus::Acknowledged | DispatchStatus::Completed
                )
            })
            .map_or_else(
                || "no linked commit report has worker air acknowledgment".into(),
                |report| {
                    format!(
                        "commit report {} links asset {}, schedule item {}, and channel {} under worker status {:?}",
                        report.report_id,
                        report.asset_id,
                        report.schedule_item_id,
                        report.channel_id,
                        report.dispatch_status
                    )
                },
            );
        checks.push(commissioning_check(
            "guided_air_proof",
            "Worker-acknowledged guided air proof",
            CommissioningPhase::GuidedAirProof,
            if acknowledged > 0 {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("schedule"),
            if acknowledged > 0 {
                format!("{acknowledged} causally linked report(s) reached worker acknowledged or completed.")
            } else {
                "The linked guided schedule has not reached worker acknowledged or completed.".into()
            },
            acknowledged_evidence,
            CommissioningBoundary::LocalAuthority,
        ));
        checks.push(commissioning_check(
            "selected_sink_ten_minute_proof",
            "Ten-minute selected-sink output proof",
            CommissioningPhase::GuidedAirProof,
            CommissioningCheckStatus::Blocked,
            true,
            Some("health"),
            "Current output proof is bounded below ten minutes and cannot satisfy the required selected-sink report.",
            "no durable ten-minute selected-sink output proof authority exists",
            CommissioningBoundary::LocalAuthority,
        ));
        let commissioning_bundle = match verified_commissioning_bundle_receipt(
            &self.storage,
            &self.database_path,
            linked_reports,
        ) {
            Ok(receipt) => receipt,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        checks.push(commissioning_check(
            "support_bundle",
            "Durable commissioning support bundle",
            CommissioningPhase::GuidedAirProof,
            if commissioning_bundle.is_some() {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("health"),
            commissioning_bundle.as_ref().map_or_else(
                || "No durable support-bundle receipt is linked to the completed guided broadcast with matching current archive bytes.".into(),
                |receipt| format!(
                    "Support bundle {} is durably linked to completed report {} and its current bytes match SHA-256 {}.",
                    receipt.bundle_id, receipt.report_id, receipt.archive_sha256
                ),
            ),
            commissioning_bundle.as_ref().map_or_else(
                || "no completed guided report has a current readable hash-matching support-bundle receipt".into(),
                |receipt| format!(
                    "append-only receipt by {} at {} binds report {}, bundle {}, {} bytes, and SHA-256 {}",
                    receipt.created_by,
                    receipt.created_at_unix_ms,
                    receipt.report_id,
                    receipt.bundle_id,
                    receipt.archive_size_bytes,
                    receipt.archive_sha256
                ),
            ),
            CommissioningBoundary::LocalAuthority,
        ));
        let completed = linked_reports
            .iter()
            .filter(|report| report.dispatch_status == DispatchStatus::Completed)
            .count();
        let completed_evidence = linked_reports
            .iter()
            .find(|report| report.dispatch_status == DispatchStatus::Completed)
            .map_or_else(
                || "no linked commit report has completed under worker authority".into(),
                |report| {
                    format!(
                        "completed commit report {} links asset {}, schedule item {}, and channel {}",
                        report.report_id,
                        report.asset_id,
                        report.schedule_item_id,
                        report.channel_id
                    )
                },
            );
        checks.push(commissioning_check(
            "first_broadcast_report",
            "First broadcast completion report",
            CommissioningPhase::GuidedAirProof,
            if completed > 0 {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("schedule"),
            if completed > 0 {
                format!("{completed} causally linked first-broadcast report(s) completed after worker return.")
            } else {
                "No causally linked guided broadcast has completed and returned under worker authority.".into()
            },
            completed_evidence,
            CommissioningBoundary::LocalAuthority,
        ));

        let receiver_verified = headend_receiver_verified(&headend);
        checks.push(commissioning_check(
            "external_receiver_proof",
            "External receiver acceptance",
            CommissioningPhase::ExternalProof,
            if receiver_verified {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::NotVerified
            },
            false,
            None,
            if receiver_verified {
                String::from("Every enabled required headend sink has authenticated receiver acceptance evidence.")
            } else {
                String::from("Local transport does not prove that a cable receiver decoded or accepted the signal.")
            },
            "external receiver evidence boundary; current worker progress is insufficient",
            CommissioningBoundary::ExternalEvidence,
        ));
        for (id, label, detail) in [
            (
                "clean_machine_install_proof",
                "Clean-machine installation proof",
                "No clean-machine installation receipt is authoritative in this station database.",
            ),
            (
                "one_station_week_proof",
                "One-station one-week field proof",
                "The required one-station week of field operation is not verified by this control plane.",
            ),
            (
                "three_station_week_proof",
                "Three-station one-week field proof",
                "The required three-station week of field operation is not verified by this control plane.",
            ),
        ] {
            checks.push(commissioning_check(
                id,
                label,
                CommissioningPhase::ExternalProof,
                CommissioningCheckStatus::NotVerified,
                false,
                None,
                detail,
                "no authenticated external proof record is stored by the current control plane",
                CommissioningBoundary::ExternalEvidence,
            ));
        }

        let required_total = checks.iter().filter(|check| check.required).count();
        let required_passed = checks
            .iter()
            .filter(|check| check.required && check.status == CommissioningCheckStatus::Pass)
            .count();
        let current_step_id = checks
            .iter()
            .find(|check| check.required && check.status != CommissioningCheckStatus::Pass)
            .map(|check| check.id);
        let core_complete = required_passed == required_total;
        let overall_status = if !core_complete {
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
                schema_version: 2,
                generated_at_unix_ms,
                overall_status,
                current_step_id,
                core_progress: CommissioningCoreProgress {
                    passed: required_passed,
                    total: required_total,
                },
                core_complete,
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

    fn run_self_test(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
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
            actor.map(|account| account.account_id().as_str().to_owned()),
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

    fn put_self_test_schedule(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
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
        let Some(command) =
            parse_authenticated_body::<PutSelfTestSchedule>(body, actor, &[&["updated_by"]])
        else {
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
            updated_by: mutation_actor(actor, &command.updated_by),
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

    fn create_support_bundle(&self, actor: Option<&OperatorAccount>) -> ApiResponse {
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match station_support::create_support_bundle(
            &self.database_path,
            &self.application_directory,
            now,
        ) {
            Ok(record) => {
                let evidence = match collect_guided_air_evidence(&self.storage) {
                    Ok(evidence) => evidence,
                    Err(message) => return error_response(500, "storage_error", &message),
                };
                let completed_report = evidence
                    .linked_reports
                    .iter()
                    .filter(|report| report.dispatch_status == DispatchStatus::Completed)
                    .max_by_key(|report| (report.approved_at_unix_ms, report.report_id.as_str()));
                let commissioning_report_id = if let Some(report) = completed_report {
                    let receipt = CommissioningSupportBundleReceipt {
                        bundle_id: record.bundle_id.clone(),
                        report_id: report.report_id.clone(),
                        archive_sha256: record.sha256.to_ascii_lowercase(),
                        archive_size_bytes: record.size_bytes,
                        created_at_unix_ms: record.created_at_unix_ms,
                        created_by: actor
                            .map(|account| account.account_id().as_str().to_owned())
                            .unwrap_or_else(|| "trusted-in-process".into()),
                    };
                    if let Err(message) = self
                        .storage
                        .append_commissioning_support_bundle_receipt(&receipt)
                    {
                        return error_response(500, "support_bundle_receipt_failed", &message);
                    }
                    Some(report.report_id.clone())
                } else {
                    None
                };
                json_response(
                    201,
                    serde_json::to_vec(&SupportBundleCreationResponse {
                        bundle: record,
                        commissioning_report_id,
                    })
                    .expect("serializing a support bundle cannot fail"),
                )
            }
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

    fn apply_update(&self, body: Option<&[u8]>, actor: Option<&OperatorAccount>) -> ApiResponse {
        let Some(command) =
            parse_authenticated_body::<ApplyUpdate>(body, actor, &[&["requested_by"]])
        else {
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
            &mutation_actor(actor, &command.requested_by),
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

    fn put_alert_rule(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(rule_id) = path.strip_prefix("/api/v1/alert-rules/") else {
            unreachable!("the alert-rule route was checked")
        };
        if rule_id.is_empty() || rule_id.contains('/') {
            return error_response(400, "invalid_path", "The alert rule identity is invalid.");
        }
        let Some(command) =
            parse_authenticated_body::<PutAlertRule>(body, actor, &[&["updated_by"]])
        else {
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
            updated_by: mutation_actor(actor, &command.updated_by),
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

    fn acknowledge_alert(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(event_id) = path
            .strip_prefix("/api/v1/alerts/")
            .and_then(|value| value.strip_suffix("/acknowledge"))
        else {
            unreachable!("the alert acknowledgment route was checked")
        };
        if event_id.is_empty() || event_id.contains('/') {
            return error_response(400, "invalid_path", "The alert identity is invalid.");
        }
        let Some(command) =
            parse_authenticated_body::<AcknowledgeAlert>(body, actor, &[&["acknowledged_by"]])
        else {
            return error_response(
                400,
                "invalid_json",
                "An alert acknowledgment body is required.",
            );
        };
        let acknowledged_by = mutation_actor(actor, &command.acknowledged_by);
        if acknowledged_by.trim().is_empty() || acknowledged_by.len() > 120 {
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
            .acknowledge_alert(event_id, now, &acknowledged_by)
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

    fn commit_schedule(&self, body: Option<&[u8]>, actor: Option<&OperatorAccount>) -> ApiResponse {
        let Some(command) =
            parse_authenticated_body::<CommitSchedule>(body, actor, &[&["approved_by"]])
        else {
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
            &mutation_actor(actor, &command.approved_by),
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

    fn write_cg_board(&self, body: Option<&[u8]>, actor: Option<&OperatorAccount>) -> ApiResponse {
        let Some(mut command) = parse_authenticated_body::<WriteCgBoard>(
            body,
            actor,
            &[&["board", "created_by"], &["audit", "operator_id"]],
        ) else {
            return error_response(400, "invalid_json", "A CG board write body is required.");
        };
        if actor.is_some() {
            match self.storage.read_cg_board(&command.board.channel_id) {
                Ok(Some(existing)) => command.board.created_by = existing.created_by,
                Ok(None) => {}
                Err(message) => return error_response(500, "storage_error", &message),
            }
        }
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

    fn write_cg_zone(&self, body: Option<&[u8]>, actor: Option<&OperatorAccount>) -> ApiResponse {
        let Some(command) =
            parse_authenticated_body::<WriteCgZone>(body, actor, &[&["audit", "operator_id"]])
        else {
            return error_response(400, "invalid_json", "A CG zone write body is required.");
        };
        cg_write_response(self.storage.write_cg_zone(
            &command.zone,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn delete_cg_zone(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(zone_id) = cg_resource_id(path, "/api/v1/cg/zones/") else {
            return error_response(400, "invalid_path", "The CG zone identity is invalid.");
        };
        let Some(command) =
            parse_authenticated_body::<DeleteCgResource>(body, actor, &[&["audit", "operator_id"]])
        else {
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

    fn write_cg_feed(&self, body: Option<&[u8]>, actor: Option<&OperatorAccount>) -> ApiResponse {
        let Some(mut command) = parse_authenticated_body::<WriteCgFeedSource>(
            body,
            actor,
            &[&["feed", "created_by"], &["audit", "operator_id"]],
        ) else {
            return error_response(400, "invalid_json", "A CG feed write body is required.");
        };
        if actor.is_some() {
            match self
                .storage
                .read_cg_feed_source(&command.feed.feed_source_id)
            {
                Ok(Some(existing)) => command.feed.created_by = existing.created_by,
                Ok(None) => {}
                Err(message) => return error_response(500, "storage_error", &message),
            }
        }
        cg_write_response(self.storage.write_cg_feed_source(
            &command.feed,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn delete_cg_feed(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(feed_id) = cg_resource_id(path, "/api/v1/cg/feeds/") else {
            return error_response(400, "invalid_path", "The CG feed identity is invalid.");
        };
        let Some(command) =
            parse_authenticated_body::<DeleteCgResource>(body, actor, &[&["audit", "operator_id"]])
        else {
            return error_response(400, "invalid_json", "A CG feed deletion body is required.");
        };
        cg_delete_response(self.storage.delete_cg_feed_source(
            feed_id,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn approve_cg_feed_item(
        &self,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(command) = parse_authenticated_body::<ApproveCgFeedItem>(
            body,
            actor,
            &[&["approval", "approved_by"], &["audit", "operator_id"]],
        ) else {
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

    fn write_cg_bulletin(
        &self,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(mut command) = parse_authenticated_body::<WriteCgBulletin>(
            body,
            actor,
            &[&["bulletin", "submitted_by"], &["audit", "operator_id"]],
        ) else {
            return error_response(400, "invalid_json", "A CG bulletin write body is required.");
        };
        if let Some(actor) = actor {
            match self.storage.read_cg_bulletin(&command.bulletin.bulletin_id) {
                Ok(Some(existing)) => {
                    command.bulletin.submitted_by = existing.submitted_by;
                    let moderation_transition = existing.state != command.bulletin.state
                        && (matches!(
                            command.bulletin.state,
                            CgBulletinState::NeedsChanges
                                | CgBulletinState::Accepted
                                | CgBulletinState::Declined
                        ) || (existing.moderated_by.is_none()
                            && command.bulletin.state == CgBulletinState::Scheduled));
                    command.bulletin.moderated_by = if moderation_transition {
                        Some(actor.account_id().as_str().to_owned())
                    } else {
                        existing.moderated_by
                    };
                }
                Ok(None) => command.bulletin.moderated_by = None,
                Err(message) => return error_response(500, "storage_error", &message),
            }
        }
        cg_write_response(self.storage.write_cg_bulletin(
            &command.bulletin,
            command.expected_revision,
            &command.audit,
        ))
    }

    fn expire_cg_bulletins(
        &self,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(command) =
            parse_authenticated_body::<ExpireCgBulletins>(body, actor, &[&["operator_id"]])
        else {
            return error_response(
                400,
                "invalid_json",
                "A CG bulletin-expiration command body is required.",
            );
        };
        cg_write_response(self.storage.expire_due_cg_bulletins(
            &command.channel_id,
            command.now_unix_ms,
            &mutation_actor(actor, &command.operator_id),
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

fn parse_authenticated_body<T: serde::de::DeserializeOwned>(
    body: Option<&[u8]>,
    actor: Option<&OperatorAccount>,
    identity_paths: &[&[&str]],
) -> Option<T> {
    let Some(actor) = actor else {
        return parse_body(body);
    };
    let mut value: serde_json::Value = serde_json::from_slice(body?).ok()?;
    for path in identity_paths {
        let (field, parents) = path.split_last()?;
        let mut object = value.as_object_mut()?;
        for parent in parents {
            object = object.get_mut(*parent)?.as_object_mut()?;
        }
        object.insert(
            (*field).to_owned(),
            serde_json::Value::String(actor.account_id().as_str().to_owned()),
        );
    }
    serde_json::from_value(value).ok()
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

fn is_canonical_api_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        && bytes.iter().enumerate().all(|(index, byte)| {
            matches!(index, 8 | 13 | 18 | 23)
                || byte.is_ascii_digit()
                || (b'a'..=b'f').contains(byte)
        })
}

fn is_api_source_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn secure_canonical_uuid() -> Result<String, String> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|error| {
        format!("the operating system could not generate a job identity: {error:?}")
    })?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(format!(
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
        bytes[15],
    ))
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
            headers: Vec::new(),
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

fn loudness_job_path(path: &str) -> Option<(&str, &str)> {
    let remainder = path.strip_prefix("/api/v1/channels/")?;
    let (channel_id, job_id) = remainder.split_once("/loudness-proof/jobs/")?;
    (!channel_id.is_empty()
        && !job_id.is_empty()
        && !channel_id.contains('/')
        && !job_id.contains('/')
        && !channel_id.contains(['?', '%', '\\'])
        && !job_id.contains(['?', '%', '\\']))
    .then_some((channel_id, job_id))
}

fn loudness_job_state_name(state: OutputLoudnessProofJobState) -> &'static str {
    match state {
        OutputLoudnessProofJobState::Pending => "pending",
        OutputLoudnessProofJobState::Claimed => "claimed",
        OutputLoudnessProofJobState::CapturePending => "capture_pending",
        OutputLoudnessProofJobState::Analyzing => "analyzing",
        OutputLoudnessProofJobState::Completed => "completed",
        OutputLoudnessProofJobState::Failed => "failed",
    }
}

fn loudness_job_response(record: OutputLoudnessProofJobRecord) -> LoudnessProofJobResponse {
    LoudnessProofJobResponse {
        job_id: record.job_id,
        channel_id: record.channel_id,
        target_group: record.target_group,
        sink_id: record.sink_id,
        target_lufs_milli: record.target_lufs_milli,
        candidate_version: record.candidate_version,
        source_commit: record.source_commit.unwrap_or_default(),
        configuration_hash: record.configuration_hash,
        sample_duration_ms: record.sample_duration_ms,
        max_capture_bytes: record.max_capture_bytes,
        state: loudness_job_state_name(record.state),
        created_at_unix_ms: record.created_at_unix_ms,
        updated_at_unix_ms: record.updated_at_unix_ms,
        lease_expires_at_unix_ms: record.lease_expires_at_unix_ms,
        capture: record
            .capture
            .map(|capture| LoudnessProofJobCaptureResponse {
                artifact_relative_path: capture.artifact_relative_path,
                capture_bytes: capture.capture_bytes,
                sample_duration_ms: capture.sample_duration_ms,
                capture_sha256: capture.capture_sha256,
            }),
        completed_at_unix_ms: record.completed_at_unix_ms,
        failed_at_unix_ms: record.failed_at_unix_ms,
        failure_detail: record.failure_detail,
    }
}

fn loudness_job_storage_error(message: &str) -> ApiResponse {
    if message.contains("already") || message.contains("active") {
        return error_response(409, "job_already_active", message);
    }
    if message.contains("stale") || message.contains("changed") {
        return error_response(409, "configuration_changed", message);
    }
    if message.contains("not currently configured") || message.contains("not an enabled") {
        return error_response(409, "sink_not_enabled", message);
    }
    if message.contains("invalid") || message.contains("must ") || message.contains("unbounded") {
        return error_response(422, "validation_failed", message);
    }
    error_response(500, "storage_error", message)
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
    let reference_station_tier_selection = storage.read_reference_station_tier_selection()?;
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
        match read_verified_candidate_identity(data_directory, application_directory) {
            Ok(identity) => (
                Some(identity.version),
                Some(identity.source_commit),
                true,
                "The active application and every file in the receipt-bound installed media-runtime manifest are rehashed and verified; live engine and factory readiness remains a separate probe.".into(),
            ),
            Err(detail) => (None, None, false, detail),
        };
    let backup_writable = resources.backup_volume_writable;
    let windows_time = probe_windows_time();
    let media_disk_free_gb = resources
        .media_volume_free_gb
        .ok_or_else(|| "live media free space is unavailable".to_string())?;
    let collected_machine_inventory =
        machine_inventory::collect(&data_directory.join("media"), generated_at_unix_ms);
    let profile_generated_at_unix_ms = system_time_millis()
        .map_err(|message| {
            format!("station clock is unavailable after machine inventory: {message}")
        })?
        .max(generated_at_unix_ms);
    let machine_inventory_evidence = machine_inventory::validate_or_unavailable(
        collected_machine_inventory,
        media_disk_free_gb,
        generated_at_unix_ms,
        profile_generated_at_unix_ms,
    );
    Ok(StationBoxProbeInput {
        generated_at_unix_ms: profile_generated_at_unix_ms,
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
        reference_station_tier_selection,
        hardware: HardwareProbeInput {
            logical_cpus: std::thread::available_parallelism()
                .map(|value| value.get() as u32)
                .unwrap_or(1),
            ram_total_gb: resources
                .ram_total_gb
                .ok_or_else(|| "live RAM total is unavailable".to_string())?,
            media_disk_free_gb,
            machine_inventory: machine_inventory_evidence,
        },
        clock: ClockProbeInput {
            configured_timezone: station.as_ref().map(|value| value.profile.timezone.clone()),
            utc_offset_seconds: None,
            sync_status: windows_time.sync_status,
        },
        network: NetworkProbeInput {
            state: if !network_up {
                StationReadinessState::Blocked
            } else if windows_time.outbound_udp == CapabilitySupport::Supported {
                StationReadinessState::Pass
            } else {
                StationReadinessState::Unknown
            },
            hostname: std::env::var("COMPUTERNAME")
                .ok()
                .filter(|value| !value.is_empty()),
            interface_up: network_up,
            dns_working: windows_time.dns_working,
            // A complete request/reply exchange with the configured Windows
            // Time source proves one outbound UDP path. It does not prove an
            // arbitrary destination, SRT, a headend, or receiver acceptance.
            outbound_udp: windows_time.outbound_udp,
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

const WINDOWS_CLOCK_PROBE_SCRIPT: &str = r#"
$ErrorActionPreference = 'Stop'
try {
    $sourceOutput = @(& w32tm.exe /query /source 2>$null)
    if (($LASTEXITCODE -ne 0) -or ($sourceOutput.Count -ne 1)) {
        Write-Output 'unavailable unknown unknown'
        exit 0
    }

    $source = ([string]$sourceOutput[0]).Split(',')[0].Trim()
    if (($source.Length -lt 1) -or ($source.Length -gt 255) -or
        ($source -notmatch '^[A-Za-z0-9._:-]+$')) {
        Write-Output 'unknown unknown unknown'
        exit 0
    }

    $parsedAddress = $null
    $sourceKind = if ([Net.IPAddress]::TryParse($source, [ref]$parsedAddress)) {
        'literal'
    } else {
        'dns'
    }

    $stripOutput = @(& w32tm.exe /stripchart "/computer:$source" /period:1 /samples:3 /rdtsc 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Write-Output 'unavailable unknown unknown'
        exit 0
    }

    $samples = [Collections.Generic.List[double]]::new()
    foreach ($line in $stripOutput) {
        $parts = ([string]$line).Split(',')
        if (($parts.Count -ne 5) -or
            ($parts[0].Trim() -notmatch '^\d+$') -or
            ($parts[1].Trim() -notmatch '^\d+$') -or
            ($parts[2].Trim() -notmatch '^\d+$')) {
            continue
        }
        try {
            $offset = [double]::Parse(
                $parts[4].Trim(),
                [Globalization.NumberStyles]::Float,
                [Globalization.CultureInfo]::CurrentCulture)
        } catch {
            Write-Output 'unknown unknown unknown'
            exit 0
        }
        if ([double]::IsNaN($offset) -or [double]::IsInfinity($offset)) {
            Write-Output 'unknown unknown unknown'
            exit 0
        }
        $samples.Add($offset)
    }

    if ($samples.Count -ne 3) {
        Write-Output 'unknown unknown unknown'
        exit 0
    }
    $clockStatus = if ($samples | Where-Object { [Math]::Abs($_) -gt 2.0 }) {
        'unsynchronized'
    } else {
        'synchronized'
    }
    Write-Output "$clockStatus $sourceKind udp"
} catch {
    Write-Output 'unavailable unknown unknown'
}
"#;

#[derive(Debug, Clone, PartialEq, Eq)]
struct WindowsTimeProbeResult {
    sync_status: ClockSyncStatus,
    dns_working: Option<bool>,
    outbound_udp: CapabilitySupport,
}

fn unknown_windows_time_probe(sync_status: ClockSyncStatus) -> WindowsTimeProbeResult {
    WindowsTimeProbeResult {
        sync_status,
        dns_working: None,
        outbound_udp: CapabilitySupport::Unknown,
    }
}

fn parse_windows_time_probe_result(output: &[u8]) -> WindowsTimeProbeResult {
    match output {
        b"synchronized dns udp\n" | b"synchronized dns udp\r\n" => WindowsTimeProbeResult {
            sync_status: ClockSyncStatus::Synchronized,
            dns_working: Some(true),
            outbound_udp: CapabilitySupport::Supported,
        },
        b"synchronized literal udp\n" | b"synchronized literal udp\r\n" => WindowsTimeProbeResult {
            sync_status: ClockSyncStatus::Synchronized,
            dns_working: None,
            outbound_udp: CapabilitySupport::Supported,
        },
        b"unsynchronized dns udp\n" | b"unsynchronized dns udp\r\n" => WindowsTimeProbeResult {
            sync_status: ClockSyncStatus::Unsynchronized,
            dns_working: Some(true),
            outbound_udp: CapabilitySupport::Supported,
        },
        b"unsynchronized literal udp\n" | b"unsynchronized literal udp\r\n" => {
            WindowsTimeProbeResult {
                sync_status: ClockSyncStatus::Unsynchronized,
                dns_working: None,
                outbound_udp: CapabilitySupport::Supported,
            }
        }
        b"unavailable unknown unknown\n" | b"unavailable unknown unknown\r\n" => {
            unknown_windows_time_probe(ClockSyncStatus::Unavailable)
        }
        b"unknown unknown unknown\n" | b"unknown unknown unknown\r\n" => {
            unknown_windows_time_probe(ClockSyncStatus::Unknown)
        }
        _ => unknown_windows_time_probe(ClockSyncStatus::Unknown),
    }
}

fn probe_windows_time() -> WindowsTimeProbeResult {
    let mut command = hidden_command("powershell.exe");
    command.args([
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        WINDOWS_CLOCK_PROBE_SCRIPT,
    ]);
    match run_bounded_command(command, Duration::from_secs(8), 64 * 1024) {
        Ok(output) if output.success => parse_windows_time_probe_result(&output.stdout),
        Ok(_) | Err(_) => unknown_windows_time_probe(ClockSyncStatus::Unavailable),
    }
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

fn system_time_millis() -> Result<i64, String> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis();
    i64::try_from(millis).map_err(|_| "system time exceeds the supported range".into())
}

pub fn serve_one(listener: TcpListener, database_path: impl AsRef<Path>) -> Result<(), String> {
    let database_path = database_path.as_ref();
    let runtime = initialize_auth_runtime(&listener, database_path)?;
    let api = Api::open(database_path)?;
    let (mut stream, _) = listener.accept().map_err(|error| error.to_string())?;
    serve_stream(&mut stream, &api, &runtime)
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
    serve_until_with_application_directory_and_health(
        listener,
        database_path,
        application_directory,
        stop,
        OrchestrationHealth::ready_without_supervisor(),
    )
}

pub fn serve_until_with_application_directory_and_health(
    listener: TcpListener,
    database_path: impl AsRef<Path>,
    application_directory: impl AsRef<Path>,
    stop: Arc<AtomicBool>,
    orchestration_health: Arc<OrchestrationHealth>,
) -> Result<(), String> {
    let database_path = database_path.as_ref().to_path_buf();
    let application_directory = application_directory.as_ref().to_path_buf();
    let auth_runtime = Arc::new(initialize_auth_runtime(&listener, &database_path)?);
    let station_box_cache = Arc::new(Mutex::new(None));
    Api::open_with_application_directory_and_cache(
        &database_path,
        &application_directory,
        Arc::clone(&station_box_cache),
        Arc::clone(&orchestration_health),
    )?;
    let active_requests = Arc::new(AtomicUsize::new(0));
    listener
        .set_nonblocking(true)
        .map_err(|error| error.to_string())?;
    while !stop.load(Ordering::Acquire) {
        match orchestration_health.status() {
            OrchestrationHealthStatus::Failed => {
                return Err("channel orchestration failed during the current service boot".into());
            }
            OrchestrationHealthStatus::Stale => {
                return Err(
                    "channel orchestration stopped making progress during the current service boot"
                        .into(),
                );
            }
            OrchestrationHealthStatus::Starting | OrchestrationHealthStatus::Ready => {}
        }
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
                let request_orchestration_health = Arc::clone(&orchestration_health);
                let request_count = Arc::clone(&active_requests);
                let request_auth_runtime = Arc::clone(&auth_runtime);
                std::thread::spawn(move || {
                    let _guard = ActiveRequestGuard(request_count);
                    let result = Api::open_with_application_directory_and_cache(
                        &request_database,
                        &request_application,
                        request_station_box_cache,
                        request_orchestration_health,
                    )
                    .and_then(|api| serve_stream(&mut stream, &api, &request_auth_runtime));
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

fn initialize_auth_runtime(
    listener: &TcpListener,
    database_path: &Path,
) -> Result<auth::AuthRuntime, String> {
    let address = listener.local_addr().map_err(|error| error.to_string())?;
    let runtime = auth::AuthRuntime::new(address).map_err(|error| error.to_string())?;
    let now = system_time_millis()?;
    StationStore::open(database_path)?.purge_operator_sessions_for_boot(&runtime.boot_id(), now)?;
    Ok(runtime)
}

fn serve_stream(
    stream: &mut TcpStream,
    api: &Api,
    auth_runtime: &auth::AuthRuntime,
) -> Result<(), String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|error| error.to_string())?;
    let response = match read_http_request(stream) {
        Ok(request) => dispatch_http(api, auth_runtime, &request),
        Err(response) => response,
    };
    write_response(stream, response)
}

fn write_response(stream: &mut TcpStream, response: ApiResponse) -> Result<(), String> {
    let reason = match response.status {
        200 => "OK",
        201 => "Created",
        202 => "Accepted",
        204 => "No Content",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Content Too Large",
        422 => "Unprocessable Content",
        428 => "Precondition Required",
        429 => "Too Many Requests",
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
    let additional_headers = response
        .headers
        .iter()
        .map(|header| format!("{}: {}\r\n", header.name, header.value))
        .collect::<String>();
    let headers = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\n{}{}Content-Length: {}\r\nConnection: close\r\n\r\n",
        response.status,
        reason,
        response.content_type,
        attachment_header,
        additional_headers,
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

#[derive(Debug)]
struct HttpRequest<'a> {
    method: &'a str,
    path: &'a str,
    headers: Vec<(&'a str, &'a str)>,
    body: &'a [u8],
}

impl<'a> HttpRequest<'a> {
    fn unique_header(&self, name: &str) -> Result<Option<&'a str>, ApiResponse> {
        let mut values = self
            .headers
            .iter()
            .filter(|(candidate, _)| candidate.eq_ignore_ascii_case(name))
            .map(|(_, value)| *value);
        let first = values.next();
        if values.next().is_some() {
            return Err(error_response(
                400,
                "ambiguous_header",
                &format!("{name} may appear only once."),
            ));
        }
        Ok(first)
    }
}

fn parse_http_request(request: &[u8]) -> Result<HttpRequest<'_>, ApiResponse> {
    let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") else {
        return Err(error_response(
            400,
            "malformed_request",
            "The HTTP headers are incomplete.",
        ));
    };
    let headers = match std::str::from_utf8(&request[..header_end]) {
        Ok(headers) => headers,
        Err(_) => {
            return Err(error_response(
                400,
                "malformed_request",
                "HTTP headers must be UTF-8.",
            ));
        }
    };
    let mut lines = headers.split("\r\n");
    let Some(request_line) = lines.next() else {
        return Err(error_response(
            400,
            "malformed_request",
            "The request line is missing.",
        ));
    };
    let mut request_parts = request_line.split_whitespace();
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
    if !path.starts_with('/') || path.starts_with("//") || path.contains('#') {
        return Err(error_response(
            400,
            "invalid_request_target",
            "Only an origin-form HTTP request target is accepted.",
        ));
    }

    let mut parsed_headers = Vec::new();
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return Err(error_response(
                400,
                "malformed_request",
                "An HTTP header is invalid.",
            ));
        };
        let value = value.trim();
        if name.is_empty()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            || value
                .bytes()
                .any(|byte| byte == 0 || byte == b'\r' || byte == b'\n')
        {
            return Err(error_response(
                400,
                "malformed_request",
                "An HTTP header is invalid.",
            ));
        }
        parsed_headers.push((name, value));
        if parsed_headers.len() > 128 {
            return Err(error_response(
                400,
                "too_many_headers",
                "HTTP requests may not contain more than 128 headers.",
            ));
        }
    }
    let parsed = HttpRequest {
        method,
        path,
        headers: parsed_headers,
        body: &request[header_end + 4..],
    };
    let Some(host) = parsed.unique_header("host")? else {
        return Err(error_response(
            400,
            "missing_host",
            "HTTP/1.1 requests require exactly one Host header.",
        ));
    };
    if host.is_empty() {
        return Err(error_response(
            400,
            "missing_host",
            "The Host header may not be empty.",
        ));
    }
    parsed.unique_header("origin")?;
    parsed.unique_header("cookie")?;
    parsed.unique_header("x-townlight-csrf")?;
    parsed.unique_header("sec-fetch-site")?;
    let content_length = match parsed.unique_header("content-length")? {
        Some(value) => value
            .parse::<usize>()
            .map_err(|_| error_response(400, "malformed_request", "Content-Length is invalid."))?,
        None => 0,
    };
    if parsed.body.len() != content_length {
        return Err(error_response(
            400,
            "malformed_request",
            "Content-Length does not match the request body.",
        ));
    }
    let body_limit = request_body_limit(method, path);
    if parsed.body.len() > body_limit {
        return Err(error_response(
            413,
            "request_too_large",
            request_too_large_message(method, path),
        ));
    }
    Ok(parsed)
}

fn dispatch_http(api: &Api, runtime: &auth::AuthRuntime, request: &[u8]) -> ApiResponse {
    let response = match parse_http_request(request) {
        Ok(request) => dispatch_parsed_http(api, runtime, &request),
        Err(response) => response,
    };
    security_headers(response)
}

fn dispatch_parsed_http(
    api: &Api,
    runtime: &auth::AuthRuntime,
    request: &HttpRequest<'_>,
) -> ApiResponse {
    let host = match request.unique_header("host") {
        Ok(Some(host)) if host == runtime.expected_host() => host,
        Ok(_) => {
            return error_response(
                400,
                "invalid_host",
                "The Host header does not match this station listener.",
            );
        }
        Err(response) => return response,
    };
    debug_assert_eq!(host, runtime.expected_host());
    let unsafe_request = !matches!(request.method, "GET" | "HEAD" | "OPTIONS");
    if unsafe_request {
        if request.unique_header("origin").ok().flatten() != Some(runtime.expected_origin()) {
            return error_response(
                403,
                "cross_origin_denied",
                "Unsafe requests require this station's exact Origin.",
            );
        }
        if request.unique_header("sec-fetch-site").ok().flatten() != Some("same-origin") {
            return error_response(
                403,
                "cross_site_denied",
                "Unsafe requests require Sec-Fetch-Site: same-origin.",
            );
        }
    }
    let cookie_header = match request.unique_header("cookie") {
        Ok(value) => value,
        Err(response) => return response,
    };
    let cookies = match auth::AuthCookies::parse(cookie_header) {
        Ok(cookies) => cookies,
        Err(message) => return error_response(400, "invalid_cookie", message),
    };
    let now = match system_time_millis() {
        Ok(now) => now,
        Err(message) => return error_response(500, "clock_error", &message),
    };
    let body = (!request.body.is_empty()).then_some(request.body);

    match (request.method, request.path) {
        ("GET", path) if path == "/" || path.starts_with("/?") => {
            return api.route("GET", path, None, None);
        }
        ("GET", "/operator.js" | "/operator.css" | "/health") => {
            return api.route(request.method, request.path, None, None);
        }
        ("GET", "/api/v1/auth/status") => {
            let authority =
                match auth::authenticate_session(&api.storage, runtime, &cookies, now, false) {
                    Ok(authority) => authority,
                    Err(error) => return auth_failure_response(error),
                };
            let state = if authority.is_some() {
                "authenticated"
            } else {
                match api.storage.has_operator_accounts() {
                    Ok(true) => "login_required",
                    Ok(false) => "bootstrap_required",
                    Err(_) => return auth_storage_error_response(),
                }
            };
            return json_response(
                200,
                serde_json::to_vec(&serde_json::json!({
                    "schema_version": 1,
                    "state": state,
                    "session": authority.as_ref().map(session_json),
                }))
                .expect("serializing auth status cannot fail"),
            );
        }
        ("POST", "/api/v1/auth/bootstrap") => {
            let Some(command) = parse_body::<BootstrapRequest>(body) else {
                return error_response(400, "invalid_json", "A bootstrap request is required.");
            };
            return match auth::bootstrap(
                &api.storage,
                runtime,
                &command.capability,
                &command.username,
                &command.display_name,
                &command.password,
                now,
            ) {
                Ok(result) => issued_session_response(
                    201,
                    &result.issued,
                    serde_json::json!({
                        "session": session_json(&result.issued.authority),
                        "recovery_codes": result.recovery_codes,
                    }),
                ),
                Err(auth::AuthFailure::Unauthorized) => error_response(
                    403,
                    "invalid_bootstrap_capability",
                    "The one-time bootstrap capability is invalid or expired.",
                ),
                Err(error) => auth_failure_response(error),
            };
        }
        ("POST", "/api/v1/auth/login") => {
            let Some(command) = parse_body::<LoginRequest>(body) else {
                return error_response(400, "invalid_json", "A login request is required.");
            };
            return match auth::login(
                &api.storage,
                runtime,
                &command.username,
                &command.password,
                now,
            ) {
                Ok(issued) => issued_session_response(
                    200,
                    &issued,
                    serde_json::json!({ "session": session_json(&issued.authority) }),
                ),
                Err(error) => auth_failure_response(error),
            };
        }
        ("POST", "/api/v1/auth/recover") => {
            let Some(command) = parse_body::<RecoverRequest>(body) else {
                return error_response(400, "invalid_json", "A recovery request is required.");
            };
            return match auth::recover(
                &api.storage,
                runtime,
                &command.username,
                &command.recovery_code,
                &command.new_password,
                now,
            ) {
                Ok(()) => clear_auth_cookies(json_response(200, br#"{"recovered":true}"#.to_vec())),
                Err(error) => auth_failure_response(error),
            };
        }
        _ => {}
    }

    let authority = match auth::authenticate_session(&api.storage, runtime, &cookies, now, true) {
        Ok(Some(authority)) => authority,
        Ok(None) => return auth_gate_response(api),
        Err(error) => return auth_failure_response(error),
    };
    if unsafe_request && !valid_csrf(request, &cookies) {
        return error_response(
            403,
            "csrf_failed",
            "Unsafe authenticated requests require the matching CSRF cookie and header.",
        );
    }
    match (request.method, request.path) {
        ("GET", "/api/v1/auth/session") => json_response(
            200,
            serde_json::to_vec(&serde_json::json!({ "session": session_json(&authority) }))
                .expect("serializing session cannot fail"),
        ),
        ("POST", "/api/v1/auth/logout") => {
            let Some(session) = cookies.session.as_ref() else {
                return auth_gate_response(api);
            };
            if let Err(message) = api
                .storage
                .revoke_operator_session(&digest_secret(session), now)
            {
                let _ = message;
                return auth_storage_error_response();
            }
            clear_auth_cookies(json_response(200, br#"{"logged_out":true}"#.to_vec()))
        }
        ("POST", "/api/v1/auth/change-password") => {
            let Some(command) = parse_body::<ChangePasswordRequest>(body) else {
                return error_response(
                    400,
                    "invalid_json",
                    "A password-change request is required.",
                );
            };
            if let Err(error) = auth::verify_current_password(
                runtime,
                &authority.account,
                &command.current_password,
            ) {
                return auth_failure_response(error);
            }
            let password_phc = match auth::hash_new_password(runtime, &command.new_password) {
                Ok(password_phc) => password_phc,
                Err(error) => return auth_failure_response(error),
            };
            match api.storage.rotate_operator_password(
                authority.account.account_id(),
                authority.account.credential_version(),
                &password_phc,
                now,
            ) {
                Ok(Some(_)) => clear_auth_cookies(json_response(
                    200,
                    br#"{"changed":true,"reauthenticate":true}"#.to_vec(),
                )),
                Ok(None) => auth_gate_response(api),
                Err(_) => auth_storage_error_response(),
            }
        }
        _ => api.handle_authenticated(request.method, request.path, body, &authority.account),
    }
}

fn valid_csrf(request: &HttpRequest<'_>, cookies: &auth::AuthCookies) -> bool {
    let (Some(cookie), Ok(Some(header))) = (
        cookies.csrf.as_ref(),
        request.unique_header(auth::CSRF_HEADER_NAME),
    ) else {
        return false;
    };
    SecretToken::try_from(header)
        .is_ok_and(|header| digest_secret(&header) == digest_secret(cookie))
}

fn mutation_actor(actor: Option<&OperatorAccount>, supplied: &str) -> String {
    actor
        .map(|account| account.account_id().as_str())
        .unwrap_or(supplied)
        .to_owned()
}

fn auth_gate_response(api: &Api) -> ApiResponse {
    match api.storage.has_operator_accounts() {
        Ok(true) => error_response(
            401,
            "authentication_required",
            "Operator sign-in is required.",
        ),
        Ok(false) => error_response(
            428,
            "operator_bootstrap_required",
            "The first operator account must be created before this station API is available.",
        ),
        Err(_) => auth_storage_error_response(),
    }
}

fn auth_failure_response(error: auth::AuthFailure) -> ApiResponse {
    match error {
        auth::AuthFailure::Invalid(message) => error_response(422, "validation_failed", message),
        auth::AuthFailure::Unauthorized => error_response(
            401,
            "invalid_credentials",
            "The supplied authentication authority is invalid.",
        ),
        auth::AuthFailure::BootstrapRequired => error_response(
            428,
            "operator_bootstrap_required",
            "The first operator account must be created before login.",
        ),
        auth::AuthFailure::Conflict(message) => error_response(409, "auth_conflict", message),
        auth::AuthFailure::Busy => error_response(
            503,
            "auth_busy",
            "The station is processing the maximum number of password verifications.",
        ),
        auth::AuthFailure::Throttled(seconds) => error_response(
            429,
            "login_throttled",
            "Login is temporarily throttled after repeated failures.",
        )
        .with_header("Retry-After", seconds.max(1).to_string())
        .unwrap_or_else(|message| error_response(500, "response_error", &message)),
        auth::AuthFailure::Storage(_) => error_response(
            500,
            "authentication_error",
            "Authentication authority could not be read or updated.",
        ),
    }
}

fn auth_storage_error_response() -> ApiResponse {
    error_response(
        500,
        "authentication_error",
        "Authentication authority could not be read or updated.",
    )
}

fn session_json(authority: &station_storage::OperatorSessionAuthority) -> serde_json::Value {
    serde_json::json!({
        "account_id": authority.account.account_id().as_str(),
        "username": authority.account.username().as_str(),
        "display_name": authority.account.display_name().as_str(),
        "roles": authority.account.roles().iter().map(|role| role.as_str()).collect::<Vec<_>>(),
        "absolute_expires_at_unix_ms": authority.session.absolute_expires_at_unix_ms(),
    })
}

fn issued_session_response(
    status: u16,
    issued: &auth::IssuedSession,
    body: serde_json::Value,
) -> ApiResponse {
    let response = json_response(
        status,
        serde_json::to_vec(&body).expect("serializing issued session cannot fail"),
    );
    response
        .with_header(
            "Set-Cookie",
            format!(
                "{}={}; HttpOnly; SameSite=Strict; Path=/api/v1/; Max-Age=43200",
                auth::SESSION_COOKIE_NAME,
                issued.session_token.expose_secret()
            ),
        )
        .and_then(|response| {
            response.with_header(
                "Set-Cookie",
                format!(
                    "{}={}; SameSite=Strict; Path=/; Max-Age=43200",
                    auth::CSRF_COOKIE_NAME,
                    issued.csrf_token.expose_secret()
                ),
            )
        })
        .unwrap_or_else(|message| error_response(500, "response_error", &message))
}

fn clear_auth_cookies(response: ApiResponse) -> ApiResponse {
    response
        .with_header(
            "Set-Cookie",
            format!(
                "{}=; HttpOnly; SameSite=Strict; Path=/api/v1/; Max-Age=0",
                auth::SESSION_COOKIE_NAME
            ),
        )
        .and_then(|response| {
            response.with_header(
                "Set-Cookie",
                format!(
                    "{}=; SameSite=Strict; Path=/; Max-Age=0",
                    auth::CSRF_COOKIE_NAME
                ),
            )
        })
        .unwrap_or_else(|message| error_response(500, "response_error", &message))
}

fn security_headers(response: ApiResponse) -> ApiResponse {
    [
        ("Cache-Control", "no-store"),
        ("Pragma", "no-cache"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        (
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        ),
    ]
    .into_iter()
    .try_fold(response, |response, (name, value)| {
        response.with_header(name, value)
    })
    .unwrap_or_else(|message| error_response(500, "response_error", &message))
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
        headers: Vec::new(),
        body,
    }
}

fn static_response(status: u16, content_type: &'static str, body: &[u8]) -> ApiResponse {
    ApiResponse {
        status,
        content_type,
        attachment_name: None,
        headers: Vec::new(),
        body: body.to_vec(),
    }
}

fn dynamic_response(status: u16, content_type: &'static str, body: Vec<u8>) -> ApiResponse {
    ApiResponse {
        status,
        content_type,
        attachment_name: None,
        headers: Vec::new(),
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
        headers: Vec::new(),
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
    use std::collections::BTreeSet;
    use std::net::TcpListener;
    use std::thread;

    const PNG_1X1: &[u8] = &[
        137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 6,
        0, 0, 0, 31, 21, 196, 137, 0, 0, 0, 13, 73, 68, 65, 84, 120, 156, 99, 248, 207, 192, 240,
        31, 0, 5, 0, 1, 255, 137, 153, 61, 29, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130,
    ];

    const TEST_PASSWORD_PHC: &str = "$argon2id$v=19$m=65536,t=3,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE";

    fn test_actor(role: station_auth::OperatorRole) -> OperatorAccount {
        OperatorAccount::new(
            station_auth::AccountId::try_from("dc3c0d56-44c8-4c4f-bd32-b501c56d34f6").unwrap(),
            station_auth::OperatorUsername::try_from("setup.admin").unwrap(),
            station_auth::OperatorDisplayName::try_from("Setup Administrator").unwrap(),
            station_auth::PasswordPhc::try_from(TEST_PASSWORD_PHC).unwrap(),
            BTreeSet::from([role]),
            station_auth::CredentialVersion::new(1).unwrap(),
            true,
            1,
            1,
        )
        .unwrap()
    }

    fn raw_request(method: &str, path: &str, headers: &[(&str, &str)], body: &[u8]) -> Vec<u8> {
        let mut request = format!(
            "{method} {path} HTTP/1.1\r\nContent-Length: {}\r\n",
            body.len()
        );
        for (name, value) in headers {
            request.push_str(name);
            request.push_str(": ");
            request.push_str(value);
            request.push_str("\r\n");
        }
        request.push_str("\r\n");
        let mut request = request.into_bytes();
        request.extend_from_slice(body);
        request
    }

    fn auth_cookie_header(response: &ApiResponse) -> (String, String) {
        let values = response
            .headers
            .iter()
            .filter(|header| header.name.eq_ignore_ascii_case("set-cookie"))
            .map(|header| header.value.as_str())
            .collect::<Vec<_>>();
        let session = values
            .iter()
            .find_map(|value| value.split(';').next())
            .filter(|value| value.starts_with(auth::SESSION_COOKIE_NAME))
            .unwrap();
        let csrf = values
            .iter()
            .find_map(|value| {
                value
                    .split(';')
                    .next()
                    .filter(|cookie| cookie.starts_with(auth::CSRF_COOKIE_NAME))
            })
            .unwrap();
        let csrf_value = csrf.split_once('=').unwrap().1.to_owned();
        (format!("{session}; {csrf}"), csrf_value)
    }

    #[test]
    fn windows_time_probe_result_accepts_only_exact_ascii_evidence() {
        assert_eq!(
            parse_windows_time_probe_result(b"synchronized dns udp\n"),
            WindowsTimeProbeResult {
                sync_status: ClockSyncStatus::Synchronized,
                dns_working: Some(true),
                outbound_udp: CapabilitySupport::Supported,
            }
        );
        assert_eq!(
            parse_windows_time_probe_result(b"synchronized literal udp\r\n"),
            WindowsTimeProbeResult {
                sync_status: ClockSyncStatus::Synchronized,
                dns_working: None,
                outbound_udp: CapabilitySupport::Supported,
            }
        );
        assert_eq!(
            parse_windows_time_probe_result(b"unsynchronized dns udp\n"),
            WindowsTimeProbeResult {
                sync_status: ClockSyncStatus::Unsynchronized,
                dns_working: Some(true),
                outbound_udp: CapabilitySupport::Supported,
            }
        );
        assert_eq!(
            parse_windows_time_probe_result(b"unsynchronized literal udp\r\n"),
            WindowsTimeProbeResult {
                sync_status: ClockSyncStatus::Unsynchronized,
                dns_working: None,
                outbound_udp: CapabilitySupport::Supported,
            }
        );
        assert_eq!(
            parse_windows_time_probe_result(b"unavailable unknown unknown\n"),
            unknown_windows_time_probe(ClockSyncStatus::Unavailable)
        );
        assert_eq!(
            parse_windows_time_probe_result(b"unknown unknown unknown\n"),
            unknown_windows_time_probe(ClockSyncStatus::Unknown)
        );
        assert_eq!(
            parse_windows_time_probe_result(b"synchronized dns udp\nextra\n"),
            unknown_windows_time_probe(ClockSyncStatus::Unknown)
        );
        assert_eq!(
            parse_windows_time_probe_result(b"Synchronized dns udp\n"),
            unknown_windows_time_probe(ClockSyncStatus::Unknown)
        );
        assert_eq!(
            parse_windows_time_probe_result(b"synchronized\n"),
            unknown_windows_time_probe(ClockSyncStatus::Unknown)
        );
        assert_eq!(
            parse_windows_time_probe_result(b"synchronized dns srt\n"),
            unknown_windows_time_probe(ClockSyncStatus::Unknown)
        );
    }

    #[test]
    fn windows_time_probe_contract_measures_the_configured_source_fail_closed() {
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains("w32tm.exe /query /source"));
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains(".Split(',')[0].Trim()"));
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains("/samples:3"));
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains("/rdtsc"));
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains("$samples.Count -ne 3"));
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains("[Net.IPAddress]::TryParse"));
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains("[Math]::Abs($_) -gt 2.0"));
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains("$clockStatus $sourceKind udp"));
        assert!(WINDOWS_CLOCK_PROBE_SCRIPT.contains("CurrentCulture"));
        assert!(!WINDOWS_CLOCK_PROBE_SCRIPT.contains("Invoke-Expression"));
    }

    #[cfg(target_os = "windows")]
    #[test]
    #[ignore = "explicit Windows configured-source network proof lane"]
    fn windows_time_probe_current_machine_completes_a_real_udp_exchange() {
        let evidence = probe_windows_time();
        assert_eq!(
            evidence.outbound_udp,
            CapabilitySupport::Supported,
            "configured Windows Time source did not complete three UDP request/reply samples: {evidence:?}"
        );
        assert!(
            matches!(
                evidence.sync_status,
                ClockSyncStatus::Synchronized | ClockSyncStatus::Unsynchronized
            ),
            "completed Windows Time samples must carry a finite clock verdict: {evidence:?}"
        );
    }

    #[test]
    fn production_dispatcher_enforces_boot_host_origin_csrf_and_cookie_contract() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-http-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let runtime = auth::AuthRuntime::new("127.0.0.1:4070".parse().unwrap()).unwrap();
        let now = system_time_millis().unwrap();
        let capability = station_auth::generate_secret_token().unwrap();
        let bootstrap = station_auth::BootstrapCredential::new(
            digest_secret(&capability),
            now.saturating_sub(1_000),
            now.saturating_add(5 * 60 * 1_000),
            None,
        )
        .unwrap();
        assert!(
            api.storage
                .provision_operator_bootstrap(&bootstrap)
                .unwrap()
        );
        let exact_headers = [("Host", runtime.expected_host())];

        let gated = dispatch_http(
            &api,
            &runtime,
            &raw_request("GET", "/api/v1/channels", &exact_headers, b""),
        );
        assert_eq!(gated.status, 428);
        assert!(
            gated
                .headers
                .iter()
                .any(|header| header.name == "Cache-Control" && header.value == "no-store")
        );

        let wrong_host = dispatch_http(
            &api,
            &runtime,
            &raw_request("GET", "/health", &[("Host", "localhost:4070")], b""),
        );
        assert_eq!(wrong_host.status, 400);

        let bootstrap_body = serde_json::to_vec(&serde_json::json!({
            "capability": capability.expose_secret(),
            "username": "setup.admin",
            "display_name": "Setup Administrator",
            "password": "A-strong-bootstrap-password-47!",
        }))
        .unwrap();
        let missing_fetch_metadata = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/auth/bootstrap",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                ],
                &bootstrap_body,
            ),
        );
        assert_eq!(missing_fetch_metadata.status, 403);

        let cross_origin = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/auth/bootstrap",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", "http://evil.invalid"),
                    ("Sec-Fetch-Site", "same-origin"),
                ],
                &bootstrap_body,
            ),
        );
        assert_eq!(cross_origin.status, 403);

        let wrong_capability = station_auth::generate_secret_token().unwrap();
        let wrong_bootstrap_body = serde_json::to_vec(&serde_json::json!({
            "capability": wrong_capability.expose_secret(),
            "username": "setup.admin",
            "display_name": "Setup Administrator",
            "password": "A-strong-bootstrap-password-47!",
        }))
        .unwrap();
        let invalid_bootstrap = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/auth/bootstrap",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                ],
                &wrong_bootstrap_body,
            ),
        );
        assert_eq!(invalid_bootstrap.status, 403);
        assert!(
            String::from_utf8_lossy(&invalid_bootstrap.body)
                .contains("invalid_bootstrap_capability")
        );

        let issued = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/auth/bootstrap",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                ],
                &bootstrap_body,
            ),
        );
        assert_eq!(
            issued.status,
            201,
            "{}",
            String::from_utf8_lossy(&issued.body)
        );
        assert!(issued.headers.iter().any(|header| {
            header.name == "Set-Cookie"
                && header.value.starts_with(auth::SESSION_COOKIE_NAME)
                && header.value.contains("HttpOnly")
                && header.value.contains("Path=/api/v1/")
        }));
        assert!(issued.headers.iter().any(|header| {
            header.name == "Set-Cookie"
                && header.value.starts_with(auth::CSRF_COOKIE_NAME)
                && !header.value.contains("HttpOnly")
                && header.value.contains("Path=/;")
        }));
        let (cookie, csrf) = auth_cookie_header(&issued);

        let session = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "GET",
                "/api/v1/auth/session",
                &[("Host", runtime.expected_host()), ("Cookie", &cookie)],
                b"",
            ),
        );
        assert_eq!(session.status, 200);

        let account = api
            .storage
            .read_operator_login(&station_auth::OperatorUsername::try_from("setup.admin").unwrap())
            .unwrap()
            .unwrap()
            .account;
        let actor_id = account.account_id().as_str().to_owned();
        let channel_id = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
        let board_id = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Government Channel".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "f103968a-640b-42dc-8084-3fe9e847ff7f",
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
        let seeded_board = CgBoard {
            board_id: board_id.into(),
            channel_id: channel_id.into(),
            template_id: "l-bar".into(),
            active: true,
            background_audio_asset_id: None,
            revision: 1,
            created_at_unix_ms: now,
            updated_at_unix_ms: now,
            created_by: "historical.creator".into(),
        };
        let seed_audit = CgAuditEvent {
            audit_id: "7bb32e14-fe69-47cb-a921-dc3fc84499bf".into(),
            board_id: Some(board_id.into()),
            channel_id: channel_id.into(),
            event_kind: station_cg::CgAuditEventKind::BoardCreated,
            operator_id: "legacy.in_process".into(),
            occurred_at_unix_ms: now,
            details: std::collections::BTreeMap::new(),
        };
        let seed_response = api.handle_in_process(
            "PUT",
            "/api/v1/cg/board",
            Some(
                &serde_json::to_vec(&serde_json::json!({
                    "board": seeded_board,
                    "expected_revision": 0,
                    "audit": seed_audit,
                }))
                .unwrap(),
            ),
        );
        assert_eq!(seed_response.status, 200);
        let board_update = serde_json::to_vec(&serde_json::json!({
            "board": {
                "board_id": board_id,
                "channel_id": channel_id,
                "template_id": "l-bar",
                "active": false,
                "background_audio_asset_id": null,
                "revision": 2,
                "created_at_unix_ms": now,
                "updated_at_unix_ms": now.saturating_add(1)
            },
            "expected_revision": 1,
            "audit": {
                "audit_id": "919ccff6-f20c-4787-8916-a4b96101ec8e",
                "board_id": board_id,
                "channel_id": channel_id,
                "event_kind": "board_updated",
                "occurred_at_unix_ms": now.saturating_add(1),
                "details": {}
            }
        }))
        .unwrap();
        let updated_board = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "PUT",
                "/api/v1/cg/board",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                &board_update,
            ),
        );
        assert_eq!(
            updated_board.status,
            200,
            "{}",
            String::from_utf8_lossy(&updated_board.body)
        );
        assert_eq!(
            serde_json::from_slice::<CgBoard>(&updated_board.body)
                .unwrap()
                .created_by,
            "historical.creator"
        );
        let board_audit = api.storage.list_cg_audit_events(channel_id, 10).unwrap();
        assert_eq!(
            board_audit
                .iter()
                .find(|event| event.audit_id == "919ccff6-f20c-4787-8916-a4b96101ec8e")
                .unwrap()
                .operator_id,
            actor_id
        );

        let feed_id = "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24";
        let seeded_feed = CgFeedSource {
            feed_source_id: feed_id.into(),
            channel_id: channel_id.into(),
            kind: station_cg::CgFeedKind::Rss,
            label: "Historical feed".into(),
            source_url: "https://notices.example.test/rss".into(),
            trust_tier: station_cg::CgFeedTrustTier::Moderated,
            tags: vec![],
            refresh_seconds: 300,
            enabled: true,
            revision: 1,
            created_at_unix_ms: now,
            updated_at_unix_ms: now,
            created_by: "historical.feed.creator".into(),
            last_fetched_at_unix_ms: None,
            last_fetch_error: None,
        };
        let feed_seed = api.handle_in_process(
            "PUT",
            "/api/v1/cg/feeds",
            Some(
                &serde_json::to_vec(&serde_json::json!({
                    "feed": seeded_feed,
                    "expected_revision": 0,
                    "audit": {
                        "audit_id": "2c60c2ac-fc66-4e88-acb4-060127f0ea90",
                        "board_id": board_id,
                        "channel_id": channel_id,
                        "event_kind": "feed_created",
                        "operator_id": "legacy.in_process",
                        "occurred_at_unix_ms": now,
                        "details": {}
                    }
                }))
                .unwrap(),
            ),
        );
        assert_eq!(feed_seed.status, 200);
        let feed_update = serde_json::to_vec(&serde_json::json!({
            "feed": {
                "feed_source_id": feed_id,
                "channel_id": channel_id,
                "kind": "rss",
                "label": "Updated feed",
                "source_url": "https://notices.example.test/rss",
                "trust_tier": "moderated",
                "tags": [],
                "refresh_seconds": 600,
                "enabled": true,
                "revision": 2,
                "created_at_unix_ms": now,
                "updated_at_unix_ms": now.saturating_add(2),
                "created_by": "browser.forgery",
                "last_fetched_at_unix_ms": null,
                "last_fetch_error": null
            },
            "expected_revision": 1,
            "audit": {
                "audit_id": "5f7b9535-d3a3-4ea2-bf96-2e8be5e85bc1",
                "board_id": board_id,
                "channel_id": channel_id,
                "event_kind": "feed_updated",
                "operator_id": "browser.forgery",
                "occurred_at_unix_ms": now.saturating_add(2),
                "details": {}
            }
        }))
        .unwrap();
        let updated_feed = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "PUT",
                "/api/v1/cg/feeds",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                &feed_update,
            ),
        );
        assert_eq!(updated_feed.status, 200);
        assert_eq!(
            serde_json::from_slice::<CgFeedSource>(&updated_feed.body)
                .unwrap()
                .created_by,
            "historical.feed.creator"
        );
        let feed_audit = api.storage.list_cg_audit_events(channel_id, 20).unwrap();
        assert_eq!(
            feed_audit
                .iter()
                .find(|event| event.audit_id == "5f7b9535-d3a3-4ea2-bf96-2e8be5e85bc1")
                .unwrap()
                .operator_id,
            actor_id
        );

        let bulletin_id = "597f52ae-f16c-4ff8-ab32-60379cd950c4";
        let bulletin_seed = api.handle_in_process(
            "PUT",
            "/api/v1/cg/bulletins",
            Some(
                &serde_json::to_vec(&serde_json::json!({
                    "bulletin": {
                        "bulletin_id": bulletin_id,
                        "channel_id": channel_id,
                        "content": {"kind":"text","title":"Meeting","body":"Tuesday at six."},
                        "state": "submitted",
                        "tags": ["community"],
                        "requested_start_at_unix_ms": now.saturating_add(10_000),
                        "requested_end_at_unix_ms": now.saturating_add(20_000),
                        "display_seconds": 12,
                        "audio_asset_id": null,
                        "submitted_by": "historical.submitter",
                        "moderation_notes": null,
                        "moderated_by": null,
                        "revision": 1,
                        "created_at_unix_ms": now,
                        "updated_at_unix_ms": now
                    },
                    "expected_revision": 0,
                    "audit": {
                        "audit_id": "88a6d930-bbfc-472c-a60a-58f2327bb8b0",
                        "board_id": board_id,
                        "channel_id": channel_id,
                        "event_kind": "bulletin_submitted",
                        "operator_id": "legacy.in_process",
                        "occurred_at_unix_ms": now,
                        "details": {}
                    }
                }))
                .unwrap(),
            ),
        );
        assert_eq!(bulletin_seed.status, 200);
        let bulletin_accept = serde_json::to_vec(&serde_json::json!({
            "bulletin": {
                "bulletin_id": bulletin_id,
                "channel_id": channel_id,
                "content": {"kind":"text","title":"Meeting","body":"Tuesday at six."},
                "state": "accepted",
                "tags": ["community"],
                "requested_start_at_unix_ms": now.saturating_add(10_000),
                "requested_end_at_unix_ms": now.saturating_add(20_000),
                "display_seconds": 12,
                "audio_asset_id": null,
                "submitted_by": "browser.forgery",
                "moderation_notes": null,
                "moderated_by": "browser.forgery",
                "revision": 2,
                "created_at_unix_ms": now,
                "updated_at_unix_ms": now.saturating_add(3)
            },
            "expected_revision": 1,
            "audit": {
                "audit_id": "f8a1f8d7-3808-45e6-9775-399b6bf7808f",
                "board_id": board_id,
                "channel_id": channel_id,
                "event_kind": "bulletin_moderated",
                "operator_id": "browser.forgery",
                "occurred_at_unix_ms": now.saturating_add(3),
                "details": {}
            }
        }))
        .unwrap();
        let accepted = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "PUT",
                "/api/v1/cg/bulletins",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                &bulletin_accept,
            ),
        );
        assert_eq!(accepted.status, 200);
        let accepted: CgBulletin = serde_json::from_slice(&accepted.body).unwrap();
        assert_eq!(accepted.submitted_by, "historical.submitter");
        assert_eq!(accepted.moderated_by.as_deref(), Some(actor_id.as_str()));

        let bulletin_schedule = serde_json::to_vec(&serde_json::json!({
            "bulletin": {
                "bulletin_id": bulletin_id,
                "channel_id": channel_id,
                "content": {"kind":"text","title":"Meeting","body":"Tuesday at six."},
                "state": "scheduled",
                "tags": ["community"],
                "requested_start_at_unix_ms": now.saturating_add(10_000),
                "requested_end_at_unix_ms": now.saturating_add(20_000),
                "display_seconds": 12,
                "audio_asset_id": null,
                "submitted_by": "browser.forgery",
                "moderation_notes": null,
                "moderated_by": "second.browser.forgery",
                "revision": 3,
                "created_at_unix_ms": now,
                "updated_at_unix_ms": now.saturating_add(4)
            },
            "expected_revision": 2,
            "audit": {
                "audit_id": "7f773412-dd9f-47a7-9724-64fc0ad81dee",
                "board_id": board_id,
                "channel_id": channel_id,
                "event_kind": "bulletin_moderated",
                "operator_id": "browser.forgery",
                "occurred_at_unix_ms": now.saturating_add(4),
                "details": {}
            }
        }))
        .unwrap();
        let scheduled = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "PUT",
                "/api/v1/cg/bulletins",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                &bulletin_schedule,
            ),
        );
        assert_eq!(scheduled.status, 200);
        let scheduled: CgBulletin = serde_json::from_slice(&scheduled.body).unwrap();
        assert_eq!(scheduled.submitted_by, "historical.submitter");
        assert_eq!(scheduled.moderated_by.as_deref(), Some(actor_id.as_str()));

        let expire_body = serde_json::to_vec(&serde_json::json!({
            "channel_id": channel_id,
            "now_unix_ms": now.saturating_add(20_000)
        }))
        .unwrap();
        let expired = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/cg/bulletins/expire",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                &expire_body,
            ),
        );
        assert_eq!(expired.status, 200);
        let expired: Vec<CgBulletin> = serde_json::from_slice(&expired.body).unwrap();
        assert_eq!(expired.len(), 1);
        assert_eq!(expired[0].bulletin_id, bulletin_id);
        assert_eq!(
            api.storage
                .list_cg_audit_events(channel_id, 50)
                .unwrap()
                .iter()
                .find(|event| { event.event_kind == station_cg::CgAuditEventKind::BulletinExpired })
                .unwrap()
                .operator_id,
            actor_id
        );

        let new_bulletin_id = "65b6f5e3-a51e-4a66-936c-c8434f07baf1";
        let new_submission = serde_json::to_vec(&serde_json::json!({
            "bulletin": {
                "bulletin_id": new_bulletin_id,
                "channel_id": channel_id,
                "content": {"kind":"text","title":"Notice","body":"Identity-free submission."},
                "state": "submitted",
                "tags": [],
                "requested_start_at_unix_ms": null,
                "requested_end_at_unix_ms": null,
                "display_seconds": 10,
                "audio_asset_id": null,
                "moderation_notes": null,
                "revision": 1,
                "created_at_unix_ms": now,
                "updated_at_unix_ms": now
            },
            "expected_revision": 0,
            "audit": {
                "audit_id": "9cf47b10-3297-42b2-a355-3ad0821323df",
                "board_id": board_id,
                "channel_id": channel_id,
                "event_kind": "bulletin_submitted",
                "occurred_at_unix_ms": now,
                "details": {}
            }
        }))
        .unwrap();
        let submitted = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "PUT",
                "/api/v1/cg/bulletins",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                &new_submission,
            ),
        );
        assert_eq!(submitted.status, 200);
        let submitted: CgBulletin = serde_json::from_slice(&submitted.body).unwrap();
        assert_eq!(submitted.submitted_by, actor_id);
        assert_eq!(submitted.moderated_by, None);

        let item_id = "a".repeat(64);
        api.storage
            .replace_cg_feed_cache(
                feed_id,
                now.saturating_add(5),
                &[CgFeedItem {
                    item_id: item_id.clone(),
                    title: "Town notice".into(),
                    summary: None,
                    starts_at_unix_ms: None,
                    url: None,
                    tags: vec![],
                }],
                None,
                None,
            )
            .unwrap();
        let approval_body = serde_json::to_vec(&serde_json::json!({
            "approval": {
                "feed_source_id": feed_id,
                "item_id": item_id,
                "approved_by": "browser.forgery",
                "approved_at_unix_ms": now.saturating_add(6)
            },
            "audit": {
                "audit_id": "df4ef773-e673-4279-a87e-6a3936692dd0",
                "board_id": board_id,
                "channel_id": channel_id,
                "event_kind": "feed_item_approved",
                "operator_id": "browser.forgery",
                "occurred_at_unix_ms": now.saturating_add(6),
                "details": {}
            }
        }))
        .unwrap();
        let approval_response = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/cg/feed-items/approve",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                &approval_body,
            ),
        );
        assert_eq!(approval_response.status, 200);
        assert_eq!(
            api.storage
                .read_cg_feed_item_approval(feed_id, &item_id)
                .unwrap()
                .unwrap()
                .approved_by,
            actor_id
        );
        assert_eq!(
            api.storage
                .list_cg_audit_events(channel_id, 50)
                .unwrap()
                .iter()
                .find(|event| event.audit_id == "df4ef773-e673-4279-a87e-6a3936692dd0")
                .unwrap()
                .operator_id,
            actor_id
        );

        let self_test_schedule = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "PUT",
                "/api/v1/self-test-schedules/daily",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                br#"{"enabled":true,"local_hour":3,"local_minute":15,"weekday":null,"updated_by":"browser.forgery"}"#,
            ),
        );
        assert_eq!(self_test_schedule.status, 200);
        assert_eq!(
            serde_json::from_slice::<SelfTestSchedule>(&self_test_schedule.body)
                .unwrap()
                .updated_by,
            actor_id
        );

        let missing_csrf = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/auth/logout",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                ],
                b"{}",
            ),
        );
        assert_eq!(missing_csrf.status, 403);

        let old_boot = auth::AuthRuntime::new("127.0.0.1:4070".parse().unwrap()).unwrap();
        let rejected_old_boot = dispatch_http(
            &api,
            &old_boot,
            &raw_request(
                "GET",
                "/api/v1/auth/session",
                &[("Host", old_boot.expected_host()), ("Cookie", &cookie)],
                b"",
            ),
        );
        assert_eq!(rejected_old_boot.status, 401);

        let expired_session_token = station_auth::generate_secret_token().unwrap();
        let expired_csrf_token = station_auth::generate_secret_token().unwrap();
        let created = now.saturating_sub(2_000);
        let expired_session = station_auth::OperatorSession::new(
            digest_secret(&expired_session_token),
            digest_secret(&expired_csrf_token),
            account.account_id().clone(),
            account.credential_version(),
            runtime.boot_id(),
            created,
            created,
            now.saturating_sub(1),
            now.saturating_add(60_000),
        )
        .unwrap();
        api.storage
            .create_operator_session(&expired_session, created)
            .unwrap();
        let expired_cookie = format!(
            "{}={}; {}={}",
            auth::SESSION_COOKIE_NAME,
            expired_session_token.expose_secret(),
            auth::CSRF_COOKIE_NAME,
            expired_csrf_token.expose_secret()
        );
        let expired_rejection = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "GET",
                "/api/v1/auth/session",
                &[
                    ("Host", runtime.expected_host()),
                    ("Cookie", &expired_cookie),
                ],
                b"",
            ),
        );
        assert_eq!(expired_rejection.status, 401);

        let logout = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/auth/logout",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Cookie", &cookie),
                    (auth::CSRF_HEADER_NAME, &csrf),
                ],
                b"{}",
            ),
        );
        assert_eq!(logout.status, 200);
        assert_eq!(logout.body, br#"{"logged_out":true}"#);
        assert!(logout.headers.iter().any(|header| {
            header.name == "Set-Cookie"
                && header.value.starts_with(auth::CSRF_COOKIE_NAME)
                && header.value.contains("Path=/;")
                && header.value.contains("Max-Age=0")
        }));

        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn health_is_not_ready_before_current_boot_orchestration_progress() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-health-starting-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api = Api::open_with_application_directory_and_health(
            &database,
            &application,
            Arc::new(OrchestrationHealth::new(Duration::from_secs(45))),
        )
        .unwrap();

        let response = api.handle_in_process("GET", "/health", None);

        assert_eq!(response.status, 503);
        let body: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
        assert_eq!(body["status"], "starting");
        assert_eq!(body["orchestration"]["status"], "starting");

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn health_becomes_ready_after_current_boot_orchestration_progress() {
        let health = OrchestrationHealth::new(Duration::from_secs(45));

        health.mark_progress();

        assert_eq!(health.status(), OrchestrationHealthStatus::Ready);
    }

    #[test]
    fn liveness_refresh_cannot_make_an_unreconciled_boot_ready() {
        let health = OrchestrationHealth::new(Duration::from_secs(45));

        health.mark_alive();

        assert_eq!(health.status(), OrchestrationHealthStatus::Starting);
    }

    #[test]
    fn liveness_refresh_extends_an_already_reconciled_boot() {
        let health = OrchestrationHealth::new(Duration::from_secs(45));
        health.mark_progress();
        let first_progress = health.inner.lock().unwrap().last_progress.unwrap();

        health.mark_alive();
        let refreshed_progress = health.inner.lock().unwrap().last_progress.unwrap();

        assert!(refreshed_progress >= first_progress);
        assert_eq!(health.status(), OrchestrationHealthStatus::Ready);
    }

    #[test]
    fn health_fails_closed_after_current_boot_orchestration_failure() {
        let health = OrchestrationHealth::new(Duration::from_secs(45));
        health.mark_progress();

        health.mark_failed();

        assert_eq!(health.status(), OrchestrationHealthStatus::Failed);
    }

    #[test]
    fn health_becomes_stale_after_the_progress_deadline() {
        let health = OrchestrationHealth::new(Duration::from_secs(45));
        health.mark_progress();
        let progress = health.inner.lock().unwrap().last_progress.unwrap();

        assert_eq!(
            health.status_at(progress + Duration::from_secs(46)),
            OrchestrationHealthStatus::Stale
        );
    }

    #[test]
    fn health_route_tracks_the_shared_current_boot_state() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-health-shared-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let health = Arc::new(OrchestrationHealth::new(Duration::from_secs(45)));
        let api = Api::open_with_application_directory_and_health(
            &database,
            &application,
            Arc::clone(&health),
        )
        .unwrap();

        health.mark_progress();
        let ready = api.handle_in_process("GET", "/health", None);
        assert_eq!(ready.status, 200);
        let ready: serde_json::Value = serde_json::from_slice(&ready.body).unwrap();
        assert_eq!(ready["status"], "ready");

        health.mark_failed();
        let failed = api.handle_in_process("GET", "/health", None);
        assert_eq!(failed.status, 503);
        let failed: serde_json::Value = serde_json::from_slice(&failed.body).unwrap();
        assert_eq!(failed["status"], "failed");

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn api_server_stops_when_orchestration_is_failed() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-health-failed-server-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let health = Arc::new(OrchestrationHealth::new(Duration::from_secs(45)));
        health.mark_failed();
        let result = serve_until_with_application_directory_and_health(
            listener,
            &database,
            &application,
            stop,
            health,
        );

        assert!(result.is_err());
        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

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
            reference_station_tier_selection: None,
            hardware: HardwareProbeInput {
                logical_cpus: 8,
                ram_total_gb: 16.0,
                media_disk_free_gb: 200.0,
                machine_inventory: machine_inventory::unavailable_evidence(1_777_000_000_000),
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

    fn receiver_test_headend(
        enabled: bool,
        sinks: Vec<station_readiness::SinkHeadendReadiness>,
    ) -> station_readiness::HeadendReadiness {
        station_readiness::HeadendReadiness {
            schema_version: 1,
            generated_at_unix_ms: 1,
            channels: vec![station_readiness::ChannelHeadendReadiness {
                channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
                display_name: "Primary".into(),
                enabled,
                profile_id: "atsc-720p5994".into(),
                profile_state: StationReadinessState::Pass,
                sinks,
                state: StationReadinessState::Pass,
                detail: "test fixture".into(),
                evidence: "test fixture".into(),
            }],
            rollup: station_readiness::HeadendReadinessRollup {
                color: station_readiness::PegReadinessColor::Green,
                enabled_channels: usize::from(enabled),
                passing_channels: usize::from(enabled),
                blocked_channels: 0,
                detail: "test fixture".into(),
            },
            detail: "test fixture".into(),
        }
    }

    #[test]
    fn external_receiver_proof_rejects_a_disabled_only_headend() {
        let headend = receiver_test_headend(
            false,
            vec![station_readiness::SinkHeadendReadiness {
                sink_id: "22222222-3333-4444-8555-666666666666".into(),
                label: "Cable".into(),
                kind: HeadendSinkKind::UdpTs,
                required: true,
                enabled: true,
                capability: CapabilitySupport::Supported,
                local_transport: StationReadinessState::Pass,
                external_receiver: ExternalReceiverState::Verified,
                state: StationReadinessState::Pass,
                detail: "test fixture".into(),
                evidence: "test fixture".into(),
            }],
        );

        assert!(!headend_receiver_verified(&headend));
    }

    #[test]
    fn external_receiver_proof_requires_an_enabled_required_sink() {
        let headend = receiver_test_headend(true, Vec::new());

        assert!(!headend_receiver_verified(&headend));
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

        let full = api.handle_in_process("GET", "/api/v1/station-box-profile", None);
        assert_eq!(full.status, 200);
        let full: serde_json::Value = serde_json::from_slice(&full.body).unwrap();
        assert_eq!(full["schema_version"], 6);
        assert_eq!(
            full["peg_readiness"]["color"], "yellow",
            "an unselected deployment tier is unknown and remains fail-closed"
        );
        assert!(
            full["peg_readiness"]["dimensions"]
                .as_array()
                .unwrap()
                .iter()
                .any(|dimension| dimension["name"] == "reference_station"
                    && dimension["state"] == "unknown")
        );
        assert_eq!(full["engine"]["sdi"], "unimplemented");
        assert_eq!(
            full["reference_station_catalog"]["version"],
            station_readiness::REFERENCE_STATION_CATALOG_VERSION
        );
        assert_eq!(
            full["reference_station_catalog"]["matching_tiers"],
            serde_json::json!([]),
            "unmeasured physical requirements must not produce a complete build match"
        );
        assert_eq!(
            full["reference_station_catalog"]["assessments"][0]["tier"],
            "tier0_batch_stream"
        );
        assert!(api.station_box_cache.lock().unwrap().is_some());

        let rollup = api.handle_in_process("GET", "/api/v1/station-box-profile/readiness", None);
        assert_eq!(rollup.status, 200);
        let rollup: serde_json::Value = serde_json::from_slice(&rollup.body).unwrap();
        assert_eq!(rollup, full["peg_readiness"]);

        let profile = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#;
        assert_eq!(
            api.handle_in_process("PUT", "/api/v1/station", Some(profile))
                .status,
            200
        );
        assert!(api.station_box_cache.lock().unwrap().is_none());

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn setup_admin_selects_audited_reference_tier_with_server_actor_and_cas() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-reference-tier-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let before = api.handle_in_process("GET", "/api/v1/station-box-profile", None);
        assert_eq!(before.status, 200);
        let before: serde_json::Value = serde_json::from_slice(&before.body).unwrap();
        assert!(before["reference_station_tier_selection"].is_null());
        assert!(api.station_box_cache.lock().unwrap().is_some());
        let request = serde_json::to_vec(&serde_json::json!({
            "expected_revision": null,
            "catalog_version": station_readiness::REFERENCE_STATION_CATALOG_VERSION,
            "tier": "tier0_batch_stream"
        }))
        .unwrap();

        let created = api.handle_authenticated(
            "PUT",
            "/api/v1/reference-station-tier",
            Some(&request),
            &setup_admin,
        );
        assert_eq!(created.status, 201);
        let created: station_readiness::ReferenceStationTierSelection =
            serde_json::from_slice(&created.body).unwrap();
        assert_eq!(created.revision, 1);
        assert!(
            api.station_box_cache.lock().unwrap().is_none(),
            "a successful mutation must invalidate the primed StationBox cache"
        );
        assert_eq!(
            created.updated_by,
            setup_admin.account_id().as_str(),
            "the server-authenticated account must own the audit identity"
        );
        assert_eq!(
            api.storage
                .list_reference_station_tier_audit()
                .unwrap()
                .len(),
            1
        );

        let stale = api.handle_authenticated(
            "PUT",
            "/api/v1/reference-station-tier",
            Some(&request),
            &setup_admin,
        );
        assert_eq!(stale.status, 409);
        assert_eq!(
            api.storage
                .list_reference_station_tier_audit()
                .unwrap()
                .len(),
            1
        );

        let forged_authority = serde_json::to_vec(&serde_json::json!({
            "expected_revision": 1,
            "catalog_version": station_readiness::REFERENCE_STATION_CATALOG_VERSION,
            "tier": "tier1_streaming",
            "actor": "browser-supplied-identity",
            "occurred_at_unix_ms": 1
        }))
        .unwrap();
        let forged = api.handle_authenticated(
            "PUT",
            "/api/v1/reference-station-tier",
            Some(&forged_authority),
            &setup_admin,
        );
        assert_eq!(forged.status, 400);
        assert_eq!(
            api.storage
                .list_reference_station_tier_audit()
                .unwrap()
                .len(),
            1,
            "browser-supplied audit identity and time must be rejected without an event"
        );

        let non_admin = test_actor(station_auth::OperatorRole::MeetingOperator);
        let forbidden = api.handle_authenticated(
            "PUT",
            "/api/v1/reference-station-tier",
            Some(&request),
            &non_admin,
        );
        assert_eq!(forbidden.status, 403);

        let profile = api.handle_in_process("GET", "/api/v1/station-box-profile", None);
        assert_eq!(
            profile.status,
            200,
            "{}",
            String::from_utf8_lossy(&profile.body)
        );
        let profile: serde_json::Value = serde_json::from_slice(&profile.body).unwrap();
        assert_eq!(
            profile["reference_station_tier_selection"]["tier"],
            "tier0_batch_stream"
        );
        let selected_state = profile["reference_station_catalog"]["assessments"]
            .as_array()
            .unwrap()
            .iter()
            .find(|assessment| assessment["tier"] == "tier0_batch_stream")
            .unwrap()["state"]
            .clone();
        let dimension_state = profile["peg_readiness"]["dimensions"]
            .as_array()
            .unwrap()
            .iter()
            .find(|dimension| dimension["name"] == "reference_station")
            .unwrap()["state"]
            .clone();
        assert_eq!(
            dimension_state, selected_state,
            "the readiness dimension must mirror the exact selected catalog assessment"
        );

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn commissioning_progress_uses_current_station_box_storage_time_and_backup_truth() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-commissioning-station-box-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let mut api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();

        let passing = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        let passing: serde_json::Value = serde_json::from_slice(&passing.body).unwrap();
        for id in ["station_box_storage", "station_time", "station_backup"] {
            let check = passing["checks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|check| check["id"] == id)
                .unwrap();
            assert_eq!(check["status"], "pass", "{id}");
        }

        let mut failed_probe = station_box_probe();
        failed_probe.hardware.media_disk_free_gb = 10.0;
        failed_probe.clock.sync_status = ClockSyncStatus::Unknown;
        failed_probe.backup.state = StationReadinessState::Blocked;
        failed_probe.backup.writable = Some(false);
        api.station_box_probe_override = Some(failed_probe);
        *api.station_box_cache.lock().unwrap() = None;
        let failed = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        let failed: serde_json::Value = serde_json::from_slice(&failed.body).unwrap();
        for id in ["station_box_storage", "station_time", "station_backup"] {
            let check = failed["checks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|check| check["id"] == id)
                .unwrap();
            assert_eq!(check["status"], "blocked", "{id}");
            assert_eq!(check["blocking"], true, "{id}");
        }

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

        let aggregate = api.handle_in_process("GET", "/api/v1/headend-readiness", None);
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

        let one = api.handle_in_process(
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
        let stopped = api.handle_in_process("GET", "/api/v1/headend-readiness", None);
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

        let current = api.handle_in_process(
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
        let unverified = api.handle_in_process(
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
        let expired = api.handle_in_process(
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
    fn loudness_proof_job_post_creates_durable_job_and_get_returns_its_status() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-loudness-job-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
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
            })
            .unwrap();
        let configuration_hash = api
            .storage
            .current_channel_output_configuration_hash(channel_id)
            .unwrap();
        let response = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            Some(
                format!(
                    r#"{{"sink_id":"{sink_id}","sample_duration_ms":3000,"max_capture_bytes":1000000}}"#
                )
                .as_bytes(),
            ),
        );
        assert_eq!(
            response.status,
            202,
            "{}",
            String::from_utf8_lossy(&response.body)
        );
        let created: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
        let job_id = created["job_id"].as_str().unwrap().to_string();
        assert_eq!(created["state"], "pending");
        assert_eq!(created["target_group"], "cable");
        assert_eq!(created["target_lufs_milli"], -24_000);
        assert_eq!(created["configuration_hash"], configuration_hash);
        assert_eq!(
            created["source_commit"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        );

        let status = api.handle_in_process(
            "GET",
            &format!("/api/v1/channels/{channel_id}/loudness-proof/jobs/{job_id}"),
            None,
        );
        assert_eq!(
            status.status,
            200,
            "{}",
            String::from_utf8_lossy(&status.body)
        );
        let status: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
        assert_eq!(status["job_id"], job_id);
        assert_eq!(status["sink_id"], sink_id);
        assert_eq!(status["state"], "pending");
        assert!(status["capture"].is_null());

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn loudness_proof_job_post_rejects_an_unverified_candidate() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-loudness-job-unverified-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let mut probe = station_box_probe();
        probe.release.candidate_verified = false;
        probe.release.state = StationReadinessState::Blocked;
        let api = Api::open_with_station_box_probe(&database, &application, probe).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Measured output".into(),
                output_profile: station_schedule::OutputProfileId::Atsc720p5994,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Cable primary",
                    "127.0.0.1:5500",
                    -24.0,
                )],
                enabled: true,
            })
            .unwrap();
        let response = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            Some(
                format!(
                    r#"{{"sink_id":"{sink_id}","sample_duration_ms":3000,"max_capture_bytes":1000000}}"#
                )
                .as_bytes(),
            ),
        );
        assert_eq!(response.status, 409);
        let body: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
        assert_eq!(body["error"]["code"], "candidate_not_verified");

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn loudness_proof_job_post_rejects_duplicate_active_target_group() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-loudness-job-duplicate-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Measured output".into(),
                output_profile: station_schedule::OutputProfileId::Atsc720p5994,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Cable primary",
                    "127.0.0.1:5500",
                    -24.0,
                )],
                enabled: true,
            })
            .unwrap();
        let body = format!(
            r#"{{"sink_id":"{sink_id}","sample_duration_ms":3000,"max_capture_bytes":1000000}}"#
        );
        let first = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            Some(body.as_bytes()),
        );
        assert_eq!(first.status, 202);
        let second = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            Some(body.as_bytes()),
        );
        assert_eq!(second.status, 409);
        let body: serde_json::Value = serde_json::from_slice(&second.body).unwrap();
        assert_eq!(body["error"]["code"], "job_already_active");

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn loudness_proof_job_post_rejects_unbounded_request_fields() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-loudness-job-bounds-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        let response = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            Some(
                format!(
                    r#"{{"sink_id":"{sink_id}","sample_duration_ms":2999,"max_capture_bytes":1000000}}"#
                )
                .as_bytes(),
            ),
        );
        assert_eq!(response.status, 422);
        let response = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            Some(
                format!(
                    r#"{{"sink_id":"{sink_id}","sample_duration_ms":300001,"max_capture_bytes":1000000}}"#
                )
                .as_bytes(),
            ),
        );
        assert_eq!(response.status, 422);
        let response = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            Some(
                format!(
                    r#"{{"sink_id":"{sink_id}","sample_duration_ms":3000,"max_capture_bytes":268435457}}"#
                )
                .as_bytes(),
            ),
        );
        assert_eq!(response.status, 422);

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
        let runtime = auth::AuthRuntime::new("127.0.0.1:4070".parse().unwrap()).unwrap();
        let image_response = dispatch_http(&api, &runtime, &parsed);
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

    #[test]
    fn http_parser_preserves_unambiguous_security_headers() {
        let request = b"POST /api/v1/auth/login HTTP/1.1\r\nHost: 127.0.0.1:4070\r\nOrigin: http://127.0.0.1:4070\r\nCookie: townlight_csrf_v1=csrf-token\r\nX-TownLight-CSRF: csrf-token\r\nContent-Length: 2\r\n\r\n{}";
        let parsed = parse_http_request(request).expect("security headers should parse");
        assert_eq!(parsed.method, "POST");
        assert_eq!(parsed.path, "/api/v1/auth/login");
        assert_eq!(
            parsed.unique_header("host").unwrap(),
            Some("127.0.0.1:4070")
        );
        assert_eq!(
            parsed.unique_header("origin").unwrap(),
            Some("http://127.0.0.1:4070")
        );
        assert_eq!(
            parsed.unique_header("x-townlight-csrf").unwrap(),
            Some("csrf-token")
        );
        assert_eq!(parsed.body, b"{}");
    }

    #[test]
    fn http_parser_rejects_ambiguous_security_headers_and_absolute_targets() {
        for request in [
            b"GET /health HTTP/1.1\r\nHost: one\r\nHost: two\r\n\r\n".as_slice(),
            b"POST /api/v1/auth/login HTTP/1.1\r\nHost: 127.0.0.1:4070\r\nOrigin: http://127.0.0.1:4070\r\nOrigin: http://evil.invalid\r\nContent-Length: 0\r\n\r\n".as_slice(),
            b"POST /api/v1/auth/login HTTP/1.1\r\nHost: 127.0.0.1:4070\r\nOrigin: http://127.0.0.1:4070\r\nSec-Fetch-Site: same-origin\r\nSec-Fetch-Site: cross-site\r\nContent-Length: 0\r\n\r\n".as_slice(),
            b"GET http://127.0.0.1:4070/health HTTP/1.1\r\nHost: 127.0.0.1:4070\r\n\r\n".as_slice(),
        ] {
            let rejection = parse_http_request(request).unwrap_err();
            assert_eq!(rejection.status, 400);
        }
    }

    #[test]
    fn response_writer_emits_only_validated_additional_headers() {
        let response = json_response(200, br#"{"ok":true}"#.to_vec())
            .with_header(
                "Set-Cookie",
                "townlight_session_v1=opaque; HttpOnly; SameSite=Strict; Path=/api/v1/",
            )
            .expect("a bounded cookie header should be valid")
            .with_header("Cache-Control", "no-store")
            .expect("a bounded cache header should be valid");
        assert_eq!(response.headers.len(), 2);
        assert!(
            json_response(200, Vec::new())
                .with_header("Set-Cookie", "safe=value\r\nX-Injected: true")
                .is_err()
        );
    }

    #[test]
    fn authentication_storage_failures_never_expose_internal_details() {
        let secret = r#"C:\ProgramData\TownLight\station.db: operator_sessions malformed"#;
        for response in [
            auth_storage_error_response(),
            auth_failure_response(auth::AuthFailure::Storage(secret.into())),
        ] {
            assert_eq!(response.status, 500);
            let body = String::from_utf8(response.body).unwrap();
            assert!(body.contains("authentication_error"));
            assert!(!body.contains(secret));
            assert!(!body.contains("station.db"));
        }
    }
}
