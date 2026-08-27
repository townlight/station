pub mod auth;
mod machine_inventory;

use std::collections::{BTreeSet, HashMap};
use std::io::{Read, Write};
use std::net::UdpSocket;
use std::net::{TcpListener, TcpStream};
use std::os::windows::fs::MetadataExt;
use std::os::windows::fs::OpenOptionsExt;
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
use station_automation_policy::{
    AdaptiveDefaultHint, AutomationCandidateIdentity, AutomationConnectionTestEvidence,
    AutomationExecutionReceipt, AutomationExecutionRequest, AutomationFeature,
    AutomationFeaturePolicy, ConnectionTestOutcome, FeatureAutomationStatus,
    derive_automation_readiness, derive_feature_automation_status,
};
use station_captions::{
    AssetCaptionCue, AssetCaptionTrack, AssetCaptionTrackAuditEvent, AssetCaptionTrackWrite,
    CaptionEgressProof, CaptionEgressProofJob, CaptionEgressProofJobRecord,
    CaptionEgressProofJobState, CaptionProofCaptureFacts, CaptionTrackSourceKind,
    CaptionTrackWriteError, MAX_CAPTION_PROOF_CAPTURE_BYTES, MAX_CAPTION_PROOF_CAPTURE_DURATION_MS,
    expected_caption_evidence_for_window,
};
use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_cg::{
    CgAuditEvent, CgBoard, CgBulletin, CgBulletinState, CgFeedItem, CgFeedItemApproval,
    CgFeedSource, CgImageAsset, CgImageReadiness, CgZoneConfig, built_in_templates,
};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertEventState, AlertRule, AlertSeverity,
    ChannelRuntimeState, SafeToAirColor, SelfTestKind, SelfTestSchedule, SelfTestTrigger,
    StationProfile, derive_runtime_safe_to_air, redact_alert_channel_target,
    validate_alert_channel_target,
};
use station_live::{LiveSource, LiveSourceWriteError, TakeoverSession, TakeoverState};
use station_maintenance::{
    acquire_install_authority_fence, read_verified_candidate_identity,
    read_verified_install_authority,
};
use station_media_assets::ingest_media;
use station_media_lifecycle::{
    AssetLoudnessAnalysis, AssetReadinessView, AssetRecoveryAudit, AssetReplacementLink,
    AssetRetentionPolicy, AssetThumbnail, IngestJobStatus, IngestSourceKind, LoudnessGateStatus,
    MediaIngestJob, MissingMediaAlert, ReadinessDashboard, ReadinessState, RetentionDisposition,
    TranscodeJobStatus, WatchFolderConfig,
};
use station_metadata::{
    AssetMetadataRecord, AssetMetadataValue, MetadataFieldDefinition, MetadataSchema,
};
use station_ott_contract::ClientPlaybackBeacon;
use station_ott_runtime::PlaybackSessionIssueRequest;
use station_publishing::{
    ConnectionTestOutcome as PublishConnectionTestOutcome, PublishCandidateIdentity,
    PublishConnectionTestEvidence, PublishExecutionReceipt, PublishExecutionTargetConfig,
    PublishTargetAccount, PublishTargetKind, PublishTargetProfile, RequiredTierStatus,
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
use station_recording::{
    RecordingActor, RecordingActorRole, RecordingCandidateIdentity, RecordingEvent,
    RecordingExecutionAuthority, RecordingTransitionRequest, ScheduledRecording,
};
use station_reporting::runtime::{render_csv, render_xmltv};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, CommitReport, DispatchStatus, HEADEND_PROFILES,
    HeadendProfileApplyRequest, MediaAsset, OUTPUT_PROFILES, ScheduleItem, ScheduleState,
    ScheduleWindow,
};
use station_schedule_automation::{
    AssetPickStrategy, AuthenticatedCandidateProjection, AuthenticatedMaterializationInputs,
    AuthenticatedPriorAirings, DaypartBlock, LocalDate, MAX_EVIDENCE_RECEIPT_LIFETIME_MS,
    MaterializationWindow, RecurrenceTimeZone, RecurringRule, SavedSearch, SavedSearchQuery,
    SourceEvidenceKind, canonical_candidate_projection_sha256, canonical_prior_airings_sha256,
    materialization_evidence_scope_sha256,
};
use station_source_evidence::{
    CanonicalProjectionSha256, EvidenceScopeSha256, StationSourceEvidenceAuthority,
};
use station_storage::{
    AlertChannelSecret, AssetMetadataWriteError, AutomationExecutionJobRecord,
    AutomationExecutionJobState, CableTransportValidationSnapshot, CaptionPolicyWriteError,
    CatalogAssetWriteError, CgSnapshotReadError, CgWriteError, ChannelCaptionMode,
    ChannelCaptionPolicy, ChannelCaptionPolicyAuditEvent, ChannelFallbackSlate,
    ChannelFallbackSlateAuditEvent, ChannelWriteError, CommissioningSupportBundleReceipt,
    CommitWriteError, CustomMetadataSchemaWriteError, ExistingScheduleSnapshot,
    ExternalReceiverAcceptanceReceipt, FallbackSlateWriteError, HeadendProfileWriteError,
    OttAuthorityError, OutputLoudnessProofJob, OutputLoudnessProofJobRecord,
    OutputLoudnessProofJobState, ProfileWriteError, PublishExecutionJob, PublishExecutionJobRecord,
    PublishExecutionJobState, PublishingAuthorityWriteError, ReceiverAcceptanceOutcome,
    ReceiverEvidenceMethod, ReferenceStationTierWrite, ReferenceStationTierWriteError,
    ScheduleAutomationWriteError, ScheduleItemWrite, ScheduleMaterializationApplyOutcome,
    ScheduleMaterializationHistoryCursor, ScheduleMaterializationHistoryEntry,
    ScheduleMaterializationHistoryReadError, ScheduleMaterializationOutcome,
    ScheduleMaterializationOutcomeRecord, ScheduleMaterializationPlanRecord,
    ScheduleMaterializationRun, ScheduleMaterializationRunOrigin,
    ScheduleMaterializationWriteError, StationStore, StoredSinkLoudnessProof, TransportProofJob,
    TransportProofJobRecord, TransportProofJobState,
};
use station_transport_proof::{CableTransportValidation, TargetDeviceConfig, TargetProtocol};
use station_update::UpdateCheckTrigger;
use station_update_coordinator::CoordinatorError;

const OPERATOR_HTML: &[u8] = include_bytes!("../../../web/operator/dist/index.html");
const OPERATOR_SCRIPT: &[u8] = include_bytes!("../../../web/operator/dist/operator.js");
const OPERATOR_STYLES: &[u8] = include_bytes!("../../../web/operator/dist/operator.css");
const MAX_CONCURRENT_REQUESTS: usize = 16;
const MAX_SMALL_REQUEST_BODY: usize = 65_536;
const MAX_CG_IMAGE_BODY: usize = 8 * 1024 * 1024;
const MAX_CAPTION_TRACK_BODY: usize = 8 * 1024 * 1024;
const STATION_BOX_CACHE_TTL: Duration = Duration::from_secs(60);
const LOUDNESS_PROOF_MAX_AGE_MS: i64 = 24 * 60 * 60 * 1_000;
const LOUDNESS_PROOF_MIN_DURATION_MS: u64 = 3_000;
const LOUDNESS_PROOF_MAX_DURATION_MS: u64 = 600_000;
const LOUDNESS_PROOF_MAX_CAPTURE_BYTES: u64 = 1024 * 1024 * 1024;
const CAPTION_PROOF_MIN_DURATION_MS: u64 = 3_000;
const CAPTION_PROOF_RUNTIME_MAX_AGE_MS: i64 = 15_000;
// The controller and worker must claim the proof before its narrow acceptance
// window opens. This is deliberately finite: it reserves a bounded command
// budget without allowing a late capture to silently become valid.
const CAPTION_PROOF_EXECUTION_LEAD_MS: u64 = 5_000;
const AUTOMATION_CONNECTION_TEST_TTL_MS: i64 = 24 * 60 * 60 * 1_000;
const PUBLISH_CONNECTION_TEST_TTL_MS: i64 = 24 * 60 * 60 * 1_000;
const RECORDING_WORKER_ACTOR_ID: &str = "00000000-0000-4000-8000-000000000021";
const RECORDING_CANDIDATE_FUTURE_WINDOW_MS: i64 = 31 * 24 * 60 * 60 * 1_000;
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

fn aggregate_cable_transport_snapshot(
    validations: &[CableTransportValidationSnapshot],
) -> (bool, Vec<String>) {
    if validations.is_empty() {
        return (
            false,
            vec![
                "No enabled required transport sink is present in the authoritative snapshot."
                    .into(),
            ],
        );
    }
    let mut details = Vec::with_capacity(validations.len());
    let mut passes = true;
    for snapshot in validations {
        match snapshot.validation.as_ref() {
            Some(validation) if validation.outcome.is_some() => details.push(format!(
                "{} / {} has current analysis and target reachability proof",
                snapshot.channel_id, snapshot.sink_id
            )),
            Some(validation) => {
                passes = false;
                details.push(format!(
                    "{} / {} remains blocked: {}",
                    snapshot.channel_id, snapshot.sink_id, validation.detail
                ));
            }
            None => {
                passes = false;
                details.push(format!(
                    "{} / {} has no configured target device",
                    snapshot.channel_id, snapshot.sink_id
                ));
            }
        }
    }
    (passes, details)
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
                    && item.revision == report.schedule_item_revision
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

fn current_cea708_caption_proof(
    storage: &StationStore,
    channels: &[ChannelConfiguration],
    candidate_version: &str,
    source_commit: &str,
) -> Result<Option<CaptionEgressProof>, String> {
    for channel in channels.iter().filter(|channel| channel.enabled) {
        let Some(policy) = storage.read_channel_caption_policy(&channel.channel_id)? else {
            continue;
        };
        if policy.mode != ChannelCaptionMode::Cea708 {
            continue;
        }
        let configuration_hash =
            storage.current_channel_output_configuration_hash(&channel.channel_id)?;
        for proof in storage.list_caption_egress_proofs(&channel.channel_id, None, 1_000)? {
            if proof.outcome != station_captions::CaptionProofOutcome::Verified
                || proof.candidate_version != candidate_version
                || proof.source_commit != source_commit
                || proof.configuration_hash != configuration_hash
                || proof.caption_policy_revision != policy.revision
                || proof.language != policy.language
                || proof.service_number != policy.service_number
            {
                continue;
            }
            if !channel.output_sinks.iter().any(|sink| {
                sink.sink_id == proof.sink_id
                    && sink.enabled
                    && matches!(
                        sink.kind,
                        station_schedule::OutputSinkKind::UdpTs
                            | station_schedule::OutputSinkKind::Srt
                    )
            }) {
                continue;
            }
            let Some(report) = storage.read_commit_report(&proof.report_id)? else {
                continue;
            };
            if report.channel_id != proof.channel_id
                || report.asset_id != proof.asset_id
                || !matches!(
                    report.dispatch_status,
                    DispatchStatus::Acknowledged | DispatchStatus::Completed
                )
                || proof.captured_at_unix_ms < report.approved_at_unix_ms
            {
                continue;
            }
            let Some(asset) = storage.read_media_asset(&proof.asset_id)? else {
                continue;
            };
            if asset.readiness != AssetReadiness::Ready || !Path::new(&asset.media_path).is_file() {
                continue;
            }
            let Some(track) = storage.read_asset_caption_track(&proof.track_id)? else {
                continue;
            };
            if track.asset_id != proof.asset_id
                || track.revision != proof.track_revision
                || track.cues_sha256 != proof.cues_sha256
                || track.language != proof.language
                || track.service_number != proof.service_number
            {
                continue;
            }
            let Ok(expected) = expected_caption_evidence_for_window(
                &track,
                asset.duration_ms,
                proof.capture_started_asset_offset_ms,
                proof.capture_duration_ms,
            ) else {
                continue;
            };
            if expected.cue_count != proof.expected_cue_count
                || expected.text_sha256 != proof.expected_text_sha256
                || proof.decoded_cue_count != proof.expected_cue_count
                || proof.decoded_text_sha256 != proof.expected_text_sha256
            {
                continue;
            }
            return Ok(Some(proof));
        }
    }
    Ok(None)
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

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestCaptionProofJob {
    report_id: String,
    sink_id: String,
    capture_duration_ms: u64,
    max_capture_bytes: u64,
}

#[derive(Debug, Serialize)]
struct CaptionProofJobResponse {
    job_id: String,
    channel_id: String,
    sink_id: String,
    report_id: String,
    asset_id: String,
    track_id: String,
    track_revision: u64,
    cues_sha256: String,
    caption_policy_revision: u64,
    language: String,
    service_number: u8,
    candidate_version: String,
    source_commit: String,
    configuration_hash: String,
    capture_started_asset_offset_ms: u64,
    capture_duration_ms: u64,
    max_capture_bytes: u64,
    expected_cue_count: u32,
    expected_text_sha256: String,
    state: &'static str,
    created_at_unix_ms: i64,
    updated_at_unix_ms: i64,
    lease_expires_at_unix_ms: Option<i64>,
    capture: Option<CaptionProofCaptureResponse>,
    completed_at_unix_ms: Option<i64>,
    failed_at_unix_ms: Option<i64>,
    failure_detail: Option<String>,
}

#[derive(Debug, Serialize)]
struct CaptionProofCaptureResponse {
    artifact_relative_path: String,
    capture_bytes: u64,
    capture_duration_ms: u64,
    capture_sha256: String,
}

#[derive(Debug, Serialize)]
struct ChannelCaptionProofResponse {
    channel_id: String,
    proofs: Vec<CaptionEgressProof>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum ReceiverReceiptOutcomeRequest {
    Accepted,
    Rejected,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum ReceiverEvidenceMethodRequest {
    HeadendOperatorConfirmation,
    ReceiverTelemetry,
    ReturnPathObservation,
    VisualAudioObservation,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CreateReceiverReceiptRequest {
    sink_id: String,
    outcome: ReceiverReceiptOutcomeRequest,
    method: ReceiverEvidenceMethodRequest,
    receiver_label: String,
    evidence_reference: String,
    detail: String,
    observed_at_unix_ms: i64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutTransportTargetRequest {
    target_id: String,
    label: String,
    protocol: TargetProtocol,
    host: String,
    port: u16,
    /// Write-only local Setup-Admin authorization. It is DPAPI-protected by
    /// storage and is never included in responses, events, or receipts.
    authorization: Option<String>,
    expected_revision: Option<u64>,
}

#[derive(Debug, Serialize)]
struct TransportProofResponse {
    channel_id: String,
    history: station_storage::TransportProofHistory,
    validations: Vec<CableTransportValidation>,
}

#[derive(Debug, Serialize)]
struct TransportProofJobResponse {
    job_id: String,
    channel_id: String,
    sink_id: String,
    target_id: String,
    target_revision: u64,
    candidate_version: String,
    source_commit: String,
    configuration_hash: String,
    state: &'static str,
    created_at_unix_ms: i64,
    updated_at_unix_ms: i64,
    lease_expires_at_unix_ms: Option<i64>,
    capture: Option<TransportProofCaptureResponse>,
    analyzer_output_sha256: Option<String>,
    tables_output_sha256: Option<String>,
    completed_at_unix_ms: Option<i64>,
    failed_at_unix_ms: Option<i64>,
    failure_detail: Option<String>,
}

#[derive(Debug, Serialize)]
struct TransportProofCaptureResponse {
    artifact_relative_path: String,
    capture_bytes: u64,
    capture_sha256: String,
    started_at_unix_ms: i64,
    ended_at_unix_ms: i64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutAutomationFeaturePolicyRequest {
    feature: AutomationFeature,
    operator_selected_adapter_id: Option<String>,
    adaptive_default: Option<AdaptiveDefaultHint>,
    configuration_hash: String,
    expected_revision: Option<u64>,
}

/// The browser proposes non-secret account settings. Identity, revision,
/// attribution, and occurrence time are always server-derived.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutPublishTargetAccountRequest {
    kind: PublishTargetKind,
    label: String,
    enabled: bool,
    expected_revision: Option<u64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutPublishAccountCredentialRequest {
    expected_account_revision: u64,
    credential: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DeletePublishAccountCredentialRequest {
    expected_account_revision: u64,
}

/// The browser proposes non-secret target configuration. Identity, revision,
/// attribution, and occurrence time are always server-derived.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutPublishTargetProfileRequest {
    account_id: String,
    kind: PublishTargetKind,
    label: String,
    enabled: bool,
    configuration: PublishExecutionTargetConfig,
    expected_revision: Option<u64>,
}

#[derive(Debug, Serialize)]
struct PublishExecutionJobResponse {
    job: PublishExecutionJob,
    state: &'static str,
    attempts: u32,
    next_attempt_at_unix_ms: i64,
    updated_at_unix_ms: i64,
    lease_expires_at_unix_ms: Option<i64>,
    completed_at_unix_ms: Option<i64>,
    failure_class: Option<String>,
    receipts: Vec<PublishExecutionReceipt>,
}

/// The browser proposes only mutable field definitions. Schema identity,
/// revision, attribution, and time are derived by the server.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutCustomMetadataSchemaRequest {
    fields: Vec<MetadataFieldDefinition>,
    expected_revision: Option<u64>,
}

/// The browser proposes values and the requested asset/schema identities only.
/// The server resolves the current schema revision, record revision, actor, and
/// occurrence time; callers cannot submit those authority facts.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutAssetMetadataRecordRequest {
    asset_id: String,
    schema_id: String,
    values: Vec<AssetMetadataValue>,
    expected_revision: Option<u64>,
}

#[derive(Debug, Serialize)]
struct CustomMetadataSchemaHistoryResponse {
    schema: MetadataSchema,
    changed_by: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Serialize)]
struct AssetMetadataRecordHistoryResponse {
    record: AssetMetadataRecord,
    changed_by: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Serialize)]
struct AutomationFeaturePolicyStatusResponse {
    policy: AutomationFeaturePolicy,
    status: FeatureAutomationStatus,
    evidence: Option<AutomationConnectionTestEvidence>,
}

#[derive(Debug, Serialize)]
struct AutomationFeaturePolicyHistoryResponse {
    policy: AutomationFeaturePolicy,
    changed_by: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Serialize)]
struct AutomationExecutionJobStatusResponse<'a> {
    request: &'a AutomationExecutionRequest,
    state: &'static str,
    attempt_count: u32,
    lease_expires_at_unix_ms: Option<i64>,
    receipt: Option<&'a AutomationExecutionReceipt>,
    last_failure_code: Option<&'a str>,
    created_at_unix_ms: i64,
    next_attempt_at_unix_ms: i64,
    completed_at_unix_ms: Option<i64>,
}

#[derive(Debug, Serialize)]
struct PublishTargetAccountHistoryResponse {
    account: PublishTargetAccount,
    changed_by: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Serialize)]
struct PublishTargetProfileHistoryResponse {
    target: PublishTargetProfile,
    changed_by: String,
    occurred_at_unix_ms: i64,
}

/// The browser proposes only the station-wide name and query. Identity,
/// revision, ownership, creation time, attribution, and change time are
/// server-derived authority facts.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutSavedSearchRequest {
    name: String,
    query: SavedSearchQuery,
    expected_revision: Option<u64>,
}

/// The browser proposes mutable daypart fields. Identity, revision,
/// attribution, and time are server-derived authority facts.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutDaypartBlockRequest {
    channel_id: String,
    label: String,
    starts_local_minute: u16,
    ends_local_minute: u16,
    weekday_mask: u8,
    season_starts_on: Option<LocalDate>,
    season_ends_on: Option<LocalDate>,
    allow_rule_ids: Vec<String>,
    expected_revision: Option<u64>,
}

/// Dependency revisions are explicit operator intent and storage verifies they
/// are still current. Rule identity, revision, attribution, and time are
/// server-derived authority facts.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutRecurringRuleRequest {
    channel_id: String,
    saved_search_id: String,
    saved_search_revision: u64,
    pick_strategy: AssetPickStrategy,
    rolling_window_days: u8,
    avoid_repeat_within_days: Option<u8>,
    enabled: bool,
    priority: i16,
    block_id: String,
    block_revision: u64,
    weekday_mask: u8,
    interval_weeks: u8,
    anchor_date: LocalDate,
    timezone: RecurrenceTimeZone,
    local_start_minute: u16,
    duration_minutes: u16,
    expected_revision: Option<u64>,
}

#[derive(Debug, Serialize)]
struct SavedSearchHistoryResponse {
    search: SavedSearch,
    changed_by: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Serialize)]
struct DaypartBlockHistoryResponse {
    block: DaypartBlock,
    changed_by: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Serialize)]
struct RecurringRuleHistoryResponse {
    rule: RecurringRule,
    changed_by: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum ScheduleMaterializationDispositionResponse {
    Applied,
    AlreadyApplied,
}

#[derive(Debug, Serialize)]
struct MaterializationWindowResponse {
    starts_at_utc_ms: i64,
    ends_at_utc_ms: i64,
}

#[derive(Debug, Serialize)]
struct ExistingScheduleSnapshotResponse<'a> {
    schedule_item_id: &'a str,
    revision: u64,
    channel_id: &'a str,
    asset_sha256: &'a str,
    title: &'a str,
    starts_at_utc_ms: i64,
    duration_ms: u64,
    state: &'a str,
}

#[derive(Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum ScheduleMaterializationOutcomeResponse<'a> {
    Proposed {
        occurrence_id: &'a str,
        schedule_item_id: &'a str,
        asset_sha256: &'a str,
        asset_title: &'a str,
        starts_at_utc_ms: i64,
        ends_at_utc_ms: i64,
    },
    NoMatch {
        starts_at_utc_ms: i64,
    },
    Blocked {
        starts_at_utc_ms: i64,
    },
    SuppressedByPrecedence {
        starts_at_utc_ms: i64,
        winner_rule_id: &'a str,
        candidate_priority: i16,
        winner_priority: i16,
        reason: &'a str,
    },
    ExistingAutomationDraftUnchanged {
        occurrence_id: &'a str,
        schedule_item_id: &'a str,
        existing: ExistingScheduleSnapshotResponse<'a>,
    },
    ExistingAutomationDraftChanged {
        occurrence_id: &'a str,
        schedule_item_id: &'a str,
        existing: ExistingScheduleSnapshotResponse<'a>,
    },
    ExistingCommitted {
        occurrence_id: &'a str,
        schedule_item_id: &'a str,
        existing: ExistingScheduleSnapshotResponse<'a>,
    },
    CancelledOccurrence {
        occurrence_id: &'a str,
        schedule_item_id: &'a str,
        existing: ExistingScheduleSnapshotResponse<'a>,
    },
    AlreadyAired {
        occurrence_id: &'a str,
        schedule_item_id: &'a str,
        existing: ExistingScheduleSnapshotResponse<'a>,
    },
    SuppressedByExistingSchedule {
        occurrence_id: &'a str,
        schedule_item_id: &'a str,
        existing: ExistingScheduleSnapshotResponse<'a>,
        existing_owner: &'a str,
    },
}

#[derive(Debug, Serialize)]
struct ScheduleMaterializationPlanResponse<'a> {
    ordinal: u32,
    rule_id: &'a str,
    rule_revision: u64,
    channel_id: &'a str,
    saved_search_id: &'a str,
    saved_search_revision: u64,
    block_id: &'a str,
    block_revision: u64,
    window: MaterializationWindowResponse,
    source_authority_sha256: &'a str,
    candidate_projection_sha256: &'a str,
    candidate_receipt_sha256: &'a str,
    prior_airings_sha256: &'a str,
    prior_receipt_sha256: &'a str,
    prior_airings_coverage: MaterializationWindowResponse,
    scope_sha256: &'a str,
    source_input_sha256: &'a str,
}

#[derive(Debug, Serialize)]
struct ScheduleMaterializationOutcomeRecordResponse<'a> {
    ordinal: u32,
    rule_id: &'a str,
    rule_revision: u64,
    channel_id: &'a str,
    saved_search_id: &'a str,
    saved_search_revision: u64,
    block_id: &'a str,
    block_revision: u64,
    plan_window: MaterializationWindowResponse,
    materialization_id: Option<&'a str>,
    source_authority_sha256: &'a str,
    candidate_projection_sha256: &'a str,
    prior_airings_sha256: &'a str,
    prior_airings_coverage: MaterializationWindowResponse,
    scope_sha256: &'a str,
    source_input_sha256: &'a str,
    compiled_outcome: ScheduleMaterializationOutcomeResponse<'a>,
    outcome: ScheduleMaterializationOutcomeResponse<'a>,
}

#[derive(Debug, Serialize)]
struct ScheduleMaterializationResponse<'a> {
    disposition: ScheduleMaterializationDispositionResponse,
    run_id: &'a str,
    request_sha256: &'a str,
    result_sha256: &'a str,
    batch_sha256: &'a str,
    window: MaterializationWindowResponse,
    observed_at_utc_ms: i64,
    changed_by: &'a str,
    occurred_at_unix_ms: i64,
    plans: Vec<ScheduleMaterializationPlanResponse<'a>>,
    outcomes: Vec<ScheduleMaterializationOutcomeRecordResponse<'a>>,
}

/// Bounded durable materialization history. It deliberately excludes
/// per-boot evidence receipts, HMAC-derived material, retry internals, and any
/// commit, dispatch, or alert-binding claim: none is durable run history.
#[derive(Debug, Serialize)]
struct ScheduleMaterializationHistoryResponse<'a> {
    entries: Vec<ScheduleMaterializationHistoryEntryResponse<'a>>,
    next_cursor: Option<ScheduleMaterializationHistoryCursorResponse>,
}

#[derive(Debug, Serialize)]
struct ScheduleMaterializationHistoryCursorResponse {
    occurred_at_unix_ms: i64,
    run_id: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum ScheduleMaterializationHistoryOriginResponse {
    Automatic,
    Operator,
}

#[derive(Debug, Serialize)]
struct ScheduleMaterializationHistoryEntryResponse<'a> {
    run_id: &'a str,
    channel_id: &'a str,
    origin: ScheduleMaterializationHistoryOriginResponse,
    actor_id: &'a str,
    request_sha256: &'a str,
    result_sha256: &'a str,
    window: MaterializationWindowResponse,
    observed_at_utc_ms: i64,
    occurred_at_unix_ms: i64,
    checkpoint_utc_day: i64,
    plans: Vec<ScheduleMaterializationHistoryPlanResponse<'a>>,
    outcomes: Vec<ScheduleMaterializationHistoryOutcomeResponse<'a>>,
    generated_drafts: Vec<ScheduleMaterializationGeneratedDraftResponse<'a>>,
}

#[derive(Debug, Serialize)]
struct ScheduleMaterializationHistoryPlanResponse<'a> {
    ordinal: u32,
    rule_id: &'a str,
    rule_revision: u64,
    channel_id: &'a str,
    saved_search_id: &'a str,
    saved_search_revision: u64,
    block_id: &'a str,
    block_revision: u64,
    window: MaterializationWindowResponse,
}

#[derive(Debug, Serialize)]
struct ScheduleMaterializationHistoryOutcomeResponse<'a> {
    ordinal: u32,
    rule_id: &'a str,
    rule_revision: u64,
    channel_id: &'a str,
    saved_search_id: &'a str,
    saved_search_revision: u64,
    block_id: &'a str,
    block_revision: u64,
    plan_window: MaterializationWindowResponse,
    materialization_id: Option<&'a str>,
    compiled_outcome: ScheduleMaterializationOutcomeResponse<'a>,
    outcome: ScheduleMaterializationOutcomeResponse<'a>,
}

#[derive(Debug, Serialize)]
struct ScheduleMaterializationGeneratedDraftResponse<'a> {
    occurrence_id: &'a str,
    schedule_item_id: &'a str,
    current_schedule: &'a ScheduleItem,
}

/// The browser proposes only mutable editorial text and CAS intent. Asset
/// identity, revision, state, first-publication time, actor, and event time are
/// derived by the server.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutCatalogAssetRequest {
    title: String,
    meeting_body: Option<String>,
    expected_revision: Option<u64>,
}

/// Publish and withdraw are explicit state transitions. The browser supplies
/// only the exact revision it observed.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogAssetTransitionRequest {
    expected_revision: u64,
}

#[derive(Debug, Serialize)]
struct CatalogAssetHistoryResponse {
    record: CatalogAssetRecord,
    changed_by: String,
    occurred_at_utc_ms: i64,
}

#[derive(Debug, Serialize)]
struct CatalogAssetPageResponse {
    records: Vec<CatalogAssetRecord>,
    next_after_asset_sha256: Option<String>,
}

/// Trusted adapter-to-server observation. Candidate identity and all timestamps are
/// deliberately absent so this type cannot self-assert server authority facts.
#[derive(Debug, Clone)]
pub struct TrustedAutomationConnectionTestObservation {
    pub evidence_id: String,
    pub policy_id: String,
    pub adapter_id: String,
    pub policy_revision: u64,
    pub adapter_revision: u64,
    pub configuration_hash: String,
    pub outcome: ConnectionTestOutcome,
    pub detail: String,
}

/// Trusted provider observation. Candidate identity, account/target revisions,
/// configuration hash, timestamps, and expiry are server-derived.
#[derive(Debug, Clone)]
pub struct TrustedPublishConnectionTestObservation {
    pub evidence_id: String,
    pub target_id: String,
    pub outcome: PublishConnectionTestOutcome,
    pub detail: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PutFallbackSlateRequest {
    image_asset_id: String,
    expected_revision: u64,
}

#[derive(Debug, Serialize)]
struct FallbackSlateSelectionResponse {
    channel_id: String,
    image_asset: CgImageAsset,
    revision: u64,
    created_at_unix_ms: i64,
    updated_at_unix_ms: i64,
    updated_by: String,
}

#[derive(Debug, Serialize)]
struct FallbackSlateAuditResponse {
    audit_id: String,
    channel_id: String,
    image_asset_id: String,
    revision: u64,
    actor: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Serialize)]
struct FallbackSlateAuthorityResponse {
    channel_id: String,
    selection: Option<FallbackSlateSelectionResponse>,
    audit: Vec<FallbackSlateAuditResponse>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum CaptionModeRequest {
    Disabled,
    Cea708,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutCaptionPolicyRequest {
    mode: CaptionModeRequest,
    service_number: u8,
    language: String,
    expected_revision: u64,
}

#[derive(Debug, Serialize)]
struct CaptionPolicyResponse {
    channel_id: String,
    mode: &'static str,
    service_number: u8,
    language: String,
    revision: u64,
    created_at_unix_ms: i64,
    updated_at_unix_ms: i64,
    updated_by: String,
}

#[derive(Debug, Serialize)]
struct CaptionPolicyAuditResponse {
    audit_id: String,
    channel_id: String,
    mode: &'static str,
    service_number: u8,
    language: String,
    revision: u64,
    actor: String,
    occurred_at_unix_ms: i64,
}

#[derive(Debug, Serialize)]
struct CaptionPolicyAuthorityResponse {
    channel_id: String,
    policy: Option<CaptionPolicyResponse>,
    audit: Vec<CaptionPolicyAuditResponse>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CaptionCueRequest {
    cue_id: String,
    start_ms: u64,
    duration_ms: u64,
    text: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PutAssetCaptionTrackRequest {
    language: String,
    service_number: u8,
    source_kind: CaptionTrackSourceKind,
    label: String,
    cues: Vec<CaptionCueRequest>,
    expected_revision: u64,
}

#[derive(Debug, Serialize)]
struct AssetCaptionTrackAuthorityResponse {
    track: AssetCaptionTrack,
    audit: Vec<AssetCaptionTrackAuditEvent>,
}

#[derive(Debug, Serialize)]
struct AssetCaptionTrackSummaryResponse {
    track_id: String,
    asset_id: String,
    language: String,
    service_number: u8,
    source_kind: CaptionTrackSourceKind,
    label: String,
    cue_count: usize,
    cues_sha256: String,
    revision: u64,
    created_at_unix_ms: i64,
    updated_at_unix_ms: i64,
    updated_by: String,
}

#[derive(Debug, Serialize)]
struct AssetCaptionTrackListResponse {
    asset_id: String,
    tracks: Vec<AssetCaptionTrackSummaryResponse>,
}

#[derive(Debug, Serialize)]
struct ReceiverReceiptResponse {
    receipt_id: String,
    channel_id: String,
    sink_id: String,
    outcome: &'static str,
    method: &'static str,
    receiver_label: String,
    evidence_reference: String,
    detail: String,
    observed_at_unix_ms: i64,
    recorded_at_unix_ms: i64,
    recorded_by: String,
    candidate_version: String,
    source_commit: String,
    configuration_hash: String,
    current: bool,
}

#[derive(Debug, Serialize)]
struct ReceiverReceiptHistoryResponse {
    channel_id: String,
    receipts: Vec<ReceiverReceiptResponse>,
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
#[serde(deny_unknown_fields)]
struct PrepareScheduleCommit {
    plan_id: String,
    schedule_item_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CommitSchedule {
    report_id: String,
    plan_id: String,
    schedule_item_id: String,
    #[serde(default)]
    operator_notes: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CreateScheduledRecordingRequest {
    recording_id: String,
    schedule_item_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RecordingExpectedRevisionRequest {
    expected_revision: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CancelScheduledRecordingRequest {
    expected_revision: u64,
    reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TrustedScheduledRecordingWorkerEvent {
    StartCapture,
    BeginFinalization,
    RecoverForFinalization {
        reason: String,
    },
    FinalizeSuccess {
        captured_asset_hash: String,
        captured_duration_ms: i64,
    },
    Fail {
        reason: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrustedScheduledRecordingWorkerObservation {
    pub recording_id: String,
    pub expected_revision: u64,
    pub event: TrustedScheduledRecordingWorkerEvent,
}

#[derive(Serialize)]
struct ScheduledRecordingHistoryResponse {
    recording: ScheduledRecording,
    receipt: Option<station_recording::RecordingTransitionReceipt>,
    actor: RecordingActor,
    occurred_at_unix_ms: i64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct IngestMedia {
    job_id: String,
    source_path: PathBuf,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RelinkMissingAssetRequest {
    source_path: PathBuf,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CreateAssetReplacementRequest {
    source_path: PathBuf,
    expected_predecessor_revision: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PutAssetRetentionRequest {
    permanent: bool,
    review_after_unix_ms: Option<i64>,
    legal_hold: bool,
    expected_revision: u64,
}

#[derive(Serialize)]
struct AssetLifecycleResponse {
    asset_id: String,
    retention: Option<AssetRetentionPolicy>,
    replacement: Option<AssetReplacementLink>,
    thumbnail: Option<AssetThumbnail>,
    recovery_history: Vec<AssetRecoveryAudit>,
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

/// Creates one authority for a standalone API/server lifetime. Production
/// stationd supplies its own boot-owned authority explicitly; this fallback
/// must never survive across sequential server boots in one process.
fn new_source_evidence_authority() -> Result<Arc<StationSourceEvidenceAuthority>, String> {
    StationSourceEvidenceAuthority::new()
        .map(Arc::new)
        .map_err(|error| error.to_string())
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

#[cfg(test)]
type CommissioningAfterChannelReadHook = Arc<dyn Fn(&StationStore) + Send + Sync + 'static>;

pub struct Api {
    storage: StationStore,
    database_path: PathBuf,
    application_directory: PathBuf,
    station_box_probe_override: Option<StationBoxProbeInput>,
    station_box_cache: Arc<Mutex<Option<(Instant, StationBoxProfile)>>>,
    orchestration_health: Arc<OrchestrationHealth>,
    source_evidence_authority: Arc<StationSourceEvidenceAuthority>,
    #[cfg(test)]
    commissioning_after_channel_read_hook: Option<CommissioningAfterChannelReadHook>,
}

/// Complete receipt-backed input for one server-owned S19 materialization.
/// It is deliberately available only to trusted in-process callers; no HTTP
/// route accepts authorities, window, raw catalog candidates, or as-run facts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthenticatedMaterializationEvidence {
    pub search: SavedSearch,
    pub block: DaypartBlock,
    pub rule: RecurringRule,
    pub window: MaterializationWindow,
    pub prior_airings_coverage: MaterializationWindow,
    pub inputs: AuthenticatedMaterializationInputs,
}

impl Api {
    /// Standalone/test constructor backed by a fresh authority for this API
    /// lifetime. Stationd production serving uses its explicit boot-owned
    /// authority instead.
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
            new_source_evidence_authority()?,
        )
    }

    fn open_with_application_directory_and_cache(
        database_path: impl AsRef<Path>,
        application_directory: impl AsRef<Path>,
        station_box_cache: Arc<Mutex<Option<(Instant, StationBoxProfile)>>>,
        orchestration_health: Arc<OrchestrationHealth>,
        source_evidence_authority: Arc<StationSourceEvidenceAuthority>,
    ) -> Result<Self, String> {
        let database_path = database_path.as_ref().to_path_buf();
        Ok(Self {
            storage: StationStore::open(&database_path)?,
            database_path,
            application_directory: application_directory.as_ref().to_path_buf(),
            station_box_probe_override: None,
            station_box_cache,
            orchestration_health,
            source_evidence_authority,
            #[cfg(test)]
            commissioning_after_channel_read_hook: None,
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
            source_evidence_authority: new_source_evidence_authority()?,
            #[cfg(test)]
            commissioning_after_channel_read_hook: None,
        })
    }

    /// Builds both receipt-backed S19 source projections from one storage
    /// snapshot. The caller supplies only the recurring-rule identity and a
    /// server-owned materialization window; authorities are never accepted
    /// from a caller and no HTTP route exposes this boundary.
    #[cfg_attr(
        not(test),
        expect(
            dead_code,
            reason = "the server-owned S19 materializer is wired in the next integration slice"
        )
    )]
    pub(crate) fn snapshot_authenticated_materialization_evidence(
        &self,
        rule_id: &str,
        window: &MaterializationWindow,
    ) -> Result<AuthenticatedMaterializationEvidence, String> {
        let snapshot = self
            .storage
            .snapshot_materialization_evidence(rule_id, window)?;
        let candidate_scope_sha256 = materialization_evidence_scope_sha256(
            &snapshot.search,
            &snapshot.block,
            &snapshot.rule,
            window,
            SourceEvidenceKind::CandidateProjection,
        )?;
        let candidate_scope_sha256 =
            EvidenceScopeSha256::new(candidate_scope_sha256).map_err(|error| error.to_string())?;
        let candidate_projection_sha256 =
            canonical_candidate_projection_sha256(&snapshot.candidate_projection.candidates)?;
        if snapshot.candidate_projection.candidate_projection_sha256 != candidate_projection_sha256
        {
            return Err(
                "catalog snapshot digest disagrees with the public S19 canonical projection".into(),
            );
        }
        let candidate_payload_sha256 = CanonicalProjectionSha256::new(candidate_projection_sha256)
            .map_err(|error| error.to_string())?;
        let candidate_receipt = self
            .source_evidence_authority
            .issue(
                SourceEvidenceKind::CandidateProjection,
                &candidate_scope_sha256,
                &candidate_payload_sha256,
                MAX_EVIDENCE_RECEIPT_LIFETIME_MS,
            )
            .map_err(|error| error.to_string())?;
        let prior_scope_sha256 = materialization_evidence_scope_sha256(
            &snapshot.search,
            &snapshot.block,
            &snapshot.rule,
            window,
            SourceEvidenceKind::PriorAirings,
        )?;
        let prior_scope_sha256 =
            EvidenceScopeSha256::new(prior_scope_sha256).map_err(|error| error.to_string())?;
        let prior_projection_sha256 = canonical_prior_airings_sha256(&snapshot.prior_airings)?;
        if snapshot.prior_airings_sha256 != prior_projection_sha256 {
            return Err(
                "prior-airings snapshot digest disagrees with the public S19 canonical projection"
                    .into(),
            );
        }
        let prior_payload_sha256 = CanonicalProjectionSha256::new(prior_projection_sha256)
            .map_err(|error| error.to_string())?;
        let prior_receipt = self
            .source_evidence_authority
            .issue(
                SourceEvidenceKind::PriorAirings,
                &prior_scope_sha256,
                &prior_payload_sha256,
                MAX_EVIDENCE_RECEIPT_LIFETIME_MS,
            )
            .map_err(|error| error.to_string())?;

        Ok(AuthenticatedMaterializationEvidence {
            search: snapshot.search,
            block: snapshot.block,
            rule: snapshot.rule,
            window: snapshot.window,
            prior_airings_coverage: snapshot.prior_airings_coverage,
            inputs: AuthenticatedMaterializationInputs {
                candidate_projection: AuthenticatedCandidateProjection {
                    receipt: candidate_receipt,
                    candidates: snapshot.candidate_projection.candidates,
                },
                prior_airings: AuthenticatedPriorAirings {
                    receipt: prior_receipt,
                    airings: snapshot.prior_airings,
                },
            },
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
            ("GET", "/api/v1/ott/catalog") => self.read_ott_catalog(),
            ("POST", "/api/v1/ott/sessions") => self.issue_ott_session(body),
            ("POST", "/api/v1/ott/beacons") => self.collect_ott_beacon(body),
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
            ("GET", "/api/v1/automation/adapters") => self.list_automation_adapters(actor),
            ("GET", "/api/v1/automation/policies") => self.list_automation_policies(actor),
            ("GET", path) if automation_execution_job_path(path).is_some() => {
                self.read_automation_execution_job(path, actor)
            }
            ("GET", path) if asrun_page_path(path).is_some() => self.read_asrun_page(path, actor),
            ("GET", path) if asrun_export_path(path).is_some() => self.export_asrun(path, actor),
            ("GET", path) if path.starts_with("/api/v1/reporting/as-run/") => error_response(
                400,
                "invalid_path",
                "The as-run path or bounded query is invalid.",
            ),
            ("GET", path) if automation_policy_status_path(path).is_some() => {
                self.read_automation_policy_status(path, actor)
            }
            ("GET", path) if automation_policy_history_path(path).is_some() => {
                self.list_automation_policy_history(path, actor)
            }
            ("GET", path) if automation_policy_path(path).is_some() => {
                self.read_automation_policy(path, actor)
            }
            ("PUT", path) if automation_policy_path(path).is_some() => {
                self.put_automation_policy(path, body, actor)
            }
            ("GET", "/api/v1/publishing/accounts") => self.list_publish_target_accounts(actor),
            ("PUT", path) if publish_account_credential_path(path).is_some() => {
                self.put_publish_account_credential(path, body, actor)
            }
            ("DELETE", path) if publish_account_credential_path(path).is_some() => {
                self.delete_publish_account_credential(path, body, actor)
            }
            ("GET", path) if publish_target_account_history_path(path).is_some() => {
                self.list_publish_target_account_history(path, actor)
            }
            ("GET", path) if publish_target_account_path(path).is_some() => {
                self.read_publish_target_account(path, actor)
            }
            ("PUT", path) if publish_target_account_path(path).is_some() => {
                self.put_publish_target_account(path, body, actor)
            }
            ("GET", "/api/v1/publishing/targets") => self.list_publish_target_profiles(actor),
            ("POST", path) if publish_execution_request_path(path).is_some() => {
                self.request_publish_execution(path, body, actor)
            }
            ("GET", path) if publish_execution_job_path(path).is_some() => {
                self.read_publish_execution_job(path, actor)
            }
            ("GET", path) if publish_execution_history_path(path).is_some() => {
                self.list_publish_execution_jobs(path, actor)
            }
            ("GET", path) if publish_target_profile_history_path(path).is_some() => {
                self.list_publish_target_profile_history(path, actor)
            }
            ("GET", path) if publish_target_profile_path(path).is_some() => {
                self.read_publish_target_profile(path, actor)
            }
            ("PUT", path) if publish_target_profile_path(path).is_some() => {
                self.put_publish_target_profile(path, body, actor)
            }
            ("GET", path) if saved_search_history_path(path).is_some() => {
                self.list_saved_search_history(path, actor)
            }
            ("GET", path) if saved_search_path(path).is_some() => {
                self.read_saved_search(path, actor)
            }
            ("PUT", path) if saved_search_path(path).is_some() => {
                self.put_saved_search(path, body, actor)
            }
            ("GET", path) if saved_search_list_path(path).is_some() => {
                self.list_saved_searches(path, actor)
            }
            ("GET", path) if daypart_block_history_path(path).is_some() => {
                self.list_daypart_block_history(path, actor)
            }
            ("GET", path) if daypart_block_path(path).is_some() => {
                self.read_daypart_block(path, actor)
            }
            ("PUT", path) if daypart_block_path(path).is_some() => {
                self.put_daypart_block(path, body, actor)
            }
            ("GET", path) if daypart_block_list_path(path).is_some() => {
                self.list_daypart_blocks(path, actor)
            }
            ("GET", path) if recurring_rule_history_path(path).is_some() => {
                self.list_recurring_rule_history(path, actor)
            }
            ("GET", path) if recurring_rule_path(path).is_some() => {
                self.read_recurring_rule(path, actor)
            }
            ("PUT", path) if recurring_rule_path(path).is_some() => {
                self.put_recurring_rule(path, body, actor)
            }
            ("GET", path) if recurring_rule_list_path(path).is_some() => {
                self.list_recurring_rules(path, actor)
            }
            ("GET", path) if schedule_materialization_history_path(path).is_some() => {
                self.list_schedule_materialization_history(path, body, actor)
            }
            ("POST", path) if schedule_materialization_path(path).is_some() => {
                self.materialize_scheduled_channel(path, body, actor)
            }
            (_, path) if is_schedule_materialization_endpoint_shape(path) => error_response(
                405,
                "method_not_allowed",
                "Schedule materialization supports POST, and materialization history supports GET.",
            ),
            ("GET" | "PUT" | "POST", path) if path.starts_with("/api/v1/scheduling/") => {
                error_response(
                    400,
                    "invalid_path",
                    "The scheduling automation path or bounded query is invalid.",
                )
            }
            (_, path) if path.starts_with("/api/v1/scheduling/") => error_response(
                405,
                "method_not_allowed",
                "Scheduling automation supports only its documented GET, PUT, and POST operations.",
            ),
            ("GET", path) if catalog_asset_history_path(path).is_some() => {
                self.list_catalog_asset_history(path, actor)
            }
            ("POST", path) if catalog_asset_action_path(path, "publish").is_some() => {
                self.transition_catalog_asset(path, body, actor, CatalogEditorialState::Published)
            }
            ("POST", path) if catalog_asset_action_path(path, "withdraw").is_some() => {
                self.transition_catalog_asset(path, body, actor, CatalogEditorialState::Withdrawn)
            }
            ("GET", path) if catalog_asset_path(path).is_some() => {
                self.read_catalog_asset(path, actor)
            }
            ("PUT", path) if catalog_asset_path(path).is_some() => {
                self.put_catalog_asset(path, body, actor)
            }
            ("GET", path) if catalog_asset_list_path(path).is_some() => {
                self.list_catalog_assets(path, actor)
            }
            ("GET" | "PUT", path) if path.starts_with("/api/v1/catalog/") => error_response(
                400,
                "invalid_path",
                "The catalog path or bounded query is invalid.",
            ),
            (_, path) if path.starts_with("/api/v1/catalog/") => error_response(
                405,
                "method_not_allowed",
                "The catalog supports only its documented GET, PUT, and POST operations.",
            ),
            ("GET", "/api/v1/custom-metadata/schemas") => self.list_custom_metadata_schemas(actor),
            ("GET", path) if custom_metadata_schema_history_path(path).is_some() => {
                self.list_custom_metadata_schema_history(path, actor)
            }
            ("GET", path) if custom_metadata_schema_path(path).is_some() => {
                self.read_custom_metadata_schema(path, actor)
            }
            ("PUT", path) if custom_metadata_schema_path(path).is_some() => {
                self.put_custom_metadata_schema(path, body, actor)
            }
            ("GET", path) if asset_metadata_record_history_path(path).is_some() => {
                self.list_asset_metadata_record_history(path, actor)
            }
            ("GET", path) if asset_metadata_record_path(path).is_some() => {
                self.read_asset_metadata_record(path, actor)
            }
            ("PUT", path) if asset_metadata_record_path(path).is_some() => {
                self.put_asset_metadata_record(path, body, actor)
            }
            ("GET", path) if asset_metadata_records_path(path).is_some() => {
                self.list_asset_metadata_records(path, actor)
            }
            ("POST", "/api/v1/scheduled-recordings") => {
                self.create_scheduled_recording(body, actor)
            }
            ("GET", path) if path.starts_with("/api/v1/scheduled-recordings/candidates") => {
                self.list_scheduled_recording_candidates(path, actor)
            }
            ("GET", path) if scheduled_recording_history_path(path).is_some() => {
                self.list_scheduled_recording_history(path, actor)
            }
            ("GET", path) if scheduled_recording_list_path(path).is_some() => {
                self.list_scheduled_recordings(path, actor)
            }
            ("GET", path) if scheduled_recording_path(path).is_some() => {
                self.read_scheduled_recording(path, actor)
            }
            ("POST", path) if scheduled_recording_action_path(path, "arm").is_some() => {
                self.arm_scheduled_recording(path, body, actor)
            }
            ("POST", path) if scheduled_recording_action_path(path, "cancel").is_some() => {
                self.cancel_scheduled_recording(path, body, actor)
            }
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
                if path.starts_with("/api/v1/channels/")
                    && path.ends_with("/receiver-receipts") =>
            {
                self.list_receiver_receipts(path)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/transport-proof") =>
            {
                self.read_transport_proof(path, actor)
            }
            ("POST", path)
                if path.starts_with("/api/v1/channels/")
                    && path.ends_with("/transport-proof/jobs") =>
            {
                self.request_transport_proof_job(path, body, actor)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/")
                    && path.contains("/transport-proof/jobs/") =>
            {
                self.read_transport_proof_job(path, actor)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/transport-target") =>
            {
                self.read_transport_target(path, actor)
            }
            ("PUT", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/transport-target") =>
            {
                self.put_transport_target(path, body, actor)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/fallback-slate") =>
            {
                self.read_fallback_slate(path)
            }
            ("PUT", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/fallback-slate") =>
            {
                self.put_fallback_slate(path, body, actor)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/caption-policy") =>
            {
                self.read_caption_policy(path)
            }
            ("PUT", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/caption-policy") =>
            {
                self.put_caption_policy(path, body, actor)
            }
            ("GET", path) if asset_caption_tracks_path(path).is_some() => {
                self.list_asset_caption_tracks(path)
            }
            ("GET", path)
                if path.starts_with("/api/v1/assets/") && path.contains("/caption-tracks/") =>
            {
                self.read_asset_caption_track(path)
            }
            ("PUT", path)
                if path.starts_with("/api/v1/assets/") && path.contains("/caption-tracks/") =>
            {
                self.put_asset_caption_track(path, body, actor)
            }
            ("POST", path)
                if path.starts_with("/api/v1/channels/")
                    && path.ends_with("/receiver-receipts") =>
            {
                self.create_receiver_receipt(path, body, actor)
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
                if path.starts_with("/api/v1/channels/") && path.ends_with("/caption-proof") =>
            {
                self.request_channel_caption_proof(path, body)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/") && path.ends_with("/caption-proof") =>
            {
                self.channel_caption_proof(path)
            }
            ("GET", path)
                if path.starts_with("/api/v1/channels/")
                    && path.contains("/caption-proof/jobs/") =>
            {
                self.channel_caption_proof_job(path)
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
            ("PUT", "/api/v1/schedule/items") => self.put_schedule_item(body, actor),
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
            ("GET", path)
                if path.starts_with("/api/v1/assets/") && path.ends_with("/lifecycle") =>
            {
                self.read_asset_lifecycle(path, actor)
            }
            ("GET", path)
                if path.starts_with("/api/v1/assets/") && path.ends_with("/thumbnail") =>
            {
                self.read_asset_thumbnail(path, actor)
            }
            ("PUT", path)
                if path.starts_with("/api/v1/assets/") && path.ends_with("/retention") =>
            {
                self.put_asset_retention(path, body, actor)
            }
            ("POST", path) if path.starts_with("/api/v1/assets/") && path.ends_with("/relink") => {
                self.relink_missing_asset(path, body, actor)
            }
            ("POST", path)
                if path.starts_with("/api/v1/assets/") && path.ends_with("/replacements") =>
            {
                self.create_asset_replacement(path, body, actor)
            }
            ("GET", path) if path.starts_with("/api/v1/assets/") => self.read_media_asset(path),
            _ => error_response(404, "not_found", "The requested route does not exist."),
        }
    }

    fn read_ott_catalog(&self) -> ApiResponse {
        match self.storage.read_ott_catalog_page() {
            Ok(Some(page)) => json_response(
                200,
                serde_json::to_vec(&page).expect("serializing OTT catalog cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "ott_catalog_unavailable",
                "No published OTT catalog is available.",
            ),
            Err(_) => error_response(
                500,
                "ott_storage_error",
                "The OTT catalog authority is temporarily unavailable.",
            ),
        }
    }

    fn issue_ott_session(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(request) = parse_body::<PlaybackSessionIssueRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An OTT playback-session request body is required.",
            );
        };
        if let Err(message) = station_ott_runtime::validate_issue_request(&request) {
            return error_response(422, "validation_failed", &message);
        }
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.issue_ott_playback_session(&request, now) {
            Ok(response) => json_response(
                200,
                serde_json::to_vec(&response).expect("serializing OTT session cannot fail"),
            ),
            Err(error) => ott_session_error(&error),
        }
    }

    fn collect_ott_beacon(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(beacon) = parse_body::<ClientPlaybackBeacon>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An OTT playback beacon body is required.",
            );
        };
        if let Err(message) = station_ott_runtime::validate_beacon_input(&beacon) {
            return error_response(422, "validation_failed", &message);
        }
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.collect_ott_playback_beacon(&beacon, now) {
            Ok(response) => json_response(
                202,
                serde_json::to_vec(&response).expect("serializing OTT beacon receipt cannot fail"),
            ),
            Err(error) => ott_beacon_error(&error),
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

    fn read_asrun_page(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_automation_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading as-run authority requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some((channel_id, cursor, limit)) = asrun_page_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The as-run path or bounded query is invalid.",
            );
        };
        let actor_id = actor
            .expect("role predicate retained actor")
            .account_id()
            .as_str();
        let Ok(now) = system_time_millis() else {
            return error_response(
                500,
                "clock_error",
                "The server clock is unavailable for as-run auditing.",
            );
        };
        match self
            .storage
            .page_asrun_projection(channel_id, cursor, limit)
        {
            Ok(Some(page)) => {
                let body = serde_json::to_vec(&page).expect("serializing as-run page cannot fail");
                if self
                    .storage
                    .audit_asrun_access(channel_id, actor_id, "read", now)
                    .is_err()
                {
                    return error_response(
                        500,
                        "storage_error",
                        "The authoritative as-run access audit could not be recorded.",
                    );
                }
                json_response(200, body)
            }
            Ok(None) => error_response(
                404,
                "asrun_unavailable",
                "No authoritative as-run projection is available for this channel.",
            ),
            Err(_) => error_response(
                422,
                "invalid_query",
                "The as-run page query is invalid or exceeds its bound.",
            ),
        }
    }

    fn export_asrun(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_automation_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Exporting as-run authority requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some((channel_id, format)) = asrun_export_path(path) else {
            return error_response(400, "invalid_path", "The as-run export path is invalid.");
        };
        let action = if format == "csv" {
            "export_csv"
        } else {
            "export_xmltv"
        };
        let actor_id = actor
            .expect("role predicate retained actor")
            .account_id()
            .as_str();
        let Ok(now) = system_time_millis() else {
            return error_response(
                500,
                "clock_error",
                "The server clock is unavailable for as-run auditing.",
            );
        };
        let projection = match self.storage.read_asrun_projection(channel_id) {
            Ok(Some(value)) => value,
            Ok(None) => {
                return error_response(
                    404,
                    "asrun_unavailable",
                    "No authoritative as-run projection is available for this channel.",
                );
            }
            Err(_) => {
                return error_response(
                    500,
                    "storage_error",
                    "The authoritative as-run projection could not be read.",
                );
            }
        };
        let rendered = match format {
            "csv" => {
                render_csv(&projection).map(|body| ("text/csv; charset=utf-8", "as-run.csv", body))
            }
            "xmltv" => render_xmltv(&projection, "TownLight Station")
                .map(|body| ("application/xml; charset=utf-8", "as-run.xml", body)),
            _ => unreachable!(),
        };
        match rendered {
            Ok((content_type, filename, body)) => {
                if self
                    .storage
                    .audit_asrun_access(channel_id, actor_id, action, now)
                    .is_err()
                {
                    return error_response(
                        500,
                        "storage_error",
                        "The authoritative as-run access audit could not be recorded.",
                    );
                }
                attachment_response(200, content_type, filename.into(), body.into_bytes())
            }
            Err(_) => error_response(
                422,
                "export_unavailable",
                "The authoritative as-run export exceeds its safety bounds.",
            ),
        }
    }

    fn list_automation_adapters(&self, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_automation_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading automation authority requires an enabled operator account.",
            );
        }
        match self.storage.list_automation_adapter_descriptors() {
            Ok(adapters) => json_response(
                200,
                serde_json::to_vec(&adapters).expect("serializing automation adapters cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_automation_policies(&self, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_automation_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading automation authority requires an enabled operator account.",
            );
        }
        match self.storage.list_automation_feature_policies() {
            Ok(policies) => json_response(
                200,
                serde_json::to_vec(&policies).expect("serializing automation policies cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_automation_execution_job(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_automation_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading automation execution status requires an enabled operator account.",
            );
        }
        let Some(job_id) = automation_execution_job_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The automation execution job identity is invalid.",
            );
        };
        match self.storage.read_automation_execution_job(job_id) {
            Ok(Some(record)) => json_response(
                200,
                serde_json::to_vec(&automation_execution_job_status_response(&record))
                    .expect("serializing automation execution status cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "automation_execution_job_not_found",
                "The automation execution job does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_automation_policy(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_automation_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading automation authority requires an enabled operator account.",
            );
        }
        let Some(policy_id) = automation_policy_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The automation policy identity is invalid.",
            );
        };
        match self.storage.read_automation_feature_policy(policy_id) {
            Ok(Some(policy)) => json_response(
                200,
                serde_json::to_vec(&policy).expect("serializing automation policy cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "automation_policy_not_found",
                "The automation policy does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_automation_policy_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_automation_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading automation authority requires an enabled operator account.",
            );
        }
        let Some((policy_id, limit)) = automation_policy_history_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The automation policy history path is invalid.",
            );
        };
        match self
            .storage
            .list_automation_feature_policy_history(policy_id, limit)
        {
            Ok(history) => {
                let response = history
                    .into_iter()
                    .map(|entry| AutomationFeaturePolicyHistoryResponse {
                        policy: entry.policy,
                        changed_by: entry.changed_by,
                        occurred_at_unix_ms: entry.occurred_at_unix_ms,
                    })
                    .collect::<Vec<_>>();
                json_response(
                    200,
                    serde_json::to_vec(&response)
                        .expect("serializing automation policy history cannot fail"),
                )
            }
            Err(message) if message.contains("unbounded") || message.contains("invalid") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_automation_policy_status(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_automation_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading automation authority requires an enabled operator account.",
            );
        }
        let Some(policy_id) = automation_policy_status_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The automation policy identity is invalid.",
            );
        };
        let policy = match self.storage.read_automation_feature_policy(policy_id) {
            Ok(Some(policy)) => policy,
            Ok(None) => {
                return error_response(
                    404,
                    "automation_policy_not_found",
                    "The automation policy does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let candidate = match self.current_automation_candidate() {
            Ok(candidate) => candidate,
            Err(message) => return error_response(409, "candidate_not_verified", &message),
        };
        let adapters = match self.storage.list_automation_adapter_descriptors() {
            Ok(adapters) => adapters,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let evidence = match self
            .storage
            .list_automation_connection_test_evidence(policy_id, 128)
        {
            Ok(evidence) => evidence.into_iter().next(),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let status = derive_feature_automation_status(
            &policy,
            &adapters,
            evidence.as_ref(),
            &candidate,
            now,
        );
        json_response(
            200,
            serde_json::to_vec(&AutomationFeaturePolicyStatusResponse {
                policy,
                status,
                evidence,
            })
            .expect("serializing automation policy status cannot fail"),
        )
    }

    fn put_automation_policy(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(policy_id) = automation_policy_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The automation policy identity is invalid.",
            );
        };
        let Some(actor) = actor.filter(|account| {
            account.enabled()
                && account
                    .roles()
                    .contains(&station_auth::OperatorRole::SetupAdmin)
        }) else {
            return error_response(
                403,
                "setup_admin_required",
                "Changing automation policy requires the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<PutAutomationFeaturePolicyRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete automation policy body is required.",
            );
        };
        let revision = match request.expected_revision {
            Some(revision) => match revision.checked_add(1) {
                Some(next) => next,
                None => {
                    return error_response(
                        422,
                        "validation_failed",
                        "The expected revision is invalid.",
                    );
                }
            },
            None => 1,
        };
        match self.storage.list_automation_feature_policies() {
            Ok(policies)
                if policies.iter().any(|policy| {
                    policy.feature == request.feature && policy.policy_id != policy_id
                }) =>
            {
                return error_response(
                    409,
                    "feature_conflict",
                    "Another policy already owns this automation feature.",
                );
            }
            Ok(_) => {}
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let policy = AutomationFeaturePolicy {
            policy_id: policy_id.into(),
            feature: request.feature,
            operator_selected_adapter_id: request.operator_selected_adapter_id,
            adaptive_default: request.adaptive_default,
            configuration_hash: request.configuration_hash,
            revision,
        };
        match self.storage.write_automation_feature_policy(
            &policy,
            request.expected_revision,
            actor.account_id().as_str(),
            now,
        ) {
            Ok(()) => json_response(
                if revision == 1 { 201 } else { 200 },
                serde_json::to_vec(&policy).expect("serializing automation policy cannot fail"),
            ),
            Err(message) if message.contains("revision conflict") => {
                error_response(409, "revision_conflict", &message)
            }
            Err(message)
                if message.contains("missing")
                    || message.contains("unsupported")
                    || message.contains("invalid") =>
            {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    pub fn record_trusted_automation_connection_test_evidence(
        &self,
        observation: TrustedAutomationConnectionTestObservation,
    ) -> Result<(), String> {
        let candidate = self.current_automation_candidate()?;
        let now = system_time_millis()?;
        let expires_at_unix_ms = now
            .checked_add(AUTOMATION_CONNECTION_TEST_TTL_MS)
            .ok_or_else(|| "automation connection evidence expiry overflowed".to_string())?;
        let evidence = AutomationConnectionTestEvidence {
            evidence_id: observation.evidence_id,
            policy_id: observation.policy_id,
            adapter_id: observation.adapter_id,
            policy_revision: observation.policy_revision,
            adapter_revision: observation.adapter_revision,
            configuration_hash: observation.configuration_hash,
            candidate,
            outcome: observation.outcome,
            tested_at_unix_ms: now,
            expires_at_unix_ms,
            detail: observation.detail,
        };
        self.storage
            .append_automation_connection_test_evidence(&evidence, &evidence.candidate, now)
    }

    fn current_automation_candidate(&self) -> Result<AutomationCandidateIdentity, String> {
        let station_box = self.station_box_profile_uncached()?;
        let source_commit = station_box
            .source_commit
            .filter(|_| station_box.release.candidate_verified)
            .ok_or_else(|| {
                "A verified installed candidate is required for automation evidence.".to_string()
            })?;
        let candidate = AutomationCandidateIdentity {
            version: station_box.product_version,
            source_commit,
        };
        candidate.validate()?;
        Ok(candidate)
    }

    fn list_publish_target_accounts(&self, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_publishing_reader(actor) {
            return publishing_reader_required();
        }
        match self.storage.list_publish_target_accounts() {
            Ok(accounts) => json_response(
                200,
                serde_json::to_vec(&accounts).expect("serializing publish accounts cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_publish_target_account(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_publishing_reader(actor) {
            return publishing_reader_required();
        }
        let Some(account_id) = publish_target_account_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publish target account identity is invalid.",
            );
        };
        match self.storage.read_publish_target_account(account_id) {
            Ok(Some(account)) => json_response(
                200,
                serde_json::to_vec(&account).expect("serializing publish account cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "publish_target_account_not_found",
                "The publish target account does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_publish_target_account_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_publishing_reader(actor) {
            return publishing_reader_required();
        }
        let Some((account_id, limit)) = publish_target_account_history_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publish target account history path is invalid.",
            );
        };
        match self
            .storage
            .list_publish_target_account_history(account_id, limit)
        {
            Ok(history) => json_response(
                200,
                serde_json::to_vec(
                    &history
                        .into_iter()
                        .map(|entry| PublishTargetAccountHistoryResponse {
                            account: entry.account,
                            changed_by: entry.changed_by,
                            occurred_at_unix_ms: entry.occurred_at_unix_ms,
                        })
                        .collect::<Vec<_>>(),
                )
                .expect("serializing publish account history cannot fail"),
            ),
            Err(message) if message.contains("invalid") || message.contains("unbounded") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_publish_target_account(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(account_id) = publish_target_account_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publish target account identity is invalid.",
            );
        };
        let Some(actor) = actor.filter(|account| account.enabled() && is_setup_admin(account))
        else {
            return error_response(
                403,
                "setup_admin_required",
                "Changing a publish target account requires the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<PutPublishTargetAccountRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete publish target account body is required.",
            );
        };
        let revision = match next_authority_revision(request.expected_revision) {
            Ok(revision) => revision,
            Err(response) => return response,
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let account = PublishTargetAccount {
            account_id: account_id.into(),
            kind: request.kind,
            label: request.label,
            enabled: request.enabled,
            credential_configured: self
                .storage
                .read_publish_target_account(account_id)
                .ok()
                .flatten()
                .is_some_and(|current| current.credential_configured),
            revision,
        };
        match self.storage.write_publish_target_account(
            &account,
            request.expected_revision,
            actor.account_id().as_str(),
            now,
        ) {
            Ok(()) => json_response(
                if revision == 1 { 201 } else { 200 },
                serde_json::to_vec(&account).expect("serializing publish account cannot fail"),
            ),
            Err(error) => publishing_write_error_response(error),
        }
    }

    fn put_publish_account_credential(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(account_id) = publish_account_credential_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publishing account identity is invalid.",
            );
        };
        let Some(actor) = actor.filter(|account| account.enabled() && is_setup_admin(account))
        else {
            return error_response(
                403,
                "setup_admin_required",
                "Publishing credentials require the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<PutPublishAccountCredentialRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A bounded write-only credential body is required.",
            );
        };
        if request.credential.len() < 8
            || request.credential.len() > 16_384
            || request
                .credential
                .chars()
                .any(|character| character == '\0')
        {
            return error_response(
                422,
                "validation_failed",
                "The publishing credential is invalid or oversized.",
            );
        }
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.provision_publish_account_credential(
            account_id,
            request.expected_account_revision,
            request.credential.as_bytes(),
            actor.account_id().as_str(),
            now,
        ) {
            Ok(_) => match self.storage.read_publish_target_account(account_id) {
                Ok(Some(account)) => json_response(
                    200,
                    serde_json::to_vec(&account)
                        .expect("serializing redacted publish account cannot fail"),
                ),
                Ok(None) => error_response(
                    404,
                    "publish_target_account_not_found",
                    "The publishing account does not exist.",
                ),
                Err(message) => error_response(500, "storage_error", &message),
            },
            Err(message) if message.contains("changed") => {
                error_response(409, "revision_conflict", &message)
            }
            Err(message) if message.contains("missing") => {
                error_response(404, "publish_target_account_not_found", &message)
            }
            Err(message) if message.contains("invalid") || message.contains("oversized") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "credential_provisioning_failed", &message),
        }
    }

    fn delete_publish_account_credential(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(account_id) = publish_account_credential_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publishing account identity is invalid.",
            );
        };
        let Some(actor) = actor.filter(|account| account.enabled() && is_setup_admin(account))
        else {
            return error_response(
                403,
                "setup_admin_required",
                "Publishing credentials require the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<DeletePublishAccountCredentialRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "The expected account revision is required.",
            );
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.delete_publish_account_credential(
            account_id,
            request.expected_account_revision,
            actor.account_id().as_str(),
            now,
        ) {
            Ok(_) => match self.storage.read_publish_target_account(account_id) {
                Ok(Some(account)) => json_response(
                    200,
                    serde_json::to_vec(&account)
                        .expect("serializing redacted publish account cannot fail"),
                ),
                Ok(None) => error_response(
                    404,
                    "publish_target_account_not_found",
                    "The publishing account does not exist.",
                ),
                Err(message) => error_response(500, "storage_error", &message),
            },
            Err(message) if message.contains("changed") => {
                error_response(409, "revision_conflict", &message)
            }
            Err(message) if message.contains("missing") => {
                error_response(404, "publish_target_account_not_found", &message)
            }
            Err(message) if message.contains("not configured") => {
                error_response(409, "credential_not_configured", &message)
            }
            Err(message) => error_response(500, "credential_deletion_failed", &message),
        }
    }

    fn list_publish_target_profiles(&self, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_publishing_reader(actor) {
            return publishing_reader_required();
        }
        match self.storage.list_publish_target_profiles() {
            Ok(targets) => json_response(
                200,
                serde_json::to_vec(&targets).expect("serializing publish targets cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_publish_target_profile(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_publishing_reader(actor) {
            return publishing_reader_required();
        }
        let Some(target_id) = publish_target_profile_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publish target identity is invalid.",
            );
        };
        match self.storage.read_publish_target_profile(target_id) {
            Ok(Some(target)) => json_response(
                200,
                serde_json::to_vec(&target).expect("serializing publish target cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "publish_target_not_found",
                "The publish target does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_publish_target_profile_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_publishing_reader(actor) {
            return publishing_reader_required();
        }
        let Some((target_id, limit)) = publish_target_profile_history_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publish target history path is invalid.",
            );
        };
        match self
            .storage
            .list_publish_target_profile_history(target_id, limit)
        {
            Ok(history) => json_response(
                200,
                serde_json::to_vec(
                    &history
                        .into_iter()
                        .map(|entry| PublishTargetProfileHistoryResponse {
                            target: entry.target,
                            changed_by: entry.changed_by,
                            occurred_at_unix_ms: entry.occurred_at_unix_ms,
                        })
                        .collect::<Vec<_>>(),
                )
                .expect("serializing publish target history cannot fail"),
            ),
            Err(message) if message.contains("invalid") || message.contains("unbounded") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_publish_target_profile(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(target_id) = publish_target_profile_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publish target identity is invalid.",
            );
        };
        let Some(actor) = actor.filter(|account| account.enabled() && is_setup_admin(account))
        else {
            return error_response(
                403,
                "setup_admin_required",
                "Changing a publish target requires the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<PutPublishTargetProfileRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete publish target body is required.",
            );
        };
        let revision = match next_authority_revision(request.expected_revision) {
            Ok(revision) => revision,
            Err(response) => return response,
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        if request.configuration.kind() != request.kind {
            return error_response(
                422,
                "validation_failed",
                "The execution configuration kind must match the publish target kind.",
            );
        }
        if let Err(message) = request.configuration.validate() {
            return error_response(422, "validation_failed", &message);
        }
        let configuration_json = match serde_json::to_vec(&request.configuration) {
            Ok(value) => value,
            Err(error) => return error_response(500, "serialization_error", &error.to_string()),
        };
        let configuration_hash = format!("{:x}", Sha256::digest(&configuration_json));
        let target = PublishTargetProfile {
            target_id: target_id.into(),
            account_id: request.account_id,
            kind: request.kind,
            label: request.label,
            enabled: request.enabled,
            configuration_hash,
            revision,
        };
        match self
            .storage
            .write_publish_target_profile_with_execution_config(
                &target,
                request.expected_revision,
                &request.configuration,
                actor.account_id().as_str(),
                now,
            ) {
            Ok(()) => json_response(
                if revision == 1 { 201 } else { 200 },
                serde_json::to_vec(&target).expect("serializing publish target cannot fail"),
            ),
            Err(error) => publishing_write_error_response(error),
        }
    }

    fn request_publish_execution(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if body.is_some_and(|body| !body.is_empty()) {
            return error_response(
                400,
                "body_not_allowed",
                "Publish execution requests are bodyless.",
            );
        }
        let Some(actor) = actor.filter(|account| {
            account.enabled()
                && (is_setup_admin(account)
                    || account
                        .roles()
                        .contains(&station_auth::OperatorRole::PublishOperator))
        }) else {
            return error_response(
                403,
                "publish_operator_required",
                "Requesting publication requires the Publish Operator or Setup Admin role.",
            );
        };
        let Some((target_id, asset_id)) = publish_execution_request_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publish execution path is invalid.",
            );
        };
        let data_directory = match self.database_path.parent() {
            Some(value) => value,
            None => {
                return error_response(
                    500,
                    "candidate_authority_unavailable",
                    "The station data directory is unavailable.",
                );
            }
        };
        let fence = match acquire_install_authority_fence(data_directory) {
            Ok(value) => value,
            Err(_) => {
                return error_response(
                    409,
                    "candidate_authority_busy",
                    "Candidate activation is in progress; wait before requesting publication.",
                );
            }
        };
        if fence.was_abandoned() {
            return error_response(
                409,
                "candidate_authority_recovery_required",
                "Candidate authority requires recovery before publication.",
            );
        }
        let candidate =
            match read_verified_install_authority(data_directory, &self.application_directory) {
                Ok(value) => value,
                Err(_) => {
                    return error_response(
                        409,
                        "candidate_not_verified",
                        "A verified installed candidate is required before publication.",
                    );
                }
            };
        let target = match self.storage.read_publish_target_profile(target_id) {
            Ok(Some(value)) => value,
            Ok(None) => {
                return error_response(
                    404,
                    "publish_target_not_found",
                    "The publish target does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let account = match self.storage.read_publish_target_account(&target.account_id) {
            Ok(Some(value)) => value,
            Ok(None) => {
                return error_response(
                    409,
                    "stale_authority",
                    "The publish account does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let credential_version = match self
            .storage
            .current_publish_account_credential_version(&account.account_id, account.revision)
        {
            Ok(Some(value)) => value,
            Ok(None) => {
                return error_response(
                    409,
                    "credential_unavailable",
                    "The publish account credential is not configured.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let asset = match self.storage.read_media_asset(asset_id) {
            Ok(Some(value)) if value.readiness == AssetReadiness::Ready => value,
            Ok(Some(_)) => {
                return error_response(
                    409,
                    "media_not_ready",
                    "The selected media is not ready for publication.",
                );
            }
            Ok(None) => {
                return error_response(
                    404,
                    "media_not_found",
                    "The selected media does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let (media_bytes, media_sha256) = match hash_publish_file(Path::new(&asset.media_path)) {
            Ok(value) => value,
            Err(message) => return error_response(409, "media_unavailable", &message),
        };
        if media_sha256 != asset.asset_id {
            return error_response(
                409,
                "media_changed",
                "The selected media no longer matches its catalog identity.",
            );
        }
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let job_id = match secure_canonical_uuid() {
            Ok(value) => value,
            Err(message) => return error_response(500, "entropy_unavailable", &message),
        };
        let job = PublishExecutionJob {
            job_id,
            account_id: account.account_id,
            account_revision: account.revision,
            target_id: target.target_id,
            target_revision: target.revision,
            target_kind: target.kind,
            configuration_hash: target.configuration_hash,
            credential_version,
            candidate_version: candidate.version().into(),
            source_commit: candidate.source_commit().into(),
            install_generation: candidate.install_generation(),
            asset_id: asset.asset_id,
            media_sha256,
            media_bytes,
            media_duration_ms: asset.duration_ms,
            requested_by: actor.account_id().as_str().into(),
            created_at_unix_ms: now,
        };
        match self.storage.enqueue_publish_execution_job(&job) {
            Ok(()) => self.publish_execution_job_response(&job.target_id, job.job_id.as_str(), 202),
            Err(message) if message.contains("stale") || message.contains("missing") => {
                error_response(409, "stale_authority", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_publish_execution_job(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_publishing_reader(actor) {
            return publishing_reader_required();
        }
        let Some((target_id, job_id)) = publish_execution_job_path(path) else {
            return error_response(400, "invalid_path", "The publish job path is invalid.");
        };
        self.publish_execution_job_response(target_id, job_id, 200)
    }

    fn list_publish_execution_jobs(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_publishing_reader(actor) {
            return publishing_reader_required();
        }
        let Some((target_id, limit)) = publish_execution_history_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The publish job history path is invalid.",
            );
        };
        match self.storage.list_publish_execution_jobs(target_id, limit) {
            Ok(records) => json_response(
                200,
                serde_json::to_vec(
                    &records
                        .into_iter()
                        .map(|record| publish_execution_job_response(record, Vec::new()))
                        .collect::<Vec<_>>(),
                )
                .expect("serializing publish job history cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn publish_execution_job_response(
        &self,
        target_id: &str,
        job_id: &str,
        status: u16,
    ) -> ApiResponse {
        match self.storage.read_publish_execution_job(job_id) {
            Ok(Some(record)) if record.job.target_id == target_id => {
                match self.storage.list_publish_execution_receipts(job_id, 128) {
                    Ok(receipts) => json_response(
                        status,
                        serde_json::to_vec(&publish_execution_job_response(record, receipts))
                            .expect("serializing publish job cannot fail"),
                    ),
                    Err(message) => error_response(500, "storage_error", &message),
                }
            }
            Ok(Some(_)) | Ok(None) => error_response(
                404,
                "publish_job_not_found",
                "The publish job does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    pub fn record_trusted_publish_connection_test_evidence(
        &self,
        observation: TrustedPublishConnectionTestObservation,
    ) -> Result<(), String> {
        let candidate = self.current_publish_candidate()?;
        let now = system_time_millis()?;
        let expires_at_unix_ms = now
            .checked_add(PUBLISH_CONNECTION_TEST_TTL_MS)
            .ok_or_else(|| "publish connection evidence expiry overflowed".to_string())?;
        let target = self
            .storage
            .read_publish_target_profile(&observation.target_id)?
            .ok_or_else(|| "publish connection evidence target does not exist".to_string())?;
        let account = self
            .storage
            .read_publish_target_account(&target.account_id)?
            .ok_or_else(|| "publish connection evidence account does not exist".to_string())?;
        let evidence = PublishConnectionTestEvidence {
            evidence_id: observation.evidence_id,
            target_id: target.target_id,
            account_revision: account.revision,
            target_revision: target.revision,
            configuration_hash: target.configuration_hash,
            candidate,
            outcome: observation.outcome,
            tested_at_unix_ms: now,
            expires_at_unix_ms,
            detail: observation.detail,
        };
        self.storage
            .append_publish_connection_test_evidence(&evidence, &evidence.candidate, now)
    }

    fn current_publish_candidate(&self) -> Result<PublishCandidateIdentity, String> {
        let station_box = self.station_box_profile_uncached()?;
        let source_commit = station_box
            .source_commit
            .filter(|_| station_box.release.candidate_verified)
            .ok_or_else(|| {
                "A verified installed candidate is required for publish-target evidence."
                    .to_string()
            })?;
        let candidate = PublishCandidateIdentity {
            version: station_box.product_version,
            source_commit,
        };
        candidate.validate()?;
        Ok(candidate)
    }

    fn list_saved_searches(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some(limit) = saved_search_list_path(path) else {
            return scheduling_invalid_path();
        };
        match self.storage.list_saved_searches(limit) {
            Ok(searches) => json_response(
                200,
                serde_json::to_vec(&searches).expect("serializing saved searches cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_saved_search(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some(search_id) = saved_search_path(path) else {
            return scheduling_invalid_path();
        };
        match self.storage.read_saved_search(search_id) {
            Ok(Some(search)) => json_response(
                200,
                serde_json::to_vec(&search).expect("serializing a saved search cannot fail"),
            ),
            Ok(None) => scheduling_not_found("saved_search"),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_saved_search_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some((search_id, limit)) = saved_search_history_path(path) else {
            return scheduling_invalid_path();
        };
        match self.storage.read_saved_search(search_id) {
            Ok(Some(_)) => {}
            Ok(None) => return scheduling_not_found("saved_search"),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self.storage.list_saved_search_history(search_id, limit) {
            Ok(history) => json_response(
                200,
                serde_json::to_vec(
                    &history
                        .into_iter()
                        .map(|entry| SavedSearchHistoryResponse {
                            search: entry.search,
                            changed_by: entry.changed_by,
                            occurred_at_unix_ms: entry.occurred_at_unix_ms,
                        })
                        .collect::<Vec<_>>(),
                )
                .expect("serializing saved search history cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_saved_search(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(search_id) = saved_search_path(path) else {
            return scheduling_invalid_path();
        };
        let Some(actor) = scheduling_writer(actor) else {
            return scheduling_writer_required();
        };
        let Some(request) = parse_body::<PutSavedSearchRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete saved-search body is required.",
            );
        };
        let revision = match scheduling_next_revision(request.expected_revision) {
            Ok(revision) => revision,
            Err(message) => return error_response(422, "validation_failed", &message),
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let current = match self.storage.read_saved_search(search_id) {
            Ok(current) => current,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if current.as_ref().is_some_and(|search| {
            !is_setup_admin(actor) && search.owner_operator_id != actor.account_id().as_str()
        }) {
            return error_response(
                403,
                "saved_search_owner_required",
                "A Meeting Operator may change only a saved search they own.",
            );
        }
        let search = SavedSearch {
            search_id: search_id.into(),
            name: request.name,
            owner_operator_id: current.as_ref().map_or_else(
                || actor.account_id().as_str().into(),
                |value| value.owner_operator_id.clone(),
            ),
            created_at_utc_ms: current
                .as_ref()
                .map_or(now, |value| value.created_at_utc_ms),
            query: request.query,
            revision,
        };
        if let Err(message) = search.validate() {
            return error_response(422, "validation_failed", &message);
        }
        match self.storage.write_saved_search(
            &search,
            request.expected_revision,
            actor.account_id().as_str(),
            now,
        ) {
            Ok(()) => json_response(
                if revision == 1 { 201 } else { 200 },
                serde_json::to_vec(&search).expect("serializing a saved search cannot fail"),
            ),
            Err(error) => scheduling_write_error_response(error, "saved_search"),
        }
    }

    fn list_daypart_blocks(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some((channel_id, limit)) = daypart_block_list_path(path) else {
            return scheduling_invalid_path();
        };
        match self.scheduling_channel_exists(channel_id) {
            Ok(true) => {}
            Ok(false) => return scheduling_not_found("channel"),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self.storage.list_daypart_blocks(channel_id, limit) {
            Ok(blocks) => json_response(
                200,
                serde_json::to_vec(&blocks).expect("serializing daypart blocks cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_daypart_block(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some(block_id) = daypart_block_path(path) else {
            return scheduling_invalid_path();
        };
        match self.storage.read_daypart_block(block_id) {
            Ok(Some(block)) => json_response(
                200,
                serde_json::to_vec(&block).expect("serializing a daypart block cannot fail"),
            ),
            Ok(None) => scheduling_not_found("daypart_block"),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_daypart_block_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some((block_id, limit)) = daypart_block_history_path(path) else {
            return scheduling_invalid_path();
        };
        match self.storage.read_daypart_block(block_id) {
            Ok(Some(_)) => {}
            Ok(None) => return scheduling_not_found("daypart_block"),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self.storage.list_daypart_block_history(block_id, limit) {
            Ok(history) => json_response(
                200,
                serde_json::to_vec(
                    &history
                        .into_iter()
                        .map(|entry| DaypartBlockHistoryResponse {
                            block: entry.block,
                            changed_by: entry.changed_by,
                            occurred_at_unix_ms: entry.occurred_at_unix_ms,
                        })
                        .collect::<Vec<_>>(),
                )
                .expect("serializing daypart block history cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_daypart_block(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(block_id) = daypart_block_path(path) else {
            return scheduling_invalid_path();
        };
        let Some(actor) = scheduling_writer(actor) else {
            return scheduling_writer_required();
        };
        let Some(request) = parse_body::<PutDaypartBlockRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete daypart-block body is required.",
            );
        };
        if !is_canonical_api_uuid(&request.channel_id) {
            return error_response(422, "validation_failed", "The channel identity is invalid.");
        }
        match self.scheduling_channel_exists(&request.channel_id) {
            Ok(true) => {}
            Ok(false) => return scheduling_not_found("channel"),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let revision = match scheduling_next_revision(request.expected_revision) {
            Ok(revision) => revision,
            Err(message) => return error_response(422, "validation_failed", &message),
        };
        let block = DaypartBlock {
            block_id: block_id.into(),
            channel_id: request.channel_id,
            label: request.label,
            starts_local_minute: request.starts_local_minute,
            ends_local_minute: request.ends_local_minute,
            weekday_mask: request.weekday_mask,
            season_starts_on: request.season_starts_on,
            season_ends_on: request.season_ends_on,
            allow_rule_ids: request.allow_rule_ids,
            revision,
        };
        if let Err(message) = block.validate() {
            return error_response(422, "validation_failed", &message);
        }
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.write_daypart_block(
            &block,
            request.expected_revision,
            actor.account_id().as_str(),
            now,
        ) {
            Ok(()) => json_response(
                if revision == 1 { 201 } else { 200 },
                serde_json::to_vec(&block).expect("serializing a daypart block cannot fail"),
            ),
            Err(error) => scheduling_write_error_response(error, "daypart_block"),
        }
    }

    fn list_recurring_rules(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some((channel_id, limit)) = recurring_rule_list_path(path) else {
            return scheduling_invalid_path();
        };
        match self.scheduling_channel_exists(channel_id) {
            Ok(true) => {}
            Ok(false) => return scheduling_not_found("channel"),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self.storage.list_recurring_rules(channel_id, limit) {
            Ok(rules) => json_response(
                200,
                serde_json::to_vec(&rules).expect("serializing recurring rules cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_recurring_rule(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some(rule_id) = recurring_rule_path(path) else {
            return scheduling_invalid_path();
        };
        match self.storage.read_recurring_rule(rule_id) {
            Ok(Some(rule)) => json_response(
                200,
                serde_json::to_vec(&rule).expect("serializing a recurring rule cannot fail"),
            ),
            Ok(None) => scheduling_not_found("recurring_rule"),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_recurring_rule_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some((rule_id, limit)) = recurring_rule_history_path(path) else {
            return scheduling_invalid_path();
        };
        match self.storage.read_recurring_rule(rule_id) {
            Ok(Some(_)) => {}
            Ok(None) => return scheduling_not_found("recurring_rule"),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self.storage.list_recurring_rule_history(rule_id, limit) {
            Ok(history) => json_response(
                200,
                serde_json::to_vec(
                    &history
                        .into_iter()
                        .map(|entry| RecurringRuleHistoryResponse {
                            rule: entry.rule,
                            changed_by: entry.changed_by,
                            occurred_at_unix_ms: entry.occurred_at_unix_ms,
                        })
                        .collect::<Vec<_>>(),
                )
                .expect("serializing recurring rule history cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_recurring_rule(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(rule_id) = recurring_rule_path(path) else {
            return scheduling_invalid_path();
        };
        let Some(actor) = scheduling_writer(actor) else {
            return scheduling_writer_required();
        };
        let Some(request) = parse_body::<PutRecurringRuleRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete recurring-rule body is required.",
            );
        };
        if !is_canonical_api_uuid(&request.channel_id) {
            return error_response(422, "validation_failed", "The channel identity is invalid.");
        }
        match self.scheduling_channel_exists(&request.channel_id) {
            Ok(true) => {}
            Ok(false) => return scheduling_not_found("channel"),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let revision = match scheduling_next_revision(request.expected_revision) {
            Ok(revision) => revision,
            Err(message) => return error_response(422, "validation_failed", &message),
        };
        let rule = RecurringRule {
            rule_id: rule_id.into(),
            channel_id: request.channel_id,
            saved_search_id: request.saved_search_id,
            saved_search_revision: request.saved_search_revision,
            pick_strategy: request.pick_strategy,
            rolling_window_days: request.rolling_window_days,
            avoid_repeat_within_days: request.avoid_repeat_within_days,
            enabled: request.enabled,
            priority: request.priority,
            block_id: request.block_id,
            block_revision: request.block_revision,
            weekday_mask: request.weekday_mask,
            interval_weeks: request.interval_weeks,
            anchor_date: request.anchor_date,
            timezone: request.timezone,
            local_start_minute: request.local_start_minute,
            duration_minutes: request.duration_minutes,
            revision,
        };
        if let Err(message) = rule.validate() {
            return error_response(422, "validation_failed", &message);
        }
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.write_recurring_rule(
            &rule,
            request.expected_revision,
            actor.account_id().as_str(),
            now,
        ) {
            Ok(()) => json_response(
                if revision == 1 { 201 } else { 200 },
                serde_json::to_vec(&rule).expect("serializing a recurring rule cannot fail"),
            ),
            Err(error) => scheduling_write_error_response(error, "recurring_rule"),
        }
    }

    fn scheduling_channel_exists(&self, channel_id: &str) -> Result<bool, String> {
        Ok(self
            .storage
            .list_channels()?
            .iter()
            .any(|channel| channel.channel_id == channel_id))
    }

    fn materialize_scheduled_channel(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(channel_id) = schedule_materialization_path(path) else {
            return scheduling_invalid_path();
        };
        let Some(actor) = scheduling_writer(actor) else {
            return scheduling_writer_required();
        };
        if body.is_some_and(|body| !body.is_empty()) {
            return error_response(
                400,
                "unexpected_body",
                "Schedule materialization does not accept a request body.",
            );
        }
        match self.scheduling_channel_exists(channel_id) {
            Ok(true) => {}
            Ok(false) => return scheduling_not_found("channel"),
            Err(_) => {
                return error_response(
                    500,
                    "storage_error",
                    "Schedule materialization could not validate the channel authority.",
                );
            }
        }

        match self.storage.materialize_scheduled_channel(
            channel_id,
            actor.account_id().as_str(),
            self.source_evidence_authority.as_ref(),
        ) {
            Ok(ScheduleMaterializationApplyOutcome::Applied(run)) => {
                schedule_materialization_success_response(
                    201,
                    ScheduleMaterializationDispositionResponse::Applied,
                    &run,
                )
            }
            Ok(ScheduleMaterializationApplyOutcome::AlreadyApplied(run)) => {
                schedule_materialization_success_response(
                    200,
                    ScheduleMaterializationDispositionResponse::AlreadyApplied,
                    &run,
                )
            }
            Err(error) => schedule_materialization_write_error_response(error),
        }
    }

    fn list_schedule_materialization_history(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return scheduling_reader_required();
        }
        let Some((channel_id, limit, cursor)) = schedule_materialization_history_path(path) else {
            return scheduling_invalid_path();
        };
        if body.is_some_and(|body| !body.is_empty()) {
            return error_response(
                400,
                "unexpected_body",
                "Schedule materialization history does not accept a request body.",
            );
        }
        if cursor
            .as_ref()
            .is_some_and(|cursor| cursor.occurred_at_unix_ms <= 0)
        {
            return error_response(
                422,
                "invalid_materialization_history_cursor",
                "The materialization history cursor is outside the durable timestamp range.",
            );
        }
        match self.scheduling_channel_exists(channel_id) {
            Ok(true) => {}
            Ok(false) => return scheduling_not_found("channel"),
            Err(_) => {
                return error_response(
                    500,
                    "storage_error",
                    "Schedule materialization history could not validate the channel authority.",
                );
            }
        }
        match self
            .storage
            .list_schedule_materialization_history(channel_id, limit, cursor.as_ref())
        {
            Ok(page) => schedule_materialization_history_success_response(
                channel_id,
                page.entries,
                page.next_cursor,
            ),
            Err(error) => schedule_materialization_history_read_error_response(error),
        }
    }

    fn list_catalog_assets(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_catalog_reader(actor) {
            return catalog_reader_required();
        }
        let Some((after_asset_sha256, limit)) = catalog_asset_list_path(path) else {
            return catalog_invalid_path();
        };
        match self
            .storage
            .list_catalog_asset_page(after_asset_sha256, limit)
        {
            Ok(page) => json_response(
                200,
                serde_json::to_vec(&CatalogAssetPageResponse {
                    records: page.records,
                    next_after_asset_sha256: page.next_after_asset_sha256,
                })
                .expect("serializing a catalog page cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_catalog_asset(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_catalog_reader(actor) {
            return catalog_reader_required();
        }
        let Some(asset_sha256) = catalog_asset_path(path) else {
            return catalog_invalid_path();
        };
        match self.storage.read_catalog_asset(asset_sha256) {
            Ok(Some(record)) => json_response(
                200,
                serde_json::to_vec(&record).expect("serializing a catalog asset cannot fail"),
            ),
            Ok(None) => catalog_asset_not_found(),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_catalog_asset_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_catalog_reader(actor) {
            return catalog_reader_required();
        }
        let Some((asset_sha256, limit)) = catalog_asset_history_path(path) else {
            return catalog_invalid_path();
        };
        match self.storage.read_catalog_asset(asset_sha256) {
            Ok(Some(_)) => {}
            Ok(None) => return catalog_asset_not_found(),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self.storage.list_catalog_asset_history(asset_sha256, limit) {
            Ok(history) => {
                let response = history
                    .into_iter()
                    .map(|entry| CatalogAssetHistoryResponse {
                        record: entry.record,
                        changed_by: entry.changed_by,
                        occurred_at_utc_ms: entry.occurred_at_utc_ms,
                    })
                    .collect::<Vec<_>>();
                json_response(
                    200,
                    serde_json::to_vec(&response).expect("serializing catalog history cannot fail"),
                )
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_catalog_asset(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(asset_sha256) = catalog_asset_path(path) else {
            return catalog_invalid_path();
        };
        let Some(actor) = catalog_writer(actor) else {
            return catalog_writer_required();
        };
        let Some(request) = parse_body::<PutCatalogAssetRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete catalog asset body is required.",
            );
        };
        match self.storage.read_media_asset(asset_sha256) {
            Ok(Some(_)) => {}
            Ok(None) => return catalog_media_asset_not_found(),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let current = match self.storage.read_catalog_asset(asset_sha256) {
            Ok(current) => current,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        match (current.as_ref(), request.expected_revision) {
            (None, None) => {}
            (Some(record), Some(expected_revision)) if expected_revision == record.revision => {}
            _ => {
                return catalog_revision_conflict(current.as_ref().map(|record| record.revision));
            }
        }
        let revision = match catalog_next_revision(request.expected_revision) {
            Ok(revision) => revision,
            Err(message) => return error_response(422, "validation_failed", &message),
        };
        let (editorial_state, published_at_utc_ms) = current
            .as_ref()
            .map_or((CatalogEditorialState::Draft, None), |record| {
                (record.editorial_state, record.published_at_utc_ms)
            });
        let record = CatalogAssetRecord {
            asset_sha256: asset_sha256.into(),
            title: request.title,
            meeting_body: request.meeting_body,
            editorial_state,
            published_at_utc_ms,
            revision,
        };
        if let Err(error) = record.validate() {
            return error_response(
                422,
                "validation_failed",
                &format!("The catalog asset is invalid: {error:?}."),
            );
        }
        let occurred_at_utc_ms = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.write_catalog_asset(
            asset_sha256,
            &record,
            request.expected_revision,
            actor.account_id().as_str(),
            occurred_at_utc_ms,
        ) {
            Ok(()) => json_response(
                if current.is_none() { 201 } else { 200 },
                serde_json::to_vec(&record).expect("serializing a catalog asset cannot fail"),
            ),
            Err(error) => catalog_write_error_response(error),
        }
    }

    fn transition_catalog_asset(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
        requested_state: CatalogEditorialState,
    ) -> ApiResponse {
        let action = match requested_state {
            CatalogEditorialState::Published => "publish",
            CatalogEditorialState::Withdrawn => "withdraw",
            CatalogEditorialState::Draft => return catalog_invalid_path(),
        };
        let Some(asset_sha256) = catalog_asset_action_path(path, action) else {
            return catalog_invalid_path();
        };
        let Some(actor) = catalog_writer(actor) else {
            return catalog_writer_required();
        };
        let Some(request) = parse_body::<CatalogAssetTransitionRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A catalog transition body with expected_revision is required.",
            );
        };
        match self.storage.read_media_asset(asset_sha256) {
            Ok(Some(_)) => {}
            Ok(None) => return catalog_media_asset_not_found(),
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let current = match self.storage.read_catalog_asset(asset_sha256) {
            Ok(Some(current)) => current,
            Ok(None) => return catalog_asset_not_found(),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if request.expected_revision != current.revision {
            return catalog_revision_conflict(Some(current.revision));
        }
        match (current.editorial_state, requested_state) {
            (CatalogEditorialState::Draft, CatalogEditorialState::Published)
            | (CatalogEditorialState::Withdrawn, CatalogEditorialState::Published)
            | (CatalogEditorialState::Published, CatalogEditorialState::Withdrawn) => {}
            _ => return catalog_state_conflict(current.editorial_state, requested_state),
        }
        let revision = match catalog_next_revision(Some(request.expected_revision)) {
            Ok(revision) => revision,
            Err(message) => return error_response(422, "validation_failed", &message),
        };
        let occurred_at_utc_ms = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let mut record = current;
        record.editorial_state = requested_state;
        record.revision = revision;
        if record.published_at_utc_ms.is_none() {
            record.published_at_utc_ms = Some(occurred_at_utc_ms);
        }
        match self.storage.write_catalog_asset(
            asset_sha256,
            &record,
            Some(request.expected_revision),
            actor.account_id().as_str(),
            occurred_at_utc_ms,
        ) {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&record).expect("serializing a catalog asset cannot fail"),
            ),
            Err(error) => catalog_write_error_response(error),
        }
    }

    fn list_custom_metadata_schemas(&self, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_metadata_reader(actor) {
            return error_response(
                403,
                "metadata_operator_required",
                "Reading custom metadata requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        match self.storage.list_custom_metadata_schemas() {
            Ok(schemas) => json_response(
                200,
                serde_json::to_vec(&schemas)
                    .expect("serializing custom metadata schemas cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_custom_metadata_schema(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_metadata_reader(actor) {
            return error_response(
                403,
                "metadata_operator_required",
                "Reading custom metadata requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some(schema_id) = custom_metadata_schema_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The metadata schema identity is invalid.",
            );
        };
        match self.storage.read_custom_metadata_schema(schema_id) {
            Ok(Some(schema)) => json_response(
                200,
                serde_json::to_vec(&schema).expect("serializing a metadata schema cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "metadata_schema_not_found",
                "The custom metadata schema does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_custom_metadata_schema_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_metadata_reader(actor) {
            return error_response(
                403,
                "metadata_operator_required",
                "Reading custom metadata requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some((schema_id, limit)) = custom_metadata_schema_history_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The metadata schema history path is invalid.",
            );
        };
        match self.storage.read_custom_metadata_schema(schema_id) {
            Ok(Some(_)) => {}
            Ok(None) => {
                return error_response(
                    404,
                    "metadata_schema_not_found",
                    "The custom metadata schema does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self
            .storage
            .list_custom_metadata_schema_history(schema_id, limit)
        {
            Ok(history) => {
                let response = history
                    .into_iter()
                    .map(|entry| CustomMetadataSchemaHistoryResponse {
                        schema: entry.schema,
                        changed_by: entry.changed_by,
                        occurred_at_unix_ms: entry.occurred_at_unix_ms,
                    })
                    .collect::<Vec<_>>();
                json_response(
                    200,
                    serde_json::to_vec(&response)
                        .expect("serializing custom metadata schema history cannot fail"),
                )
            }
            Err(message) if message.contains("invalid") || message.contains("unbounded") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_custom_metadata_schema(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(schema_id) = custom_metadata_schema_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The metadata schema identity is invalid.",
            );
        };
        let Some(actor) = metadata_writer(actor, false) else {
            return error_response(
                403,
                "setup_admin_required",
                "Changing custom metadata schemas requires the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<PutCustomMetadataSchemaRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A custom metadata schema body is required.",
            );
        };
        let revision = match metadata_next_revision(request.expected_revision) {
            Ok(revision) => revision,
            Err(message) => return error_response(422, "validation_failed", &message),
        };
        let schema = MetadataSchema {
            schema_id: schema_id.into(),
            revision,
            fields: request.fields,
        };
        if let Err(message) = schema.validate() {
            return error_response(422, "validation_failed", &message);
        }
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.write_custom_metadata_schema(
            &schema,
            request.expected_revision,
            actor.account_id().as_str(),
            occurred_at_unix_ms,
        ) {
            Ok(()) => json_response(
                if revision == 1 { 201 } else { 200 },
                serde_json::to_vec(&schema).expect("serializing a metadata schema cannot fail"),
            ),
            Err(CustomMetadataSchemaWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", &message)
            }
            Err(CustomMetadataSchemaWriteError::Conflict(message)) => {
                error_response(409, "revision_conflict", &message)
            }
            Err(CustomMetadataSchemaWriteError::Rebound) => error_response(
                409,
                "metadata_identity_rebound",
                "An existing metadata schema identity cannot be rebound.",
            ),
            Err(CustomMetadataSchemaWriteError::Capacity) => error_response(
                422,
                "metadata_schema_capacity_exceeded",
                "Custom metadata cannot exceed 64 schemas.",
            ),
            Err(CustomMetadataSchemaWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn list_asset_metadata_records(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_metadata_reader(actor) {
            return error_response(
                403,
                "metadata_operator_required",
                "Reading custom metadata requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some(asset_id) = asset_metadata_records_path(path) else {
            return error_response(400, "invalid_path", "The media asset identity is invalid.");
        };
        match self.storage.read_media_asset(asset_id) {
            Ok(Some(_)) => {}
            Ok(None) => {
                return error_response(404, "asset_not_found", "The media asset does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self.storage.list_asset_metadata_records(asset_id) {
            Ok(records) => json_response(
                200,
                serde_json::to_vec(&records)
                    .expect("serializing asset metadata records cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_asset_metadata_record(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_metadata_reader(actor) {
            return error_response(
                403,
                "metadata_operator_required",
                "Reading custom metadata requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some(metadata_id) = asset_metadata_record_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The metadata record identity is invalid.",
            );
        };
        match self.storage.read_asset_metadata_record(metadata_id) {
            Ok(Some(record)) => json_response(
                200,
                serde_json::to_vec(&record)
                    .expect("serializing an asset metadata record cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "metadata_record_not_found",
                "The asset metadata record does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_asset_metadata_record_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if !is_metadata_reader(actor) {
            return error_response(
                403,
                "metadata_operator_required",
                "Reading custom metadata requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some((metadata_id, limit)) = asset_metadata_record_history_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The asset metadata history path is invalid.",
            );
        };
        match self.storage.read_asset_metadata_record(metadata_id) {
            Ok(Some(_)) => {}
            Ok(None) => {
                return error_response(
                    404,
                    "metadata_record_not_found",
                    "The asset metadata record does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        match self
            .storage
            .list_asset_metadata_record_history(metadata_id, limit)
        {
            Ok(history) => {
                let response = history
                    .into_iter()
                    .map(|entry| AssetMetadataRecordHistoryResponse {
                        record: entry.record,
                        changed_by: entry.changed_by,
                        occurred_at_unix_ms: entry.occurred_at_unix_ms,
                    })
                    .collect::<Vec<_>>();
                json_response(
                    200,
                    serde_json::to_vec(&response)
                        .expect("serializing asset metadata history cannot fail"),
                )
            }
            Err(message) if message.contains("invalid") || message.contains("unbounded") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_asset_metadata_record(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(metadata_id) = asset_metadata_record_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The metadata record identity is invalid.",
            );
        };
        let Some(actor) = metadata_writer(actor, true) else {
            return error_response(
                403,
                "metadata_operator_required",
                "Changing asset metadata requires an enabled Setup Admin or Meeting Operator account.",
            );
        };
        let Some(request) = parse_body::<PutAssetMetadataRecordRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An asset metadata record body is required.",
            );
        };
        if !is_lowercase_sha256(&request.asset_id) {
            return error_response(
                422,
                "validation_failed",
                "The asset identity must be a lowercase SHA-256 digest.",
            );
        }
        if !is_canonical_api_uuid(&request.schema_id) {
            return error_response(
                422,
                "validation_failed",
                "The metadata schema identity must be a lowercase canonical UUID.",
            );
        }
        match self.storage.read_media_asset(&request.asset_id) {
            Ok(Some(_)) => {}
            Ok(None) => {
                return error_response(404, "asset_not_found", "The media asset does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let schema = match self.storage.read_custom_metadata_schema(&request.schema_id) {
            Ok(Some(schema)) => schema,
            Ok(None) => {
                return error_response(
                    404,
                    "metadata_schema_not_found",
                    "The custom metadata schema does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        self.write_resolved_asset_metadata_record(metadata_id, request, actor, schema)
    }

    /// Completes an asset-metadata write after API schema resolution. Keeping
    /// this boundary explicit lets the deterministic regression advance the
    /// schema between resolution and persistence; storage must reject that
    /// stale revision inside its own BEGIN IMMEDIATE transaction.
    fn write_resolved_asset_metadata_record(
        &self,
        metadata_id: &str,
        request: PutAssetMetadataRecordRequest,
        actor: &OperatorAccount,
        schema: MetadataSchema,
    ) -> ApiResponse {
        let current = match self.storage.read_asset_metadata_record(metadata_id) {
            Ok(current) => current,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if let Some(current) = &current
            && (current.asset_id != request.asset_id || current.schema_id != request.schema_id)
        {
            return error_response(
                409,
                "metadata_identity_rebound",
                "An existing metadata record cannot be rebound to another asset or schema.",
            );
        }
        let revision = match metadata_next_revision(request.expected_revision) {
            Ok(revision) => revision,
            Err(message) => return error_response(422, "validation_failed", &message),
        };
        let record = AssetMetadataRecord {
            metadata_id: metadata_id.into(),
            asset_id: request.asset_id,
            schema_id: request.schema_id,
            schema_revision: schema.revision,
            revision,
            values: request.values,
        };
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.write_asset_metadata_record(
            &record,
            request.expected_revision,
            actor.account_id().as_str(),
            occurred_at_unix_ms,
        ) {
            Ok(()) => json_response(
                if revision == 1 { 201 } else { 200 },
                serde_json::to_vec(&record)
                    .expect("serializing an asset metadata record cannot fail"),
            ),
            Err(error) => asset_metadata_write_error_response(error),
        }
    }

    fn create_scheduled_recording(
        &self,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(requested_by) = recording_operator(actor) else {
            return error_response(
                403,
                "recording_operator_required",
                "Creating a scheduled recording requires an enabled Setup Admin or Meeting Operator account.",
            );
        };
        let Some(request) = parse_body::<CreateScheduledRecordingRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete scheduled recording body is required.",
            );
        };
        let schedule_item =
            match self.read_committed_recording_schedule_item(&request.schedule_item_id) {
                Ok(item) => item,
                Err(message) => return recording_schedule_error_response(message),
            };
        let scheduled_end_unix_ms = match schedule_item.ends_at_unix_ms() {
            Ok(end) => end,
            Err(error) => {
                return error_response(409, "schedule_binding_changed", &format!("{error:?}"));
            }
        };
        let requested_at_unix_ms = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let recording = match ScheduledRecording::new(station_recording::RecordingDefinition {
            recording_id: request.recording_id,
            channel_id: schedule_item.channel_id,
            schedule_item_id: request.schedule_item_id,
            schedule_revision: schedule_item.revision,
            scheduled_start_unix_ms: schedule_item.starts_at_unix_ms,
            scheduled_end_unix_ms,
            requested_by,
            requested_at_unix_ms,
        }) {
            Ok(recording) => recording,
            Err(message) => return error_response(422, "validation_failed", &message),
        };
        match self.storage.create_scheduled_recording(&recording) {
            Ok(()) => json_response(
                201,
                serde_json::to_vec(&recording)
                    .expect("serializing scheduled recording cannot fail"),
            ),
            Err(message) if message.contains("already exists") => {
                error_response(409, "recording_conflict", &message)
            }
            Err(message)
                if message.contains("scheduled recording schedule item")
                    || message.contains("schedule binding") =>
            {
                recording_schedule_error_response(message)
            }
            Err(message) if message.contains("invalid") || message.contains("revision") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_scheduled_recording(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if recording_operator(actor).is_none() {
            return error_response(
                403,
                "recording_operator_required",
                "Reading scheduled recordings requires an enabled operator account.",
            );
        }
        let Some(recording_id) = scheduled_recording_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The scheduled recording identity is invalid.",
            );
        };
        match self.storage.read_scheduled_recording(recording_id) {
            Ok(Some(recording)) => json_response(
                200,
                serde_json::to_vec(&recording)
                    .expect("serializing scheduled recording cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "recording_not_found",
                "The scheduled recording does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_scheduled_recordings(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if recording_operator(actor).is_none() {
            return error_response(
                403,
                "recording_operator_required",
                "Reading scheduled recordings requires an enabled operator account.",
            );
        }
        let Some((channel_id, limit)) = scheduled_recording_list_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The scheduled recording list path is invalid.",
            );
        };
        match self
            .storage
            .list_scheduled_recordings_for_channel(channel_id, limit)
        {
            Ok(recordings) => json_response(
                200,
                serde_json::to_vec(&recordings)
                    .expect("serializing scheduled recordings cannot fail"),
            ),
            Err(message) if message.contains("invalid") || message.contains("unbounded") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_scheduled_recording_candidates(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if recording_operator(actor).is_none() {
            return error_response(
                403,
                "recording_operator_required",
                "Reading recording candidates requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some((channel_id, limit)) = scheduled_recording_candidates_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The scheduled recording candidate path is invalid.",
            );
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let ends_before_unix_ms = match now.checked_add(RECORDING_CANDIDATE_FUTURE_WINDOW_MS) {
            Some(end) => end,
            None => {
                return error_response(
                    500,
                    "clock_error",
                    "The recording candidate window overflows the station clock.",
                );
            }
        };
        match self.storage.list_recording_schedule_candidates(
            channel_id,
            now,
            ends_before_unix_ms,
            limit,
        ) {
            Ok(items) => json_response(
                200,
                serde_json::to_vec(&items)
                    .expect("serializing recording schedule candidates cannot fail"),
            ),
            Err(message) if message.contains("invalid") || message.contains("unbounded") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_scheduled_recording_history(
        &self,
        path: &str,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        if recording_operator(actor).is_none() {
            return error_response(
                403,
                "recording_operator_required",
                "Reading scheduled recording history requires an enabled operator account.",
            );
        }
        let Some((recording_id, limit)) = scheduled_recording_history_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The scheduled recording history path is invalid.",
            );
        };
        match self
            .storage
            .list_scheduled_recording_history(recording_id, limit)
        {
            Ok(history) => {
                let response = history
                    .into_iter()
                    .map(|entry| ScheduledRecordingHistoryResponse {
                        recording: entry.recording,
                        receipt: entry.receipt,
                        actor: entry.actor,
                        occurred_at_unix_ms: entry.occurred_at_unix_ms,
                    })
                    .collect::<Vec<_>>();
                json_response(
                    200,
                    serde_json::to_vec(&response)
                        .expect("serializing scheduled recording history cannot fail"),
                )
            }
            Err(message) if message.contains("invalid") || message.contains("unbounded") => {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn arm_scheduled_recording(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(actor) = recording_operator(actor) else {
            return error_response(
                403,
                "recording_operator_required",
                "Arming a scheduled recording requires an enabled Setup Admin or Meeting Operator account.",
            );
        };
        let Some(recording_id) = scheduled_recording_action_path(path, "arm") else {
            return error_response(
                400,
                "invalid_path",
                "The scheduled recording identity is invalid.",
            );
        };
        let Some(request) = parse_body::<RecordingExpectedRevisionRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An expected recording revision is required.",
            );
        };
        let recording = match self.storage.read_scheduled_recording(recording_id) {
            Ok(Some(recording)) => recording,
            Ok(None) => {
                return error_response(
                    404,
                    "recording_not_found",
                    "The scheduled recording does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if let Err(message) = self.recheck_scheduled_recording_binding(&recording) {
            return recording_schedule_error_response(message);
        }
        let execution =
            match self.current_recording_execution_authority(&recording.definition.channel_id) {
                Ok(execution) => execution,
                Err(message) => {
                    return error_response(409, "execution_authority_unavailable", &message);
                }
            };
        self.transition_scheduled_recording(
            recording,
            RecordingTransitionRequest {
                expected_revision: request.expected_revision,
                actor,
                occurred_at_unix_ms: match system_time_millis() {
                    Ok(now) => now,
                    Err(message) => return error_response(500, "clock_error", &message),
                },
                execution: Some(execution),
                event: RecordingEvent::Arm,
            },
        )
    }

    fn cancel_scheduled_recording(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(actor) = recording_operator(actor) else {
            return error_response(
                403,
                "recording_operator_required",
                "Cancelling a scheduled recording requires an enabled Setup Admin or Meeting Operator account.",
            );
        };
        let Some(recording_id) = scheduled_recording_action_path(path, "cancel") else {
            return error_response(
                400,
                "invalid_path",
                "The scheduled recording identity is invalid.",
            );
        };
        let Some(request) = parse_body::<CancelScheduledRecordingRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An expected recording revision and cancellation reason are required.",
            );
        };
        let recording = match self.storage.read_scheduled_recording(recording_id) {
            Ok(Some(recording)) => recording,
            Ok(None) => {
                return error_response(
                    404,
                    "recording_not_found",
                    "The scheduled recording does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        self.transition_scheduled_recording(
            recording.clone(),
            RecordingTransitionRequest {
                expected_revision: request.expected_revision,
                actor,
                occurred_at_unix_ms: match system_time_millis() {
                    Ok(now) => now,
                    Err(message) => return error_response(500, "clock_error", &message),
                },
                execution: recording_execution_authority(&recording),
                event: RecordingEvent::Cancel {
                    reason: request.reason,
                },
            },
        )
    }

    pub fn record_trusted_scheduled_recording_worker_observation(
        &self,
        observation: TrustedScheduledRecordingWorkerObservation,
    ) -> Result<(), String> {
        let recording = self
            .storage
            .read_scheduled_recording(&observation.recording_id)?
            .ok_or_else(|| "scheduled recording does not exist".to_string())?;
        self.recheck_scheduled_recording_binding(&recording)?;
        let execution =
            self.current_recording_execution_authority(&recording.definition.channel_id)?;
        let now = system_time_millis()?;
        let outcome = recording.transition(RecordingTransitionRequest {
            expected_revision: observation.expected_revision,
            actor: RecordingActor {
                actor_id: RECORDING_WORKER_ACTOR_ID.into(),
                role: RecordingActorRole::RecordingWorker,
            },
            occurred_at_unix_ms: now,
            execution: Some(execution),
            event: trusted_recording_worker_event(observation.event),
        })?;
        self.storage
            .write_scheduled_recording_transition(&outcome.recording, &outcome.receipt)
    }

    fn current_recording_execution_authority(
        &self,
        channel_id: &str,
    ) -> Result<RecordingExecutionAuthority, String> {
        let station_box = self.station_box_profile_uncached()?;
        let source_commit = station_box
            .source_commit
            .filter(|_| station_box.release.candidate_verified)
            .ok_or_else(|| {
                "A verified installed candidate is required for recording execution authority."
                    .to_string()
            })?;
        let candidate = RecordingCandidateIdentity {
            version: station_box.product_version,
            source_commit,
        };
        candidate.validate()?;
        let channel_configuration_hash = self
            .storage
            .current_channel_output_configuration_hash(channel_id)?;
        let execution = RecordingExecutionAuthority {
            candidate,
            channel_configuration_hash,
        };
        execution.validate()?;
        Ok(execution)
    }

    fn read_committed_recording_schedule_item(
        &self,
        schedule_item_id: &str,
    ) -> Result<ScheduleItem, String> {
        let item = self
            .storage
            .read_schedule_item(schedule_item_id)?
            .ok_or_else(|| "scheduled recording schedule item does not exist".to_string())?;
        if item.state != ScheduleState::Committed {
            return Err("scheduled recording schedule item is not committed".into());
        }
        item.validate().map_err(|error| format!("{error:?}"))?;
        Ok(item)
    }

    fn recheck_scheduled_recording_binding(
        &self,
        recording: &ScheduledRecording,
    ) -> Result<(), String> {
        let item =
            self.read_committed_recording_schedule_item(&recording.definition.schedule_item_id)?;
        if item.revision != recording.definition.schedule_revision
            || item.channel_id != recording.definition.channel_id
            || item.starts_at_unix_ms != recording.definition.scheduled_start_unix_ms
            || item
                .ends_at_unix_ms()
                .map_err(|error| format!("{error:?}"))?
                != recording.definition.scheduled_end_unix_ms
        {
            return Err("scheduled recording schedule binding is stale or changed".into());
        }
        Ok(())
    }

    fn transition_scheduled_recording(
        &self,
        recording: ScheduledRecording,
        request: RecordingTransitionRequest,
    ) -> ApiResponse {
        let outcome = match recording.transition(request) {
            Ok(outcome) => outcome,
            Err(message) if message.contains("revision") => {
                return error_response(409, "revision_conflict", &message);
            }
            Err(message) => return error_response(422, "transition_rejected", &message),
        };
        match self
            .storage
            .write_scheduled_recording_transition(&outcome.recording, &outcome.receipt)
        {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&outcome.recording)
                    .expect("serializing scheduled recording cannot fail"),
            ),
            Err(message) if message.contains("conflict") || message.contains("race") => {
                error_response(409, "revision_conflict", &message)
            }
            Err(message)
                if message.contains("scheduled recording schedule item")
                    || message.contains("schedule binding") =>
            {
                recording_schedule_error_response(message)
            }
            Err(message) => error_response(500, "storage_error", &message),
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

    fn read_asset_lifecycle(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_metadata_reader(actor) {
            return error_response(
                403,
                "media_lifecycle_reader_required",
                "Reading media lifecycle authority requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some(asset_id) = asset_lifecycle_path(path, "/lifecycle") else {
            return error_response(400, "invalid_path", "The media asset identity is invalid.");
        };
        if let Err(response) = self.require_lifecycle_asset(asset_id) {
            return response;
        }
        let response = AssetLifecycleResponse {
            asset_id: asset_id.into(),
            retention: match self.storage.read_asset_retention_policy(asset_id) {
                Ok(value) => value,
                Err(message) => return error_response(500, "storage_error", &message),
            },
            replacement: match self.storage.read_asset_replacement_link(asset_id) {
                Ok(value) => value,
                Err(message) => return error_response(500, "storage_error", &message),
            },
            thumbnail: match self.storage.read_asset_thumbnail(asset_id) {
                Ok(value) => value,
                Err(message) => return error_response(500, "storage_error", &message),
            },
            recovery_history: match self.storage.list_asset_recovery_history(asset_id, 64) {
                Ok(value) => value,
                Err(message) => return error_response(500, "storage_error", &message),
            },
        };
        json_response(
            200,
            serde_json::to_vec(&response).expect("serializing lifecycle response cannot fail"),
        )
    }

    /// Serves only a server-derived PNG whose content identity is bound to the
    /// asset thumbnail record. Neither the request nor the durable record
    /// carries a filesystem path.
    fn read_asset_thumbnail(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_metadata_reader(actor) {
            return error_response(
                403,
                "media_lifecycle_reader_required",
                "Reading media review evidence requires an enabled Setup Admin or Meeting Operator account.",
            );
        }
        let Some(asset_id) = asset_lifecycle_path(path, "/thumbnail") else {
            return error_response(400, "invalid_path", "The media asset identity is invalid.");
        };
        if let Err(response) = self.require_lifecycle_asset(asset_id) {
            return response;
        }
        let thumbnail = match self.storage.read_asset_thumbnail(asset_id) {
            Ok(Some(value)) => value,
            Ok(None) => {
                return error_response(
                    404,
                    "thumbnail_pending",
                    "Review evidence has not been derived for this asset yet.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if thumbnail.source_asset_sha256 != asset_id {
            return error_response(
                500,
                "thumbnail_authority_invalid",
                "Stored review evidence is not bound to this asset.",
            );
        }
        let root = match self.database_path.parent() {
            Some(parent) => parent.join("media-review"),
            None => {
                return error_response(
                    500,
                    "managed_root_unavailable",
                    "The managed review evidence root is unavailable.",
                );
            }
        };
        let bytes = match read_pinned_thumbnail_bytes(&root, &thumbnail.thumbnail_sha256) {
            Ok(value) => value,
            Err(message) => return error_response(500, "thumbnail_evidence_unavailable", &message),
        };
        dynamic_response(200, "image/png", bytes)
            .with_header("Cache-Control", "no-store")
            .expect("static thumbnail response headers are valid")
    }

    fn put_asset_retention(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(actor) = lifecycle_writer(actor) else {
            return error_response(
                403,
                "setup_admin_required",
                "Changing media retention requires an enabled Setup Admin account.",
            );
        };
        let Some(asset_id) = asset_lifecycle_path(path, "/retention") else {
            return error_response(400, "invalid_path", "The media asset identity is invalid.");
        };
        if let Err(response) = self.require_lifecycle_asset(asset_id) {
            return response;
        }
        let Some(request) = parse_body::<PutAssetRetentionRequest>(body) else {
            return error_response(400, "invalid_json", "A media retention body is required.");
        };
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let revision = match request.expected_revision.checked_add(1) {
            Some(value) => value,
            None => {
                return error_response(
                    422,
                    "validation_failed",
                    "The expected retention revision is invalid.",
                );
            }
        };
        let disposition = if request.legal_hold {
            RetentionDisposition::LegalHold
        } else {
            RetentionDisposition::Retained
        };
        let policy = AssetRetentionPolicy {
            asset_id: asset_id.into(),
            revision,
            permanent: request.permanent,
            review_after_unix_ms: request.review_after_unix_ms,
            legal_hold: request.legal_hold,
            disposition,
        };
        match self.storage.write_asset_retention_policy(
            &policy,
            request.expected_revision,
            actor.account_id().as_str(),
            occurred_at_unix_ms,
        ) {
            Ok(policy) => json_response(
                200,
                serde_json::to_vec(&policy).expect("serializing retention policy cannot fail"),
            ),
            Err(message) if message.contains("conflict") => {
                error_response(409, "revision_conflict", &message)
            }
            Err(message)
                if message.contains("invalid")
                    || message.contains("requires")
                    || message.contains("must") =>
            {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn relink_missing_asset(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(actor) = lifecycle_writer(actor) else {
            return error_response(
                403,
                "setup_admin_required",
                "Relinking managed media requires an enabled Setup Admin account.",
            );
        };
        let Some(asset_id) = asset_lifecycle_path(path, "/relink") else {
            return error_response(400, "invalid_path", "The media asset identity is invalid.");
        };
        let asset = match self.storage.read_media_asset(asset_id) {
            Ok(Some(asset)) if asset.readiness == AssetReadiness::Missing => asset,
            Ok(Some(_)) => {
                return error_response(
                    409,
                    "asset_not_missing",
                    "Only a missing managed asset can be relinked.",
                );
            }
            Ok(None) => {
                return error_response(404, "asset_not_found", "The media asset does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let Some(request) = parse_body::<RelinkMissingAssetRequest>(body) else {
            return error_response(400, "invalid_json", "A candidate source path is required.");
        };
        let admitted = match self.admit_lifecycle_source(&request.source_path) {
            Ok(value) => value,
            Err(message) => return error_response(422, "media_admission_failed", &message),
        };
        if admitted.asset_id != asset.asset_id {
            return error_response(
                422,
                "media_identity_mismatch",
                "The admitted candidate bytes do not match the missing asset identity.",
            );
        }
        let duration_ms = (admitted.metadata.duration_ns / 1_000_000).max(1);
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let history = match self.storage.list_asset_recovery_history(asset_id, 256) {
            Ok(value) => value,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let revision = match history
            .last()
            .map_or(Some(1), |entry| entry.revision.checked_add(1))
        {
            Some(value) => value,
            None => {
                return error_response(
                    409,
                    "recovery_revision_exhausted",
                    "The recovery history revision is exhausted.",
                );
            }
        };
        let audit = AssetRecoveryAudit {
            asset_id: asset_id.into(),
            revision,
            recovered_asset_sha256: admitted.asset_id,
            managed_path: admitted.stored_path.to_string_lossy().into_owned(),
            byte_count: admitted.size_bytes,
            duration_ms,
            changed_by: actor.account_id().as_str().into(),
            occurred_at_unix_ms,
        };
        match self.storage.relink_missing_media_asset(&audit) {
            Ok(_asset) => json_response(
                200,
                serde_json::to_vec(&audit).expect("serializing recovery audit cannot fail"),
            ),
            Err(message) if message.contains("missing") || message.contains("raced") => {
                error_response(409, "asset_state_conflict", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn create_asset_replacement(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(actor) = lifecycle_writer(actor) else {
            return error_response(
                403,
                "setup_admin_required",
                "Replacing managed media requires an enabled Setup Admin account.",
            );
        };
        let Some(predecessor_asset_id) = asset_lifecycle_path(path, "/replacements") else {
            return error_response(400, "invalid_path", "The media asset identity is invalid.");
        };
        if let Err(response) = self.require_lifecycle_asset(predecessor_asset_id) {
            return response;
        }
        let Some(request) = parse_body::<CreateAssetReplacementRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A replacement source body is required.",
            );
        };
        let admitted = match self.admit_lifecycle_source(&request.source_path) {
            Ok(value) => value,
            Err(message) => return error_response(422, "media_admission_failed", &message),
        };
        if admitted.asset_id == predecessor_asset_id {
            return error_response(
                422,
                "replacement_identity_unchanged",
                "Replacement bytes must create a different immutable media identity.",
            );
        }
        let replacement = MediaAsset {
            asset_id: admitted.asset_id.clone(),
            media_path: admitted.stored_path.to_string_lossy().into_owned(),
            duration_ms: (admitted.metadata.duration_ns / 1_000_000).max(1),
            readiness: AssetReadiness::Ready,
        };
        match self.storage.read_media_asset(&replacement.asset_id) {
            Ok(None) => {
                if let Err(message) = self.storage.put_media_asset(&replacement) {
                    return error_response(500, "storage_error", &message);
                }
            }
            Ok(Some(existing))
                if existing.readiness == AssetReadiness::Ready
                    && existing.asset_id == replacement.asset_id => {}
            Ok(Some(_)) => {
                return error_response(
                    409,
                    "replacement_asset_state_conflict",
                    "The immutable replacement asset exists but is not ready.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let link = AssetReplacementLink {
            predecessor_asset_id: predecessor_asset_id.into(),
            successor_asset_id: replacement.asset_id.clone(),
            revision: 1,
            changed_by: actor.account_id().as_str().into(),
            occurred_at_unix_ms,
        };
        match self
            .storage
            .create_asset_replacement_link(&link, request.expected_predecessor_revision)
        {
            Ok(()) => json_response(
                201,
                serde_json::to_vec(&link).expect("serializing replacement link cannot fail"),
            ),
            Err(message)
                if message.contains("already")
                    || message.contains("once")
                    || message.contains("ready") =>
            {
                error_response(409, "replacement_conflict", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn require_lifecycle_asset(&self, asset_id: &str) -> Result<(), ApiResponse> {
        match self.storage.read_media_asset(asset_id) {
            Ok(Some(_)) => Ok(()),
            Ok(None) => Err(error_response(
                404,
                "asset_not_found",
                "The media asset does not exist.",
            )),
            Err(message) => Err(error_response(500, "storage_error", &message)),
        }
    }

    fn admit_lifecycle_source(
        &self,
        source_path: &Path,
    ) -> Result<station_media_assets::IngestedAsset, String> {
        if !source_path.is_absolute() {
            return Err("the candidate source path must be absolute".into());
        }
        ensure_non_reparse_ancestry(source_path).map_err(
            |_| "the candidate source ancestry contains a reparse point or is unavailable",
        )?;
        let metadata = std::fs::symlink_metadata(source_path)
            .map_err(|error| format!("inspect candidate source: {error}"))?;
        if !metadata.file_type().is_file()
            || metadata.len() == 0
            || metadata.file_attributes() & 0x400 != 0
        {
            return Err("the candidate source must be a non-empty regular non-reparse file".into());
        }
        let parent = self
            .database_path
            .parent()
            .ok_or("the station data root is unavailable")?;
        let library = parent.join("media");
        ingest_media(source_path, library)
            .map_err(|error| format!("admit candidate source: {error:?}"))
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

    fn read_fallback_slate(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/fallback-slate") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let channel_exists = match self.storage.list_channels() {
            Ok(channels) => channels
                .iter()
                .any(|channel| channel.channel_id == channel_id),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if !channel_exists {
            return error_response(404, "channel_not_found", "The channel does not exist.");
        }
        let selection = match self.storage.read_channel_fallback_slate(channel_id) {
            Ok(selection) => selection.map(fallback_slate_selection_response),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let audit = match self.storage.list_channel_fallback_slate_audit(channel_id) {
            Ok(events) => events
                .into_iter()
                .map(fallback_slate_audit_response)
                .collect(),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        json_response(
            200,
            serde_json::to_vec(&FallbackSlateAuthorityResponse {
                channel_id: channel_id.into(),
                selection,
                audit,
            })
            .expect("serializing fallback slate authority cannot fail"),
        )
    }

    fn put_fallback_slate(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/fallback-slate") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let Some(actor) = actor.filter(|account| {
            account.enabled()
                && account
                    .roles()
                    .contains(&station_auth::OperatorRole::SetupAdmin)
        }) else {
            return error_response(
                403,
                "setup_admin_required",
                "Selecting a fallback slate requires the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<PutFallbackSlateRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A fallback slate selection body is required.",
            );
        };
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let audit_id = match secure_canonical_uuid() {
            Ok(value) => value,
            Err(message) => return error_response(500, "entropy_unavailable", &message),
        };
        match self.storage.write_channel_fallback_slate(
            channel_id,
            &request.image_asset_id,
            request.expected_revision,
            actor.account_id().as_str(),
            occurred_at_unix_ms,
            &audit_id,
        ) {
            Ok(selection) => json_response(
                200,
                serde_json::to_vec(&fallback_slate_selection_response(selection))
                    .expect("serializing fallback slate selection cannot fail"),
            ),
            Err(FallbackSlateWriteError::NotFound) => {
                error_response(404, "channel_not_found", "The channel does not exist.")
            }
            Err(FallbackSlateWriteError::MustBeStopped) => error_response(
                409,
                "channel_must_be_stopped",
                "Stop this output before changing its fallback slate.",
            ),
            Err(FallbackSlateWriteError::ImageNotReady) => error_response(
                409,
                "fallback_slate_not_ready",
                "The selected managed image is missing, corrupt, or not ready.",
            ),
            Err(FallbackSlateWriteError::Conflict(current_revision)) => error_response(
                409,
                "revision_conflict",
                &format!("The current fallback slate revision is {current_revision}."),
            ),
            Err(FallbackSlateWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", &message)
            }
            Err(FallbackSlateWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn read_caption_policy(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/caption-policy") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let channel_exists = match self.storage.list_channels() {
            Ok(channels) => channels
                .iter()
                .any(|channel| channel.channel_id == channel_id),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if !channel_exists {
            return error_response(404, "channel_not_found", "The channel does not exist.");
        }
        let policy = match self.storage.read_channel_caption_policy(channel_id) {
            Ok(policy) => policy.map(caption_policy_response),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let audit = match self.storage.list_channel_caption_policy_audit(channel_id) {
            Ok(events) => events
                .into_iter()
                .map(caption_policy_audit_response)
                .collect(),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        json_response(
            200,
            serde_json::to_vec(&CaptionPolicyAuthorityResponse {
                channel_id: channel_id.into(),
                policy,
                audit,
            })
            .expect("serializing caption policy authority cannot fail"),
        )
    }

    fn put_caption_policy(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/caption-policy") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let Some(actor) = actor.filter(|account| {
            account.enabled()
                && account
                    .roles()
                    .contains(&station_auth::OperatorRole::SetupAdmin)
        }) else {
            return error_response(
                403,
                "setup_admin_required",
                "Changing caption policy requires the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<PutCaptionPolicyRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete caption policy body is required.",
            );
        };
        let mode = match request.mode {
            CaptionModeRequest::Disabled => ChannelCaptionMode::Disabled,
            CaptionModeRequest::Cea708 => ChannelCaptionMode::Cea708,
        };
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let audit_id = match secure_canonical_uuid() {
            Ok(value) => value,
            Err(message) => return error_response(500, "entropy_unavailable", &message),
        };
        match self.storage.write_channel_caption_policy(
            channel_id,
            mode,
            request.service_number,
            &request.language,
            request.expected_revision,
            actor.account_id().as_str(),
            occurred_at_unix_ms,
            &audit_id,
        ) {
            Ok(policy) => json_response(
                200,
                serde_json::to_vec(&caption_policy_response(policy))
                    .expect("serializing caption policy cannot fail"),
            ),
            Err(CaptionPolicyWriteError::NotFound) => {
                error_response(404, "channel_not_found", "The channel does not exist.")
            }
            Err(CaptionPolicyWriteError::MustBeStopped) => error_response(
                409,
                "channel_must_be_stopped",
                "Stop this output before changing its caption policy.",
            ),
            Err(CaptionPolicyWriteError::Conflict(current_revision)) => error_response(
                409,
                "revision_conflict",
                &format!("The current caption policy revision is {current_revision}."),
            ),
            Err(CaptionPolicyWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", &message)
            }
            Err(CaptionPolicyWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn read_asset_caption_track(&self, path: &str) -> ApiResponse {
        let Some((asset_id, track_id)) = asset_caption_track_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The asset or caption track identity is invalid.",
            );
        };
        let track = match self.storage.read_asset_caption_track(track_id) {
            Ok(Some(track)) if track.asset_id == asset_id => track,
            Ok(Some(_)) | Ok(None) => {
                return error_response(
                    404,
                    "caption_track_not_found",
                    "The caption track does not exist for this media asset.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let audit = match self.storage.list_asset_caption_track_audit(track_id) {
            Ok(audit) => audit,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        json_response(
            200,
            serde_json::to_vec(&AssetCaptionTrackAuthorityResponse { track, audit })
                .expect("serializing asset caption authority cannot fail"),
        )
    }

    fn list_asset_caption_tracks(&self, path: &str) -> ApiResponse {
        let Some(asset_id) = asset_caption_tracks_path(path) else {
            return error_response(400, "invalid_path", "The asset identity is invalid.");
        };
        match self.storage.read_media_asset(asset_id) {
            Ok(Some(_)) => {}
            Ok(None) => {
                return error_response(404, "asset_not_found", "The media asset does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let tracks = match self.storage.list_asset_caption_tracks(asset_id) {
            Ok(tracks) => tracks,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let tracks = tracks
            .into_iter()
            .map(|track| AssetCaptionTrackSummaryResponse {
                track_id: track.track_id,
                asset_id: track.asset_id,
                language: track.language,
                service_number: track.service_number,
                source_kind: track.source_kind,
                label: track.label,
                cue_count: track.cues.len(),
                cues_sha256: track.cues_sha256,
                revision: track.revision,
                created_at_unix_ms: track.created_at_unix_ms,
                updated_at_unix_ms: track.updated_at_unix_ms,
                updated_by: track.updated_by,
            })
            .collect();
        json_response(
            200,
            serde_json::to_vec(&AssetCaptionTrackListResponse {
                asset_id: asset_id.into(),
                tracks,
            })
            .expect("serializing asset caption track list cannot fail"),
        )
    }

    fn put_asset_caption_track(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some((asset_id, track_id)) = asset_caption_track_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The asset or caption track identity is invalid.",
            );
        };
        let Some(actor) = actor.filter(|account| {
            account.enabled()
                && account
                    .roles()
                    .contains(&station_auth::OperatorRole::SetupAdmin)
        }) else {
            return error_response(
                403,
                "setup_admin_required",
                "Changing an asset caption track requires the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<PutAssetCaptionTrackRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A complete asset caption track body is required.",
            );
        };
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let audit_id = match secure_canonical_uuid() {
            Ok(value) => value,
            Err(message) => return error_response(500, "entropy_unavailable", &message),
        };
        let write = AssetCaptionTrackWrite {
            track_id: track_id.into(),
            asset_id: asset_id.into(),
            language: request.language,
            service_number: request.service_number,
            source_kind: request.source_kind,
            label: request.label,
            cues: request
                .cues
                .into_iter()
                .map(|cue| AssetCaptionCue {
                    cue_id: cue.cue_id,
                    start_ms: cue.start_ms,
                    duration_ms: cue.duration_ms,
                    text: cue.text,
                })
                .collect(),
            expected_revision: request.expected_revision,
            actor: actor.account_id().as_str().into(),
            occurred_at_unix_ms,
            audit_id,
        };
        match self.storage.write_asset_caption_track(&write) {
            Ok(track) => json_response(
                200,
                serde_json::to_vec(&track).expect("serializing asset caption track cannot fail"),
            ),
            Err(CaptionTrackWriteError::AssetNotFound) => {
                error_response(404, "asset_not_found", "The media asset does not exist.")
            }
            Err(CaptionTrackWriteError::AssetNotReady) => error_response(
                409,
                "asset_not_ready",
                "Caption authority requires a currently ready media asset.",
            ),
            Err(CaptionTrackWriteError::AssetOnAir) => error_response(
                409,
                "asset_on_air",
                "Wait until this media asset is off air before changing its captions.",
            ),
            Err(CaptionTrackWriteError::NotFound) => error_response(
                404,
                "caption_track_not_found",
                "The caption track revision target does not exist.",
            ),
            Err(CaptionTrackWriteError::Conflict(current_revision)) => error_response(
                409,
                "revision_conflict",
                &format!("The current caption track revision is {current_revision}."),
            ),
            Err(CaptionTrackWriteError::SlotOccupied { track_id, revision }) => error_response(
                409,
                "caption_slot_occupied",
                &format!(
                    "Caption track {track_id} already owns this asset, language, and service at revision {revision}."
                ),
            ),
            Err(CaptionTrackWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", &message)
            }
            Err(CaptionTrackWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn put_transport_target(
        &self,
        path: &str,
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
                "Configuring a target device requires the Setup Admin role.",
            );
        };
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/transport-target") else {
            return error_response(400, "invalid_path", "The transport target path is invalid.");
        };
        let Some(request) = parse_body::<PutTransportTargetRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A transport target configuration body is required.",
            );
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let config = TargetDeviceConfig {
            target_id: request.target_id,
            label: request.label,
            protocol: request.protocol,
            host: request.host,
            port: request.port,
            credential_configured: request.authorization.is_some(),
            revision: 1,
            created_at_unix_ms: now,
            updated_at_unix_ms: now,
            updated_by: actor.account_id().as_str().into(),
        };
        // Target configuration is attached to a specific sink. The body may not
        // select an arbitrary output; require the canonical required sink when
        // the channel has only one, otherwise reject the ambiguity.
        let channel = match self.storage.list_channels() {
            Ok(channels) => channels
                .into_iter()
                .find(|value| value.channel_id == channel_id),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let Some(channel) = channel else {
            return error_response(404, "channel_not_found", "The channel does not exist.");
        };
        let sinks = channel
            .output_sinks
            .iter()
            .filter(|sink| sink.enabled && sink.required)
            .collect::<Vec<_>>();
        if sinks.len() != 1 {
            return error_response(
                409,
                "transport_target_sink_ambiguous",
                "Target configuration requires exactly one enabled required transport sink.",
            );
        }
        match self.storage.write_transport_target_config(
            channel_id,
            &sinks[0].sink_id,
            request.expected_revision,
            &config,
        ) {
            Ok(stored) => {
                if let Some(authorization) = request.authorization.as_deref()
                    && let Err(message) = self.storage.store_transport_target_authorization(
                        channel_id,
                        &sinks[0].sink_id,
                        &stored,
                        authorization,
                    )
                {
                    return error_response(422, "credential_provisioning_failed", &message);
                }
                match self
                    .storage
                    .read_transport_target_config(channel_id, &sinks[0].sink_id)
                {
                    Ok(Some(stored)) => json_response(
                        200,
                        serde_json::to_vec(&stored)
                            .expect("serializing transport target configuration cannot fail"),
                    ),
                    Ok(None) => error_response(
                        500,
                        "storage_error",
                        "Target configuration disappeared after write.",
                    ),
                    Err(message) => error_response(500, "storage_error", &message),
                }
            }
            Err(message) if message.contains("revision conflict") => {
                error_response(409, "revision_conflict", &message)
            }
            Err(message) => error_response(422, "validation_failed", &message),
        }
    }

    fn read_transport_target(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading target device configuration requires an enabled operator account.",
            );
        }
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/transport-target") else {
            return error_response(400, "invalid_path", "The transport target path is invalid.");
        };
        let channel = match self.storage.list_channels() {
            Ok(channels) => channels
                .into_iter()
                .find(|value| value.channel_id == channel_id),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let Some(channel) = channel else {
            return error_response(404, "channel_not_found", "The channel does not exist.");
        };
        let sinks = channel
            .output_sinks
            .iter()
            .filter(|sink| sink.enabled && sink.required)
            .collect::<Vec<_>>();
        if sinks.len() != 1 {
            return error_response(
                409,
                "transport_target_sink_ambiguous",
                "Target configuration requires exactly one enabled required transport sink.",
            );
        }
        match self
            .storage
            .read_transport_target_config(channel_id, &sinks[0].sink_id)
        {
            Ok(Some(config)) => json_response(
                200,
                serde_json::to_vec(&config)
                    .expect("serializing transport target configuration cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "transport_target_not_configured",
                "No target device is configured for the required transport sink.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_transport_proof(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading transport proof requires an enabled operator account.",
            );
        }
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/transport-proof") else {
            return error_response(400, "invalid_path", "The transport proof path is invalid.");
        };
        let now_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let history = match self
            .storage
            .read_transport_proof_history(channel_id, None, 1_000)
        {
            Ok(history) => history,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let station_box = self.cached_station_box_profile().ok();
        let candidate = station_box.as_ref().and_then(|profile| {
            profile.release.candidate_verified.then_some({
                (
                    profile.product_version.as_str(),
                    profile.source_commit.as_deref(),
                )
            })
        });
        let mut validations = Vec::new();
        if let Some((candidate_version, Some(source_commit))) = candidate
            && let Ok(channels) = self.storage.list_channels()
            && let Some(channel) = channels
                .into_iter()
                .find(|value| value.channel_id == channel_id)
        {
            for sink in channel
                .output_sinks
                .iter()
                .filter(|sink| sink.enabled && sink.required)
            {
                if let Ok(Some(validation)) = self.storage.read_current_cable_transport_validation(
                    channel_id,
                    &sink.sink_id,
                    candidate_version,
                    source_commit,
                    now_unix_ms,
                ) {
                    validations.push(validation);
                }
            }
        }
        json_response(
            200,
            serde_json::to_vec(&TransportProofResponse {
                channel_id: channel_id.into(),
                history,
                validations,
            })
            .expect("serializing transport proof cannot fail"),
        )
    }

    /// A browser may request work, but it cannot nominate the candidate,
    /// target, configuration, actor, clock, capture path, or outcome.  Those
    /// facts are derived by the server and the store rechecks target/config in
    /// its insertion transaction.
    fn request_transport_proof_job(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(actor) = actor.filter(|account| account.enabled() && is_setup_admin(account))
        else {
            return error_response(
                403,
                "setup_admin_required",
                "Requesting transport proof requires the Setup Admin role.",
            );
        };
        if body.is_some_and(|bytes| !bytes.is_empty()) {
            return error_response(
                400,
                "invalid_json",
                "A transport-proof request accepts no caller authority fields.",
            );
        }
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/transport-proof/jobs")
        else {
            return error_response(
                400,
                "invalid_path",
                "The transport proof job path is invalid.",
            );
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let channel = match self.storage.list_channels() {
            Ok(channels) => channels
                .into_iter()
                .find(|channel| channel.channel_id == channel_id),
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let Some(channel) = channel else {
            return error_response(404, "channel_not_found", "The channel does not exist.");
        };
        let sinks = channel
            .output_sinks
            .into_iter()
            .filter(|sink| {
                sink.enabled
                    && sink.required
                    && sink.kind == station_schedule::OutputSinkKind::UdpTs
            })
            .collect::<Vec<_>>();
        let [sink] = sinks.as_slice() else {
            return error_response(
                409,
                "transport_sink_ambiguous",
                "Transport proof currently requires exactly one enabled required UDP transport-stream sink.",
            );
        };
        let target = match self
            .storage
            .read_transport_target_config(channel_id, &sink.sink_id)
        {
            Ok(Some(target)) => target,
            Ok(None) => {
                return error_response(
                    409,
                    "transport_target_not_configured",
                    "The required transport sink has no target configuration.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let data_directory = match self.database_path.parent() {
            Some(path) => path,
            None => {
                return error_response(
                    500,
                    "candidate_authority_unavailable",
                    "The station data directory is unavailable.",
                );
            }
        };
        let install_authority_fence = match acquire_install_authority_fence(data_directory) {
            Ok(fence) => fence,
            Err(_) => {
                return error_response(
                    409,
                    "candidate_authority_busy",
                    "Candidate activation is in progress; wait for it to finish before requesting transport proof.",
                );
            }
        };
        if install_authority_fence.was_abandoned() {
            return error_response(
                409,
                "candidate_authority_recovery_required",
                "Candidate authority requires interrupted-update recovery before transport proof can be requested.",
            );
        }
        let candidate = match read_verified_install_authority(
            data_directory,
            &self.application_directory,
        ) {
            Ok(candidate) => candidate,
            Err(_) => {
                return error_response(
                    409,
                    "candidate_not_verified",
                    "A verified installed candidate is required before requesting transport proof.",
                );
            }
        };
        let now = match system_time_millis() {
            Ok(now) => now,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let job_id = match secure_canonical_uuid() {
            Ok(value) => value,
            Err(message) => return error_response(500, "entropy_unavailable", &message),
        };
        let configuration_hash = match self
            .storage
            .current_channel_output_configuration_hash(channel_id)
        {
            Ok(value) => value,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let job = TransportProofJob {
            job_id,
            channel_id: channel_id.into(),
            sink_id: sink.sink_id.clone(),
            target_id: target.target_id,
            target_revision: target.revision,
            candidate_version: candidate.version().into(),
            source_commit: candidate.source_commit().into(),
            install_generation: Some(candidate.install_generation()),
            configuration_hash,
            requested_by: actor.account_id().as_str().into(),
            created_at_unix_ms: now,
            max_capture_bytes: station_transport_proof::MAX_PROOF_CAPTURE_BYTES,
        };
        match self.storage.enqueue_transport_proof_job(&job, || {
            read_verified_install_authority(data_directory, &self.application_directory)
        }) {
            Ok(true) => json_response(
                202,
                serde_json::to_vec(&transport_proof_job_response(
                    self.storage
                        .read_transport_proof_job(&job.job_id)
                        .ok()
                        .flatten()
                        .expect("created transport job must be readable"),
                ))
                .expect("serializing transport proof job cannot fail"),
            ),
            Ok(false) => error_response(
                409,
                "job_already_active",
                "An active transport proof job already exists for this sink.",
            ),
            Err(message) if message.contains("stale") || message.contains("not configured") => {
                error_response(
                    409,
                    "stale_authority",
                    "The target or output configuration changed; refresh and request again.",
                )
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_transport_proof_job(&self, path: &str, actor: Option<&OperatorAccount>) -> ApiResponse {
        if !is_scheduling_reader(actor) {
            return error_response(
                403,
                "operator_required",
                "Reading transport proof jobs requires an enabled operator account.",
            );
        }
        let Some((channel_id, job_id)) = transport_proof_job_path(path) else {
            return error_response(
                400,
                "invalid_path",
                "The transport proof job path is invalid.",
            );
        };
        if !is_api_uuid(channel_id) || !is_canonical_api_uuid(job_id) {
            return error_response(
                400,
                "invalid_path",
                "The transport proof job identity is invalid.",
            );
        }
        match self.storage.read_transport_proof_job(job_id) {
            Ok(Some(record)) if record.job.channel_id == channel_id => json_response(
                200,
                serde_json::to_vec(&transport_proof_job_response(record))
                    .expect("serializing transport proof job cannot fail"),
            ),
            Ok(_) => error_response(
                404,
                "job_not_found",
                "The transport proof job does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn create_receiver_receipt(
        &self,
        path: &str,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/receiver-receipts")
        else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let Some(actor) = actor.filter(|account| {
            account.enabled()
                && account
                    .roles()
                    .contains(&station_auth::OperatorRole::SetupAdmin)
        }) else {
            return error_response(
                403,
                "setup_admin_required",
                "Recording external receiver evidence requires the Setup Admin role.",
            );
        };
        let Some(request) = parse_body::<CreateReceiverReceiptRequest>(body) else {
            return error_response(
                400,
                "invalid_json",
                "An external receiver receipt body is required.",
            );
        };
        if !is_canonical_api_uuid(&request.sink_id) {
            return error_response(422, "validation_failed", "The sink identity is invalid.");
        }
        let station_box = match self.station_box_profile_uncached() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        let Some(source_commit) = station_box
            .source_commit
            .clone()
            .filter(|_| station_box.release.candidate_verified)
        else {
            return error_response(
                409,
                "candidate_not_verified",
                "External receiver evidence requires a verified installed candidate.",
            );
        };
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
        if !snapshot
            .output_sinks
            .iter()
            .any(|sink| sink.sink_id == request.sink_id && sink.enabled)
        {
            return error_response(
                409,
                "sink_not_enabled",
                "External receiver evidence requires a currently configured enabled sink.",
            );
        }
        let recorded_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        if request.observed_at_unix_ms > recorded_at_unix_ms {
            return error_response(
                422,
                "validation_failed",
                "External receiver evidence cannot be observed in the future.",
            );
        }
        let receipt_id = match secure_canonical_uuid() {
            Ok(value) => value,
            Err(message) => return error_response(500, "entropy_unavailable", &message),
        };
        let receipt = ExternalReceiverAcceptanceReceipt {
            receipt_id,
            channel_id: channel_id.into(),
            sink_id: request.sink_id,
            outcome: match request.outcome {
                ReceiverReceiptOutcomeRequest::Accepted => ReceiverAcceptanceOutcome::Accepted,
                ReceiverReceiptOutcomeRequest::Rejected => ReceiverAcceptanceOutcome::Rejected,
            },
            method: match request.method {
                ReceiverEvidenceMethodRequest::HeadendOperatorConfirmation => {
                    ReceiverEvidenceMethod::HeadendOperatorConfirmation
                }
                ReceiverEvidenceMethodRequest::ReceiverTelemetry => {
                    ReceiverEvidenceMethod::ReceiverTelemetry
                }
                ReceiverEvidenceMethodRequest::ReturnPathObservation => {
                    ReceiverEvidenceMethod::ReturnPathObservation
                }
                ReceiverEvidenceMethodRequest::VisualAudioObservation => {
                    ReceiverEvidenceMethod::VisualAudioObservation
                }
            },
            receiver_label: request.receiver_label,
            evidence_reference: request.evidence_reference,
            detail: request.detail,
            observed_at_unix_ms: request.observed_at_unix_ms,
            recorded_at_unix_ms,
            recorded_by: actor.account_id().as_str().into(),
            candidate_version: station_box.product_version,
            source_commit,
            configuration_hash: snapshot.configuration_hash,
        };
        match self
            .storage
            .append_external_receiver_acceptance_receipt(&receipt)
        {
            Ok(()) => json_response(
                201,
                serde_json::to_vec(&receiver_receipt_response(receipt, true))
                    .expect("serializing an external receiver receipt cannot fail"),
            ),
            Err(message)
                if message.contains("configuration") || message.contains("enabled sink") =>
            {
                error_response(409, "receiver_evidence_stale", &message)
            }
            Err(message)
                if message.contains("invalid")
                    || message.contains("missing")
                    || message.contains("unbounded") =>
            {
                error_response(422, "validation_failed", &message)
            }
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn list_receiver_receipts(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/receiver-receipts")
        else {
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
        let station_box = match self.station_box_profile_uncached() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        let current_candidate = station_box
            .source_commit
            .as_deref()
            .filter(|_| station_box.release.candidate_verified)
            .map(|source_commit| (station_box.product_version.as_str(), source_commit));
        let enabled_sinks = snapshot
            .output_sinks
            .iter()
            .filter(|sink| sink.enabled)
            .map(|sink| sink.sink_id.as_str())
            .collect::<BTreeSet<_>>();
        let mut current_receipt_ids = BTreeSet::new();
        if let Some((candidate_version, source_commit)) = current_candidate {
            for sink_id in &enabled_sinks {
                match self
                    .storage
                    .read_current_external_receiver_acceptance_receipt(
                        channel_id,
                        sink_id,
                        candidate_version,
                        source_commit,
                    ) {
                    Ok(Some(receipt)) => {
                        current_receipt_ids.insert(receipt.receipt_id);
                    }
                    Ok(None) => {}
                    Err(message) => return error_response(500, "storage_error", &message),
                }
            }
        }
        let receipts = match self
            .storage
            .list_external_receiver_acceptance_receipts(channel_id, None, 1_000)
        {
            Ok(receipts) => receipts,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let receipts = receipts
            .into_iter()
            .map(|receipt| {
                let current = current_receipt_ids.contains(receipt.receipt_id.as_str());
                receiver_receipt_response(receipt, current)
            })
            .collect();
        json_response(
            200,
            serde_json::to_vec(&ReceiverReceiptHistoryResponse {
                channel_id: channel_id.into(),
                receipts,
            })
            .expect("serializing external receiver receipt history cannot fail"),
        )
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
        let candidate = match self.station_box_profile_uncached() {
            Ok(profile) if profile.release.candidate_verified => Some(CandidateIdentity {
                version: profile.product_version,
                source_commit: profile.source_commit,
            }),
            Ok(_) => None,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let mut sinks = Vec::with_capacity(snapshot.output_sinks.len());
        for sink in snapshot.output_sinks {
            let stored = latest_by_sink.remove(&sink.sink_id);
            let receiver_acceptance = match candidate.as_ref().and_then(|identity| {
                identity
                    .source_commit
                    .as_deref()
                    .map(|commit| (identity, commit))
            }) {
                Some((identity, source_commit)) => match self
                    .storage
                    .read_current_external_receiver_acceptance_receipt(
                        channel_id,
                        &sink.sink_id,
                        &identity.version,
                        source_commit,
                    ) {
                    Ok(Some(receipt)) => match receipt.outcome {
                        ReceiverAcceptanceOutcome::Accepted => ReceiverAcceptance::Verified,
                        ReceiverAcceptanceOutcome::Rejected => ReceiverAcceptance::Rejected,
                    },
                    Ok(None) => ReceiverAcceptance::Unknown,
                    Err(message) => return error_response(500, "storage_error", &message),
                },
                None => ReceiverAcceptance::Unknown,
            };
            sinks.push(loudness_proof_response(
                sink,
                stored,
                receiver_acceptance,
                candidate.as_ref(),
                &snapshot.configuration_hash,
                now,
            ));
        }
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

    fn request_channel_caption_proof(&self, path: &str, body: Option<&[u8]>) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/caption-proof") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        let Some(request) = parse_body::<RequestCaptionProofJob>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A caption proof job request body is required.",
            );
        };
        if request.report_id.trim().is_empty()
            || request.report_id.len() > 200
            || request.report_id.chars().any(char::is_control)
            || !is_canonical_api_uuid(&request.sink_id)
            || !(CAPTION_PROOF_MIN_DURATION_MS..=MAX_CAPTION_PROOF_CAPTURE_DURATION_MS)
                .contains(&request.capture_duration_ms)
            || !(1..=MAX_CAPTION_PROOF_CAPTURE_BYTES).contains(&request.max_capture_bytes)
        {
            return error_response(
                422,
                "validation_failed",
                "The report identity, sink identity, capture duration, or capture limit is invalid or unbounded.",
            );
        }

        let now = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        let report = match self.storage.read_commit_report(&request.report_id) {
            Ok(Some(report)) => report,
            Ok(None) => {
                return error_response(
                    404,
                    "commit_report_not_found",
                    "The schedule commit report does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if report.channel_id != channel_id {
            return error_response(
                404,
                "commit_report_not_found",
                "The schedule commit report does not exist for this channel.",
            );
        }
        if report.dispatch_status != DispatchStatus::Acknowledged {
            return error_response(
                409,
                "schedule_not_on_air",
                "Caption proof requires an acknowledged on-air schedule report.",
            );
        }
        let item = match self.storage.read_schedule_item(&report.schedule_item_id) {
            Ok(Some(item)) => item,
            Ok(None) => {
                return error_response(
                    409,
                    "schedule_binding_changed",
                    "The committed schedule item no longer exists.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if item.state != ScheduleState::Committed
            || item.revision != report.schedule_item_revision
            || item.channel_id != report.channel_id
            || item.asset_id != report.asset_id
        {
            return error_response(
                409,
                "schedule_binding_changed",
                "The committed schedule binding has changed.",
            );
        }
        let current_asset_offset_ms = match item.window_at(now) {
            Ok(ScheduleWindow::Active { media_offset_ms }) => media_offset_ms,
            Ok(ScheduleWindow::Future | ScheduleWindow::Ended) => {
                return error_response(
                    409,
                    "schedule_not_on_air",
                    "The committed schedule item is not currently active.",
                );
            }
            Err(_) => {
                return error_response(
                    409,
                    "schedule_binding_changed",
                    "The committed schedule window is invalid.",
                );
            }
        };
        let runtime = match self.storage.read_channel_runtime(channel_id) {
            Ok(Some(runtime)) => runtime,
            Ok(None) => {
                return error_response(
                    409,
                    "runtime_not_on_air",
                    "The channel has no current on-air runtime observation.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let runtime_age_ms = now.checked_sub(runtime.heartbeat_at_unix_ms);
        if runtime.state != ChannelRuntimeState::OnAir
            || runtime.on_air_asset_id.as_deref() != Some(report.asset_id.as_str())
            || runtime_age_ms.is_none_or(|age| age > CAPTION_PROOF_RUNTIME_MAX_AGE_MS)
        {
            return error_response(
                409,
                "runtime_not_on_air",
                "Caption proof requires a fresh runtime observation of the exact scheduled asset on air.",
            );
        }
        let asset = match self.storage.read_media_asset(&report.asset_id) {
            Ok(Some(asset)) if asset.readiness == AssetReadiness::Ready => asset,
            Ok(Some(_)) => {
                return error_response(
                    409,
                    "asset_not_ready",
                    "The on-air asset is not currently ready.",
                );
            }
            Ok(None) => {
                return error_response(409, "asset_not_ready", "The on-air asset does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let policy = match self.storage.read_channel_caption_policy(channel_id) {
            Ok(Some(policy)) if policy.mode == ChannelCaptionMode::Cea708 => policy,
            Ok(_) => {
                return error_response(
                    409,
                    "caption_policy_not_enabled",
                    "The channel does not have an enabled CEA-708 caption policy.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let tracks = match self.storage.list_asset_caption_tracks(&report.asset_id) {
            Ok(tracks) => tracks,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let Some(track) = tracks.into_iter().find(|track| {
            track.language == policy.language && track.service_number == policy.service_number
        }) else {
            return error_response(
                409,
                "caption_track_not_ready",
                "The on-air asset has no reviewed track matching the active caption policy.",
            );
        };
        let earliest_capture_offset_ms =
            match current_asset_offset_ms.checked_add(CAPTION_PROOF_EXECUTION_LEAD_MS) {
                Some(value) => value,
                None => {
                    return error_response(
                        409,
                        "capture_window_unavailable",
                        "No bounded future caption capture window remains in the on-air asset.",
                    );
                }
            };
        let mut capture_offsets = Vec::with_capacity(track.cues.len() + 1);
        capture_offsets.push(earliest_capture_offset_ms);
        capture_offsets.extend(
            track
                .cues
                .iter()
                .map(|cue| cue.start_ms)
                .filter(|start| *start > earliest_capture_offset_ms),
        );
        let schedule_media_duration_ms = item.duration_ms.min(asset.duration_ms);
        let selected_window = capture_offsets.into_iter().find_map(|capture_offset| {
            let capture_end = capture_offset.checked_add(request.capture_duration_ms)?;
            if capture_end > schedule_media_duration_ms {
                return None;
            }
            expected_caption_evidence_for_window(
                &track,
                asset.duration_ms,
                capture_offset,
                request.capture_duration_ms,
            )
            .ok()
            .map(|expected| (capture_offset, expected))
        });
        let Some((capture_started_asset_offset_ms, expected)) = selected_window else {
            return error_response(
                409,
                "capture_window_unavailable",
                "No bounded future capture window with expected captions remains in the on-air asset.",
            );
        };

        let station_box = match self.station_box_profile_uncached() {
            Ok(profile) => profile,
            Err(message) => return error_response(500, "readiness_probe_failed", &message),
        };
        if !station_box.release.candidate_verified {
            return error_response(
                409,
                "candidate_not_verified",
                "A verified installed candidate is required before requesting caption proof.",
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
        let source_commit = source_commit.to_owned();
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
        if !snapshot
            .output_sinks
            .iter()
            .any(|sink| sink.sink_id == request.sink_id)
        {
            return error_response(
                409,
                "sink_not_enabled",
                "The requested sink is not an enabled UDP or SRT output.",
            );
        }
        let job_id = match secure_canonical_uuid() {
            Ok(value) => value,
            Err(message) => return error_response(500, "entropy_unavailable", &message),
        };
        let job = CaptionEgressProofJob {
            job_id,
            channel_id: channel_id.into(),
            sink_id: request.sink_id,
            report_id: report.report_id,
            asset_id: report.asset_id,
            track_id: track.track_id,
            track_revision: track.revision,
            cues_sha256: track.cues_sha256,
            caption_policy_revision: policy.revision,
            language: policy.language,
            service_number: policy.service_number,
            candidate_version: station_box.product_version,
            source_commit,
            configuration_hash: snapshot.configuration_hash,
            capture_started_asset_offset_ms,
            capture_duration_ms: request.capture_duration_ms,
            max_capture_bytes: request.max_capture_bytes,
            expected_cue_count: expected.cue_count,
            expected_text_sha256: expected.text_sha256,
            created_at_unix_ms: now,
        };
        let created = match self.storage.create_caption_egress_job(&job) {
            Ok(created) => created,
            Err(message) => return caption_job_storage_error(&message),
        };
        if !created {
            return error_response(
                409,
                "job_already_active",
                "An active caption proof job already exists for this channel and sink.",
            );
        }
        let record = match self.storage.read_caption_egress_job(&job.job_id) {
            Ok(Some(record)) => record,
            Ok(None) => {
                return error_response(500, "storage_error", "The created job disappeared.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        json_response(
            202,
            serde_json::to_vec(&caption_job_response(record))
                .expect("serializing caption proof job response cannot fail"),
        )
    }

    fn channel_caption_proof(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path_identity(path, "/api/v1/channels/", "/caption-proof") else {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        };
        if !is_api_uuid(channel_id) {
            return error_response(400, "invalid_path", "The channel identity is invalid.");
        }
        match self
            .storage
            .read_channel_loudness_proof_snapshot(channel_id)
        {
            Ok(Some(_)) => {}
            Ok(None) => {
                return error_response(404, "channel_not_found", "The channel does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        }
        let proofs = match self
            .storage
            .list_caption_egress_proofs(channel_id, None, 100)
        {
            Ok(proofs) => proofs,
            Err(message) => return error_response(500, "storage_error", &message),
        };
        json_response(
            200,
            serde_json::to_vec(&ChannelCaptionProofResponse {
                channel_id: channel_id.into(),
                proofs,
            })
            .expect("serializing caption proof history cannot fail"),
        )
    }

    fn channel_caption_proof_job(&self, path: &str) -> ApiResponse {
        let Some((channel_id, job_id)) = caption_job_path(path) else {
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
        let record = match self.storage.read_caption_egress_job(job_id) {
            Ok(Some(record)) => record,
            Ok(None) => {
                return error_response(
                    404,
                    "job_not_found",
                    "The caption proof job does not exist.",
                );
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        if record.job.channel_id != channel_id {
            return error_response(
                404,
                "job_not_found",
                "The caption proof job does not exist.",
            );
        }
        json_response(
            200,
            serde_json::to_vec(&caption_job_response(record))
                .expect("serializing caption proof job response cannot fail"),
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
        let current_candidate = station_box
            .source_commit
            .as_deref()
            .filter(|_| station_box.release.candidate_verified)
            .map(|source_commit| (station_box.product_version.as_str(), source_commit));
        let collected = match collect_headend_probe(
            &self.storage,
            generated_at_unix_ms,
            channel_id,
            current_candidate,
        ) {
            Ok(Some(collected)) => collected,
            Ok(None) => {
                return error_response(404, "channel_not_found", "The channel does not exist.");
            }
            Err(message) => return error_response(500, "storage_error", &message),
        };
        let CollectedHeadendProbe {
            probe,
            receiver_evidence,
        } = collected;
        match derive_headend_readiness(&station_box, probe) {
            Ok(mut readiness) => {
                for channel in &mut readiness.channels {
                    for sink in &mut channel.sinks {
                        if let Some(evidence) = receiver_evidence
                            .get(&receiver_evidence_key(&channel.channel_id, &sink.sink_id))
                        {
                            sink.external_receiver_evidence = Some(evidence.clone());
                        }
                    }
                }
                json_response(
                    200,
                    serde_json::to_vec(&readiness)
                        .expect("serializing headend readiness cannot fail"),
                )
            }
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
        #[cfg(test)]
        if let Some(hook) = &self.commissioning_after_channel_read_hook {
            hook(&self.storage);
        }
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
        let mut fallback_slates = Vec::with_capacity(enabled.len());
        for channel in &enabled {
            let selection = match self
                .storage
                .read_channel_fallback_slate(&channel.channel_id)
            {
                Ok(selection) => selection,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            fallback_slates.push((channel, selection));
        }
        let ready_fallback_count = fallback_slates
            .iter()
            .filter(|(_, selection)| {
                selection
                    .as_ref()
                    .is_some_and(|value| value.image_asset.readiness == CgImageReadiness::Ready)
            })
            .count();
        let runtime_is_green = safe_to_air
            .as_ref()
            .is_some_and(|status| status.color == SafeToAirColor::Green);
        let fallback_slate_pass =
            !enabled.is_empty() && ready_fallback_count == enabled.len() && runtime_is_green;
        let fallback_evidence = fallback_slates
            .iter()
            .map(|(channel, selection)| match selection {
                Some(selection) => format!(
                    "{}=revision {} image {} ({:?})",
                    channel.channel_id,
                    selection.revision,
                    &selection.image_asset.asset_id[..12],
                    selection.image_asset.readiness
                ),
                None => format!("{}=missing", channel.channel_id),
            })
            .collect::<Vec<_>>()
            .join("; ");
        checks.push(commissioning_check(
            "fallback_slate",
            "Fallback output and configured station slate",
            CommissioningPhase::SourcesOutputs,
            if fallback_slate_pass {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("channels"),
            format!(
                "{} of {} enabled channel(s) have a current verified fallback slate. {}",
                ready_fallback_count,
                enabled.len(),
                safe_to_air.as_ref().map_or(
                    "No enabled runtime is available.".into(),
                    |status| status.operator_message.clone()
                )
            ),
            if fallback_evidence.is_empty() {
                "no enabled channel has fallback-slate runtime evidence".into()
            } else {
                format!(
                    "durable channel fallback-slate authority plus current heartbeat-derived runtime: {fallback_evidence}"
                )
            },
            CommissioningBoundary::LocalAuthority,
        ));
        let current_caption_proof = match station_box
            .source_commit
            .as_deref()
            .filter(|_| station_box.release.candidate_verified)
        {
            Some(source_commit) => match current_cea708_caption_proof(
                &self.storage,
                &channels,
                &station_box.product_version,
                source_commit,
            ) {
                Ok(proof) => proof,
                Err(message) => return error_response(500, "storage_error", &message),
            },
            None => None,
        };
        checks.push(commissioning_check(
            "cea708_captions",
            "CEA-708 caption path",
            CommissioningPhase::SourcesOutputs,
            if current_caption_proof.is_some() {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("channels"),
            current_caption_proof.as_ref().map_or_else(
                || {
                    "No verified CEA-708 egress proof matches an enabled channel's current policy, track, acknowledged dispatch, transport configuration, and installed candidate.".into()
                },
                |proof| {
                    format!(
                        "Verified proof {} establishes the current CEA-708 policy, caption track, acknowledged dispatch, and decoded output path for enabled channel {} sink {}.",
                        proof.proof_id, proof.channel_id, proof.sink_id
                    )
                },
            ),
            current_caption_proof.as_ref().map_or_else(
                || {
                    "requires one verified CEA-708 proof with exact current candidate, output configuration, policy, track, dispatch, and decode-back evidence".into()
                },
                |proof| {
                    format!(
                        "proof {} binds candidate {}@{}, configuration {}, policy revision {}, track {} revision {}, report {}, and verified independent CEA-708 decode-back",
                        proof.proof_id,
                        proof.candidate_version,
                        proof.source_commit,
                        proof.configuration_hash,
                        proof.caption_policy_revision,
                        proof.track_id,
                        proof.track_revision,
                        proof.report_id,
                    )
                },
            ),
            CommissioningBoundary::LocalAuthority,
        ));

        let publication_readiness = match station_box
            .source_commit
            .as_deref()
            .filter(|_| station_box.release.candidate_verified)
        {
            Some(source_commit) => {
                let candidate = PublishCandidateIdentity {
                    version: station_box.product_version.clone(),
                    source_commit: source_commit.into(),
                };
                match self
                    .storage
                    .derive_current_publication_readiness(&candidate, generated_at_unix_ms)
                {
                    Ok(readiness) => Some(readiness),
                    Err(message) => return error_response(500, "storage_error", &message),
                }
            }
            None => None,
        };
        let publish_targets_pass = publication_readiness.as_ref().is_some_and(|readiness| {
            readiness.canonical_portal == RequiredTierStatus::Passed
                && readiness.permanence == RequiredTierStatus::Passed
                && readiness.reach_capacity == RequiredTierStatus::Passed
        });
        let execution_readiness = self.database_path.parent().and_then(|data_root| {
            let fence = acquire_install_authority_fence(data_root).ok()?;
            if fence.was_abandoned() {
                return None;
            }
            let candidate =
                read_verified_install_authority(data_root, &self.application_directory).ok()?;
            self.storage
                .derive_current_publish_execution_readiness(
                    candidate.version(),
                    candidate.source_commit(),
                    candidate.install_generation(),
                )
                .ok()
        });
        let publish_execution_pass = execution_readiness.as_ref().is_some_and(|readiness| {
            readiness.canonical_portal
                && readiness.internet_archive
                && readiness.local_nas
                && readiness.reach_capacity
        });
        checks.push(commissioning_check(
            "publish_targets",
            "Required publish targets",
            CommissioningPhase::SourcesOutputs,
            if publish_targets_pass && publish_execution_pass { CommissioningCheckStatus::Pass } else { CommissioningCheckStatus::Blocked },
            true,
            Some("publishing"),
            publication_readiness.as_ref().map_or_else(
                || "No current-candidate publish-target connection authority is available because the installed candidate is not verified.".into(),
                |readiness| format!(
                    "Current publish-target authority: canonical portal={:?}; permanence={:?}; reach capacity={:?}; {} target record(s). Exact provider execution is proven for {} current target(s).",
                    readiness.canonical_portal, readiness.permanence, readiness.reach_capacity, readiness.targets.len(), execution_readiness.as_ref().map_or(0, |execution| execution.proven_target_count)
                ),
            ),
            publication_readiness.as_ref().map_or_else(
                || "requires a verified installed candidate plus current account, target, connection-test, and provider-execution receipts; it never claims audience reach".into(),
                |readiness| format!(
                    "current-candidate connection-test authority plus exact current provider execution receipts: portal={:?}; permanence={:?}; reach_capacity={:?}; it does not claim audience reach",
                    readiness.canonical_portal, readiness.permanence, readiness.reach_capacity
                ),
            ),
            CommissioningBoundary::LocalAuthority,
        ));

        let automation_readiness = match station_box
            .source_commit
            .as_deref()
            .filter(|_| station_box.release.candidate_verified)
        {
            Some(source_commit) => {
                let candidate = AutomationCandidateIdentity {
                    version: station_box.product_version.clone(),
                    source_commit: source_commit.into(),
                };
                let adapters = match self.storage.list_automation_adapter_descriptors() {
                    Ok(adapters) => adapters,
                    Err(message) => return error_response(500, "storage_error", &message),
                };
                let policies = match self.storage.list_automation_feature_policies() {
                    Ok(policies) => policies,
                    Err(message) => return error_response(500, "storage_error", &message),
                };
                let mut evidence = Vec::with_capacity(policies.len());
                for policy in &policies {
                    match self
                        .storage
                        .list_automation_connection_test_evidence(&policy.policy_id, 1)
                    {
                        Ok(mut records) => evidence.append(&mut records),
                        Err(message) => return error_response(500, "storage_error", &message),
                    }
                }
                match derive_automation_readiness(
                    &adapters,
                    &policies,
                    &evidence,
                    &candidate,
                    generated_at_unix_ms,
                ) {
                    Ok(readiness) => Some(readiness),
                    Err(message) => return error_response(500, "storage_error", &message),
                }
            }
            None => None,
        };
        let automation_policy_pass = automation_readiness.as_ref().is_some_and(|readiness| {
            readiness.overall == station_automation_policy::AutomationReadinessStatus::Ready
        });
        checks.push(commissioning_check(
            "automation_model_policy",
            "Automation and model policy",
            CommissioningPhase::SourcesOutputs,
            if automation_policy_pass { CommissioningCheckStatus::Pass } else { CommissioningCheckStatus::Blocked },
            true,
            Some("automation"),
            automation_readiness.as_ref().map_or_else(
                || "No current-candidate automation connection authority is available because the installed candidate is not verified.".into(),
                |readiness| format!(
                    "Current automation authority: captions={:?}; summaries={:?}; translation={:?}; overall={:?}.",
                    readiness.captions, readiness.summaries, readiness.translation, readiness.overall
                ),
            ),
            automation_readiness.as_ref().map_or_else(
                || "requires a verified installed candidate and all feature policies with connection tests only; this does not claim model execution or output quality".into(),
                |readiness| format!(
                    "current-candidate connection tests only: captions={:?}; summaries={:?}; translation={:?}; it does not claim model execution or output quality",
                    readiness.captions, readiness.summaries, readiness.translation
                ),
            ),
            CommissioningBoundary::LocalAuthority,
        ));

        let receiver_candidate = station_box
            .source_commit
            .as_deref()
            .filter(|_| station_box.release.candidate_verified)
            .map(|source_commit| (station_box.product_version.as_str(), source_commit));
        let headend_probe = match collect_headend_probe(
            &self.storage,
            generated_at_unix_ms,
            None,
            receiver_candidate,
        ) {
            Ok(Some(collected)) => collected.probe,
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
        let (cable_transport_pass, cable_transport_detail) = if let Some((
            candidate_version,
            source_commit,
        )) = receiver_candidate
        {
            let validations = match self.storage.read_current_cable_transport_validations(
                candidate_version,
                source_commit,
                generated_at_unix_ms,
            ) {
                Ok(validations) => validations,
                Err(message) => return error_response(500, "storage_error", &message),
            };
            aggregate_cable_transport_snapshot(&validations)
        } else {
            let detail = "The installed candidate identity is unavailable, so transport evidence cannot be bound.";
            (false, vec![detail.into()])
        };
        checks.push(commissioning_check(
            "cable_transport_validation",
            "Cable transport analysis and device reachability",
            CommissioningPhase::SourcesOutputs,
            if cable_transport_pass {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("channels"),
            if cable_transport_detail.is_empty() {
                "No enabled channel has a current cable transport validation.".into()
            } else {
                cable_transport_detail.join("; ")
            },
            "durable transport analysis and authenticated target-device reachability receipts bound to the current candidate and output configuration",
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
        let current_candidate = if station_box.release.candidate_verified {
            station_box
                .source_commit
                .clone()
                .map(|source_commit| CandidateIdentity {
                    version: station_box.product_version.clone(),
                    source_commit: Some(source_commit),
                })
        } else {
            None
        };
        let mut ten_minute_sink_proof: Option<StoredSinkLoudnessProof> = None;
        if let Some(candidate) = current_candidate.as_ref() {
            for channel in &enabled {
                let snapshot = match self
                    .storage
                    .read_channel_loudness_proof_snapshot(&channel.channel_id)
                {
                    Ok(Some(snapshot)) => snapshot,
                    Ok(None) => continue,
                    Err(message) => return error_response(500, "storage_error", &message),
                };
                for stored in snapshot.latest_proofs {
                    if stored.proof.sample_duration_ms >= 600_000
                        && stored.proof.effective_status(
                            candidate,
                            &snapshot.configuration_hash,
                            generated_at_unix_ms,
                            LOUDNESS_PROOF_MAX_AGE_MS,
                        ) == EffectiveLoudnessStatus::Pass
                        && ten_minute_sink_proof.as_ref().is_none_or(|current| {
                            stored.proof.captured_at_unix_ms > current.proof.captured_at_unix_ms
                        })
                    {
                        ten_minute_sink_proof = Some(stored);
                    }
                }
            }
        }
        checks.push(commissioning_check(
            "selected_sink_ten_minute_proof",
            "Ten-minute selected-sink output proof",
            CommissioningPhase::GuidedAirProof,
            if ten_minute_sink_proof.is_some() {
                CommissioningCheckStatus::Pass
            } else {
                CommissioningCheckStatus::Blocked
            },
            true,
            Some("health"),
            ten_minute_sink_proof.as_ref().map_or_else(
                || "No current passing ten-minute proof exists for an enabled selected output sink on the verified installed candidate and exact output configuration.".into(),
                |stored| format!(
                    "Selected sink {} on channel {} passed a {} ms exact-branch output proof with {} captured bytes.",
                    stored.proof.sink_id,
                    stored.proof.channel_id,
                    stored.proof.sample_duration_ms,
                    stored.proof.capture_bytes
                ),
            ),
            ten_minute_sink_proof.as_ref().map_or_else(
                || "no current candidate/configuration-bound passing selected-sink proof covers at least 600000 ms".into(),
                |stored| format!(
                    "append-only proof {} binds selected sink {}, candidate {}, configuration {}, {} ms, and SHA-256 {}",
                    stored.proof.proof_id,
                    stored.proof.sink_id,
                    stored.proof.candidate_version,
                    stored.proof.configuration_hash,
                    stored.proof.sample_duration_ms,
                    stored.proof.capture_sha256
                ),
            ),
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

    fn put_schedule_item(
        &self,
        body: Option<&[u8]>,
        actor: Option<&OperatorAccount>,
    ) -> ApiResponse {
        let Some(actor) = recording_operator(actor) else {
            return error_response(
                403,
                "schedule_operator_required",
                "Changing schedule items requires an enabled Setup Admin or Meeting Operator account.",
            );
        };
        let Some(item) = parse_body::<ScheduleItem>(body) else {
            return error_response(400, "invalid_json", "A schedule item body is required.");
        };
        if let Err(error) = item.validate() {
            return error_response(422, "validation_failed", &format!("{error:?}"));
        }
        if item.state != ScheduleState::Draft {
            return error_response(
                409,
                "schedule_commit_route_required",
                "Create or edit schedule items as drafts, then use the audited schedule commit route.",
            );
        }
        let occurred_at_unix_ms = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.write_schedule_item(&ScheduleItemWrite {
            item: item.clone(),
            changed_by: actor.actor_id,
            occurred_at_unix_ms,
        }) {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&item).expect("serializing a schedule item cannot fail"),
            ),
            Err(message) if message.contains("compare-and-set revision") => {
                error_response(409, "revision_conflict", &message)
            }
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
        let Some(actor) = recording_operator(actor) else {
            return error_response(
                403,
                "schedule_operator_required",
                "Committing schedule items requires an enabled Setup Admin or Meeting Operator account.",
            );
        };
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
            &actor.actor_id,
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

fn is_automation_reader(actor: Option<&OperatorAccount>) -> bool {
    actor.is_some_and(|account| {
        account.enabled()
            && (account
                .roles()
                .contains(&station_auth::OperatorRole::SetupAdmin)
                || account
                    .roles()
                    .contains(&station_auth::OperatorRole::MeetingOperator))
    })
}

fn automation_execution_job_status_response(
    record: &AutomationExecutionJobRecord,
) -> AutomationExecutionJobStatusResponse<'_> {
    let state = match record.state {
        AutomationExecutionJobState::Pending => "pending",
        AutomationExecutionJobState::Claimed => "claimed",
        AutomationExecutionJobState::Completed => "completed",
        AutomationExecutionJobState::Failed => "failed",
    };
    AutomationExecutionJobStatusResponse {
        request: &record.request,
        state,
        attempt_count: record.attempt_count,
        lease_expires_at_unix_ms: record.lease_expires_at_unix_ms,
        receipt: record.receipt.as_ref(),
        last_failure_code: record.last_failure_code.as_deref(),
        created_at_unix_ms: record.created_at_unix_ms,
        next_attempt_at_unix_ms: record.next_attempt_at_unix_ms,
        completed_at_unix_ms: record.completed_at_unix_ms,
    }
}

fn is_publishing_reader(actor: Option<&OperatorAccount>) -> bool {
    is_automation_reader(actor)
}

fn publishing_reader_required() -> ApiResponse {
    error_response(
        403,
        "operator_required",
        "Reading publishing authority requires an enabled Setup Admin or Meeting Operator account.",
    )
}

fn next_authority_revision(expected_revision: Option<u64>) -> Result<u64, ApiResponse> {
    match expected_revision {
        Some(revision) => revision.checked_add(1).ok_or_else(|| {
            error_response(
                422,
                "validation_failed",
                "The expected revision is invalid.",
            )
        }),
        None => Ok(1),
    }
}

fn publishing_write_error_response(error: PublishingAuthorityWriteError) -> ApiResponse {
    match error {
        PublishingAuthorityWriteError::Conflict { current_revision } => error_response(
            409,
            "revision_conflict",
            &format!("The publish authority changed; current revision is {current_revision:?}."),
        ),
        PublishingAuthorityWriteError::Rebound => error_response(
            409,
            "authority_rebound",
            "A publish authority identity cannot be rebound to another account or target kind.",
        ),
        PublishingAuthorityWriteError::Invalid(message) => {
            error_response(422, "validation_failed", &message)
        }
        PublishingAuthorityWriteError::Storage(message) => {
            error_response(500, "storage_error", &message)
        }
    }
}

fn is_scheduling_reader(actor: Option<&OperatorAccount>) -> bool {
    actor.is_some_and(|account| {
        account.enabled()
            && (account
                .roles()
                .contains(&station_auth::OperatorRole::SetupAdmin)
                || account
                    .roles()
                    .contains(&station_auth::OperatorRole::MeetingOperator)
                || account
                    .roles()
                    .contains(&station_auth::OperatorRole::SupportAdmin))
    })
}

fn scheduling_writer(actor: Option<&OperatorAccount>) -> Option<&OperatorAccount> {
    actor.filter(|account| {
        account.enabled()
            && (account
                .roles()
                .contains(&station_auth::OperatorRole::SetupAdmin)
                || account
                    .roles()
                    .contains(&station_auth::OperatorRole::MeetingOperator))
    })
}

fn is_setup_admin(actor: &OperatorAccount) -> bool {
    actor
        .roles()
        .contains(&station_auth::OperatorRole::SetupAdmin)
}

fn scheduling_reader_required() -> ApiResponse {
    error_response(
        403,
        "scheduling_operator_required",
        "Reading scheduling automation requires an enabled Setup Admin, Meeting Operator, or Support Admin account.",
    )
}

fn scheduling_writer_required() -> ApiResponse {
    error_response(
        403,
        "scheduling_writer_required",
        "Changing scheduling automation requires the Setup Admin or Meeting Operator role.",
    )
}

fn scheduling_invalid_path() -> ApiResponse {
    error_response(
        400,
        "invalid_path",
        "The scheduling automation path or bounded query is invalid.",
    )
}

fn scheduling_next_revision(expected_revision: Option<u64>) -> Result<u64, String> {
    expected_revision.map_or(Ok(1), |revision| {
        revision
            .checked_add(1)
            .ok_or_else(|| "The expected revision is invalid.".to_string())
    })
}

fn scheduling_not_found(authority: &str) -> ApiResponse {
    let (code, message) = match authority {
        "saved_search" => ("saved_search_not_found", "The saved search does not exist."),
        "daypart_block" => (
            "daypart_block_not_found",
            "The daypart block does not exist.",
        ),
        "recurring_rule" => (
            "recurring_rule_not_found",
            "The recurring rule does not exist.",
        ),
        "channel" => ("channel_not_found", "The channel does not exist."),
        _ => (
            "scheduling_authority_not_found",
            "The required scheduling authority does not exist.",
        ),
    };
    error_response(404, code, message)
}

fn scheduling_write_error_response(
    error: ScheduleAutomationWriteError,
    resource: &str,
) -> ApiResponse {
    match error {
        ScheduleAutomationWriteError::Invalid(message) => {
            error_response(422, "validation_failed", &message)
        }
        ScheduleAutomationWriteError::Conflict { current_revision } => error_response(
            409,
            "revision_conflict",
            &format!("The scheduling authority changed; current revision is {current_revision:?}."),
        ),
        ScheduleAutomationWriteError::DependencyConflict {
            authority,
            requested_revision,
            current_revision,
        } => error_response(
            409,
            "authority_revision_conflict",
            &format!(
                "The {authority} dependency changed; requested revision {requested_revision}, current revision {current_revision:?}."
            ),
        ),
        ScheduleAutomationWriteError::NotFound { authority } => scheduling_not_found(authority),
        ScheduleAutomationWriteError::Rebound => error_response(
            409,
            "scheduling_authority_rebound",
            "An existing scheduling authority identity cannot be rebound.",
        ),
        ScheduleAutomationWriteError::Capacity => error_response(
            422,
            &format!("{resource}_capacity_exceeded"),
            "Scheduling automation has reached its bounded authority capacity.",
        ),
        ScheduleAutomationWriteError::Storage(message) => {
            error_response(500, "storage_error", &message)
        }
    }
}

fn schedule_materialization_success_response(
    status: u16,
    disposition: ScheduleMaterializationDispositionResponse,
    run: &ScheduleMaterializationRun,
) -> ApiResponse {
    let response = ScheduleMaterializationResponse {
        disposition,
        run_id: &run.run_id,
        request_sha256: &run.request_sha256,
        result_sha256: &run.result_sha256,
        batch_sha256: &run.batch_sha256,
        window: materialization_window_response(&run.window),
        observed_at_utc_ms: run.observed_at_utc_ms,
        changed_by: &run.changed_by,
        occurred_at_unix_ms: run.occurred_at_unix_ms,
        plans: run
            .plans
            .iter()
            .map(schedule_materialization_plan_response)
            .collect(),
        outcomes: run
            .outcomes
            .iter()
            .map(schedule_materialization_outcome_record_response)
            .collect(),
    };
    json_response(
        status,
        serde_json::to_vec(&response)
            .expect("serializing a schedule materialization response cannot fail"),
    )
}

fn schedule_materialization_history_success_response(
    channel_id: &str,
    entries: Vec<ScheduleMaterializationHistoryEntry>,
    next_cursor: Option<ScheduleMaterializationHistoryCursor>,
) -> ApiResponse {
    let response = ScheduleMaterializationHistoryResponse {
        entries: entries
            .iter()
            .map(|entry| schedule_materialization_history_entry_response(channel_id, entry))
            .collect(),
        next_cursor: next_cursor.map(|cursor| ScheduleMaterializationHistoryCursorResponse {
            occurred_at_unix_ms: cursor.occurred_at_unix_ms,
            run_id: cursor.run_id,
        }),
    };
    json_response(
        200,
        serde_json::to_vec(&response)
            .expect("serializing materialization history response cannot fail"),
    )
}

fn schedule_materialization_history_entry_response<'a>(
    channel_id: &'a str,
    entry: &'a ScheduleMaterializationHistoryEntry,
) -> ScheduleMaterializationHistoryEntryResponse<'a> {
    let run = &entry.run;
    ScheduleMaterializationHistoryEntryResponse {
        run_id: &run.run_id,
        channel_id,
        origin: match entry.origin {
            ScheduleMaterializationRunOrigin::Automatic => {
                ScheduleMaterializationHistoryOriginResponse::Automatic
            }
            ScheduleMaterializationRunOrigin::Operator => {
                ScheduleMaterializationHistoryOriginResponse::Operator
            }
        },
        actor_id: &run.changed_by,
        request_sha256: &run.request_sha256,
        result_sha256: &run.result_sha256,
        window: materialization_window_response(&run.window),
        observed_at_utc_ms: run.observed_at_utc_ms,
        occurred_at_unix_ms: run.occurred_at_unix_ms,
        checkpoint_utc_day: run.occurred_at_unix_ms / 86_400_000,
        plans: run
            .plans
            .iter()
            .map(schedule_materialization_history_plan_response)
            .collect(),
        outcomes: run
            .outcomes
            .iter()
            .map(schedule_materialization_history_outcome_response)
            .collect(),
        generated_drafts: entry
            .generated_drafts
            .iter()
            .map(|draft| ScheduleMaterializationGeneratedDraftResponse {
                occurrence_id: &draft.occurrence_id,
                schedule_item_id: &draft.schedule_item_id,
                current_schedule: &draft.current_schedule,
            })
            .collect(),
    }
}

fn schedule_materialization_history_plan_response(
    plan: &ScheduleMaterializationPlanRecord,
) -> ScheduleMaterializationHistoryPlanResponse<'_> {
    ScheduleMaterializationHistoryPlanResponse {
        ordinal: plan.ordinal,
        rule_id: &plan.rule_id,
        rule_revision: plan.rule_revision,
        channel_id: &plan.channel_id,
        saved_search_id: &plan.saved_search_id,
        saved_search_revision: plan.saved_search_revision,
        block_id: &plan.block_id,
        block_revision: plan.block_revision,
        window: materialization_window_response(&plan.window),
    }
}

fn schedule_materialization_history_outcome_response(
    record: &ScheduleMaterializationOutcomeRecord,
) -> ScheduleMaterializationHistoryOutcomeResponse<'_> {
    ScheduleMaterializationHistoryOutcomeResponse {
        ordinal: record.ordinal,
        rule_id: &record.rule_id,
        rule_revision: record.rule_revision,
        channel_id: &record.channel_id,
        saved_search_id: &record.saved_search_id,
        saved_search_revision: record.saved_search_revision,
        block_id: &record.block_id,
        block_revision: record.block_revision,
        plan_window: materialization_window_response(&record.plan_window),
        materialization_id: record.materialization_id.as_deref(),
        compiled_outcome: schedule_materialization_outcome_response(&record.compiled_outcome),
        outcome: schedule_materialization_outcome_response(&record.outcome),
    }
}

fn materialization_window_response(
    window: &MaterializationWindow,
) -> MaterializationWindowResponse {
    MaterializationWindowResponse {
        starts_at_utc_ms: window.starts_at_utc_ms,
        ends_at_utc_ms: window.ends_at_utc_ms,
    }
}

fn existing_schedule_snapshot_response(
    existing: &ExistingScheduleSnapshot,
) -> ExistingScheduleSnapshotResponse<'_> {
    ExistingScheduleSnapshotResponse {
        schedule_item_id: &existing.schedule_item_id,
        revision: existing.revision,
        channel_id: &existing.channel_id,
        asset_sha256: &existing.asset_sha256,
        title: &existing.title,
        starts_at_utc_ms: existing.starts_at_utc_ms,
        duration_ms: existing.duration_ms,
        state: &existing.state,
    }
}

fn schedule_materialization_outcome_response(
    outcome: &ScheduleMaterializationOutcome,
) -> ScheduleMaterializationOutcomeResponse<'_> {
    match outcome {
        ScheduleMaterializationOutcome::Proposed {
            occurrence_id,
            schedule_item_id,
            asset_sha256,
            asset_title,
            starts_at_utc_ms,
            ends_at_utc_ms,
        } => ScheduleMaterializationOutcomeResponse::Proposed {
            occurrence_id,
            schedule_item_id,
            asset_sha256,
            asset_title,
            starts_at_utc_ms: *starts_at_utc_ms,
            ends_at_utc_ms: *ends_at_utc_ms,
        },
        ScheduleMaterializationOutcome::NoMatch { starts_at_utc_ms } => {
            ScheduleMaterializationOutcomeResponse::NoMatch {
                starts_at_utc_ms: *starts_at_utc_ms,
            }
        }
        ScheduleMaterializationOutcome::Blocked { starts_at_utc_ms } => {
            ScheduleMaterializationOutcomeResponse::Blocked {
                starts_at_utc_ms: *starts_at_utc_ms,
            }
        }
        ScheduleMaterializationOutcome::SuppressedByPrecedence {
            starts_at_utc_ms,
            winner_rule_id,
            candidate_priority,
            winner_priority,
            reason,
        } => ScheduleMaterializationOutcomeResponse::SuppressedByPrecedence {
            starts_at_utc_ms: *starts_at_utc_ms,
            winner_rule_id,
            candidate_priority: *candidate_priority,
            winner_priority: *winner_priority,
            reason,
        },
        ScheduleMaterializationOutcome::ExistingAutomationDraftUnchanged {
            occurrence_id,
            schedule_item_id,
            existing,
        } => ScheduleMaterializationOutcomeResponse::ExistingAutomationDraftUnchanged {
            occurrence_id,
            schedule_item_id,
            existing: existing_schedule_snapshot_response(existing),
        },
        ScheduleMaterializationOutcome::ExistingAutomationDraftChanged {
            occurrence_id,
            schedule_item_id,
            existing,
        } => ScheduleMaterializationOutcomeResponse::ExistingAutomationDraftChanged {
            occurrence_id,
            schedule_item_id,
            existing: existing_schedule_snapshot_response(existing),
        },
        ScheduleMaterializationOutcome::ExistingCommitted {
            occurrence_id,
            schedule_item_id,
            existing,
        } => ScheduleMaterializationOutcomeResponse::ExistingCommitted {
            occurrence_id,
            schedule_item_id,
            existing: existing_schedule_snapshot_response(existing),
        },
        ScheduleMaterializationOutcome::CancelledOccurrence {
            occurrence_id,
            schedule_item_id,
            existing,
        } => ScheduleMaterializationOutcomeResponse::CancelledOccurrence {
            occurrence_id,
            schedule_item_id,
            existing: existing_schedule_snapshot_response(existing),
        },
        ScheduleMaterializationOutcome::AlreadyAired {
            occurrence_id,
            schedule_item_id,
            existing,
        } => ScheduleMaterializationOutcomeResponse::AlreadyAired {
            occurrence_id,
            schedule_item_id,
            existing: existing_schedule_snapshot_response(existing),
        },
        ScheduleMaterializationOutcome::SuppressedByExistingSchedule {
            occurrence_id,
            schedule_item_id,
            existing,
            existing_owner,
        } => ScheduleMaterializationOutcomeResponse::SuppressedByExistingSchedule {
            occurrence_id,
            schedule_item_id,
            existing: existing_schedule_snapshot_response(existing),
            existing_owner,
        },
    }
}

fn schedule_materialization_plan_response(
    plan: &ScheduleMaterializationPlanRecord,
) -> ScheduleMaterializationPlanResponse<'_> {
    ScheduleMaterializationPlanResponse {
        ordinal: plan.ordinal,
        rule_id: &plan.rule_id,
        rule_revision: plan.rule_revision,
        channel_id: &plan.channel_id,
        saved_search_id: &plan.saved_search_id,
        saved_search_revision: plan.saved_search_revision,
        block_id: &plan.block_id,
        block_revision: plan.block_revision,
        window: materialization_window_response(&plan.window),
        source_authority_sha256: &plan.source_authority_sha256,
        candidate_projection_sha256: &plan.candidate_projection_sha256,
        candidate_receipt_sha256: &plan.candidate_receipt_sha256,
        prior_airings_sha256: &plan.prior_airings_sha256,
        prior_receipt_sha256: &plan.prior_receipt_sha256,
        prior_airings_coverage: materialization_window_response(&plan.prior_airings_coverage),
        scope_sha256: &plan.scope_sha256,
        source_input_sha256: &plan.source_input_sha256,
    }
}

fn schedule_materialization_outcome_record_response(
    record: &ScheduleMaterializationOutcomeRecord,
) -> ScheduleMaterializationOutcomeRecordResponse<'_> {
    ScheduleMaterializationOutcomeRecordResponse {
        ordinal: record.ordinal,
        rule_id: &record.rule_id,
        rule_revision: record.rule_revision,
        channel_id: &record.channel_id,
        saved_search_id: &record.saved_search_id,
        saved_search_revision: record.saved_search_revision,
        block_id: &record.block_id,
        block_revision: record.block_revision,
        plan_window: materialization_window_response(&record.plan_window),
        materialization_id: record.materialization_id.as_deref(),
        source_authority_sha256: &record.source_authority_sha256,
        candidate_projection_sha256: &record.candidate_projection_sha256,
        prior_airings_sha256: &record.prior_airings_sha256,
        prior_airings_coverage: materialization_window_response(&record.prior_airings_coverage),
        scope_sha256: &record.scope_sha256,
        source_input_sha256: &record.source_input_sha256,
        compiled_outcome: schedule_materialization_outcome_response(&record.compiled_outcome),
        outcome: schedule_materialization_outcome_response(&record.outcome),
    }
}

fn schedule_materialization_write_error_response(
    error: ScheduleMaterializationWriteError,
) -> ApiResponse {
    match error {
        ScheduleMaterializationWriteError::StaleAuthority(_) => error_response(
            409,
            "materialization_authority_conflict",
            "Schedule materialization authority is unavailable or stale.",
        ),
        ScheduleMaterializationWriteError::Invalid(_) => error_response(
            422,
            "materialization_invalid",
            "Server-derived schedule materialization input is invalid.",
        ),
        ScheduleMaterializationWriteError::Capacity => error_response(
            422,
            "materialization_capacity_exceeded",
            "Schedule materialization exceeded a bounded station capacity.",
        ),
        ScheduleMaterializationWriteError::Collision(_) => error_response(
            500,
            "materialization_integrity_error",
            "Schedule materialization encountered an integrity contradiction and was rolled back.",
        ),
        ScheduleMaterializationWriteError::Storage(_) => error_response(
            500,
            "storage_error",
            "Schedule materialization could not be completed.",
        ),
    }
}

fn schedule_materialization_history_read_error_response(
    error: ScheduleMaterializationHistoryReadError,
) -> ApiResponse {
    match error {
        ScheduleMaterializationHistoryReadError::Invalid(_) => error_response(
            422,
            "materialization_history_invalid",
            "The materialization history request is not valid for the station authority.",
        ),
        ScheduleMaterializationHistoryReadError::Capacity => error_response(
            422,
            "materialization_history_capacity_exceeded",
            "Schedule materialization history exceeded its bounded station capacity.",
        ),
        ScheduleMaterializationHistoryReadError::Integrity(_) => error_response(
            500,
            "materialization_integrity_error",
            "Schedule materialization history encountered an integrity contradiction.",
        ),
        ScheduleMaterializationHistoryReadError::Storage(_) => error_response(
            500,
            "storage_error",
            "Schedule materialization history could not be read safely.",
        ),
    }
}

fn is_catalog_reader(actor: Option<&OperatorAccount>) -> bool {
    actor.is_some_and(|account| {
        account.enabled()
            && (account
                .roles()
                .contains(&station_auth::OperatorRole::SetupAdmin)
                || account
                    .roles()
                    .contains(&station_auth::OperatorRole::MeetingOperator)
                || account
                    .roles()
                    .contains(&station_auth::OperatorRole::SupportAdmin))
    })
}

fn catalog_writer(actor: Option<&OperatorAccount>) -> Option<&OperatorAccount> {
    actor.filter(|account| {
        account.enabled()
            && (account
                .roles()
                .contains(&station_auth::OperatorRole::SetupAdmin)
                || account
                    .roles()
                    .contains(&station_auth::OperatorRole::MeetingOperator))
    })
}

fn catalog_reader_required() -> ApiResponse {
    error_response(
        403,
        "catalog_operator_required",
        "Reading the catalog requires an enabled Setup Admin, Meeting Operator, or Support Admin account.",
    )
}

fn catalog_writer_required() -> ApiResponse {
    error_response(
        403,
        "catalog_writer_required",
        "Changing the catalog requires an enabled Setup Admin or Meeting Operator account.",
    )
}

fn catalog_invalid_path() -> ApiResponse {
    error_response(
        400,
        "invalid_path",
        "The catalog path or bounded query is invalid.",
    )
}

fn catalog_media_asset_not_found() -> ApiResponse {
    error_response(404, "asset_not_found", "The media asset does not exist.")
}

fn catalog_asset_not_found() -> ApiResponse {
    error_response(
        404,
        "catalog_asset_not_found",
        "The catalog asset does not exist.",
    )
}

fn catalog_next_revision(expected_revision: Option<u64>) -> Result<u64, String> {
    expected_revision.map_or(Ok(1), |revision| {
        revision
            .checked_add(1)
            .ok_or_else(|| "The expected catalog revision is invalid.".to_string())
    })
}

fn catalog_revision_conflict(current_revision: Option<u64>) -> ApiResponse {
    error_response(
        409,
        "revision_conflict",
        &format!("The catalog authority changed; current revision is {current_revision:?}."),
    )
}

fn catalog_state_conflict(
    current: CatalogEditorialState,
    requested: CatalogEditorialState,
) -> ApiResponse {
    error_response(
        409,
        "catalog_state_conflict",
        &format!("The catalog transition from {current:?} to {requested:?} is not available."),
    )
}

fn catalog_write_error_response(error: CatalogAssetWriteError) -> ApiResponse {
    match error {
        CatalogAssetWriteError::Invalid(message) => {
            error_response(422, "validation_failed", &message)
        }
        CatalogAssetWriteError::Conflict { current_revision } => {
            catalog_revision_conflict(current_revision)
        }
        CatalogAssetWriteError::Rebound => error_response(
            409,
            "catalog_identity_rebound",
            "An existing catalog asset identity cannot be rebound.",
        ),
        CatalogAssetWriteError::Capacity => error_response(
            422,
            "catalog_capacity_exceeded",
            "The catalog has reached its bounded asset capacity.",
        ),
        CatalogAssetWriteError::Storage(message) => error_response(500, "storage_error", &message),
    }
}

fn is_metadata_reader(actor: Option<&OperatorAccount>) -> bool {
    actor.is_some_and(|account| {
        account.enabled()
            && (account
                .roles()
                .contains(&station_auth::OperatorRole::SetupAdmin)
                || account
                    .roles()
                    .contains(&station_auth::OperatorRole::MeetingOperator))
    })
}

fn metadata_writer(
    actor: Option<&OperatorAccount>,
    permit_meeting_operator: bool,
) -> Option<&OperatorAccount> {
    actor.filter(|account| {
        account.enabled()
            && (account
                .roles()
                .contains(&station_auth::OperatorRole::SetupAdmin)
                || (permit_meeting_operator
                    && account
                        .roles()
                        .contains(&station_auth::OperatorRole::MeetingOperator)))
    })
}

fn lifecycle_writer(actor: Option<&OperatorAccount>) -> Option<&OperatorAccount> {
    actor.filter(|account| {
        account.enabled()
            && account
                .roles()
                .contains(&station_auth::OperatorRole::SetupAdmin)
    })
}

fn metadata_next_revision(expected_revision: Option<u64>) -> Result<u64, String> {
    match expected_revision {
        Some(revision) => revision
            .checked_add(1)
            .ok_or_else(|| "The expected revision is invalid.".to_string()),
        None => Ok(1),
    }
}

fn asset_metadata_write_error_response(error: AssetMetadataWriteError) -> ApiResponse {
    match error {
        AssetMetadataWriteError::Invalid(message) => {
            error_response(422, "validation_failed", &message)
        }
        AssetMetadataWriteError::SchemaNotFound => error_response(
            404,
            "metadata_schema_not_found",
            "The custom metadata schema does not exist.",
        ),
        AssetMetadataWriteError::StaleSchema {
            requested_revision,
            current_revision,
        } => error_response(
            409,
            "schema_revision_conflict",
            &format!(
                "Metadata schema revision {requested_revision} is stale; the current revision is {current_revision}."
            ),
        ),
        AssetMetadataWriteError::Conflict(message) => {
            error_response(409, "revision_conflict", &message)
        }
        AssetMetadataWriteError::Rebound => error_response(
            409,
            "metadata_identity_rebound",
            "An existing metadata record cannot be rebound to another asset or schema.",
        ),
        AssetMetadataWriteError::DuplicateBinding => error_response(
            409,
            "metadata_binding_conflict",
            "This asset and schema already have a metadata record.",
        ),
        AssetMetadataWriteError::Storage(message) => error_response(500, "storage_error", &message),
    }
}

fn recording_operator(actor: Option<&OperatorAccount>) -> Option<RecordingActor> {
    let account = actor.filter(|account| account.enabled())?;
    let role = if account
        .roles()
        .contains(&station_auth::OperatorRole::SetupAdmin)
    {
        RecordingActorRole::Supervisor
    } else if account
        .roles()
        .contains(&station_auth::OperatorRole::MeetingOperator)
    {
        RecordingActorRole::Operator
    } else {
        return None;
    };
    Some(RecordingActor {
        actor_id: account.account_id().as_str().into(),
        role,
    })
}

fn trusted_recording_worker_event(event: TrustedScheduledRecordingWorkerEvent) -> RecordingEvent {
    match event {
        TrustedScheduledRecordingWorkerEvent::StartCapture => RecordingEvent::StartCapture,
        TrustedScheduledRecordingWorkerEvent::BeginFinalization => {
            RecordingEvent::BeginFinalization
        }
        TrustedScheduledRecordingWorkerEvent::RecoverForFinalization { reason } => {
            RecordingEvent::RecoverForFinalization { reason }
        }
        TrustedScheduledRecordingWorkerEvent::FinalizeSuccess {
            captured_asset_hash,
            captured_duration_ms,
        } => RecordingEvent::FinalizeSuccess {
            captured_asset_hash,
            captured_duration_ms,
        },
        TrustedScheduledRecordingWorkerEvent::Fail { reason } => RecordingEvent::Fail { reason },
    }
}

fn recording_execution_authority(
    recording: &ScheduledRecording,
) -> Option<RecordingExecutionAuthority> {
    match &recording.lifecycle {
        station_recording::RecordingLifecycle::Draft
        | station_recording::RecordingLifecycle::CancelledBeforeArm { .. } => None,
        station_recording::RecordingLifecycle::Armed { execution, .. }
        | station_recording::RecordingLifecycle::Capturing { execution, .. }
        | station_recording::RecordingLifecycle::Finalizing { execution, .. }
        | station_recording::RecordingLifecycle::Completed { execution, .. }
        | station_recording::RecordingLifecycle::Failed { execution, .. }
        | station_recording::RecordingLifecycle::CancelledAfterArm { execution, .. } => {
            Some(execution.clone())
        }
    }
}

fn recording_schedule_error_response(message: String) -> ApiResponse {
    if message.contains("does not exist") {
        error_response(
            404,
            "schedule_item_not_found",
            "The scheduled recording's schedule item does not exist.",
        )
    } else if message.contains("not committed") {
        error_response(
            409,
            "schedule_not_committed",
            "The scheduled recording's schedule item is not committed.",
        )
    } else {
        error_response(409, "schedule_binding_changed", &message)
    }
}

fn path_identity<'a>(path: &'a str, prefix: &str, suffix: &str) -> Option<&'a str> {
    let identity = path.strip_prefix(prefix)?.strip_suffix(suffix)?;
    (!identity.is_empty() && !identity.contains('/') && !identity.contains(['?', '%', '\\']))
        .then_some(identity)
}

/// The reporting read surface accepts a bounded page size and an optional
/// opaque continuation token. The token is validated by storage against the
/// current channel/window/checkpoint projection revision.
fn asrun_page_path(path: &str) -> Option<(&str, Option<&str>, usize)> {
    let rest = path.strip_prefix("/api/v1/reporting/as-run/")?;
    let (channel_id, query) = rest.split_once('?')?;
    if !is_canonical_api_uuid(channel_id) {
        return None;
    }
    let mut cursor = None;
    let mut limit = None;
    for part in query.split('&') {
        let (key, value) = part.split_once('=')?;
        match key {
            "cursor"
                if cursor.is_none()
                    && !value.is_empty()
                    && value.bytes().all(|byte| {
                        byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_')
                    }) =>
            {
                cursor = Some(value)
            }
            "limit" if limit.is_none() => limit = value.parse::<usize>().ok(),
            _ => return None,
        }
    }
    let limit = limit?;
    (limit > 0 && limit <= station_storage::MAX_ASRUN_EPG_PAGE_SIZE)
        .then_some((channel_id, cursor, limit))
}
fn asrun_export_path(path: &str) -> Option<(&str, &'static str)> {
    for (suffix, format) in [("/export.csv", "csv"), ("/export.xmltv", "xmltv")] {
        if let Some(channel_id) = path_identity(path, "/api/v1/reporting/as-run/", suffix)
            .filter(|id| is_canonical_api_uuid(id))
        {
            return Some((channel_id, format));
        }
    }
    None
}

fn automation_policy_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/automation/policies/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn automation_execution_job_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/automation/jobs/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn automation_policy_status_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/automation/policies/", "/status")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn automation_policy_history_path(path: &str) -> Option<(&str, usize)> {
    let remainder = path.strip_prefix("/api/v1/automation/policies/")?;
    let (policy_id, query) = remainder.split_once("/history?")?;
    if !is_canonical_api_uuid(policy_id) || query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((policy_id, limit))
}

fn publish_target_account_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/publishing/accounts/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn publish_account_credential_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/publishing/accounts/", "/credential")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn publish_target_account_history_path(path: &str) -> Option<(&str, usize)> {
    publishing_history_path(path, "/api/v1/publishing/accounts/")
}

fn publish_target_profile_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/publishing/targets/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn publish_target_profile_history_path(path: &str) -> Option<(&str, usize)> {
    publishing_history_path(path, "/api/v1/publishing/targets/")
}

fn publish_execution_request_path(path: &str) -> Option<(&str, &str)> {
    let remainder = path.strip_prefix("/api/v1/publishing/targets/")?;
    let (target_id, remainder) = remainder.split_once("/assets/")?;
    let asset_id = remainder.strip_suffix("/jobs")?;
    (is_canonical_api_uuid(target_id) && is_lowercase_sha256(asset_id))
        .then_some((target_id, asset_id))
}

fn publish_execution_job_path(path: &str) -> Option<(&str, &str)> {
    let remainder = path.strip_prefix("/api/v1/publishing/targets/")?;
    let (target_id, job_id) = remainder.split_once("/jobs/")?;
    (is_canonical_api_uuid(target_id) && is_canonical_api_uuid(job_id))
        .then_some((target_id, job_id))
}

fn publish_execution_history_path(path: &str) -> Option<(&str, usize)> {
    let remainder = path.strip_prefix("/api/v1/publishing/targets/")?;
    let (target_id, query) = remainder.split_once("/jobs?")?;
    if !is_canonical_api_uuid(target_id) || query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((target_id, limit))
}

fn publishing_history_path<'a>(path: &'a str, prefix: &str) -> Option<(&'a str, usize)> {
    let remainder = path.strip_prefix(prefix)?;
    let (identity, query) = remainder.split_once("/history?")?;
    if !is_canonical_api_uuid(identity) || query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((identity, limit))
}

fn saved_search_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/scheduling/saved-searches/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn saved_search_history_path(path: &str) -> Option<(&str, usize)> {
    scheduling_history_path(path, "/api/v1/scheduling/saved-searches/")
}

fn saved_search_list_path(path: &str) -> Option<usize> {
    let query = path.strip_prefix("/api/v1/scheduling/saved-searches?")?;
    if query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some(limit)
}

fn daypart_block_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/scheduling/daypart-blocks/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn daypart_block_history_path(path: &str) -> Option<(&str, usize)> {
    scheduling_history_path(path, "/api/v1/scheduling/daypart-blocks/")
}

fn daypart_block_list_path(path: &str) -> Option<(&str, usize)> {
    scheduling_channel_list_path(path, "/api/v1/scheduling/daypart-blocks?")
}

fn recurring_rule_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/scheduling/recurring-rules/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn recurring_rule_history_path(path: &str) -> Option<(&str, usize)> {
    scheduling_history_path(path, "/api/v1/scheduling/recurring-rules/")
}

fn recurring_rule_list_path(path: &str) -> Option<(&str, usize)> {
    scheduling_channel_list_path(path, "/api/v1/scheduling/recurring-rules?")
}

fn schedule_materialization_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/scheduling/channels/", "/materializations")
        .filter(|identity| is_canonical_api_uuid(identity))
}

/// Recognizes only the two complete schedule-materialization endpoint shapes.
/// This keeps wrong-method responses distinct from malformed-path responses
/// without relaxing either endpoint's exact path and query grammar.
fn is_schedule_materialization_endpoint_shape(path: &str) -> bool {
    schedule_materialization_path(path).is_some()
        || schedule_materialization_history_path(path).is_some()
}

/// Accept exactly the documented keyset grammar.  A partial cursor, unknown
/// member, duplicate, or reordered pair is an invalid path instead of a
/// best-effort query: history pagination cannot safely guess caller intent.
fn schedule_materialization_history_path(
    path: &str,
) -> Option<(&str, usize, Option<ScheduleMaterializationHistoryCursor>)> {
    let remainder = path.strip_prefix("/api/v1/scheduling/channels/")?;
    let (channel_id, query) = remainder.split_once("/materializations/history?")?;
    if !is_canonical_api_uuid(channel_id) {
        return None;
    }
    let mut pairs = query.split('&');
    let limit = pairs
        .next()?
        .strip_prefix("limit=")?
        .parse::<usize>()
        .ok()?;
    if !(1..=128).contains(&limit) {
        return None;
    }
    let Some(first_cursor_pair) = pairs.next() else {
        return Some((channel_id, limit, None));
    };
    let occurred_at_unix_ms = first_cursor_pair
        .strip_prefix("before_occurred_at_unix_ms=")?
        .parse::<i64>()
        .ok()?;
    let run_id = pairs.next()?.strip_prefix("before_run_id=")?;
    if pairs.next().is_some() || !is_canonical_api_uuid(run_id) {
        return None;
    }
    Some((
        channel_id,
        limit,
        Some(ScheduleMaterializationHistoryCursor {
            occurred_at_unix_ms,
            run_id: run_id.into(),
        }),
    ))
}

fn scheduling_history_path<'a>(path: &'a str, prefix: &str) -> Option<(&'a str, usize)> {
    let remainder = path.strip_prefix(prefix)?;
    let (identity, query) = remainder.split_once("/history?")?;
    if !is_canonical_api_uuid(identity) || query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((identity, limit))
}

fn scheduling_channel_list_path<'a>(path: &'a str, prefix: &str) -> Option<(&'a str, usize)> {
    let query = path.strip_prefix(prefix)?;
    let (channel, limit) = query.split_once("&limit=")?;
    let channel_id = channel.strip_prefix("channel_id=")?;
    if !is_canonical_api_uuid(channel_id) || limit.contains('&') {
        return None;
    }
    let limit = limit.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((channel_id, limit))
}

fn catalog_asset_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/catalog/assets/", "")
        .filter(|identity| is_lowercase_sha256(identity))
}

fn catalog_asset_action_path<'a>(path: &'a str, action: &str) -> Option<&'a str> {
    path_identity(path, "/api/v1/catalog/assets/", &format!("/{action}"))
        .filter(|identity| is_lowercase_sha256(identity))
}

fn catalog_asset_history_path(path: &str) -> Option<(&str, usize)> {
    let remainder = path.strip_prefix("/api/v1/catalog/assets/")?;
    let (asset_sha256, query) = remainder.split_once("/history?")?;
    if !is_lowercase_sha256(asset_sha256) || query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=1_000)
        .contains(&limit)
        .then_some((asset_sha256, limit))
}

fn catalog_asset_list_path(path: &str) -> Option<(Option<&str>, usize)> {
    let query = path.strip_prefix("/api/v1/catalog/assets?")?;
    if let Some(limit) = query.strip_prefix("limit=") {
        if limit.contains('&') {
            return None;
        }
        let limit = limit.parse::<usize>().ok()?;
        return (1..=1_000).contains(&limit).then_some((None, limit));
    }
    let (cursor, limit) = query.split_once("&limit=")?;
    let cursor = cursor.strip_prefix("after_asset_sha256=")?;
    if !is_lowercase_sha256(cursor) || limit.contains('&') {
        return None;
    }
    let limit = limit.parse::<usize>().ok()?;
    (1..=1_000)
        .contains(&limit)
        .then_some((Some(cursor), limit))
}

fn custom_metadata_schema_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/custom-metadata/schemas/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn custom_metadata_schema_history_path(path: &str) -> Option<(&str, usize)> {
    let remainder = path.strip_prefix("/api/v1/custom-metadata/schemas/")?;
    let (schema_id, query) = remainder.split_once("/history?")?;
    if !is_canonical_api_uuid(schema_id) || query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((schema_id, limit))
}

fn asset_metadata_record_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/custom-metadata/records/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn asset_metadata_record_history_path(path: &str) -> Option<(&str, usize)> {
    let remainder = path.strip_prefix("/api/v1/custom-metadata/records/")?;
    let (metadata_id, query) = remainder.split_once("/history?")?;
    if !is_canonical_api_uuid(metadata_id) || query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((metadata_id, limit))
}

fn asset_metadata_records_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/custom-metadata/assets/", "/records")
        .filter(|identity| is_lowercase_sha256(identity))
}

fn scheduled_recording_path(path: &str) -> Option<&str> {
    path_identity(path, "/api/v1/scheduled-recordings/", "")
        .filter(|identity| is_canonical_api_uuid(identity))
}

fn scheduled_recording_action_path<'a>(path: &'a str, action: &str) -> Option<&'a str> {
    let remainder = path.strip_prefix("/api/v1/scheduled-recordings/")?;
    let (recording_id, requested_action) = remainder.rsplit_once('/')?;
    (requested_action == action && is_canonical_api_uuid(recording_id)).then_some(recording_id)
}

fn scheduled_recording_history_path(path: &str) -> Option<(&str, usize)> {
    let remainder = path.strip_prefix("/api/v1/scheduled-recordings/")?;
    let (recording_id, query) = remainder.split_once("/history?")?;
    if !is_canonical_api_uuid(recording_id) || query.contains('&') {
        return None;
    }
    let limit = query.strip_prefix("limit=")?.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((recording_id, limit))
}

fn scheduled_recording_list_path(path: &str) -> Option<(&str, usize)> {
    let query = path.strip_prefix("/api/v1/scheduled-recordings?")?;
    let (channel, limit) = query.split_once("&limit=")?;
    let channel_id = channel.strip_prefix("channel_id=")?;
    if !is_canonical_api_uuid(channel_id) || limit.contains('&') {
        return None;
    }
    let limit = limit.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((channel_id, limit))
}

fn scheduled_recording_candidates_path(path: &str) -> Option<(&str, usize)> {
    let query = path.strip_prefix("/api/v1/scheduled-recordings/candidates?")?;
    let (channel, limit) = query.split_once("&limit=")?;
    let channel_id = channel.strip_prefix("channel_id=")?;
    if !is_canonical_api_uuid(channel_id) || limit.contains('&') {
        return None;
    }
    let limit = limit.parse::<usize>().ok()?;
    (1..=128).contains(&limit).then_some((channel_id, limit))
}

fn asset_caption_track_path(path: &str) -> Option<(&str, &str)> {
    let remainder = path.strip_prefix("/api/v1/assets/")?;
    let (asset_id, track_id) = remainder.split_once("/caption-tracks/")?;
    (is_lowercase_sha256(asset_id)
        && is_canonical_api_uuid(track_id)
        && !track_id.contains(['/', '?', '%', '\\']))
    .then_some((asset_id, track_id))
}

fn asset_caption_tracks_path(path: &str) -> Option<&str> {
    let asset_id = path
        .strip_prefix("/api/v1/assets/")?
        .strip_suffix("/caption-tracks")?;
    is_lowercase_sha256(asset_id).then_some(asset_id)
}

fn asset_lifecycle_path<'a>(path: &'a str, suffix: &str) -> Option<&'a str> {
    let asset_id = path.strip_prefix("/api/v1/assets/")?.strip_suffix(suffix)?;
    is_lowercase_sha256(asset_id).then_some(asset_id)
}

fn read_pinned_thumbnail_bytes(root: &Path, digest: &str) -> Result<Vec<u8>, String> {
    const MAX_THUMBNAIL_BYTES: u64 = 8 * 1024 * 1024;
    if !is_lowercase_sha256(digest) {
        return Err("thumbnail identity is invalid".into());
    }
    ensure_non_reparse_ancestry(root)?;
    let canonical_root =
        std::fs::canonicalize(root).map_err(|_| "managed review evidence root is unavailable")?;
    ensure_non_reparse_ancestry(&canonical_root)?;
    let path = canonical_root.join(format!("{digest}.png"));
    let metadata =
        std::fs::symlink_metadata(&path).map_err(|_| "managed review evidence is unavailable")?;
    if metadata.file_attributes() & 0x0400 != 0
        || !metadata.is_file()
        || metadata.len() == 0
        || metadata.len() > MAX_THUMBNAIL_BYTES
    {
        return Err("managed review evidence is invalid or exceeds its bound".into());
    }
    let mut file =
        std::fs::File::open(&path).map_err(|_| "managed review evidence cannot be opened")?;
    let handle_metadata = file
        .metadata()
        .map_err(|_| "managed review evidence cannot be inspected")?;
    if !handle_metadata.is_file() || handle_metadata.len() != metadata.len() {
        return Err("managed review evidence changed before it could be opened".into());
    }
    let mut bytes = Vec::with_capacity(handle_metadata.len() as usize);
    std::io::Read::by_ref(&mut file)
        .take(MAX_THUMBNAIL_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "managed review evidence cannot be read")?;
    if bytes.is_empty() || bytes.len() as u64 != handle_metadata.len() {
        return Err("managed review evidence changed while it was read".into());
    }
    if format!("{:x}", Sha256::digest(&bytes)) != digest {
        return Err("managed review evidence does not match its recorded identity".into());
    }
    Ok(bytes)
}

fn ensure_non_reparse_ancestry(path: &Path) -> Result<(), String> {
    let mut current = Some(path);
    while let Some(candidate) = current {
        let metadata = std::fs::symlink_metadata(candidate)
            .map_err(|_| "managed review evidence ancestry is unavailable")?;
        if metadata.file_attributes() & 0x0400 != 0 {
            return Err("managed review evidence ancestry contains a reparse point".into());
        }
        current = candidate.parent();
    }
    Ok(())
}

fn is_lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn publish_execution_job_response(
    record: PublishExecutionJobRecord,
    receipts: Vec<PublishExecutionReceipt>,
) -> PublishExecutionJobResponse {
    let state = match record.state {
        PublishExecutionJobState::Pending => "pending",
        PublishExecutionJobState::Running => "running",
        PublishExecutionJobState::Completed => "completed",
        PublishExecutionJobState::Failed => "failed",
    };
    PublishExecutionJobResponse {
        job: record.job,
        state,
        attempts: record.attempts,
        next_attempt_at_unix_ms: record.next_attempt_at_unix_ms,
        updated_at_unix_ms: record.updated_at_unix_ms,
        lease_expires_at_unix_ms: record.lease_expires_at_unix_ms,
        completed_at_unix_ms: record.completed_at_unix_ms,
        failure_class: record.failure_class,
        receipts,
    }
}

fn hash_publish_file(path: &Path) -> Result<(u64, String), String> {
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(
            windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ
                | windows_sys::Win32::Storage::FileSystem::FILE_SHARE_WRITE,
        )
        .custom_flags(windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|error| format!("open publish media for hashing: {error}"))?;
    let metadata = file
        .metadata()
        .map_err(|error| format!("inspect publish media for hashing: {error}"))?;
    if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_file() {
        return Err("publish media is a reparse point or not a regular file".into());
    }
    let mut digest = Sha256::new();
    let mut bytes = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|error| format!("hash publish media: {error}"))?;
        if read == 0 {
            break;
        }
        bytes = bytes
            .checked_add(read as u64)
            .ok_or_else(|| "publish media size overflowed".to_string())?;
        if bytes > 1_099_511_627_776 {
            return Err("publish media exceeds the bounded maximum".into());
        }
        digest.update(&buffer[..read]);
    }
    if bytes == 0 {
        return Err("publish media is empty".into());
    }
    Ok((bytes, format!("{:x}", digest.finalize())))
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

fn receiver_receipt_response(
    receipt: ExternalReceiverAcceptanceReceipt,
    current: bool,
) -> ReceiverReceiptResponse {
    ReceiverReceiptResponse {
        receipt_id: receipt.receipt_id,
        channel_id: receipt.channel_id,
        sink_id: receipt.sink_id,
        outcome: match receipt.outcome {
            ReceiverAcceptanceOutcome::Accepted => "accepted",
            ReceiverAcceptanceOutcome::Rejected => "rejected",
        },
        method: receiver_evidence_method_api_name(receipt.method),
        receiver_label: receipt.receiver_label,
        evidence_reference: receipt.evidence_reference,
        detail: receipt.detail,
        observed_at_unix_ms: receipt.observed_at_unix_ms,
        recorded_at_unix_ms: receipt.recorded_at_unix_ms,
        recorded_by: receipt.recorded_by,
        candidate_version: receipt.candidate_version,
        source_commit: receipt.source_commit,
        configuration_hash: receipt.configuration_hash,
        current,
    }
}

fn receiver_evidence_method_api_name(method: ReceiverEvidenceMethod) -> &'static str {
    match method {
        ReceiverEvidenceMethod::HeadendOperatorConfirmation => "headend_operator_confirmation",
        ReceiverEvidenceMethod::ReceiverTelemetry => "receiver_telemetry",
        ReceiverEvidenceMethod::ReturnPathObservation => "return_path_observation",
        ReceiverEvidenceMethod::VisualAudioObservation => "visual_audio_observation",
    }
}

fn loudness_proof_response(
    sink: station_schedule::OutputSinkConfiguration,
    stored: Option<StoredSinkLoudnessProof>,
    receiver_acceptance: ReceiverAcceptance,
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
            receiver_acceptance,
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
        receiver_acceptance,
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

fn transport_proof_job_path(path: &str) -> Option<(&str, &str)> {
    let remainder = path.strip_prefix("/api/v1/channels/")?;
    let (channel_id, job_id) = remainder.split_once("/transport-proof/jobs/")?;
    (!channel_id.is_empty()
        && !job_id.is_empty()
        && !channel_id.contains('/')
        && !job_id.contains('/')
        && !channel_id.contains(['?', '%', '\\'])
        && !job_id.contains(['?', '%', '\\']))
    .then_some((channel_id, job_id))
}

fn caption_job_path(path: &str) -> Option<(&str, &str)> {
    let remainder = path.strip_prefix("/api/v1/channels/")?;
    let (channel_id, job_id) = remainder.split_once("/caption-proof/jobs/")?;
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

fn transport_proof_job_state_name(state: TransportProofJobState) -> &'static str {
    match state {
        TransportProofJobState::Pending => "pending",
        TransportProofJobState::Claimed => "claimed",
        TransportProofJobState::Capturing => "capturing",
        TransportProofJobState::Analyzing => "analyzing",
        TransportProofJobState::Completed => "completed",
        TransportProofJobState::Failed => "failed",
    }
}

fn caption_job_state_name(state: CaptionEgressProofJobState) -> &'static str {
    match state {
        CaptionEgressProofJobState::Pending => "pending",
        CaptionEgressProofJobState::Claimed => "claimed",
        CaptionEgressProofJobState::CapturePending => "capture_pending",
        CaptionEgressProofJobState::Analyzing => "analyzing",
        CaptionEgressProofJobState::Completed => "completed",
        CaptionEgressProofJobState::Failed => "failed",
    }
}

fn fallback_slate_selection_response(
    selection: ChannelFallbackSlate,
) -> FallbackSlateSelectionResponse {
    FallbackSlateSelectionResponse {
        channel_id: selection.channel_id,
        image_asset: selection.image_asset,
        revision: selection.revision,
        created_at_unix_ms: selection.created_at_unix_ms,
        updated_at_unix_ms: selection.updated_at_unix_ms,
        updated_by: selection.updated_by,
    }
}

fn fallback_slate_audit_response(
    event: ChannelFallbackSlateAuditEvent,
) -> FallbackSlateAuditResponse {
    FallbackSlateAuditResponse {
        audit_id: event.audit_id,
        channel_id: event.channel_id,
        image_asset_id: event.image_asset_id,
        revision: event.revision,
        actor: event.actor,
        occurred_at_unix_ms: event.occurred_at_unix_ms,
    }
}

fn caption_mode_name(mode: ChannelCaptionMode) -> &'static str {
    match mode {
        ChannelCaptionMode::Disabled => "disabled",
        ChannelCaptionMode::Cea708 => "cea708",
    }
}

fn caption_policy_response(policy: ChannelCaptionPolicy) -> CaptionPolicyResponse {
    CaptionPolicyResponse {
        channel_id: policy.channel_id,
        mode: caption_mode_name(policy.mode),
        service_number: policy.service_number,
        language: policy.language,
        revision: policy.revision,
        created_at_unix_ms: policy.created_at_unix_ms,
        updated_at_unix_ms: policy.updated_at_unix_ms,
        updated_by: policy.updated_by,
    }
}

fn caption_policy_audit_response(
    event: ChannelCaptionPolicyAuditEvent,
) -> CaptionPolicyAuditResponse {
    CaptionPolicyAuditResponse {
        audit_id: event.audit_id,
        channel_id: event.channel_id,
        mode: caption_mode_name(event.mode),
        service_number: event.service_number,
        language: event.language,
        revision: event.revision,
        actor: event.actor,
        occurred_at_unix_ms: event.occurred_at_unix_ms,
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

fn transport_proof_job_response(record: TransportProofJobRecord) -> TransportProofJobResponse {
    let job = record.job;
    TransportProofJobResponse {
        job_id: job.job_id,
        channel_id: job.channel_id,
        sink_id: job.sink_id,
        target_id: job.target_id,
        target_revision: job.target_revision,
        candidate_version: job.candidate_version,
        source_commit: job.source_commit,
        configuration_hash: job.configuration_hash,
        state: transport_proof_job_state_name(record.state),
        created_at_unix_ms: job.created_at_unix_ms,
        updated_at_unix_ms: record.updated_at_unix_ms,
        lease_expires_at_unix_ms: record.lease_expires_at_unix_ms,
        capture: record.capture.map(|capture| TransportProofCaptureResponse {
            artifact_relative_path: capture.artifact_relative_path,
            capture_bytes: capture.capture_bytes,
            capture_sha256: capture.capture_sha256,
            started_at_unix_ms: capture.started_at_unix_ms,
            ended_at_unix_ms: capture.ended_at_unix_ms,
        }),
        analyzer_output_sha256: record.analyzer_output_sha256,
        tables_output_sha256: record.tables_output_sha256,
        completed_at_unix_ms: record.completed_at_unix_ms,
        failed_at_unix_ms: record.failed_at_unix_ms,
        failure_detail: record.failure_detail,
    }
}

fn caption_job_response(record: CaptionEgressProofJobRecord) -> CaptionProofJobResponse {
    let job = record.job;
    CaptionProofJobResponse {
        job_id: job.job_id,
        channel_id: job.channel_id,
        sink_id: job.sink_id,
        report_id: job.report_id,
        asset_id: job.asset_id,
        track_id: job.track_id,
        track_revision: job.track_revision,
        cues_sha256: job.cues_sha256,
        caption_policy_revision: job.caption_policy_revision,
        language: job.language,
        service_number: job.service_number,
        candidate_version: job.candidate_version,
        source_commit: job.source_commit,
        configuration_hash: job.configuration_hash,
        capture_started_asset_offset_ms: job.capture_started_asset_offset_ms,
        capture_duration_ms: job.capture_duration_ms,
        max_capture_bytes: job.max_capture_bytes,
        expected_cue_count: job.expected_cue_count,
        expected_text_sha256: job.expected_text_sha256,
        state: caption_job_state_name(record.state),
        created_at_unix_ms: job.created_at_unix_ms,
        updated_at_unix_ms: record.updated_at_unix_ms,
        lease_expires_at_unix_ms: record.lease_expires_at_unix_ms,
        capture: record.capture.map(|capture: CaptionProofCaptureFacts| {
            CaptionProofCaptureResponse {
                artifact_relative_path: capture.artifact_relative_path,
                capture_bytes: capture.capture_bytes,
                capture_duration_ms: capture.capture_duration_ms,
                capture_sha256: capture.capture_sha256,
            }
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

fn caption_job_storage_error(message: &str) -> ApiResponse {
    if message.contains("already") || message.contains("active") {
        return error_response(409, "job_already_active", message);
    }
    if message.contains("stale") || message.contains("changed") {
        return error_response(409, "authority_changed", message);
    }
    if message.contains("not ready") || message.contains("does not exist") {
        return error_response(409, "authority_unavailable", message);
    }
    if message.contains("not enabled") || message.contains("not an enabled") {
        return error_response(409, "sink_not_enabled", message);
    }
    if message.contains("invalid") || message.contains("must ") || message.contains("unbounded") {
        return error_response(422, "validation_failed", message);
    }
    error_response(500, "storage_error", message)
}

struct CollectedHeadendProbe {
    probe: HeadendProbeInput,
    receiver_evidence: HashMap<String, String>,
}

fn collect_headend_probe(
    storage: &StationStore,
    generated_at_unix_ms: i64,
    channel_id: Option<&str>,
    current_candidate: Option<(&str, &str)>,
) -> Result<Option<CollectedHeadendProbe>, String> {
    let mut channels = storage.list_channels()?;
    if let Some(channel_id) = channel_id {
        channels.retain(|channel| channel.channel_id == channel_id);
        if channels.is_empty() {
            return Ok(None);
        }
    }
    let mut probes = Vec::with_capacity(channels.len());
    let mut receiver_evidence = HashMap::new();
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
        let mut sinks = Vec::with_capacity(channel.output_sinks.len());
        for sink in channel.output_sinks {
            let (external_receiver, evidence) = if let Some((candidate_version, source_commit)) =
                current_candidate
            {
                match storage.read_current_external_receiver_acceptance_receipt(
                    &channel.channel_id,
                    &sink.sink_id,
                    candidate_version,
                    source_commit,
                )? {
                    Some(receipt) => {
                        let state = match receipt.outcome {
                            ReceiverAcceptanceOutcome::Accepted => ExternalReceiverState::Verified,
                            ReceiverAcceptanceOutcome::Rejected => {
                                ExternalReceiverState::NotVerified
                            }
                        };
                        let evidence = format!(
                            "external receiver receipt {} via {} with reference {}",
                            receipt.receipt_id,
                            receiver_evidence_method_api_name(receipt.method),
                            receipt.evidence_reference
                        );
                        (state, Some(evidence))
                    }
                    None => (ExternalReceiverState::Unknown, None),
                }
            } else {
                (ExternalReceiverState::Unknown, None)
            };
            if let Some(evidence) = evidence {
                receiver_evidence.insert(
                    receiver_evidence_key(&channel.channel_id, &sink.sink_id),
                    evidence,
                );
            }
            sinks.push(HeadendSinkProbeInput {
                sink_id: sink.sink_id,
                label: sink.label,
                kind: match sink.kind {
                    station_schedule::OutputSinkKind::UdpTs => HeadendSinkKind::UdpTs,
                    station_schedule::OutputSinkKind::Srt => HeadendSinkKind::Srt,
                },
                required: sink.required,
                enabled: sink.enabled,
                external_receiver,
            });
        }
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
            sinks,
            runtime,
        });
    }
    Ok(Some(CollectedHeadendProbe {
        probe: HeadendProbeInput {
            generated_at_unix_ms,
            channels: probes,
        },
        receiver_evidence,
    }))
}

fn receiver_evidence_key(channel_id: &str, sink_id: &str) -> String {
    format!("{channel_id}/{sink_id}")
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

/// Standalone/test serve wrapper backed by one fresh authority for this serve
/// invocation. Stationd uses the explicit boot-authority entrypoint below.
pub fn serve_until_with_application_directory_and_health(
    listener: TcpListener,
    database_path: impl AsRef<Path>,
    application_directory: impl AsRef<Path>,
    stop: Arc<AtomicBool>,
    orchestration_health: Arc<OrchestrationHealth>,
) -> Result<(), String> {
    serve_until_with_application_directory_and_health_and_source_evidence_authority(
        listener,
        database_path,
        application_directory,
        stop,
        orchestration_health,
        new_source_evidence_authority()?,
    )
}

/// Production serve entrypoint. The authority must be constructed once by
/// stationd for the current console/service boot and retained here across all
/// per-request `Api` reconstruction.
pub fn serve_until_with_application_directory_and_health_and_source_evidence_authority(
    listener: TcpListener,
    database_path: impl AsRef<Path>,
    application_directory: impl AsRef<Path>,
    stop: Arc<AtomicBool>,
    orchestration_health: Arc<OrchestrationHealth>,
    source_evidence_authority: Arc<StationSourceEvidenceAuthority>,
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
        Arc::clone(&source_evidence_authority),
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
                let request_source_evidence_authority = Arc::clone(&source_evidence_authority);
                let request_count = Arc::clone(&active_requests);
                let request_auth_runtime = Arc::clone(&auth_runtime);
                std::thread::spawn(move || {
                    let _guard = ActiveRequestGuard(request_count);
                    let result = Api::open_with_application_directory_and_cache(
                        &request_database,
                        &request_application,
                        request_station_box_cache,
                        request_orchestration_health,
                        request_source_evidence_authority,
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
    let public_ott_request = is_public_ott_endpoint(request.method, request.path);
    if unsafe_request && !public_ott_request {
        let origin = match request.unique_header("origin") {
            Ok(origin) => origin,
            Err(response) => return response,
        };
        if origin != Some(runtime.expected_origin()) {
            return error_response(
                403,
                "cross_origin_denied",
                "Unsafe requests require this station's exact Origin.",
            );
        }
        let fetch_site = match request.unique_header("sec-fetch-site") {
            Ok(fetch_site) => fetch_site,
            Err(response) => return response,
        };
        if fetch_site != Some("same-origin") {
            return error_response(
                403,
                "cross_site_denied",
                "Unsafe requests require Sec-Fetch-Site: same-origin.",
            );
        }
    }
    if unsafe_request && public_ott_request {
        let origin = match request.unique_header("origin") {
            Ok(origin) => origin,
            Err(response) => return response,
        };
        if let Some(origin) = origin
            && origin != runtime.expected_origin()
        {
            return error_response(
                403,
                "cross_origin_denied",
                "OTT requests with a browser Origin must use this station's exact Origin.",
            );
        }
        let fetch_site = match request.unique_header("sec-fetch-site") {
            Ok(fetch_site) => fetch_site,
            Err(response) => return response,
        };
        if let Some(fetch_site) = fetch_site
            && fetch_site != "same-origin"
        {
            return error_response(
                403,
                "cross_site_denied",
                "OTT requests with browser Fetch Metadata must be same-origin.",
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
        // OTT catalog/session/beacon endpoints are deliberately public: the
        // session body carries a bounded client capability declaration, while
        // the server derives all content, entitlement/session, and time facts.
        // They still pass the exact-origin checks above for unsafe methods.
        ("GET", "/api/v1/ott/catalog")
        | ("POST", "/api/v1/ott/sessions" | "/api/v1/ott/beacons") => {
            return api.route(request.method, request.path, body, None);
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
    } else if method == "PUT" && asset_caption_track_path(path).is_some() {
        MAX_CAPTION_TRACK_BODY
    } else {
        MAX_SMALL_REQUEST_BODY
    }
}

fn request_too_large_message(method: &str, path: &str) -> &'static str {
    match request_body_limit(method, path) {
        MAX_CG_IMAGE_BODY if method == "POST" && path == "/api/v1/cg/images" => {
            "CG image uploads may not exceed 8 MiB."
        }
        MAX_CAPTION_TRACK_BODY => "Caption track requests may not exceed 8 MiB.",
        _ => "Requests may not exceed 64 KiB.",
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

fn is_public_ott_endpoint(method: &str, path: &str) -> bool {
    matches!(
        (method, path),
        ("GET", "/api/v1/ott/catalog")
            | ("POST", "/api/v1/ott/sessions")
            | ("POST", "/api/v1/ott/beacons")
    )
}

fn ott_session_error(error: &OttAuthorityError) -> ApiResponse {
    match error {
        OttAuthorityError::Unavailable => error_response(
            404,
            "ott_content_unavailable",
            "The requested OTT content is unavailable.",
        ),
        OttAuthorityError::ReplayConflict => error_response(
            409,
            "ott_request_replay_conflict",
            "The OTT session request conflicts with a previous request.",
        ),
        OttAuthorityError::RateLimited(seconds) => error_response(
            429,
            "ott_rate_limited",
            "The OTT session admission window is full; retry later.",
        )
        .with_header("Retry-After", seconds.max(&1).to_string())
        .unwrap_or_else(|_| {
            error_response(
                500,
                "response_error",
                "The OTT session response could not be constructed.",
            )
        }),
        OttAuthorityError::InvalidCapability
        | OttAuthorityError::Stale
        | OttAuthorityError::InvalidSequence => error_response(
            409,
            "ott_session_rejected",
            "The OTT session request is stale or invalid.",
        ),
        OttAuthorityError::Corrupt | OttAuthorityError::Internal => error_response(
            500,
            "ott_storage_error",
            "The OTT session authority is temporarily unavailable.",
        ),
    }
}

fn ott_beacon_error(error: &OttAuthorityError) -> ApiResponse {
    match error {
        OttAuthorityError::Unavailable => error_response(
            404,
            "ott_session_unavailable",
            "The OTT playback session is unavailable.",
        ),
        OttAuthorityError::ReplayConflict
        | OttAuthorityError::Stale
        | OttAuthorityError::InvalidSequence => error_response(
            409,
            "ott_beacon_rejected",
            "The OTT playback beacon is stale or conflicts with session state.",
        ),
        OttAuthorityError::RateLimited(seconds) => error_response(
            429,
            "ott_rate_limited",
            "The OTT beacon admission window is full; retry later.",
        )
        .with_header("Retry-After", seconds.max(&1).to_string())
        .unwrap_or_else(|_| {
            error_response(
                500,
                "response_error",
                "The OTT beacon response could not be constructed.",
            )
        }),
        OttAuthorityError::InvalidCapability => error_response(
            403,
            "ott_capability_rejected",
            "The OTT playback capability is invalid.",
        ),
        OttAuthorityError::Corrupt | OttAuthorityError::Internal => error_response(
            500,
            "ott_storage_error",
            "The OTT beacon authority is temporarily unavailable.",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gst::prelude::*;
    use gstreamer as gst;
    use station_schedule_automation::{
        AssetPredicate, DstApplication, SavedSearchOrder, SourceEvidenceVerifier,
    };
    use std::collections::BTreeSet;
    use std::net::TcpListener;
    use std::thread;

    trait ScheduleStoreTestExt {
        fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String>;
    }

    impl ScheduleStoreTestExt for StationStore {
        fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String> {
            if item.state != ScheduleState::Draft {
                return Err(
                    "test schedule fixtures must seed drafts through the public write path".into(),
                );
            }
            self.write_schedule_item(&ScheduleItemWrite {
                item: item.clone(),
                changed_by: "test-harness".into(),
                occurred_at_unix_ms: 1,
            })
        }
    }

    const PNG_1X1: &[u8] = &[
        137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 6,
        0, 0, 0, 31, 21, 196, 137, 0, 0, 0, 13, 73, 68, 65, 84, 120, 156, 99, 248, 207, 192, 240,
        31, 0, 5, 0, 1, 255, 137, 153, 61, 29, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130,
    ];

    const TEST_PASSWORD_PHC: &str = "$argon2id$v=19$m=65536,t=3,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE";

    #[test]
    fn thumbnail_reader_accepts_only_the_content_addressed_managed_png() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-thumbnail-reader-{nonce}"));
        std::fs::create_dir_all(&root).unwrap();
        let digest = format!("{:x}", Sha256::digest(PNG_1X1));
        std::fs::write(root.join(format!("{digest}.png")), PNG_1X1).unwrap();
        assert_eq!(
            read_pinned_thumbnail_bytes(&root, &digest).unwrap(),
            PNG_1X1
        );
        assert!(read_pinned_thumbnail_bytes(&root, "0".repeat(64).as_str()).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

    fn install_transport_candidate(data: &Path, application: &Path) {
        const VERSION: &str = "0.2.0";
        const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";
        const RUNTIME_SHA: &str =
            "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd";
        const TSDUCK_SHA: &str = "b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1";
        let runtime = application.join("runtime/gstreamer");
        let tsduck = application.join("runtime/tsduck");
        std::fs::create_dir_all(runtime.join("bin")).unwrap();
        std::fs::create_dir_all(runtime.join("lib/gstreamer-1.0")).unwrap();
        std::fs::create_dir_all(runtime.join("lib/gst-validate-launcher/python/launcher/apps"))
            .unwrap();
        for (path, bytes) in [
            ("bin/gstreamer.dll", b"runtime".as_slice()),
            ("lib/gstreamer-1.0/plugin.dll", b"plugin".as_slice()),
            (
                "lib/gst-validate-launcher/python/launcher/apps/empty.py",
                b"".as_slice(),
            ),
        ] {
            std::fs::write(runtime.join(path), bytes).unwrap();
        }
        for (path, bytes) in [
            ("LICENSE.txt", b"license".as_slice()),
            ("OTHERS.txt", b"notices".as_slice()),
            ("bin/tsp.exe", b"tsp".as_slice()),
            ("bin/tsanalyze.exe", b"analyze".as_slice()),
            ("bin/tstables.exe", b"tables".as_slice()),
            ("bin/tsduck.dll", b"tsduck".as_slice()),
            ("bin/tscore.dll", b"tscore".as_slice()),
        ] {
            let destination = tsduck.join(path);
            std::fs::create_dir_all(destination.parent().unwrap()).unwrap();
            std::fs::write(destination, bytes).unwrap();
        }
        let runtime_manifest_sha = station_maintenance::write_installed_runtime_manifest(
            &runtime,
            &data.join(station_maintenance::INSTALLED_RUNTIME_MANIFEST_NAME),
            VERSION,
            COMMIT,
            RUNTIME_SHA,
        )
        .unwrap();
        let tsduck_manifest_sha = station_maintenance::write_installed_runtime_manifest(
            &tsduck,
            &data.join(station_maintenance::TSDUCK_RUNTIME_MANIFEST_NAME),
            VERSION,
            COMMIT,
            TSDUCK_SHA,
        )
        .unwrap();
        let files = [
            ("stationd.exe", b"stationd".as_slice()),
            ("channel-worker.exe", b"worker".as_slice()),
        ];
        let manifest = station_update::UpdatePackageManifest {
            schema_version: 2,
            product: "TownLight Station".into(),
            version: VERSION.into(),
            source_commit: COMMIT.into(),
            files: files
                .iter()
                .map(|(path, bytes)| station_update::UpdatePackageFile {
                    path: (*path).into(),
                    size_bytes: bytes.len() as u64,
                    sha256: format!("{:x}", Sha256::digest(bytes)),
                })
                .collect(),
        };
        for (path, bytes) in files {
            std::fs::write(application.join(path), bytes).unwrap();
        }
        std::fs::write(
            data.join(station_maintenance::ACTIVE_SLOT_RECEIPT_NAME),
            serde_json::json!({"schema_version":1,"generation":7,"activated_at_unix_ms":1,"transaction_id":"bootstrap","active":{"relative_directory":".","package_manifest":manifest},"previous":null}).to_string(),
        ).unwrap();
        std::fs::write(
            data.join(station_maintenance::INSTALL_RECEIPT_NAME),
            serde_json::json!({"product":"TownLight Station","version":VERSION,"source_commit":COMMIT,"media_runtime_sha256":RUNTIME_SHA,"runtime_manifest_sha256":runtime_manifest_sha,"tsduck_version":"3.44-4676","tsduck_source_url":"https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip","tsduck_archive_name":"TSDuck-Win64-3.44-4676-Portable.zip","tsduck_runtime_sha256":TSDUCK_SHA,"tsduck_runtime_manifest_sha256":tsduck_manifest_sha,"installed_at_unix_ms":1,"health_uri":"http://127.0.0.1:4070/health"}).to_string(),
        ).unwrap();
    }

    fn test_actor_with_account(
        account_id: &str,
        username: &str,
        role: station_auth::OperatorRole,
    ) -> OperatorAccount {
        OperatorAccount::new(
            station_auth::AccountId::try_from(account_id).unwrap(),
            station_auth::OperatorUsername::try_from(username).unwrap(),
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

    fn test_actor(role: station_auth::OperatorRole) -> OperatorAccount {
        test_actor_with_account("dc3c0d56-44c8-4c4f-bd32-b501c56d34f6", "setup.admin", role)
    }

    fn disabled_test_actor(role: station_auth::OperatorRole) -> OperatorAccount {
        OperatorAccount::new(
            station_auth::AccountId::try_from("eeeeeeee-ffff-4000-8000-111111111111").unwrap(),
            station_auth::OperatorUsername::try_from("disabled.operator").unwrap(),
            station_auth::OperatorDisplayName::try_from("Disabled Operator").unwrap(),
            station_auth::PasswordPhc::try_from(TEST_PASSWORD_PHC).unwrap(),
            BTreeSet::from([role]),
            station_auth::CredentialVersion::new(1).unwrap(),
            false,
            1,
            1,
        )
        .unwrap()
    }

    #[test]
    fn asrun_reads_and_exports_require_roles_and_are_bounded() {
        let path = std::env::temp_dir().join(format!(
            "townlight-api-asrun-{}.db",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let api = Api::open(&path).unwrap();
        let take = station_reporting::runtime::JournalTake {
            journal_id: "station-worker-00000000-0000-4000-8000-000000000001".into(),
            journal_sequence: 1,
            channel_id: "00000000-0000-4000-8000-000000000001".into(),
            started_at_unix_ms: 1_700_000_000_000,
            ended_at_unix_ms: 1_700_000_060_000,
            state: station_reporting::runtime::AsRunState::Program,
            content_id: Some("00000000-0000-4000-8000-000000000003".into()),
            franchise_id: Some("00000000-0000-4000-8000-000000000004".into()),
            category: Some("program".into()),
            title: Some("=Council <Meeting>".into()),
            catalog_revision: Some(1),
            schedule_revision: Some(1),
            journal_hash: "a".repeat(64),
            unavailable_reason: None,
        };
        let mut projection = station_reporting::runtime::ReportingRuntime::new()
            .project(
                station_reporting::runtime::ProjectionInput::new(
                    "00000000-0000-4000-8000-000000000001",
                    "America/Denver",
                    vec![take],
                )
                .unwrap(),
            )
            .unwrap();
        projection.window = Some(station_reporting::runtime::ProjectionWindow {
            local_date: "2023-11-14".into(),
            starts_at_utc_ms: 1_699_945_200_000,
            ends_at_utc_ms: 1_700_031_600_000,
        });
        api.storage
            .persist_asrun_projection(&projection, 1_700_000_060_000)
            .unwrap();
        let page = "/api/v1/reporting/as-run/00000000-0000-4000-8000-000000000001?limit=1";
        assert_eq!(api.handle_in_process("GET", page, None).status, 403);
        let actor = test_actor(station_auth::OperatorRole::SetupAdmin);
        let page_response = api.handle_authenticated("GET", page, None, &actor);
        assert_eq!(page_response.status, 200);
        let page_body = String::from_utf8(page_response.body).unwrap();
        assert!(page_body.contains("\"projection_window\""));
        assert!(page_body.contains("\"source_journal_id\""));
        assert!(page_body.contains("\"next_cursor\""));
        assert_eq!(
            api.handle_authenticated(
                "GET",
                "/api/v1/reporting/as-run/00000000-0000-4000-8000-000000000001?limit=201",
                None,
                &actor
            )
            .status,
            400
        );
        let csv = api.handle_authenticated(
            "GET",
            "/api/v1/reporting/as-run/00000000-0000-4000-8000-000000000001/export.csv",
            None,
            &actor,
        );
        assert_eq!(csv.status, 200);
        assert!(String::from_utf8(csv.body).unwrap().contains("'=Council"));
        let xml = api.handle_authenticated(
            "GET",
            "/api/v1/reporting/as-run/00000000-0000-4000-8000-000000000001/export.xmltv",
            None,
            &actor,
        );
        assert_eq!(xml.status, 200);
        assert!(
            String::from_utf8(xml.body)
                .unwrap()
                .contains("&lt;Meeting&gt;")
        );
    }

    fn put_catalog_test_media(api: &Api, asset_sha256: &str, readiness: AssetReadiness) {
        api.storage
            .put_media_asset(&MediaAsset {
                asset_id: asset_sha256.into(),
                media_path: format!(r"C:\TownLight\media\{asset_sha256}.ts"),
                duration_ms: 60_000,
                readiness,
            })
            .unwrap();
    }

    fn generate_lifecycle_media_fixture(path: &Path, video_buffers: i32) {
        gst::init().unwrap();
        let pipeline = gst::Pipeline::new();
        let video = gst::ElementFactory::make("videotestsrc")
            .property("num-buffers", video_buffers)
            .build()
            .unwrap();
        let video_convert = gst::ElementFactory::make("videoconvert").build().unwrap();
        let video_encoder = gst::ElementFactory::make("openh264enc").build().unwrap();
        let video_parser = gst::ElementFactory::make("h264parse").build().unwrap();
        let audio = gst::ElementFactory::make("audiotestsrc")
            .property("num-buffers", 64_i32)
            .build()
            .unwrap();
        let audio_convert = gst::ElementFactory::make("audioconvert").build().unwrap();
        let audio_encoder = gst::ElementFactory::make("voaacenc").build().unwrap();
        let audio_parser = gst::ElementFactory::make("aacparse").build().unwrap();
        let mux = gst::ElementFactory::make("mpegtsmux").build().unwrap();
        let sink = gst::ElementFactory::make("filesink")
            .property("location", path.to_string_lossy().as_ref())
            .build()
            .unwrap();
        pipeline
            .add_many([
                &video,
                &video_convert,
                &video_encoder,
                &video_parser,
                &audio,
                &audio_convert,
                &audio_encoder,
                &audio_parser,
                &mux,
                &sink,
            ])
            .unwrap();
        gst::Element::link_many([
            &video,
            &video_convert,
            &video_encoder,
            &video_parser,
            &mux,
            &sink,
        ])
        .unwrap();
        gst::Element::link_many([&audio, &audio_convert, &audio_encoder, &audio_parser, &mux])
            .unwrap();
        pipeline.set_state(gst::State::Playing).unwrap();
        let bus = pipeline.bus().unwrap();
        for message in bus.iter_timed(gst::ClockTime::from_seconds(15)) {
            match message.view() {
                gst::MessageView::Eos(_) => break,
                gst::MessageView::Error(error) => {
                    panic!("lifecycle fixture failed: {}", error.error())
                }
                _ => {}
            }
        }
        pipeline.set_state(gst::State::Null).unwrap();
        assert!(path.is_file());
    }

    #[test]
    fn media_lifecycle_api_enforces_roles_server_authority_and_isolated_immutable_changes() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-media-lifecycle-api-{nonce}"));
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        let recovery_source = root.join("recovery-source.ts");
        let replacement_source = root.join("replacement-source.ts");
        generate_lifecycle_media_fixture(&recovery_source, 30);
        generate_lifecycle_media_fixture(&replacement_source, 31);
        let seeded =
            station_media_assets::ingest_media(&recovery_source, root.join("seed")).unwrap();
        let api = Api::open(&database).unwrap();
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let operator = test_actor_with_account(
            "eeeeeeee-ffff-4000-8000-111111111111",
            "meeting.operator",
            station_auth::OperatorRole::MeetingOperator,
        );
        api.storage
            .put_media_asset(&MediaAsset {
                asset_id: seeded.asset_id.clone(),
                media_path: r"C:\missing\original.ts".into(),
                duration_ms: 1,
                readiness: AssetReadiness::Missing,
            })
            .unwrap();
        let lifecycle_path = format!("/api/v1/assets/{}/lifecycle", seeded.asset_id);
        assert_eq!(api.route("GET", &lifecycle_path, None, None).status, 403);
        assert_eq!(
            api.route("GET", &lifecycle_path, None, Some(&operator))
                .status,
            200
        );

        let relink_path = format!("/api/v1/assets/{}/relink", seeded.asset_id);
        let relink_body =
            serde_json::to_vec(&serde_json::json!({"source_path": recovery_source})).unwrap();
        assert_eq!(
            api.route("POST", &relink_path, Some(&relink_body), Some(&operator))
                .status,
            403
        );
        let before = system_time_millis().unwrap();
        let recovered = api.route("POST", &relink_path, Some(&relink_body), Some(&admin));
        assert_eq!(recovered.status, 200);
        let recovered: serde_json::Value = serde_json::from_slice(&recovered.body).unwrap();
        assert_eq!(recovered["asset_id"], seeded.asset_id);
        assert_eq!(recovered["recovered_asset_sha256"], seeded.asset_id);
        assert_eq!(recovered["changed_by"], admin.account_id().as_str());
        assert!(recovered["occurred_at_unix_ms"].as_i64().unwrap() >= before);
        assert_eq!(recovered["byte_count"], seeded.size_bytes);
        let lifecycle = api.route("GET", &lifecycle_path, None, Some(&operator));
        let lifecycle: serde_json::Value = serde_json::from_slice(&lifecycle.body).unwrap();
        assert_eq!(lifecycle["recovery_history"].as_array().unwrap().len(), 1);

        let retention_path = format!("/api/v1/assets/{}/retention", seeded.asset_id);
        let retention = serde_json::to_vec(&serde_json::json!({"permanent":true,"review_after_unix_ms":null,"legal_hold":false,"expected_revision":0})).unwrap();
        assert_eq!(
            api.route("PUT", &retention_path, Some(&retention), Some(&operator))
                .status,
            403
        );
        assert_eq!(
            api.route("PUT", &retention_path, Some(&retention), Some(&admin))
                .status,
            200
        );
        assert_eq!(
            api.route("PUT", &retention_path, Some(&retention), Some(&admin))
                .status,
            409
        );

        let schedule_item = ScheduleItem {
            item_id: "11111111-2222-4333-8444-555555555555".into(),
            revision: 1,
            channel_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
            asset_id: seeded.asset_id.clone(),
            title: "Recovery isolation".into(),
            starts_at_unix_ms: 10_000,
            duration_ms: 1_000,
            state: ScheduleState::Draft,
        };
        api.storage
            .write_schedule_item(&ScheduleItemWrite {
                item: schedule_item.clone(),
                changed_by: admin.account_id().as_str().into(),
                occurred_at_unix_ms: 1,
            })
            .unwrap();
        let replacement_path = format!("/api/v1/assets/{}/replacements", seeded.asset_id);
        let replacement_body = serde_json::to_vec(&serde_json::json!({"source_path":replacement_source,"expected_predecessor_revision":0})).unwrap();
        assert_eq!(
            api.route(
                "POST",
                &replacement_path,
                Some(&replacement_body),
                Some(&operator)
            )
            .status,
            403
        );
        let replacement = api.route(
            "POST",
            &replacement_path,
            Some(&replacement_body),
            Some(&admin),
        );
        assert_eq!(replacement.status, 201);
        let replacement: serde_json::Value = serde_json::from_slice(&replacement.body).unwrap();
        assert_ne!(replacement["successor_asset_id"], seeded.asset_id);
        assert_eq!(
            api.storage
                .read_schedule_item(&schedule_item.item_id)
                .unwrap()
                .unwrap(),
            schedule_item
        );

        let review_root = root.join("media-review");
        std::fs::create_dir_all(&review_root).unwrap();
        let thumbnail_sha256 = format!("{:x}", Sha256::digest(PNG_1X1));
        std::fs::write(review_root.join(format!("{thumbnail_sha256}.png")), PNG_1X1).unwrap();
        api.storage
            .record_asset_thumbnail(&AssetThumbnail {
                asset_id: seeded.asset_id.clone(),
                thumbnail_sha256: thumbnail_sha256.clone(),
                source_asset_sha256: seeded.asset_id.clone(),
                generated_at_unix_ms: 2,
                byte_count: PNG_1X1.len() as u64,
            })
            .unwrap();
        let thumbnail_path = format!("/api/v1/assets/{}/thumbnail", seeded.asset_id);
        assert_eq!(api.route("GET", &thumbnail_path, None, None).status, 403);
        let thumbnail = api.route("GET", &thumbnail_path, None, Some(&operator));
        assert_eq!(thumbnail.status, 200);
        assert_eq!(thumbnail.content_type, "image/png");
        assert_eq!(thumbnail.body, PNG_1X1);

        drop(api);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn schedule_mutations_bind_authorized_actors_and_reject_forged_approval() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-schedule-api-{nonce}.db"));
        let scheduled_media = database.with_extension("scheduled.ts");
        std::fs::write(&scheduled_media, b"admitted bytes").unwrap();
        let api = Api::open(&database).unwrap();
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let asset = MediaAsset {
            asset_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            media_path: scheduled_media.to_string_lossy().into_owned(),
            duration_ms: 60_000,
            readiness: AssetReadiness::Ready,
        };
        api.storage.put_media_asset(&asset).unwrap();
        let item = ScheduleItem {
            item_id: "256d5a07-92d3-4718-aec9-05cad42fae7d".into(),
            revision: 1,
            channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
            asset_id: asset.asset_id.clone(),
            title: "City Council".into(),
            starts_at_unix_ms: 100_000,
            duration_ms: 60_000,
            state: ScheduleState::Draft,
        };
        let item_json = serde_json::to_vec(&item).unwrap();
        assert_eq!(
            api.handle_in_process("PUT", "/api/v1/schedule/items", Some(&item_json))
                .status,
            403
        );
        for role in [
            station_auth::OperatorRole::RecordsClerk,
            station_auth::OperatorRole::PublishOperator,
            station_auth::OperatorRole::SupportAdmin,
        ] {
            let rejected = test_actor(role);
            assert_eq!(
                api.handle_authenticated(
                    "PUT",
                    "/api/v1/schedule/items",
                    Some(&item_json),
                    &rejected,
                )
                .status,
                403
            );
            assert_eq!(
                api.handle_authenticated(
                    "POST",
                    "/api/v1/schedule/commit",
                    Some(br#"{"report_id":"role-rejected","plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d"}"#),
                    &rejected,
                )
                .status,
                403
            );
        }
        assert_eq!(
            api.handle_authenticated("PUT", "/api/v1/schedule/items", Some(&item_json), &operator)
                .status,
            200
        );
        for forged_prepare in [
            br#"{"plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","actor_id":"forged"}"#.as_slice(),
            br#"{"plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","approved_by":"forged"}"#.as_slice(),
            br#"{"plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","approved_at_unix_ms":1}"#.as_slice(),
            br#"{"plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","occurred_at_unix_ms":1}"#.as_slice(),
            br#"{"plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","approval":true}"#.as_slice(),
            br#"{"plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","extra":"ignored-before-hardening"}"#.as_slice(),
        ] {
            let rejected = api.handle_in_process(
                "POST",
                "/api/v1/schedule/prepare",
                Some(forged_prepare),
            );
            assert_eq!(rejected.status, 400);
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&rejected.body).unwrap()["error"]
                    ["code"],
                "invalid_json"
            );
        }
        let prepare = api.handle_in_process(
            "POST",
            "/api/v1/schedule/prepare",
            Some(br#"{"plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d"}"#),
        );
        assert_eq!(prepare.status, 200);
        assert!(
            serde_json::from_slice::<station_schedule::CommitPlan>(&prepare.body)
                .unwrap()
                .dry_run_passed
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                "/api/v1/schedule/commit",
                Some(br#"{"report_id":"forged-report","plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","approved_by":"forged"}"#),
                &operator,
            )
            .status,
            400
        );
        let commit = api.handle_authenticated(
            "POST",
            "/api/v1/schedule/commit",
            Some(br#"{"report_id":"report-1","plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","operator_notes":"Reviewed."}"#),
            &operator,
        );
        assert_eq!(commit.status, 201);
        let report: CommitReport = serde_json::from_slice(&commit.body).unwrap();
        assert_eq!(report.dispatch_status, DispatchStatus::Pending);
        assert_eq!(report.approved_by, operator.account_id().as_str());
        assert_eq!(
            api.handle_in_process("GET", "/api/v1/schedule/commits/report-1", None)
                .status,
            200
        );
        drop(api);
        let _ = std::fs::remove_file(database);
        let _ = std::fs::remove_file(scheduled_media);
    }

    #[test]
    fn setup_admin_selects_a_verified_fallback_slate_and_commissioning_consumes_it() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "townlight-fallback-slate-api-{}-{nonce}",
            std::process::id()
        ));
        let application = root.join("application");
        std::fs::create_dir_all(&application).unwrap();
        let database = root.join("station.db");
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let mut channel = ChannelConfiguration {
            channel_id: channel_id.into(),
            display_name: "Fallback proof".into(),
            output_profile: station_schedule::OutputProfileId::Proof360p30,
            output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                "22222222-3333-4444-8555-666666666666",
                "Proof output",
                "127.0.0.1:5500",
                -16.0,
            )],
            enabled: false,
        };
        api.storage.put_channel(&channel).unwrap();
        let image = api.storage.admit_cg_image_asset(PNG_1X1).unwrap();
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);

        let forbidden = api.route(
            "PUT",
            &format!("/api/v1/channels/{channel_id}/fallback-slate"),
            Some(
                format!(
                    r#"{{"image_asset_id":"{}","expected_revision":0}}"#,
                    image.asset_id
                )
                .as_bytes(),
            ),
            None,
        );
        assert_eq!(forbidden.status, 403);

        let selected = api.route(
            "PUT",
            &format!("/api/v1/channels/{channel_id}/fallback-slate"),
            Some(
                format!(
                    r#"{{"image_asset_id":"{}","expected_revision":0}}"#,
                    image.asset_id
                )
                .as_bytes(),
            ),
            Some(&admin),
        );
        assert_eq!(
            selected.status,
            200,
            "{}",
            String::from_utf8_lossy(&selected.body)
        );
        let selected: serde_json::Value = serde_json::from_slice(&selected.body).unwrap();
        assert_eq!(selected["revision"], 1);
        assert_eq!(selected["updated_by"], admin.account_id().as_str());

        let authority = api.handle_in_process(
            "GET",
            &format!("/api/v1/channels/{channel_id}/fallback-slate"),
            None,
        );
        let authority: serde_json::Value = serde_json::from_slice(&authority.body).unwrap();
        assert_eq!(authority["selection"]["image_asset"]["readiness"], "ready");
        assert_eq!(authority["audit"].as_array().unwrap().len(), 1);

        channel.enabled = true;
        api.storage.put_channel(&channel).unwrap();
        let now = system_time_millis().unwrap();
        api.storage
            .put_channel_runtime(&station_domain::ChannelRuntimeObservation {
                channel_id: channel_id.into(),
                state: ChannelRuntimeState::HealthyFallback,
                on_air_asset_id: None,
                state_started_at_unix_ms: now,
                heartbeat_at_unix_ms: now,
                detail: None,
            })
            .unwrap();
        let report = api.route("GET", "/api/v1/commissioning/report", None, Some(&admin));
        assert_eq!(report.status, 200);
        let report: serde_json::Value = serde_json::from_slice(&report.body).unwrap();
        let fallback = report["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|check| check["id"] == "fallback_slate")
            .unwrap();
        assert_eq!(fallback["status"], "pass");
        assert!(
            fallback["evidence"]
                .as_str()
                .unwrap()
                .contains("revision 1")
        );

        drop(api);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn setup_admin_controls_audited_channel_caption_policy() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "townlight-caption-policy-api-{}-{nonce}",
            std::process::id()
        ));
        let application = root.join("application");
        std::fs::create_dir_all(&application).unwrap();
        let database = root.join("station.db");
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let mut channel = ChannelConfiguration {
            channel_id: channel_id.into(),
            display_name: "Caption proof".into(),
            output_profile: station_schedule::OutputProfileId::Proof360p30,
            output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                "22222222-3333-4444-8555-666666666666",
                "Proof output",
                "127.0.0.1:5500",
                -16.0,
            )],
            enabled: false,
        };
        api.storage.put_channel(&channel).unwrap();
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let path = format!("/api/v1/channels/{channel_id}/caption-policy");
        let body =
            br#"{"mode":"cea708","service_number":1,"language":"en-US","expected_revision":0}"#;

        assert_eq!(api.route("PUT", &path, Some(body), None).status, 403);
        assert_eq!(
            api.route("PUT", &path, Some(body), Some(&operator)).status,
            403
        );
        let written = api.route("PUT", &path, Some(body), Some(&admin));
        assert_eq!(
            written.status,
            200,
            "{}",
            String::from_utf8_lossy(&written.body)
        );
        let written: serde_json::Value = serde_json::from_slice(&written.body).unwrap();
        assert_eq!(written["mode"], "cea708");
        assert_eq!(written["service_number"], 1);
        assert_eq!(written["language"], "en-US");
        assert_eq!(written["revision"], 1);
        assert_eq!(written["updated_by"], admin.account_id().as_str());

        let authority = api.handle_in_process("GET", &path, None);
        assert_eq!(authority.status, 200);
        let authority: serde_json::Value = serde_json::from_slice(&authority.body).unwrap();
        assert_eq!(authority["policy"]["revision"], 1);
        assert_eq!(authority["audit"].as_array().unwrap().len(), 1);

        let stale = api.route("PUT", &path, Some(body), Some(&admin));
        assert_eq!(stale.status, 409);

        channel.enabled = true;
        api.storage.put_channel(&channel).unwrap();
        let running_update =
            br#"{"mode":"disabled","service_number":1,"language":"en-US","expected_revision":1}"#;
        let running = api.route("PUT", &path, Some(running_update), Some(&admin));
        assert_eq!(running.status, 409);

        drop(api);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn setup_admin_controls_asset_caption_tracks_with_server_owned_audit() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "townlight-caption-track-api-{}-{nonce}",
            std::process::id()
        ));
        let application = root.join("application");
        std::fs::create_dir_all(&application).unwrap();
        let database = root.join("station.db");
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let asset_id = "a".repeat(64);
        let track_id = "65a26343-0339-4654-a92c-4bf9384f18df";
        api.storage
            .put_media_asset(&MediaAsset {
                asset_id: asset_id.clone(),
                media_path: r"C:\TownLight\media\captioned.ts".into(),
                duration_ms: 10_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let path = format!("/api/v1/assets/{asset_id}/caption-tracks/{track_id}");
        let body = br#"{
          "language":"en-US",
          "service_number":1,
          "source_kind":"external",
          "label":"Reviewed English",
          "cues":[
            {
              "cue_id":"bf0aa2ca-1488-42d2-8c1b-5d9179ee5d30",
              "start_ms":1000,
              "duration_ms":2000,
              "text":"Town Council"
            }
          ],
          "expected_revision":0
        }"#;

        assert_eq!(api.route("PUT", &path, Some(body), None).status, 403);
        assert_eq!(
            api.route("PUT", &path, Some(body), Some(&operator)).status,
            403
        );
        let mut forged_body: serde_json::Value = serde_json::from_slice(body).unwrap();
        forged_body["actor"] = serde_json::Value::String("forged-operator".into());
        forged_body["audit_id"] =
            serde_json::Value::String("e28331a8-a133-4756-b90c-91885fef40ee".into());
        let forged_body = serde_json::to_vec(&forged_body).unwrap();
        assert_eq!(
            api.route("PUT", &path, Some(&forged_body), Some(&admin))
                .status,
            400
        );
        let written = api.route("PUT", &path, Some(body), Some(&admin));
        assert_eq!(
            written.status,
            200,
            "{}",
            String::from_utf8_lossy(&written.body)
        );
        let written: serde_json::Value = serde_json::from_slice(&written.body).unwrap();
        assert_eq!(written["track_id"], track_id);
        assert_eq!(written["asset_id"], asset_id);
        assert_eq!(written["revision"], 1);
        assert_eq!(written["updated_by"], admin.account_id().as_str());
        assert_eq!(written["cues"].as_array().unwrap().len(), 1);

        let listed = api.route(
            "GET",
            &format!("/api/v1/assets/{asset_id}/caption-tracks"),
            None,
            Some(&operator),
        );
        assert_eq!(listed.status, 200);
        let listed: serde_json::Value = serde_json::from_slice(&listed.body).unwrap();
        assert_eq!(listed["asset_id"], asset_id);
        assert_eq!(listed["tracks"].as_array().unwrap().len(), 1);
        assert_eq!(listed["tracks"][0]["track_id"], track_id);
        assert_eq!(listed["tracks"][0]["cue_count"], 1);
        assert!(listed["tracks"][0].get("cues").is_none());

        let unknown_asset = api.route(
            "GET",
            &format!("/api/v1/assets/{}/caption-tracks", "b".repeat(64)),
            None,
            Some(&operator),
        );
        assert_eq!(unknown_asset.status, 404);

        let not_ready_asset_id = "c".repeat(64);
        api.storage
            .put_media_asset(&MediaAsset {
                asset_id: not_ready_asset_id.clone(),
                media_path: r"C:\TownLight\media\pending.ts".into(),
                duration_ms: 10_000,
                readiness: AssetReadiness::Missing,
            })
            .unwrap();
        let not_ready = api.route(
            "PUT",
            &format!(
                "/api/v1/assets/{not_ready_asset_id}/caption-tracks/6f8787a6-a829-4e85-a01e-650984c913ce"
            ),
            Some(body),
            Some(&admin),
        );
        assert_eq!(not_ready.status, 409);

        let occupied_slot = api.route(
            "PUT",
            &format!(
                "/api/v1/assets/{asset_id}/caption-tracks/6f8787a6-a829-4e85-a01e-650984c913ce"
            ),
            Some(body),
            Some(&admin),
        );
        assert_eq!(occupied_slot.status, 409);

        let uppercase_asset_path = format!(
            "/api/v1/assets/{}/caption-tracks/{track_id}",
            asset_id.to_uppercase()
        );
        assert_eq!(
            api.route("GET", &uppercase_asset_path, None, Some(&operator))
                .status,
            400
        );

        let wrong_asset_path = format!(
            "/api/v1/assets/{}/caption-tracks/{track_id}",
            "d".repeat(64)
        );
        assert_eq!(
            api.route("GET", &wrong_asset_path, None, Some(&operator))
                .status,
            404
        );

        let authority = api.route("GET", &path, None, Some(&operator));
        assert_eq!(authority.status, 200);
        let authority: serde_json::Value = serde_json::from_slice(&authority.body).unwrap();
        assert_eq!(authority["track"]["cues_sha256"], written["cues_sha256"]);
        assert_eq!(authority["audit"].as_array().unwrap().len(), 1);
        assert_eq!(authority["audit"][0]["actor"], admin.account_id().as_str());
        assert_eq!(authority["audit"][0]["cues"], written["cues"]);

        let stale = api.route("PUT", &path, Some(body), Some(&admin));
        assert_eq!(stale.status, 409);

        drop(api);
        let _ = std::fs::remove_dir_all(root);
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

    fn transport_station_box_probe() -> StationBoxProbeInput {
        let mut probe = station_box_probe();
        probe.product_version = "0.2.0".into();
        probe.source_commit = Some("0123456789abcdef0123456789abcdef01234567".into());
        probe
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
                external_receiver_evidence: Some("external receiver fixture".into()),
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
    fn commissioning_requires_a_current_passing_ten_minute_selected_sink_proof() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-commissioning-ten-minute-proof-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let probe = station_box_probe();
        let candidate_version = probe.product_version.clone();
        let source_commit = probe.source_commit.clone();
        let api = Api::open_with_station_box_probe(&database, &application, probe).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Commissioning output".into(),
                output_profile: station_schedule::OutputProfileId::Atsc720p5994,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Selected cable sink",
                    "127.0.0.1:5500",
                    -24.0,
                )],
                enabled: true,
            })
            .unwrap();
        let configuration_hash = api
            .storage
            .current_channel_output_configuration_hash(channel_id)
            .unwrap();
        let now = system_time_millis().unwrap();
        let proof = |proof_id: &str, sample_duration_ms: u64, captured_at_unix_ms: i64| {
            station_readiness::SinkLoudnessProof {
                proof_id: proof_id.into(),
                channel_id: channel_id.into(),
                sink_id: sink_id.into(),
                target_lufs_milli: -24_000,
                measured_lufs_milli: Some(-23_800),
                tolerance_lufs_milli: 2_000,
                outcome: station_readiness::LoudnessProofOutcome::Pass,
                receiver_acceptance: ReceiverAcceptance::NotAttempted,
                captured_at_unix_ms,
                sample_duration_ms,
                capture_bytes: 1_000_000,
                capture_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                    .into(),
                candidate_version: candidate_version.clone(),
                source_commit: source_commit.clone(),
                configuration_hash: configuration_hash.clone(),
                measurement_engine: "gstreamer-ebur128".into(),
                measurement_method: "station-media-assets::measure_output_loudness".into(),
                detail: "exact selected-sink output branch".into(),
            }
        };
        api.storage
            .append_sink_loudness_proof(
                &proof("33333333-4444-4555-8666-777777777777", 300_000, now),
                None,
            )
            .unwrap();

        let short = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        let short: serde_json::Value = serde_json::from_slice(&short.body).unwrap();
        let find = |report: &serde_json::Value| {
            report["checks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|check| check["id"] == "selected_sink_ten_minute_proof")
                .unwrap()
                .clone()
        };
        assert_eq!(find(&short)["status"], "blocked");

        api.storage
            .append_sink_loudness_proof(
                &proof("44444444-5555-4666-8777-888888888888", 600_000, now + 1),
                None,
            )
            .unwrap();
        let passing = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        let passing: serde_json::Value = serde_json::from_slice(&passing.body).unwrap();
        assert_eq!(find(&passing)["status"], "pass");
        assert!(
            find(&passing)["evidence"]
                .as_str()
                .unwrap()
                .contains("44444444-5555-4666-8777-888888888888")
        );

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
    fn setup_admin_receiver_receipt_is_server_bound_and_drives_headend_readiness() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-receiver-receipt-api-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Government".into(),
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
        let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let body = serde_json::to_vec(&serde_json::json!({
            "sink_id": sink_id,
            "outcome": "accepted",
            "method": "headend_operator_confirmation",
            "receiver_label": "Cable operator master control",
            "evidence_reference": "headend-ticket-1042",
            "detail": "Cable operator confirmed stable decoded video and audible program audio.",
            "observed_at_unix_ms": now - 1_000
        }))
        .unwrap();

        let created = api.route(
            "POST",
            &format!("/api/v1/channels/{channel_id}/receiver-receipts"),
            Some(&body),
            Some(&setup_admin),
        );
        assert_eq!(
            created.status,
            201,
            "{}",
            String::from_utf8_lossy(&created.body)
        );
        let created: serde_json::Value = serde_json::from_slice(&created.body).unwrap();
        assert_eq!(created["channel_id"], channel_id);
        assert_eq!(created["sink_id"], sink_id);
        assert_eq!(created["candidate_version"], "0.1.0");
        assert_eq!(
            created["source_commit"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        );
        assert_eq!(created["recorded_by"], setup_admin.account_id().as_str());
        assert_eq!(created["current"], true);

        let listed = api.route(
            "GET",
            &format!("/api/v1/channels/{channel_id}/receiver-receipts"),
            None,
            Some(&setup_admin),
        );
        assert_eq!(listed.status, 200);
        let listed: serde_json::Value = serde_json::from_slice(&listed.body).unwrap();
        assert_eq!(listed["receipts"].as_array().unwrap().len(), 1);

        let readiness = api.handle_in_process("GET", "/api/v1/headend-readiness", None);
        assert_eq!(readiness.status, 200);
        let readiness: serde_json::Value = serde_json::from_slice(&readiness.body).unwrap();
        assert_eq!(readiness["rollup"]["color"], "green");
        assert_eq!(
            readiness["channels"][0]["sinks"][0]["external_receiver"],
            "verified"
        );
        assert!(
            readiness["channels"][0]["sinks"][0]["external_receiver_evidence"]
                .as_str()
                .unwrap()
                .contains("headend-ticket-1042")
        );
        assert!(
            !readiness["channels"][0]["sinks"][0]["evidence"]
                .as_str()
                .unwrap()
                .contains("headend-ticket-1042")
        );

        let loudness = api.handle_in_process(
            "GET",
            &format!("/api/v1/channels/{channel_id}/loudness-proof"),
            None,
        );
        assert_eq!(loudness.status, 200);
        let loudness: serde_json::Value = serde_json::from_slice(&loudness.body).unwrap();
        assert_eq!(loudness["sinks"][0]["receiver_acceptance"], "verified");
        assert_eq!(loudness["sinks"][0]["measurement_status"], "not_run");

        let commissioning = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        assert_eq!(commissioning.status, 200);
        let commissioning: serde_json::Value = serde_json::from_slice(&commissioning.body).unwrap();
        let receiver_check = commissioning["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|check| check["id"] == "external_receiver_proof")
            .unwrap();
        assert_eq!(receiver_check["status"], "pass");

        let rejected_body = serde_json::to_vec(&serde_json::json!({
            "sink_id": sink_id,
            "outcome": "rejected",
            "method": "receiver_telemetry",
            "receiver_label": "Cable receiver telemetry",
            "evidence_reference": "headend-ticket-1043",
            "detail": "Receiver reported loss of program decode after the earlier acceptance.",
            "observed_at_unix_ms": now - 500
        }))
        .unwrap();
        let rejected = api.route(
            "POST",
            &format!("/api/v1/channels/{channel_id}/receiver-receipts"),
            Some(&rejected_body),
            Some(&setup_admin),
        );
        assert_eq!(rejected.status, 201);
        let listed = api.route(
            "GET",
            &format!("/api/v1/channels/{channel_id}/receiver-receipts"),
            None,
            Some(&setup_admin),
        );
        let listed: serde_json::Value = serde_json::from_slice(&listed.body).unwrap();
        let current = listed["receipts"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|receipt| receipt["current"] == true)
            .collect::<Vec<_>>();
        assert_eq!(current.len(), 1);
        assert_eq!(current[0]["outcome"], "rejected");
        let readiness = api.handle_in_process("GET", "/api/v1/headend-readiness", None);
        let readiness: serde_json::Value = serde_json::from_slice(&readiness.body).unwrap();
        assert_eq!(
            readiness["channels"][0]["sinks"][0]["external_receiver"],
            "not_verified"
        );

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn receiver_receipt_rejects_non_admin_forgery_and_unverified_candidate() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-receiver-receipt-auth-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let mut probe = station_box_probe();
        probe.release.candidate_verified = false;
        probe.release.state = StationReadinessState::Blocked;
        let api = Api::open_with_station_box_probe(&database, &application, probe).unwrap();
        let channel_id = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Government".into(),
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
        let body = serde_json::to_vec(&serde_json::json!({
            "sink_id": sink_id,
            "outcome": "accepted",
            "method": "receiver_telemetry",
            "receiver_label": "IRD telemetry",
            "evidence_reference": "telemetry-record-7",
            "detail": "Receiver reported decoded program.",
            "observed_at_unix_ms": system_time_millis().unwrap(),
            "candidate_version": "forged",
            "source_commit": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        }))
        .unwrap();
        let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let forged = api.route(
            "POST",
            &format!("/api/v1/channels/{channel_id}/receiver-receipts"),
            Some(&body),
            Some(&setup_admin),
        );
        assert_eq!(forged.status, 400);

        let clean_body = serde_json::to_vec(&serde_json::json!({
            "sink_id": sink_id,
            "outcome": "accepted",
            "method": "receiver_telemetry",
            "receiver_label": "IRD telemetry",
            "evidence_reference": "telemetry-record-7",
            "detail": "Receiver reported decoded program.",
            "observed_at_unix_ms": system_time_millis().unwrap()
        }))
        .unwrap();
        let meeting_operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let forbidden = api.route(
            "POST",
            &format!("/api/v1/channels/{channel_id}/receiver-receipts"),
            Some(&clean_body),
            Some(&meeting_operator),
        );
        assert_eq!(forbidden.status, 403);
        let unverified = api.route(
            "POST",
            &format!("/api/v1/channels/{channel_id}/receiver-receipts"),
            Some(&clean_body),
            Some(&setup_admin),
        );
        assert_eq!(unverified.status, 409);
        let unverified: serde_json::Value = serde_json::from_slice(&unverified.body).unwrap();
        assert_eq!(unverified["error"]["code"], "candidate_not_verified");

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
    fn authenticated_transport_post_and_get_derive_server_authority() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "townlight-transport-job-api-{}-{nonce}",
            std::process::id()
        ));
        let application = root.join("application");
        let database = root.join("data/station.db");
        std::fs::create_dir_all(database.parent().unwrap()).unwrap();
        std::fs::create_dir_all(&application).unwrap();
        install_transport_candidate(database.parent().unwrap(), &application);
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Transport proof".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Cable primary",
                    "127.0.0.1:5500",
                    -24.0,
                )],
                enabled: true,
            })
            .unwrap();
        api.storage
            .write_transport_target_config(
                channel_id,
                sink_id,
                None,
                &TargetDeviceConfig {
                    target_id: "receiver-1".into(),
                    label: "Headend receiver".into(),
                    protocol: TargetProtocol::HttpsApi,
                    host: "receiver.example.test".into(),
                    port: 443,
                    credential_configured: true,
                    revision: 1,
                    created_at_unix_ms: 1,
                    updated_at_unix_ms: 1,
                    updated_by: "setup-admin".into(),
                },
            )
            .unwrap();
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let forbidden = api.handle_authenticated(
            "POST",
            &format!("/api/v1/channels/{channel_id}/transport-proof/jobs"),
            None,
            &test_actor(station_auth::OperatorRole::MeetingOperator),
        );
        assert_eq!(forbidden.status, 403);
        let forged = api.handle_authenticated(
            "POST",
            &format!("/api/v1/channels/{channel_id}/transport-proof/jobs"),
            Some(br#"{"candidate_version":"forged"}"#),
            &admin,
        );
        assert_eq!(forged.status, 400);
        let created = api.handle_authenticated(
            "POST",
            &format!("/api/v1/channels/{channel_id}/transport-proof/jobs"),
            None,
            &admin,
        );
        assert_eq!(
            created.status,
            202,
            "{}",
            String::from_utf8_lossy(&created.body)
        );
        let created: serde_json::Value = serde_json::from_slice(&created.body).unwrap();
        assert_eq!(created["state"], "pending");
        assert_eq!(created["target_id"], "receiver-1");
        assert_eq!(created["candidate_version"], "0.2.0");
        let job_id = created["job_id"].as_str().unwrap();
        let status = api.handle_authenticated(
            "GET",
            &format!("/api/v1/channels/{channel_id}/transport-proof/jobs/{job_id}"),
            None,
            &admin,
        );
        assert_eq!(status.status, 200);
        let status: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
        assert_eq!(status["job_id"], job_id);
        assert_eq!(status["state"], "pending");
        drop(api);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn caption_proof_job_post_derives_live_authority_and_get_returns_status() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "townlight-caption-proof-job-api-{}-{nonce}",
            std::process::id()
        ));
        let application = root.join("application");
        let media_path = root.join("captioned.ts");
        std::fs::create_dir_all(&application).unwrap();
        std::fs::write(&media_path, b"test media boundary").unwrap();
        let database = root.join("station.db");
        let mut api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        let asset_id = "b".repeat(64);
        let track_id = "33333333-4444-4555-8666-777777777777";
        let item_id = "44444444-5555-4666-8777-888888888888";
        let report_id = "caption-report-1";
        let now = system_time_millis().unwrap();
        let mut channel = ChannelConfiguration {
            channel_id: channel_id.into(),
            display_name: "Caption proof output".into(),
            output_profile: station_schedule::OutputProfileId::Proof360p30,
            output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                sink_id,
                "Proof output",
                "127.0.0.1:5500",
                -16.0,
            )],
            enabled: false,
        };
        api.storage.put_channel(&channel).unwrap();
        api.storage
            .put_media_asset(&MediaAsset {
                asset_id: asset_id.clone(),
                media_path: media_path.to_string_lossy().into_owned(),
                duration_ms: 20_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        api.storage
            .write_asset_caption_track(&station_captions::AssetCaptionTrackWrite {
                track_id: track_id.into(),
                asset_id: asset_id.clone(),
                language: "en-US".into(),
                service_number: 1,
                source_kind: station_captions::CaptionTrackSourceKind::External,
                label: "Reviewed English".into(),
                cues: vec![station_captions::AssetCaptionCue {
                    cue_id: "55555555-6666-4777-8888-999999999999".into(),
                    start_ms: 10_000,
                    duration_ms: 5_000,
                    text: "Town Council is now in session".into(),
                }],
                expected_revision: 0,
                actor: "setup-admin".into(),
                occurred_at_unix_ms: now - 2_000,
                audit_id: "66666666-7777-4888-8999-aaaaaaaaaaaa".into(),
            })
            .unwrap();
        api.storage
            .write_channel_caption_policy(
                channel_id,
                ChannelCaptionMode::Cea708,
                1,
                "en-US",
                0,
                "setup-admin",
                now - 2_000,
                "77777777-8888-4999-8aaa-bbbbbbbbbbbb",
            )
            .unwrap();
        api.storage
            .put_schedule_item(&ScheduleItem {
                item_id: item_id.into(),
                revision: 1,
                channel_id: channel_id.into(),
                asset_id: asset_id.clone(),
                title: "Caption proof program".into(),
                starts_at_unix_ms: now - 1_000,
                duration_ms: 20_000,
                state: ScheduleState::Draft,
            })
            .unwrap();
        api.storage
            .commit_schedule(
                report_id,
                "caption-plan-1",
                item_id,
                "meeting-operator",
                now - 1_500,
                "Caption proof fixture",
            )
            .unwrap();
        api.storage
            .advance_dispatch_status(report_id, DispatchStatus::Pending, DispatchStatus::Queued)
            .unwrap();
        api.storage
            .advance_dispatch_status(
                report_id,
                DispatchStatus::Queued,
                DispatchStatus::Acknowledged,
            )
            .unwrap();
        channel.enabled = true;
        api.storage.put_channel(&channel).unwrap();
        api.storage
            .put_channel_runtime(&station_domain::ChannelRuntimeObservation {
                channel_id: channel_id.into(),
                state: ChannelRuntimeState::OnAir,
                on_air_asset_id: Some(asset_id.clone()),
                state_started_at_unix_ms: now - 20_000,
                heartbeat_at_unix_ms: now - 20_000,
                detail: Some("scheduled asset is advancing".into()),
            })
            .unwrap();

        let request_body = format!(
            r#"{{"report_id":"{report_id}","sink_id":"{sink_id}","capture_duration_ms":3000,"max_capture_bytes":1000000}}"#
        );
        let forged_body = format!(
            r#"{{"report_id":"{report_id}","sink_id":"{sink_id}","capture_duration_ms":3000,"max_capture_bytes":1000000,"asset_id":"{asset_id}"}}"#
        );
        let forged = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/caption-proof"),
            Some(forged_body.as_bytes()),
        );
        assert_eq!(forged.status, 400);
        let forged: serde_json::Value = serde_json::from_slice(&forged.body).unwrap();
        assert_eq!(forged["error"]["code"], "invalid_json");

        let stale_runtime = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/caption-proof"),
            Some(request_body.as_bytes()),
        );
        assert_eq!(stale_runtime.status, 409);
        let stale_runtime: serde_json::Value = serde_json::from_slice(&stale_runtime.body).unwrap();
        assert_eq!(stale_runtime["error"]["code"], "runtime_not_on_air");

        api.storage
            .put_channel_runtime(&station_domain::ChannelRuntimeObservation {
                channel_id: channel_id.into(),
                state: ChannelRuntimeState::OnAir,
                on_air_asset_id: Some(asset_id.clone()),
                state_started_at_unix_ms: now - 20_000,
                heartbeat_at_unix_ms: now,
                detail: Some("scheduled asset is advancing".into()),
            })
            .unwrap();
        let no_future_window_body = format!(
            r#"{{"report_id":"{report_id}","sink_id":"{sink_id}","capture_duration_ms":120000,"max_capture_bytes":1000000}}"#
        );
        let no_future_window = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/caption-proof"),
            Some(no_future_window_body.as_bytes()),
        );
        assert_eq!(no_future_window.status, 409);
        let no_future_window: serde_json::Value =
            serde_json::from_slice(&no_future_window.body).unwrap();
        assert_eq!(
            no_future_window["error"]["code"],
            "capture_window_unavailable"
        );
        let mut unverified_probe = station_box_probe();
        unverified_probe.release.candidate_verified = false;
        unverified_probe.release.state = StationReadinessState::Blocked;
        api.station_box_probe_override = Some(unverified_probe);
        let unverified = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/caption-proof"),
            Some(request_body.as_bytes()),
        );
        assert_eq!(unverified.status, 409);
        let unverified: serde_json::Value = serde_json::from_slice(&unverified.body).unwrap();
        assert_eq!(unverified["error"]["code"], "candidate_not_verified");
        api.station_box_probe_override = Some(station_box_probe());

        let response = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/caption-proof"),
            Some(request_body.as_bytes()),
        );
        assert_eq!(
            response.status,
            202,
            "{}",
            String::from_utf8_lossy(&response.body)
        );
        let created: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
        let job_id = created["job_id"].as_str().unwrap();
        assert_eq!(created["state"], "pending");
        assert_eq!(created["asset_id"], asset_id);
        assert_eq!(created["track_id"], track_id);
        assert_eq!(created["track_revision"], 1);
        assert_eq!(created["caption_policy_revision"], 1);
        assert_eq!(created["language"], "en-US");
        assert_eq!(created["service_number"], 1);
        assert_eq!(created["expected_cue_count"], 1);
        assert_eq!(
            created["capture_started_asset_offset_ms"].as_u64(),
            Some(10_000),
            "the API must select the future cue window after reserving the bounded controller-start lead"
        );
        assert_eq!(created["candidate_version"], "0.1.0");
        assert_eq!(
            created["source_commit"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        );

        let status = api.handle_in_process(
            "GET",
            &format!("/api/v1/channels/{channel_id}/caption-proof/jobs/{job_id}"),
            None,
        );
        assert_eq!(status.status, 200);
        let status: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
        assert_eq!(status["job_id"], job_id);
        assert_eq!(status["report_id"], report_id);
        assert_eq!(status["sink_id"], sink_id);
        assert!(status["capture"].is_null());

        let wrong_channel = api.handle_in_process(
            "GET",
            &format!(
                "/api/v1/channels/99999999-aaaa-4bbb-8ccc-dddddddddddd/caption-proof/jobs/{job_id}"
            ),
            None,
        );
        assert_eq!(wrong_channel.status, 404);

        let duplicate = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/caption-proof"),
            Some(request_body.as_bytes()),
        );
        assert_eq!(duplicate.status, 409);
        let duplicate: serde_json::Value = serde_json::from_slice(&duplicate.body).unwrap();
        assert_eq!(duplicate["error"]["code"], "job_already_active");

        let history = api.handle_in_process(
            "GET",
            &format!("/api/v1/channels/{channel_id}/caption-proof"),
            None,
        );
        assert_eq!(
            history.status,
            200,
            "{}",
            String::from_utf8_lossy(&history.body)
        );
        let history: serde_json::Value = serde_json::from_slice(&history.body).unwrap();
        assert_eq!(history["channel_id"], channel_id);
        assert_eq!(history["proofs"], serde_json::json!([]));

        let configuration_hash = api
            .storage
            .current_channel_output_configuration_hash(channel_id)
            .unwrap();
        let track = api
            .storage
            .read_asset_caption_track(track_id)
            .unwrap()
            .unwrap();
        let expected = expected_caption_evidence_for_window(&track, 20_000, 10_000, 3_000).unwrap();
        let proof = CaptionEgressProof {
            proof_id: "88888888-9999-4aaa-8bbb-cccccccccccc".into(),
            channel_id: channel_id.into(),
            sink_id: sink_id.into(),
            report_id: report_id.into(),
            asset_id: asset_id.clone(),
            track_id: track_id.into(),
            track_revision: 1,
            cues_sha256: track.cues_sha256,
            caption_policy_revision: 1,
            language: "en-US".into(),
            service_number: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            configuration_hash,
            capture_started_asset_offset_ms: 10_000,
            capture_duration_ms: 3_000,
            capture_bytes: 1_000,
            capture_sha256: "d".repeat(64),
            artifact_relative_path: "caption-proofs/88888888-9999-4aaa-8bbb-cccccccccccc.ts".into(),
            expected_cue_count: expected.cue_count,
            decoded_cue_count: expected.cue_count,
            expected_text_sha256: expected.text_sha256.clone(),
            decoded_text_sha256: expected.text_sha256,
            decoder_engine: "GStreamer 1.28.6".into(),
            decoder_method: "independent MPEG-TS CEA-708 decode-back".into(),
            outcome: station_captions::CaptionProofOutcome::Verified,
            captured_at_unix_ms: now,
        };
        api.storage.append_caption_egress_proof(&proof).unwrap();

        let commissioning = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        assert_eq!(commissioning.status, 200);
        let commissioning: serde_json::Value = serde_json::from_slice(&commissioning.body).unwrap();
        let caption_check = commissioning["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|check| check["id"] == "cea708_captions")
            .unwrap();
        assert_eq!(caption_check["status"], "pass");

        let mut stale_candidate = station_box_probe();
        stale_candidate.source_commit = Some("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into());
        api.station_box_probe_override = Some(stale_candidate);
        *api.station_box_cache.lock().unwrap() = None;
        let stale_candidate = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        let stale_candidate: serde_json::Value =
            serde_json::from_slice(&stale_candidate.body).unwrap();
        let caption_check = stale_candidate["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|check| check["id"] == "cea708_captions")
            .unwrap();
        assert_eq!(caption_check["status"], "blocked");

        api.station_box_probe_override = Some(station_box_probe());
        *api.station_box_cache.lock().unwrap() = None;
        channel.output_sinks[0].destination = "127.0.0.1:5501".into();
        api.storage.put_channel(&channel).unwrap();
        let stale_configuration =
            api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        let stale_configuration: serde_json::Value =
            serde_json::from_slice(&stale_configuration.body).unwrap();
        let caption_check = stale_configuration["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|check| check["id"] == "cea708_captions")
            .unwrap();
        assert_eq!(caption_check["status"], "blocked");

        // Recommitting the same item creates a new schedule revision. The
        // existing proof request must reject the old acknowledged report.
        let current = api.storage.read_schedule_item(item_id).unwrap().unwrap();
        let mut draft = current.clone();
        draft.revision += 1;
        draft.state = ScheduleState::Draft;
        draft.title = "Caption proof program revision two".into();
        api.storage.put_schedule_item(&draft).unwrap();
        api.storage
            .commit_schedule(
                "caption-report-recommit",
                "caption-plan-recommit",
                item_id,
                "meeting-operator",
                now + 1,
                "Recommitted after the original proof request.",
            )
            .unwrap();
        let stale_revision = api.handle_in_process(
            "POST",
            &format!("/api/v1/channels/{channel_id}/caption-proof"),
            Some(request_body.as_bytes()),
        );
        assert_eq!(stale_revision.status, 409);
        let stale_revision: serde_json::Value =
            serde_json::from_slice(&stale_revision.body).unwrap();
        assert_eq!(stale_revision["error"]["code"], "schedule_binding_changed");

        drop(api);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn caption_proof_execution_lead_reserves_a_bounded_controller_start_budget() {
        assert_eq!(CAPTION_PROOF_EXECUTION_LEAD_MS, 5_000);
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
                    r#"{{"sink_id":"{sink_id}","sample_duration_ms":600001,"max_capture_bytes":1000000}}"#
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
                    r#"{{"sink_id":"{sink_id}","sample_duration_ms":3000,"max_capture_bytes":1073741825}}"#
                )
                .as_bytes(),
            ),
        );
        assert_eq!(response.status, 422);

        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn http_parser_allows_large_bodies_only_for_exact_image_and_caption_routes() {
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

        let caption_path = format!(
            "/api/v1/assets/{}/caption-tracks/65a26343-0339-4654-a92c-4bf9384f18df",
            "a".repeat(64)
        );
        let mut caption_request = format!(
            "PUT {caption_path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {}\r\n\r\n",
            image.len()
        )
        .into_bytes();
        caption_request.extend_from_slice(&image);
        let parsed_caption =
            read_request_over_tcp(caption_request).expect("caption request should parse");
        assert!(parsed_caption.len() > MAX_SMALL_REQUEST_BODY);

        let mut oversized_caption = format!(
            "PUT {caption_path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {}\r\n\r\n",
            MAX_CAPTION_TRACK_BODY + 1
        )
        .into_bytes();
        oversized_caption.resize(oversized_caption.len() + MAX_CAPTION_TRACK_BODY + 1, 0);
        let rejection = read_request_over_tcp(oversized_caption).unwrap_err();
        assert_eq!(rejection.status, 413);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&rejection.body).unwrap()["error"]["message"],
            "Caption track requests may not exceed 8 MiB."
        );

        let malformed_caption_path = format!("{caption_path}/extra");
        let mut malformed_caption = format!(
            "PUT {malformed_caption_path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {}\r\n\r\n",
            image.len()
        )
        .into_bytes();
        malformed_caption.extend_from_slice(&image);
        assert_eq!(
            read_request_over_tcp(malformed_caption).unwrap_err().status,
            413
        );

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

    #[test]
    fn automation_policy_api_uses_setup_admin_cas_and_server_derived_evidence_binding() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-automation-policy-api-{nonce}.db"));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let adapter = station_automation_policy::AutomationAdapterDescriptor {
            adapter_id: "11111111-2222-4333-8444-555555555555".into(),
            location: station_automation_policy::AdapterLocation::Local,
            model_key: "gemma4-12b-ollama".into(),
            model_identifier: "gemma4:12b".into(),
            supported_features: vec![AutomationFeature::Summaries],
            credential_availability: station_automation_policy::CredentialAvailability::NotRequired,
            revision: 1,
        };
        api.storage
            .write_automation_adapter_descriptor(&adapter, None, "seed-authority", 1)
            .unwrap();
        let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let policy_id = "22222222-2222-4333-8444-555555555555";
        let request = serde_json::to_vec(&serde_json::json!({
            "feature": "summaries",
            "operator_selected_adapter_id": adapter.adapter_id,
            "adaptive_default": null,
            "configuration_hash": "b".repeat(64),
            "expected_revision": null
        }))
        .unwrap();
        let created = api.handle_authenticated(
            "PUT",
            &format!("/api/v1/automation/policies/{policy_id}"),
            Some(&request),
            &setup_admin,
        );
        assert_eq!(created.status, 201);
        assert_eq!(
            api.handle_in_process("GET", "/api/v1/automation/policies", None)
                .status,
            403
        );
        let status = api.handle_authenticated(
            "GET",
            &format!("/api/v1/automation/policies/{policy_id}/status"),
            None,
            &setup_admin,
        );
        let status: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
        assert_eq!(status["status"], "not_tested");

        let observation = TrustedAutomationConnectionTestObservation {
            evidence_id: "33333333-2222-4333-8444-555555555555".into(),
            policy_id: policy_id.into(),
            adapter_id: adapter.adapter_id.clone(),
            policy_revision: 1,
            adapter_revision: 1,
            configuration_hash: "b".repeat(64),
            outcome: station_automation_policy::ConnectionTestOutcome::Passed,
            detail: "trusted test passed".into(),
        };
        let before_recorded = system_time_millis().unwrap();
        api.record_trusted_automation_connection_test_evidence(observation)
            .unwrap();
        let persisted = api
            .storage
            .list_automation_connection_test_evidence(policy_id, 1)
            .unwrap()
            .pop()
            .unwrap();
        assert_eq!(
            persisted.candidate.source_commit,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        );
        assert!(persisted.tested_at_unix_ms >= before_recorded);
        assert_eq!(
            persisted.expires_at_unix_ms - persisted.tested_at_unix_ms,
            AUTOMATION_CONNECTION_TEST_TTL_MS
        );
        assert_eq!(
            api.handle_in_process(
                "POST",
                &format!("/api/v1/automation/policies/{policy_id}/evidence"),
                Some(br#"{"candidate":{"source_commit":"forged"},"tested_at_unix_ms":0}"#),
            )
            .status,
            404
        );
        let status = api.handle_authenticated(
            "GET",
            &format!("/api/v1/automation/policies/{policy_id}/status"),
            None,
            &setup_admin,
        );
        let status: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
        assert_eq!(status["status"], "passed");

        let history = api.handle_authenticated(
            "GET",
            &format!("/api/v1/automation/policies/{policy_id}/history?limit=1"),
            None,
            &setup_admin,
        );
        assert_eq!(history.status, 200);
        let non_admin = test_actor(station_auth::OperatorRole::MeetingOperator);
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &format!("/api/v1/automation/policies/{policy_id}"),
                Some(&request),
                &non_admin,
            )
            .status,
            403
        );

        drop(api);
        let _ = std::fs::remove_file(database);
        let _ = std::fs::remove_dir_all(application);
    }

    #[test]
    fn automation_execution_status_requires_an_authenticated_automation_reader() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-automation-execution-api-{nonce}.db"));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();

        let response = api.handle_in_process(
            "GET",
            "/api/v1/automation/jobs/33333333-2222-4333-8444-555555555555",
            None,
        );
        assert_eq!(response.status, 403);

        drop(api);
        let _ = std::fs::remove_file(database);
        let _ = std::fs::remove_dir_all(application);
    }

    #[test]
    fn automation_execution_status_returns_durable_truth_without_live_claim_identity() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-automation-status-api-{nonce}"));
        let database = root.join("station.db");
        let application = root.join("application");
        std::fs::create_dir_all(&application).unwrap();
        install_transport_candidate(&root, &application);
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let adapter_id = "11111111-2222-4333-8444-555555555555";
        let policy_id = "22222222-2222-4333-8444-555555555555";
        let job_id = "33333333-2222-4333-8444-555555555555";
        let configuration_hash = "b".repeat(64);
        let candidate = read_verified_install_authority(&root, &application).unwrap();
        let candidate_identity = AutomationCandidateIdentity {
            version: candidate.version().into(),
            source_commit: candidate.source_commit().into(),
        };
        let adapter = station_automation_policy::AutomationAdapterDescriptor {
            adapter_id: adapter_id.into(),
            location: station_automation_policy::AdapterLocation::Local,
            model_key: "test-summary-adapter".into(),
            model_identifier: "test/model-v1".into(),
            supported_features: vec![AutomationFeature::Summaries],
            credential_availability: station_automation_policy::CredentialAvailability::NotRequired,
            revision: 1,
        };
        let policy = AutomationFeaturePolicy {
            policy_id: policy_id.into(),
            feature: AutomationFeature::Summaries,
            operator_selected_adapter_id: Some(adapter_id.into()),
            adaptive_default: None,
            configuration_hash: configuration_hash.clone(),
            revision: 1,
        };
        api.storage
            .write_automation_adapter_descriptor(&adapter, None, "setup-admin", 10)
            .unwrap();
        api.storage
            .write_automation_feature_policy(&policy, None, "setup-admin", 20)
            .unwrap();
        api.storage
            .append_automation_connection_test_evidence(
                &AutomationConnectionTestEvidence {
                    evidence_id: "44444444-2222-4333-8444-555555555555".into(),
                    policy_id: policy_id.into(),
                    adapter_id: adapter_id.into(),
                    policy_revision: 1,
                    adapter_revision: 1,
                    configuration_hash: configuration_hash.clone(),
                    candidate: candidate_identity.clone(),
                    outcome: ConnectionTestOutcome::Passed,
                    tested_at_unix_ms: 50,
                    expires_at_unix_ms: 10_000,
                    detail: "trusted test passed".into(),
                },
                &candidate_identity,
                50,
            )
            .unwrap();
        api.storage
            .enqueue_automation_execution_job(
                &AutomationExecutionRequest {
                    job_id: job_id.into(),
                    feature: AutomationFeature::Summaries,
                    policy_id: policy_id.into(),
                    policy_revision: 1,
                    adapter_id: adapter_id.into(),
                    adapter_revision: 1,
                    configuration_hash,
                    candidate: candidate_identity,
                    install_generation: candidate.install_generation(),
                    input: station_automation_policy::AutomationInputArtifact {
                        kind: station_automation_policy::AutomationInputKind::Transcript,
                        artifact_sha256: "c".repeat(64),
                        bytes: 4_096,
                        media_type: "text/plain; charset=utf-8".into(),
                        language: Some("en-US".into()),
                    },
                    requested_output_language: None,
                    requested_at_unix_ms: 100,
                },
                &candidate,
                100,
            )
            .unwrap();

        let response = api.handle_authenticated(
            "GET",
            &format!("/api/v1/automation/jobs/{job_id}"),
            None,
            &test_actor(station_auth::OperatorRole::MeetingOperator),
        );
        assert_eq!(response.status, 200);
        let body: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
        assert_eq!(body["request"]["job_id"], job_id);
        assert_eq!(body["state"], "pending");
        assert_eq!(body["next_attempt_at_unix_ms"], 100);
        assert!(body.get("lease_owner").is_none());
        assert!(body.get("claim_id").is_none());

        drop(api);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn publishing_authority_api_and_commissioning_require_current_trusted_tier_evidence() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-publishing-api-{nonce}"));
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        let application = root.join("application");
        std::fs::create_dir_all(&application).unwrap();
        install_transport_candidate(&root, &application);
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let reader = test_actor(station_auth::OperatorRole::MeetingOperator);
        let targets = [
            (
                "11111111-2222-4333-8444-555555555551",
                "22222222-2222-4333-8444-555555555551",
                station_publishing::PublishTargetKind::Portal,
            ),
            (
                "11111111-2222-4333-8444-555555555552",
                "22222222-2222-4333-8444-555555555552",
                station_publishing::PublishTargetKind::InternetArchive,
            ),
            (
                "11111111-2222-4333-8444-555555555553",
                "22222222-2222-4333-8444-555555555553",
                station_publishing::PublishTargetKind::LocalNas,
            ),
            (
                "11111111-2222-4333-8444-555555555554",
                "22222222-2222-4333-8444-555555555554",
                station_publishing::PublishTargetKind::YouTubeLive,
            ),
        ];
        for (index, (account_id, target_id, kind)) in targets.iter().enumerate() {
            let account_request = serde_json::to_vec(&serde_json::json!({
                "kind": kind, "label": format!("{kind:?} account"), "enabled": true,
                "expected_revision": null
            }))
            .unwrap();
            assert_eq!(
                api.handle_authenticated(
                    "PUT",
                    &format!("/api/v1/publishing/accounts/{account_id}"),
                    Some(&account_request),
                    &setup_admin
                )
                .status,
                201
            );
            let credential_request = serde_json::to_vec(&serde_json::json!({
                "expected_account_revision": 1,
                "credential": format!("provider-secret-{index}")
            }))
            .unwrap();
            let credential_response = api.handle_authenticated(
                "PUT",
                &format!("/api/v1/publishing/accounts/{account_id}/credential"),
                Some(&credential_request),
                &setup_admin,
            );
            assert_eq!(credential_response.status, 200);
            assert!(
                !credential_response
                    .body
                    .windows(15)
                    .any(|bytes| bytes == b"provider-secret")
            );
            let configuration = match kind {
                station_publishing::PublishTargetKind::Portal => serde_json::json!({
                    "kind": "portal", "endpoint": "https://portal.example.test/api/media"
                }),
                station_publishing::PublishTargetKind::InternetArchive => serde_json::json!({
                    "kind": "internet_archive", "item_identifier": "townlight-station"
                }),
                station_publishing::PublishTargetKind::LocalNas => serde_json::json!({
                    "kind": "local_nas", "managed_root": r"\\station-nas\archive"
                }),
                station_publishing::PublishTargetKind::YouTubeLive => serde_json::json!({
                    "kind": "you_tube_live", "channel_id": "townlight-channel",
                    "broadcast_id": "townlight-broadcast", "stream_id": "townlight-stream",
                    "privacy_status": "public"
                }),
                _ => unreachable!("required publishing test target"),
            };
            let target_request = serde_json::to_vec(&serde_json::json!({
                "account_id": account_id, "kind": kind, "label": format!("{kind:?} target"),
                "enabled": true, "configuration": configuration, "expected_revision": null
            }))
            .unwrap();
            assert_eq!(
                api.handle_authenticated(
                    "PUT",
                    &format!("/api/v1/publishing/targets/{target_id}"),
                    Some(&target_request),
                    &setup_admin
                )
                .status,
                201
            );
            api.record_trusted_publish_connection_test_evidence(
                TrustedPublishConnectionTestObservation {
                    evidence_id: format!("33333333-2222-4333-8444-55555555555{}", index + 1),
                    target_id: (*target_id).into(),
                    outcome: station_publishing::ConnectionTestOutcome::Passed,
                    detail: "Trusted provider connection test passed.".into(),
                },
            )
            .unwrap();
        }
        let media_path = root.join("publish-media.ts");
        std::fs::write(&media_path, b"exact publish media").unwrap();
        let media_sha256 = format!("{:x}", Sha256::digest(b"exact publish media"));
        api.storage
            .put_media_asset(&MediaAsset {
                asset_id: media_sha256.clone(),
                media_path: media_path.to_string_lossy().into_owned(),
                duration_ms: 60_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        let request_path = format!(
            "/api/v1/publishing/targets/{}/assets/{media_sha256}/jobs",
            targets[0].1
        );
        assert_eq!(
            api.handle_authenticated("POST", &request_path, Some(br#"{}"#), &setup_admin)
                .status,
            400,
            "publish execution is bodyless"
        );
        let queued = api.handle_authenticated("POST", &request_path, None, &setup_admin);
        assert_eq!(
            queued.status,
            202,
            "{}",
            String::from_utf8_lossy(&queued.body)
        );
        let queued: serde_json::Value = serde_json::from_slice(&queued.body).unwrap();
        let job_id = queued["job"]["job_id"].as_str().unwrap();
        assert_eq!(queued["state"], "pending");
        assert_eq!(queued["receipts"].as_array().unwrap().len(), 0);
        let nas_request_path = format!(
            "/api/v1/publishing/targets/{}/assets/{media_sha256}/jobs",
            targets[2].1
        );
        let nas_queued = api.handle_authenticated("POST", &nas_request_path, None, &setup_admin);
        assert_eq!(
            nas_queued.status,
            202,
            "Local NAS queueing must use the same server-owned authority path: {}",
            String::from_utf8_lossy(&nas_queued.body)
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/publishing/targets/{}/jobs/{job_id}", targets[0].1),
                None,
                &reader,
            )
            .status,
            200
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/publishing/targets/{}/jobs?limit=10", targets[0].1),
                None,
                &reader,
            )
            .status,
            200
        );
        assert_eq!(
            api.handle_in_process("GET", "/api/v1/publishing/accounts", None)
                .status,
            403
        );
        let account_list =
            api.handle_authenticated("GET", "/api/v1/publishing/accounts", None, &reader);
        assert_eq!(
            account_list.status,
            200,
            "{}",
            String::from_utf8_lossy(&account_list.body)
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                "/api/v1/publishing/accounts/11111111-2222-4333-8444-555555555551/history?limit=1",
                None,
                &reader
            )
            .status,
            200
        );
        assert_eq!(
            api.handle_in_process(
                "POST",
                "/api/v1/publishing/targets/22222222-2222-4333-8444-555555555551/evidence",
                Some(br#"{"candidate":{"source_commit":"forged"}}"#)
            )
            .status,
            404
        );

        let report = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        assert_eq!(report.status, 200);
        let report: serde_json::Value = serde_json::from_slice(&report.body).unwrap();
        let publish = report["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|check| check["id"] == "publish_targets")
            .unwrap();
        assert_eq!(publish["status"], "blocked");
        assert!(
            publish["evidence"]
                .as_str()
                .unwrap()
                .contains("provider execution receipts")
        );

        drop(api);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn custom_metadata_api_derives_authority_and_rejects_rebinding() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-metadata-api-{nonce}.db"));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let asset_id = "a".repeat(64);
        let replacement_asset_id = "b".repeat(64);
        for asset_id in [&asset_id, &replacement_asset_id] {
            api.storage
                .put_media_asset(&MediaAsset {
                    asset_id: asset_id.clone(),
                    media_path: r"C:\TownLight\media\metadata.ts".into(),
                    duration_ms: 10_000,
                    readiness: AssetReadiness::Ready,
                })
                .unwrap();
        }
        let schema_id = "11111111-2222-4333-8444-555555555555";
        let field_id = "22222222-3333-4444-8555-666666666666";
        let record_id = "33333333-4444-4555-8666-777777777777";
        let schema_path = format!("/api/v1/custom-metadata/schemas/{schema_id}");
        let schema_request = serde_json::json!({
            "fields": [{
                "field_id": field_id,
                "key": "title",
                "label": "Title",
                "description": null,
                "required": true,
                "public": true,
                "faceted": true,
                "field_type": {"kind": "text", "constraints": {"min_chars": 1, "max_chars": 80}}
            }],
            "expected_revision": null
        });
        let mut forged_schema = schema_request.clone();
        forged_schema["actor"] = serde_json::Value::String("forged-admin".into());
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &schema_path,
                Some(&serde_json::to_vec(&forged_schema).unwrap()),
                &admin,
            )
            .status,
            400
        );
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &schema_path,
                Some(&serde_json::to_vec(&schema_request).unwrap()),
                &operator,
            )
            .status,
            403
        );
        let mut invalid_schema = schema_request.clone();
        invalid_schema["fields"][0]["key"] = serde_json::Value::String("Invalid-Key".into());
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &schema_path,
                Some(&serde_json::to_vec(&invalid_schema).unwrap()),
                &admin,
            )
            .status,
            422
        );
        let before_write = system_time_millis().unwrap();
        let written_schema = api.handle_authenticated(
            "PUT",
            &schema_path,
            Some(&serde_json::to_vec(&schema_request).unwrap()),
            &admin,
        );
        assert_eq!(written_schema.status, 201);
        let written_schema: serde_json::Value =
            serde_json::from_slice(&written_schema.body).unwrap();
        assert_eq!(written_schema["schema_id"], schema_id);
        assert_eq!(written_schema["revision"], 1);
        let schema_history = api.handle_authenticated(
            "GET",
            &format!("{schema_path}/history?limit=1"),
            None,
            &operator,
        );
        assert_eq!(schema_history.status, 200);
        let schema_history: serde_json::Value =
            serde_json::from_slice(&schema_history.body).unwrap();
        assert_eq!(schema_history[0]["changed_by"], admin.account_id().as_str());
        assert!(schema_history[0]["occurred_at_unix_ms"].as_i64().unwrap() >= before_write);
        let stale_schema = serde_json::json!({
            "fields": schema_request["fields"].clone(),
            "expected_revision": 0
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &schema_path,
                Some(&serde_json::to_vec(&stale_schema).unwrap()),
                &admin,
            )
            .status,
            409
        );

        let record_path = format!("/api/v1/custom-metadata/records/{record_id}");
        let record_request = serde_json::json!({
            "asset_id": asset_id,
            "schema_id": schema_id,
            "values": [{
                "field_id": field_id,
                "value": {"kind": "text", "value": "Council Meeting"}
            }],
            "expected_revision": null
        });
        let mut forged_record = record_request.clone();
        forged_record["schema_revision"] = serde_json::Value::from(999_u64);
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &record_path,
                Some(&serde_json::to_vec(&forged_record).unwrap()),
                &operator,
            )
            .status,
            400
        );
        let mut forged_attribution = record_request.clone();
        forged_attribution["actor"] = serde_json::Value::String("browser-forgery".into());
        forged_attribution["occurred_at_unix_ms"] = serde_json::Value::from(1_i64);
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &record_path,
                Some(&serde_json::to_vec(&forged_attribution).unwrap()),
                &operator,
            )
            .status,
            400
        );
        let mut invalid_schema_id = record_request.clone();
        invalid_schema_id["schema_id"] = serde_json::Value::String("not-a-schema-id".into());
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &record_path,
                Some(&serde_json::to_vec(&invalid_schema_id).unwrap()),
                &operator,
            )
            .status,
            422
        );
        let mut unknown_field = record_request.clone();
        unknown_field["values"][0]["field_id"] =
            serde_json::Value::String("99999999-8888-4777-8666-555555555555".into());
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &record_path,
                Some(&serde_json::to_vec(&unknown_field).unwrap()),
                &operator,
            )
            .status,
            422
        );

        let api_resolved_schema = api
            .storage
            .read_custom_metadata_schema(schema_id)
            .unwrap()
            .unwrap();
        let advance_schema = serde_json::json!({
            "fields": schema_request["fields"].clone(),
            "expected_revision": 1
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &schema_path,
                Some(&serde_json::to_vec(&advance_schema).unwrap()),
                &admin,
            )
            .status,
            200
        );
        let stale_interleaved_request: PutAssetMetadataRecordRequest =
            serde_json::from_value(record_request.clone()).unwrap();
        let stale_interleaved = api.write_resolved_asset_metadata_record(
            record_id,
            stale_interleaved_request,
            &operator,
            api_resolved_schema,
        );
        assert_eq!(stale_interleaved.status, 409);
        let stale_interleaved: serde_json::Value =
            serde_json::from_slice(&stale_interleaved.body).unwrap();
        assert_eq!(
            stale_interleaved["error"]["code"],
            "schema_revision_conflict"
        );

        let before_record_write = system_time_millis().unwrap();
        let created = api.handle_authenticated(
            "PUT",
            &record_path,
            Some(&serde_json::to_vec(&record_request).unwrap()),
            &operator,
        );
        let after_record_write = system_time_millis().unwrap();
        assert_eq!(
            created.status,
            201,
            "{}",
            String::from_utf8_lossy(&created.body)
        );
        let created: serde_json::Value = serde_json::from_slice(&created.body).unwrap();
        assert_eq!(created["metadata_id"], record_id);
        assert_eq!(created["schema_revision"], 2);
        assert_eq!(created["revision"], 1);
        let duplicate = api.handle_authenticated(
            "PUT",
            "/api/v1/custom-metadata/records/77777777-8888-4999-8aaa-bbbbbbbbbbbb",
            Some(&serde_json::to_vec(&record_request).unwrap()),
            &admin,
        );
        assert_eq!(duplicate.status, 409);
        let duplicate: serde_json::Value = serde_json::from_slice(&duplicate.body).unwrap();
        assert_eq!(duplicate["error"]["code"], "metadata_binding_conflict");
        assert_eq!(api.handle_in_process("GET", &record_path, None).status, 403);
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/custom-metadata/assets/{asset_id}/records"),
                None,
                &operator,
            )
            .status,
            200
        );
        let record_history = api.handle_authenticated(
            "GET",
            &format!("{record_path}/history?limit=1"),
            None,
            &admin,
        );
        assert_eq!(record_history.status, 200);
        let record_history: serde_json::Value =
            serde_json::from_slice(&record_history.body).unwrap();
        assert_eq!(
            record_history[0]["changed_by"],
            operator.account_id().as_str()
        );
        let record_occurred_at = record_history[0]["occurred_at_unix_ms"].as_i64().unwrap();
        assert!(record_occurred_at >= before_record_write);
        assert!(record_occurred_at <= after_record_write);
        let stale_record = serde_json::json!({
            "asset_id": asset_id,
            "schema_id": schema_id,
            "values": record_request["values"].clone(),
            "expected_revision": 0
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &record_path,
                Some(&serde_json::to_vec(&stale_record).unwrap()),
                &admin,
            )
            .status,
            409
        );
        let rebound = serde_json::json!({
            "asset_id": replacement_asset_id,
            "schema_id": schema_id,
            "values": record_request["values"].clone(),
            "expected_revision": 1
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &record_path,
                Some(&serde_json::to_vec(&rebound).unwrap()),
                &admin,
            )
            .status,
            409
        );
        let missing_asset = serde_json::json!({
            "asset_id": "c".repeat(64),
            "schema_id": schema_id,
            "values": record_request["values"].clone(),
            "expected_revision": null
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                "/api/v1/custom-metadata/records/44444444-5555-4666-8777-888888888888",
                Some(&serde_json::to_vec(&missing_asset).unwrap()),
                &admin,
            )
            .status,
            404
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                "/api/v1/custom-metadata/schemas/55555555-6666-4777-8888-999999999999/history?limit=1",
                None,
                &admin,
            )
            .status,
            404
        );

        drop(api);
        let _ = std::fs::remove_file(database);
        let _ = std::fs::remove_dir_all(application);
    }

    #[test]
    fn custom_metadata_schema_creation_conflict_and_capacity_are_exact_client_errors() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-metadata-schema-errors-{nonce}.db"));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let field_id = "22222222-3333-4444-8555-666666666666";
        let fields = serde_json::json!([{
            "field_id": field_id,
            "key": "title",
            "label": "Title",
            "description": null,
            "required": true,
            "public": true,
            "faceted": true,
            "field_type": {"kind": "text", "constraints": {"min_chars": 1, "max_chars": 80}}
        }]);

        let conflict_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
        let conflict = api.handle_authenticated(
            "PUT",
            &format!("/api/v1/custom-metadata/schemas/{conflict_id}"),
            Some(
                &serde_json::to_vec(&serde_json::json!({
                    "fields": fields.clone(),
                    "expected_revision": 0
                }))
                .unwrap(),
            ),
            &admin,
        );
        assert_eq!(conflict.status, 409);
        let conflict: serde_json::Value = serde_json::from_slice(&conflict.body).unwrap();
        assert_eq!(conflict["error"]["code"], "revision_conflict");
        assert!(
            api.storage
                .read_custom_metadata_schema(conflict_id)
                .unwrap()
                .is_none()
        );

        let fields: Vec<MetadataFieldDefinition> = serde_json::from_value(fields).unwrap();
        for index in 1_u64..=64 {
            let schema_id = format!("{index:08x}-aaaa-4bbb-8ccc-{index:012x}");
            api.storage
                .write_custom_metadata_schema(
                    &MetadataSchema {
                        schema_id,
                        revision: 1,
                        fields: fields.clone(),
                    },
                    None,
                    admin.account_id().as_str(),
                    i64::try_from(index).unwrap(),
                )
                .unwrap();
        }
        let capacity_id = "00000041-aaaa-4bbb-8ccc-000000000041";
        let capacity = api.handle_authenticated(
            "PUT",
            &format!("/api/v1/custom-metadata/schemas/{capacity_id}"),
            Some(
                &serde_json::to_vec(&serde_json::json!({
                    "fields": fields,
                    "expected_revision": null
                }))
                .unwrap(),
            ),
            &admin,
        );
        assert_eq!(capacity.status, 422);
        let capacity: serde_json::Value = serde_json::from_slice(&capacity.body).unwrap();
        assert_eq!(
            capacity["error"]["code"],
            "metadata_schema_capacity_exceeded"
        );
        assert!(
            api.storage
                .read_custom_metadata_schema(capacity_id)
                .unwrap()
                .is_none()
        );

        drop(api);
        let _ = std::fs::remove_file(database);
        let _ = std::fs::remove_dir_all(application);
    }

    #[test]
    fn catalog_api_derives_authority_and_preserves_editorial_lifecycle() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-catalog-api-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let operator = test_actor_with_account(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "catalog.operator",
            station_auth::OperatorRole::MeetingOperator,
        );
        let admin = test_actor_with_account(
            "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            "catalog.admin",
            station_auth::OperatorRole::SetupAdmin,
        );
        let support = test_actor_with_account(
            "cccccccc-dddd-4eee-8fff-000000000000",
            "catalog.support",
            station_auth::OperatorRole::SupportAdmin,
        );
        let first_asset = "a".repeat(64);
        let second_asset = "b".repeat(64);
        put_catalog_test_media(&api, &first_asset, AssetReadiness::Processing);
        put_catalog_test_media(&api, &second_asset, AssetReadiness::Ready);
        let first_path = format!("/api/v1/catalog/assets/{first_asset}");
        let second_path = format!("/api/v1/catalog/assets/{second_asset}");

        let create = serde_json::json!({
            "title": "City Council",
            "meeting_body": "City Council",
            "expected_revision": null
        });
        let created = api.handle_authenticated(
            "PUT",
            &first_path,
            Some(&serde_json::to_vec(&create).unwrap()),
            &operator,
        );
        assert_eq!(
            created.status,
            201,
            "{}",
            String::from_utf8_lossy(&created.body)
        );
        let created: CatalogAssetRecord = serde_json::from_slice(&created.body).unwrap();
        assert_eq!(created.asset_sha256, first_asset);
        assert_eq!(created.revision, 1);
        assert_eq!(created.editorial_state, CatalogEditorialState::Draft);
        assert_eq!(created.published_at_utc_ms, None);

        let before_publish = system_time_millis().unwrap();
        let published = api.handle_authenticated(
            "POST",
            &format!("{first_path}/publish"),
            Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 1})).unwrap()),
            &admin,
        );
        assert_eq!(published.status, 200);
        let published: CatalogAssetRecord = serde_json::from_slice(&published.body).unwrap();
        let first_publication = published.published_at_utc_ms.unwrap();
        assert!(first_publication >= before_publish);
        assert_eq!(published.revision, 2);
        assert_eq!(published.editorial_state, CatalogEditorialState::Published);

        let edited = api.handle_authenticated(
            "PUT",
            &first_path,
            Some(
                &serde_json::to_vec(&serde_json::json!({
                    "title": "City Council regular meeting",
                    "meeting_body": "City Council",
                    "expected_revision": 2
                }))
                .unwrap(),
            ),
            &operator,
        );
        assert_eq!(edited.status, 200);
        let edited: CatalogAssetRecord = serde_json::from_slice(&edited.body).unwrap();
        assert_eq!(edited.revision, 3);
        assert_eq!(edited.editorial_state, CatalogEditorialState::Published);
        assert_eq!(edited.published_at_utc_ms, Some(first_publication));

        let withdrawn = api.handle_authenticated(
            "POST",
            &format!("{first_path}/withdraw"),
            Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 3})).unwrap()),
            &operator,
        );
        assert_eq!(withdrawn.status, 200);
        let withdrawn: CatalogAssetRecord = serde_json::from_slice(&withdrawn.body).unwrap();
        assert_eq!(withdrawn.editorial_state, CatalogEditorialState::Withdrawn);
        assert_eq!(withdrawn.published_at_utc_ms, Some(first_publication));

        let republished = api.handle_authenticated(
            "POST",
            &format!("{first_path}/publish"),
            Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 4})).unwrap()),
            &admin,
        );
        assert_eq!(republished.status, 200);
        let republished: CatalogAssetRecord = serde_json::from_slice(&republished.body).unwrap();
        assert_eq!(republished.revision, 5);
        assert_eq!(
            republished.editorial_state,
            CatalogEditorialState::Published
        );
        assert_eq!(republished.published_at_utc_ms, Some(first_publication));

        let history = api.handle_authenticated(
            "GET",
            &format!("{first_path}/history?limit=10"),
            None,
            &support,
        );
        assert_eq!(history.status, 200);
        let history: serde_json::Value = serde_json::from_slice(&history.body).unwrap();
        assert_eq!(history.as_array().unwrap().len(), 5);
        assert_eq!(history[0]["record"]["revision"], 5);
        let publish_history = history
            .as_array()
            .unwrap()
            .iter()
            .find(|entry| entry["record"]["revision"] == 2)
            .unwrap();
        assert_eq!(
            publish_history["occurred_at_utc_ms"],
            publish_history["record"]["published_at_utc_ms"]
        );
        assert_eq!(publish_history["changed_by"], admin.account_id().as_str());

        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &second_path,
                Some(
                    &serde_json::to_vec(&serde_json::json!({
                        "title": "School Board",
                        "meeting_body": "School Board",
                        "expected_revision": null
                    }))
                    .unwrap()
                ),
                &admin,
            )
            .status,
            201
        );
        let first_page =
            api.handle_authenticated("GET", "/api/v1/catalog/assets?limit=1", None, &support);
        assert_eq!(first_page.status, 200);
        let first_page: serde_json::Value = serde_json::from_slice(&first_page.body).unwrap();
        assert_eq!(first_page["records"][0]["asset_sha256"], first_asset);
        assert_eq!(first_page["next_after_asset_sha256"], first_asset);
        let next_page = api.handle_authenticated(
            "GET",
            &format!("/api/v1/catalog/assets?after_asset_sha256={first_asset}&limit=1"),
            None,
            &support,
        );
        assert_eq!(next_page.status, 200);
        let next_page: serde_json::Value = serde_json::from_slice(&next_page.body).unwrap();
        assert_eq!(next_page["records"][0]["asset_sha256"], second_asset);
        assert!(next_page["next_after_asset_sha256"].is_null());

        assert_eq!(
            api.handle_authenticated("GET", &first_path, None, &support)
                .status,
            200
        );
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &first_path,
                Some(&serde_json::to_vec(&create).unwrap()),
                &support,
            )
            .status,
            403
        );
        assert_eq!(api.handle_in_process("GET", &first_path, None).status, 403);

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn catalog_api_rejects_forged_authority_stale_writes_and_invalid_routes() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-catalog-api-errors-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let support = test_actor(station_auth::OperatorRole::SupportAdmin);
        let records = test_actor(station_auth::OperatorRole::RecordsClerk);
        let asset = "c".repeat(64);
        let missing_asset = "d".repeat(64);
        let path = format!("/api/v1/catalog/assets/{asset}");
        let base = serde_json::json!({
            "title": "Planning Commission",
            "meeting_body": "Planning Commission",
            "expected_revision": null
        });

        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &format!("/api/v1/catalog/assets/{missing_asset}"),
                Some(&serde_json::to_vec(&base).unwrap()),
                &operator,
            )
            .status,
            404
        );
        put_catalog_test_media(&api, &asset, AssetReadiness::Missing);
        assert_eq!(
            api.handle_authenticated("GET", &path, None, &records)
                .status,
            403
        );

        for forged_field in [
            "asset_sha256",
            "revision",
            "editorial_state",
            "published_at_utc_ms",
            "changed_by",
            "occurred_at_utc_ms",
        ] {
            let mut forged = base.clone();
            forged[forged_field] = serde_json::Value::String("forged".into());
            assert_eq!(
                api.handle_authenticated(
                    "PUT",
                    &path,
                    Some(&serde_json::to_vec(&forged).unwrap()),
                    &operator,
                )
                .status,
                400,
                "browser authority field {forged_field} must be rejected"
            );
        }
        let invalid = serde_json::json!({
            "title": "",
            "meeting_body": null,
            "expected_revision": null
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &path,
                Some(&serde_json::to_vec(&invalid).unwrap()),
                &operator,
            )
            .status,
            422
        );
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &path,
                Some(&serde_json::to_vec(&base).unwrap()),
                &operator,
            )
            .status,
            201
        );

        let stale = serde_json::json!({
            "title": "Planning Commission update",
            "meeting_body": "Planning Commission",
            "expected_revision": 0
        });
        let stale = api.handle_authenticated(
            "PUT",
            &path,
            Some(&serde_json::to_vec(&stale).unwrap()),
            &operator,
        );
        assert_eq!(stale.status, 409);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&stale.body).unwrap()["error"]["code"],
            "revision_conflict"
        );
        let overflow_stale = api.handle_authenticated(
            "PUT",
            &path,
            Some(
                &serde_json::to_vec(&serde_json::json!({
                    "title": "Planning Commission update",
                    "meeting_body": "Planning Commission",
                    "expected_revision": u64::MAX
                }))
                .unwrap(),
            ),
            &operator,
        );
        assert_eq!(overflow_stale.status, 409);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&overflow_stale.body).unwrap()["error"]["code"],
            "revision_conflict"
        );
        let withdraw_draft = api.handle_authenticated(
            "POST",
            &format!("{path}/withdraw"),
            Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 1})).unwrap()),
            &operator,
        );
        assert_eq!(withdraw_draft.status, 409);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&withdraw_draft.body).unwrap()["error"]["code"],
            "catalog_state_conflict"
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("{path}/publish"),
                Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 1})).unwrap()),
                &operator,
            )
            .status,
            200
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("{path}/publish"),
                Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 2})).unwrap()),
                &operator,
            )
            .status,
            409
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("{path}/withdraw"),
                Some(
                    &serde_json::to_vec(&serde_json::json!({
                        "expected_revision": 2,
                        "actor": "forged"
                    }))
                    .unwrap()
                ),
                &operator,
            )
            .status,
            400
        );
        assert_eq!(
            api.handle_authenticated("POST", &format!("{path}/withdraw"), None, &operator)
                .status,
            400
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("{path}/withdraw"),
                Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 2})).unwrap()),
                &support,
            )
            .status,
            403
        );

        for invalid_path in [
            format!("/api/v1/catalog/assets/{}", asset.to_uppercase()),
            format!("/api/v1/catalog/assets/{asset}/"),
            "/api/v1/catalog/assets?limit=0".into(),
            "/api/v1/catalog/assets?limit=1001".into(),
            "/api/v1/catalog/assets?after_asset_sha256=bad&limit=1".into(),
            format!("/api/v1/catalog/assets?limit=1&after_asset_sha256={asset}"),
            format!("{path}/history?limit=0"),
            format!("{path}/history?limit=1001"),
        ] {
            assert_eq!(
                api.handle_authenticated("GET", &invalid_path, None, &operator)
                    .status,
                400,
                "invalid catalog path must fail: {invalid_path}"
            );
        }
        assert_eq!(
            api.handle_authenticated("GET", &format!("{path}%2fextra"), None, &operator)
                .status,
            400
        );
        assert_eq!(
            api.handle_authenticated("DELETE", &path, None, &operator)
                .status,
            405
        );
        assert_eq!(
            api.handle_authenticated("PATCH", &path, Some(b"{}"), &operator)
                .status,
            405
        );
        assert_eq!(
            api.handle_authenticated("POST", &path, Some(b"{}"), &operator)
                .status,
            405
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/catalog/assets/{}/history?limit=1", "e".repeat(64)),
                None,
                &operator,
            )
            .status,
            404
        );

        let orphan_asset = "f".repeat(64);
        assert_eq!(
            api.storage.write_catalog_asset(
                &orphan_asset,
                &CatalogAssetRecord {
                    asset_sha256: orphan_asset.clone(),
                    title: "Orphan authority".into(),
                    meeting_body: None,
                    editorial_state: CatalogEditorialState::Draft,
                    published_at_utc_ms: None,
                    revision: 1,
                },
                None,
                "test-harness",
                1,
            ),
            Err(CatalogAssetWriteError::Invalid(
                "catalog asset media dependency does not exist".into()
            )),
            "storage must make orphan catalog authority impossible"
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("/api/v1/catalog/assets/{orphan_asset}/publish"),
                Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 1})).unwrap()),
                &operator,
            )
            .status,
            404,
            "catalog transitions must reject identities without media authority"
        );

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn catalog_write_errors_have_exact_client_boundaries() {
        for (error, status, code) in [
            (
                CatalogAssetWriteError::Invalid("invalid catalog".into()),
                422,
                "validation_failed",
            ),
            (
                CatalogAssetWriteError::Conflict {
                    current_revision: Some(2),
                },
                409,
                "revision_conflict",
            ),
            (
                CatalogAssetWriteError::Rebound,
                409,
                "catalog_identity_rebound",
            ),
            (
                CatalogAssetWriteError::Capacity,
                422,
                "catalog_capacity_exceeded",
            ),
            (
                CatalogAssetWriteError::Storage("corrupt catalog".into()),
                500,
                "storage_error",
            ),
        ] {
            let response = catalog_write_error_response(error);
            assert_eq!(response.status, status);
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&response.body).unwrap()["error"]["code"],
                code
            );
        }
    }

    #[test]
    fn scheduling_automation_api_derives_authority_and_exposes_current_and_history() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-scheduling-api-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let support = test_actor_with_account(
            "cccccccc-dddd-4eee-8fff-000000000000",
            "schedule.support",
            station_auth::OperatorRole::SupportAdmin,
        );
        let channel_id = "11111111-2222-4333-8444-555555555555";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Government".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "22222222-3333-4444-8555-666666666666",
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();

        let search_id = "33333333-4444-4555-8666-777777777777";
        let block_id = "44444444-5555-4666-8777-888888888888";
        let rule_id = "55555555-6666-4777-8888-999999999999";
        let search_path = format!("/api/v1/scheduling/saved-searches/{search_id}");
        let block_path = format!("/api/v1/scheduling/daypart-blocks/{block_id}");
        let rule_path = format!("/api/v1/scheduling/recurring-rules/{rule_id}");
        let query = station_schedule_automation::SavedSearchQuery {
            all_of: vec![
                station_schedule_automation::AssetPredicate::MeetingBodyEquals(
                    "City Council".into(),
                ),
                station_schedule_automation::AssetPredicate::DurationMs {
                    min_inclusive: Some(60_000),
                    max_inclusive: Some(7_200_000),
                },
                station_schedule_automation::AssetPredicate::PublishedAtUtcMs {
                    starts_at_inclusive: Some(1),
                    ends_before_exclusive: Some(2_000_000_000_000),
                },
                station_schedule_automation::AssetPredicate::Custom {
                    field: "program_type".into(),
                    predicate: station_schedule_automation::CustomMetadataPredicate::Equals(
                        station_schedule_automation::MetadataValue::Choice("meeting".into()),
                    ),
                },
            ],
            any_of: Vec::new(),
            order_by: station_schedule_automation::SavedSearchOrder::PublishedNewest,
            limit: 20,
        };
        let search_request = serde_json::json!({
            "name": "Council meetings",
            "query": query,
            "expected_revision": null
        });
        let before_write = system_time_millis().unwrap();
        let created_search = api.handle_authenticated(
            "PUT",
            &search_path,
            Some(&serde_json::to_vec(&search_request).unwrap()),
            &operator,
        );
        assert_eq!(
            created_search.status,
            201,
            "{}",
            String::from_utf8_lossy(&created_search.body)
        );
        let created_search: serde_json::Value =
            serde_json::from_slice(&created_search.body).unwrap();
        assert_eq!(
            created_search["owner_operator_id"],
            operator.account_id().as_str()
        );
        assert_eq!(created_search["revision"], 1);
        assert_eq!(created_search["name"], "Council meetings");
        assert!(created_search["created_at_utc_ms"].as_i64().unwrap() >= before_write);

        let block_request = serde_json::json!({
            "channel_id": channel_id,
            "label": "Evening",
            "starts_local_minute": 1080,
            "ends_local_minute": 1320,
            "weekday_mask": 127,
            "season_starts_on": null,
            "season_ends_on": null,
            "allow_rule_ids": [rule_id],
            "expected_revision": null
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &block_path,
                Some(&serde_json::to_vec(&block_request).unwrap()),
                &operator,
            )
            .status,
            201
        );
        let rule_request = serde_json::json!({
            "channel_id": channel_id,
            "saved_search_id": search_id,
            "saved_search_revision": 1,
            "pick_strategy": station_schedule_automation::AssetPickStrategy::Newest,
            "rolling_window_days": 14,
            "avoid_repeat_within_days": 7,
            "enabled": true,
            "priority": 10,
            "block_id": block_id,
            "block_revision": 1,
            "weekday_mask": 127,
            "interval_weeks": 1,
            "anchor_date": {"year": 2026, "month": 8, "day": 24},
            "timezone": station_schedule_automation::RecurrenceTimeZone::Utc,
            "local_start_minute": 1140,
            "duration_minutes": 60,
            "expected_revision": null
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &rule_path,
                Some(&serde_json::to_vec(&rule_request).unwrap()),
                &admin,
            )
            .status,
            201
        );

        let second_channel_id = "66666666-7777-4888-8999-aaaaaaaaaaaa";
        let second_block_id = "77777777-8888-4999-8aaa-bbbbbbbbbbbb";
        let second_rule_id = "88888888-9999-4aaa-8bbb-cccccccccccc";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: second_channel_id.into(),
                display_name: "Education".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "99999999-aaaa-4bbb-8ccc-dddddddddddd",
                    "Education proof output",
                    "127.0.0.1:5501",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
        let mut second_block_request = block_request.clone();
        second_block_request["channel_id"] = serde_json::Value::String(second_channel_id.into());
        second_block_request["allow_rule_ids"] = serde_json::json!([second_rule_id]);
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &format!("/api/v1/scheduling/daypart-blocks/{second_block_id}"),
                Some(&serde_json::to_vec(&second_block_request).unwrap()),
                &operator,
            )
            .status,
            201
        );
        let mut second_rule_request = rule_request.clone();
        second_rule_request["channel_id"] = serde_json::Value::String(second_channel_id.into());
        second_rule_request["block_id"] = serde_json::Value::String(second_block_id.into());
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &format!("/api/v1/scheduling/recurring-rules/{second_rule_id}"),
                Some(&serde_json::to_vec(&second_rule_request).unwrap()),
                &operator,
            )
            .status,
            201,
            "a station-wide saved search must be reusable by another channel"
        );
        let mut stale_dependency_request = rule_request.clone();
        stale_dependency_request["saved_search_revision"] = serde_json::Value::from(999_u64);
        let stale_dependency = api.handle_authenticated(
            "PUT",
            "/api/v1/scheduling/recurring-rules/99999999-aaaa-4bbb-8ccc-dddddddddddd",
            Some(&serde_json::to_vec(&stale_dependency_request).unwrap()),
            &operator,
        );
        assert_eq!(stale_dependency.status, 409);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&stale_dependency.body).unwrap()["error"]["code"],
            "authority_revision_conflict"
        );
        let mut missing_dependency_request = rule_request.clone();
        missing_dependency_request["saved_search_id"] =
            serde_json::Value::String("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into());
        let missing_dependency = api.handle_authenticated(
            "PUT",
            "/api/v1/scheduling/recurring-rules/aaaaaaaa-cccc-4ddd-8eee-ffffffffffff",
            Some(&serde_json::to_vec(&missing_dependency_request).unwrap()),
            &operator,
        );
        assert_eq!(missing_dependency.status, 404);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&missing_dependency.body).unwrap()["error"]
                ["code"],
            "saved_search_not_found"
        );

        for (path, identity_field, identity) in [
            (&search_path, "search_id", search_id),
            (&block_path, "block_id", block_id),
            (&rule_path, "rule_id", rule_id),
        ] {
            let current = api.handle_authenticated("GET", path, None, &operator);
            assert_eq!(current.status, 200);
            let current: serde_json::Value = serde_json::from_slice(&current.body).unwrap();
            assert_eq!(current[identity_field], identity);
            let history = api.handle_authenticated(
                "GET",
                &format!("{path}/history?limit=1"),
                None,
                &operator,
            );
            assert_eq!(history.status, 200);
            let history: serde_json::Value = serde_json::from_slice(&history.body).unwrap();
            assert!(history[0]["occurred_at_unix_ms"].as_i64().unwrap() >= before_write);
        }
        let searches = api.handle_authenticated(
            "GET",
            "/api/v1/scheduling/saved-searches?limit=8",
            None,
            &support,
        );
        assert_eq!(searches.status, 200);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&searches.body)
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            1
        );
        for collection in ["daypart-blocks", "recurring-rules"] {
            let listed = api.handle_authenticated(
                "GET",
                &format!("/api/v1/scheduling/{collection}?channel_id={channel_id}&limit=8"),
                None,
                &operator,
            );
            assert_eq!(listed.status, 200);
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&listed.body)
                    .unwrap()
                    .as_array()
                    .unwrap()
                    .len(),
                1
            );
        }
        assert_eq!(api.handle_in_process("GET", &search_path, None).status, 403);
        assert_eq!(
            api.handle_authenticated("GET", &search_path, None, &support)
                .status,
            200
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("{search_path}/history?limit=1"),
                None,
                &support,
            )
            .status,
            200
        );
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &search_path,
                Some(
                    &serde_json::to_vec(&serde_json::json!({
                        "name": "Support edit",
                        "query": search_request["query"].clone(),
                        "expected_revision": 1
                    }))
                    .unwrap()
                ),
                &support,
            )
            .status,
            403
        );
        assert_eq!(
            api.handle_authenticated("DELETE", &rule_path, None, &admin)
                .status,
            405
        );

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn schedule_materialization_post_validates_the_server_owned_channel() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-materialization-api-red-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let response = api.handle_authenticated(
            "POST",
            "/api/v1/scheduling/channels/11111111-2222-4333-8444-555555555555/materializations",
            None,
            &operator,
        );

        assert_eq!(response.status, 404);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&response.body).unwrap()["error"]["code"],
            "channel_not_found"
        );

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn schedule_materialization_history_requires_a_bounded_authenticated_reader_contract() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-materialization-history-api-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Materialization history proof".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "22222222-3333-4444-8555-666666666666",
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: true,
            })
            .unwrap();
        let support = test_actor(station_auth::OperatorRole::SupportAdmin);
        let meeting = test_actor(station_auth::OperatorRole::MeetingOperator);
        let setup = test_actor(station_auth::OperatorRole::SetupAdmin);
        let records = test_actor(station_auth::OperatorRole::RecordsClerk);
        let disabled = disabled_test_actor(station_auth::OperatorRole::SetupAdmin);
        let path =
            format!("/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1");

        assert_eq!(api.handle_in_process("GET", &path, None).status, 403);
        for rejected in [&records, &disabled] {
            assert_eq!(
                api.handle_authenticated("GET", &path, None, rejected)
                    .status,
                403
            );
        }
        for reader in [&support, &meeting, &setup] {
            let response = api.handle_authenticated("GET", &path, None, reader);
            assert_eq!(
                response.status,
                200,
                "{}",
                String::from_utf8_lossy(&response.body)
            );
            let json: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
            assert_eq!(json["entries"], serde_json::json!([]));
            assert!(json["next_cursor"].is_null());
        }

        assert_eq!(
            api.handle_authenticated("GET", &path, Some(br#"{}"#), &support)
                .status,
            400
        );
        for method in ["POST", "PUT", "DELETE"] {
            let response = api.handle_authenticated(method, &path, None, &support);
            assert_eq!(response.status, 405, "{method}");
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&response.body).unwrap()["error"]["code"],
                "method_not_allowed"
            );
        }
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!(
                    "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=0"
                ),
                None,
                &support,
            )
            .status,
            400,
            "invalid history grammar must not become a recognized endpoint"
        );
        for malformed in [
            format!("/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=0"),
            format!("/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=129"),
            format!(
                "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1&before_run_id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
            ),
            format!(
                "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1&before_occurred_at_unix_ms=1"
            ),
            format!(
                "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1&before_occurred_at_unix_ms=1&before_run_id=not-a-uuid"
            ),
            format!(
                "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1&before_run_id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee&before_occurred_at_unix_ms=1"
            ),
            format!(
                "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1&limit=1"
            ),
            format!(
                "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1&unexpected=1"
            ),
        ] {
            let response = api.handle_authenticated("GET", &malformed, None, &support);
            assert_eq!(response.status, 400, "{malformed}");
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&response.body).unwrap()["error"]["code"],
                "invalid_path"
            );
        }
        let invalid_cursor = format!(
            "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1&before_occurred_at_unix_ms=-1&before_run_id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        );
        let invalid_cursor = api.handle_authenticated("GET", &invalid_cursor, None, &support);
        assert_eq!(invalid_cursor.status, 422);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&invalid_cursor.body).unwrap()["error"]["code"],
            "invalid_materialization_history_cursor"
        );
        let zero_cursor = format!(
            "/api/v1/scheduling/channels/{channel_id}/materializations/history?limit=1&before_occurred_at_unix_ms=0&before_run_id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        );
        let zero_cursor = api.handle_authenticated("GET", &zero_cursor, None, &support);
        assert_eq!(zero_cursor.status, 422);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&zero_cursor.body).unwrap()["error"]["code"],
            "invalid_materialization_history_cursor"
        );

        let missing = api.handle_authenticated(
            "GET",
            "/api/v1/scheduling/channels/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/materializations/history?limit=1",
            None,
            &support,
        );
        assert_eq!(missing.status, 404);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&missing.body).unwrap()["error"]["code"],
            "channel_not_found"
        );

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn schedule_materialization_history_is_channel_scoped_cursor_stable_and_redacted() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-materialization-history-projection-api-{nonce}.db"
        ));
        let api = Api::open(&database).unwrap();
        let operator = test_actor_with_account(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "materialization.history.operator",
            station_auth::OperatorRole::MeetingOperator,
        );
        let reader = test_actor(station_auth::OperatorRole::SupportAdmin);
        let (search, block, rule) = source_evidence_authorities();
        let second_channel_id = "11111111-2222-4333-8444-555555555555";
        let second_block_id = "22222222-3333-4444-8555-666666666666";
        let second_rule_id = "33333333-4444-4555-8666-777777777777";
        let mut second_block = block.clone();
        second_block.block_id = second_block_id.into();
        second_block.channel_id = second_channel_id.into();
        second_block.allow_rule_ids = vec![second_rule_id.into()];
        let mut second_rule = rule.clone();
        second_rule.rule_id = second_rule_id.into();
        second_rule.channel_id = second_channel_id.into();
        second_rule.block_id = second_block_id.into();

        for (channel_id, display_name, sink_id, destination) in [
            (
                rule.channel_id.as_str(),
                "Operator history",
                "22222222-3333-4444-8555-666666666666",
                "127.0.0.1:5500",
            ),
            (
                second_channel_id,
                "Automatic history",
                "33333333-4444-4555-8666-777777777777",
                "127.0.0.1:5501",
            ),
        ] {
            api.storage
                .put_channel(&ChannelConfiguration {
                    channel_id: channel_id.into(),
                    display_name: display_name.into(),
                    output_profile: station_schedule::OutputProfileId::Proof360p30,
                    output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                        sink_id,
                        "History proof output",
                        destination,
                        -16.0,
                    )],
                    enabled: true,
                })
                .unwrap();
        }
        seed_source_evidence_authorities(&api, &search, &block, &rule);
        api.storage
            .write_daypart_block(&second_block, None, &search.owner_operator_id, 4)
            .unwrap();
        api.storage
            .write_recurring_rule(&second_rule, None, &search.owner_operator_id, 5)
            .unwrap();

        let first_asset = "a".repeat(64);
        let first_catalog_path = format!("/api/v1/catalog/assets/{first_asset}");
        put_catalog_test_media(&api, &first_asset, AssetReadiness::Ready);
        let first_create = api.handle_authenticated(
            "PUT",
            &first_catalog_path,
            Some(
                &serde_json::to_vec(&serde_json::json!({
                    "title": "City Council regular meeting",
                    "meeting_body": "Council",
                    "expected_revision": null
                }))
                .unwrap(),
            ),
            &operator,
        );
        assert_eq!(first_create.status, 201);
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("{first_catalog_path}/publish"),
                Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 1})).unwrap()),
                &operator,
            )
            .status,
            200
        );

        let operator_materialization_path = format!(
            "/api/v1/scheduling/channels/{}/materializations",
            rule.channel_id
        );
        let first_operator_run =
            api.handle_authenticated("POST", &operator_materialization_path, Some(&[]), &operator);
        assert_eq!(
            first_operator_run.status,
            201,
            "{}",
            String::from_utf8_lossy(&first_operator_run.body)
        );
        let first_operator_run: serde_json::Value =
            serde_json::from_slice(&first_operator_run.body).unwrap();

        let second_asset = "b".repeat(64);
        let second_catalog_path = format!("/api/v1/catalog/assets/{second_asset}");
        put_catalog_test_media(&api, &second_asset, AssetReadiness::Ready);
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &second_catalog_path,
                Some(
                    &serde_json::to_vec(&serde_json::json!({
                        "title": "City Council special meeting",
                        "meeting_body": "Council",
                        "expected_revision": null
                    }))
                    .unwrap(),
                ),
                &operator,
            )
            .status,
            201
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("{second_catalog_path}/publish"),
                Some(&serde_json::to_vec(&serde_json::json!({"expected_revision": 1})).unwrap()),
                &operator,
            )
            .status,
            200
        );
        let second_operator_run =
            api.handle_authenticated("POST", &operator_materialization_path, None, &operator);
        assert_eq!(
            second_operator_run.status,
            201,
            "{}",
            String::from_utf8_lossy(&second_operator_run.body)
        );
        let second_operator_run: serde_json::Value =
            serde_json::from_slice(&second_operator_run.body).unwrap();
        assert_ne!(first_operator_run["run_id"], second_operator_run["run_id"]);

        let automatic = api
            .storage
            .materialize_scheduled_channel(
                second_channel_id,
                station_domain::AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID,
                api.source_evidence_authority.as_ref(),
            )
            .unwrap();
        let automatic_run_id = match automatic {
            ScheduleMaterializationApplyOutcome::Applied(run) => run.run_id,
            ScheduleMaterializationApplyOutcome::AlreadyApplied(run) => run.run_id,
        };

        let history_path = format!(
            "/api/v1/scheduling/channels/{}/materializations/history?limit=1",
            rule.channel_id
        );
        let first_page = api.handle_authenticated("GET", &history_path, None, &reader);
        assert_eq!(
            first_page.status,
            200,
            "{}",
            String::from_utf8_lossy(&first_page.body)
        );
        let first_page: serde_json::Value = serde_json::from_slice(&first_page.body).unwrap();
        assert_eq!(first_page["entries"].as_array().unwrap().len(), 1);
        let first_entry = &first_page["entries"][0];
        assert_eq!(first_entry["channel_id"], rule.channel_id);
        assert_eq!(first_entry["origin"], "operator");
        assert_eq!(first_entry["actor_id"], operator.account_id().as_str());
        assert_eq!(first_entry["request_sha256"].as_str().unwrap().len(), 64);
        assert_eq!(first_entry["result_sha256"].as_str().unwrap().len(), 64);
        assert_eq!(
            first_entry["checkpoint_utc_day"],
            first_entry["occurred_at_unix_ms"].as_i64().unwrap() / 86_400_000
        );
        assert!(first_entry["plans"].as_array().unwrap().iter().all(|plan| {
            plan["channel_id"] == rule.channel_id
                && plan.get("candidate_receipt_sha256").is_none()
                && plan.get("prior_receipt_sha256").is_none()
                && plan.get("source_authority_sha256").is_none()
        }));
        assert!(
            first_entry["outcomes"]
                .as_array()
                .unwrap()
                .iter()
                .all(|outcome| {
                    outcome["channel_id"] == rule.channel_id
                        && outcome.get("source_authority_sha256").is_none()
                        && outcome.get("candidate_projection_sha256").is_none()
                        && outcome.get("prior_airings_sha256").is_none()
                })
        );
        assert!(first_entry.get("batch_sha256").is_none());
        assert!(first_entry.get("alert_binding").is_none());
        assert!(first_entry.get("retry_state").is_none());
        assert!(first_entry.get("apply_disposition").is_none());
        assert!(first_entry.get("source_commit").is_none());
        let cursor = first_page["next_cursor"].as_object().unwrap();
        let cursor_path = format!(
            "{history_path}&before_occurred_at_unix_ms={}&before_run_id={}",
            cursor["occurred_at_unix_ms"].as_i64().unwrap(),
            cursor["run_id"].as_str().unwrap()
        );
        let second_page = api.handle_authenticated("GET", &cursor_path, None, &reader);
        assert_eq!(second_page.status, 200);
        let second_page: serde_json::Value = serde_json::from_slice(&second_page.body).unwrap();
        assert_eq!(second_page["entries"].as_array().unwrap().len(), 1);
        assert!(second_page["next_cursor"].is_null());
        let returned_run_ids = [
            first_page["entries"][0]["run_id"].as_str().unwrap(),
            second_page["entries"][0]["run_id"].as_str().unwrap(),
        ]
        .into_iter()
        .collect::<BTreeSet<_>>();
        assert_eq!(returned_run_ids.len(), 2);
        assert!(returned_run_ids.contains(first_operator_run["run_id"].as_str().unwrap()));
        assert!(returned_run_ids.contains(second_operator_run["run_id"].as_str().unwrap()));
        let all_entries = first_page["entries"]
            .as_array()
            .unwrap()
            .iter()
            .chain(second_page["entries"].as_array().unwrap());
        assert!(
            all_entries
                .flat_map(|entry| entry["generated_drafts"].as_array().unwrap())
                .any(|draft| {
                    draft["occurrence_id"].is_string()
                        && draft["schedule_item_id"].is_string()
                        && draft["current_schedule"]["channel_id"] == rule.channel_id
                        && draft["current_schedule"]["state"] == "draft"
                })
        );

        let automatic_path = format!(
            "/api/v1/scheduling/channels/{second_channel_id}/materializations/history?limit=1"
        );
        let automatic_page = api.handle_authenticated("GET", &automatic_path, None, &reader);
        assert_eq!(automatic_page.status, 200);
        let automatic_page: serde_json::Value =
            serde_json::from_slice(&automatic_page.body).unwrap();
        assert_eq!(automatic_page["entries"].as_array().unwrap().len(), 1);
        assert_eq!(automatic_page["entries"][0]["run_id"], automatic_run_id);
        assert_eq!(
            automatic_page["entries"][0]["channel_id"],
            second_channel_id
        );
        assert_eq!(automatic_page["entries"][0]["origin"], "automatic");
        assert_eq!(
            automatic_page["entries"][0]["actor_id"],
            station_domain::AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID
        );

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn schedule_materialization_history_read_errors_are_typed_and_redacted() {
        for (error, status, code) in [
            (
                ScheduleMaterializationHistoryReadError::Invalid("private invalid detail".into()),
                422,
                "materialization_history_invalid",
            ),
            (
                ScheduleMaterializationHistoryReadError::Capacity,
                422,
                "materialization_history_capacity_exceeded",
            ),
            (
                ScheduleMaterializationHistoryReadError::Integrity(
                    "private integrity detail".into(),
                ),
                500,
                "materialization_integrity_error",
            ),
            (
                ScheduleMaterializationHistoryReadError::Storage("private storage detail".into()),
                500,
                "storage_error",
            ),
        ] {
            let response = schedule_materialization_history_read_error_response(error);
            assert_eq!(response.status, status);
            let body: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
            assert_eq!(body["error"]["code"], code);
            assert!(!String::from_utf8_lossy(&response.body).contains("private"));
        }
    }

    #[test]
    fn schedule_materialization_post_rejects_browser_authority_and_retries_exactly() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-materialization-api-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let admin = test_actor_with_account(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "materialization.admin",
            station_auth::OperatorRole::SetupAdmin,
        );
        let support = test_actor(station_auth::OperatorRole::SupportAdmin);
        let records = test_actor(station_auth::OperatorRole::RecordsClerk);
        let disabled = disabled_test_actor(station_auth::OperatorRole::SetupAdmin);
        let (search, block, rule) = source_evidence_authorities();
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: rule.channel_id.clone(),
                display_name: "Materialization proof".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "22222222-3333-4444-8555-666666666666",
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: true,
            })
            .unwrap();
        seed_source_evidence_authorities(&api, &search, &block, &rule);
        let path = format!(
            "/api/v1/scheduling/channels/{}/materializations",
            rule.channel_id
        );

        assert_eq!(api.handle_in_process("POST", &path, None).status, 403);
        for rejected in [&support, &records, &disabled] {
            assert_eq!(
                api.handle_authenticated("POST", &path, None, rejected)
                    .status,
                403
            );
        }
        for malformed in [
            format!("{path}/"),
            format!("{path}?window_days=60"),
            format!("{path}/preview"),
            "/api/v1/scheduling/channels/not-a-uuid/materializations".into(),
        ] {
            let response = api.handle_authenticated("POST", &malformed, None, &operator);
            assert_eq!(response.status, 400, "{malformed}");
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&response.body).unwrap()["error"]["code"],
                "invalid_path"
            );
        }
        let wrong_method = api.handle_authenticated("GET", &path, None, &operator);
        assert_eq!(wrong_method.status, 405);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&wrong_method.body).unwrap()["error"]["code"],
            "method_not_allowed"
        );
        assert_eq!(
            api.handle_authenticated("DELETE", &path, None, &operator)
                .status,
            405
        );

        for forged_body in [
            br#"{}"#.as_slice(),
            b"\n".as_slice(),
            br#"{"window":{"starts_at_utc_ms":1,"ends_at_utc_ms":2}}"#.as_slice(),
            br#"{"rule_ids":[],"candidates":[],"prior_airings":[]}"#.as_slice(),
            br#"{"actor":"forged","approved":true,"occurred_at_unix_ms":1}"#.as_slice(),
        ] {
            let response = api.handle_authenticated("POST", &path, Some(forged_body), &operator);
            assert_eq!(response.status, 400);
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&response.body).unwrap()["error"]["code"],
                "unexpected_body"
            );
        }

        let applied = api.handle_authenticated("POST", &path, Some(&[]), &operator);
        assert_eq!(
            applied.status,
            201,
            "{}",
            String::from_utf8_lossy(&applied.body)
        );
        let applied_json: serde_json::Value = serde_json::from_slice(&applied.body).unwrap();
        assert_eq!(applied_json["disposition"], "applied");
        assert_eq!(applied_json["changed_by"], operator.account_id().as_str());
        assert!(applied_json["observed_at_utc_ms"].as_i64().unwrap() > 0);
        assert!(applied_json["occurred_at_unix_ms"].as_i64().unwrap() > 0);
        assert_eq!(applied_json["request_sha256"].as_str().unwrap().len(), 64);
        assert_eq!(applied_json["result_sha256"].as_str().unwrap().len(), 64);
        assert_eq!(applied_json["batch_sha256"], applied_json["result_sha256"]);
        assert_eq!(applied_json["plans"].as_array().unwrap().len(), 1);
        assert_eq!(applied_json["plans"][0]["rule_id"], rule.rule_id);
        assert_eq!(applied_json["plans"][0]["channel_id"], rule.channel_id);
        assert_eq!(
            applied_json["plans"][0]["saved_search_id"],
            search.search_id
        );
        assert_eq!(applied_json["plans"][0]["block_id"], block.block_id);
        assert!(applied_json["plans"][0]["candidate_receipt_sha256"].is_string());
        assert!(applied_json["plans"][0]["prior_receipt_sha256"].is_string());
        let outcomes = applied_json["outcomes"].as_array().unwrap();
        assert!(!outcomes.is_empty());
        assert!(outcomes.iter().all(|outcome| {
            outcome["compiled_outcome"]["kind"] == "no_match"
                && outcome["outcome"]["kind"] == "no_match"
                && outcome["rule_id"] == rule.rule_id
                && outcome["materialization_id"].is_null()
        }));

        let retry = api.handle_authenticated("POST", &path, None, &operator);
        let (expected_applied_json, retry) = if retry.status == 201 {
            // A call straddling the authority-owned minute boundary legitimately
            // derives a new server window. Retry that exact new observation.
            (
                serde_json::from_slice::<serde_json::Value>(&retry.body).unwrap(),
                api.handle_authenticated("POST", &path, None, &operator),
            )
        } else {
            (applied_json.clone(), retry)
        };
        assert_eq!(
            retry.status,
            200,
            "{}",
            String::from_utf8_lossy(&retry.body)
        );
        let retry_json: serde_json::Value = serde_json::from_slice(&retry.body).unwrap();
        assert_eq!(retry_json["disposition"], "already_applied");
        assert_eq!(retry_json["run_id"], expected_applied_json["run_id"]);
        assert_eq!(
            retry_json["result_sha256"],
            expected_applied_json["result_sha256"]
        );

        let admin_retry = api.handle_authenticated("POST", &path, None, &admin);
        assert!(matches!(admin_retry.status, 200 | 201));

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn schedule_materialization_post_maps_stale_and_fail_closed_errors() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-materialization-api-empty-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let channel_id = "11111111-2222-4333-8444-555555555555";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "No automation".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "22222222-3333-4444-8555-666666666666",
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
        let path = format!("/api/v1/scheduling/channels/{channel_id}/materializations");
        let stale = api.handle_authenticated("POST", &path, Some(&[]), &operator);
        assert_eq!(stale.status, 409);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&stale.body).unwrap()["error"]["code"],
            "materialization_authority_conflict"
        );

        for (error, status, code) in [
            (
                ScheduleMaterializationWriteError::StaleAuthority("private stale detail".into()),
                409,
                "materialization_authority_conflict",
            ),
            (
                ScheduleMaterializationWriteError::Invalid("private invalid detail".into()),
                422,
                "materialization_invalid",
            ),
            (
                ScheduleMaterializationWriteError::Capacity,
                422,
                "materialization_capacity_exceeded",
            ),
            (
                ScheduleMaterializationWriteError::Collision("private collision detail".into()),
                500,
                "materialization_integrity_error",
            ),
            (
                ScheduleMaterializationWriteError::Storage("private storage detail".into()),
                500,
                "storage_error",
            ),
        ] {
            let response = schedule_materialization_write_error_response(error);
            assert_eq!(response.status, status);
            let body: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
            assert_eq!(body["error"]["code"], code);
            assert!(!String::from_utf8_lossy(&response.body).contains("private"));
        }

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn scheduling_automation_api_enforces_owner_roles_cas_and_dependencies() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-scheduling-api-errors-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let owner = test_actor_with_account(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "schedule.owner",
            station_auth::OperatorRole::MeetingOperator,
        );
        let other = test_actor_with_account(
            "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            "schedule.other",
            station_auth::OperatorRole::MeetingOperator,
        );
        let records = test_actor(station_auth::OperatorRole::RecordsClerk);
        let admin = test_actor_with_account(
            "cccccccc-dddd-4eee-8fff-000000000000",
            "schedule.admin",
            station_auth::OperatorRole::SetupAdmin,
        );
        let channel_id = "11111111-2222-4333-8444-555555555555";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Government".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "22222222-3333-4444-8555-666666666666",
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
        let search_id = "33333333-4444-4555-8666-777777777777";
        let search_path = format!("/api/v1/scheduling/saved-searches/{search_id}");
        let query = station_schedule_automation::SavedSearchQuery {
            all_of: vec![station_schedule_automation::AssetPredicate::Custom {
                field: "program_type".into(),
                predicate: station_schedule_automation::CustomMetadataPredicate::Equals(
                    station_schedule_automation::MetadataValue::Choice("meeting".into()),
                ),
            }],
            any_of: Vec::new(),
            order_by: station_schedule_automation::SavedSearchOrder::TitleAscending,
            limit: 20,
        };
        let create = serde_json::json!({
            "name": "Meeting search",
            "query": query,
            "expected_revision": null
        });
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &search_path,
                Some(&serde_json::to_vec(&create).unwrap()),
                &records,
            )
            .status,
            403
        );
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &search_path,
                Some(&serde_json::to_vec(&create).unwrap()),
                &owner,
            )
            .status,
            201
        );
        let mut forged = create.clone();
        forged["owner_operator_id"] = serde_json::Value::String(other.account_id().as_str().into());
        forged["created_at_utc_ms"] = serde_json::Value::from(1_i64);
        forged["expected_revision"] = serde_json::Value::from(1_u64);
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                &search_path,
                Some(&serde_json::to_vec(&forged).unwrap()),
                &owner,
            )
            .status,
            400
        );
        let update = serde_json::json!({
            "name": "Meeting search update",
            "query": create["query"].clone(),
            "expected_revision": 1
        });
        let forbidden = api.handle_authenticated(
            "PUT",
            &search_path,
            Some(&serde_json::to_vec(&update).unwrap()),
            &other,
        );
        assert_eq!(forbidden.status, 403);
        let forbidden: serde_json::Value = serde_json::from_slice(&forbidden.body).unwrap();
        assert_eq!(forbidden["error"]["code"], "saved_search_owner_required");
        let stale = serde_json::json!({
            "name": "Meeting search update",
            "query": create["query"].clone(),
            "expected_revision": 0
        });
        let stale = api.handle_authenticated(
            "PUT",
            &search_path,
            Some(&serde_json::to_vec(&stale).unwrap()),
            &owner,
        );
        assert_eq!(stale.status, 409);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&stale.body).unwrap()["error"]["code"],
            "revision_conflict"
        );
        let admin_update = api.handle_authenticated(
            "PUT",
            &search_path,
            Some(&serde_json::to_vec(&update).unwrap()),
            &admin,
        );
        assert_eq!(admin_update.status, 200);
        let admin_update: serde_json::Value = serde_json::from_slice(&admin_update.body).unwrap();
        assert_eq!(
            admin_update["owner_operator_id"],
            owner.account_id().as_str()
        );
        assert_eq!(admin_update["revision"], 2);
        assert_eq!(
            api.handle_authenticated(
                "GET",
                "/api/v1/scheduling/saved-searches?channel_id=bad&limit=999",
                None,
                &owner,
            )
            .status,
            400
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                "/api/v1/scheduling/saved-searches/99999999-8888-4777-8666-555555555555/history?limit=1",
                None,
                &owner,
            )
            .status,
            404
        );

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn scheduling_automation_api_rejects_unknown_and_malformed_nested_query_members() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-scheduling-api-nested-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let owner = test_actor_with_account(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "schedule.nested",
            station_auth::OperatorRole::MeetingOperator,
        );
        let search_path = "/api/v1/scheduling/saved-searches/33333333-4444-4555-8666-777777777777";
        let base = serde_json::json!({
            "name": "Meeting search",
            "query": {
                "all_of": [{
                    "Custom": {
                        "field": "program_type",
                        "predicate": {"Equals": {"Choice": "Meeting"}}
                    }
                }],
                "any_of": [],
                "order_by": "TitleAscending",
                "limit": 20
            },
            "expected_revision": null
        });

        let mut unknown_query_field = base.clone();
        unknown_query_field["query"]["unexpected"] = serde_json::Value::Bool(true);
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                search_path,
                Some(&serde_json::to_vec(&unknown_query_field).unwrap()),
                &owner,
            )
            .status,
            400
        );

        let mut unknown_predicate_field = base.clone();
        unknown_predicate_field["query"]["all_of"][0]["Custom"]["unexpected"] =
            serde_json::Value::Bool(true);
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                search_path,
                Some(&serde_json::to_vec(&unknown_predicate_field).unwrap()),
                &owner,
            )
            .status,
            400
        );

        let mut malformed_nested_member = base;
        malformed_nested_member["query"]["all_of"][0]["Custom"]["predicate"] =
            serde_json::json!({"NumberRange": {"min_inclusive": null, "max_inclusive": null}});
        assert_eq!(
            api.handle_authenticated(
                "PUT",
                search_path,
                Some(&serde_json::to_vec(&malformed_nested_member).unwrap()),
                &owner,
            )
            .status,
            422
        );

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn scheduling_automation_write_errors_are_exact_client_responses() {
        for (error, status, code) in [
            (
                ScheduleAutomationWriteError::DependencyConflict {
                    authority: "saved_search",
                    requested_revision: 1,
                    current_revision: Some(2),
                },
                409,
                "authority_revision_conflict",
            ),
            (
                ScheduleAutomationWriteError::Rebound,
                409,
                "scheduling_authority_rebound",
            ),
            (
                ScheduleAutomationWriteError::Capacity,
                422,
                "recurring_rule_capacity_exceeded",
            ),
        ] {
            let response = scheduling_write_error_response(error, "recurring_rule");
            assert_eq!(response.status, status);
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&response.body).unwrap()["error"]["code"],
                code
            );
        }
    }

    #[test]
    fn asset_metadata_storage_corruption_remains_a_server_error() {
        let response = asset_metadata_write_error_response(AssetMetadataWriteError::Storage(
            "database returned invalid asset metadata binding: row and record disagree".into(),
        ));
        assert_eq!(response.status, 500);
        let body: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
        assert_eq!(body["error"]["code"], "storage_error");
    }

    #[test]
    fn scheduled_recording_api_uses_authenticated_operator_and_server_authority() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-recording-api-{nonce}.db"));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let now = system_time_millis().unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Recording authority".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "22222222-3333-4444-8555-666666666666",
                    "Recording output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
        let admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let recording_id = "33333333-4444-4555-8666-777777777777";
        let schedule_item_id = "44444444-5555-4666-8777-888888888888";
        let schedule_start = now + 60_000;
        let media_path = database.with_extension("recording.ts");
        std::fs::write(&media_path, b"scheduled recording API media").unwrap();
        for asset_id in ["a".repeat(64), "b".repeat(64), "d".repeat(64)] {
            api.storage
                .put_media_asset(&MediaAsset {
                    asset_id,
                    media_path: media_path.to_string_lossy().into_owned(),
                    duration_ms: 30_000,
                    readiness: AssetReadiness::Ready,
                })
                .unwrap();
        }
        api.storage
            .put_schedule_item(&ScheduleItem {
                item_id: schedule_item_id.into(),
                revision: 1,
                channel_id: channel_id.into(),
                asset_id: "a".repeat(64),
                title: "Committed recording item".into(),
                starts_at_unix_ms: schedule_start,
                duration_ms: 30_000,
                state: ScheduleState::Draft,
            })
            .unwrap();
        api.storage
            .commit_schedule(
                "recording-api-report-one",
                "recording-api-plan-one",
                schedule_item_id,
                "test-harness",
                now,
                "scheduled recording fixture",
            )
            .unwrap();
        let candidates_path =
            format!("/api/v1/scheduled-recordings/candidates?channel_id={channel_id}&limit=1");
        assert_eq!(
            api.handle_in_process("GET", &candidates_path, None).status,
            403
        );
        let records_clerk = test_actor(station_auth::OperatorRole::RecordsClerk);
        assert_eq!(
            api.handle_authenticated("GET", &candidates_path, None, &records_clerk)
                .status,
            403
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/scheduled-recordings/candidates?channel_id={channel_id}&limit=0"),
                None,
                &admin,
            )
            .status,
            400
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/scheduled-recordings/candidates?limit=1&channel_id={channel_id}"),
                None,
                &admin,
            )
            .status,
            400
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!(
                    "/api/v1/scheduled-recordings/candidates?channel_id={channel_id}&limit=1&extra=value"
                ),
                None,
                &admin,
            )
            .status,
            400
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                "/api/v1/scheduled-recordings/candidates?channel_id=AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA&limit=1",
                None,
                &admin,
            )
            .status,
            400
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!(
                    "/api/v1/scheduled-recordings/candidates?channel_id={channel_id}&limit=129"
                ),
                None,
                &admin,
            )
            .status,
            400
        );
        let candidates = api.handle_authenticated("GET", &candidates_path, None, &admin);
        assert_eq!(candidates.status, 200);
        let candidates: Vec<ScheduleItem> = serde_json::from_slice(&candidates.body).unwrap();
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].item_id, schedule_item_id);
        assert_eq!(candidates[0].revision, 2);
        assert_eq!(candidates[0].state, ScheduleState::Committed);
        assert!(candidates[0].starts_at_unix_ms > now);
        let meeting_candidate_reader = test_actor(station_auth::OperatorRole::MeetingOperator);
        assert_eq!(
            api.handle_authenticated("GET", &candidates_path, None, &meeting_candidate_reader,)
                .status,
            200
        );
        let create = serde_json::to_vec(&serde_json::json!({
            "recording_id": recording_id,
            "schedule_item_id": schedule_item_id
        }))
        .unwrap();
        let forged = serde_json::to_vec(&serde_json::json!({
            "recording_id": recording_id,
            "channel_id": channel_id,
            "schedule_item_id": schedule_item_id,
            "schedule_revision": 7,
            "scheduled_start_unix_ms": now + 60_000
        }))
        .unwrap();
        assert_eq!(
            api.handle_authenticated(
                "POST",
                "/api/v1/scheduled-recordings",
                Some(&forged),
                &admin,
            )
            .status,
            400
        );
        let created = api.handle_authenticated(
            "POST",
            "/api/v1/scheduled-recordings",
            Some(&create),
            &admin,
        );
        assert_eq!(created.status, 201);
        let created: station_recording::ScheduledRecording =
            serde_json::from_slice(&created.body).unwrap();
        assert_eq!(
            created.definition.requested_by.actor_id,
            admin.account_id().as_str()
        );
        assert_eq!(
            created.definition.requested_by.role,
            station_recording::RecordingActorRole::Supervisor
        );
        assert!(created.definition.requested_at_unix_ms >= now);
        assert_eq!(created.definition.channel_id, channel_id);
        assert_eq!(created.definition.schedule_revision, 2);
        assert_eq!(created.definition.scheduled_start_unix_ms, schedule_start);
        assert_eq!(
            api.handle_in_process("POST", "/api/v1/scheduled-recordings", Some(&create),)
                .status,
            403
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/scheduled-recordings/{recording_id}"),
                None,
                &admin,
            )
            .status,
            200
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/scheduled-recordings?channel_id={channel_id}&limit=1"),
                None,
                &admin,
            )
            .status,
            200
        );
        assert_eq!(
            api.handle_authenticated(
                "GET",
                &format!("/api/v1/scheduled-recordings/{recording_id}/history?limit=1"),
                None,
                &admin,
            )
            .status,
            200
        );
        let armed = api.handle_authenticated(
            "POST",
            &format!("/api/v1/scheduled-recordings/{recording_id}/arm"),
            Some(br#"{"expected_revision":1}"#),
            &admin,
        );
        assert_eq!(armed.status, 200);
        let armed: station_recording::ScheduledRecording =
            serde_json::from_slice(&armed.body).unwrap();
        assert!(matches!(
            &armed.lifecycle,
            station_recording::RecordingLifecycle::Armed { .. }
        ));
        assert_eq!(
            api.handle_in_process(
                "POST",
                &format!("/api/v1/scheduled-recordings/{recording_id}/worker-transition"),
                Some(br#"{"candidate":{"source_commit":"forged"},"occurred_at_unix_ms":0}"#),
            )
            .status,
            404
        );
        api.record_trusted_scheduled_recording_worker_observation(
            TrustedScheduledRecordingWorkerObservation {
                recording_id: recording_id.into(),
                expected_revision: armed.revision,
                event: TrustedScheduledRecordingWorkerEvent::Fail {
                    reason: "trusted capture fault".into(),
                },
            },
        )
        .unwrap();
        assert!(matches!(
            api.storage
                .read_scheduled_recording(recording_id)
                .unwrap()
                .unwrap()
                .lifecycle,
            station_recording::RecordingLifecycle::Failed { .. }
        ));
        let meeting_operator = test_actor(station_auth::OperatorRole::MeetingOperator);
        let second_recording_id = "55555555-6666-4777-8888-999999999999";
        let second_schedule_item_id = "66666666-7777-4888-8999-aaaaaaaaaaaa";
        api.storage
            .put_schedule_item(&ScheduleItem {
                item_id: second_schedule_item_id.into(),
                revision: 1,
                channel_id: channel_id.into(),
                asset_id: "b".repeat(64),
                title: "Meeting operator recording item".into(),
                starts_at_unix_ms: schedule_start + 120_000,
                duration_ms: 30_000,
                state: ScheduleState::Draft,
            })
            .unwrap();
        api.storage
            .commit_schedule(
                "recording-api-report-two",
                "recording-api-plan-two",
                second_schedule_item_id,
                "test-harness",
                now + 1,
                "scheduled recording fixture",
            )
            .unwrap();
        let meeting_create = serde_json::to_vec(&serde_json::json!({
            "recording_id": second_recording_id,
            "schedule_item_id": second_schedule_item_id
        }))
        .unwrap();
        assert_eq!(
            api.handle_authenticated(
                "POST",
                "/api/v1/scheduled-recordings",
                Some(&meeting_create),
                &meeting_operator,
            )
            .status,
            201
        );
        let cancelled = api.handle_authenticated(
            "POST",
            &format!("/api/v1/scheduled-recordings/{second_recording_id}/cancel"),
            Some(br#"{"expected_revision":1,"reason":"meeting moved"}"#),
            &meeting_operator,
        );
        assert_eq!(cancelled.status, 200);
        let missing = api.handle_authenticated(
            "POST",
            "/api/v1/scheduled-recordings",
            Some(br#"{"recording_id":"77777777-8888-4999-8aaa-bbbbbbbbbbbb","schedule_item_id":"88888888-9999-4aaa-8bbb-cccccccccccc"}"#),
            &admin,
        );
        assert_eq!(missing.status, 404);
        let draft_schedule_item_id = "99999999-aaaa-4bbb-8ccc-dddddddddddd";
        api.storage
            .put_schedule_item(&ScheduleItem {
                item_id: draft_schedule_item_id.into(),
                revision: 1,
                channel_id: channel_id.into(),
                asset_id: "c".repeat(64),
                title: "Draft recording item".into(),
                starts_at_unix_ms: schedule_start,
                duration_ms: 30_000,
                state: ScheduleState::Draft,
            })
            .unwrap();
        let draft_body = format!(
            r#"{{"recording_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","schedule_item_id":"{draft_schedule_item_id}"}}"#
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                "/api/v1/scheduled-recordings",
                Some(draft_body.as_bytes()),
                &admin,
            )
            .status,
            409
        );
        let stale_recording_id = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
        let stale_schedule_item_id = "cccccccc-dddd-4eee-8fff-000000000000";
        let stale_schedule = ScheduleItem {
            item_id: stale_schedule_item_id.into(),
            revision: 1,
            channel_id: channel_id.into(),
            asset_id: "d".repeat(64),
            title: "Stale binding item".into(),
            starts_at_unix_ms: schedule_start + 240_000,
            duration_ms: 30_000,
            state: ScheduleState::Draft,
        };
        api.storage.put_schedule_item(&stale_schedule).unwrap();
        api.storage
            .commit_schedule(
                "recording-api-report-three",
                "recording-api-plan-three",
                stale_schedule_item_id,
                "test-harness",
                now + 2,
                "scheduled recording fixture",
            )
            .unwrap();
        let stale_create = format!(
            r#"{{"recording_id":"{stale_recording_id}","schedule_item_id":"{stale_schedule_item_id}"}}"#
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                "/api/v1/scheduled-recordings",
                Some(stale_create.as_bytes()),
                &admin,
            )
            .status,
            201
        );
        let mut changed_schedule = api
            .storage
            .read_schedule_item(stale_schedule_item_id)
            .unwrap()
            .unwrap();
        changed_schedule.revision = 3;
        changed_schedule.title = "Changed schedule item".into();
        changed_schedule.state = ScheduleState::Draft;
        api.storage.put_schedule_item(&changed_schedule).unwrap();
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("/api/v1/scheduled-recordings/{stale_recording_id}/arm"),
                Some(br#"{"expected_revision":1}"#),
                &admin,
            )
            .status,
            409
        );
        assert!(
            api.record_trusted_scheduled_recording_worker_observation(
                TrustedScheduledRecordingWorkerObservation {
                    recording_id: stale_recording_id.into(),
                    expected_revision: 1,
                    event: TrustedScheduledRecordingWorkerEvent::Fail {
                        reason: "stale schedule binding".into(),
                    },
                },
            )
            .unwrap_err()
            .contains("schedule item is not committed")
        );
        assert_eq!(
            api.handle_authenticated(
                "POST",
                &format!("/api/v1/scheduled-recordings/{recording_id}/arm"),
                Some(br#"{"expected_revision":3}"#),
                &meeting_operator,
            )
            .status,
            422
        );
        drop(api);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
        let _ = std::fs::remove_file(media_path);
        let _ = std::fs::remove_dir_all(application);
    }

    fn source_evidence_authorities() -> (SavedSearch, DaypartBlock, RecurringRule) {
        let search_id = "00000000-0000-4000-8000-000000000001";
        let channel_id = "00000000-0000-4000-8000-000000000002";
        let block_id = "00000000-0000-4000-8000-000000000004";
        let rule_id = "00000000-0000-4000-8000-000000000005";
        let search = SavedSearch {
            search_id: search_id.into(),
            name: "Council meeting rotation".into(),
            owner_operator_id: "00000000-0000-4000-8000-000000000003".into(),
            created_at_utc_ms: 1_700_000_000_000,
            query: SavedSearchQuery {
                all_of: vec![AssetPredicate::TitleContains("Council".into())],
                any_of: Vec::new(),
                order_by: SavedSearchOrder::TitleAscending,
                limit: 8,
            },
            revision: 1,
        };
        let block = DaypartBlock {
            block_id: block_id.into(),
            channel_id: channel_id.into(),
            label: "Weeknight civic".into(),
            starts_local_minute: 1_080,
            ends_local_minute: 1_320,
            weekday_mask: 0b0111_1111,
            season_starts_on: None,
            season_ends_on: None,
            allow_rule_ids: vec![rule_id.into()],
            revision: 1,
        };
        let rule = RecurringRule {
            rule_id: rule_id.into(),
            channel_id: channel_id.into(),
            saved_search_id: search_id.into(),
            saved_search_revision: 1,
            pick_strategy: AssetPickStrategy::Top,
            rolling_window_days: 14,
            avoid_repeat_within_days: None,
            enabled: true,
            priority: 0,
            block_id: block_id.into(),
            block_revision: 1,
            weekday_mask: 0b0111_1111,
            interval_weeks: 1,
            anchor_date: LocalDate {
                year: 2026,
                month: 1,
                day: 1,
            },
            timezone: RecurrenceTimeZone::Named {
                iana_name: "America/Denver".into(),
                dst: DstApplication::EarlierFold,
            },
            local_start_minute: 1_140,
            duration_minutes: 60,
            revision: 1,
        };
        (search, block, rule)
    }

    fn source_evidence_window() -> MaterializationWindow {
        MaterializationWindow {
            starts_at_utc_ms: 1_767_225_600_000,
            ends_at_utc_ms: 1_768_435_200_000,
        }
    }

    fn open_api_with_source_evidence(
        database: &Path,
        authority: Arc<StationSourceEvidenceAuthority>,
    ) -> Api {
        Api::open_with_application_directory_and_cache(
            database,
            database.with_extension("application"),
            Arc::new(Mutex::new(None)),
            OrchestrationHealth::ready_without_supervisor(),
            authority,
        )
        .unwrap()
    }

    #[test]
    fn standalone_api_lifetimes_use_distinct_source_evidence_authorities() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-source-evidence-authority-lifetime-{nonce}.db"
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();

        let first_authority = {
            let first_api = Api::open_with_application_directory(&database, &application).unwrap();
            Arc::clone(&first_api.source_evidence_authority)
        };
        let second_api = Api::open_with_application_directory(&database, &application).unwrap();

        assert!(!Arc::ptr_eq(
            &first_authority,
            &second_api.source_evidence_authority
        ));

        drop(second_api);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
        let _ = std::fs::remove_dir_all(application);
    }

    fn seed_source_evidence_authorities(
        api: &Api,
        search: &SavedSearch,
        block: &DaypartBlock,
        rule: &RecurringRule,
    ) {
        api.storage
            .write_saved_search(search, None, &search.owner_operator_id, 1)
            .unwrap();
        api.storage
            .write_daypart_block(block, None, &search.owner_operator_id, 2)
            .unwrap();
        api.storage
            .write_recurring_rule(rule, None, &search.owner_operator_id, 3)
            .unwrap();
    }

    #[test]
    fn materialization_evidence_snapshot_survives_same_boot_reconstruction_and_binds_both_inputs() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-candidate-source-evidence-same-boot-{nonce}.db"
        ));
        let authority = Arc::new(StationSourceEvidenceAuthority::new().unwrap());
        let (search, block, rule) = source_evidence_authorities();
        let window = source_evidence_window();
        let first_api = open_api_with_source_evidence(&database, Arc::clone(&authority));
        seed_source_evidence_authorities(&first_api, &search, &block, &rule);
        assert!(Arc::ptr_eq(
            &first_api.source_evidence_authority,
            &authority
        ));
        let evidence = first_api
            .snapshot_authenticated_materialization_evidence(&rule.rule_id, &window)
            .unwrap();
        assert_eq!(evidence.search, search);
        assert_eq!(evidence.block, block);
        assert_eq!(evidence.rule, rule);
        assert_eq!(evidence.window, window);
        assert!(evidence.inputs.candidate_projection.candidates.is_empty());
        assert!(evidence.inputs.prior_airings.airings.is_empty());
        drop(first_api);

        let reconstructed_api = open_api_with_source_evidence(&database, Arc::clone(&authority));
        assert!(Arc::ptr_eq(
            &reconstructed_api.source_evidence_authority,
            &authority
        ));
        let candidate_scope_sha256 = materialization_evidence_scope_sha256(
            &search,
            &block,
            &rule,
            &window,
            SourceEvidenceKind::CandidateProjection,
        )
        .unwrap();
        let candidate_payload_sha256 =
            canonical_candidate_projection_sha256(&evidence.inputs.candidate_projection.candidates)
                .unwrap();
        authority
            .verify(
                SourceEvidenceKind::CandidateProjection,
                &evidence.inputs.candidate_projection.receipt,
                &candidate_scope_sha256,
                &candidate_payload_sha256,
                0,
            )
            .unwrap();

        let prior_scope_sha256 = materialization_evidence_scope_sha256(
            &search,
            &block,
            &rule,
            &window,
            SourceEvidenceKind::PriorAirings,
        )
        .unwrap();
        let prior_payload_sha256 =
            canonical_prior_airings_sha256(&evidence.inputs.prior_airings.airings).unwrap();
        authority
            .verify(
                SourceEvidenceKind::PriorAirings,
                &evidence.inputs.prior_airings.receipt,
                &prior_scope_sha256,
                &prior_payload_sha256,
                0,
            )
            .unwrap();

        let mut rebound_search = search.clone();
        rebound_search.name.push_str(" rebound");
        let rebound_scope_sha256 = materialization_evidence_scope_sha256(
            &rebound_search,
            &block,
            &rule,
            &window,
            SourceEvidenceKind::CandidateProjection,
        )
        .unwrap();
        assert!(
            authority
                .verify(
                    SourceEvidenceKind::CandidateProjection,
                    &evidence.inputs.candidate_projection.receipt,
                    &rebound_scope_sha256,
                    &candidate_payload_sha256,
                    i64::MAX,
                )
                .is_err()
        );
        let mut rebound_payload_sha256 = candidate_payload_sha256.clone();
        rebound_payload_sha256.replace_range(
            ..1,
            if candidate_payload_sha256.starts_with('0') {
                "1"
            } else {
                "0"
            },
        );
        assert!(
            authority
                .verify(
                    SourceEvidenceKind::CandidateProjection,
                    &evidence.inputs.candidate_projection.receipt,
                    &candidate_scope_sha256,
                    &rebound_payload_sha256,
                    i64::MAX,
                )
                .is_err()
        );

        drop(reconstructed_api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn materialization_evidence_receipts_are_invalid_after_authority_restart() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-candidate-source-restart-{nonce}.db"));
        let first_authority = Arc::new(StationSourceEvidenceAuthority::new().unwrap());
        let first_api = open_api_with_source_evidence(&database, Arc::clone(&first_authority));
        let (search, block, rule) = source_evidence_authorities();
        let window = source_evidence_window();
        seed_source_evidence_authorities(&first_api, &search, &block, &rule);
        let evidence = first_api
            .snapshot_authenticated_materialization_evidence(&rule.rule_id, &window)
            .unwrap();
        let scope_sha256 = materialization_evidence_scope_sha256(
            &search,
            &block,
            &rule,
            &window,
            SourceEvidenceKind::CandidateProjection,
        )
        .unwrap();
        let payload_sha256 =
            canonical_candidate_projection_sha256(&evidence.inputs.candidate_projection.candidates)
                .unwrap();
        let replacement_authority = StationSourceEvidenceAuthority::new().unwrap();
        assert!(
            replacement_authority
                .verify(
                    SourceEvidenceKind::CandidateProjection,
                    &evidence.inputs.candidate_projection.receipt,
                    &scope_sha256,
                    &payload_sha256,
                    0,
                )
                .is_err()
        );

        drop(first_api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn source_evidence_candidate_adapter_has_no_public_http_route() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-source-evidence-no-route-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let forged = br#"{
            "candidates": [],
            "scope_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "payload_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "receipt": {"issuer_id": "browser"},
            "observed_at_utc_ms": 0
        }"#;
        for path in [
            "/api/v1/scheduling/candidate-projection",
            "/api/v1/scheduling/source-evidence",
            "/api/v1/scheduling/materialize",
        ] {
            assert_eq!(api.handle_in_process("GET", path, None).status, 400);
            assert_eq!(
                api.handle_in_process("POST", path, Some(forged)).status,
                400
            );
        }

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn transport_target_api_rejects_secret_bearing_hosts_and_never_echoes_them() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-target-api-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Transport target API".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    "22222222-3333-4444-8555-666666666666",
                    "Required sink",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
        let secret = "not-a-real-secret";
        let body = serde_json::to_vec(&serde_json::json!({
            "target_id": "target-1",
            "label": "Headend receiver",
            "protocol": "https_api",
            "host": format!("receiver.example.test/api/{secret}"),
            "port": 443,
            "expected_revision": null
        }))
        .unwrap();
        let path = format!("/api/v1/channels/{channel_id}/transport-target");

        let unauthenticated = api.handle_in_process("PUT", &path, Some(&body));
        assert_eq!(unauthenticated.status, 403);
        assert!(!String::from_utf8_lossy(&unauthenticated.body).contains(secret));

        let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let rejected = api.handle_authenticated("PUT", &path, Some(&body), &setup_admin);
        assert_eq!(rejected.status, 422);
        assert!(!String::from_utf8_lossy(&rejected.body).contains(secret));
        assert!(
            api.storage
                .read_transport_target_config(channel_id, "22222222-3333-4444-8555-666666666666")
                .unwrap()
                .is_none()
        );

        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn setup_admin_provisions_rotates_and_removes_a_write_only_transport_authorization() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-target-credential-api-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Transport credential API".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Required sink",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: true,
            })
            .unwrap();
        let path = format!("/api/v1/channels/{channel_id}/transport-target");
        let secret = "Bearer local-setup-admin-secret";
        let create = serde_json::to_vec(&serde_json::json!({
            "target_id": "target-1",
            "label": "Headend receiver",
            "protocol": "https_api",
            "host": "receiver.example.test",
            "port": 443,
            "authorization": secret,
            "expected_revision": null
        }))
        .unwrap();
        let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let response = api.handle_authenticated("PUT", &path, Some(&create), &setup_admin);
        assert_eq!(
            response.status,
            200,
            "{}",
            String::from_utf8_lossy(&response.body)
        );
        assert!(!String::from_utf8_lossy(&response.body).contains(secret));
        let created: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
        assert_eq!(created["credential_configured"], true);

        // Supplying a fresh write-only authorization rotates the target
        // revision. Omitting it on a later revision removes authorization;
        // old protected material is immutable but cannot authorize the new
        // target revision.
        let rotation = serde_json::to_vec(&serde_json::json!({
            "target_id": "target-1",
            "label": "Headend receiver",
            "protocol": "https_api",
            "host": "receiver.example.test",
            "port": 443,
            "authorization": "Bearer rotated-secret",
            "expected_revision": 1
        }))
        .unwrap();
        let rotated = api.handle_authenticated("PUT", &path, Some(&rotation), &setup_admin);
        assert_eq!(rotated.status, 200);
        assert!(!String::from_utf8_lossy(&rotated.body).contains("rotated-secret"));
        let remove = serde_json::to_vec(&serde_json::json!({
            "target_id": "target-1",
            "label": "Headend receiver",
            "protocol": "https_api",
            "host": "receiver.example.test",
            "port": 443,
            "expected_revision": 2
        }))
        .unwrap();
        let removed = api.handle_authenticated("PUT", &path, Some(&remove), &setup_admin);
        assert_eq!(removed.status, 200);
        let removed: serde_json::Value = serde_json::from_slice(&removed.body).unwrap();
        assert_eq!(removed["credential_configured"], false);
        let reader = test_actor(station_auth::OperatorRole::MeetingOperator);
        let read = api.handle_authenticated("GET", &path, None, &reader);
        assert_eq!(read.status, 200);
        assert!(!String::from_utf8_lossy(&read.body).contains("secret"));
        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn authenticated_api_queue_runs_private_analyzer_and_receiver_to_exact_current_receipts() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-transport-outer-e2e-{nonce}"));
        let application = root.join("application");
        let database = root.join("data/station.db");
        std::fs::create_dir_all(database.parent().unwrap()).unwrap();
        std::fs::create_dir_all(&application).unwrap();
        install_transport_candidate(database.parent().unwrap(), &application);
        let api = Api::open_with_station_box_probe(
            &database,
            &application,
            transport_station_box_probe(),
        )
        .unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Transport outer E2E".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Required sink",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: true,
            })
            .unwrap();
        let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
        let reader = test_actor(station_auth::OperatorRole::MeetingOperator);
        let initial_authorization = "Bearer outer-e2e-initial-secret";
        let authorization = "Bearer outer-e2e-rotated-secret";
        let target_path = format!("/api/v1/channels/{channel_id}/transport-target");
        let target = serde_json::to_vec(&serde_json::json!({
            "target_id": "receiver-1",
            "label": "Headend receiver",
            "protocol": "https_api",
            "host": "receiver.example.test",
            "port": 443,
            "authorization": initial_authorization,
            "expected_revision": null
        }))
        .unwrap();
        assert_eq!(
            api.handle_authenticated("PUT", &target_path, Some(&target), &setup_admin)
                .status,
            200
        );
        let rotated_target = serde_json::to_vec(&serde_json::json!({
            "target_id": "receiver-1",
            "label": "Headend receiver",
            "protocol": "https_api",
            "host": "receiver.example.test",
            "port": 443,
            "authorization": authorization,
            "expected_revision": 1
        }))
        .unwrap();
        let rotated =
            api.handle_authenticated("PUT", &target_path, Some(&rotated_target), &setup_admin);
        assert_eq!(rotated.status, 200);
        assert!(!String::from_utf8_lossy(&rotated.body).contains(initial_authorization));
        assert!(!String::from_utf8_lossy(&rotated.body).contains(authorization));
        let jobs_path = format!("/api/v1/channels/{channel_id}/transport-proof/jobs");
        let queued = api.handle_authenticated("POST", &jobs_path, None, &setup_admin);
        assert_eq!(
            queued.status,
            202,
            "{}",
            String::from_utf8_lossy(&queued.body)
        );
        let queued: serde_json::Value = serde_json::from_slice(&queued.body).unwrap();
        let job_id = queued["job_id"].as_str().unwrap();
        let response = b"authenticated receiver response";
        station_storage::test_support::run_injected_transport_controller_iteration(
            &database,
            &application,
            authorization,
            response,
            station_storage::test_support::InjectedAuthorityMutation::None,
        )
        .unwrap();
        let status_path = format!("/api/v1/channels/{channel_id}/transport-proof/jobs/{job_id}");
        let status = api.handle_authenticated("GET", &status_path, None, &reader);
        assert_eq!(status.status, 200);
        let status: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
        assert_eq!(status["state"], "completed");
        assert_eq!(
            status["tables_output_sha256"],
            format!(
                "{:x}",
                Sha256::digest(include_bytes!(
                    "../../station-storage/tests/fixtures/tsduck-3.44-h264-aac-tables.json"
                ))
            )
        );
        let proof_path = format!("/api/v1/channels/{channel_id}/transport-proof");
        let proof = api.handle_authenticated("GET", &proof_path, None, &reader);
        assert_eq!(proof.status, 200);
        let proof: serde_json::Value = serde_json::from_slice(&proof.body).unwrap();
        assert_eq!(proof["history"]["analysis"][0]["proof_id"], job_id);
        assert_eq!(proof["history"]["reachability"][0]["proof_id"], job_id);
        assert_eq!(
            proof["history"]["reachability"][0]["response_identity_sha256"],
            format!("{:x}", Sha256::digest(response))
        );
        assert_eq!(
            proof["validations"][0]["outcome"],
            "pass",
            "{}",
            serde_json::to_string_pretty(&proof).unwrap()
        );
        drop(api);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn private_controller_fails_closed_when_authority_mutates_after_receiver_probe() {
        use station_storage::test_support::InjectedAuthorityMutation;
        for mutation in [
            InjectedAuthorityMutation::TargetRevision,
            InjectedAuthorityMutation::OutputConfiguration,
            InjectedAuthorityMutation::InstallGeneration,
        ] {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = std::env::temp_dir().join(format!(
                "townlight-transport-stale-e2e-{mutation:?}-{nonce}"
            ));
            let application = root.join("application");
            let database = root.join("data/station.db");
            std::fs::create_dir_all(database.parent().unwrap()).unwrap();
            std::fs::create_dir_all(&application).unwrap();
            install_transport_candidate(database.parent().unwrap(), &application);
            let api = Api::open_with_station_box_probe(
                &database,
                &application,
                transport_station_box_probe(),
            )
            .unwrap();
            let channel_id = "11111111-2222-4333-8444-555555555555";
            let sink_id = "22222222-3333-4444-8555-666666666666";
            api.storage
                .put_channel(&ChannelConfiguration {
                    channel_id: channel_id.into(),
                    display_name: "Stale authority E2E".into(),
                    output_profile: station_schedule::OutputProfileId::Proof360p30,
                    output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                        sink_id,
                        "Required sink",
                        "127.0.0.1:5500",
                        -16.0,
                    )],
                    enabled: true,
                })
                .unwrap();
            let setup_admin = test_actor(station_auth::OperatorRole::SetupAdmin);
            let authorization = "Bearer stale-e2e-secret";
            let target_path = format!("/api/v1/channels/{channel_id}/transport-target");
            let target = serde_json::to_vec(&serde_json::json!({
                "target_id": "receiver-1", "label": "Headend receiver",
                "protocol": "https_api", "host": "receiver.example.test", "port": 443,
                "authorization": authorization, "expected_revision": null
            }))
            .unwrap();
            assert_eq!(
                api.handle_authenticated("PUT", &target_path, Some(&target), &setup_admin)
                    .status,
                200
            );
            let jobs_path = format!("/api/v1/channels/{channel_id}/transport-proof/jobs");
            let queued = api.handle_authenticated("POST", &jobs_path, None, &setup_admin);
            assert_eq!(queued.status, 202);
            let queued: serde_json::Value = serde_json::from_slice(&queued.body).unwrap();
            let job_id = queued["job_id"].as_str().unwrap();
            assert!(
                station_storage::test_support::run_injected_transport_controller_iteration(
                    &database,
                    &application,
                    authorization,
                    b"receiver response",
                    mutation,
                )
                .is_err()
            );
            let record = api
                .storage
                .read_transport_proof_job(job_id)
                .unwrap()
                .unwrap();
            assert_eq!(
                record.state,
                station_storage::TransportProofJobState::Failed
            );
            let history = api
                .storage
                .read_transport_proof_history(channel_id, None, 10)
                .unwrap();
            assert!(history.analysis.is_empty());
            assert!(history.reachability.is_empty());
            drop(api);
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn commissioning_transport_uses_the_authoritative_sink_snapshot_after_a_stale_channel_read() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-commissioning-transport-snapshot-{}-{nonce}.db",
            std::process::id()
        ));
        let application = database.with_extension("application");
        std::fs::create_dir_all(&application).unwrap();
        let mut api =
            Api::open_with_station_box_probe(&database, &application, station_box_probe()).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_a = "22222222-3333-4444-8555-666666666666";
        let sink_b = "33333333-4444-4555-8666-777777777777";
        let sink_a_config = station_schedule::OutputSinkConfiguration::required_udp(
            sink_a,
            "Sink A",
            "127.0.0.1:5500",
            -16.0,
        );
        api.storage
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Commissioning transport snapshot".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![sink_a_config.clone()],
                enabled: true,
            })
            .unwrap();
        let hook_fired = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let hook_observed = Arc::clone(&hook_fired);
        let hook_channel_id = channel_id.to_string();
        let hook_sink_b = sink_b.to_string();
        api.commissioning_after_channel_read_hook = Some(Arc::new(move |storage| {
            if hook_observed.swap(true, std::sync::atomic::Ordering::SeqCst) {
                return;
            }
            storage
                .put_channel(&ChannelConfiguration {
                    channel_id: hook_channel_id.clone(),
                    display_name: "Commissioning transport snapshot".into(),
                    output_profile: station_schedule::OutputProfileId::Proof360p30,
                    output_sinks: vec![
                        sink_a_config.clone(),
                        station_schedule::OutputSinkConfiguration::required_udp(
                            &hook_sink_b,
                            "Sink B",
                            "127.0.0.1:5501",
                            -16.0,
                        ),
                    ],
                    enabled: true,
                })
                .unwrap();
        }));

        let report = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        assert!(hook_fired.load(std::sync::atomic::Ordering::SeqCst));
        let report: serde_json::Value = serde_json::from_slice(&report.body).unwrap();
        let cable = report["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|check| check["id"] == "cable_transport_validation")
            .unwrap();
        assert_eq!(cable["status"], "blocked");
        assert!(cable["detail"].as_str().unwrap().contains(sink_b));

        drop(api);
        std::fs::remove_dir_all(application).unwrap();
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn public_ott_routes_issue_server_bound_sessions_and_collect_verified_beacons() {
        use station_ott_contract::{
            AccessibilitySemantics, ClientPlaybackBeacon, OttCapability, OttPlatform,
            PlaybackBeaconEvent, PlaybackProtocol, PlaybackSource, PublicCatalogItem,
            PublicCatalogPage, PublicContentIdentity, PublicSubtitleTrack, SubtitleKind,
        };
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-api-ott-{nonce}.db"));
        let api = Api::open(&database).unwrap();
        let page = PublicCatalogPage {
            contract_version: station_ott_contract::OTT_CONTRACT_VERSION,
            catalog_revision: 1,
            items: vec![PublicCatalogItem {
                content_id: "22222222-2222-4222-8222-222222222222".into(),
                identity: PublicContentIdentity::Live {
                    channel_id: "11111111-1111-4111-8111-111111111111".into(),
                },
                title: "Council meeting".into(),
                summary: "Accessible civic programming".into(),
                vod_duration_ms: None,
                playback: PlaybackSource {
                    protocol: PlaybackProtocol::Hls,
                    manifest_url: "https://cdn.example.test/live.m3u8".into(),
                },
                subtitles: vec![PublicSubtitleTrack {
                    track_id: "33333333-3333-4333-8333-333333333333".into(),
                    language: "en-US".into(),
                    kind: SubtitleKind::ClosedCaptions,
                    url: "https://cdn.example.test/live.vtt".into(),
                    is_default: true,
                }],
                accessibility: AccessibilitySemantics {
                    captions_available: true,
                    screen_reader_summary: "Council meeting".into(),
                    remote_navigation_supported: true,
                },
            }],
            next_cursor: None,
        };
        api.storage
            .write_ott_catalog_page(&page, "test", 1)
            .unwrap();
        let runtime = auth::AuthRuntime::new("127.0.0.1:4070".parse().unwrap()).unwrap();
        let native_catalog = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "GET",
                "/api/v1/ott/catalog",
                &[("Host", runtime.expected_host())],
                b"",
            ),
        );
        assert_eq!(native_catalog.status, 200);
        let request = station_ott_runtime::PlaybackSessionIssueRequest {
            request_id: "api-request-1".into(),
            content_id: page.items[0].content_id.clone(),
            client: station_ott_contract::PlatformCapabilityDeclaration {
                contract_version: station_ott_contract::OTT_CONTRACT_VERSION,
                client_id: "44444444-4444-4444-8444-444444444444".into(),
                platform: OttPlatform::Roku,
                capabilities: vec![
                    OttCapability::HlsPlayback,
                    OttCapability::WebVttSubtitles,
                    OttCapability::ClosedCaptions,
                    OttCapability::ScreenReaderSemantics,
                    OttCapability::KeyboardOrRemoteNavigation,
                    OttCapability::PlaybackBeacons,
                ],
            },
        };
        let request_body = serde_json::to_vec(&request).unwrap();
        let issued = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/ott/sessions",
                &[("Host", runtime.expected_host())],
                &request_body,
            ),
        );
        assert_eq!(issued.status, 200);
        let issued: station_ott_runtime::PlaybackSessionIssueResponse =
            serde_json::from_slice(&issued.body).unwrap();
        let beacon = ClientPlaybackBeacon {
            contract_version: station_ott_contract::OTT_CONTRACT_VERSION,
            beacon_id: "55555555-5555-4555-8555-555555555555".into(),
            session_id: issued.session.session_id.clone(),
            session_revision: issued.session.session_revision,
            session_capability: issued.session_capability.clone(),
            observed_at_unix_ms: issued.session.issued_at_unix_ms + 1,
            event: PlaybackBeaconEvent::Started,
            position_ms: 0,
        };
        let beacon_body = serde_json::to_vec(&beacon).unwrap();
        let accepted = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/ott/beacons",
                &[("Host", runtime.expected_host())],
                &beacon_body,
            ),
        );
        assert_eq!(accepted.status, 202);
        let replay = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/ott/beacons",
                &[("Host", runtime.expected_host())],
                &beacon_body,
            ),
        );
        assert_eq!(replay.status, 202);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&replay.body).unwrap()["replayed"],
            true
        );
        let hostile = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/ott/beacons",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", "https://evil.invalid"),
                ],
                &beacon_body,
            ),
        );
        assert_eq!(hostile.status, 403);
        let duplicate_origin = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/ott/beacons",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Origin", runtime.expected_origin()),
                ],
                &beacon_body,
            ),
        );
        assert_eq!(duplicate_origin.status, 400);
        let contradictory_fetch = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/ott/beacons",
                &[
                    ("Host", runtime.expected_host()),
                    ("Origin", runtime.expected_origin()),
                    ("Sec-Fetch-Site", "cross-site"),
                ],
                &beacon_body,
            ),
        );
        assert_eq!(contradictory_fetch.status, 403);
        let mut forged = beacon;
        forged.beacon_id = "66666666-6666-4666-8666-666666666666".into();
        forged.session_capability = "a".repeat(64);
        assert_ne!(
            api.handle_in_process(
                "POST",
                "/api/v1/ott/beacons",
                Some(&serde_json::to_vec(&forged).unwrap())
            )
            .status,
            202
        );
        for suffix in 2..=station_ott_runtime::MAX_SESSION_ISSUES_PER_WINDOW {
            let mut admitted = request.clone();
            admitted.request_id = format!("api-request-{suffix}");
            let response = dispatch_http(
                &api,
                &runtime,
                &raw_request(
                    "POST",
                    "/api/v1/ott/sessions",
                    &[("Host", runtime.expected_host())],
                    &serde_json::to_vec(&admitted).unwrap(),
                ),
            );
            assert_eq!(response.status, 200);
        }
        let mut blocked_request = request;
        blocked_request.request_id = "api-request-rate-limited".into();
        let blocked = dispatch_http(
            &api,
            &runtime,
            &raw_request(
                "POST",
                "/api/v1/ott/sessions",
                &[("Host", runtime.expected_host())],
                &serde_json::to_vec(&blocked_request).unwrap(),
            ),
        );
        assert_eq!(blocked.status, 429);
        let retry_after = blocked
            .headers
            .iter()
            .find(|header| header.name == "Retry-After")
            .unwrap()
            .value
            .parse::<u64>()
            .unwrap();
        assert!((1..=60).contains(&retry_after));
        drop(api);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn ott_route_storage_failures_are_typed_and_redacted() {
        let session_error = ott_session_error(&OttAuthorityError::Internal);
        assert_eq!(session_error.status, 500);
        assert!(!String::from_utf8_lossy(&session_error.body).contains("123"));
        let beacon_error = ott_beacon_error(&OttAuthorityError::Corrupt);
        assert_eq!(beacon_error.status, 500);
        assert!(!String::from_utf8_lossy(&beacon_error.body).contains("secret"));
        let throttled_session = ott_session_error(&OttAuthorityError::RateLimited(7));
        assert_eq!(throttled_session.status, 429);
        assert_eq!(
            throttled_session.headers,
            vec![ApiResponseHeader {
                name: "Retry-After",
                value: "7".into(),
            }]
        );
        let throttled_beacon = ott_beacon_error(&OttAuthorityError::RateLimited(11));
        assert_eq!(throttled_beacon.status, 429);
        assert_eq!(
            throttled_beacon.headers,
            vec![ApiResponseHeader {
                name: "Retry-After",
                value: "11".into(),
            }]
        );
    }
}
