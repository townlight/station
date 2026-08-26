use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::ffi::{CStr, CString, c_char, c_int, c_longlong, c_void};
use std::os::windows::fs::MetadataExt;
use std::path::{Component, Path, PathBuf};
use std::ptr;
use std::{
    fs,
    io::Write,
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Instant, SystemTime},
};

use serde::de::DeserializeOwned;
use sha2::{Digest, Sha256};
use station_auth::{
    AccountId, BootstrapCredential, CredentialVersion, OperatorAccount, OperatorDisplayName,
    OperatorRole, OperatorSession, OperatorUsername, PasswordPhc, RecoveryCredential, SecretDigest,
};
use station_automation_policy::{
    AutomationAdapterDescriptor, AutomationCandidateIdentity, AutomationConnectionTestEvidence,
    AutomationFeature, AutomationFeaturePolicy, FeatureAutomationStatus,
    derive_feature_automation_status,
};
use station_captions::{
    AssetCaptionCue, AssetCaptionTrack, AssetCaptionTrackAuditEvent, AssetCaptionTrackWrite,
    CaptionEgressProof, CaptionEgressProofJob, CaptionEgressProofJobRecord,
    CaptionEgressProofJobState, CaptionProofCaptureFacts, CaptionProofOutcome,
    CaptionTrackSourceKind, CaptionTrackWriteError, expected_caption_evidence_for_window,
};
use station_cg::{
    CgAuditEvent, CgAuditEventKind, CgBoard, CgBulletin, CgBulletinContent, CgBulletinState,
    CgContentSource, CgFeedCacheEntry, CgFeedItem, CgFeedItemApproval, CgFeedKind, CgFeedSource,
    CgFeedTrustTier, CgImageAsset, CgImageReadiness, CgSnapshotInputs, CgZoneConfig, CgZoneKind,
    built_in_template, inspect_cg_image, managed_cg_image_path,
};
use station_domain::{
    AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID, AlertChannel, AlertChannelKind,
    AlertConditionKind, AlertDelivery, AlertDeliveryPayload, AlertDeliveryState, AlertEvent,
    AlertEventState, AlertNotificationKind, AlertRule, AlertSeverity, ChannelRuntimeObservation,
    ChannelRuntimeState, SelfTestKind, SelfTestSchedule, SelfTestStatus, SelfTestTrigger,
    StationProfile, StationProfileDocument, SystemResourceSample, SystemSelfTest,
    redact_alert_channel_target, validate_alert_channel_target, validate_secure_url,
};
use station_live::{
    LiveSource, LiveSourceWriteError, TakeoverAuditEvent, TakeoverSession, TakeoverState,
};
use station_media_lifecycle::{
    AssetLoudnessAnalysis, IngestJobStatus, IngestSourceKind, LoudnessGateStatus, MediaIngestJob,
    TranscodeFormat, TranscodeJob, TranscodeJobStatus, WatchFileObservation, WatchFolderConfig,
};
use station_metadata::{
    AssetMetadataRecord, MetadataSchema, validate_asset_metadata_record,
    validate_asset_metadata_write, validate_schema_write,
};
use station_readiness::{
    LoudnessProofOutcome, REFERENCE_STATION_CATALOG_VERSION, ReceiverAcceptance,
    ReferenceStationTier, ReferenceStationTierAuditEvent, ReferenceStationTierSelection,
    SinkLoudnessProof,
};
use station_recording::{
    RecordingActor, RecordingActorRole, RecordingExecutionAuthority, RecordingLifecycle,
    RecordingTransitionKind, RecordingTransitionReceipt, ScheduledRecording,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, CommitPlan, CommitReport, DispatchJob, DispatchStatus,
    HeadendProfileApplication, HeadendProfileApplyRequest, HeadendProfileId, HeadendSinkContract,
    MediaAsset, OutputProfileId, OutputSinkConfiguration, OutputSinkKind, ScheduleItem,
    ScheduleState, apply_headend_profile, prepare_commit,
};
pub use station_transport_proof::{
    CableTransportValidation, CableTransportValidationInput, ProofOutcome, TargetDeviceConfig,
    TargetDeviceReachabilityReceipt, TargetProtocol, TransportAnalysisReceipt, TransportFindings,
};
use station_update::{
    PreparedSlotRecord, StagedPackageRecord, UpdateCheckOutcome, UpdateCheckRecord,
    UpdateCheckTrigger,
};

mod schedule_automation_authority;
pub use schedule_automation_authority::{
    DaypartBlockHistoryEntry, RecurringRuleHistoryEntry, SavedSearchHistoryEntry,
    ScheduleAutomationWriteError,
};
mod catalog_authority;
pub use catalog_authority::{CatalogAssetHistoryEntry, CatalogAssetPage, CatalogAssetWriteError};
mod publishing_authority;
pub use publishing_authority::{
    PublishTargetAccountHistory, PublishTargetProfileHistory, PublishingAuthorityWriteError,
};
mod actual_airing_authority;
pub use actual_airing_authority::{
    ActualAiringFact, ActualAiringObservation, ActualAiringReadError, ActualAiringWriteError,
    ActualAiringWriteOutcome, MAX_ACTUAL_AIRING_FUTURE_SKEW_MS, MAX_ACTUAL_AIRING_QUERY,
    MAX_ACTUAL_AIRING_QUERY_WINDOW_MS, MAX_ACTUAL_AIRING_RECEIVE_DELAY_MS, MAX_ACTUAL_AIRINGS,
    ScheduledTakeAcknowledgement, ScheduledTakeAcknowledgementBinding, ScheduledTakeApplyError,
    ScheduledTakeApplyOutcome,
};
mod catalog_candidate_projection;
pub use catalog_candidate_projection::CatalogCandidateProjection;
mod materialization_evidence;
pub use materialization_evidence::MaterializationEvidenceSnapshot;
mod schedule_materialization;
pub use schedule_materialization::{
    AutomaticMaterializationChannelCheckpoint, ExistingScheduleSnapshot,
    MAX_SCHEDULE_MATERIALIZATION_HISTORY_PAGE_SIZE, ScheduleMaterializationApplyOutcome,
    ScheduleMaterializationFailureClass, ScheduleMaterializationGeneratedDraft,
    ScheduleMaterializationHistoryCursor, ScheduleMaterializationHistoryEntry,
    ScheduleMaterializationHistoryPage, ScheduleMaterializationHistoryReadError,
    ScheduleMaterializationOutcome, ScheduleMaterializationOutcomeRecord,
    ScheduleMaterializationPlanRecord, ScheduleMaterializationRun,
    ScheduleMaterializationRunOrigin, ScheduleMaterializationSourceSnapshot,
    ScheduleMaterializationWriteError,
};

const SQLITE_OK: c_int = 0;
const SQLITE_ROW: c_int = 100;
const SQLITE_DONE: c_int = 101;
const SQLITE_OPEN_READWRITE: c_int = 0x0000_0002;
const SQLITE_OPEN_CREATE: c_int = 0x0000_0004;
const SQLITE_OPEN_FULLMUTEX: c_int = 0x0001_0000;
const CG_IMAGE_VERIFICATION_CACHE_LIMIT: usize = 256;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
const OUTPUT_LOUDNESS_MAX_CAPTURE_BYTES: u64 = 1024 * 1024 * 1024;
const OUTPUT_LOUDNESS_MIN_DURATION_MS: u64 = 3_000;
const OUTPUT_LOUDNESS_MAX_DURATION_MS: u64 = 600_000;
const OUTPUT_LOUDNESS_MAX_TARGET_GROUP_TEXT: usize = 64;
const OUTPUT_LOUDNESS_MAX_CANDIDATE_TEXT: usize = 200;
const MAX_CUSTOM_METADATA_SCHEMAS: usize = 64;
const MAX_CUSTOM_METADATA_RECORDS_PER_ASSET: usize = 128;
const MAX_CUSTOM_METADATA_HISTORY: usize = 128;
static CG_IMAGE_TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(1);

struct LegacyCommitReportRevisionBinding {
    report_id: String,
    channel_id: String,
    schedule_item_id: String,
    asset_id: String,
    approved_by: String,
    approved_at_unix_ms: i64,
}

#[repr(C)]
struct Sqlite3 {
    _private: [u8; 0],
}

#[repr(C)]
struct SqliteStatement {
    _private: [u8; 0],
}

#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut Sqlite3,
        flags: c_int,
        virtual_file_system: *const c_char,
    ) -> c_int;
    fn sqlite3_close(database: *mut Sqlite3) -> c_int;
    fn sqlite3_exec(
        database: *mut Sqlite3,
        sql: *const c_char,
        callback: *mut c_void,
        callback_argument: *mut c_void,
        error_message: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_prepare_v2(
        database: *mut Sqlite3,
        sql: *const c_char,
        byte_count: c_int,
        statement: *mut *mut SqliteStatement,
        tail: *mut *const c_char,
    ) -> c_int;
    fn sqlite3_bind_text(
        statement: *mut SqliteStatement,
        index: c_int,
        value: *const c_char,
        byte_count: c_int,
        destructor: *mut c_void,
    ) -> c_int;
    fn sqlite3_bind_int64(
        statement: *mut SqliteStatement,
        index: c_int,
        value: c_longlong,
    ) -> c_int;
    fn sqlite3_bind_blob(
        statement: *mut SqliteStatement,
        index: c_int,
        value: *const c_void,
        byte_count: c_int,
        destructor: *mut c_void,
    ) -> c_int;
    fn sqlite3_reset(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_clear_bindings(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_step(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_column_text(statement: *mut SqliteStatement, column: c_int) -> *const u8;
    fn sqlite3_column_blob(statement: *mut SqliteStatement, column: c_int) -> *const c_void;
    fn sqlite3_column_bytes(statement: *mut SqliteStatement, column: c_int) -> c_int;
    fn sqlite3_column_int64(statement: *mut SqliteStatement, column: c_int) -> c_longlong;
    fn sqlite3_finalize(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_errmsg(database: *mut Sqlite3) -> *const c_char;
    fn sqlite3_get_autocommit(database: *mut Sqlite3) -> c_int;
}

#[repr(C)]
struct DataBlob {
    byte_count: u32,
    data: *mut u8,
}

#[link(name = "crypt32")]
unsafe extern "system" {
    fn CryptProtectData(
        input: *mut DataBlob,
        description: *const u16,
        entropy: *mut DataBlob,
        reserved: *mut c_void,
        prompt: *mut c_void,
        flags: u32,
        output: *mut DataBlob,
    ) -> i32;
    fn CryptUnprotectData(
        input: *mut DataBlob,
        description: *mut *mut u16,
        entropy: *mut DataBlob,
        reserved: *mut c_void,
        prompt: *mut c_void,
        flags: u32,
        output: *mut DataBlob,
    ) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
    fn GetLastError() -> u32;
}

const CRYPTPROTECT_UI_FORBIDDEN: u32 = 0x1;
const CRYPTPROTECT_LOCAL_MACHINE: u32 = 0x4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProfileWriteError {
    Conflict(u64),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AssetMetadataWriteError {
    Invalid(String),
    SchemaNotFound,
    StaleSchema {
        requested_revision: u64,
        current_revision: u64,
    },
    Conflict(String),
    Rebound,
    DuplicateBinding,
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CustomMetadataSchemaWriteError {
    Invalid(String),
    Conflict(String),
    Rebound,
    Capacity,
    Storage(String),
}

/// A compare-and-swap rejection for the persisted Reference Station target.
/// `None` means the singleton was absent at the time the write was attempted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReferenceStationTierWriteError {
    Conflict { current_revision: Option<u64> },
    CatalogVersionMismatch { expected: u32, actual: u32 },
    Invalid(String),
    Storage(String),
}

/// A complete, auditable change request. `expected_revision = None` is the
/// first-write CAS value; subsequent writes must name the exact revision they
/// replace.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReferenceStationTierWrite {
    pub expected_revision: Option<u64>,
    pub catalog_version: u32,
    pub tier: ReferenceStationTier,
    pub actor: String,
    pub occurred_at_unix_ms: i64,
}

impl ReferenceStationTierWrite {
    fn validate(&self) -> Result<(), String> {
        if self.occurred_at_unix_ms < 0
            || self.actor.is_empty()
            || self.actor.len() > 200
            || self.actor.trim() != self.actor
            || self.actor.chars().any(char::is_control)
        {
            return Err("reference station tier write facts are invalid".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitWriteError {
    NotFound,
    GateFailed(Box<CommitPlan>),
    Invalid(&'static str),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommissioningSupportBundleReceipt {
    pub bundle_id: String,
    pub report_id: String,
    pub archive_sha256: String,
    pub archive_size_bytes: u64,
    pub created_at_unix_ms: i64,
    pub created_by: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReceiverAcceptanceOutcome {
    Accepted,
    Rejected,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReceiverEvidenceMethod {
    HeadendOperatorConfirmation,
    ReceiverTelemetry,
    ReturnPathObservation,
    VisualAudioObservation,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExternalReceiverAcceptanceReceipt {
    pub receipt_id: String,
    pub channel_id: String,
    pub sink_id: String,
    pub outcome: ReceiverAcceptanceOutcome,
    pub method: ReceiverEvidenceMethod,
    pub receiver_label: String,
    pub evidence_reference: String,
    pub detail: String,
    pub observed_at_unix_ms: i64,
    pub recorded_at_unix_ms: i64,
    pub recorded_by: String,
    pub candidate_version: String,
    pub source_commit: String,
    pub configuration_hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct TransportProofHistory {
    pub target: Option<TargetDeviceConfig>,
    pub analysis: Vec<TransportAnalysisReceipt>,
    pub reachability: Vec<TargetDeviceReachabilityReceipt>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct CableTransportValidationSnapshot {
    pub channel_id: String,
    pub sink_id: String,
    pub validation: Option<CableTransportValidation>,
}

impl ExternalReceiverAcceptanceReceipt {
    fn validate(&self) -> Result<(), String> {
        const MAX_OBSERVATION_DELAY_MS: i64 = 7 * 24 * 60 * 60 * 1_000;
        if !is_canonical_uuid(&self.receipt_id)
            || !is_canonical_uuid(&self.channel_id)
            || !is_canonical_uuid(&self.sink_id)
            || !secret_field(&self.receiver_label, 200)
            || !secret_field(&self.evidence_reference, 500)
            || !secret_field(&self.detail, 2_000)
            || self.observed_at_unix_ms < 0
            || self.recorded_at_unix_ms < self.observed_at_unix_ms
            || self.recorded_at_unix_ms - self.observed_at_unix_ms > MAX_OBSERVATION_DELAY_MS
            || !secret_field(&self.recorded_by, 200)
            || !secret_field(&self.candidate_version, 200)
            || !is_source_commit_identity(&self.source_commit)
            || !is_sha256_identity(&self.configuration_hash)
        {
            return Err(
                "external receiver receipt facts are invalid, missing, or unbounded".into(),
            );
        }
        Ok(())
    }
}

impl CommissioningSupportBundleReceipt {
    fn validate(&self) -> Result<(), String> {
        let valid_bundle_id = self.bundle_id.starts_with("support-bundle-")
            && (18..=120).contains(&self.bundle_id.len())
            && self
                .bundle_id
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-');
        if !valid_bundle_id
            || self.report_id.is_empty()
            || self.report_id.len() > 200
            || self.report_id.trim() != self.report_id
            || self.report_id.chars().any(char::is_control)
            || !is_sha256_identity(&self.archive_sha256)
            || self.archive_size_bytes == 0
            || self.archive_size_bytes > 16 * 1024 * 1024
            || self.created_at_unix_ms < 0
            || !secret_field(&self.created_by, 200)
        {
            return Err(
                "commissioning support-bundle receipt facts are invalid or unbounded".into(),
            );
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CgWriteError {
    NotFound,
    Conflict(u64),
    Invalid(&'static str),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FallbackSlateWriteError {
    NotFound,
    MustBeStopped,
    ImageNotReady,
    Conflict(u64),
    Invalid(String),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelFallbackSlate {
    pub channel_id: String,
    pub image_asset: CgImageAsset,
    pub revision: u64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub updated_by: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelFallbackSlateAuditEvent {
    pub audit_id: String,
    pub channel_id: String,
    pub image_asset_id: String,
    pub revision: u64,
    pub actor: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChannelCaptionMode {
    Disabled,
    Cea708,
}

impl ChannelCaptionMode {
    fn storage_name(self) -> &'static str {
        match self {
            Self::Disabled => "disabled",
            Self::Cea708 => "cea708",
        }
    }

    fn from_storage(value: &str) -> Result<Self, String> {
        match value {
            "disabled" => Ok(Self::Disabled),
            "cea708" => Ok(Self::Cea708),
            _ => Err("database returned an invalid caption mode".into()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CaptionPolicyWriteError {
    NotFound,
    MustBeStopped,
    Conflict(u64),
    Invalid(String),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelCaptionPolicy {
    pub channel_id: String,
    pub mode: ChannelCaptionMode,
    pub service_number: u8,
    pub language: String,
    pub revision: u64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub updated_by: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelCaptionPolicyAuditEvent {
    pub audit_id: String,
    pub channel_id: String,
    pub mode: ChannelCaptionMode,
    pub service_number: u8,
    pub language: String,
    pub revision: u64,
    pub actor: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HeadendProfileWriteError {
    NotFound,
    MustBeStopped,
    Invalid(String),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChannelWriteError {
    Conflict,
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CgSnapshotReadError {
    InvalidChannel,
    InvalidTimestamp,
    ChannelNotFound,
    BoardNotFound,
    BoardInactive,
    InvalidData(String),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlertChannelSecret {
    pub target: String,
    pub endpoint: Option<String>,
    pub sender: Option<String>,
    pub identity: Option<String>,
    pub credential: String,
}

impl AlertChannelSecret {
    pub fn validate(&self, kind: AlertChannelKind) -> Result<(), String> {
        validate_alert_channel_target(kind, &self.target).map_err(str::to_string)?;
        if !secret_field(&self.credential, 2_000) {
            return Err("alert destination credential is invalid".into());
        }
        match kind {
            AlertChannelKind::Webhook => {
                if self.endpoint.is_some() || self.sender.is_some() || self.identity.is_some() {
                    return Err("webhook destinations do not accept mail or SMS fields".into());
                }
            }
            AlertChannelKind::Email => {
                let endpoint = self
                    .endpoint
                    .as_deref()
                    .ok_or("email destinations require an SMTP endpoint")?;
                if !valid_smtp_endpoint(endpoint)
                    || !self.sender.as_deref().is_some_and(|value| {
                        validate_alert_channel_target(AlertChannelKind::Email, value).is_ok()
                    })
                    || !self
                        .identity
                        .as_deref()
                        .is_some_and(|value| secret_field(value, 500))
                {
                    return Err("email destination delivery fields are invalid".into());
                }
            }
            AlertChannelKind::Sms => {
                let endpoint = self
                    .endpoint
                    .as_deref()
                    .ok_or("SMS destinations require a provider endpoint")?;
                if validate_secure_url(endpoint, "SMS provider").is_err()
                    || !self.sender.as_deref().is_some_and(|value| {
                        validate_alert_channel_target(AlertChannelKind::Sms, value).is_ok()
                    })
                    || !self
                        .identity
                        .as_deref()
                        .is_some_and(|value| secret_field(value, 500))
                {
                    return Err("SMS destination delivery fields are invalid".into());
                }
            }
        }
        Ok(())
    }
}

pub struct SrtOutputSecret {
    passphrase: String,
}

impl SrtOutputSecret {
    pub fn expose(&self) -> &str {
        &self.passphrase
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredSinkLoudnessProof {
    pub proof: SinkLoudnessProof,
    pub artifact_relative_path: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ChannelLoudnessProofSnapshot {
    pub channel_id: String,
    pub output_sinks: Vec<OutputSinkConfiguration>,
    pub configuration_hash: String,
    pub latest_proofs: Vec<StoredSinkLoudnessProof>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputLoudnessProofJobState {
    Pending,
    Claimed,
    CapturePending,
    Analyzing,
    Completed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputLoudnessCaptureFacts {
    pub artifact_relative_path: String,
    pub capture_bytes: u64,
    pub sample_duration_ms: u64,
    pub capture_sha256: String,
}

impl OutputLoudnessCaptureFacts {
    pub fn validate(&self, max_capture_bytes: u64, max_duration_ms: u64) -> Result<(), String> {
        if max_capture_bytes == 0
            || max_capture_bytes > OUTPUT_LOUDNESS_MAX_CAPTURE_BYTES
            || !(OUTPUT_LOUDNESS_MIN_DURATION_MS..=OUTPUT_LOUDNESS_MAX_DURATION_MS)
                .contains(&max_duration_ms)
        {
            return Err("output loudness job bounds are invalid".into());
        }
        validate_artifact_relative_path(&self.artifact_relative_path)?;
        if self.capture_bytes == 0
            || self.capture_bytes > max_capture_bytes
            || self.sample_duration_ms < OUTPUT_LOUDNESS_MIN_DURATION_MS
            || self.sample_duration_ms > max_duration_ms
            || !is_sha256_identity(&self.capture_sha256)
        {
            return Err(
                "output loudness capture facts are invalid or exceed the job bounds".into(),
            );
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputLoudnessProofJob {
    pub job_id: String,
    pub channel_id: String,
    pub target_group: String,
    pub sink_id: String,
    pub target_lufs_milli: i32,
    pub candidate_version: String,
    pub source_commit: Option<String>,
    pub configuration_hash: String,
    pub sample_duration_ms: u64,
    pub max_capture_bytes: u64,
    pub created_at_unix_ms: i64,
}

impl OutputLoudnessProofJob {
    pub fn validate(&self) -> Result<(), String> {
        for (value, field) in [
            (&self.job_id, "output loudness job identity"),
            (&self.channel_id, "output loudness channel identity"),
            (&self.sink_id, "output loudness sink identity"),
        ] {
            if !is_canonical_uuid(value) {
                return Err(format!("{field} must be a canonical UUID"));
            }
        }
        if self.target_group.is_empty()
            || self.target_group.len() > OUTPUT_LOUDNESS_MAX_TARGET_GROUP_TEXT
            || !self.target_group.bytes().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
            })
        {
            return Err("output loudness target group is invalid or unbounded".into());
        }
        let expected_target_group = match self.target_lufs_milli {
            -24_000 => "cable",
            -16_000 => "streaming",
            _ => return Err("output loudness target must be exactly -24 or -16 LUFS".into()),
        };
        if self.target_group != expected_target_group {
            return Err("output loudness target group does not match its exact target".into());
        }
        if !secret_field(&self.candidate_version, OUTPUT_LOUDNESS_MAX_CANDIDATE_TEXT)
            || !self
                .source_commit
                .as_deref()
                .is_some_and(is_source_commit_identity)
            || !is_sha256_identity(&self.configuration_hash)
            || self.created_at_unix_ms < 0
            || self.sample_duration_ms < OUTPUT_LOUDNESS_MIN_DURATION_MS
            || self.sample_duration_ms > OUTPUT_LOUDNESS_MAX_DURATION_MS
            || self.max_capture_bytes == 0
            || self.max_capture_bytes > OUTPUT_LOUDNESS_MAX_CAPTURE_BYTES
        {
            return Err("output loudness job facts are invalid or unbounded".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputLoudnessProofJobRecord {
    pub job_id: String,
    pub channel_id: String,
    pub target_group: String,
    pub sink_id: String,
    pub target_lufs_milli: i32,
    pub candidate_version: String,
    pub source_commit: Option<String>,
    pub configuration_hash: String,
    pub sample_duration_ms: u64,
    pub max_capture_bytes: u64,
    pub state: OutputLoudnessProofJobState,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub lease_expires_at_unix_ms: Option<i64>,
    pub capture: Option<OutputLoudnessCaptureFacts>,
    pub completed_at_unix_ms: Option<i64>,
    pub failed_at_unix_ms: Option<i64>,
    pub failure_detail: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorLoginAuthority {
    pub account: OperatorAccount,
    pub failed_attempts: u32,
    pub first_failure_at_unix_ms: Option<i64>,
    pub locked_until_unix_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorBootstrapInstallation {
    pub account: OperatorAccount,
    pub session: OperatorSession,
    pub recovery_digests: Vec<SecretDigest>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorSessionAuthority {
    pub account: OperatorAccount,
    pub session: OperatorSession,
}

pub struct StationStore {
    database: *mut Sqlite3,
    _data_root: PathBuf,
    cg_image_root: PathBuf,
    cg_image_verification_cache: Mutex<HashMap<String, CachedCgImageVerification>>,
    cg_image_verification_count: AtomicU64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomationAdapterDescriptorHistory {
    pub descriptor: AutomationAdapterDescriptor,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomationFeaturePolicyHistory {
    pub policy: AutomationFeaturePolicy,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct CustomMetadataSchemaHistory {
    pub schema: MetadataSchema,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct AssetMetadataRecordHistory {
    pub record: AssetMetadataRecord,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduledRecordingHistoryEntry {
    pub recording: ScheduledRecording,
    pub receipt: Option<RecordingTransitionReceipt>,
    pub actor: RecordingActor,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleItemWrite {
    pub item: ScheduleItem,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleItemHistoryEntry {
    pub item: ScheduleItem,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

struct CachedCgImageVerification {
    byte_len: u64,
    modified: Option<SystemTime>,
    checked_at: Instant,
    authority: CgImageAsset,
    asset: CgImageAsset,
}

impl StationStore {
    pub fn open(database_path: impl AsRef<Path>) -> Result<Self, String> {
        let database_path = database_path.as_ref();
        let data_root = station_data_root(database_path)?;
        fs::create_dir_all(&data_root).map_err(|error| format!("create data root: {error}"))?;
        let data_root = fs::canonicalize(&data_root)
            .map_err(|error| format!("canonicalize data root: {error}"))?;
        let cg_image_root = data_root.join("cg-images");
        fs::create_dir_all(&cg_image_root)
            .map_err(|error| format!("create CG image root: {error}"))?;
        let cg_image_root = fs::canonicalize(&cg_image_root)
            .map_err(|error| format!("canonicalize CG image root: {error}"))?;
        if cg_image_root.parent() != Some(data_root.as_path()) {
            return Err("CG image root escapes station data root".into());
        }
        let filename = CString::new(database_path.to_string_lossy().as_bytes())
            .map_err(|_| "database path contains a null byte".to_string())?;
        let mut database = ptr::null_mut();
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX;
        // SAFETY: `filename` is a valid C string, `database` points to writable storage, and the
        // returned handle is owned by `StationStore` until `Drop` closes it.
        let status =
            unsafe { sqlite3_open_v2(filename.as_ptr(), &mut database, flags, ptr::null()) };
        if status != SQLITE_OK {
            let message = sqlite_error(database);
            if !database.is_null() {
                // SAFETY: SQLite returned this handle and it has not been closed.
                unsafe { sqlite3_close(database) };
            }
            return Err(message);
        }

        let store = Self {
            database,
            _data_root: data_root,
            cg_image_root,
            cg_image_verification_cache: Mutex::new(HashMap::new()),
            cg_image_verification_count: AtomicU64::new(0),
        };
        store.execute("PRAGMA busy_timeout=5000;")?;
        store.execute(
            "PRAGMA journal_mode=WAL;\
             PRAGMA foreign_keys=ON;\
             CREATE TABLE IF NOT EXISTS station_profile (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               station_id TEXT NOT NULL,\
               display_name TEXT NOT NULL,\
               timezone TEXT NOT NULL,\
               revision INTEGER NOT NULL DEFAULT 1\
             );\
             CREATE TABLE IF NOT EXISTS media_assets (\
               asset_id TEXT PRIMARY KEY,\
               media_path TEXT NOT NULL,\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               readiness TEXT NOT NULL CHECK (readiness IN ('ready','missing','rejected','processing'))\
             );\
             CREATE TABLE IF NOT EXISTS asset_loudness_analysis (\
               asset_id TEXT PRIMARY KEY,\
               status TEXT NOT NULL CHECK (status IN ('ok','failed','not_checked')),\
               measured_lufs_milli INTEGER NOT NULL DEFAULT -2147483648,\
               target_lufs_milli INTEGER NOT NULL,\
               detail TEXT NOT NULL DEFAULT '',\
               FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS media_ingest_jobs (\
               job_id TEXT PRIMARY KEY,\
               source_kind TEXT NOT NULL CHECK (source_kind IN ('operator_path','watch_folder','live_finalization')),\
               source_path TEXT NOT NULL,\
               status TEXT NOT NULL CHECK (status IN ('pending','running','awaiting_transcode','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               error_detail TEXT NOT NULL DEFAULT '',\
               asset_id TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             CREATE INDEX IF NOT EXISTS media_ingest_jobs_status_time \
               ON media_ingest_jobs(status, created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS transcode_jobs (\
               job_id TEXT PRIMARY KEY,\
               ingest_job_id TEXT NOT NULL UNIQUE,\
               source_asset_id TEXT NOT NULL,\
               source_path TEXT NOT NULL,\
               output_format TEXT NOT NULL CHECK (output_format IN ('h264_720p4mbps_aac192k_ts')),\
               status TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               output_asset_id TEXT NOT NULL DEFAULT '',\
               error_detail TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             CREATE INDEX IF NOT EXISTS transcode_jobs_status_time \
               ON transcode_jobs(status, next_attempt_at_unix_ms, created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS watch_folder_configs (\
               config_id TEXT PRIMARY KEY,\
               display_name TEXT NOT NULL,\
               monitor_path TEXT NOT NULL,\
               extensions_json TEXT NOT NULL,\
               settle_seconds INTEGER NOT NULL CHECK (settle_seconds BETWEEN 1 AND 3600),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               last_scan_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (last_scan_at_unix_ms >= -1),\
               last_error TEXT NOT NULL DEFAULT ''\
             );\
             CREATE TABLE IF NOT EXISTS watch_file_observations (\
               config_id TEXT NOT NULL,\
               source_path TEXT NOT NULL,\
               size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),\
               modified_at_unix_ms INTEGER NOT NULL CHECK (modified_at_unix_ms >= 0),\
               unchanged_since_unix_ms INTEGER NOT NULL CHECK (unchanged_since_unix_ms >= 0),\
               observation_count INTEGER NOT NULL CHECK (observation_count >= 1),\
               queued_job_id TEXT NOT NULL DEFAULT '',\
               PRIMARY KEY(config_id, source_path),\
               FOREIGN KEY(config_id) REFERENCES watch_folder_configs(config_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS channels (\
               channel_id TEXT PRIMARY KEY,\
               display_name TEXT NOT NULL,\
               udp_destination TEXT NOT NULL,\
               output_profile TEXT NOT NULL DEFAULT 'proof-360p30',\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1))\
             );\
             CREATE TABLE IF NOT EXISTS channel_runtime (\
               channel_id TEXT PRIMARY KEY,\
               state TEXT NOT NULL CHECK (state IN ('starting','healthy_fallback','on_air','live','error','stopped')),\
               on_air_asset_id TEXT NOT NULL DEFAULT '',\
               state_started_at_unix_ms INTEGER NOT NULL CHECK (state_started_at_unix_ms >= 0),\
               heartbeat_at_unix_ms INTEGER NOT NULL CHECK (heartbeat_at_unix_ms >= state_started_at_unix_ms),\
               detail TEXT NOT NULL DEFAULT '',\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS channel_output_sinks (\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               sink_order INTEGER NOT NULL CHECK (sink_order >= 0),\
               label TEXT NOT NULL,\
               kind TEXT NOT NULL CHECK (kind IN ('udp-ts','srt')),\
               destination TEXT NOT NULL,\
               loudness_target_milli INTEGER NOT NULL,\
               required INTEGER NOT NULL CHECK (required IN (0,1)),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               headend_profile TEXT NOT NULL DEFAULT '',\
               headend_muxrate_kbps INTEGER NOT NULL DEFAULT 0 CHECK (headend_muxrate_kbps >= 0),\
               headend_packet_size INTEGER NOT NULL DEFAULT 0 CHECK (headend_packet_size >= 0),\
               PRIMARY KEY(channel_id, sink_id),\
               UNIQUE(channel_id, label COLLATE NOCASE),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS alert_events (\
               event_id TEXT PRIMARY KEY,\
               condition TEXT NOT NULL,\
               severity TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),\
               state TEXT NOT NULL CHECK (state IN ('firing','resolved')),\
               resource_ref TEXT NOT NULL,\
               summary TEXT NOT NULL,\
               detail TEXT NOT NULL DEFAULT '',\
               source_section TEXT NOT NULL,\
               first_observed_at_unix_ms INTEGER NOT NULL CHECK (first_observed_at_unix_ms >= 0),\
               last_observed_at_unix_ms INTEGER NOT NULL CHECK (last_observed_at_unix_ms >= first_observed_at_unix_ms),\
               resolved_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (resolved_at_unix_ms >= -1),\
               occurrence_count INTEGER NOT NULL CHECK (occurrence_count >= 1),\
               acknowledged_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (acknowledged_at_unix_ms >= -1),\
               acknowledged_by TEXT NOT NULL DEFAULT ''\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS alert_events_one_firing_condition_resource \
               ON alert_events(condition, resource_ref) WHERE state='firing';\
             CREATE INDEX IF NOT EXISTS alert_events_state_severity_time \
               ON alert_events(state, severity, last_observed_at_unix_ms DESC);\
             CREATE TABLE IF NOT EXISTS alert_rules (\
               rule_id TEXT PRIMARY KEY,\
               condition TEXT NOT NULL UNIQUE,\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               severity TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),\
               channel_ids_json TEXT NOT NULL,\
               dedupe_window_seconds INTEGER NOT NULL CHECK (dedupe_window_seconds BETWEEN 0 AND 86400),\
               re_alert_after_seconds INTEGER NOT NULL CHECK (re_alert_after_seconds BETWEEN 0 AND 604800),\
               scope_channel_id TEXT NOT NULL DEFAULT '',\
               notify_on_resolve INTEGER NOT NULL CHECK (notify_on_resolve IN (0,1)),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= 0),\
               updated_by TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS system_resource_samples (\
               sample_id INTEGER PRIMARY KEY AUTOINCREMENT,\
               sampled_at_unix_ms INTEGER NOT NULL CHECK (sampled_at_unix_ms >= 0),\
               cpu_milli_percent INTEGER NOT NULL DEFAULT -1,\
               ram_used_milli_gb INTEGER NOT NULL DEFAULT -1,\
               ram_total_milli_gb INTEGER NOT NULL DEFAULT -1,\
               gpu_milli_percent INTEGER NOT NULL DEFAULT -1,\
               gpu_vram_used_milli_gb INTEGER NOT NULL DEFAULT -1,\
               media_volume_free_milli_gb INTEGER NOT NULL DEFAULT -1,\
               backup_volume_free_milli_gb INTEGER NOT NULL DEFAULT -1,\
               database_reachable INTEGER NOT NULL CHECK (database_reachable IN (0,1)),\
               backup_volume_writable INTEGER NOT NULL CHECK (backup_volume_writable IN (0,1)),\
               service_running INTEGER NOT NULL CHECK (service_running IN (0,1)),\
               clock_skew_milliseconds INTEGER NOT NULL DEFAULT -9223372036854775808\
             );\
             CREATE INDEX IF NOT EXISTS system_resource_samples_time \
               ON system_resource_samples(sampled_at_unix_ms DESC, sample_id DESC);\
             CREATE TABLE IF NOT EXISTS self_test_schedules (\
               kind TEXT PRIMARY KEY CHECK (kind IN ('daily','weekly')),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               local_hour INTEGER NOT NULL CHECK (local_hour BETWEEN 0 AND 23),\
               local_minute INTEGER NOT NULL CHECK (local_minute BETWEEN 0 AND 59),\
               weekday INTEGER NOT NULL DEFAULT -1 CHECK (weekday BETWEEN -1 AND 6),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= 0),\
               updated_by TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS system_self_tests (\
               self_test_id TEXT PRIMARY KEY,\
               kind TEXT NOT NULL CHECK (kind IN ('daily','weekly')),\
               trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('scheduled','manual')),\
               schedule_period_key TEXT NOT NULL DEFAULT '',\
               started_at_unix_ms INTEGER NOT NULL CHECK (started_at_unix_ms >= 0),\
               finished_at_unix_ms INTEGER NOT NULL CHECK (finished_at_unix_ms >= started_at_unix_ms),\
               status TEXT NOT NULL CHECK (status IN ('pass','warn','fail')),\
               checks_json TEXT NOT NULL,\
               summary TEXT NOT NULL,\
               evidence_path TEXT NOT NULL DEFAULT ''\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS system_self_tests_one_scheduled_period \
               ON system_self_tests(kind, schedule_period_key) \
               WHERE trigger_kind='scheduled';\
             CREATE INDEX IF NOT EXISTS system_self_tests_time \
               ON system_self_tests(started_at_unix_ms DESC, self_test_id DESC);\
             CREATE TABLE IF NOT EXISTS update_check_status (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               checked_at_unix_ms INTEGER NOT NULL CHECK (checked_at_unix_ms >= 0),\
               next_automatic_check_at_unix_ms INTEGER NOT NULL \
                 CHECK (next_automatic_check_at_unix_ms > checked_at_unix_ms),\
               trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('automatic','manual')),\
               outcome TEXT NOT NULL CHECK (outcome IN ('up_to_date','update_available','updater_too_old','failed')),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS update_stage_status (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               staged_at_unix_ms INTEGER NOT NULL CHECK (staged_at_unix_ms >= 0),\
               version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS update_prepare_status (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               prepared_at_unix_ms INTEGER NOT NULL CHECK (prepared_at_unix_ms >= 0),\
               version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS alert_channels (\
               channel_id TEXT PRIMARY KEY,\
               kind TEXT NOT NULL CHECK (kind IN ('email','sms','webhook')),\
               label TEXT NOT NULL,\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               target_redacted TEXT NOT NULL,\
               credential_configured INTEGER NOT NULL CHECK (credential_configured IN (0,1)),\
               quiet_hours_start_utc TEXT NOT NULL DEFAULT '',\
               quiet_hours_end_utc TEXT NOT NULL DEFAULT '',\
               encrypted_configuration BLOB NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms)\
             );\
             CREATE TABLE IF NOT EXISTS alert_deliveries (\
               delivery_id TEXT PRIMARY KEY,\
               event_id TEXT NOT NULL,\
               alert_channel_id TEXT NOT NULL,\
               notification_kind TEXT NOT NULL CHECK (notification_kind IN ('firing','realert','resolved','test')),\
               state TEXT NOT NULL CHECK (state IN ('pending','leased','sent','dead_letter')),\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL CHECK (next_attempt_at_unix_ms >= 0),\
               lease_expires_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (lease_expires_at_unix_ms >= -1),\
               last_error TEXT NOT NULL DEFAULT '',\
               signature TEXT NOT NULL DEFAULT '',\
               payload_json TEXT NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               sent_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (sent_at_unix_ms >= -1),\
               FOREIGN KEY(event_id) REFERENCES alert_events(event_id),\
               FOREIGN KEY(alert_channel_id) REFERENCES alert_channels(channel_id)\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS alert_deliveries_one_initial_kind \
               ON alert_deliveries(event_id, alert_channel_id, notification_kind) \
               WHERE notification_kind IN ('firing','resolved','test');\
             CREATE INDEX IF NOT EXISTS alert_deliveries_due \
               ON alert_deliveries(state, next_attempt_at_unix_ms, created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS schedule_items (\
               item_id TEXT PRIMARY KEY,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               channel_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               title TEXT NOT NULL,\
               starts_at_unix_ms INTEGER NOT NULL CHECK (starts_at_unix_ms >= 0),\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               state TEXT NOT NULL CHECK (state IN ('draft','committed','cancelled'))\
             );\
             CREATE INDEX IF NOT EXISTS schedule_items_channel_time \
               ON schedule_items(channel_id, starts_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS commit_reports (\
               report_id TEXT PRIMARY KEY,\
               plan_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               schedule_item_id TEXT NOT NULL,\
               schedule_item_revision INTEGER NOT NULL CHECK (schedule_item_revision >= 1),\
               asset_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL,\
               dispatch_status TEXT NOT NULL CHECK (dispatch_status IN ('pending','queued','acknowledged','missed','completed','error','cancelled')),\
               operator_notes TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS commit_reports_channel_time \
               ON commit_reports(channel_id, approved_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS live_sources (\
               source_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               label TEXT NOT NULL,\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               endpoint_kind TEXT NOT NULL CHECK (endpoint_kind IN ('udp_mpeg_ts')),\
               bind_ip TEXT NOT NULL,\
               port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id)\
             );\
             CREATE TABLE IF NOT EXISTS takeover_sessions (\
               session_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               source_id TEXT NOT NULL,\
               operator_id TEXT NOT NULL,\
               reason TEXT,\
               requested_at_unix_ms INTEGER NOT NULL CHECK (requested_at_unix_ms >= 0),\
               taken_at_unix_ms INTEGER,\
               returned_at_unix_ms INTEGER,\
               source_revision INTEGER NOT NULL CHECK (source_revision >= 1),\
               authority_revision INTEGER NOT NULL CHECK (authority_revision >= 1),\
               expires_at_unix_ms INTEGER NOT NULL CHECK (expires_at_unix_ms > requested_at_unix_ms),\
               state TEXT NOT NULL CHECK (state IN ('arming','armed','take_requested','on_air','return_requested','returning','completed','failed')),\
               failure_detail TEXT,\
               FOREIGN KEY(source_id) REFERENCES live_sources(source_id)\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS takeover_sessions_one_active_channel \
               ON takeover_sessions(channel_id)\
               WHERE state IN ('arming','armed','take_requested','on_air','return_requested','returning');\
             CREATE INDEX IF NOT EXISTS takeover_sessions_channel_time \
               ON takeover_sessions(channel_id, requested_at_unix_ms DESC);\
             CREATE TABLE IF NOT EXISTS takeover_audit_events (\
               audit_id TEXT PRIMARY KEY,\
               session_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               source_id TEXT NOT NULL,\
               operator_id TEXT NOT NULL,\
               state TEXT NOT NULL CHECK (state IN ('arming','armed','take_requested','on_air','return_requested','returning','completed','failed')),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               detail TEXT,\
               FOREIGN KEY(session_id) REFERENCES takeover_sessions(session_id) ON DELETE CASCADE\
             );\
             CREATE INDEX IF NOT EXISTS takeover_audit_session_time \
               ON takeover_audit_events(session_id, occurred_at_unix_ms, audit_id);\
             CREATE TABLE IF NOT EXISTS takeover_authority (\
               channel_id TEXT PRIMARY KEY,\
               revision INTEGER NOT NULL CHECK (revision >= 0)\
             );",
        )?;
        store.migrate_ingest_retry_schedule()?;
        store.migrate_ingest_transcode_status()?;
        store.migrate_commit_report_statuses()?;
        store.migrate_schedule_item_revision()?;
        store.migrate_commit_report_schedule_item_revision()?;
        store.initialize_schedule_item_history_schema()?;
        store.initialize_commissioning_support_bundle_schema()?;
        store.migrate_channel_runtime_live_state()?;
        store.migrate_alert_delivery_realerts()?;
        store.migrate_channel_output_profile()?;
        store.migrate_channel_output_sink_kinds()?;
        store.migrate_headend_sink_contract()?;
        store.migrate_channel_output_sink_order()?;
        store.migrate_channel_output_sinks()?;
        store.initialize_external_receiver_acceptance_schema()?;
        store.initialize_transport_proof_schema()?;
        store.migrate_sink_loudness_proofs()?;
        store.migrate_output_loudness_jobs()?;
        store.initialize_reference_station_tier_schema()?;
        store.initialize_cg_schema()?;
        store.initialize_fallback_slate_schema()?;
        store.initialize_caption_policy_schema()?;
        store.initialize_asset_caption_track_schema()?;
        store.initialize_caption_egress_proof_schema()?;
        store.initialize_caption_egress_job_schema()?;
        store.initialize_automation_policy_schema()?;
        store.initialize_publishing_authority_schema()?;
        store.initialize_custom_metadata_schema()?;
        store.initialize_schedule_automation_schema()?;
        store.initialize_catalog_authority_schema()?;
        store.initialize_actual_airing_authority_schema()?;
        store.initialize_schedule_materialization_schema()?;
        store.initialize_scheduled_recording_schema()?;
        store.migrate_caption_egress_history_sink_foreign_keys()?;
        store.migrate_cg_feed_cache_schema()?;
        store.initialize_operator_auth_schema()?;
        store.execute(
            "CREATE TABLE IF NOT EXISTS srt_output_secrets (\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               encrypted_passphrase BLOB NOT NULL,\
               PRIMARY KEY(channel_id, sink_id),\
               FOREIGN KEY(channel_id, sink_id) REFERENCES channel_output_sinks(channel_id, sink_id) ON DELETE CASCADE\
             );",
        )?;
        store.seed_default_alert_rules()?;
        store.seed_default_self_test_schedules()?;
        Ok(store)
    }

    fn initialize_commissioning_support_bundle_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS commissioning_support_bundle_receipts (\
               bundle_id TEXT PRIMARY KEY,\
               report_id TEXT NOT NULL,\
               archive_sha256 TEXT NOT NULL CHECK (length(archive_sha256) = 64),\
               archive_size_bytes INTEGER NOT NULL CHECK (archive_size_bytes BETWEEN 1 AND 16777216),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               created_by TEXT NOT NULL,\
               FOREIGN KEY(report_id) REFERENCES commit_reports(report_id)\
             );\
             CREATE INDEX IF NOT EXISTS commissioning_support_bundle_receipts_time \
               ON commissioning_support_bundle_receipts(created_at_unix_ms DESC, bundle_id DESC);\
             CREATE TRIGGER IF NOT EXISTS commissioning_support_bundle_receipts_no_update \
               BEFORE UPDATE ON commissioning_support_bundle_receipts \
               BEGIN SELECT RAISE(ABORT, 'commissioning support bundle receipts are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS commissioning_support_bundle_receipts_no_delete \
               BEFORE DELETE ON commissioning_support_bundle_receipts \
               BEGIN SELECT RAISE(ABORT, 'commissioning support bundle receipts are append-only'); END;",
        )
    }

    fn initialize_automation_policy_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS automation_adapter_descriptors (\
               adapter_id TEXT PRIMARY KEY,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS automation_feature_policies (\
               policy_id TEXT PRIMARY KEY,\
               feature TEXT NOT NULL UNIQUE CHECK (feature IN ('captions','summaries','translation')),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS automation_adapter_descriptor_history (\
               adapter_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(adapter_id, revision)\
             );\
             CREATE TABLE IF NOT EXISTS automation_feature_policy_history (\
               policy_id TEXT NOT NULL,\
               feature TEXT NOT NULL CHECK (feature IN ('captions','summaries','translation')),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(policy_id, revision)\
             );\
             CREATE TABLE IF NOT EXISTS automation_connection_test_evidence (\
               evidence_id TEXT PRIMARY KEY,\
               policy_id TEXT NOT NULL,\
               adapter_id TEXT NOT NULL,\
               policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),\
               adapter_revision INTEGER NOT NULL CHECK (adapter_revision >= 1),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL CHECK (length(source_commit)=40),\
               tested_at_unix_ms INTEGER NOT NULL CHECK (tested_at_unix_ms >= 0),\
               expires_at_unix_ms INTEGER NOT NULL CHECK (expires_at_unix_ms > tested_at_unix_ms),\
               record_json TEXT NOT NULL,\
               FOREIGN KEY(policy_id) REFERENCES automation_feature_policies(policy_id),\
               FOREIGN KEY(adapter_id) REFERENCES automation_adapter_descriptors(adapter_id)\
             );\
             CREATE INDEX IF NOT EXISTS automation_connection_test_evidence_current \
               ON automation_connection_test_evidence(policy_id, candidate_version, source_commit, tested_at_unix_ms DESC, evidence_id DESC);\
             CREATE TRIGGER IF NOT EXISTS automation_connection_test_evidence_no_update \
               BEFORE UPDATE ON automation_connection_test_evidence \
               BEGIN SELECT RAISE(ABORT, 'automation connection evidence is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_connection_test_evidence_no_delete \
               BEFORE DELETE ON automation_connection_test_evidence \
               BEGIN SELECT RAISE(ABORT, 'automation connection evidence is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_adapter_descriptor_history_no_update \
               BEFORE UPDATE ON automation_adapter_descriptor_history \
               BEGIN SELECT RAISE(ABORT, 'automation adapter history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_adapter_descriptor_history_no_delete \
               BEFORE DELETE ON automation_adapter_descriptor_history \
               BEGIN SELECT RAISE(ABORT, 'automation adapter history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_feature_policy_history_no_update \
               BEFORE UPDATE ON automation_feature_policy_history \
               BEGIN SELECT RAISE(ABORT, 'automation policy history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_feature_policy_history_no_delete \
               BEFORE DELETE ON automation_feature_policy_history \
               BEGIN SELECT RAISE(ABORT, 'automation policy history is append-only'); END;",
        )
    }

    fn initialize_custom_metadata_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS custom_metadata_schemas (\
               schema_id TEXT PRIMARY KEY,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS custom_metadata_schema_history (\
               schema_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(schema_id, revision)\
             );\
             CREATE TABLE IF NOT EXISTS asset_metadata_records (\
               metadata_id TEXT PRIMARY KEY,\
               asset_id TEXT NOT NULL CHECK (length(asset_id) = 64),\
               schema_id TEXT NOT NULL,\
               schema_revision INTEGER NOT NULL CHECK (schema_revision >= 1),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               UNIQUE(asset_id, schema_id),\
               FOREIGN KEY(schema_id) REFERENCES custom_metadata_schemas(schema_id)\
             );\
             CREATE INDEX IF NOT EXISTS asset_metadata_records_asset \
               ON asset_metadata_records(asset_id, metadata_id);\
             CREATE TABLE IF NOT EXISTS asset_metadata_record_history (\
               metadata_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL CHECK (length(asset_id) = 64),\
               schema_id TEXT NOT NULL,\
               schema_revision INTEGER NOT NULL CHECK (schema_revision >= 1),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(metadata_id, revision)\
             );\
             CREATE INDEX IF NOT EXISTS asset_metadata_record_history_asset \
               ON asset_metadata_record_history(asset_id, metadata_id, revision);\
             CREATE TRIGGER IF NOT EXISTS custom_metadata_schema_history_no_update \
               BEFORE UPDATE ON custom_metadata_schema_history \
               BEGIN SELECT RAISE(ABORT, 'custom metadata schema history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS custom_metadata_schemas_no_delete \
               BEFORE DELETE ON custom_metadata_schemas \
               BEGIN SELECT RAISE(ABORT, 'custom metadata schemas cannot be deleted'); END;\
             CREATE TRIGGER IF NOT EXISTS custom_metadata_schema_history_no_delete \
               BEFORE DELETE ON custom_metadata_schema_history \
               BEGIN SELECT RAISE(ABORT, 'custom metadata schema history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_metadata_record_history_no_update \
               BEFORE UPDATE ON asset_metadata_record_history \
               BEGIN SELECT RAISE(ABORT, 'asset metadata record history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_metadata_records_no_delete \
               BEFORE DELETE ON asset_metadata_records \
               BEGIN SELECT RAISE(ABORT, 'asset metadata records cannot be deleted'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_metadata_record_history_no_delete \
               BEFORE DELETE ON asset_metadata_record_history \
               BEGIN SELECT RAISE(ABORT, 'asset metadata record history is append-only'); END;",
        )
    }

    fn initialize_scheduled_recording_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_recordings (\
               recording_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               schedule_item_id TEXT NOT NULL,\
               schedule_revision INTEGER NOT NULL CHECK (schedule_revision >= 1),\
               scheduled_start_unix_ms INTEGER NOT NULL CHECK (scheduled_start_unix_ms >= 0),\
               scheduled_end_unix_ms INTEGER NOT NULL CHECK (scheduled_end_unix_ms > scheduled_start_unix_ms),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               lifecycle TEXT NOT NULL CHECK (lifecycle IN ('draft','armed','capturing','finalizing','completed','failed','cancelled_before_arm','cancelled_after_arm')),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,\
               channel_configuration_hash TEXT NOT NULL,\
               record_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS scheduled_recordings_channel_window \
               ON scheduled_recordings(channel_id, scheduled_start_unix_ms, recording_id);\
             CREATE INDEX IF NOT EXISTS scheduled_recordings_schedule_item \
               ON scheduled_recordings(schedule_item_id, schedule_revision, recording_id);\
             CREATE TABLE IF NOT EXISTS scheduled_recording_history (\
               recording_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               transition_kind TEXT NOT NULL CHECK (transition_kind IN ('created','arm','start_capture','begin_finalization','recover_for_finalization','finalize_success','fail','cancel')),\
               actor_id TEXT NOT NULL,\
               actor_role TEXT NOT NULL CHECK (actor_role IN ('operator','supervisor','recording_worker')),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,\
               channel_configuration_hash TEXT NOT NULL,\
               receipt_json TEXT NOT NULL,\
               record_json TEXT NOT NULL,\
               PRIMARY KEY(recording_id, revision),\
               FOREIGN KEY(recording_id) REFERENCES scheduled_recordings(recording_id)\
             );\
             CREATE INDEX IF NOT EXISTS scheduled_recording_history_time \
               ON scheduled_recording_history(recording_id, occurred_at_unix_ms, revision);\
             CREATE TRIGGER IF NOT EXISTS scheduled_recording_history_no_update \
               BEFORE UPDATE ON scheduled_recording_history \
               BEGIN SELECT RAISE(ABORT, 'scheduled recording history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS scheduled_recording_history_no_delete \
               BEFORE DELETE ON scheduled_recording_history \
               BEGIN SELECT RAISE(ABORT, 'scheduled recording history is append-only'); END;",
        )
    }

    fn initialize_external_receiver_acceptance_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS external_receiver_acceptance_receipts (\
               receipt_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               outcome TEXT NOT NULL CHECK (outcome IN ('accepted','rejected')),\
               method TEXT NOT NULL CHECK (method IN ('headend_operator_confirmation','receiver_telemetry','return_path_observation','visual_audio_observation')),\
               receiver_label TEXT NOT NULL CHECK (length(receiver_label) BETWEEN 1 AND 200),\
               evidence_reference TEXT NOT NULL CHECK (length(evidence_reference) BETWEEN 1 AND 500),\
               detail TEXT NOT NULL CHECK (length(detail) BETWEEN 1 AND 2000),\
               observed_at_unix_ms INTEGER NOT NULL CHECK (observed_at_unix_ms >= 0),\
               recorded_at_unix_ms INTEGER NOT NULL CHECK (recorded_at_unix_ms >= observed_at_unix_ms),\
               recorded_by TEXT NOT NULL CHECK (length(recorded_by) BETWEEN 1 AND 200),\
               candidate_version TEXT NOT NULL CHECK (length(candidate_version) BETWEEN 1 AND 200),\
               source_commit TEXT NOT NULL CHECK (length(source_commit) = 40),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash) = 64),\
               CHECK (recorded_at_unix_ms - observed_at_unix_ms <= 604800000)\
             );\
             CREATE INDEX IF NOT EXISTS external_receiver_acceptance_current \
               ON external_receiver_acceptance_receipts(channel_id, sink_id, candidate_version, source_commit, configuration_hash, recorded_at_unix_ms DESC, receipt_id DESC);\
             CREATE INDEX IF NOT EXISTS external_receiver_acceptance_history \
               ON external_receiver_acceptance_receipts(channel_id, recorded_at_unix_ms DESC, receipt_id DESC);\
             CREATE TRIGGER IF NOT EXISTS external_receiver_acceptance_no_update \
               BEFORE UPDATE ON external_receiver_acceptance_receipts \
               BEGIN SELECT RAISE(ABORT, 'external receiver receipts are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS external_receiver_acceptance_no_delete \
               BEFORE DELETE ON external_receiver_acceptance_receipts \
               BEGIN SELECT RAISE(ABORT, 'external receiver receipts are append-only'); END;",
        )
    }

    fn initialize_transport_proof_schema(&self) -> Result<(), String> {
        self.migrate_transport_proof_schema()
            .map_err(|error| format!("migrate legacy transport proof schema: {error}"))?;
        self.execute(
            "CREATE TABLE IF NOT EXISTS transport_target_configs (\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               target_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               PRIMARY KEY(channel_id, sink_id, revision)\
             );\
             CREATE TABLE IF NOT EXISTS transport_analysis_receipts (\
               proof_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               target_id TEXT NOT NULL,\
               target_revision INTEGER NOT NULL CHECK (target_revision >= 1),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,\
               configuration_hash TEXT NOT NULL,\
               outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail')),\
               captured_at_unix_ms INTEGER NOT NULL CHECK (captured_at_unix_ms >= 0),\
               recorded_at_unix_ms INTEGER NOT NULL CHECK (recorded_at_unix_ms >= captured_at_unix_ms),\
               record_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS transport_analysis_current \
               ON transport_analysis_receipts(channel_id, sink_id, target_id, target_revision, candidate_version, source_commit, configuration_hash, recorded_at_unix_ms DESC, proof_id DESC);\
             CREATE TABLE IF NOT EXISTS target_device_reachability_receipts (\
               proof_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               target_id TEXT NOT NULL,\
               target_revision INTEGER NOT NULL CHECK (target_revision >= 1),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,\
               configuration_hash TEXT NOT NULL,\
               outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail')),\
               observed_at_unix_ms INTEGER NOT NULL CHECK (observed_at_unix_ms >= 0),\
               recorded_at_unix_ms INTEGER NOT NULL CHECK (recorded_at_unix_ms >= observed_at_unix_ms),\
               record_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS target_device_reachability_current \
               ON target_device_reachability_receipts(channel_id, sink_id, target_id, target_revision, candidate_version, source_commit, configuration_hash, recorded_at_unix_ms DESC, proof_id DESC);\
             CREATE INDEX IF NOT EXISTS transport_analysis_history \
               ON transport_analysis_receipts(channel_id, recorded_at_unix_ms DESC, proof_id DESC);\
             CREATE INDEX IF NOT EXISTS target_device_reachability_history \
               ON target_device_reachability_receipts(channel_id, recorded_at_unix_ms DESC, proof_id DESC);\
             CREATE TRIGGER IF NOT EXISTS transport_target_configs_no_delete \
               BEFORE DELETE ON transport_target_configs \
               BEGIN SELECT RAISE(ABORT, 'transport target configurations are not deletable'); END;\
             CREATE TRIGGER IF NOT EXISTS transport_target_configs_no_update \
               BEFORE UPDATE ON transport_target_configs \
               BEGIN SELECT RAISE(ABORT, 'transport target configurations are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS transport_analysis_no_update \
               BEFORE UPDATE ON transport_analysis_receipts \
               BEGIN SELECT RAISE(ABORT, 'transport analysis receipts are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS transport_analysis_no_delete \
               BEFORE DELETE ON transport_analysis_receipts \
               BEGIN SELECT RAISE(ABORT, 'transport analysis receipts are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS target_device_reachability_no_update \
               BEFORE UPDATE ON target_device_reachability_receipts \
               BEGIN SELECT RAISE(ABORT, 'target device reachability receipts are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS target_device_reachability_no_delete \
               BEFORE DELETE ON target_device_reachability_receipts \
               BEGIN SELECT RAISE(ABORT, 'target device reachability receipts are append-only'); END;",
        )
        .map_err(|error| format!("initialize transport proof schema: {error}"))
    }

    /// Upgrade the short-lived pre-revision transport authority introduced by
    /// 66c85ae.  A receipt from that schema cannot prove which target revision
    /// it observed, so it is retained for audit but deliberately cannot satisfy
    /// the new authority.  Target configuration records, on the other hand,
    /// are converted only when their old endpoint can be represented exactly by
    /// the structured host/port contract.  Unrepresentable (including fake)
    /// records remain unreadable and therefore fail closed without preventing
    /// the database from opening.
    fn migrate_transport_proof_schema(&self) -> Result<(), String> {
        let analysis_exists = self.sqlite_table_exists("transport_analysis_receipts")?;
        let analysis_needs_revision = analysis_exists
            && !self.sqlite_table_has_column("transport_analysis_receipts", "target_revision")?;
        let target_configs_exist = self.sqlite_table_exists("transport_target_configs")?;
        let migrated_configs = if target_configs_exist {
            self.legacy_transport_target_config_rewrites()?
        } else {
            Vec::new()
        };
        if !analysis_needs_revision && migrated_configs.is_empty() {
            return Ok(());
        }

        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if !migrated_configs.is_empty() {
                // The legacy trigger guards normal operation.  This one-time
                // migration replaces only byte-for-byte representable records
                // and restores the trigger below through schema initialization.
                self.execute("DROP TRIGGER IF EXISTS transport_target_configs_no_update")?;
                let statement = self.prepare(
                    "UPDATE transport_target_configs SET record_json=?1 WHERE channel_id=?2 AND sink_id=?3 AND revision=?4",
                )?;
                for (channel_id, sink_id, revision, record_json) in &migrated_configs {
                    bind_text(
                        self.database,
                        statement.raw,
                        1,
                        &c_string(record_json, "migrated transport target record")?,
                    )?;
                    bind_text(
                        self.database,
                        statement.raw,
                        2,
                        &c_string(channel_id, "migrated transport channel")?,
                    )?;
                    bind_text(
                        self.database,
                        statement.raw,
                        3,
                        &c_string(sink_id, "migrated transport sink")?,
                    )?;
                    bind_i64(
                        self.database,
                        statement.raw,
                        4,
                        i64::try_from(*revision).map_err(|_| {
                            "migrated transport target revision exceeds SQLite range".to_string()
                        })?,
                    )?;
                    expect_done(self.database, statement.raw)?;
                    unsafe { sqlite3_reset(statement.raw) };
                    unsafe { sqlite3_clear_bindings(statement.raw) };
                }
                self.execute(
                    "CREATE TRIGGER IF NOT EXISTS transport_target_configs_no_update \
                     BEFORE UPDATE ON transport_target_configs \
                     BEGIN SELECT RAISE(ABORT, 'transport target configurations are append-only'); END;",
                )?;
            }
            if analysis_needs_revision {
                self.execute(
                    "CREATE TABLE transport_analysis_receipts_replacement (\
                       proof_id TEXT PRIMARY KEY,\
                       channel_id TEXT NOT NULL,\
                       sink_id TEXT NOT NULL,\
                       target_id TEXT NOT NULL,\
                       target_revision INTEGER NOT NULL CHECK (target_revision >= 1),\
                       candidate_version TEXT NOT NULL,\
                       source_commit TEXT NOT NULL,\
                       configuration_hash TEXT NOT NULL,\
                       outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail')),\
                       captured_at_unix_ms INTEGER NOT NULL CHECK (captured_at_unix_ms >= 0),\
                       recorded_at_unix_ms INTEGER NOT NULL CHECK (recorded_at_unix_ms >= captured_at_unix_ms),\
                       record_json TEXT NOT NULL\
                     );",
                )
                .map_err(|error| format!("create transport analysis replacement: {error}"))?;
                if !self.sqlite_table_has_column("transport_analysis_receipts", "proof_id")?
                    || !self.sqlite_table_has_column(
                        "transport_analysis_receipts_replacement",
                        "proof_id",
                    )?
                {
                    return Err(
                        "transport analysis migration source or replacement is missing proof_id"
                            .into(),
                    );
                }
                self.execute(
                    "INSERT INTO transport_analysis_receipts_replacement(proof_id,channel_id,sink_id,target_id,target_revision,candidate_version,source_commit,configuration_hash,outcome,captured_at_unix_ms,recorded_at_unix_ms,record_json) \
                     SELECT legacy.proof_id,legacy.channel_id,legacy.sink_id,legacy.target_id,1,legacy.candidate_version,legacy.source_commit,legacy.configuration_hash,legacy.outcome,legacy.captured_at_unix_ms,legacy.recorded_at_unix_ms,legacy.record_json \
                     FROM transport_analysis_receipts AS legacy;",
                )
                .map_err(|error| format!("copy legacy transport analysis receipts: {error}"))?;
                self.execute("DROP TRIGGER IF EXISTS transport_analysis_no_update")?;
                self.execute("DROP TRIGGER IF EXISTS transport_analysis_no_delete")?;
                self.execute("DROP INDEX IF EXISTS transport_analysis_current")?;
                self.execute("DROP INDEX IF EXISTS transport_analysis_history")?;
                self.execute("DROP TABLE transport_analysis_receipts")?;
                self.execute(
                    "ALTER TABLE transport_analysis_receipts_replacement RENAME TO transport_analysis_receipts",
                )?;
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    fn sqlite_table_exists(&self, table: &str) -> Result<bool, String> {
        let statement =
            self.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(table, "SQLite table name")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(true),
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn sqlite_table_has_column(&self, table: &str, column: &str) -> Result<bool, String> {
        if !self.sqlite_table_exists(table)? {
            return Ok(false);
        }
        let statement = self.prepare(&format!("PRAGMA table_info({table})"))?;
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW if column_text(statement.raw, 1)? == column => return Ok(true),
                SQLITE_ROW => {}
                SQLITE_DONE => return Ok(false),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn legacy_transport_target_config_rewrites(
        &self,
    ) -> Result<Vec<(String, String, u64, String)>, String> {
        let statement = self.prepare(
            "SELECT channel_id,sink_id,revision,record_json FROM transport_target_configs ORDER BY channel_id,sink_id,revision",
        )?;
        let mut rewrites = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let channel_id = column_text(statement.raw, 0)?;
                    let sink_id = column_text(statement.raw, 1)?;
                    let revision = u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 2) })
                        .map_err(|_| "legacy transport target revision is invalid".to_string())?;
                    let record_json = column_text(statement.raw, 3)?;
                    if let Some(rewritten) = migrate_legacy_transport_target_config(&record_json)? {
                        rewrites.push((channel_id, sink_id, revision, rewritten));
                    }
                }
                SQLITE_DONE => return Ok(rewrites),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn initialize_operator_auth_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS operator_accounts (\
               account_id TEXT PRIMARY KEY,\
               username TEXT NOT NULL COLLATE NOCASE UNIQUE,\
               display_name TEXT NOT NULL,\
               password_phc TEXT NOT NULL,\
               credential_version INTEGER NOT NULL CHECK (credential_version >= 1),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),\
               first_failure_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (first_failure_at_unix_ms >= -1),\
               locked_until_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (locked_until_unix_ms >= -1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms)\
             );\
             CREATE TABLE IF NOT EXISTS account_roles (\
               account_id TEXT NOT NULL,\
               role TEXT NOT NULL CHECK (role IN ('setup_admin','meeting_operator','records_clerk','publish_operator','support_admin')),\
               PRIMARY KEY(account_id, role),\
               FOREIGN KEY(account_id) REFERENCES operator_accounts(account_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS operator_sessions (\
               session_digest BLOB PRIMARY KEY CHECK (length(session_digest) = 32),\
               csrf_digest BLOB NOT NULL UNIQUE CHECK (length(csrf_digest) = 32),\
               account_id TEXT NOT NULL,\
               credential_version INTEGER NOT NULL CHECK (credential_version >= 1),\
               boot_id BLOB NOT NULL CHECK (length(boot_id) = 32),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               last_seen_at_unix_ms INTEGER NOT NULL CHECK (last_seen_at_unix_ms >= created_at_unix_ms),\
               idle_expires_at_unix_ms INTEGER NOT NULL CHECK (idle_expires_at_unix_ms > last_seen_at_unix_ms),\
               absolute_expires_at_unix_ms INTEGER NOT NULL CHECK (absolute_expires_at_unix_ms >= idle_expires_at_unix_ms),\
               revoked_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (revoked_at_unix_ms >= -1),\
               FOREIGN KEY(account_id) REFERENCES operator_accounts(account_id) ON DELETE CASCADE\
             );\
             CREATE INDEX IF NOT EXISTS operator_sessions_account \
               ON operator_sessions(account_id, credential_version);\
             CREATE INDEX IF NOT EXISTS operator_sessions_expiry \
               ON operator_sessions(idle_expires_at_unix_ms, absolute_expires_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS operator_bootstrap (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               secret_digest BLOB NOT NULL UNIQUE CHECK (length(secret_digest) = 32),\
               expires_at_unix_ms INTEGER NOT NULL CHECK (expires_at_unix_ms >= 0),\
               consumed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (consumed_at_unix_ms >= -1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               CHECK (expires_at_unix_ms > created_at_unix_ms),\
               CHECK (consumed_at_unix_ms = -1 OR consumed_at_unix_ms >= created_at_unix_ms)\
             );\
             CREATE TABLE IF NOT EXISTS operator_recovery_hashes (\
               account_id TEXT NOT NULL,\
               recovery_digest BLOB NOT NULL CHECK (length(recovery_digest) = 32),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               consumed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (consumed_at_unix_ms >= -1),\
               PRIMARY KEY(account_id, recovery_digest),\
               FOREIGN KEY(account_id) REFERENCES operator_accounts(account_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS operator_auth_audit (\
               audit_id INTEGER PRIMARY KEY AUTOINCREMENT,\
               event_kind TEXT NOT NULL CHECK (length(event_kind) BETWEEN 1 AND 64),\
               account_id TEXT,\
               session_digest BLOB CHECK (session_digest IS NULL OR length(session_digest) = 32),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               detail TEXT NOT NULL CHECK (length(detail) <= 2048)\
             );\
             CREATE INDEX IF NOT EXISTS operator_auth_audit_account_time \
               ON operator_auth_audit(account_id, occurred_at_unix_ms, audit_id);\
             CREATE TRIGGER IF NOT EXISTS operator_auth_audit_no_update \
               BEFORE UPDATE ON operator_auth_audit \
               BEGIN SELECT RAISE(ABORT, 'operator auth audit is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS operator_auth_audit_no_delete \
               BEFORE DELETE ON operator_auth_audit \
               BEGIN SELECT RAISE(ABORT, 'operator auth audit is append-only'); END;",
        )?;
        self.execute(&format!(
            "CREATE TRIGGER IF NOT EXISTS operator_accounts_no_reserved_materialization_actor_insert \
             BEFORE INSERT ON operator_accounts \
             WHEN NEW.account_id='{AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID}' \
             BEGIN SELECT RAISE(ABORT, 'automatic materialization actor is reserved'); END;\
             CREATE TRIGGER IF NOT EXISTS operator_accounts_no_reserved_materialization_actor_update \
             BEFORE UPDATE OF account_id ON operator_accounts \
             WHEN NEW.account_id='{AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID}' \
             BEGIN SELECT RAISE(ABORT, 'automatic materialization actor is reserved'); END;"
        ))?;
        self.reject_reserved_operator_account_identity()
    }

    fn reject_reserved_operator_account_identity(&self) -> Result<(), String> {
        let statement =
            self.prepare("SELECT EXISTS(SELECT 1 FROM operator_accounts WHERE account_id=?1)")?;
        let account_id = c_string(
            AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID,
            "reserved automatic materialization actor",
        )?;
        bind_text(self.database, statement.raw, 1, &account_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW if unsafe { sqlite3_column_int64(statement.raw, 0) } == 0 => Ok(()),
            SQLITE_ROW => {
                Err("operator account identity is reserved for automatic materialization".into())
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn ensure_operator_account_identity_is_not_reserved(
        account: &OperatorAccount,
    ) -> Result<(), String> {
        if account.account_id().as_str() == AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID {
            return Err(
                "operator account identity is reserved for automatic materialization".into(),
            );
        }
        Ok(())
    }

    pub fn read_operator_login(
        &self,
        username: &OperatorUsername,
    ) -> Result<Option<OperatorLoginAuthority>, String> {
        let statement = self.prepare(
            "SELECT account_id,username,display_name,password_phc,credential_version,enabled,\
             failed_attempts,first_failure_at_unix_ms,locked_until_unix_ms,\
             created_at_unix_ms,updated_at_unix_ms \
             FROM operator_accounts WHERE username=?1 COLLATE NOCASE",
        )?;
        let username_text = c_string(username.as_str(), "operator username")?;
        bind_text(self.database, statement.raw, 1, &username_text)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let account_id = AccountId::try_from(column_text(statement.raw, 0)?)
                    .map_err(|error| format!("database returned invalid account id: {error}"))?;
                let stored_username = OperatorUsername::try_from(column_text(statement.raw, 1)?)
                    .map_err(|error| format!("database returned invalid username: {error}"))?;
                let display_name = OperatorDisplayName::try_from(column_text(statement.raw, 2)?)
                    .map_err(|error| format!("database returned invalid display name: {error}"))?;
                let password_phc = PasswordPhc::try_from(column_text(statement.raw, 3)?)
                    .map_err(|error| format!("database returned invalid password hash: {error}"))?;
                let credential_version = CredentialVersion::new(
                    u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 4) })
                        .map_err(|_| "database returned invalid credential version".to_string())?,
                )
                .map_err(|error| {
                    format!("database returned invalid credential version: {error}")
                })?;
                let enabled = unsafe { sqlite3_column_int64(statement.raw, 5) } == 1;
                let failed_attempts =
                    u32::try_from(unsafe { sqlite3_column_int64(statement.raw, 6) }).map_err(
                        |_| "database returned invalid failed-attempt count".to_string(),
                    )?;
                let first_failure_at_unix_ms = optional_nonnegative_column(statement.raw, 7)?;
                let locked_until_unix_ms = optional_nonnegative_column(statement.raw, 8)?;
                let created_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 9) };
                let updated_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 10) };
                let roles = self.read_operator_roles(&account_id)?;
                let account = OperatorAccount::new(
                    account_id,
                    stored_username,
                    display_name,
                    password_phc,
                    roles,
                    credential_version,
                    enabled,
                    created_at_unix_ms,
                    updated_at_unix_ms,
                )
                .map_err(|error| format!("database returned invalid operator account: {error}"))?;
                Ok(Some(OperatorLoginAuthority {
                    account,
                    failed_attempts,
                    first_failure_at_unix_ms,
                    locked_until_unix_ms,
                }))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn record_operator_login_failure(
        &self,
        account_id: &AccountId,
        occurred_at_unix_ms: i64,
        locked_until_unix_ms: Option<i64>,
    ) -> Result<bool, String> {
        if occurred_at_unix_ms < 0
            || locked_until_unix_ms.is_some_and(|locked| locked < occurred_at_unix_ms)
        {
            return Err("operator login failure timestamps are invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE operator_accounts SET \
                 failed_attempts=failed_attempts+1,\
                 first_failure_at_unix_ms=CASE WHEN first_failure_at_unix_ms=-1 THEN ?1 ELSE first_failure_at_unix_ms END,\
                 locked_until_unix_ms=MAX(locked_until_unix_ms,?2),\
                 updated_at_unix_ms=MAX(updated_at_unix_ms,?1) \
                 WHERE account_id=?3 AND enabled=1",
            )?;
            bind_i64(self.database, statement.raw, 1, occurred_at_unix_ms)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                locked_until_unix_ms.unwrap_or(-1),
            )?;
            let id = c_string(account_id.as_str(), "account id")?;
            bind_text(self.database, statement.raw, 3, &id)?;
            expect_done(self.database, statement.raw)?;
            let changed = self.changes() == 1;
            if changed {
                self.append_operator_auth_audit(
                    "login_failed",
                    account_id,
                    None,
                    occurred_at_unix_ms,
                    "operator login failed",
                )?;
            }
            Ok(changed)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn clear_operator_login_failures(
        &self,
        account_id: &AccountId,
        occurred_at_unix_ms: i64,
    ) -> Result<bool, String> {
        if occurred_at_unix_ms < 0 {
            return Err("operator login success timestamp is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE operator_accounts SET failed_attempts=0,first_failure_at_unix_ms=-1,\
                 locked_until_unix_ms=-1,updated_at_unix_ms=MAX(updated_at_unix_ms,?1) \
                 WHERE account_id=?2 AND enabled=1",
            )?;
            bind_i64(self.database, statement.raw, 1, occurred_at_unix_ms)?;
            let id = c_string(account_id.as_str(), "account id")?;
            bind_text(self.database, statement.raw, 2, &id)?;
            expect_done(self.database, statement.raw)?;
            let changed = self.changes() == 1;
            if changed {
                self.append_operator_auth_audit(
                    "login_succeeded",
                    account_id,
                    None,
                    occurred_at_unix_ms,
                    "operator login succeeded",
                )?;
            }
            Ok(changed)
        })();
        self.finish_value_transaction(outcome)
    }

    fn read_operator_roles(
        &self,
        account_id: &AccountId,
    ) -> Result<BTreeSet<OperatorRole>, String> {
        let statement =
            self.prepare("SELECT role FROM account_roles WHERE account_id=?1 ORDER BY role")?;
        let id = c_string(account_id.as_str(), "account id")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        let mut roles = BTreeSet::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let role_text = column_text(statement.raw, 0)?;
                    roles.insert(OperatorRole::try_from(role_text.as_str()).map_err(|error| {
                        format!("database returned invalid operator role: {error}")
                    })?);
                }
                SQLITE_DONE => return Ok(roles),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn provision_operator_bootstrap(
        &self,
        bootstrap: &BootstrapCredential,
    ) -> Result<bool, String> {
        if bootstrap.issued_at_unix_ms() < 0
            || bootstrap.expires_at_unix_ms() <= bootstrap.issued_at_unix_ms()
            || bootstrap.consumed_at_unix_ms().is_some()
        {
            return Err("bootstrap credential is not an unconsumed bounded credential".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "INSERT INTO operator_bootstrap(singleton,secret_digest,expires_at_unix_ms,consumed_at_unix_ms,created_at_unix_ms) \
                 SELECT 1,?1,?2,-1,?3 WHERE NOT EXISTS(SELECT 1 FROM operator_accounts) \
                 ON CONFLICT(singleton) DO UPDATE SET secret_digest=excluded.secret_digest,\
                 expires_at_unix_ms=excluded.expires_at_unix_ms,consumed_at_unix_ms=-1,\
                 created_at_unix_ms=excluded.created_at_unix_ms \
                 WHERE operator_bootstrap.consumed_at_unix_ms=-1 \
                 AND operator_bootstrap.expires_at_unix_ms<=excluded.created_at_unix_ms \
                 AND NOT EXISTS(SELECT 1 FROM operator_accounts)",
            )?;
            bind_blob(
                self.database,
                statement.raw,
                1,
                bootstrap.token_digest().as_bytes(),
            )?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                bootstrap.expires_at_unix_ms(),
            )?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                bootstrap.issued_at_unix_ms(),
            )?;
            expect_done(self.database, statement.raw)?;
            Ok(self.changes() == 1)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn read_operator_bootstrap(&self) -> Result<Option<BootstrapCredential>, String> {
        let statement = self.prepare(
            "SELECT secret_digest,created_at_unix_ms,expires_at_unix_ms,consumed_at_unix_ms \
             FROM operator_bootstrap WHERE singleton=1",
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => BootstrapCredential::new(
                secret_digest_column(statement.raw, 0)?,
                unsafe { sqlite3_column_int64(statement.raw, 1) },
                unsafe { sqlite3_column_int64(statement.raw, 2) },
                optional_nonnegative_column(statement.raw, 3)?,
            )
            .map(Some)
            .map_err(|error| format!("database returned invalid bootstrap authority: {error}")),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn has_operator_accounts(&self) -> Result<bool, String> {
        let statement = self.prepare("SELECT EXISTS(SELECT 1 FROM operator_accounts)")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(unsafe { sqlite3_column_int64(statement.raw, 0) } == 1),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn consume_operator_bootstrap(
        &self,
        presented_digest: &SecretDigest,
        installation: &OperatorBootstrapInstallation,
        occurred_at_unix_ms: i64,
    ) -> Result<bool, String> {
        Self::ensure_operator_account_identity_is_not_reserved(&installation.account)?;
        if occurred_at_unix_ms < 0
            || !installation
                .account
                .roles()
                .contains(&OperatorRole::SetupAdmin)
            || installation.session.account_id() != installation.account.account_id()
            || installation.session.credential_version()
                != installation.account.credential_version()
            || installation.recovery_digests.is_empty()
            || installation.recovery_digests.len() > 32
            || installation
                .recovery_digests
                .iter()
                .enumerate()
                .any(|(index, digest)| installation.recovery_digests[..index].contains(digest))
        {
            return Err("bootstrap installation authority is invalid or unbounded".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let consume = self.prepare(
                "UPDATE operator_bootstrap SET consumed_at_unix_ms=?1 \
                 WHERE singleton=1 AND secret_digest=?2 AND consumed_at_unix_ms=-1 \
                 AND created_at_unix_ms<=?1 AND expires_at_unix_ms>?1 \
                 AND NOT EXISTS(SELECT 1 FROM operator_accounts)",
            )?;
            bind_i64(self.database, consume.raw, 1, occurred_at_unix_ms)?;
            bind_blob(self.database, consume.raw, 2, presented_digest.as_bytes())?;
            expect_done(self.database, consume.raw)?;
            if self.changes() != 1 {
                return Ok(false);
            }
            self.insert_operator_account_rows(&installation.account)
                .map_err(|error| format!("insert bootstrap account: {error}"))?;
            self.insert_operator_session_rows(&installation.session)
                .map_err(|error| format!("insert bootstrap session: {error}"))?;
            for digest in &installation.recovery_digests {
                let recovery = self.prepare(
                    "INSERT INTO operator_recovery_hashes(account_id,recovery_digest,created_at_unix_ms,consumed_at_unix_ms) \
                     VALUES(?1,?2,?3,-1)",
                )?;
                let id = c_string(installation.account.account_id().as_str(), "account id")?;
                bind_text(self.database, recovery.raw, 1, &id)?;
                bind_blob(self.database, recovery.raw, 2, digest.as_bytes())?;
                bind_i64(self.database, recovery.raw, 3, occurred_at_unix_ms)?;
                expect_done(self.database, recovery.raw)
                    .map_err(|error| format!("insert bootstrap recovery digest: {error}"))?;
            }
            self.append_operator_auth_audit(
                "bootstrap_consumed",
                installation.account.account_id(),
                Some(&installation.session.session_digest()),
                occurred_at_unix_ms,
                "setup administrator authority created",
            )
            .map_err(|error| format!("append bootstrap audit: {error}"))?;
            Ok(true)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn insert_operator_account(&self, account: &OperatorAccount) -> Result<(), String> {
        Self::ensure_operator_account_identity_is_not_reserved(account)?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.insert_operator_account_rows(account)?;
            self.append_operator_auth_audit(
                "account_created",
                account.account_id(),
                None,
                account.created_at_unix_ms(),
                "operator account created",
            )
        })();
        self.finish_transaction(outcome)
    }

    pub fn create_operator_session(
        &self,
        session: &OperatorSession,
        occurred_at_unix_ms: i64,
    ) -> Result<(), String> {
        if occurred_at_unix_ms < 0 {
            return Err("session creation timestamp is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let authority = self.read_account_version_and_enabled(session.account_id())?;
            if authority != Some((session.credential_version(), true)) {
                return Err("session account authority is disabled, absent, or stale".into());
            }
            self.ensure_operator_session_capacity(session.account_id(), occurred_at_unix_ms)?;
            self.insert_operator_session_rows(session)?;
            self.append_operator_auth_audit(
                "session_created",
                session.account_id(),
                Some(&session.session_digest()),
                occurred_at_unix_ms,
                "operator session created",
            )
        })();
        self.finish_transaction(outcome)
    }

    pub fn validate_operator_session(
        &self,
        session_digest: &SecretDigest,
        csrf_digest: &SecretDigest,
        now_unix_ms: i64,
        current_boot_id: &SecretDigest,
    ) -> Result<Option<OperatorSessionAuthority>, String> {
        if now_unix_ms < 0 {
            return Ok(None);
        }
        let statement = self.prepare(
            "SELECT account_id,credential_version,boot_id,created_at_unix_ms,last_seen_at_unix_ms,\
             idle_expires_at_unix_ms,absolute_expires_at_unix_ms \
             FROM operator_sessions WHERE session_digest=?1 AND csrf_digest=?2 AND revoked_at_unix_ms=-1",
        )?;
        bind_blob(self.database, statement.raw, 1, session_digest.as_bytes())?;
        bind_blob(self.database, statement.raw, 2, csrf_digest.as_bytes())?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let account_id =
                    AccountId::try_from(column_text(statement.raw, 0)?).map_err(|error| {
                        format!("database returned invalid session account: {error}")
                    })?;
                let version = credential_version_column(statement.raw, 1)?;
                let session = OperatorSession::new(
                    *session_digest,
                    *csrf_digest,
                    account_id.clone(),
                    version,
                    secret_digest_column(statement.raw, 2)?,
                    unsafe { sqlite3_column_int64(statement.raw, 3) },
                    unsafe { sqlite3_column_int64(statement.raw, 4) },
                    unsafe { sqlite3_column_int64(statement.raw, 5) },
                    unsafe { sqlite3_column_int64(statement.raw, 6) },
                )
                .map_err(|error| format!("database returned invalid session authority: {error}"))?;
                let account = self
                    .read_operator_account_by_id(&account_id)?
                    .ok_or_else(|| "session referenced a missing operator account".to_string())?;
                if session
                    .validate_at(
                        now_unix_ms,
                        current_boot_id,
                        account.credential_version(),
                        account.enabled(),
                    )
                    .is_err()
                {
                    return Ok(None);
                }
                Ok(Some(OperatorSessionAuthority { account, session }))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn touch_operator_session(
        &self,
        session_digest: &SecretDigest,
        last_seen_at_unix_ms: i64,
        idle_expires_at_unix_ms: i64,
    ) -> Result<bool, String> {
        if last_seen_at_unix_ms < 0 || idle_expires_at_unix_ms <= last_seen_at_unix_ms {
            return Err("session touch timestamps are invalid".into());
        }
        let statement = self.prepare(
            "UPDATE operator_sessions SET last_seen_at_unix_ms=?1,idle_expires_at_unix_ms=?2 \
             WHERE session_digest=?3 AND revoked_at_unix_ms=-1 \
             AND last_seen_at_unix_ms<=?1 AND absolute_expires_at_unix_ms>=?2",
        )?;
        bind_i64(self.database, statement.raw, 1, last_seen_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 2, idle_expires_at_unix_ms)?;
        bind_blob(self.database, statement.raw, 3, session_digest.as_bytes())?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    pub fn revoke_operator_session(
        &self,
        session_digest: &SecretDigest,
        occurred_at_unix_ms: i64,
    ) -> Result<bool, String> {
        if occurred_at_unix_ms < 0 {
            return Err("session revocation timestamp is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE operator_sessions SET revoked_at_unix_ms=?1 \
             WHERE session_digest=?2 AND revoked_at_unix_ms=-1",
        )?;
        bind_i64(self.database, statement.raw, 1, occurred_at_unix_ms)?;
        bind_blob(self.database, statement.raw, 2, session_digest.as_bytes())?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    pub fn purge_operator_sessions_for_boot(
        &self,
        current_boot_id: &SecretDigest,
        occurred_at_unix_ms: i64,
    ) -> Result<usize, String> {
        if occurred_at_unix_ms < 0 {
            return Err("session purge timestamp is invalid".into());
        }
        let statement = self.prepare("DELETE FROM operator_sessions WHERE boot_id<>?1")?;
        bind_blob(self.database, statement.raw, 1, current_boot_id.as_bytes())?;
        expect_done(self.database, statement.raw)?;
        usize::try_from(self.changes()).map_err(|_| "invalid purged session count".into())
    }

    pub fn purge_operator_sessions_for_credential_version(
        &self,
        account_id: &AccountId,
        current_version: CredentialVersion,
        occurred_at_unix_ms: i64,
    ) -> Result<usize, String> {
        if occurred_at_unix_ms < 0 {
            return Err("session purge timestamp is invalid".into());
        }
        let statement = self.prepare(
            "DELETE FROM operator_sessions WHERE account_id=?1 AND credential_version<>?2",
        )?;
        let id = c_string(account_id.as_str(), "account id")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(current_version.get(), "credential version")?,
        )?;
        expect_done(self.database, statement.raw)?;
        usize::try_from(self.changes()).map_err(|_| "invalid purged session count".into())
    }

    pub fn consume_operator_recovery(
        &self,
        account_id: &AccountId,
        recovery_digest: &SecretDigest,
        new_password_phc: &PasswordPhc,
        occurred_at_unix_ms: i64,
    ) -> Result<Option<CredentialVersion>, String> {
        if occurred_at_unix_ms < 0 {
            return Err("recovery timestamp is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let recovery = self.prepare(
                "UPDATE operator_recovery_hashes SET consumed_at_unix_ms=?1 \
                 WHERE account_id=?2 AND recovery_digest=?3 AND consumed_at_unix_ms=-1",
            )?;
            bind_i64(self.database, recovery.raw, 1, occurred_at_unix_ms)?;
            let id = c_string(account_id.as_str(), "account id")?;
            bind_text(self.database, recovery.raw, 2, &id)?;
            bind_blob(self.database, recovery.raw, 3, recovery_digest.as_bytes())?;
            expect_done(self.database, recovery.raw)?;
            if self.changes() != 1 {
                return Ok(None);
            }
            let (current_version, _) = self
                .read_account_version_and_enabled(account_id)?
                .ok_or_else(|| "recovery referenced a missing operator account".to_string())?;
            let next_version = current_version
                .next()
                .map_err(|error| format!("credential version cannot rotate: {error}"))?;
            let account = self.prepare(
                "UPDATE operator_accounts SET password_phc=?1,credential_version=?2,\
                 failed_attempts=0,first_failure_at_unix_ms=-1,locked_until_unix_ms=-1,\
                 updated_at_unix_ms=MAX(updated_at_unix_ms,?3) WHERE account_id=?4 AND credential_version=?5",
            )?;
            let phc = c_string(new_password_phc.as_str(), "password hash")?;
            bind_text(self.database, account.raw, 1, &phc)?;
            bind_i64(
                self.database,
                account.raw,
                2,
                checked_i64(next_version.get(), "credential version")?,
            )?;
            bind_i64(self.database, account.raw, 3, occurred_at_unix_ms)?;
            bind_text(self.database, account.raw, 4, &id)?;
            bind_i64(
                self.database,
                account.raw,
                5,
                checked_i64(current_version.get(), "credential version")?,
            )?;
            expect_done(self.database, account.raw)?;
            if self.changes() != 1 {
                return Err("credential rotation lost account authority".into());
            }
            let sessions = self.prepare("DELETE FROM operator_sessions WHERE account_id=?1")?;
            bind_text(self.database, sessions.raw, 1, &id)?;
            expect_done(self.database, sessions.raw)?;
            self.append_operator_auth_audit(
                "recovery_consumed",
                account_id,
                None,
                occurred_at_unix_ms,
                "credential version rotated and sessions invalidated",
            )?;
            Ok(Some(next_version))
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn rotate_operator_password(
        &self,
        account_id: &AccountId,
        expected_version: CredentialVersion,
        new_password_phc: &PasswordPhc,
        occurred_at_unix_ms: i64,
    ) -> Result<Option<CredentialVersion>, String> {
        if occurred_at_unix_ms < 0 {
            return Err("password rotation timestamp is invalid".into());
        }
        let next_version = expected_version
            .next()
            .map_err(|error| format!("credential version cannot rotate: {error}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE operator_accounts SET password_phc=?1,credential_version=?2,\
                 failed_attempts=0,first_failure_at_unix_ms=-1,locked_until_unix_ms=-1,\
                 updated_at_unix_ms=MAX(updated_at_unix_ms,?3) \
                 WHERE account_id=?4 AND credential_version=?5 AND enabled=1",
            )?;
            let phc = c_string(new_password_phc.as_str(), "password hash")?;
            let id = c_string(account_id.as_str(), "account id")?;
            bind_text(self.database, statement.raw, 1, &phc)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(next_version.get(), "credential version")?,
            )?;
            bind_i64(self.database, statement.raw, 3, occurred_at_unix_ms)?;
            bind_text(self.database, statement.raw, 4, &id)?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                checked_i64(expected_version.get(), "credential version")?,
            )?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Ok(None);
            }
            let sessions = self.prepare("DELETE FROM operator_sessions WHERE account_id=?1")?;
            bind_text(self.database, sessions.raw, 1, &id)?;
            expect_done(self.database, sessions.raw)?;
            self.append_operator_auth_audit(
                "password_changed",
                account_id,
                None,
                occurred_at_unix_ms,
                "credential version rotated and sessions invalidated",
            )?;
            Ok(Some(next_version))
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn list_operator_recovery_credentials(
        &self,
        account_id: &AccountId,
    ) -> Result<Vec<RecoveryCredential>, String> {
        let statement = self.prepare(
            "SELECT recovery_digest,created_at_unix_ms,consumed_at_unix_ms \
             FROM operator_recovery_hashes WHERE account_id=?1 ORDER BY recovery_digest",
        )?;
        let id = c_string(account_id.as_str(), "account id")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        let mut credentials = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => credentials.push(
                    RecoveryCredential::new(
                        secret_digest_column(statement.raw, 0)?,
                        account_id.clone(),
                        unsafe { sqlite3_column_int64(statement.raw, 1) },
                        optional_nonnegative_column(statement.raw, 2)?,
                    )
                    .map_err(|error| {
                        format!("database returned invalid recovery authority: {error}")
                    })?,
                ),
                SQLITE_DONE => return Ok(credentials),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn insert_operator_account_rows(&self, account: &OperatorAccount) -> Result<(), String> {
        Self::ensure_operator_account_identity_is_not_reserved(account)?;
        let statement = self.prepare(
            "INSERT INTO operator_accounts(account_id,username,display_name,password_phc,credential_version,enabled,\
             failed_attempts,first_failure_at_unix_ms,locked_until_unix_ms,created_at_unix_ms,updated_at_unix_ms) \
             VALUES(?1,?2,?3,?4,?5,?6,0,-1,-1,?7,?8)",
        )?;
        let account_id = c_string(account.account_id().as_str(), "account id")?;
        let username = c_string(account.username().as_str(), "operator username")?;
        let display_name = c_string(account.display_name().as_str(), "operator display name")?;
        let password_phc = c_string(account.password_phc().as_str(), "password hash")?;
        bind_text(self.database, statement.raw, 1, &account_id)?;
        bind_text(self.database, statement.raw, 2, &username)?;
        bind_text(self.database, statement.raw, 3, &display_name)?;
        bind_text(self.database, statement.raw, 4, &password_phc)?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            checked_i64(account.credential_version().get(), "credential version")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            i64::from(account.enabled()),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            account.created_at_unix_ms(),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            8,
            account.updated_at_unix_ms(),
        )?;
        expect_done(self.database, statement.raw)?;
        for role in account.roles() {
            let statement =
                self.prepare("INSERT INTO account_roles(account_id,role) VALUES(?1,?2)")?;
            let id = c_string(account.account_id().as_str(), "account id")?;
            let role = c_string(role.as_str(), "operator role")?;
            bind_text(self.database, statement.raw, 1, &id)?;
            bind_text(self.database, statement.raw, 2, &role)?;
            expect_done(self.database, statement.raw)?;
        }
        Ok(())
    }

    fn insert_operator_session_rows(&self, session: &OperatorSession) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO operator_sessions(session_digest,csrf_digest,account_id,credential_version,boot_id,\
             created_at_unix_ms,last_seen_at_unix_ms,idle_expires_at_unix_ms,absolute_expires_at_unix_ms,revoked_at_unix_ms) \
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,-1)",
        )?;
        bind_blob(
            self.database,
            statement.raw,
            1,
            session.session_digest().as_bytes(),
        )?;
        bind_blob(
            self.database,
            statement.raw,
            2,
            session.csrf_digest().as_bytes(),
        )?;
        let id = c_string(session.account_id().as_str(), "account id")?;
        bind_text(self.database, statement.raw, 3, &id)?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            checked_i64(session.credential_version().get(), "credential version")?,
        )?;
        bind_blob(
            self.database,
            statement.raw,
            5,
            session.boot_id().as_bytes(),
        )?;
        for (index, value) in [
            session.created_at_unix_ms(),
            session.last_seen_at_unix_ms(),
            session.idle_expires_at_unix_ms(),
            session.absolute_expires_at_unix_ms(),
        ]
        .into_iter()
        .enumerate()
        {
            bind_i64(self.database, statement.raw, (index + 6) as c_int, value)?;
        }
        expect_done(self.database, statement.raw)
    }

    fn ensure_operator_session_capacity(
        &self,
        account_id: &AccountId,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM operator_sessions WHERE account_id=?1 AND revoked_at_unix_ms=-1 \
             AND idle_expires_at_unix_ms>?2 AND absolute_expires_at_unix_ms>?2",
        )?;
        let id = c_string(account_id.as_str(), "account id")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        bind_i64(self.database, statement.raw, 2, now_unix_ms)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW if unsafe { sqlite3_column_int64(statement.raw, 0) } < 8 => Ok(()),
            SQLITE_ROW => {
                Err("operator account already has the maximum of 8 active sessions".into())
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_operator_account_by_id(
        &self,
        account_id: &AccountId,
    ) -> Result<Option<OperatorAccount>, String> {
        let statement =
            self.prepare("SELECT username FROM operator_accounts WHERE account_id=?1")?;
        let id = c_string(account_id.as_str(), "account id")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let username = OperatorUsername::try_from(column_text(statement.raw, 0)?)
                    .map_err(|error| format!("database returned invalid username: {error}"))?;
                self.read_operator_login(&username)
                    .map(|authority| authority.map(|authority| authority.account))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_account_version_and_enabled(
        &self,
        account_id: &AccountId,
    ) -> Result<Option<(CredentialVersion, bool)>, String> {
        let statement = self.prepare(
            "SELECT credential_version,enabled FROM operator_accounts WHERE account_id=?1",
        )?;
        let id = c_string(account_id.as_str(), "account id")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => Ok(Some((
                credential_version_column(statement.raw, 0)?,
                unsafe { sqlite3_column_int64(statement.raw, 1) } == 1,
            ))),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn append_operator_auth_audit(
        &self,
        event_kind: &str,
        account_id: &AccountId,
        session_digest: Option<&SecretDigest>,
        occurred_at_unix_ms: i64,
        detail: &str,
    ) -> Result<(), String> {
        let (sql, has_session) = if session_digest.is_some() {
            (
                "INSERT INTO operator_auth_audit(event_kind,account_id,session_digest,occurred_at_unix_ms,detail) VALUES(?1,?2,?3,?4,?5)",
                true,
            )
        } else {
            (
                "INSERT INTO operator_auth_audit(event_kind,account_id,session_digest,occurred_at_unix_ms,detail) VALUES(?1,?2,NULL,?3,?4)",
                false,
            )
        };
        let statement = self.prepare(sql)?;
        let kind = c_string(event_kind, "auth audit event kind")?;
        let id = c_string(account_id.as_str(), "account id")?;
        let detail = c_string(detail, "auth audit detail")?;
        bind_text(self.database, statement.raw, 1, &kind)?;
        bind_text(self.database, statement.raw, 2, &id)?;
        if let Some(digest) = session_digest {
            bind_blob(self.database, statement.raw, 3, digest.as_bytes())?;
        }
        let time_index = if has_session { 4 } else { 3 };
        bind_i64(
            self.database,
            statement.raw,
            time_index,
            occurred_at_unix_ms,
        )?;
        bind_text(self.database, statement.raw, time_index + 1, &detail)?;
        expect_done(self.database, statement.raw)
    }

    pub fn write_profile(
        &self,
        profile: &StationProfile,
        expected_revision: u64,
    ) -> Result<StationProfileDocument, ProfileWriteError> {
        self.execute("BEGIN IMMEDIATE")
            .map_err(ProfileWriteError::Storage)?;
        let outcome = (|| {
            let current = self.read_profile().map_err(ProfileWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |document| document.revision);
            if current_revision != expected_revision {
                return Err(ProfileWriteError::Conflict(current_revision));
            }
            self.upsert_profile(profile)
                .map_err(ProfileWriteError::Storage)?;
            Ok(StationProfileDocument {
                profile: profile.clone(),
                revision: current_revision + 1,
            })
        })();

        match outcome {
            Ok(document) => match self.execute("COMMIT") {
                Ok(()) => Ok(document),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(ProfileWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    /// Returns the explicitly selected reference build, if commissioning has
    /// recorded one. A returned selection may name an older catalog version;
    /// callers must inspect `uses_current_catalog()` before treating it as a
    /// current qualification target.
    pub fn read_reference_station_tier_selection(
        &self,
    ) -> Result<Option<ReferenceStationTierSelection>, String> {
        let statement = self.prepare(
            "SELECT catalog_version,tier,revision,created_at_unix_ms,updated_at_unix_ms,updated_by \
             FROM reference_station_tier_selection WHERE singleton=1",
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_reference_station_tier_selection_row(
                statement.raw,
            )?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    /// Lists the immutable history in insertion (revision) order.
    pub fn list_reference_station_tier_audit(
        &self,
    ) -> Result<Vec<ReferenceStationTierAuditEvent>, String> {
        let statement = self.prepare(
            "SELECT revision,catalog_version,tier,actor,occurred_at_unix_ms \
             FROM reference_station_tier_audit ORDER BY revision",
        )?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_reference_station_tier_audit_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    /// Atomically records a new selected tier and exactly one corresponding
    /// append-only audit row. This is a CAS: `None` means create-only and a
    /// concrete revision means replace-only.
    pub fn write_reference_station_tier_selection(
        &self,
        request: &ReferenceStationTierWrite,
    ) -> Result<ReferenceStationTierSelection, ReferenceStationTierWriteError> {
        request
            .validate()
            .map_err(ReferenceStationTierWriteError::Invalid)?;
        if request.catalog_version != REFERENCE_STATION_CATALOG_VERSION {
            return Err(ReferenceStationTierWriteError::CatalogVersionMismatch {
                expected: REFERENCE_STATION_CATALOG_VERSION,
                actual: request.catalog_version,
            });
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(ReferenceStationTierWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_reference_station_tier_selection()
                .map_err(ReferenceStationTierWriteError::Storage)?;
            let current_revision = current.as_ref().map(|selection| selection.revision);
            if current_revision != request.expected_revision {
                return Err(ReferenceStationTierWriteError::Conflict { current_revision });
            }
            if current
                .as_ref()
                .is_some_and(|selection| request.occurred_at_unix_ms < selection.updated_at_unix_ms)
            {
                return Err(ReferenceStationTierWriteError::Invalid(
                    "reference station tier timestamp predates the current revision".into(),
                ));
            }
            let selection = ReferenceStationTierSelection {
                catalog_version: request.catalog_version,
                tier: request.tier.clone(),
                revision: current_revision.unwrap_or(0) + 1,
                created_at_unix_ms: current
                    .as_ref()
                    .map_or(request.occurred_at_unix_ms, |item| item.created_at_unix_ms),
                updated_at_unix_ms: request.occurred_at_unix_ms,
                updated_by: request.actor.clone(),
            };
            selection
                .validate()
                .map_err(ReferenceStationTierWriteError::Invalid)?;
            self.upsert_reference_station_tier_selection(&selection)
                .map_err(ReferenceStationTierWriteError::Storage)?;
            let audit = ReferenceStationTierAuditEvent {
                revision: selection.revision,
                catalog_version: selection.catalog_version,
                tier: selection.tier.clone(),
                actor: request.actor.clone(),
                occurred_at_unix_ms: request.occurred_at_unix_ms,
            };
            audit
                .validate()
                .map_err(ReferenceStationTierWriteError::Invalid)?;
            self.append_reference_station_tier_audit(&audit)
                .map_err(ReferenceStationTierWriteError::Storage)?;
            Ok(selection)
        })();
        match outcome {
            Ok(selection) => match self.execute("COMMIT") {
                Ok(()) => Ok(selection),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(ReferenceStationTierWriteError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_live_source(&self, source_id: &str) -> Result<Option<LiveSource>, String> {
        let statement = self.prepare(
            "SELECT source_id, channel_id, label, enabled, endpoint_kind, bind_ip, port, revision \
             FROM live_sources WHERE source_id=?1",
        )?;
        let source_id = c_string(source_id, "live source identity")?;
        bind_text(self.database, statement.raw, 1, &source_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_live_source_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_live_sources(&self) -> Result<Vec<LiveSource>, String> {
        let statement = self.prepare(
            "SELECT source_id, channel_id, label, enabled, endpoint_kind, bind_ip, port, revision \
             FROM live_sources ORDER BY label, source_id",
        )?;
        let mut sources = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => sources.push(read_live_source_row(statement.raw)?),
                SQLITE_DONE => return Ok(sources),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_channel_live_sources(&self, channel_id: &str) -> Result<Vec<LiveSource>, String> {
        let statement = self.prepare(
            "SELECT source_id, channel_id, label, enabled, endpoint_kind, bind_ip, port, revision \
             FROM live_sources WHERE channel_id=?1 ORDER BY label, source_id",
        )?;
        let channel_id = c_string(channel_id, "live source channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut sources = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => sources.push(read_live_source_row(statement.raw)?),
                SQLITE_DONE => return Ok(sources),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_live_source(
        &self,
        source: &LiveSource,
        expected_revision: u64,
    ) -> Result<LiveSource, LiveSourceWriteError> {
        source.validate().map_err(LiveSourceWriteError::Invalid)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(LiveSourceWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_live_source(&source.source_id)
                .map_err(LiveSourceWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(LiveSourceWriteError::Conflict(current_revision));
            }
            if source.revision != expected_revision.saturating_add(1) {
                return Err(LiveSourceWriteError::Invalid(
                    "live source revision must be exactly one greater than expected".into(),
                ));
            }
            let (kind, bind_ip, port) = source.endpoint.storage_fields();
            let statement = self
                .prepare(
                    "INSERT INTO live_sources(source_id,channel_id,label,enabled,endpoint_kind,bind_ip,port,revision) \
                     VALUES(?1,?2,?3,?4,?5,?6,?7,?8) \
                     ON CONFLICT(source_id) DO UPDATE SET channel_id=excluded.channel_id, label=excluded.label, enabled=excluded.enabled, \
                     endpoint_kind=excluded.endpoint_kind, bind_ip=excluded.bind_ip, port=excluded.port, revision=excluded.revision",
                )
                .map_err(LiveSourceWriteError::Storage)?;
            let source_id = c_string(&source.source_id, "live source identity")
                .map_err(LiveSourceWriteError::Invalid)?;
            let label = c_string(&source.label, "live source label")
                .map_err(LiveSourceWriteError::Invalid)?;
            let channel_id = c_string(&source.channel_id, "live source channel identity")
                .map_err(LiveSourceWriteError::Invalid)?;
            let kind =
                c_string(kind, "live endpoint kind").map_err(LiveSourceWriteError::Invalid)?;
            let bind_ip =
                c_string(bind_ip, "live source bind IP").map_err(LiveSourceWriteError::Invalid)?;
            bind_text(self.database, statement.raw, 1, &source_id)
                .map_err(LiveSourceWriteError::Storage)?;
            bind_text(self.database, statement.raw, 2, &channel_id)
                .map_err(LiveSourceWriteError::Storage)?;
            bind_text(self.database, statement.raw, 3, &label)
                .map_err(LiveSourceWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 4, i64::from(source.enabled))
                .map_err(LiveSourceWriteError::Storage)?;
            bind_text(self.database, statement.raw, 5, &kind)
                .map_err(LiveSourceWriteError::Storage)?;
            bind_text(self.database, statement.raw, 6, &bind_ip)
                .map_err(LiveSourceWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 7, i64::from(port))
                .map_err(LiveSourceWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                8,
                checked_i64(source.revision, "live source revision")
                    .map_err(LiveSourceWriteError::Invalid)?,
            )
            .map_err(LiveSourceWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(LiveSourceWriteError::Storage)?;
            Ok(source.clone())
        })();
        match outcome {
            Ok(source) => match self.execute("COMMIT") {
                Ok(()) => Ok(source),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(LiveSourceWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn request_takeover(
        &self,
        session: &TakeoverSession,
        expected_authority_revision: u64,
    ) -> Result<(), String> {
        session.validate()?;
        if session.state != TakeoverState::Arming {
            return Err("new takeover sessions must begin in arming state".into());
        }
        if session.authority_revision != expected_authority_revision.saturating_add(1) {
            return Err(
                "takeover authority revision must be exactly one greater than expected".into(),
            );
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let source = self
                .read_live_source(&session.source_id)?
                .ok_or_else(|| "takeover source does not exist".to_string())?;
            if !source.enabled
                || source.channel_id != session.channel_id
                || source.revision != session.source_revision
            {
                return Err("takeover source is disabled, channel-mismatched, or stale".into());
            }
            let channel = self.prepare("SELECT enabled FROM channels WHERE channel_id=?1")?;
            let channel_id = c_string(&session.channel_id, "takeover channel identity")?;
            bind_text(self.database, channel.raw, 1, &channel_id)?;
            match unsafe { sqlite3_step(channel.raw) } {
                SQLITE_ROW if unsafe { sqlite3_column_int64(channel.raw, 0) } != 0 => {}
                SQLITE_ROW | SQLITE_DONE => return Err("takeover channel is not enabled".into()),
                _ => return Err(sqlite_error(self.database)),
            }
            let authority =
                self.prepare("SELECT revision FROM takeover_authority WHERE channel_id=?1")?;
            bind_text(self.database, authority.raw, 1, &channel_id)?;
            let current_authority = match unsafe { sqlite3_step(authority.raw) } {
                SQLITE_ROW => u64::try_from(unsafe { sqlite3_column_int64(authority.raw, 0) })
                    .map_err(|_| "invalid takeover authority revision".to_string())?,
                SQLITE_DONE => 0,
                _ => return Err(sqlite_error(self.database)),
            };
            if current_authority != expected_authority_revision {
                return Err("takeover authority compare-and-set conflict".into());
            }
            let authority_write = self.prepare(
                "INSERT INTO takeover_authority(channel_id,revision) VALUES(?1,?2) \
                 ON CONFLICT(channel_id) DO UPDATE SET revision=excluded.revision",
            )?;
            bind_text(self.database, authority_write.raw, 1, &channel_id)?;
            bind_i64(
                self.database,
                authority_write.raw,
                2,
                checked_i64(session.authority_revision, "takeover authority revision")?,
            )?;
            expect_done(self.database, authority_write.raw)?;
            self.insert_takeover_session(session)?;
            self.insert_takeover_audit(&TakeoverAuditEvent {
                audit_id: session.session_id.clone(),
                session_id: session.session_id.clone(),
                channel_id: session.channel_id.clone(),
                source_id: session.source_id.clone(),
                operator_id: session.operator_id.clone(),
                state: session.state,
                occurred_at_unix_ms: session.requested_at_unix_ms,
                detail: session.reason.clone(),
            })
        })();
        match outcome {
            Ok(()) => self.execute("COMMIT"),
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_takeover_session(
        &self,
        session_id: &str,
    ) -> Result<Option<TakeoverSession>, String> {
        let statement = self.prepare(
            "SELECT session_id, channel_id, source_id, operator_id, reason, requested_at_unix_ms, \
             taken_at_unix_ms, returned_at_unix_ms, source_revision, authority_revision, expires_at_unix_ms, state, failure_detail \
             FROM takeover_sessions WHERE session_id=?1",
        )?;
        let session_id = c_string(session_id, "takeover session identity")?;
        bind_text(self.database, statement.raw, 1, &session_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_takeover_session_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn read_takeover_authority_revision(&self, channel_id: &str) -> Result<u64, String> {
        let statement =
            self.prepare("SELECT revision FROM takeover_authority WHERE channel_id=?1")?;
        let channel_id = c_string(channel_id, "takeover channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 0) })
                .map_err(|_| "invalid takeover authority revision".to_string()),
            SQLITE_DONE => Ok(0),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn read_active_takeover(
        &self,
        channel_id: &str,
    ) -> Result<Option<TakeoverSession>, String> {
        let statement = self.prepare(
            "SELECT session_id, channel_id, source_id, operator_id, reason, requested_at_unix_ms, \
             taken_at_unix_ms, returned_at_unix_ms, source_revision, authority_revision, expires_at_unix_ms, state, failure_detail \
             FROM takeover_sessions WHERE channel_id=?1 AND state IN \
             ('arming','armed','take_requested','on_air','return_requested','returning') \
             ORDER BY requested_at_unix_ms DESC LIMIT 1",
        )?;
        let channel_id = c_string(channel_id, "takeover channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_takeover_session_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn transition_takeover(
        &self,
        session_id: &str,
        expected: TakeoverState,
        next: TakeoverState,
        occurred_at_unix_ms: i64,
        detail: Option<&str>,
        actor_id: &str,
        expected_authority_revision: u64,
    ) -> Result<TakeoverSession, String> {
        if occurred_at_unix_ms < 0 || !expected.can_transition_to(next) {
            return Err("invalid takeover state transition or timestamp".into());
        }
        if detail.is_some_and(|value| value.trim() != value || value.chars().any(char::is_control))
        {
            return Err("takeover detail contains invalid text".into());
        }
        let current = self
            .read_takeover_session(session_id)?
            .ok_or_else(|| "takeover session does not exist".to_string())?;
        if current.state != expected {
            return Err("takeover state compare-and-set conflict".into());
        }
        if current.state != TakeoverState::OnAir
            && current.state != TakeoverState::ReturnRequested
            && occurred_at_unix_ms >= current.expires_at_unix_ms
            && next != TakeoverState::Failed
        {
            return Err("takeover intent has expired".into());
        }
        let mut updated = current.clone();
        updated.state = next;
        if next == TakeoverState::OnAir {
            updated.taken_at_unix_ms = Some(occurred_at_unix_ms);
        }
        if next == TakeoverState::Completed {
            updated.returned_at_unix_ms = Some(occurred_at_unix_ms);
        }
        if next == TakeoverState::Failed {
            updated.failure_detail = detail.map(str::to_owned);
        }
        updated.validate()?;
        if current.authority_revision != expected_authority_revision {
            return Err("takeover authority compare-and-set conflict".into());
        }
        updated.authority_revision = expected_authority_revision.saturating_add(1);
        updated.operator_id = current.operator_id.clone();
        if actor_id.trim().is_empty()
            || actor_id.chars().any(char::is_control)
            || actor_id.len() > 120
        {
            return Err("takeover actor identity is invalid".into());
        }
        let audit = TakeoverAuditEvent {
            audit_id: format!("{}-{}", session_id, updated.authority_revision),
            session_id: updated.session_id.clone(),
            channel_id: updated.channel_id.clone(),
            source_id: updated.source_id.clone(),
            operator_id: actor_id.into(),
            state: next,
            occurred_at_unix_ms,
            detail: detail.map(str::to_owned),
        };
        audit.validate()?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE takeover_sessions SET taken_at_unix_ms=?1, returned_at_unix_ms=?2, authority_revision=?3, state=?4, failure_detail=?5 \
                 WHERE session_id=?6 AND state=?7 AND authority_revision=?8",
            )?;
            bind_i64(
                self.database,
                statement.raw,
                1,
                updated.taken_at_unix_ms.unwrap_or(-1),
            )?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                updated.returned_at_unix_ms.unwrap_or(-1),
            )?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(updated.authority_revision, "authority revision")?,
            )?;
            let next_text = c_string(next.as_str(), "takeover state")?;
            bind_text(self.database, statement.raw, 4, &next_text)?;
            let failure = c_string(
                updated.failure_detail.as_deref().unwrap_or(""),
                "takeover failure",
            )?;
            bind_text(self.database, statement.raw, 5, &failure)?;
            let id = c_string(session_id, "takeover session identity")?;
            bind_text(self.database, statement.raw, 6, &id)?;
            let expected_text = c_string(expected.as_str(), "takeover state")?;
            bind_text(self.database, statement.raw, 7, &expected_text)?;
            bind_i64(
                self.database,
                statement.raw,
                8,
                checked_i64(expected_authority_revision, "authority revision")?,
            )?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("takeover state compare-and-set conflict".into());
            }
            let authority = self.prepare(
                "UPDATE takeover_authority SET revision=?1 WHERE channel_id=?2 AND revision=?3",
            )?;
            bind_i64(
                self.database,
                authority.raw,
                1,
                checked_i64(updated.authority_revision, "authority revision")?,
            )?;
            let channel_id = c_string(&updated.channel_id, "takeover channel identity")?;
            bind_text(self.database, authority.raw, 2, &channel_id)?;
            bind_i64(
                self.database,
                authority.raw,
                3,
                checked_i64(expected_authority_revision, "authority revision")?,
            )?;
            expect_done(self.database, authority.raw)?;
            if self.changes() != 1 {
                return Err("takeover authority compare-and-set conflict".into());
            }
            self.insert_takeover_audit(&audit)?;
            Ok(())
        })();
        match outcome {
            Ok(()) => {
                self.execute("COMMIT")?;
                Ok(updated)
            }
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn list_takeover_audit(&self, session_id: &str) -> Result<Vec<TakeoverAuditEvent>, String> {
        let statement = self.prepare(
            "SELECT audit_id, session_id, channel_id, source_id, operator_id, state, occurred_at_unix_ms, detail \
             FROM takeover_audit_events WHERE session_id=?1 ORDER BY occurred_at_unix_ms, audit_id",
        )?;
        let session_id = c_string(session_id, "takeover session identity")?;
        bind_text(self.database, statement.raw, 1, &session_id)?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_takeover_audit_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn insert_takeover_session(&self, session: &TakeoverSession) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO takeover_sessions(session_id,channel_id,source_id,operator_id,reason,requested_at_unix_ms,\
             taken_at_unix_ms,returned_at_unix_ms,source_revision,authority_revision,expires_at_unix_ms,state,failure_detail) \
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",
        )?;
        let session_id = c_string(&session.session_id, "takeover session identity")?;
        let channel_id = c_string(&session.channel_id, "takeover channel identity")?;
        let source_id = c_string(&session.source_id, "takeover source identity")?;
        let operator_id = c_string(&session.operator_id, "takeover operator")?;
        let reason = c_string(session.reason.as_deref().unwrap_or(""), "takeover reason")?;
        let state = c_string(session.state.as_str(), "takeover state")?;
        bind_text(self.database, statement.raw, 1, &session_id)?;
        bind_text(self.database, statement.raw, 2, &channel_id)?;
        bind_text(self.database, statement.raw, 3, &source_id)?;
        bind_text(self.database, statement.raw, 4, &operator_id)?;
        bind_text(self.database, statement.raw, 5, &reason)?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            session.requested_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            session.taken_at_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            8,
            session.returned_at_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            9,
            checked_i64(session.source_revision, "source revision")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            checked_i64(session.authority_revision, "authority revision")?,
        )?;
        bind_i64(self.database, statement.raw, 11, session.expires_at_unix_ms)?;
        bind_text(self.database, statement.raw, 12, &state)?;
        let failure = c_string(
            session.failure_detail.as_deref().unwrap_or(""),
            "takeover failure",
        )?;
        bind_text(self.database, statement.raw, 13, &failure)?;
        expect_done(self.database, statement.raw)
    }

    fn insert_takeover_audit(&self, audit: &TakeoverAuditEvent) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO takeover_audit_events(audit_id,session_id,channel_id,source_id,operator_id,state,occurred_at_unix_ms,detail) \
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
        )?;
        let audit_id = c_string(&audit.audit_id, "takeover audit identity")?;
        let session_id = c_string(&audit.session_id, "takeover session identity")?;
        let channel_id = c_string(&audit.channel_id, "takeover channel identity")?;
        let source_id = c_string(&audit.source_id, "takeover source identity")?;
        let operator_id = c_string(&audit.operator_id, "takeover operator")?;
        let state = c_string(audit.state.as_str(), "takeover state")?;
        bind_text(self.database, statement.raw, 1, &audit_id)?;
        bind_text(self.database, statement.raw, 2, &session_id)?;
        bind_text(self.database, statement.raw, 3, &channel_id)?;
        bind_text(self.database, statement.raw, 4, &source_id)?;
        bind_text(self.database, statement.raw, 5, &operator_id)?;
        bind_text(self.database, statement.raw, 6, &state)?;
        bind_i64(self.database, statement.raw, 7, audit.occurred_at_unix_ms)?;
        let detail = c_string(audit.detail.as_deref().unwrap_or(""), "takeover detail")?;
        bind_text(self.database, statement.raw, 8, &detail)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_profile(&self) -> Result<Option<StationProfileDocument>, String> {
        let statement = self.prepare(
            "SELECT station_id, display_name, timezone, revision FROM station_profile WHERE singleton=1",
        )?;
        // SAFETY: the prepared statement is live.
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(StationProfileDocument {
                profile: StationProfile {
                    station_id: column_text(statement.raw, 0)?,
                    display_name: column_text(statement.raw, 1)?,
                    timezone: column_text(statement.raw, 2)?,
                },
                revision: u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 3) })
                    .map_err(|_| "database returned an invalid profile revision".to_string())?,
            })),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_media_asset(&self, asset: &MediaAsset) -> Result<(), String> {
        asset.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO media_assets(asset_id, media_path, duration_ms, readiness) VALUES(?1, ?2, ?3, ?4) \
             ON CONFLICT(asset_id) DO UPDATE SET media_path=excluded.media_path, duration_ms=excluded.duration_ms, readiness=excluded.readiness",
        )?;
        let asset_id = c_string(&asset.asset_id, "asset identity")?;
        let media_path = c_string(&asset.media_path, "media path")?;
        let readiness = c_string(readiness_name(asset.readiness), "asset readiness")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        bind_text(self.database, statement.raw, 2, &media_path)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            checked_i64(asset.duration_ms, "asset duration")?,
        )?;
        bind_text(self.database, statement.raw, 4, &readiness)?;
        expect_done(self.database, statement.raw)
    }

    pub fn create_ingest_job(&self, job: &MediaIngestJob) -> Result<bool, String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        if job.status != IngestJobStatus::Pending
            || job.progress_percent != 0
            || job.started_at_unix_ms.is_some()
            || job.completed_at_unix_ms.is_some()
            || job.error_detail.is_some()
            || job.asset_id.is_some()
            || job.attempts != 0
            || job.next_attempt_at_unix_ms.is_some()
        {
            return Err("new ingest jobs must begin in the pending state".into());
        }
        let statement = self.prepare(
            "INSERT OR IGNORE INTO media_ingest_jobs(job_id, source_kind, source_path, status, progress_percent, \
             created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms) \
             VALUES(?1, ?2, ?3, 'pending', 0, ?4, -1, -1, '', '', 0, -1)",
        )?;
        let job_id = c_string(&job.job_id, "ingest job identity")?;
        let source_kind = c_string(
            ingest_source_kind_name(job.source_kind),
            "ingest source kind",
        )?;
        let source_path = c_string(&job.source_path, "ingest source path")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        bind_text(self.database, statement.raw, 2, &source_kind)?;
        bind_text(self.database, statement.raw, 3, &source_path)?;
        bind_i64(self.database, statement.raw, 4, job.created_at_unix_ms)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    pub fn read_ingest_job(&self, job_id: &str) -> Result<Option<MediaIngestJob>, String> {
        let statement = self.prepare(
             "SELECT job_id, source_kind, source_path, status, progress_percent, created_at_unix_ms, \
             started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms \
             FROM media_ingest_jobs WHERE job_id=?1",
        )?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_ingest_job_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn recover_interrupted_ingest_jobs(&self) -> Result<u64, String> {
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             error_detail='', next_attempt_at_unix_ms=-1 WHERE status='running'",
        )?;
        expect_done(self.database, statement.raw)?;
        u64::try_from(self.changes()).map_err(|_| "SQLite returned invalid changes".into())
    }

    pub fn claim_next_ingest_job(
        &self,
        started_at_unix_ms: i64,
    ) -> Result<Option<MediaIngestJob>, String> {
        if started_at_unix_ms < 0 {
            return Err("ingest start time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT job_id FROM media_ingest_jobs WHERE status='pending' \
                 AND (next_attempt_at_unix_ms=-1 OR next_attempt_at_unix_ms<=?1) \
                 ORDER BY created_at_unix_ms, job_id LIMIT 1",
            )?;
            bind_i64(self.database, statement.raw, 1, started_at_unix_ms)?;
            let job_id = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => column_text(statement.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            drop(statement);
            let update = self.prepare(
                "UPDATE media_ingest_jobs SET status='running', progress_percent=10, started_at_unix_ms=?1, \
                 completed_at_unix_ms=-1, error_detail='', asset_id='', attempts=attempts+1, next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?2 AND status='pending'",
            )?;
            let id = c_string(&job_id, "ingest job identity")?;
            bind_i64(self.database, update.raw, 1, started_at_unix_ms)?;
            bind_text(self.database, update.raw, 2, &id)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("ingest job was claimed concurrently".into());
            }
            self.read_ingest_job(&job_id)?
                .ok_or_else(|| "claimed ingest job disappeared".into())
                .map(Some)
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn complete_ingest_job(
        &self,
        job_id: &str,
        asset: &MediaAsset,
        loudness: &AssetLoudnessAnalysis,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0 {
            return Err("ingest completion time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.put_media_asset(asset)?;
            self.put_asset_loudness(loudness)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='completed', progress_percent=100, completed_at_unix_ms=?1, \
                 error_detail='', asset_id=?2 WHERE job_id=?3 AND status='running'",
            )?;
            let asset_id = c_string(&asset.asset_id, "asset identity")?;
            let job_id = c_string(job_id, "ingest job identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &asset_id)?;
            bind_text(self.database, statement.raw, 3, &job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not running".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn fail_ingest_job(
        &self,
        job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("ingest failure time or detail is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='failed', completed_at_unix_ms=?1, error_detail=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "ingest error detail")?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &detail)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("ingest job is not running".into())
        }
    }

    pub fn retry_ingest_job(
        &self,
        job_id: &str,
        next_attempt_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if next_attempt_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("ingest retry schedule is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, error_detail=?1, asset_id='', next_attempt_at_unix_ms=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "ingest retry detail")?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_text(self.database, statement.raw, 1, &detail)?;
        bind_i64(self.database, statement.raw, 2, next_attempt_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("ingest job is not running".into())
        }
    }

    pub fn create_transcode_job(&self, job: &TranscodeJob) -> Result<bool, String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        if job.status != TranscodeJobStatus::Pending
            || job.progress_percent != 0
            || job.started_at_unix_ms.is_some()
            || job.completed_at_unix_ms.is_some()
            || job.output_asset_id.is_some()
            || job.error_detail.is_some()
            || job.attempts != 0
            || job.next_attempt_at_unix_ms.is_some()
        {
            return Err("new transcode jobs must begin in the pending state".into());
        }
        let statement = self.prepare(
            "INSERT OR IGNORE INTO transcode_jobs(job_id, ingest_job_id, source_asset_id, source_path, \
             output_format, status, progress_percent, created_at_unix_ms, started_at_unix_ms, \
             completed_at_unix_ms, output_asset_id, error_detail, attempts, next_attempt_at_unix_ms) \
             VALUES(?1, ?2, ?3, ?4, ?5, 'pending', 0, ?6, -1, -1, '', '', 0, -1)",
        )?;
        let values = [
            c_string(&job.job_id, "transcode job identity")?,
            c_string(&job.ingest_job_id, "transcode ingest identity")?,
            c_string(&job.source_asset_id, "transcode source identity")?,
            c_string(&job.source_path, "transcode source path")?,
            c_string(transcode_format_name(job.output_format), "transcode format")?,
        ];
        for (offset, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (offset + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 6, job.created_at_unix_ms)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    pub fn read_transcode_job(&self, job_id: &str) -> Result<Option<TranscodeJob>, String> {
        let statement = self.prepare(
            "SELECT job_id, ingest_job_id, source_asset_id, source_path, output_format, status, \
             progress_percent, created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, \
             output_asset_id, error_detail, attempts, next_attempt_at_unix_ms \
             FROM transcode_jobs WHERE job_id=?1",
        )?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_transcode_job_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_transcode_jobs(&self) -> Result<Vec<TranscodeJob>, String> {
        let statement = self.prepare(
            "SELECT job_id, ingest_job_id, source_asset_id, source_path, output_format, status, \
             progress_percent, created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, \
             output_asset_id, error_detail, attempts, next_attempt_at_unix_ms \
             FROM transcode_jobs ORDER BY created_at_unix_ms DESC, job_id",
        )?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(read_transcode_job_row(statement.raw)?),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn recover_interrupted_transcode_jobs(&self) -> Result<u64, String> {
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, output_asset_id='', error_detail='interrupted before completion', \
             next_attempt_at_unix_ms=-1 WHERE status='running'",
        )?;
        expect_done(self.database, statement.raw)?;
        u64::try_from(self.changes()).map_err(|_| "SQLite returned invalid changes".into())
    }

    pub fn claim_next_transcode_job(
        &self,
        started_at_unix_ms: i64,
    ) -> Result<Option<TranscodeJob>, String> {
        if started_at_unix_ms < 0 {
            return Err("transcode start time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT job_id FROM transcode_jobs WHERE status='pending' \
                 AND (next_attempt_at_unix_ms=-1 OR next_attempt_at_unix_ms<=?1) \
                 ORDER BY created_at_unix_ms, job_id LIMIT 1",
            )?;
            bind_i64(self.database, statement.raw, 1, started_at_unix_ms)?;
            let job_id = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => column_text(statement.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            drop(statement);
            let update = self.prepare(
                "UPDATE transcode_jobs SET status='running', progress_percent=5, started_at_unix_ms=?1, \
                 completed_at_unix_ms=-1, output_asset_id='', error_detail='', attempts=attempts+1, \
                 next_attempt_at_unix_ms=-1 WHERE job_id=?2 AND status='pending'",
            )?;
            let id = c_string(&job_id, "transcode job identity")?;
            bind_i64(self.database, update.raw, 1, started_at_unix_ms)?;
            bind_text(self.database, update.raw, 2, &id)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("transcode job was claimed concurrently".into());
            }
            self.read_transcode_job(&job_id)?
                .ok_or_else(|| "claimed transcode job disappeared".into())
                .map(Some)
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn update_transcode_progress(
        &self,
        job_id: &str,
        progress_percent: u8,
    ) -> Result<(), String> {
        if !(1..100).contains(&progress_percent) {
            return Err("running transcode progress must be between 1 and 99".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET progress_percent=?1 WHERE job_id=?2 AND status='running' \
             AND progress_percent<?1",
        )?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, i64::from(progress_percent))?;
        bind_text(self.database, statement.raw, 2, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or progress did not advance".into())
        }
    }

    pub fn complete_transcode_job(
        &self,
        job_id: &str,
        output_asset_id: &str,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0 || !is_sha256_identity(output_asset_id) {
            return Err("transcode completion time or output identity is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='completed', progress_percent=100, \
             completed_at_unix_ms=?1, output_asset_id=?2, error_detail='', next_attempt_at_unix_ms=-1 \
             WHERE job_id=?3 AND status='running' AND started_at_unix_ms<=?1",
        )?;
        let output_asset_id = c_string(output_asset_id, "transcode output identity")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &output_asset_id)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or completion predates its start".into())
        }
    }

    pub fn fail_transcode_job(
        &self,
        job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("transcode failure time or detail is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='failed', completed_at_unix_ms=?1, error_detail=?2, \
             output_asset_id='', next_attempt_at_unix_ms=-1 WHERE job_id=?3 AND status='running' \
             AND started_at_unix_ms<=?1",
        )?;
        let detail = c_string(error_detail, "transcode error detail")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &detail)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or failure predates its start".into())
        }
    }

    pub fn retry_transcode_job(
        &self,
        job_id: &str,
        next_attempt_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if next_attempt_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("transcode retry schedule is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, output_asset_id='', error_detail=?1, next_attempt_at_unix_ms=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "transcode retry detail")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_text(self.database, statement.raw, 1, &detail)?;
        bind_i64(self.database, statement.raw, 2, next_attempt_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running".into())
        }
    }

    pub fn handoff_ingest_to_transcode(&self, job: &TranscodeJob) -> Result<(), String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if !self.create_transcode_job(job)? {
                return Err("transcode job or ingest handoff already exists".into());
            }
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='awaiting_transcode', progress_percent=60, \
                 completed_at_unix_ms=-1, error_detail='', asset_id='', next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?1 AND status='running'",
            )?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_text(self.database, statement.raw, 1, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not running".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn complete_transcode_and_ingest(
        &self,
        transcode_job_id: &str,
        output: &MediaAsset,
        loudness: &AssetLoudnessAnalysis,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        output.validate().map_err(|error| format!("{error:?}"))?;
        loudness.validate().map_err(|error| format!("{error:?}"))?;
        if output.readiness != AssetReadiness::Ready
            || loudness.asset_id != output.asset_id
            || loudness.status != LoudnessGateStatus::Ok
        {
            return Err(
                "transcode output and passing loudness identities must match and be ready".into(),
            );
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let job = self
                .read_transcode_job(transcode_job_id)?
                .ok_or_else(|| "transcode job does not exist".to_string())?;
            if job.status != TranscodeJobStatus::Running {
                return Err("transcode job is not running".into());
            }
            self.put_media_asset(output)?;
            self.put_asset_loudness(loudness)?;
            self.complete_transcode_job(transcode_job_id, &output.asset_id, completed_at_unix_ms)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='completed', progress_percent=100, \
                 completed_at_unix_ms=?1, error_detail='', asset_id=?2, next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?3 AND status='awaiting_transcode' AND started_at_unix_ms<=?1",
            )?;
            let output_asset_id = c_string(&output.asset_id, "transcode output identity")?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &output_asset_id)?;
            bind_text(self.database, statement.raw, 3, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not awaiting this transcode".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn fail_transcode_and_ingest(
        &self,
        transcode_job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let job = self
                .read_transcode_job(transcode_job_id)?
                .ok_or_else(|| "transcode job does not exist".to_string())?;
            self.fail_transcode_job(transcode_job_id, completed_at_unix_ms, error_detail)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='failed', completed_at_unix_ms=?1, \
                 error_detail=?2, asset_id='', next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?3 AND status='awaiting_transcode' AND started_at_unix_ms<=?1",
            )?;
            let detail = c_string(error_detail, "transcode error detail")?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &detail)?;
            bind_text(self.database, statement.raw, 3, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not awaiting this transcode".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn put_channel(&self, channel: &ChannelConfiguration) -> Result<(), String> {
        self.put_channel_with_srt_passphrases(channel, &HashMap::new())
    }

    pub fn stage_headend_profile(
        &self,
        channel_id: &str,
        request: &HeadendProfileApplyRequest,
    ) -> Result<HeadendProfileApplication, HeadendProfileWriteError> {
        self.execute("BEGIN IMMEDIATE")
            .map_err(HeadendProfileWriteError::Storage)?;
        let outcome = (|| {
            let channel = self
                .list_channels()
                .map_err(HeadendProfileWriteError::Storage)?
                .into_iter()
                .find(|channel| channel.channel_id == channel_id)
                .ok_or(HeadendProfileWriteError::NotFound)?;
            if channel.enabled {
                return Err(HeadendProfileWriteError::MustBeStopped);
            }
            let application = apply_headend_profile(&channel, request)
                .map_err(|error| HeadendProfileWriteError::Invalid(format!("{error:?}")))?;
            self.put_channel_transaction(&application.channel, &HashMap::new())
                .map_err(HeadendProfileWriteError::Storage)?;
            Ok(application)
        })();
        match outcome {
            Ok(application) => {
                if let Err(message) = self.execute("COMMIT") {
                    let _ = self.execute("ROLLBACK");
                    return Err(HeadendProfileWriteError::Storage(message));
                }
                Ok(application)
            }
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn put_channel_with_srt_passphrases(
        &self,
        channel: &ChannelConfiguration,
        passphrases: &HashMap<String, String>,
    ) -> Result<(), String> {
        channel.validate().map_err(|error| format!("{error:?}"))?;
        validate_channel_passphrases(channel, passphrases)?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = self.put_channel_transaction(channel, passphrases);
        self.finish_transaction(outcome)
    }

    pub fn put_channel_with_srt_passphrases_if_current(
        &self,
        channel: &ChannelConfiguration,
        passphrases: &HashMap<String, String>,
        expected: Option<&ChannelConfiguration>,
    ) -> Result<(), ChannelWriteError> {
        channel
            .validate()
            .map_err(|error| ChannelWriteError::Storage(format!("{error:?}")))?;
        validate_channel_passphrases(channel, passphrases).map_err(ChannelWriteError::Storage)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(ChannelWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .list_channels()
                .map_err(ChannelWriteError::Storage)?
                .into_iter()
                .find(|current| current.channel_id == channel.channel_id);
            if current.as_ref() != expected {
                return Err(ChannelWriteError::Conflict);
            }
            self.put_channel_transaction(channel, passphrases)
                .map_err(ChannelWriteError::Storage)
        })();
        match outcome {
            Ok(()) => self.execute("COMMIT").map_err(|message| {
                let _ = self.execute("ROLLBACK");
                ChannelWriteError::Storage(message)
            }),
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    fn put_channel_transaction(
        &self,
        channel: &ChannelConfiguration,
        passphrases: &HashMap<String, String>,
    ) -> Result<(), String> {
        let prior_passphrases = self.list_encrypted_srt_passphrases(&channel.channel_id)?;
        let statement = self.prepare(
            "INSERT INTO channels(channel_id, display_name, udp_destination, output_profile, enabled) VALUES(?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(channel_id) DO UPDATE SET display_name=excluded.display_name, udp_destination=excluded.udp_destination, output_profile=excluded.output_profile, enabled=excluded.enabled",
        )?;
        let channel_id = c_string(&channel.channel_id, "channel identity")?;
        let display_name = c_string(&channel.display_name, "channel display name")?;
        let destination = c_string(
            &channel.output_sinks[0].destination,
            "primary output destination",
        )?;
        let output_profile = c_string(
            channel.output_profile.storage_name(),
            "channel output profile",
        )?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_text(self.database, statement.raw, 2, &display_name)?;
        bind_text(self.database, statement.raw, 3, &destination)?;
        bind_text(self.database, statement.raw, 4, &output_profile)?;
        bind_i64(self.database, statement.raw, 5, i64::from(channel.enabled))?;
        expect_done(self.database, statement.raw)?;
        drop(statement);
        let delete = self.prepare("DELETE FROM channel_output_sinks WHERE channel_id=?1")?;
        bind_text(self.database, delete.raw, 1, &channel_id)?;
        expect_done(self.database, delete.raw)?;
        drop(delete);
        for (sink_order, sink) in channel.output_sinks.iter().enumerate() {
            let insert = self.prepare(
                "INSERT INTO channel_output_sinks(channel_id, sink_id, label, kind, destination, loudness_target_milli, required, enabled, headend_profile, headend_muxrate_kbps, headend_packet_size, sink_order) \
                 VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            )?;
            let sink_id = c_string(&sink.sink_id, "output sink identity")?;
            let label = c_string(&sink.label, "output sink label")?;
            let kind = c_string(output_sink_kind_name(sink.kind), "output sink kind")?;
            let destination = c_string(&sink.destination, "output sink destination")?;
            bind_text(self.database, insert.raw, 1, &channel_id)?;
            bind_text(self.database, insert.raw, 2, &sink_id)?;
            bind_text(self.database, insert.raw, 3, &label)?;
            bind_text(self.database, insert.raw, 4, &kind)?;
            bind_text(self.database, insert.raw, 5, &destination)?;
            bind_i64(
                self.database,
                insert.raw,
                6,
                (sink.loudness_target_lufs * 1_000.0).round() as i64,
            )?;
            bind_i64(self.database, insert.raw, 7, i64::from(sink.required))?;
            bind_i64(self.database, insert.raw, 8, i64::from(sink.enabled))?;
            let headend_profile = c_string(
                sink.headend
                    .as_ref()
                    .map_or("", |headend| headend.profile_id.storage_name()),
                "headend profile identity",
            )?;
            bind_text(self.database, insert.raw, 9, &headend_profile)?;
            bind_i64(
                self.database,
                insert.raw,
                10,
                i64::from(
                    sink.headend
                        .as_ref()
                        .map_or(0, |headend| headend.muxrate_kbps),
                ),
            )?;
            bind_i64(
                self.database,
                insert.raw,
                11,
                i64::from(
                    sink.headend
                        .as_ref()
                        .map_or(0, |headend| headend.packet_size_bytes),
                ),
            )?;
            bind_i64(
                self.database,
                insert.raw,
                12,
                i64::try_from(sink_order)
                    .map_err(|_| "channel output sink order exceeds database range")?,
            )?;
            expect_done(self.database, insert.raw)?;
            drop(insert);
            if sink.kind == OutputSinkKind::Srt && sink.encryption_configured {
                let encrypted = if let Some(passphrase) = passphrases.get(&sink.sink_id) {
                    protect_local_machine(passphrase.as_bytes())?
                } else {
                    prior_passphrases
                        .get(&sink.sink_id)
                        .cloned()
                        .ok_or("an encrypted SRT output requires a write-only passphrase")?
                };
                let secret = self.prepare(
                    "INSERT INTO srt_output_secrets(channel_id, sink_id, encrypted_passphrase) \
                     VALUES(?1, ?2, ?3)",
                )?;
                bind_text(self.database, secret.raw, 1, &channel_id)?;
                bind_text(self.database, secret.raw, 2, &sink_id)?;
                bind_blob(self.database, secret.raw, 3, &encrypted)?;
                expect_done(self.database, secret.raw)?;
            }
        }
        Ok(())
    }

    fn list_encrypted_srt_passphrases(
        &self,
        channel_id: &str,
    ) -> Result<HashMap<String, Vec<u8>>, String> {
        let statement = self.prepare(
            "SELECT sink_id, encrypted_passphrase FROM srt_output_secrets WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut secrets = HashMap::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    secrets.insert(
                        column_text(statement.raw, 0)?,
                        column_blob(statement.raw, 1)?,
                    );
                }
                SQLITE_DONE => return Ok(secrets),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_channels(&self) -> Result<Vec<ChannelConfiguration>, String> {
        let statement = self.prepare(
            "SELECT channel_id, display_name, output_profile, enabled FROM channels ORDER BY channel_id",
        )?;
        let mut channels = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => channels.push(ChannelConfiguration {
                    channel_id: column_text(statement.raw, 0)?,
                    display_name: column_text(statement.raw, 1)?,
                    output_profile: OutputProfileId::from_storage_name(&column_text(
                        statement.raw,
                        2,
                    )?)
                    .map_err(|error| format!("{error:?}"))?,
                    output_sinks: self
                        .list_channel_output_sinks(&column_text(statement.raw, 0)?)?,
                    enabled: unsafe { sqlite3_column_int64(statement.raw, 3) } == 1,
                }),
                SQLITE_DONE => return Ok(channels),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn load_cg_snapshot_inputs(
        &self,
        channel_id: &str,
        generated_at_unix_ms: i64,
    ) -> Result<CgSnapshotInputs, CgSnapshotReadError> {
        if channel_id.trim().is_empty() {
            return Err(CgSnapshotReadError::InvalidChannel);
        }
        if generated_at_unix_ms < 0 {
            return Err(CgSnapshotReadError::InvalidTimestamp);
        }
        self.execute("BEGIN DEFERRED")
            .map_err(CgSnapshotReadError::Storage)?;
        let outcome = (|| {
            let channel_exists = self
                .list_channels()
                .map_err(CgSnapshotReadError::Storage)?
                .into_iter()
                .any(|channel| channel.channel_id == channel_id);
            if !channel_exists {
                return Err(CgSnapshotReadError::ChannelNotFound);
            }
            let board = self
                .read_cg_board(channel_id)
                .map_err(CgSnapshotReadError::Storage)?
                .ok_or(CgSnapshotReadError::BoardNotFound)?;
            if !board.active {
                return Err(CgSnapshotReadError::BoardInactive);
            }
            let template = built_in_template(&board.template_id).ok_or_else(|| {
                CgSnapshotReadError::InvalidData("CG board template is unavailable".into())
            })?;
            let zones = self
                .list_cg_zones(&board.board_id)
                .map_err(CgSnapshotReadError::Storage)?;
            let feed_sources = self
                .list_cg_feed_sources(channel_id)
                .map_err(CgSnapshotReadError::Storage)?;
            let mut feed_cache = Vec::new();
            let mut approvals = Vec::new();
            for feed in &feed_sources {
                if let Some(cache) = self
                    .read_cg_feed_cache(&feed.feed_source_id)
                    .map_err(CgSnapshotReadError::Storage)?
                {
                    feed_cache.push(cache);
                }
                approvals.extend(
                    self.list_cg_feed_item_approvals(&feed.feed_source_id)
                        .map_err(CgSnapshotReadError::Storage)?,
                );
            }
            let bulletins = self
                .list_airable_cg_bulletins(channel_id, generated_at_unix_ms)
                .map_err(CgSnapshotReadError::Storage)?;
            let referenced_image_ids = zones
                .iter()
                .filter_map(|zone| match &zone.content_source {
                    CgContentSource::Image { asset_id } => Some(asset_id.as_str()),
                    _ => None,
                })
                .collect::<BTreeSet<_>>();
            let image_assets = referenced_image_ids
                .iter()
                .map(|asset_id| {
                    self.read_cg_image_asset(asset_id)
                        .map_err(CgSnapshotReadError::Storage)
                })
                .collect::<Result<Vec<_>, _>>()?
                .into_iter()
                .flatten()
                .map(|asset| self.verify_cg_image_asset(asset))
                .collect();
            Ok(CgSnapshotInputs {
                board,
                template,
                zones,
                feed_sources,
                feed_cache,
                approvals,
                image_assets,
                bulletins,
                generated_at_unix_ms,
            })
        })();
        match outcome {
            Ok(inputs) => match self.execute("COMMIT") {
                Ok(()) => Ok(inputs),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CgSnapshotReadError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn cg_image_verification_count(&self) -> u64 {
        self.cg_image_verification_count.load(Ordering::Relaxed)
    }

    fn verify_cg_image_asset(&self, mut asset: CgImageAsset) -> CgImageAsset {
        if asset.readiness != CgImageReadiness::Ready {
            return asset;
        }
        let image_root = match self.validated_cg_image_root() {
            Ok(root) => root,
            Err(_) => {
                asset.readiness = CgImageReadiness::Rejected;
                return asset;
            }
        };
        let path = match managed_cg_image_path(&image_root, &asset.asset_id, &asset.mime_type) {
            Ok(path) => path,
            Err(_) => {
                asset.readiness = CgImageReadiness::Rejected;
                return asset;
            }
        };
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata)
                if metadata.is_file()
                    && metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT == 0 =>
            {
                metadata
            }
            _ => {
                self.cg_image_verification_cache
                    .lock()
                    .unwrap()
                    .remove(&asset.asset_id);
                asset.readiness = CgImageReadiness::Missing;
                return asset;
            }
        };
        let byte_len = metadata.len();
        let modified = metadata.modified().ok();
        if let Some(cached) = self
            .cg_image_verification_cache
            .lock()
            .unwrap()
            .get(&asset.asset_id)
            && cached.byte_len == byte_len
            && cached.modified == modified
            && cached.checked_at.elapsed() <= std::time::Duration::from_secs(60)
            && cached.authority == asset
        {
            return cached.asset.clone();
        }
        self.cg_image_verification_count
            .fetch_add(1, Ordering::Relaxed);
        let authority = asset.clone();
        let verified = match fs::read(&path)
            .map_err(|error| error.to_string())
            .and_then(|bytes| {
                inspect_cg_image(&asset.asset_id, &bytes).map_err(|error| error.to_string())
            }) {
            Ok(candidate) if candidate == asset => asset,
            _ => {
                asset.readiness = CgImageReadiness::Rejected;
                asset
            }
        };
        let mut cache = self.cg_image_verification_cache.lock().unwrap();
        if cache.len() >= CG_IMAGE_VERIFICATION_CACHE_LIMIT
            && !cache.contains_key(&verified.asset_id)
            && let Some(oldest) = cache
                .iter()
                .min_by_key(|(_, entry)| entry.checked_at)
                .map(|(asset_id, _)| asset_id.clone())
        {
            cache.remove(&oldest);
        }
        cache.insert(
            verified.asset_id.clone(),
            CachedCgImageVerification {
                byte_len,
                modified,
                checked_at: Instant::now(),
                authority,
                asset: verified.clone(),
            },
        );
        verified
    }

    fn validated_cg_image_root(&self) -> Result<PathBuf, String> {
        let current = fs::canonicalize(&self.cg_image_root)
            .map_err(|error| format!("canonicalize CG image root: {error}"))?;
        if current != self.cg_image_root {
            return Err("CG image root authority changed after station startup".into());
        }
        Ok(current)
    }

    pub fn write_cg_board(
        &self,
        board: &CgBoard,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<CgBoard, CgWriteError> {
        board.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.channel_id != board.channel_id
            || audit.board_id.as_deref() != Some(board.board_id.as_str())
            || audit.operator_id.trim().is_empty()
        {
            return Err(CgWriteError::Invalid(
                "CG board audit evidence does not identify the proposed board.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_cg_board(&board.channel_id)
                .map_err(CgWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CgWriteError::Conflict(current_revision));
            }
            if board.revision != expected_revision.saturating_add(1) {
                return Err(CgWriteError::Invalid(
                    "CG board revision must be exactly one greater than the expected revision.",
                ));
            }
            match current {
                None if audit.event_kind != CgAuditEventKind::BoardCreated => {
                    return Err(CgWriteError::Invalid(
                        "A new CG board requires a board-created audit event.",
                    ));
                }
                Some(existing)
                    if audit.event_kind != CgAuditEventKind::BoardUpdated
                        || existing.board_id != board.board_id
                        || existing.created_at_unix_ms != board.created_at_unix_ms
                        || existing.created_by != board.created_by
                        || board.updated_at_unix_ms < existing.updated_at_unix_ms =>
                {
                    return Err(CgWriteError::Invalid(
                        "A CG board update cannot replace identity, provenance, or move time backward.",
                    ));
                }
                _ => {}
            }
            if let Some(asset_id) = &board.background_audio_asset_id {
                let asset = self
                    .read_media_asset(asset_id)
                    .map_err(CgWriteError::Storage)?
                    .ok_or(CgWriteError::NotFound)?;
                if asset.readiness != AssetReadiness::Ready {
                    return Err(CgWriteError::Invalid(
                        "CG background audio must identify an air-ready managed asset.",
                    ));
                }
            }
            let template = built_in_template(&board.template_id).ok_or(CgWriteError::Invalid(
                "CG board refers to an unavailable template.",
            ))?;
            for zone in self
                .list_cg_zones(&board.board_id)
                .map_err(CgWriteError::Storage)?
            {
                let Some(template_zone) = template
                    .zones
                    .iter()
                    .find(|candidate| candidate.template_zone_key == zone.template_zone_key)
                else {
                    return Err(CgWriteError::Invalid(
                        "The selected CG template does not contain an existing configured zone.",
                    ));
                };
                if template_zone.zone_kind != zone.zone_kind {
                    return Err(CgWriteError::Invalid(
                        "The selected CG template changes an existing zone kind.",
                    ));
                }
            }
            self.upsert_cg_board(board).map_err(CgWriteError::Storage)?;
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(board.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_board(&self, channel_id: &str) -> Result<Option<CgBoard>, String> {
        let statement = self.prepare(
            "SELECT board_id, channel_id, template_id, active, background_audio_asset_id, revision, \
             created_at_unix_ms, updated_at_unix_ms, created_by FROM cg_boards WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "CG board channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_board_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn write_cg_zone(
        &self,
        zone: &CgZoneConfig,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<CgZoneConfig, CgWriteError> {
        zone.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.board_id.as_deref() != Some(zone.board_id.as_str()) {
            return Err(CgWriteError::Invalid(
                "CG zone audit evidence does not identify the proposed board.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let board = self
                .read_cg_board_by_id(&zone.board_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if audit.channel_id != board.channel_id {
                return Err(CgWriteError::Invalid(
                    "CG zone audit evidence identifies the wrong channel.",
                ));
            }
            let template = built_in_template(&board.template_id).ok_or(CgWriteError::Invalid(
                "CG board refers to an unavailable template.",
            ))?;
            let template_zone = template
                .zones
                .iter()
                .find(|candidate| candidate.template_zone_key == zone.template_zone_key)
                .ok_or(CgWriteError::Invalid(
                    "CG zone does not belong to the board template.",
                ))?;
            if template_zone.zone_kind != zone.zone_kind {
                return Err(CgWriteError::Invalid(
                    "CG zone kind does not match its template region.",
                ));
            }
            match &zone.content_source {
                CgContentSource::Feed { feed_source_id } => {
                    let feed = self
                        .read_cg_feed_source(feed_source_id)
                        .map_err(CgWriteError::Storage)?
                        .ok_or(CgWriteError::NotFound)?;
                    if feed.channel_id != board.channel_id || !feed.enabled {
                        return Err(CgWriteError::Invalid(
                            "CG zone feed must be enabled and belong to the board channel.",
                        ));
                    }
                }
                CgContentSource::Image { asset_id } => {
                    let asset = self
                        .read_cg_image_asset(asset_id)
                        .map_err(CgWriteError::Storage)?
                        .ok_or(CgWriteError::NotFound)?;
                    if asset.readiness != CgImageReadiness::Ready {
                        return Err(CgWriteError::Invalid(
                            "CG zone image must identify a ready managed asset.",
                        ));
                    }
                }
                _ => {}
            }
            let current = self
                .read_cg_zone(&zone.zone_id)
                .map_err(CgWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CgWriteError::Conflict(current_revision));
            }
            if zone.revision != expected_revision.saturating_add(1) {
                return Err(CgWriteError::Invalid(
                    "CG zone revision must be exactly one greater than the expected revision.",
                ));
            }
            match current {
                None if audit.event_kind != CgAuditEventKind::ZoneCreated => {
                    return Err(CgWriteError::Invalid(
                        "A new CG zone requires a zone-created audit event.",
                    ));
                }
                Some(existing)
                    if audit.event_kind != CgAuditEventKind::ZoneUpdated
                        || existing.board_id != zone.board_id
                        || existing.created_at_unix_ms != zone.created_at_unix_ms
                        || zone.updated_at_unix_ms < existing.updated_at_unix_ms =>
                {
                    return Err(CgWriteError::Invalid(
                        "A CG zone update cannot replace identity, provenance, or move time backward.",
                    ));
                }
                _ => {}
            }
            self.upsert_cg_zone(zone).map_err(CgWriteError::Storage)?;
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(zone.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_zone(&self, zone_id: &str) -> Result<Option<CgZoneConfig>, String> {
        let statement = self.prepare(
            "SELECT zone_id, board_id, template_zone_key, zone_kind, content_source_json, \
             rotation_seconds, approval_required, allowed_tags_json, revision, created_at_unix_ms, \
             updated_at_unix_ms FROM cg_zone_configs WHERE zone_id=?1",
        )?;
        let zone_id = c_string(zone_id, "CG zone identity")?;
        bind_text(self.database, statement.raw, 1, &zone_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_zone_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_cg_zones(&self, board_id: &str) -> Result<Vec<CgZoneConfig>, String> {
        let statement = self.prepare(
            "SELECT zone_id, board_id, template_zone_key, zone_kind, content_source_json, \
             rotation_seconds, approval_required, allowed_tags_json, revision, created_at_unix_ms, \
             updated_at_unix_ms FROM cg_zone_configs WHERE board_id=?1 \
             ORDER BY template_zone_key, zone_id",
        )?;
        let board_id = c_string(board_id, "CG board identity")?;
        bind_text(self.database, statement.raw, 1, &board_id)?;
        let mut zones = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => zones.push(read_cg_zone_row(statement.raw)?),
                SQLITE_DONE => return Ok(zones),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn delete_cg_zone(
        &self,
        zone_id: &str,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<(), CgWriteError> {
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.event_kind != CgAuditEventKind::ZoneRemoved {
            return Err(CgWriteError::Invalid(
                "CG zone deletion requires zone-removed audit evidence.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let zone = self
                .read_cg_zone(zone_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if zone.revision != expected_revision {
                return Err(CgWriteError::Conflict(zone.revision));
            }
            let board = self
                .read_cg_board_by_id(&zone.board_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if audit.board_id.as_deref() != Some(zone.board_id.as_str())
                || audit.channel_id != board.channel_id
                || audit.occurred_at_unix_ms < zone.updated_at_unix_ms
            {
                return Err(CgWriteError::Invalid(
                    "CG zone deletion audit evidence does not match the current zone.",
                ));
            }
            let statement = self
                .prepare("DELETE FROM cg_zone_configs WHERE zone_id=?1 AND revision=?2")
                .map_err(CgWriteError::Storage)?;
            let zone_id = c_string(zone_id, "CG zone identity").map_err(CgWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &zone_id).map_err(CgWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(expected_revision, "CG zone revision")
                    .map_err(CgWriteError::Storage)?,
            )
            .map_err(CgWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
            drop(statement);
            if self.changes() != 1 {
                return Err(CgWriteError::Conflict(zone.revision));
            }
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn list_cg_audit_events(
        &self,
        channel_id: &str,
        limit: u32,
    ) -> Result<Vec<CgAuditEvent>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("CG audit limit must be between 1 and 1000".into());
        }
        let statement = self.prepare(
            "SELECT audit_id, board_id, channel_id, event_kind, operator_id, occurred_at_unix_ms, \
             details_json FROM cg_audit_events WHERE channel_id=?1 \
             ORDER BY occurred_at_unix_ms DESC, audit_id DESC LIMIT ?2",
        )?;
        let channel_id = c_string(channel_id, "CG audit channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_i64(self.database, statement.raw, 2, i64::from(limit))?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_cg_audit_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_cg_feed_source(
        &self,
        feed: &CgFeedSource,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<CgFeedSource, CgWriteError> {
        feed.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.channel_id != feed.channel_id {
            return Err(CgWriteError::Invalid(
                "CG feed audit evidence identifies the wrong channel.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_cg_feed_source(&feed.feed_source_id)
                .map_err(CgWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CgWriteError::Conflict(current_revision));
            }
            if feed.revision != expected_revision.saturating_add(1) {
                return Err(CgWriteError::Invalid(
                    "CG feed revision must be exactly one greater than the expected revision.",
                ));
            }
            match current {
                None if audit.event_kind != CgAuditEventKind::FeedCreated => {
                    return Err(CgWriteError::Invalid(
                        "A new CG feed requires a feed-created audit event.",
                    ));
                }
                Some(existing)
                    if !matches!(
                        audit.event_kind,
                        CgAuditEventKind::FeedUpdated | CgAuditEventKind::FeedRemoved
                    ) || (audit.event_kind == CgAuditEventKind::FeedRemoved && feed.enabled)
                        || existing.channel_id != feed.channel_id
                        || existing.created_at_unix_ms != feed.created_at_unix_ms
                        || existing.created_by != feed.created_by
                        || feed.updated_at_unix_ms < existing.updated_at_unix_ms =>
                {
                    return Err(CgWriteError::Invalid(
                        "A CG feed update cannot replace identity, provenance, move time backward, or forge removal.",
                    ));
                }
                _ => {}
            }
            self.upsert_cg_feed_source(feed)
                .map_err(CgWriteError::Storage)?;
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(feed.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_feed_source(
        &self,
        feed_source_id: &str,
    ) -> Result<Option<CgFeedSource>, String> {
        let statement = self.prepare(
            "SELECT feed_source_id, channel_id, kind, label, source_url, trust_tier, tags_json, refresh_seconds, \
             enabled, revision, created_at_unix_ms, updated_at_unix_ms, created_by, \
             last_fetched_at_unix_ms, last_fetch_error FROM cg_feed_sources WHERE feed_source_id=?1",
        )?;
        let feed_source_id = c_string(feed_source_id, "CG feed identity")?;
        bind_text(self.database, statement.raw, 1, &feed_source_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_feed_source_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_cg_feed_sources(&self, channel_id: &str) -> Result<Vec<CgFeedSource>, String> {
        let statement = self.prepare(
            "SELECT feed_source_id, channel_id, kind, label, source_url, trust_tier, tags_json, refresh_seconds, \
             enabled, revision, created_at_unix_ms, updated_at_unix_ms, created_by, \
             last_fetched_at_unix_ms, last_fetch_error FROM cg_feed_sources WHERE channel_id=?1 \
             ORDER BY label COLLATE NOCASE, feed_source_id",
        )?;
        let channel_id = c_string(channel_id, "CG feed channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut feeds = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => feeds.push(read_cg_feed_source_row(statement.raw)?),
                SQLITE_DONE => return Ok(feeds),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_all_cg_feed_sources(&self) -> Result<Vec<CgFeedSource>, String> {
        let mut feeds = Vec::new();
        for channel in self.list_channels()? {
            feeds.extend(self.list_cg_feed_sources(&channel.channel_id)?);
        }
        Ok(feeds)
    }

    pub fn replace_cg_feed_cache(
        &self,
        feed_source_id: &str,
        fetched_at_unix_ms: i64,
        items: &[CgFeedItem],
        etag: Option<&str>,
        last_modified: Option<&str>,
    ) -> Result<CgFeedCacheEntry, CgWriteError> {
        if fetched_at_unix_ms < 0 {
            return Err(CgWriteError::Invalid(
                "CG feed-cache time cannot be negative.",
            ));
        }
        let feed_source_id_value = feed_source_id.to_string();
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_cg_feed_source(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if !current.enabled {
                return Err(CgWriteError::Invalid(
                    "CG feed cache cannot be replaced for a disabled source.",
                ));
            }
            if fetched_at_unix_ms < current.created_at_unix_ms
                || current
                    .last_fetched_at_unix_ms
                    .is_some_and(|last| fetched_at_unix_ms < last)
            {
                return Err(CgWriteError::Invalid(
                    "CG feed-cache time cannot move backward.",
                ));
            }
            let expires_at_unix_ms = fetched_at_unix_ms
                .checked_add(i64::from(current.refresh_seconds) * 1_000)
                .ok_or(CgWriteError::Invalid(
                    "CG feed-cache expiry exceeds the supported range.",
                ))?;
            let entry = CgFeedCacheEntry {
                feed_source_id: feed_source_id_value.clone(),
                fetched_at_unix_ms,
                expires_at_unix_ms,
                etag: etag.map(str::to_string),
                last_modified: last_modified.map(str::to_string),
                last_error: None,
                items: items.to_vec(),
            };
            entry.validate().map_err(CgWriteError::Invalid)?;
            let source = c_string(feed_source_id, "CG feed-cache source identity")
                .map_err(CgWriteError::Storage)?;
            let delete_items = self
                .prepare("DELETE FROM cg_feed_cache_items WHERE feed_source_id=?1")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, delete_items.raw, 1, &source)
                .map_err(CgWriteError::Storage)?;
            expect_done(self.database, delete_items.raw).map_err(CgWriteError::Storage)?;

            for item in &entry.items {
                let insert = self
                    .prepare(
                        "INSERT INTO cg_feed_cache_items(feed_source_id, item_id, title, summary, \
                         starts_at_unix_ms, url, tags_json, fetched_at_unix_ms) \
                         VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                    )
                    .map_err(CgWriteError::Storage)?;
                let item_id = c_string(&item.item_id, "CG feed item identity")
                    .map_err(CgWriteError::Storage)?;
                let title =
                    c_string(&item.title, "CG feed item title").map_err(CgWriteError::Storage)?;
                let summary = c_string(
                    item.summary.as_deref().unwrap_or_default(),
                    "CG feed item summary",
                )
                .map_err(CgWriteError::Storage)?;
                let url = c_string(item.url.as_deref().unwrap_or_default(), "CG feed item URL")
                    .map_err(CgWriteError::Storage)?;
                let tags = c_string(
                    &serde_json::to_string(&item.tags)
                        .map_err(|error| CgWriteError::Storage(error.to_string()))?,
                    "CG feed item tags",
                )
                .map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 1, &source).map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 2, &item_id).map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 3, &title).map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 4, &summary).map_err(CgWriteError::Storage)?;
                bind_i64(
                    self.database,
                    insert.raw,
                    5,
                    item.starts_at_unix_ms.unwrap_or(-1),
                )
                .map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 6, &url).map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 7, &tags).map_err(CgWriteError::Storage)?;
                bind_i64(self.database, insert.raw, 8, fetched_at_unix_ms)
                    .map_err(CgWriteError::Storage)?;
                expect_done(self.database, insert.raw).map_err(CgWriteError::Storage)?;
            }
            let meta = self
                .prepare(
                    "INSERT INTO cg_feed_cache_meta(feed_source_id, fetched_at_unix_ms, \
                     expires_at_unix_ms, etag, last_modified, last_error) VALUES(?1, ?2, ?3, ?4, ?5, '') \
                     ON CONFLICT(feed_source_id) DO UPDATE SET fetched_at_unix_ms=excluded.fetched_at_unix_ms, \
                     expires_at_unix_ms=excluded.expires_at_unix_ms, etag=excluded.etag, \
                     last_modified=excluded.last_modified, last_error=''",
                )
                .map_err(CgWriteError::Storage)?;
            let etag =
                c_string(etag.unwrap_or_default(), "CG ETag").map_err(CgWriteError::Storage)?;
            let last_modified = c_string(last_modified.unwrap_or_default(), "CG Last-Modified")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, meta.raw, 1, &source).map_err(CgWriteError::Storage)?;
            bind_i64(self.database, meta.raw, 2, fetched_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, meta.raw, 3, expires_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, meta.raw, 4, &etag).map_err(CgWriteError::Storage)?;
            bind_text(self.database, meta.raw, 5, &last_modified).map_err(CgWriteError::Storage)?;
            expect_done(self.database, meta.raw).map_err(CgWriteError::Storage)?;
            let update = self
                .prepare(
                    "UPDATE cg_feed_sources SET last_fetched_at_unix_ms=?1, last_fetch_error='' \
                     WHERE feed_source_id=?2",
                )
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, update.raw, 1, fetched_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, update.raw, 2, &source).map_err(CgWriteError::Storage)?;
            expect_done(self.database, update.raw).map_err(CgWriteError::Storage)?;
            Ok(entry.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn refresh_cg_feed_cache(
        &self,
        feed_source_id: &str,
        fetched_at_unix_ms: i64,
    ) -> Result<CgFeedCacheEntry, CgWriteError> {
        if fetched_at_unix_ms < 0 {
            return Err(CgWriteError::Invalid(
                "CG feed-cache refresh time cannot be negative.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let feed = self
                .read_cg_feed_source(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if !feed.enabled {
                return Err(CgWriteError::Invalid(
                    "CG feed cache cannot be refreshed for a disabled source.",
                ));
            }
            if fetched_at_unix_ms < feed.created_at_unix_ms
                || feed
                    .last_fetched_at_unix_ms
                    .is_some_and(|last| fetched_at_unix_ms < last)
            {
                return Err(CgWriteError::Invalid(
                    "CG feed-cache refresh time cannot move backward.",
                ));
            }
            let current = self
                .read_cg_feed_cache(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            let expires_at_unix_ms = fetched_at_unix_ms
                .checked_add(i64::from(feed.refresh_seconds) * 1_000)
                .ok_or(CgWriteError::Invalid(
                    "CG feed-cache expiry exceeds the supported range.",
                ))?;
            let source = c_string(feed_source_id, "CG feed-cache source identity")
                .map_err(CgWriteError::Storage)?;
            let meta = self
                .prepare(
                    "UPDATE cg_feed_cache_meta SET fetched_at_unix_ms=?1, \
                     expires_at_unix_ms=?2, last_error='' WHERE feed_source_id=?3",
                )
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, meta.raw, 1, fetched_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, meta.raw, 2, expires_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, meta.raw, 3, &source).map_err(CgWriteError::Storage)?;
            expect_done(self.database, meta.raw).map_err(CgWriteError::Storage)?;
            let source_update = self
                .prepare(
                    "UPDATE cg_feed_sources SET last_fetched_at_unix_ms=?1, last_fetch_error='' \
                     WHERE feed_source_id=?2",
                )
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, source_update.raw, 1, fetched_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, source_update.raw, 2, &source)
                .map_err(CgWriteError::Storage)?;
            expect_done(self.database, source_update.raw).map_err(CgWriteError::Storage)?;
            Ok(CgFeedCacheEntry {
                feed_source_id: feed_source_id.into(),
                fetched_at_unix_ms,
                expires_at_unix_ms,
                etag: current.etag,
                last_modified: current.last_modified,
                last_error: None,
                items: current.items,
            })
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn record_cg_feed_fetch_failure(
        &self,
        feed_source_id: &str,
        failed_at_unix_ms: i64,
        error: &str,
    ) -> Result<(), CgWriteError> {
        if failed_at_unix_ms < 0 || error.trim().is_empty() || error.len() > 1_000 {
            return Err(CgWriteError::Invalid(
                "CG feed-fetch failure evidence is invalid.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let feed = self
                .read_cg_feed_source(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if failed_at_unix_ms < feed.created_at_unix_ms
                || feed
                    .last_fetched_at_unix_ms
                    .is_some_and(|last| failed_at_unix_ms < last)
            {
                return Err(CgWriteError::Invalid(
                    "CG feed-fetch time cannot move backward.",
                ));
            }
            let statement = self
                .prepare(
                    "UPDATE cg_feed_sources SET last_fetched_at_unix_ms=?1, last_fetch_error=?2 \
                     WHERE feed_source_id=?3",
                )
                .map_err(CgWriteError::Storage)?;
            let error = c_string(error, "CG feed-fetch error").map_err(CgWriteError::Storage)?;
            let id = c_string(feed_source_id, "CG feed identity").map_err(CgWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 1, failed_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, statement.raw, 2, &error).map_err(CgWriteError::Storage)?;
            bind_text(self.database, statement.raw, 3, &id).map_err(CgWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
            let cache_error = self
                .prepare("UPDATE cg_feed_cache_meta SET last_error=?1 WHERE feed_source_id=?2")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, cache_error.raw, 1, &error).map_err(CgWriteError::Storage)?;
            bind_text(self.database, cache_error.raw, 2, &id).map_err(CgWriteError::Storage)?;
            expect_done(self.database, cache_error.raw).map_err(CgWriteError::Storage)
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_feed_cache(
        &self,
        feed_source_id: &str,
    ) -> Result<Option<CgFeedCacheEntry>, String> {
        let meta = self.prepare(
            "SELECT fetched_at_unix_ms, expires_at_unix_ms, etag, last_modified, last_error \
             FROM cg_feed_cache_meta WHERE feed_source_id=?1",
        )?;
        let source = c_string(feed_source_id, "CG feed-cache source identity")?;
        bind_text(self.database, meta.raw, 1, &source)?;
        let (fetched, expires, etag, last_modified, last_error) =
            match unsafe { sqlite3_step(meta.raw) } {
                SQLITE_ROW => (
                    unsafe { sqlite3_column_int64(meta.raw, 0) },
                    unsafe { sqlite3_column_int64(meta.raw, 1) },
                    column_text(meta.raw, 2)?,
                    column_text(meta.raw, 3)?,
                    column_text(meta.raw, 4)?,
                ),
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
        let statement = self.prepare(
            "SELECT item_id, title, summary, starts_at_unix_ms, url, tags_json \
             FROM cg_feed_cache_items WHERE feed_source_id=?1 ORDER BY starts_at_unix_ms, item_id",
        )?;
        bind_text(self.database, statement.raw, 1, &source)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let summary = column_text(statement.raw, 2)?;
                    let url = column_text(statement.raw, 4)?;
                    items.push(CgFeedItem {
                        item_id: column_text(statement.raw, 0)?,
                        title: column_text(statement.raw, 1)?,
                        summary: (!summary.is_empty()).then_some(summary),
                        starts_at_unix_ms: {
                            let value = unsafe { sqlite3_column_int64(statement.raw, 3) };
                            (value >= 0).then_some(value)
                        },
                        url: (!url.is_empty()).then_some(url),
                        tags: serde_json::from_str(&column_text(statement.raw, 5)?).map_err(
                            |error| format!("database returned invalid CG cache tags: {error}"),
                        )?,
                    });
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        let entry = CgFeedCacheEntry {
            feed_source_id: feed_source_id.into(),
            fetched_at_unix_ms: fetched,
            expires_at_unix_ms: expires,
            etag: (!etag.is_empty()).then_some(etag),
            last_modified: (!last_modified.is_empty()).then_some(last_modified),
            last_error: (!last_error.is_empty()).then_some(last_error),
            items,
        };
        entry.validate().map_err(str::to_string)?;
        Ok(Some(entry))
    }

    pub fn list_cg_approved_feed_item_ids(
        &self,
        feed_source_id: &str,
    ) -> Result<BTreeSet<String>, String> {
        let statement = self.prepare(
            "SELECT item_id FROM cg_feed_item_approvals WHERE feed_source_id=?1 ORDER BY item_id",
        )?;
        let source = c_string(feed_source_id, "CG approval feed identity")?;
        bind_text(self.database, statement.raw, 1, &source)?;
        let mut ids = BTreeSet::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    ids.insert(column_text(statement.raw, 0)?);
                }
                SQLITE_DONE => return Ok(ids),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_cg_feed_item_approvals(
        &self,
        feed_source_id: &str,
    ) -> Result<Vec<CgFeedItemApproval>, String> {
        let statement = self.prepare(
            "SELECT feed_source_id, item_id, approved_by, approved_at_unix_ms \
             FROM cg_feed_item_approvals WHERE feed_source_id=?1 ORDER BY item_id",
        )?;
        let source = c_string(feed_source_id, "CG approval feed identity")?;
        bind_text(self.database, statement.raw, 1, &source)?;
        let mut approvals = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => approvals.push(read_cg_feed_item_approval_row(statement.raw)?),
                SQLITE_DONE => return Ok(approvals),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn delete_cg_feed_source(
        &self,
        feed_source_id: &str,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<(), CgWriteError> {
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.event_kind != CgAuditEventKind::FeedRemoved {
            return Err(CgWriteError::Invalid(
                "CG feed deletion requires feed-removed audit evidence.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let feed = self
                .read_cg_feed_source(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if feed.revision != expected_revision {
                return Err(CgWriteError::Conflict(feed.revision));
            }
            if audit.channel_id != feed.channel_id
                || audit.occurred_at_unix_ms < feed.updated_at_unix_ms
            {
                return Err(CgWriteError::Invalid(
                    "CG feed deletion audit evidence does not match the current feed.",
                ));
            }
            if let Some(board) = self
                .read_cg_board(&feed.channel_id)
                .map_err(CgWriteError::Storage)?
            {
                if audit.board_id.as_deref() != Some(board.board_id.as_str()) {
                    return Err(CgWriteError::Invalid(
                        "CG feed deletion audit evidence identifies the wrong board.",
                    ));
                }
                if self
                    .list_cg_zones(&board.board_id)
                    .map_err(CgWriteError::Storage)?
                    .iter()
                    .any(|zone| {
                        matches!(
                            &zone.content_source,
                            CgContentSource::Feed { feed_source_id: used_id }
                                if used_id == feed_source_id
                        )
                    })
                {
                    return Err(CgWriteError::Invalid(
                        "CG feed cannot be removed while a board zone still uses it.",
                    ));
                }
            } else if audit.board_id.is_some() {
                return Err(CgWriteError::Invalid(
                    "CG feed deletion audit evidence identifies an unavailable board.",
                ));
            }
            let statement = self
                .prepare("DELETE FROM cg_feed_sources WHERE feed_source_id=?1 AND revision=?2")
                .map_err(CgWriteError::Storage)?;
            let feed_source_id =
                c_string(feed_source_id, "CG feed identity").map_err(CgWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &feed_source_id)
                .map_err(CgWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(expected_revision, "CG feed revision")
                    .map_err(CgWriteError::Storage)?,
            )
            .map_err(CgWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
            drop(statement);
            if self.changes() != 1 {
                return Err(CgWriteError::Conflict(feed.revision));
            }
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn write_cg_bulletin(
        &self,
        bulletin: &CgBulletin,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<CgBulletin, CgWriteError> {
        bulletin.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.channel_id != bulletin.channel_id {
            return Err(CgWriteError::Invalid(
                "CG bulletin audit evidence identifies the wrong channel.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let mut asset_ids = Vec::new();
            match &bulletin.content {
                CgBulletinContent::UploadedImage { asset_id, .. }
                | CgBulletinContent::FullscreenSlide { asset_id, .. } => {
                    asset_ids.push(asset_id.as_str());
                }
                _ => {}
            }
            if let Some(asset_id) = bulletin.audio_asset_id.as_deref() {
                asset_ids.push(asset_id);
            }
            for asset_id in asset_ids {
                let asset = self
                    .read_media_asset(asset_id)
                    .map_err(CgWriteError::Storage)?
                    .ok_or(CgWriteError::NotFound)?;
                if asset.readiness != AssetReadiness::Ready {
                    return Err(CgWriteError::Invalid(
                        "CG bulletin media must identify ready managed assets.",
                    ));
                }
            }
            let current = self
                .read_cg_bulletin(&bulletin.bulletin_id)
                .map_err(CgWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CgWriteError::Conflict(current_revision));
            }
            if bulletin.revision != expected_revision.saturating_add(1) {
                return Err(CgWriteError::Invalid(
                    "CG bulletin revision must be exactly one greater than the expected revision.",
                ));
            }
            match current {
                None if bulletin.state != CgBulletinState::Submitted
                    || audit.event_kind != CgAuditEventKind::BulletinSubmitted =>
                {
                    return Err(CgWriteError::Invalid(
                        "A new CG bulletin must begin submitted with submission audit evidence.",
                    ));
                }
                Some(existing)
                    if existing.channel_id != bulletin.channel_id
                        || existing.created_at_unix_ms != bulletin.created_at_unix_ms
                        || existing.submitted_by != bulletin.submitted_by
                        || bulletin.updated_at_unix_ms < existing.updated_at_unix_ms
                        || !valid_cg_bulletin_transition(existing.state, bulletin.state)
                        || !audit_matches_cg_bulletin_state(audit.event_kind, bulletin.state) =>
                {
                    return Err(CgWriteError::Invalid(
                        "CG bulletin transition, identity, provenance, time, or audit evidence is invalid.",
                    ));
                }
                _ => {}
            }
            self.upsert_cg_bulletin(bulletin)
                .map_err(CgWriteError::Storage)?;
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(bulletin.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_bulletin(&self, bulletin_id: &str) -> Result<Option<CgBulletin>, String> {
        let statement = self.prepare(
            "SELECT bulletin_id, channel_id, content_json, state, tags_json, \
             requested_start_at_unix_ms, requested_end_at_unix_ms, display_seconds, audio_asset_id, \
             submitted_by, moderation_notes, moderated_by, revision, created_at_unix_ms, updated_at_unix_ms \
             FROM cg_bulletins WHERE bulletin_id=?1",
        )?;
        let bulletin_id = c_string(bulletin_id, "CG bulletin identity")?;
        bind_text(self.database, statement.raw, 1, &bulletin_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_bulletin_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_cg_bulletins(&self, channel_id: &str) -> Result<Vec<CgBulletin>, String> {
        let statement = self.prepare(
            "SELECT bulletin_id, channel_id, content_json, state, tags_json, \
             requested_start_at_unix_ms, requested_end_at_unix_ms, display_seconds, audio_asset_id, \
             submitted_by, moderation_notes, moderated_by, revision, created_at_unix_ms, updated_at_unix_ms \
             FROM cg_bulletins WHERE channel_id=?1 ORDER BY updated_at_unix_ms DESC, bulletin_id",
        )?;
        let channel_id = c_string(channel_id, "CG bulletin channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut bulletins = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => bulletins.push(read_cg_bulletin_row(statement.raw)?),
                SQLITE_DONE => return Ok(bulletins),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_airable_cg_bulletins(
        &self,
        channel_id: &str,
        now_unix_ms: i64,
    ) -> Result<Vec<CgBulletin>, String> {
        if now_unix_ms < 0 {
            return Err("CG bulletin resolution time must be nonnegative".into());
        }
        Ok(self
            .list_cg_bulletins(channel_id)?
            .into_iter()
            .filter(|bulletin| bulletin.is_airable_at(now_unix_ms))
            .collect())
    }

    /// Expire scheduled bulletins whose requested end has arrived.
    ///
    /// This is deliberately an explicit, channel-scoped operation. It does not
    /// run a scheduler; callers supply the observation time and operator
    /// identity, and the state changes plus audit rows commit together.
    pub fn expire_due_cg_bulletins(
        &self,
        channel_id: &str,
        now_unix_ms: i64,
        operator_id: &str,
    ) -> Result<Vec<CgBulletin>, CgWriteError> {
        if now_unix_ms < 0 {
            return Err(CgWriteError::Invalid(
                "CG bulletin expiration time must be nonnegative.",
            ));
        }
        let operator_id = operator_id.trim();
        let validation_audit = CgAuditEvent {
            audit_id: deterministic_cg_expiration_audit_id(channel_id, channel_id, now_unix_ms),
            board_id: None,
            channel_id: channel_id.into(),
            event_kind: CgAuditEventKind::BulletinExpired,
            operator_id: operator_id.into(),
            occurred_at_unix_ms: now_unix_ms,
            details: BTreeMap::new(),
        };
        validation_audit.validate().map_err(CgWriteError::Invalid)?;

        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let channel = self
                .prepare("SELECT 1 FROM channels WHERE channel_id=?1")
                .map_err(CgWriteError::Storage)?;
            let channel_id_value = c_string(channel_id, "CG bulletin expiration channel identity")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, channel.raw, 1, &channel_id_value)
                .map_err(CgWriteError::Storage)?;
            if unsafe { sqlite3_step(channel.raw) } != SQLITE_ROW {
                return Err(CgWriteError::NotFound);
            }
            drop(channel);

            let due: Vec<CgBulletin> = self
                .list_cg_bulletins(channel_id)
                .map_err(CgWriteError::Storage)?
                .into_iter()
                .filter(|bulletin| {
                    bulletin.channel_id == channel_id
                        && bulletin.state == CgBulletinState::Scheduled
                        && bulletin
                            .requested_end_at_unix_ms
                            .is_some_and(|end| end <= now_unix_ms)
                })
                .collect();
            let mut expired = Vec::with_capacity(due.len());
            for bulletin in due {
                if bulletin.updated_at_unix_ms > now_unix_ms {
                    return Err(CgWriteError::Invalid(
                        "CG bulletin expiration time cannot precede the bulletin update time.",
                    ));
                }
                let revision = bulletin
                    .revision
                    .checked_add(1)
                    .ok_or(CgWriteError::Invalid(
                        "CG bulletin revision cannot be incremented.",
                    ))?;
                let mut updated = bulletin.clone();
                updated.state = CgBulletinState::Expired;
                updated.revision = revision;
                updated.updated_at_unix_ms = now_unix_ms;
                updated.validate().map_err(CgWriteError::Invalid)?;
                let statement = self
                    .prepare(
                        "UPDATE cg_bulletins SET state='expired', revision=?1, updated_at_unix_ms=?2 \
                         WHERE bulletin_id=?3 AND channel_id=?4 AND state='scheduled' \
                         AND requested_end_at_unix_ms>=0 AND requested_end_at_unix_ms<=?2 AND revision=?5",
                    )
                    .map_err(CgWriteError::Storage)?;
                bind_i64(
                    self.database,
                    statement.raw,
                    1,
                    checked_i64(revision, "CG bulletin revision").map_err(CgWriteError::Storage)?,
                )
                .map_err(CgWriteError::Storage)?;
                bind_i64(self.database, statement.raw, 2, now_unix_ms)
                    .map_err(CgWriteError::Storage)?;
                let bulletin_id =
                    c_string(&bulletin.bulletin_id, "CG bulletin expiration identity")
                        .map_err(CgWriteError::Storage)?;
                bind_text(self.database, statement.raw, 3, &bulletin_id)
                    .map_err(CgWriteError::Storage)?;
                bind_text(self.database, statement.raw, 4, &channel_id_value)
                    .map_err(CgWriteError::Storage)?;
                bind_i64(
                    self.database,
                    statement.raw,
                    5,
                    checked_i64(bulletin.revision, "prior CG bulletin revision")
                        .map_err(CgWriteError::Storage)?,
                )
                .map_err(CgWriteError::Storage)?;
                expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
                if self.changes() != 1 {
                    return Err(CgWriteError::Conflict(bulletin.revision));
                }
                drop(statement);

                let mut details = BTreeMap::new();
                details.insert("bulletin_id".into(), bulletin.bulletin_id.clone());
                details.insert("prior_revision".into(), bulletin.revision.to_string());
                details.insert(
                    "requested_end_at_unix_ms".into(),
                    bulletin
                        .requested_end_at_unix_ms
                        .expect("eligible bulletin has an end time")
                        .to_string(),
                );
                let audit = CgAuditEvent {
                    audit_id: deterministic_cg_expiration_audit_id(
                        channel_id,
                        &bulletin.bulletin_id,
                        now_unix_ms,
                    ),
                    board_id: None,
                    channel_id: channel_id.into(),
                    event_kind: CgAuditEventKind::BulletinExpired,
                    operator_id: operator_id.into(),
                    occurred_at_unix_ms: now_unix_ms,
                    details,
                };
                audit.validate().map_err(CgWriteError::Invalid)?;
                self.insert_cg_audit(&audit)
                    .map_err(CgWriteError::Storage)?;
                expired.push(updated);
            }
            Ok(expired)
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn approve_cg_feed_item(
        &self,
        approval: &CgFeedItemApproval,
        audit: &CgAuditEvent,
    ) -> Result<CgFeedItemApproval, CgWriteError> {
        approval.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.event_kind != CgAuditEventKind::FeedItemApproved {
            return Err(CgWriteError::Invalid(
                "CG feed-item approval requires matching audit evidence.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let feed = self
                .read_cg_feed_source(&approval.feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if feed.channel_id != audit.channel_id || approval.approved_by != audit.operator_id {
                return Err(CgWriteError::Invalid(
                    "CG feed-item approval and audit identities disagree.",
                ));
            }
            if let Some(existing) = self
                .read_cg_feed_item_approval(&approval.feed_source_id, &approval.item_id)
                .map_err(CgWriteError::Storage)?
            {
                if existing != *approval {
                    return Err(CgWriteError::Invalid(
                        "A CG feed-item approval is immutable once recorded.",
                    ));
                }
                return Ok(existing);
            }
            let observed = self
                .prepare("SELECT 1 FROM cg_feed_cache_items WHERE feed_source_id=?1 AND item_id=?2")
                .map_err(CgWriteError::Storage)?;
            let feed_id = c_string(&approval.feed_source_id, "CG approval feed identity")
                .map_err(CgWriteError::Storage)?;
            let item_id = c_string(&approval.item_id, "CG approval item identity")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, observed.raw, 1, &feed_id).map_err(CgWriteError::Storage)?;
            bind_text(self.database, observed.raw, 2, &item_id).map_err(CgWriteError::Storage)?;
            if unsafe { sqlite3_step(observed.raw) } != SQLITE_ROW {
                return Err(CgWriteError::NotFound);
            }
            let statement = self
                .prepare(
                    "INSERT INTO cg_feed_item_approvals(feed_source_id, item_id, approved_by, \
                     approved_at_unix_ms) VALUES(?1, ?2, ?3, ?4)",
                )
                .map_err(CgWriteError::Storage)?;
            let values = [
                c_string(&approval.feed_source_id, "CG approval feed identity")
                    .map_err(CgWriteError::Storage)?,
                c_string(&approval.item_id, "CG approval item identity")
                    .map_err(CgWriteError::Storage)?,
                c_string(&approval.approved_by, "CG approval operator")
                    .map_err(CgWriteError::Storage)?,
            ];
            for (index, value) in values.iter().enumerate() {
                bind_text(self.database, statement.raw, (index + 1) as c_int, value)
                    .map_err(CgWriteError::Storage)?;
            }
            bind_i64(
                self.database,
                statement.raw,
                4,
                approval.approved_at_unix_ms,
            )
            .map_err(CgWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
            drop(statement);
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(approval.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_feed_item_approval(
        &self,
        feed_source_id: &str,
        item_id: &str,
    ) -> Result<Option<CgFeedItemApproval>, String> {
        let statement = self.prepare(
            "SELECT feed_source_id, item_id, approved_by, approved_at_unix_ms \
             FROM cg_feed_item_approvals WHERE feed_source_id=?1 AND item_id=?2",
        )?;
        let feed_source_id = c_string(feed_source_id, "CG approval feed identity")?;
        let item_id = c_string(item_id, "CG approval item identity")?;
        bind_text(self.database, statement.raw, 1, &feed_source_id)?;
        bind_text(self.database, statement.raw, 2, &item_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_feed_item_approval_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_cg_board_by_id(&self, board_id: &str) -> Result<Option<CgBoard>, String> {
        let statement = self.prepare(
            "SELECT board_id, channel_id, template_id, active, background_audio_asset_id, revision, \
             created_at_unix_ms, updated_at_unix_ms, created_by FROM cg_boards WHERE board_id=?1",
        )?;
        let board_id = c_string(board_id, "CG board identity")?;
        bind_text(self.database, statement.raw, 1, &board_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_board_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn upsert_cg_board(&self, board: &CgBoard) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_boards(board_id, channel_id, template_id, active, background_audio_asset_id, \
             revision, created_at_unix_ms, updated_at_unix_ms, created_by) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9) \
             ON CONFLICT(board_id) DO UPDATE SET template_id=excluded.template_id, active=excluded.active, \
             background_audio_asset_id=excluded.background_audio_asset_id, revision=excluded.revision, \
             updated_at_unix_ms=excluded.updated_at_unix_ms \
             WHERE cg_boards.channel_id=excluded.channel_id AND cg_boards.revision=?10",
        )?;
        let values = [
            c_string(&board.board_id, "CG board identity")?,
            c_string(&board.channel_id, "CG board channel identity")?,
            c_string(&board.template_id, "CG board template identity")?,
            c_string(
                board
                    .background_audio_asset_id
                    .as_deref()
                    .unwrap_or_default(),
                "CG board background audio identity",
            )?,
            c_string(&board.created_by, "CG board creator")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_i64(self.database, statement.raw, 4, i64::from(board.active))?;
        bind_text(self.database, statement.raw, 5, &values[3])?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            checked_i64(board.revision, "CG board revision")?,
        )?;
        bind_i64(self.database, statement.raw, 7, board.created_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 8, board.updated_at_unix_ms)?;
        bind_text(self.database, statement.raw, 9, &values[4])?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            checked_i64(board.revision - 1, "prior CG board revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("CG board write lost its revision authority".into())
        }
    }

    fn upsert_cg_zone(&self, zone: &CgZoneConfig) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_zone_configs(zone_id, board_id, template_zone_key, zone_kind, \
             content_source_json, rotation_seconds, approval_required, allowed_tags_json, revision, \
             created_at_unix_ms, updated_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
             ON CONFLICT(zone_id) DO UPDATE SET template_zone_key=excluded.template_zone_key, \
             zone_kind=excluded.zone_kind, content_source_json=excluded.content_source_json, \
             rotation_seconds=excluded.rotation_seconds, approval_required=excluded.approval_required, \
             allowed_tags_json=excluded.allowed_tags_json, revision=excluded.revision, \
             updated_at_unix_ms=excluded.updated_at_unix_ms \
             WHERE cg_zone_configs.board_id=excluded.board_id AND cg_zone_configs.revision=?12",
        )?;
        let values = [
            c_string(&zone.zone_id, "CG zone identity")?,
            c_string(&zone.board_id, "CG zone board identity")?,
            c_string(&zone.template_zone_key, "CG template zone key")?,
            c_string(cg_zone_kind_name(zone.zone_kind), "CG zone kind")?,
            c_string(
                &serde_json::to_string(&zone.content_source)
                    .map_err(|error| format!("could not serialize CG content source: {error}"))?,
                "CG content source",
            )?,
            c_string(
                &serde_json::to_string(&zone.allowed_tags)
                    .map_err(|error| format!("could not serialize CG zone tags: {error}"))?,
                "CG zone tags",
            )?,
        ];
        for (index, value) in values[..5].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            6,
            zone.rotation_seconds.map_or(-1, i64::from),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            i64::from(zone.approval_required),
        )?;
        bind_text(self.database, statement.raw, 8, &values[5])?;
        bind_i64(
            self.database,
            statement.raw,
            9,
            checked_i64(zone.revision, "CG zone revision")?,
        )?;
        bind_i64(self.database, statement.raw, 10, zone.created_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 11, zone.updated_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            12,
            checked_i64(zone.revision - 1, "prior CG zone revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("CG zone write lost its revision authority".into())
        }
    }

    fn upsert_cg_feed_source(&self, feed: &CgFeedSource) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_feed_sources(feed_source_id, channel_id, kind, label, source_url, \
             trust_tier, tags_json, refresh_seconds, enabled, revision, created_at_unix_ms, updated_at_unix_ms, \
             created_by, last_fetched_at_unix_ms, last_fetch_error) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15) \
             ON CONFLICT(feed_source_id) DO UPDATE SET kind=excluded.kind, label=excluded.label, \
             source_url=excluded.source_url, trust_tier=excluded.trust_tier, \
             tags_json=excluded.tags_json, refresh_seconds=excluded.refresh_seconds, enabled=excluded.enabled, \
             revision=excluded.revision, updated_at_unix_ms=excluded.updated_at_unix_ms, \
             last_fetched_at_unix_ms=excluded.last_fetched_at_unix_ms, \
             last_fetch_error=excluded.last_fetch_error \
             WHERE cg_feed_sources.channel_id=excluded.channel_id AND cg_feed_sources.revision=?16",
        )?;
        let values = [
            c_string(&feed.feed_source_id, "CG feed identity")?,
            c_string(&feed.channel_id, "CG feed channel identity")?,
            c_string(cg_feed_kind_name(feed.kind), "CG feed kind")?,
            c_string(&feed.label, "CG feed label")?,
            c_string(&feed.source_url, "CG feed source URL")?,
            c_string(
                cg_feed_trust_tier_name(feed.trust_tier),
                "CG feed trust tier",
            )?,
            c_string(
                &serde_json::to_string(&feed.tags)
                    .map_err(|error| format!("could not serialize CG feed tags: {error}"))?,
                "CG feed tags",
            )?,
            c_string(&feed.created_by, "CG feed creator")?,
            c_string(
                feed.last_fetch_error.as_deref().unwrap_or_default(),
                "CG feed fetch error",
            )?,
        ];
        for (index, value) in values[..7].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            8,
            i64::from(feed.refresh_seconds),
        )?;
        bind_i64(self.database, statement.raw, 9, i64::from(feed.enabled))?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            checked_i64(feed.revision, "CG feed revision")?,
        )?;
        bind_i64(self.database, statement.raw, 11, feed.created_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 12, feed.updated_at_unix_ms)?;
        bind_text(self.database, statement.raw, 13, &values[7])?;
        bind_i64(
            self.database,
            statement.raw,
            14,
            feed.last_fetched_at_unix_ms.unwrap_or(-1),
        )?;
        bind_text(self.database, statement.raw, 15, &values[8])?;
        bind_i64(
            self.database,
            statement.raw,
            16,
            checked_i64(feed.revision - 1, "prior CG feed revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("CG feed write lost its revision authority".into())
        }
    }

    fn upsert_cg_bulletin(&self, bulletin: &CgBulletin) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_bulletins(bulletin_id, channel_id, content_json, state, tags_json, \
             requested_start_at_unix_ms, requested_end_at_unix_ms, display_seconds, audio_asset_id, \
             submitted_by, moderation_notes, moderated_by, revision, created_at_unix_ms, updated_at_unix_ms) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15) \
             ON CONFLICT(bulletin_id) DO UPDATE SET content_json=excluded.content_json, \
             state=excluded.state, tags_json=excluded.tags_json, \
             requested_start_at_unix_ms=excluded.requested_start_at_unix_ms, \
             requested_end_at_unix_ms=excluded.requested_end_at_unix_ms, \
             display_seconds=excluded.display_seconds, audio_asset_id=excluded.audio_asset_id, \
             moderation_notes=excluded.moderation_notes, moderated_by=excluded.moderated_by, \
             revision=excluded.revision, updated_at_unix_ms=excluded.updated_at_unix_ms \
             WHERE cg_bulletins.channel_id=excluded.channel_id AND cg_bulletins.revision=?16",
        )?;
        let values = [
            c_string(&bulletin.bulletin_id, "CG bulletin identity")?,
            c_string(&bulletin.channel_id, "CG bulletin channel identity")?,
            c_string(
                &serde_json::to_string(&bulletin.content)
                    .map_err(|error| format!("could not serialize CG bulletin content: {error}"))?,
                "CG bulletin content",
            )?,
            c_string(cg_bulletin_state_name(bulletin.state), "CG bulletin state")?,
            c_string(
                &serde_json::to_string(&bulletin.tags)
                    .map_err(|error| format!("could not serialize CG bulletin tags: {error}"))?,
                "CG bulletin tags",
            )?,
            c_string(
                bulletin.audio_asset_id.as_deref().unwrap_or_default(),
                "CG bulletin audio identity",
            )?,
            c_string(&bulletin.submitted_by, "CG bulletin submitter")?,
            c_string(
                bulletin.moderation_notes.as_deref().unwrap_or_default(),
                "CG bulletin moderation notes",
            )?,
            c_string(
                bulletin.moderated_by.as_deref().unwrap_or_default(),
                "CG bulletin moderator",
            )?,
        ];
        for (index, value) in values[..5].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            6,
            bulletin.requested_start_at_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            bulletin.requested_end_at_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            8,
            i64::from(bulletin.display_seconds),
        )?;
        for (parameter, value) in [
            (9, &values[5]),
            (10, &values[6]),
            (11, &values[7]),
            (12, &values[8]),
        ] {
            bind_text(self.database, statement.raw, parameter, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            13,
            checked_i64(bulletin.revision, "CG bulletin revision")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            14,
            bulletin.created_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            15,
            bulletin.updated_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            16,
            checked_i64(bulletin.revision - 1, "prior CG bulletin revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("CG bulletin write lost its revision authority".into())
        }
    }

    fn insert_cg_audit(&self, audit: &CgAuditEvent) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_audit_events(audit_id, board_id, channel_id, event_kind, operator_id, \
             occurred_at_unix_ms, details_json) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        )?;
        let values = [
            c_string(&audit.audit_id, "CG audit identity")?,
            c_string(
                audit.board_id.as_deref().unwrap_or_default(),
                "CG audit board identity",
            )?,
            c_string(&audit.channel_id, "CG audit channel identity")?,
            c_string(
                cg_audit_event_kind_name(audit.event_kind),
                "CG audit event kind",
            )?,
            c_string(&audit.operator_id, "CG audit operator identity")?,
            c_string(
                &serde_json::to_string(&audit.details)
                    .map_err(|error| format!("could not serialize CG audit details: {error}"))?,
                "CG audit details",
            )?,
        ];
        for (index, value) in values[..5].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 6, audit.occurred_at_unix_ms)?;
        bind_text(self.database, statement.raw, 7, &values[5])?;
        expect_done(self.database, statement.raw)
    }

    fn list_channel_output_sinks(
        &self,
        channel_id: &str,
    ) -> Result<Vec<OutputSinkConfiguration>, String> {
        let statement = self.prepare(
            "SELECT sink_id, label, kind, destination, loudness_target_milli, required, enabled, \
             EXISTS(SELECT 1 FROM srt_output_secrets secret \
                    WHERE secret.channel_id=channel_output_sinks.channel_id \
                      AND secret.sink_id=channel_output_sinks.sink_id), \
             headend_profile, headend_muxrate_kbps, headend_packet_size \
             FROM channel_output_sinks WHERE channel_id=?1 ORDER BY sink_order, sink_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut sinks = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let headend_profile = column_text(statement.raw, 8)?;
                    let headend = if headend_profile.is_empty() {
                        None
                    } else {
                        Some(HeadendSinkContract {
                            profile_id: HeadendProfileId::from_storage_name(&headend_profile)
                                .map_err(|error| format!("{error:?}"))?,
                            muxrate_kbps: u32::try_from(unsafe {
                                sqlite3_column_int64(statement.raw, 9)
                            })
                            .map_err(|_| "database returned an invalid headend muxrate")?,
                            packet_size_bytes: u16::try_from(unsafe {
                                sqlite3_column_int64(statement.raw, 10)
                            })
                            .map_err(|_| "database returned an invalid headend packet size")?,
                        })
                    };
                    sinks.push(OutputSinkConfiguration {
                        sink_id: column_text(statement.raw, 0)?,
                        label: column_text(statement.raw, 1)?,
                        kind: parse_output_sink_kind(&column_text(statement.raw, 2)?)?,
                        destination: column_text(statement.raw, 3)?,
                        loudness_target_lufs: unsafe { sqlite3_column_int64(statement.raw, 4) }
                            as f32
                            / 1_000.0,
                        required: unsafe { sqlite3_column_int64(statement.raw, 5) } == 1,
                        enabled: unsafe { sqlite3_column_int64(statement.raw, 6) } == 1,
                        encryption_configured: unsafe { sqlite3_column_int64(statement.raw, 7) }
                            == 1,
                        headend,
                    })
                }
                SQLITE_DONE => return Ok(sinks),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_srt_output_secret(
        &self,
        channel_id: &str,
        sink_id: &str,
    ) -> Result<Option<SrtOutputSecret>, String> {
        let statement = self.prepare(
            "SELECT encrypted_passphrase FROM srt_output_secrets WHERE channel_id=?1 AND sink_id=?2",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        let sink_id = c_string(sink_id, "output sink identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_text(self.database, statement.raw, 2, &sink_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let decrypted = unprotect_local_machine(&column_blob(statement.raw, 0)?)?;
                let passphrase = String::from_utf8(decrypted)
                    .map_err(|_| "stored SRT passphrase is not valid UTF-8".to_string())?;
                validate_srt_passphrase(&passphrase)?;
                Ok(Some(SrtOutputSecret { passphrase }))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_channel_runtime(
        &self,
        observation: &ChannelRuntimeObservation,
    ) -> Result<(), String> {
        observation.validate().map_err(str::to_string)?;
        let statement = self.prepare(
            "INSERT INTO channel_runtime(channel_id, state, on_air_asset_id, \
             state_started_at_unix_ms, heartbeat_at_unix_ms, detail) VALUES(?1, ?2, ?3, ?4, ?5, ?6) \
             ON CONFLICT(channel_id) DO UPDATE SET state=excluded.state, \
             on_air_asset_id=excluded.on_air_asset_id, state_started_at_unix_ms=excluded.state_started_at_unix_ms, \
             heartbeat_at_unix_ms=excluded.heartbeat_at_unix_ms, detail=excluded.detail \
             WHERE excluded.heartbeat_at_unix_ms >= channel_runtime.heartbeat_at_unix_ms",
        )?;
        let channel_id = c_string(&observation.channel_id, "runtime channel identity")?;
        let state = c_string(
            channel_runtime_state_name(observation.state),
            "runtime state",
        )?;
        let asset_id = c_string(
            observation.on_air_asset_id.as_deref().unwrap_or_default(),
            "runtime asset identity",
        )?;
        let detail = c_string(
            observation.detail.as_deref().unwrap_or_default(),
            "runtime detail",
        )?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_text(self.database, statement.raw, 2, &state)?;
        bind_text(self.database, statement.raw, 3, &asset_id)?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            observation.state_started_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            observation.heartbeat_at_unix_ms,
        )?;
        bind_text(self.database, statement.raw, 6, &detail)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("channel runtime heartbeat moved backward".into())
        }
    }

    pub fn read_channel_runtime(
        &self,
        channel_id: &str,
    ) -> Result<Option<ChannelRuntimeObservation>, String> {
        let statement = self.prepare(
            "SELECT channel_id, state, on_air_asset_id, state_started_at_unix_ms, \
             heartbeat_at_unix_ms, detail FROM channel_runtime WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "runtime channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_channel_runtime_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_channel_runtime(&self) -> Result<Vec<ChannelRuntimeObservation>, String> {
        let statement = self.prepare(
            "SELECT channel_id, state, on_air_asset_id, state_started_at_unix_ms, \
             heartbeat_at_unix_ms, detail FROM channel_runtime ORDER BY channel_id",
        )?;
        let mut observations = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => observations.push(read_channel_runtime_row(statement.raw)?),
                SQLITE_DONE => return Ok(observations),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn record_system_resource_sample(
        &self,
        sample: &SystemResourceSample,
    ) -> Result<SystemResourceSample, String> {
        sample.validate().map_err(str::to_string)?;
        if sample.sample_id.is_some() {
            return Err("a new system resource sample cannot supply its database identity".into());
        }
        let statement = self.prepare(
            "INSERT INTO system_resource_samples(sampled_at_unix_ms, cpu_milli_percent, \
             ram_used_milli_gb, ram_total_milli_gb, gpu_milli_percent, gpu_vram_used_milli_gb, \
             media_volume_free_milli_gb, backup_volume_free_milli_gb, database_reachable, \
             backup_volume_writable, service_running, clock_skew_milliseconds) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        )?;
        bind_i64(self.database, statement.raw, 1, sample.sampled_at_unix_ms)?;
        for (index, value) in [
            sample.cpu_percent,
            sample.ram_used_gb,
            sample.ram_total_gb,
            sample.gpu_percent,
            sample.gpu_vram_used_gb,
            sample.media_volume_free_gb,
            sample.backup_volume_free_gb,
        ]
        .into_iter()
        .enumerate()
        {
            bind_i64(
                self.database,
                statement.raw,
                c_int::try_from(index + 2).map_err(|_| "resource metric index overflowed")?,
                scaled_metric(value)?,
            )?;
        }
        bind_i64(
            self.database,
            statement.raw,
            9,
            i64::from(sample.database_reachable),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            i64::from(sample.backup_volume_writable),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            11,
            i64::from(sample.service_running),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            12,
            scaled_signed_metric(sample.clock_skew_seconds)?,
        )?;
        expect_done(self.database, statement.raw)?;
        self.execute(
            "DELETE FROM system_resource_samples WHERE sample_id NOT IN (\
               SELECT sample_id FROM system_resource_samples ORDER BY sample_id DESC LIMIT 10080)",
        )?;
        self.latest_system_resource_sample()?
            .ok_or_else(|| "recorded system resource sample disappeared".into())
    }

    pub fn latest_system_resource_sample(&self) -> Result<Option<SystemResourceSample>, String> {
        let statement = self.prepare(
            "SELECT sample_id, sampled_at_unix_ms, cpu_milli_percent, ram_used_milli_gb, \
             ram_total_milli_gb, gpu_milli_percent, gpu_vram_used_milli_gb, \
             media_volume_free_milli_gb, backup_volume_free_milli_gb, database_reachable, \
             backup_volume_writable, service_running, clock_skew_milliseconds \
             FROM system_resource_samples ORDER BY sample_id DESC LIMIT 1",
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_system_resource_sample_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_system_resource_samples(
        &self,
        limit: u32,
    ) -> Result<Vec<SystemResourceSample>, String> {
        if !(1..=10_080).contains(&limit) {
            return Err("resource sample limit must be between 1 and 10080".into());
        }
        let statement = self.prepare(
            "SELECT sample_id, sampled_at_unix_ms, cpu_milli_percent, ram_used_milli_gb, \
             ram_total_milli_gb, gpu_milli_percent, gpu_vram_used_milli_gb, \
             media_volume_free_milli_gb, backup_volume_free_milli_gb, database_reachable, \
             backup_volume_writable, service_running, clock_skew_milliseconds \
             FROM system_resource_samples ORDER BY sample_id DESC LIMIT ?1",
        )?;
        bind_i64(self.database, statement.raw, 1, i64::from(limit))?;
        let mut samples = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => samples.push(read_system_resource_sample_row(statement.raw)?),
                SQLITE_DONE => return Ok(samples),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn put_self_test_schedule(
        &self,
        schedule: &SelfTestSchedule,
    ) -> Result<SelfTestSchedule, String> {
        schedule.validate().map_err(str::to_string)?;
        if let Some(existing) = self.read_self_test_schedule(schedule.kind)?
            && schedule.updated_at_unix_ms < existing.updated_at_unix_ms
        {
            return Err("self-test schedule update time moved backward".into());
        }
        let statement = self.prepare(
            "INSERT INTO self_test_schedules(kind, enabled, local_hour, local_minute, weekday, \
             updated_at_unix_ms, updated_by) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(kind) DO UPDATE SET enabled=excluded.enabled, local_hour=excluded.local_hour, \
             local_minute=excluded.local_minute, weekday=excluded.weekday, \
             updated_at_unix_ms=excluded.updated_at_unix_ms, updated_by=excluded.updated_by",
        )?;
        let kind = c_string(
            self_test_kind_name(schedule.kind),
            "self-test schedule kind",
        )?;
        let updated_by = c_string(&schedule.updated_by, "self-test schedule operator")?;
        bind_text(self.database, statement.raw, 1, &kind)?;
        bind_i64(self.database, statement.raw, 2, i64::from(schedule.enabled))?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            i64::from(schedule.local_hour),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            i64::from(schedule.local_minute),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            schedule.weekday.map_or(-1, i64::from),
        )?;
        bind_i64(self.database, statement.raw, 6, schedule.updated_at_unix_ms)?;
        bind_text(self.database, statement.raw, 7, &updated_by)?;
        expect_done(self.database, statement.raw)?;
        self.read_self_test_schedule(schedule.kind)?
            .ok_or_else(|| "stored self-test schedule disappeared".into())
    }

    pub fn read_self_test_schedule(
        &self,
        kind: SelfTestKind,
    ) -> Result<Option<SelfTestSchedule>, String> {
        let statement = self.prepare(
            "SELECT kind, enabled, local_hour, local_minute, weekday, updated_at_unix_ms, updated_by \
             FROM self_test_schedules WHERE kind=?1",
        )?;
        let kind = c_string(self_test_kind_name(kind), "self-test schedule kind")?;
        bind_text(self.database, statement.raw, 1, &kind)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_self_test_schedule_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_self_test_schedules(&self) -> Result<Vec<SelfTestSchedule>, String> {
        let statement = self.prepare(
            "SELECT kind, enabled, local_hour, local_minute, weekday, updated_at_unix_ms, updated_by \
             FROM self_test_schedules ORDER BY CASE kind WHEN 'daily' THEN 0 ELSE 1 END",
        )?;
        let mut schedules = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => schedules.push(read_self_test_schedule_row(statement.raw)?),
                SQLITE_DONE => return Ok(schedules),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn record_system_self_test(
        &self,
        record: &SystemSelfTest,
    ) -> Result<SystemSelfTest, String> {
        record.validate().map_err(str::to_string)?;
        let checks_json = serde_json::to_string(&record.checks)
            .map_err(|error| format!("could not encode self-test checks: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO system_self_tests(self_test_id, kind, trigger_kind, schedule_period_key, \
             started_at_unix_ms, finished_at_unix_ms, status, checks_json, summary, evidence_path) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        )?;
        let values = [
            c_string(&record.self_test_id, "self-test identity")?,
            c_string(self_test_kind_name(record.kind), "self-test kind")?,
            c_string(self_test_trigger_name(record.trigger), "self-test trigger")?,
            c_string(
                record.schedule_period_key.as_deref().unwrap_or_default(),
                "self-test schedule period",
            )?,
            c_string(self_test_status_name(record.status), "self-test status")?,
            c_string(&checks_json, "self-test checks")?,
            c_string(&record.summary, "self-test summary")?,
            c_string(
                record.evidence_path.as_deref().unwrap_or_default(),
                "self-test evidence path",
            )?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_text(self.database, statement.raw, 4, &values[3])?;
        bind_i64(self.database, statement.raw, 5, record.started_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 6, record.finished_at_unix_ms)?;
        bind_text(self.database, statement.raw, 7, &values[4])?;
        bind_text(self.database, statement.raw, 8, &values[5])?;
        bind_text(self.database, statement.raw, 9, &values[6])?;
        bind_text(self.database, statement.raw, 10, &values[7])?;
        expect_done(self.database, statement.raw)?;
        self.execute(
            "DELETE FROM system_self_tests WHERE self_test_id NOT IN (\
               SELECT self_test_id FROM system_self_tests ORDER BY started_at_unix_ms DESC, \
               self_test_id DESC LIMIT 400)",
        )?;
        self.read_system_self_test(&record.self_test_id)?
            .ok_or_else(|| "stored self-test record disappeared".into())
    }

    pub fn list_system_self_tests(&self, limit: u32) -> Result<Vec<SystemSelfTest>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("self-test history limit must be between 1 and 1000".into());
        }
        let statement = self.prepare(
            "SELECT self_test_id, kind, trigger_kind, schedule_period_key, started_at_unix_ms, \
             finished_at_unix_ms, status, checks_json, summary, evidence_path \
             FROM system_self_tests ORDER BY started_at_unix_ms DESC, self_test_id DESC LIMIT ?1",
        )?;
        bind_i64(self.database, statement.raw, 1, i64::from(limit))?;
        let mut records = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => records.push(read_system_self_test_row(statement.raw)?),
                SQLITE_DONE => return Ok(records),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_update_check(
        &self,
        record: &UpdateCheckRecord,
    ) -> Result<UpdateCheckRecord, String> {
        record.validate().map_err(str::to_string)?;
        let record_json = serde_json::to_string(record)
            .map_err(|error| format!("could not encode update-check record: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO update_check_status(singleton, checked_at_unix_ms, \
             next_automatic_check_at_unix_ms, trigger_kind, outcome, record_json) \
             VALUES(1, ?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(singleton) DO UPDATE SET \
               checked_at_unix_ms=excluded.checked_at_unix_ms, \
               next_automatic_check_at_unix_ms=excluded.next_automatic_check_at_unix_ms, \
               trigger_kind=excluded.trigger_kind, outcome=excluded.outcome, \
               record_json=excluded.record_json \
             WHERE update_check_status.checked_at_unix_ms <= excluded.checked_at_unix_ms",
        )?;
        let trigger = c_string(
            update_check_trigger_name(record.trigger),
            "update-check trigger",
        )?;
        let outcome = c_string(
            update_check_outcome_name(record.outcome),
            "update-check outcome",
        )?;
        let record_json = c_string(&record_json, "update-check record")?;
        bind_i64(self.database, statement.raw, 1, record.checked_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            record.next_automatic_check_at_unix_ms,
        )?;
        bind_text(self.database, statement.raw, 3, &trigger)?;
        bind_text(self.database, statement.raw, 4, &outcome)?;
        bind_text(self.database, statement.raw, 5, &record_json)?;
        expect_done(self.database, statement.raw)?;
        let stored = self
            .read_update_check()?
            .ok_or_else(|| "stored update-check record disappeared".to_string())?;
        if stored != *record {
            return Err("a newer update-check record already exists".into());
        }
        Ok(stored)
    }

    pub fn read_update_check(&self) -> Result<Option<UpdateCheckRecord>, String> {
        let statement =
            self.prepare("SELECT record_json FROM update_check_status WHERE singleton=1")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let json = column_text(statement.raw, 0)?;
                let record: UpdateCheckRecord = serde_json::from_str(&json)
                    .map_err(|error| format!("stored update-check record is invalid: {error}"))?;
                record.validate().map_err(str::to_string)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn clear_update_check(&self) -> Result<(), String> {
        let statement = self.prepare("DELETE FROM update_check_status WHERE singleton=1")?;
        expect_done(self.database, statement.raw)
    }

    pub fn write_staged_package(
        &self,
        record: &StagedPackageRecord,
    ) -> Result<StagedPackageRecord, String> {
        record.validate().map_err(str::to_string)?;
        let record_json = serde_json::to_string(record)
            .map_err(|error| format!("could not encode staged-package record: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO update_stage_status(singleton, staged_at_unix_ms, version, \
             source_commit, record_json) VALUES(1, ?1, ?2, ?3, ?4) \
             ON CONFLICT(singleton) DO UPDATE SET \
               staged_at_unix_ms=excluded.staged_at_unix_ms, version=excluded.version, \
               source_commit=excluded.source_commit, record_json=excluded.record_json \
             WHERE update_stage_status.staged_at_unix_ms <= excluded.staged_at_unix_ms",
        )?;
        let version = c_string(&record.manifest.version, "staged-package version")?;
        let commit = c_string(
            &record.manifest.source_commit,
            "staged-package source commit",
        )?;
        let record_json = c_string(&record_json, "staged-package record")?;
        bind_i64(self.database, statement.raw, 1, record.staged_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &version)?;
        bind_text(self.database, statement.raw, 3, &commit)?;
        bind_text(self.database, statement.raw, 4, &record_json)?;
        expect_done(self.database, statement.raw)?;
        let stored = self
            .read_staged_package()?
            .ok_or_else(|| "stored staged-package record disappeared".to_string())?;
        if stored != *record {
            return Err("a newer staged-package record already exists".into());
        }
        Ok(stored)
    }

    pub fn read_staged_package(&self) -> Result<Option<StagedPackageRecord>, String> {
        let statement =
            self.prepare("SELECT record_json FROM update_stage_status WHERE singleton=1")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let json = column_text(statement.raw, 0)?;
                let record: StagedPackageRecord = serde_json::from_str(&json)
                    .map_err(|error| format!("stored staged-package record is invalid: {error}"))?;
                record.validate().map_err(str::to_string)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn clear_staged_package(&self) -> Result<(), String> {
        let statement = self.prepare("DELETE FROM update_stage_status WHERE singleton=1")?;
        expect_done(self.database, statement.raw)
    }

    pub fn write_prepared_slot(
        &self,
        record: &PreparedSlotRecord,
    ) -> Result<PreparedSlotRecord, String> {
        record.validate().map_err(str::to_string)?;
        let record_json = serde_json::to_string(record)
            .map_err(|error| format!("could not encode prepared-slot record: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO update_prepare_status(singleton, prepared_at_unix_ms, version, \
             source_commit, record_json) VALUES(1, ?1, ?2, ?3, ?4) \
             ON CONFLICT(singleton) DO UPDATE SET \
               prepared_at_unix_ms=excluded.prepared_at_unix_ms, version=excluded.version, \
               source_commit=excluded.source_commit, record_json=excluded.record_json \
             WHERE update_prepare_status.prepared_at_unix_ms <= excluded.prepared_at_unix_ms",
        )?;
        let version = c_string(&record.manifest.version, "prepared-slot version")?;
        let commit = c_string(
            &record.manifest.source_commit,
            "prepared-slot source commit",
        )?;
        let record_json = c_string(&record_json, "prepared-slot record")?;
        bind_i64(self.database, statement.raw, 1, record.prepared_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &version)?;
        bind_text(self.database, statement.raw, 3, &commit)?;
        bind_text(self.database, statement.raw, 4, &record_json)?;
        expect_done(self.database, statement.raw)?;
        let stored = self
            .read_prepared_slot()?
            .ok_or_else(|| "stored prepared-slot record disappeared".to_string())?;
        if stored != *record {
            return Err("a newer prepared-slot record already exists".into());
        }
        Ok(stored)
    }

    pub fn read_prepared_slot(&self) -> Result<Option<PreparedSlotRecord>, String> {
        let statement =
            self.prepare("SELECT record_json FROM update_prepare_status WHERE singleton=1")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let json = column_text(statement.raw, 0)?;
                let record: PreparedSlotRecord = serde_json::from_str(&json)
                    .map_err(|error| format!("stored prepared-slot record is invalid: {error}"))?;
                record.validate().map_err(str::to_string)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn clear_prepared_slot(&self) -> Result<(), String> {
        let statement = self.prepare("DELETE FROM update_prepare_status WHERE singleton=1")?;
        expect_done(self.database, statement.raw)
    }

    pub fn self_test_exists_for_period(
        &self,
        kind: SelfTestKind,
        period_key: &str,
    ) -> Result<bool, String> {
        if period_key.trim().is_empty() || period_key.len() > 40 {
            return Err("self-test period identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT 1 FROM system_self_tests WHERE kind=?1 AND trigger_kind='scheduled' \
             AND schedule_period_key=?2 LIMIT 1",
        )?;
        let kind = c_string(self_test_kind_name(kind), "self-test kind")?;
        let period_key = c_string(period_key, "self-test period identity")?;
        bind_text(self.database, statement.raw, 1, &kind)?;
        bind_text(self.database, statement.raw, 2, &period_key)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(true),
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn database_integrity_check(&self) -> Result<bool, String> {
        let statement = self.prepare("PRAGMA quick_check")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(column_text(statement.raw, 0)? == "ok"),
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn prove_database_restore_round_trip(&self, target: &Path) -> Result<bool, String> {
        if target.exists() {
            return Err("database restore rehearsal target already exists".into());
        }
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|error| {
                format!("could not create restore rehearsal directory: {error}")
            })?;
        }
        let statement = self.prepare("VACUUM INTO ?1")?;
        let target_text = c_string(
            target.to_string_lossy().as_ref(),
            "database restore rehearsal path",
        )?;
        bind_text(self.database, statement.raw, 1, &target_text)?;
        let outcome = (|| {
            expect_done(self.database, statement.raw)?;
            let restored = StationStore::open(target)?;
            let healthy = restored.database_integrity_check()?;
            drop(restored);
            Ok(healthy)
        })();
        let mut cleanup_error = None;
        for candidate in [
            target.to_path_buf(),
            PathBuf::from(format!("{}-wal", target.display())),
            PathBuf::from(format!("{}-shm", target.display())),
        ] {
            if candidate.exists()
                && let Err(error) = std::fs::remove_file(&candidate)
            {
                cleanup_error = Some(format!(
                    "could not remove restore rehearsal artifact {}: {error}",
                    candidate.display()
                ));
                break;
            }
        }
        match (outcome, cleanup_error) {
            (Ok(healthy), None) => Ok(healthy),
            (Err(error), _) | (_, Some(error)) => Err(error),
        }
    }

    fn read_system_self_test(&self, self_test_id: &str) -> Result<Option<SystemSelfTest>, String> {
        let statement = self.prepare(
            "SELECT self_test_id, kind, trigger_kind, schedule_period_key, started_at_unix_ms, \
             finished_at_unix_ms, status, checks_json, summary, evidence_path \
             FROM system_self_tests WHERE self_test_id=?1",
        )?;
        let self_test_id = c_string(self_test_id, "self-test identity")?;
        bind_text(self.database, statement.raw, 1, &self_test_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_system_self_test_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_alert_rule(&self, rule: &AlertRule) -> Result<AlertRule, String> {
        rule.validate().map_err(str::to_string)?;
        for channel_id in &rule.channel_ids {
            if self.read_alert_channel(channel_id)?.is_none() {
                return Err(format!(
                    "alert rule references missing destination {channel_id}"
                ));
            }
        }
        if let Some(existing) = self.read_alert_rule(&rule.rule_id)? {
            if existing.condition != rule.condition {
                return Err("an alert rule condition cannot be changed".into());
            }
            if rule.updated_at_unix_ms < existing.updated_at_unix_ms {
                return Err("alert rule update time moved backward".into());
            }
        }
        let channel_ids_json = serde_json::to_string(&rule.channel_ids)
            .map_err(|error| format!("could not encode alert rule destinations: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO alert_rules(rule_id, condition, enabled, severity, channel_ids_json, \
             dedupe_window_seconds, re_alert_after_seconds, scope_channel_id, notify_on_resolve, \
             updated_at_unix_ms, updated_by) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
             ON CONFLICT(rule_id) DO UPDATE SET enabled=excluded.enabled, severity=excluded.severity, \
             channel_ids_json=excluded.channel_ids_json, dedupe_window_seconds=excluded.dedupe_window_seconds, \
             re_alert_after_seconds=excluded.re_alert_after_seconds, scope_channel_id=excluded.scope_channel_id, \
             notify_on_resolve=excluded.notify_on_resolve, updated_at_unix_ms=excluded.updated_at_unix_ms, \
             updated_by=excluded.updated_by",
        )?;
        let values = [
            c_string(&rule.rule_id, "alert rule identity")?,
            c_string(alert_condition_name(rule.condition), "alert rule condition")?,
            c_string(alert_severity_name(rule.severity), "alert rule severity")?,
            c_string(&channel_ids_json, "alert rule destinations")?,
            c_string(
                rule.scope_channel_id.as_deref().unwrap_or_default(),
                "alert rule scope",
            )?,
            c_string(&rule.updated_by, "alert rule operator")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_i64(self.database, statement.raw, 3, i64::from(rule.enabled))?;
        bind_text(self.database, statement.raw, 4, &values[2])?;
        bind_text(self.database, statement.raw, 5, &values[3])?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            i64::from(rule.dedupe_window_seconds),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            i64::from(rule.re_alert_after_seconds),
        )?;
        bind_text(self.database, statement.raw, 8, &values[4])?;
        bind_i64(
            self.database,
            statement.raw,
            9,
            i64::from(rule.notify_on_resolve),
        )?;
        bind_i64(self.database, statement.raw, 10, rule.updated_at_unix_ms)?;
        bind_text(self.database, statement.raw, 11, &values[5])?;
        expect_done(self.database, statement.raw)?;
        self.read_alert_rule(&rule.rule_id)?
            .ok_or_else(|| "stored alert rule disappeared".into())
    }

    pub fn read_alert_rule(&self, rule_id: &str) -> Result<Option<AlertRule>, String> {
        let statement = self.prepare(
            "SELECT rule_id, condition, enabled, severity, channel_ids_json, dedupe_window_seconds, \
             re_alert_after_seconds, scope_channel_id, notify_on_resolve, updated_at_unix_ms, updated_by \
             FROM alert_rules WHERE rule_id=?1",
        )?;
        let rule_id = c_string(rule_id, "alert rule identity")?;
        bind_text(self.database, statement.raw, 1, &rule_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_rule_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_alert_rules(&self) -> Result<Vec<AlertRule>, String> {
        let statement = self.prepare(
            "SELECT rule_id, condition, enabled, severity, channel_ids_json, dedupe_window_seconds, \
             re_alert_after_seconds, scope_channel_id, notify_on_resolve, updated_at_unix_ms, updated_by \
             FROM alert_rules ORDER BY condition, rule_id",
        )?;
        let mut rules = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => rules.push(read_alert_rule_row(statement.raw)?),
                SQLITE_DONE => return Ok(rules),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn alert_rule_for_condition(
        &self,
        condition: AlertConditionKind,
    ) -> Result<Option<AlertRule>, String> {
        let statement = self.prepare(
            "SELECT rule_id, condition, enabled, severity, channel_ids_json, dedupe_window_seconds, \
             re_alert_after_seconds, scope_channel_id, notify_on_resolve, updated_at_unix_ms, updated_by \
             FROM alert_rules WHERE condition=?1",
        )?;
        let condition = c_string(alert_condition_name(condition), "alert rule condition")?;
        bind_text(self.database, statement.raw, 1, &condition)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_rule_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_alert_channel(
        &self,
        channel: &AlertChannel,
        secret: &AlertChannelSecret,
    ) -> Result<AlertChannel, String> {
        channel.validate().map_err(str::to_string)?;
        secret.validate(channel.kind)?;
        let redacted =
            redact_alert_channel_target(channel.kind, &secret.target).map_err(str::to_string)?;
        if channel.target_redacted != redacted || !channel.credential_configured {
            return Err("alert channel redaction or credential state is inconsistent".into());
        }
        let plaintext = serde_json::to_vec(&serde_json::json!({
            "target": secret.target,
            "endpoint": secret.endpoint,
            "sender": secret.sender,
            "identity": secret.identity,
            "credential": secret.credential,
        }))
        .map_err(|error| format!("could not encode alert destination secret: {error}"))?;
        let encrypted = protect_local_machine(&plaintext)?;
        let statement = self.prepare(
            "INSERT INTO alert_channels(channel_id, kind, label, enabled, target_redacted, \
             credential_configured, quiet_hours_start_utc, quiet_hours_end_utc, encrypted_configuration, \
             created_at_unix_ms, updated_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
             ON CONFLICT(channel_id) DO UPDATE SET kind=excluded.kind, label=excluded.label, \
             enabled=excluded.enabled, target_redacted=excluded.target_redacted, \
             credential_configured=excluded.credential_configured, \
             quiet_hours_start_utc=excluded.quiet_hours_start_utc, \
             quiet_hours_end_utc=excluded.quiet_hours_end_utc, \
             encrypted_configuration=excluded.encrypted_configuration, \
             updated_at_unix_ms=excluded.updated_at_unix_ms",
        )?;
        let values = [
            c_string(&channel.channel_id, "alert channel identity")?,
            c_string(alert_channel_kind_name(channel.kind), "alert channel kind")?,
            c_string(&channel.label, "alert channel label")?,
            c_string(&channel.target_redacted, "redacted alert target")?,
            c_string(
                channel.quiet_hours_start_utc.as_deref().unwrap_or_default(),
                "quiet-hours start",
            )?,
            c_string(
                channel.quiet_hours_end_utc.as_deref().unwrap_or_default(),
                "quiet-hours end",
            )?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_i64(self.database, statement.raw, 4, i64::from(channel.enabled))?;
        bind_text(self.database, statement.raw, 5, &values[3])?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            i64::from(channel.credential_configured),
        )?;
        bind_text(self.database, statement.raw, 7, &values[4])?;
        bind_text(self.database, statement.raw, 8, &values[5])?;
        bind_blob(self.database, statement.raw, 9, &encrypted)?;
        bind_i64(self.database, statement.raw, 10, channel.created_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 11, channel.updated_at_unix_ms)?;
        expect_done(self.database, statement.raw)?;
        self.read_alert_channel(&channel.channel_id)?
            .ok_or_else(|| "stored alert channel disappeared".into())
    }

    pub fn read_alert_channel(&self, channel_id: &str) -> Result<Option<AlertChannel>, String> {
        let statement = self.prepare(
            "SELECT channel_id, kind, label, enabled, target_redacted, credential_configured, \
             quiet_hours_start_utc, quiet_hours_end_utc, created_at_unix_ms, updated_at_unix_ms \
             FROM alert_channels WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "alert channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_channel_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_alert_channels(&self) -> Result<Vec<AlertChannel>, String> {
        let statement = self.prepare(
            "SELECT channel_id, kind, label, enabled, target_redacted, credential_configured, \
             quiet_hours_start_utc, quiet_hours_end_utc, created_at_unix_ms, updated_at_unix_ms \
             FROM alert_channels ORDER BY label, channel_id",
        )?;
        let mut channels = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => channels.push(read_alert_channel_row(statement.raw)?),
                SQLITE_DONE => return Ok(channels),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_alert_channel_secret(
        &self,
        channel_id: &str,
    ) -> Result<Option<AlertChannelSecret>, String> {
        let statement = self.prepare(
            "SELECT kind, encrypted_configuration FROM alert_channels WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "alert channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {}
            SQLITE_DONE => return Ok(None),
            _ => return Err(sqlite_error(self.database)),
        }
        let kind = parse_alert_channel_kind(&column_text(statement.raw, 0)?)?;
        let decrypted = unprotect_local_machine(&column_blob(statement.raw, 1)?)?;
        let value: serde_json::Value = serde_json::from_slice(&decrypted)
            .map_err(|error| format!("stored alert destination secret is invalid: {error}"))?;
        let optional = |name: &str| {
            value
                .get(name)
                .and_then(serde_json::Value::as_str)
                .map(str::to_string)
        };
        let secret = AlertChannelSecret {
            target: optional("target").ok_or("stored alert target is missing")?,
            endpoint: optional("endpoint"),
            sender: optional("sender"),
            identity: optional("identity"),
            credential: optional("credential").ok_or("stored alert credential is missing")?,
        };
        secret.validate(kind)?;
        Ok(Some(secret))
    }

    pub fn enqueue_alert_delivery(
        &self,
        delivery_id: &str,
        event: &AlertEvent,
        alert_channel_id: &str,
        notification_kind: AlertNotificationKind,
        not_before_unix_ms: i64,
        created_at_unix_ms: i64,
    ) -> Result<AlertDelivery, String> {
        event.validate().map_err(str::to_string)?;
        if self.read_alert_event(&event.event_id)?.as_ref() != Some(event) {
            return Err("alert delivery event is not the current durable event".into());
        }
        let channel = self
            .read_alert_channel(alert_channel_id)?
            .ok_or_else(|| "alert delivery channel does not exist".to_string())?;
        if !channel.enabled {
            return Err("alert delivery channel is disabled".into());
        }
        let payload =
            AlertDeliveryPayload::from_event(notification_kind, event).map_err(str::to_string)?;
        let delivery = AlertDelivery {
            delivery_id: delivery_id.into(),
            event_id: event.event_id.clone(),
            alert_channel_id: alert_channel_id.into(),
            notification_kind,
            state: AlertDeliveryState::Pending,
            attempts: 0,
            next_attempt_at_unix_ms: not_before_unix_ms,
            lease_expires_at_unix_ms: None,
            last_error: String::new(),
            signature: None,
            created_at_unix_ms,
            updated_at_unix_ms: created_at_unix_ms,
            sent_at_unix_ms: None,
        };
        delivery.validate().map_err(str::to_string)?;
        self.insert_alert_delivery(&delivery, &payload)?;
        self.read_alert_delivery(delivery_id)?
            .ok_or_else(|| "queued alert delivery disappeared".into())
    }

    pub fn enqueue_alert_channel_test(
        &self,
        event_id: &str,
        delivery_id: &str,
        alert_channel_id: &str,
        created_at_unix_ms: i64,
    ) -> Result<AlertDelivery, String> {
        let channel = self
            .read_alert_channel(alert_channel_id)?
            .ok_or_else(|| "alert test channel does not exist".to_string())?;
        let event = AlertEvent {
            event_id: event_id.into(),
            condition: AlertConditionKind::ServiceDown,
            severity: AlertSeverity::Info,
            state: AlertEventState::Resolved,
            resource_ref: alert_channel_id.into(),
            summary: "Operator requested an alert-channel test.".into(),
            detail: format!(
                "Test delivery for the configured {} destination.",
                alert_channel_kind_name(channel.kind)
            ),
            source_section: "S8".into(),
            first_observed_at_unix_ms: created_at_unix_ms,
            last_observed_at_unix_ms: created_at_unix_ms,
            resolved_at_unix_ms: Some(created_at_unix_ms),
            occurrence_count: 1,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        };
        event.validate().map_err(str::to_string)?;
        let payload = AlertDeliveryPayload::from_event(AlertNotificationKind::Test, &event)
            .map_err(str::to_string)?;
        let delivery = AlertDelivery {
            delivery_id: delivery_id.into(),
            event_id: event_id.into(),
            alert_channel_id: alert_channel_id.into(),
            notification_kind: AlertNotificationKind::Test,
            state: AlertDeliveryState::Pending,
            attempts: 0,
            next_attempt_at_unix_ms: created_at_unix_ms,
            lease_expires_at_unix_ms: None,
            last_error: String::new(),
            signature: None,
            created_at_unix_ms,
            updated_at_unix_ms: created_at_unix_ms,
            sent_at_unix_ms: None,
        };
        delivery.validate().map_err(str::to_string)?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.insert_alert(&event)?;
            self.insert_alert_delivery(&delivery, &payload)
        })();
        self.finish_transaction(outcome)?;
        self.read_alert_delivery(delivery_id)?
            .ok_or_else(|| "queued alert-channel test disappeared".into())
    }

    fn insert_alert_delivery(
        &self,
        delivery: &AlertDelivery,
        payload: &AlertDeliveryPayload,
    ) -> Result<(), String> {
        let payload_json = serde_json::to_string(&payload)
            .map_err(|error| format!("could not encode alert delivery payload: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO alert_deliveries(delivery_id, event_id, alert_channel_id, notification_kind, \
             state, attempts, next_attempt_at_unix_ms, lease_expires_at_unix_ms, last_error, \
             signature, payload_json, created_at_unix_ms, updated_at_unix_ms, sent_at_unix_ms) \
             VALUES(?1, ?2, ?3, ?4, 'pending', 0, ?5, -1, '', '', ?6, ?7, ?7, -1)",
        )?;
        let values = [
            c_string(&delivery.delivery_id, "alert delivery identity")?,
            c_string(&delivery.event_id, "alert event identity")?,
            c_string(&delivery.alert_channel_id, "alert channel identity")?,
            c_string(
                alert_notification_kind_name(delivery.notification_kind),
                "alert notification kind",
            )?,
            c_string(&payload_json, "alert delivery payload")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_text(self.database, statement.raw, 4, &values[3])?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            delivery.next_attempt_at_unix_ms,
        )?;
        bind_text(self.database, statement.raw, 6, &values[4])?;
        bind_i64(self.database, statement.raw, 7, delivery.created_at_unix_ms)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_alert_delivery(&self, delivery_id: &str) -> Result<Option<AlertDelivery>, String> {
        let statement = self.prepare(
            "SELECT delivery_id, event_id, alert_channel_id, notification_kind, state, attempts, \
             next_attempt_at_unix_ms, lease_expires_at_unix_ms, last_error, signature, \
             created_at_unix_ms, updated_at_unix_ms, sent_at_unix_ms \
             FROM alert_deliveries WHERE delivery_id=?1",
        )?;
        let delivery_id = c_string(delivery_id, "alert delivery identity")?;
        bind_text(self.database, statement.raw, 1, &delivery_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_delivery_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_alert_deliveries(&self) -> Result<Vec<AlertDelivery>, String> {
        let statement = self.prepare(
            "SELECT delivery_id, event_id, alert_channel_id, notification_kind, state, attempts, \
             next_attempt_at_unix_ms, lease_expires_at_unix_ms, last_error, signature, \
             created_at_unix_ms, updated_at_unix_ms, sent_at_unix_ms \
             FROM alert_deliveries ORDER BY created_at_unix_ms, delivery_id",
        )?;
        let mut deliveries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => deliveries.push(read_alert_delivery_row(statement.raw)?),
                SQLITE_DONE => return Ok(deliveries),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_recent_alert_deliveries(&self, limit: u32) -> Result<Vec<AlertDelivery>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("alert delivery limit must be between 1 and 1000".into());
        }
        let statement = self.prepare(
            "SELECT delivery_id, event_id, alert_channel_id, notification_kind, state, attempts, \
             next_attempt_at_unix_ms, lease_expires_at_unix_ms, last_error, signature, \
             created_at_unix_ms, updated_at_unix_ms, sent_at_unix_ms \
             FROM alert_deliveries ORDER BY created_at_unix_ms DESC, delivery_id DESC LIMIT ?1",
        )?;
        bind_i64(self.database, statement.raw, 1, i64::from(limit))?;
        let mut deliveries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => deliveries.push(read_alert_delivery_row(statement.raw)?),
                SQLITE_DONE => return Ok(deliveries),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_alert_delivery_payload(
        &self,
        delivery_id: &str,
    ) -> Result<Option<AlertDeliveryPayload>, String> {
        let statement =
            self.prepare("SELECT payload_json FROM alert_deliveries WHERE delivery_id=?1")?;
        let delivery_id = c_string(delivery_id, "alert delivery identity")?;
        bind_text(self.database, statement.raw, 1, &delivery_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let payload: AlertDeliveryPayload =
                    serde_json::from_str(&column_text(statement.raw, 0)?).map_err(|error| {
                        format!("stored alert delivery payload is invalid: {error}")
                    })?;
                payload.validate().map_err(str::to_string)?;
                Ok(Some(payload))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn claim_due_alert_delivery(
        &self,
        now_unix_ms: i64,
        lease_duration_ms: i64,
    ) -> Result<Option<AlertDelivery>, String> {
        if now_unix_ms < 0 || !(1_000..=300_000).contains(&lease_duration_ms) {
            return Err("alert delivery lease request is invalid".into());
        }
        let lease_expires_at_unix_ms = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "alert delivery lease time overflowed".to_string())?;
        self.execute("BEGIN IMMEDIATE")?;
        let mut claimed = None;
        let outcome = (|| {
            let recovery = self.prepare(
                "UPDATE alert_deliveries SET state='pending', lease_expires_at_unix_ms=-1, \
                 last_error=CASE WHEN last_error='' THEN 'delivery lease expired before completion' \
                 ELSE substr(last_error || '; delivery lease expired before completion', 1, 1000) END, \
                 updated_at_unix_ms=?1 WHERE state='leased' AND lease_expires_at_unix_ms<=?1",
            )?;
            bind_i64(self.database, recovery.raw, 1, now_unix_ms)?;
            expect_done(self.database, recovery.raw)?;

            let due = self.prepare(
                "SELECT delivery_id FROM alert_deliveries WHERE state='pending' \
                 AND next_attempt_at_unix_ms<=?1 ORDER BY next_attempt_at_unix_ms, created_at_unix_ms, \
                 delivery_id LIMIT 1",
            )?;
            bind_i64(self.database, due.raw, 1, now_unix_ms)?;
            if unsafe { sqlite3_step(due.raw) } == SQLITE_DONE {
                return Ok(());
            }
            let delivery_id = column_text(due.raw, 0)?;
            let update = self.prepare(
                "UPDATE alert_deliveries SET state='leased', attempts=attempts+1, \
                 lease_expires_at_unix_ms=?1, updated_at_unix_ms=?2 \
                 WHERE delivery_id=?3 AND state='pending' AND next_attempt_at_unix_ms<=?2",
            )?;
            let delivery_id_value = c_string(&delivery_id, "alert delivery identity")?;
            bind_i64(self.database, update.raw, 1, lease_expires_at_unix_ms)?;
            bind_i64(self.database, update.raw, 2, now_unix_ms)?;
            bind_text(self.database, update.raw, 3, &delivery_id_value)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("due alert delivery could not be leased".into());
            }
            claimed = self.read_alert_delivery(&delivery_id)?;
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        Ok(claimed)
    }

    pub fn complete_alert_delivery(
        &self,
        delivery_id: &str,
        sent_at_unix_ms: i64,
        signature: Option<&str>,
    ) -> Result<AlertDelivery, String> {
        if sent_at_unix_ms < 0
            || signature.is_some_and(|value| {
                value.is_empty() || value.len() > 200 || value.chars().any(char::is_control)
            })
        {
            return Err("alert delivery completion is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE alert_deliveries SET state='sent', lease_expires_at_unix_ms=-1, signature=?1, \
             updated_at_unix_ms=?2, sent_at_unix_ms=?2 WHERE delivery_id=?3 AND state='leased' \
             AND updated_at_unix_ms<=?2",
        )?;
        let signature = c_string(signature.unwrap_or_default(), "alert delivery signature")?;
        let delivery_id = c_string(delivery_id, "alert delivery identity")?;
        bind_text(self.database, statement.raw, 1, &signature)?;
        bind_i64(self.database, statement.raw, 2, sent_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &delivery_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() != 1 {
            return Err("only a leased alert delivery can be completed".into());
        }
        self.read_alert_delivery(delivery_id.to_str().map_err(|_| "invalid alert identity")?)?
            .ok_or_else(|| "completed alert delivery disappeared".into())
    }

    pub fn fail_alert_delivery(
        &self,
        delivery_id: &str,
        failed_at_unix_ms: i64,
        error: &str,
        maximum_attempts: u32,
        base_backoff_ms: i64,
    ) -> Result<AlertDelivery, String> {
        if failed_at_unix_ms < 0
            || error.trim().is_empty()
            || error.len() > 1_000
            || error.chars().any(char::is_control)
            || !(1..=20).contains(&maximum_attempts)
            || !(1_000..=86_400_000).contains(&base_backoff_ms)
        {
            return Err("alert delivery failure is invalid".into());
        }
        let current = self
            .read_alert_delivery(delivery_id)?
            .ok_or_else(|| "alert delivery does not exist".to_string())?;
        if current.state != AlertDeliveryState::Leased
            || current.attempts == 0
            || failed_at_unix_ms < current.updated_at_unix_ms
        {
            return Err("only a current leased alert delivery can fail".into());
        }
        let dead_letter = current.attempts >= maximum_attempts;
        let exponent = current.attempts.saturating_sub(1).min(20);
        let backoff_ms = base_backoff_ms
            .saturating_mul(1_i64 << exponent)
            .min(86_400_000);
        let next_attempt_at_unix_ms = if dead_letter {
            current.next_attempt_at_unix_ms
        } else {
            failed_at_unix_ms
                .checked_add(backoff_ms)
                .ok_or_else(|| "alert delivery retry time overflowed".to_string())?
        };
        let statement = self.prepare(
            "UPDATE alert_deliveries SET state=?1, next_attempt_at_unix_ms=?2, \
             lease_expires_at_unix_ms=-1, last_error=?3, updated_at_unix_ms=?4 \
             WHERE delivery_id=?5 AND state='leased' AND attempts=?6",
        )?;
        let state = c_string(
            if dead_letter {
                "dead_letter"
            } else {
                "pending"
            },
            "alert delivery state",
        )?;
        let error = c_string(error, "alert delivery error")?;
        let delivery_id = c_string(delivery_id, "alert delivery identity")?;
        bind_text(self.database, statement.raw, 1, &state)?;
        bind_i64(self.database, statement.raw, 2, next_attempt_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &error)?;
        bind_i64(self.database, statement.raw, 4, failed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 5, &delivery_id)?;
        bind_i64(self.database, statement.raw, 6, i64::from(current.attempts))?;
        expect_done(self.database, statement.raw)?;
        if self.changes() != 1 {
            return Err("alert delivery lease changed before failure was recorded".into());
        }
        self.read_alert_delivery(delivery_id.to_str().map_err(|_| "invalid alert identity")?)?
            .ok_or_else(|| "failed alert delivery disappeared".into())
    }

    pub fn observe_alert(&self, proposed: &AlertEvent) -> Result<AlertEvent, String> {
        validate_new_alert_observation(proposed)?;
        self.execute("BEGIN IMMEDIATE")?;
        let mut recorded = None;
        let outcome = (|| {
            let (event, _) = self.observe_alert_locked(proposed)?;
            recorded = Some(event);
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        recorded.ok_or_else(|| "alert transaction produced no event".into())
    }

    pub fn observe_alert_and_route(&self, proposed: &AlertEvent) -> Result<AlertEvent, String> {
        validate_new_alert_observation(proposed)?;
        self.execute("BEGIN IMMEDIATE")?;
        let mut recorded = None;
        let outcome = (|| {
            recorded = Some(self.observe_alert_and_route_locked(proposed)?);
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        recorded.ok_or_else(|| "alert routing transaction produced no event".into())
    }

    /// Records one operator-visible automatic materialization failure per
    /// unchanged typed class. The alert and its initial outbox deliveries share
    /// this transaction, which is deliberately separate from the failed
    /// materialization write transaction.
    pub fn observe_schedule_materialization_failure_and_route(
        &self,
        channel_id: &str,
        failure_class: ScheduleMaterializationFailureClass,
        observed_at_unix_ms: i64,
    ) -> Result<AlertEvent, String> {
        if !is_canonical_uuid(channel_id) || observed_at_unix_ms < 0 {
            return Err("automatic materialization failure alert input is invalid".into());
        }
        let summary = "Automatic recurring schedule materialization needs operator attention.";
        let detail = schedule_materialization_failure_detail(failure_class);
        self.execute("BEGIN IMMEDIATE")?;
        let mut recorded = None;
        let outcome = (|| {
            let current = self.read_firing_alert(
                AlertConditionKind::ScheduleMaterializationFailure,
                channel_id,
            )?;
            if let Some(current) = &current
                && current.summary == summary
                && current.detail == detail
                && current.source_section == "S19"
            {
                recorded = Some(current.clone());
                return Ok(());
            }
            let event_id = match current {
                Some(current) => current.event_id,
                None => format!(
                    "schedule-materialization-failure-{channel_id}-{}",
                    self.next_alert_episode_ordinal_locked(
                        AlertConditionKind::ScheduleMaterializationFailure,
                        channel_id,
                    )?
                ),
            };
            recorded = Some(self.observe_alert_and_route_locked(&AlertEvent {
                event_id,
                condition: AlertConditionKind::ScheduleMaterializationFailure,
                severity: AlertSeverity::Warning,
                state: AlertEventState::Firing,
                resource_ref: channel_id.into(),
                summary: summary.into(),
                detail: detail.into(),
                source_section: "S19".into(),
                first_observed_at_unix_ms: observed_at_unix_ms,
                last_observed_at_unix_ms: observed_at_unix_ms,
                resolved_at_unix_ms: None,
                occurrence_count: 1,
                acknowledged_at_unix_ms: None,
                acknowledged_by: None,
            })?);
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        recorded.ok_or_else(|| "automatic materialization failure alert produced no event".into())
    }

    fn observe_alert_and_route_locked(&self, proposed: &AlertEvent) -> Result<AlertEvent, String> {
        validate_new_alert_observation(proposed)?;
        let rule = self
            .alert_rule_for_condition(proposed.condition)?
            .ok_or_else(|| "alert condition has no configured rule".to_string())?;
        if !rule.enabled
            || rule
                .scope_channel_id
                .as_deref()
                .is_some_and(|scope| scope != proposed.resource_ref)
        {
            return Ok(proposed.clone());
        }
        let mut ruled = proposed.clone();
        ruled.severity = rule.severity;
        let (event, is_first_observation) = self.observe_alert_locked(&ruled)?;
        if is_first_observation {
            self.route_alert_locked(
                &event,
                &rule,
                AlertNotificationKind::Firing,
                event.first_observed_at_unix_ms,
            )?;
        } else if rule.re_alert_after_seconds > 0 {
            self.route_due_realerts_locked(&event, &rule, event.last_observed_at_unix_ms)?;
        }
        Ok(event)
    }

    fn observe_alert_locked(&self, proposed: &AlertEvent) -> Result<(AlertEvent, bool), String> {
        if let Some(mut current) =
            self.read_firing_alert(proposed.condition, &proposed.resource_ref)?
        {
            if proposed.last_observed_at_unix_ms < current.last_observed_at_unix_ms {
                return Err("alert observation time moved backward".into());
            }
            let statement = self.prepare(
                "UPDATE alert_events SET severity=?1, summary=?2, detail=?3, source_section=?4, \
                 last_observed_at_unix_ms=?5, occurrence_count=occurrence_count+1 WHERE event_id=?6 AND state='firing'",
            )?;
            let severity = c_string(alert_severity_name(proposed.severity), "alert severity")?;
            let summary = c_string(&proposed.summary, "alert summary")?;
            let detail = c_string(&proposed.detail, "alert detail")?;
            let source = c_string(&proposed.source_section, "alert source")?;
            let event_id = c_string(&current.event_id, "alert identity")?;
            bind_text(self.database, statement.raw, 1, &severity)?;
            bind_text(self.database, statement.raw, 2, &summary)?;
            bind_text(self.database, statement.raw, 3, &detail)?;
            bind_text(self.database, statement.raw, 4, &source)?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                proposed.last_observed_at_unix_ms,
            )?;
            bind_text(self.database, statement.raw, 6, &event_id)?;
            expect_done(self.database, statement.raw)?;
            current.severity = proposed.severity;
            current.summary.clone_from(&proposed.summary);
            current.detail.clone_from(&proposed.detail);
            current.source_section.clone_from(&proposed.source_section);
            current.last_observed_at_unix_ms = proposed.last_observed_at_unix_ms;
            current.occurrence_count = current
                .occurrence_count
                .checked_add(1)
                .ok_or_else(|| "alert occurrence count overflowed".to_string())?;
            return Ok((current, false));
        }
        self.insert_alert(proposed)?;
        Ok((proposed.clone(), true))
    }

    fn next_alert_episode_ordinal_locked(
        &self,
        condition: AlertConditionKind,
        resource_ref: &str,
    ) -> Result<u64, String> {
        let statement = self
            .prepare("SELECT COUNT(*) FROM alert_events WHERE condition=?1 AND resource_ref=?2")?;
        let condition = c_string(alert_condition_name(condition), "alert condition")?;
        let resource_ref = c_string(resource_ref, "alert resource")?;
        bind_text(self.database, statement.raw, 1, &condition)?;
        bind_text(self.database, statement.raw, 2, &resource_ref)?;
        if unsafe { sqlite3_step(statement.raw) } != SQLITE_ROW {
            return Err("alert episode count is unavailable".into());
        }
        let count = u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 0) })
            .map_err(|_| "alert episode count is invalid".to_string())?;
        count
            .checked_add(1)
            .ok_or_else(|| "alert episode count overflowed".into())
    }

    fn route_alert_locked(
        &self,
        event: &AlertEvent,
        rule: &AlertRule,
        notification_kind: AlertNotificationKind,
        observed_at_unix_ms: i64,
    ) -> Result<(), String> {
        for channel in self.list_alert_channels()?.into_iter().filter(|channel| {
            channel.enabled
                && (rule.channel_ids.is_empty() || rule.channel_ids.contains(&channel.channel_id))
        }) {
            let not_before_unix_ms =
                quiet_hours_release(&channel, event.severity, observed_at_unix_ms)?;
            let delivery_id = deterministic_alert_delivery_id(
                &event.event_id,
                &channel.channel_id,
                notification_kind,
                observed_at_unix_ms,
            );
            self.enqueue_alert_delivery(
                &delivery_id,
                event,
                &channel.channel_id,
                notification_kind,
                not_before_unix_ms,
                observed_at_unix_ms,
            )?;
        }
        Ok(())
    }

    fn route_due_realerts_locked(
        &self,
        event: &AlertEvent,
        rule: &AlertRule,
        observed_at_unix_ms: i64,
    ) -> Result<(), String> {
        let interval_ms = i64::from(rule.re_alert_after_seconds) * 1_000;
        for channel in self.list_alert_channels()?.into_iter().filter(|channel| {
            channel.enabled
                && (rule.channel_ids.is_empty() || rule.channel_ids.contains(&channel.channel_id))
        }) {
            if !self.realert_is_due(
                &event.event_id,
                &channel.channel_id,
                observed_at_unix_ms,
                interval_ms,
            )? {
                continue;
            }
            let not_before_unix_ms =
                quiet_hours_release(&channel, event.severity, observed_at_unix_ms)?;
            let delivery_id = deterministic_alert_delivery_id(
                &event.event_id,
                &channel.channel_id,
                AlertNotificationKind::Realert,
                observed_at_unix_ms,
            );
            self.enqueue_alert_delivery(
                &delivery_id,
                event,
                &channel.channel_id,
                AlertNotificationKind::Realert,
                not_before_unix_ms,
                observed_at_unix_ms,
            )?;
        }
        Ok(())
    }

    fn realert_is_due(
        &self,
        event_id: &str,
        alert_channel_id: &str,
        observed_at_unix_ms: i64,
        interval_ms: i64,
    ) -> Result<bool, String> {
        let statement = self.prepare(
            "SELECT state, sent_at_unix_ms, updated_at_unix_ms FROM alert_deliveries \
             WHERE event_id=?1 AND alert_channel_id=?2 AND notification_kind IN ('firing','realert') \
             ORDER BY created_at_unix_ms DESC, delivery_id DESC LIMIT 1",
        )?;
        let event_id = c_string(event_id, "alert event identity")?;
        let channel_id = c_string(alert_channel_id, "alert channel identity")?;
        bind_text(self.database, statement.raw, 1, &event_id)?;
        bind_text(self.database, statement.raw, 2, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let state = parse_alert_delivery_state(&column_text(statement.raw, 0)?)?;
                let sent_at = unsafe { sqlite3_column_int64(statement.raw, 1) };
                let updated_at = unsafe { sqlite3_column_int64(statement.raw, 2) };
                let anchor = match state {
                    AlertDeliveryState::Sent if sent_at >= 0 => Some(sent_at),
                    AlertDeliveryState::DeadLetter => Some(updated_at),
                    AlertDeliveryState::Pending | AlertDeliveryState::Leased => None,
                    AlertDeliveryState::Sent => None,
                };
                Ok(anchor.is_some_and(|anchor| {
                    observed_at_unix_ms.saturating_sub(anchor) >= interval_ms
                }))
            }
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn acknowledge_alert(
        &self,
        event_id: &str,
        acknowledged_at_unix_ms: i64,
        acknowledged_by: &str,
    ) -> Result<Option<AlertEvent>, String> {
        if acknowledged_at_unix_ms < 0
            || acknowledged_by.trim().is_empty()
            || acknowledged_by.len() > 120
        {
            return Err("alert acknowledgment is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE alert_events SET acknowledged_at_unix_ms=?1, acknowledged_by=?2 \
             WHERE event_id=?3 AND acknowledged_at_unix_ms=-1",
        )?;
        let operator = c_string(acknowledged_by, "alert operator")?;
        let event_id = c_string(event_id, "alert identity")?;
        bind_i64(self.database, statement.raw, 1, acknowledged_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &operator)?;
        bind_text(self.database, statement.raw, 3, &event_id)?;
        expect_done(self.database, statement.raw)?;
        self.read_alert_event(event_id.to_str().map_err(|_| "invalid alert identity")?)
    }

    pub fn resolve_alert(
        &self,
        condition: AlertConditionKind,
        resource_ref: &str,
        resolved_at_unix_ms: i64,
    ) -> Result<Option<AlertEvent>, String> {
        if resolved_at_unix_ms < 0 {
            return Err("alert resolution time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let mut resolved = None;
        let outcome = (|| {
            resolved = self.resolve_alert_locked(condition, resource_ref, resolved_at_unix_ms)?;
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        Ok(resolved)
    }

    fn resolve_alert_locked(
        &self,
        condition: AlertConditionKind,
        resource_ref: &str,
        resolved_at_unix_ms: i64,
    ) -> Result<Option<AlertEvent>, String> {
        if resolved_at_unix_ms < 0 {
            return Err("alert resolution time must be nonnegative".into());
        }
        let statement = self.prepare(
            "UPDATE alert_events SET state='resolved', resolved_at_unix_ms=?1 \
             WHERE condition=?2 AND resource_ref=?3 AND state='firing' AND last_observed_at_unix_ms<=?1",
        )?;
        let condition_name = c_string(alert_condition_name(condition), "alert condition")?;
        let resource = c_string(resource_ref, "alert resource")?;
        bind_i64(self.database, statement.raw, 1, resolved_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &condition_name)?;
        bind_text(self.database, statement.raw, 3, &resource)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 0 {
            return Ok(None);
        }
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events \
             WHERE condition=?1 AND resource_ref=?2 AND resolved_at_unix_ms=?3 ORDER BY rowid DESC LIMIT 1",
        )?;
        bind_text(self.database, statement.raw, 1, &condition_name)?;
        bind_text(self.database, statement.raw, 2, &resource)?;
        bind_i64(self.database, statement.raw, 3, resolved_at_unix_ms)?;
        let event = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => read_alert_event_row(statement.raw)?,
            _ => return Err("resolved alert disappeared".into()),
        };
        if let Some(rule) = self.alert_rule_for_condition(event.condition)?
            && rule.enabled
            && rule.notify_on_resolve
            && !rule
                .scope_channel_id
                .as_deref()
                .is_some_and(|scope| scope != event.resource_ref)
        {
            self.route_alert_locked(
                &event,
                &rule,
                AlertNotificationKind::Resolved,
                resolved_at_unix_ms,
            )?;
        }
        Ok(Some(event))
    }

    pub fn read_alert_event(&self, event_id: &str) -> Result<Option<AlertEvent>, String> {
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events WHERE event_id=?1",
        )?;
        let event_id = c_string(event_id, "alert identity")?;
        bind_text(self.database, statement.raw, 1, &event_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_event_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_alert_events(&self) -> Result<Vec<AlertEvent>, String> {
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events \
             ORDER BY CASE state WHEN 'firing' THEN 0 ELSE 1 END, last_observed_at_unix_ms DESC, event_id",
        )?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_alert_event_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_recent_alert_events(&self, limit: u32) -> Result<Vec<AlertEvent>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("alert event limit must be between 1 and 1000".into());
        }
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events \
             ORDER BY last_observed_at_unix_ms DESC, event_id DESC LIMIT ?1",
        )?;
        bind_i64(self.database, statement.raw, 1, i64::from(limit))?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_alert_event_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn read_firing_alert(
        &self,
        condition: AlertConditionKind,
        resource_ref: &str,
    ) -> Result<Option<AlertEvent>, String> {
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events \
             WHERE condition=?1 AND resource_ref=?2 AND state='firing'",
        )?;
        let condition = c_string(alert_condition_name(condition), "alert condition")?;
        let resource_ref = c_string(resource_ref, "alert resource")?;
        bind_text(self.database, statement.raw, 1, &condition)?;
        bind_text(self.database, statement.raw, 2, &resource_ref)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_event_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn insert_alert(&self, event: &AlertEvent) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO alert_events(event_id, condition, severity, state, resource_ref, summary, \
             detail, source_section, first_observed_at_unix_ms, last_observed_at_unix_ms, \
             resolved_at_unix_ms, occurrence_count, acknowledged_at_unix_ms, acknowledged_by) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
        )?;
        let values = [
            c_string(&event.event_id, "alert identity")?,
            c_string(alert_condition_name(event.condition), "alert condition")?,
            c_string(alert_severity_name(event.severity), "alert severity")?,
            c_string(alert_event_state_name(event.state), "alert state")?,
            c_string(&event.resource_ref, "alert resource")?,
            c_string(&event.summary, "alert summary")?,
            c_string(&event.detail, "alert detail")?,
            c_string(&event.source_section, "alert source")?,
        ];
        for (offset, value) in values.iter().enumerate() {
            bind_text(
                self.database,
                statement.raw,
                i32::try_from(offset + 1).unwrap(),
                value,
            )?;
        }
        bind_i64(
            self.database,
            statement.raw,
            9,
            event.first_observed_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            event.last_observed_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            11,
            event.resolved_at_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            12,
            checked_i64(event.occurrence_count, "alert occurrence count")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            13,
            event.acknowledged_at_unix_ms.unwrap_or(-1),
        )?;
        let acknowledged_by = c_string(
            event.acknowledged_by.as_deref().unwrap_or_default(),
            "alert operator",
        )?;
        bind_text(self.database, statement.raw, 14, &acknowledged_by)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_media_asset(&self, asset_id: &str) -> Result<Option<MediaAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, media_path, duration_ms, readiness FROM media_assets WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_media_asset_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    /// Admit an immutable, content-addressed PNG/JPEG into the CG image library.
    /// The database stores only the path derived from `managed_cg_image_path`; a caller-supplied
    /// path is never accepted as authority.
    pub fn admit_cg_image_asset(&self, bytes: &[u8]) -> Result<CgImageAsset, String> {
        let asset_id = format!("{:x}", Sha256::digest(bytes));
        let mime_type = if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
            "image/png"
        } else if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
            "image/jpeg"
        } else {
            return Err("only PNG and JPEG images are supported".into());
        };
        let image_dir = self.validated_cg_image_root()?;
        let path = managed_cg_image_path(&image_dir, &asset_id, mime_type)?;
        let asset = inspect_cg_image(&asset_id, bytes)?;

        if path.exists() {
            let metadata = fs::symlink_metadata(&path)
                .map_err(|error| format!("inspect immutable image: {error}"))?;
            if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            {
                return Err("managed image path is not a regular non-reparse file".into());
            }
            let existing =
                fs::read(&path).map_err(|error| format!("read immutable image: {error}"))?;
            if existing != bytes {
                return Err("managed image path already contains different bytes".into());
            }
        } else {
            let sequence = CG_IMAGE_TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let temp_path = image_dir.join(format!(
                ".{}.part-{}-{}",
                asset.asset_id,
                std::process::id(),
                sequence
            ));
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temp_path)
                .map_err(|error| format!("create immutable image: {error}"))?;
            if let Err(error) = file.write_all(bytes).and_then(|_| file.sync_all()) {
                let _ = fs::remove_file(&temp_path);
                return Err(format!("write immutable image: {error}"));
            }
            if let Err(error) = fs::rename(&temp_path, &path) {
                let _ = fs::remove_file(&temp_path);
                if !path.exists() {
                    return Err(format!("publish immutable image: {error}"));
                }
                let existing = fs::read(&path)
                    .map_err(|read_error| format!("read raced image: {read_error}"))?;
                if existing != bytes {
                    return Err("managed image path already contains different bytes".into());
                }
            }
        }

        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if let Some(existing) = self.read_cg_image_asset(&asset.asset_id)? {
                if existing != asset {
                    return Err("CG image identity already has different metadata".into());
                }
                return Ok(existing);
            }
            let statement = self.prepare(
                "INSERT INTO cg_image_assets(asset_id, mime_type, byte_size, width, height, readiness) VALUES(?1, ?2, ?3, ?4, ?5, ?6)",
            )?;
            let asset_id = c_string(&asset.asset_id, "image identity")?;
            let mime_type = c_string(&asset.mime_type, "image MIME")?;
            let readiness = c_string(cg_image_readiness_name(asset.readiness), "image readiness")?;
            bind_text(self.database, statement.raw, 1, &asset_id)?;
            bind_text(self.database, statement.raw, 2, &mime_type)?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(asset.byte_size, "image byte size")?,
            )?;
            bind_i64(self.database, statement.raw, 4, i64::from(asset.width))?;
            bind_i64(self.database, statement.raw, 5, i64::from(asset.height))?;
            bind_text(self.database, statement.raw, 6, &readiness)?;
            expect_done(self.database, statement.raw)?;
            Ok(asset)
        })();
        match outcome {
            Ok(asset) => match self.execute("COMMIT") {
                Ok(()) => Ok(asset),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_cg_image_asset(&self, asset_id: &str) -> Result<Option<CgImageAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, mime_type, byte_size, width, height, readiness FROM cg_image_assets WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "image identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_image_asset_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_cg_image_assets(&self) -> Result<Vec<CgImageAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, mime_type, byte_size, width, height, readiness FROM cg_image_assets ORDER BY asset_id",
        )?;
        let mut assets = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => assets.push(read_cg_image_asset_row(statement.raw)?),
                SQLITE_DONE => return Ok(assets),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    /// List the managed CG image library with current filesystem readiness rather than trusting
    /// the last database state. This is the operator-facing view; snapshot loading remains scoped
    /// to only the identities referenced by the active board.
    pub fn list_verified_cg_image_assets(&self) -> Result<Vec<CgImageAsset>, String> {
        self.list_cg_image_assets().map(|assets| {
            assets
                .into_iter()
                .map(|asset| self.verify_cg_image_asset(asset))
                .collect()
        })
    }

    pub fn read_channel_fallback_slate(
        &self,
        channel_id: &str,
    ) -> Result<Option<ChannelFallbackSlate>, String> {
        if !is_canonical_uuid(channel_id) {
            return Err("fallback slate channel identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT s.channel_id, a.asset_id, a.mime_type, a.byte_size, a.width, a.height, \
                    a.readiness, s.revision, s.created_at_unix_ms, s.updated_at_unix_ms, s.updated_by \
             FROM channel_fallback_slates s \
             JOIN cg_image_assets a ON a.asset_id=s.image_asset_id \
             WHERE s.channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "fallback slate channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let image_asset =
                    self.verify_cg_image_asset(read_cg_image_asset_columns(statement.raw, 1)?);
                let selection = ChannelFallbackSlate {
                    channel_id: column_text(statement.raw, 0)?,
                    image_asset,
                    revision: column_u64(statement.raw, 7, "fallback slate revision")?,
                    created_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 8) },
                    updated_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 9) },
                    updated_by: column_text(statement.raw, 10)?,
                };
                validate_fallback_slate_selection(&selection)?;
                Ok(Some(selection))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn write_channel_fallback_slate(
        &self,
        channel_id: &str,
        image_asset_id: &str,
        expected_revision: u64,
        actor: &str,
        occurred_at_unix_ms: i64,
        audit_id: &str,
    ) -> Result<ChannelFallbackSlate, FallbackSlateWriteError> {
        if !is_canonical_uuid(channel_id)
            || !is_sha256_identity(image_asset_id)
            || !is_canonical_uuid(audit_id)
            || occurred_at_unix_ms < 0
            || !secret_field(actor, 200)
        {
            return Err(FallbackSlateWriteError::Invalid(
                "fallback slate write facts are invalid or unbounded".into(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(FallbackSlateWriteError::Storage)?;
        let outcome = (|| {
            let channel = self
                .list_channels()
                .map_err(FallbackSlateWriteError::Storage)?
                .into_iter()
                .find(|channel| channel.channel_id == channel_id)
                .ok_or(FallbackSlateWriteError::NotFound)?;
            if channel.enabled {
                return Err(FallbackSlateWriteError::MustBeStopped);
            }
            let asset = self
                .read_cg_image_asset(image_asset_id)
                .map_err(FallbackSlateWriteError::Storage)?
                .map(|asset| self.verify_cg_image_asset(asset))
                .filter(|asset| asset.readiness == CgImageReadiness::Ready)
                .ok_or(FallbackSlateWriteError::ImageNotReady)?;
            let current = self
                .read_channel_fallback_slate(channel_id)
                .map_err(FallbackSlateWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(FallbackSlateWriteError::Conflict(current_revision));
            }
            if current
                .as_ref()
                .is_some_and(|value| occurred_at_unix_ms < value.updated_at_unix_ms)
            {
                return Err(FallbackSlateWriteError::Invalid(
                    "fallback slate write time precedes current authority".into(),
                ));
            }
            let revision = current_revision.checked_add(1).ok_or_else(|| {
                FallbackSlateWriteError::Invalid("fallback slate revision overflow".into())
            })?;
            let created_at_unix_ms = current
                .as_ref()
                .map_or(occurred_at_unix_ms, |value| value.created_at_unix_ms);
            let statement = self
                .prepare(
                    "INSERT INTO channel_fallback_slates(\
                       channel_id, image_asset_id, revision, created_at_unix_ms, updated_at_unix_ms, updated_by\
                     ) VALUES(?1, ?2, ?3, ?4, ?5, ?6) \
                     ON CONFLICT(channel_id) DO UPDATE SET \
                       image_asset_id=excluded.image_asset_id, revision=excluded.revision, \
                       updated_at_unix_ms=excluded.updated_at_unix_ms, updated_by=excluded.updated_by",
                )
                .map_err(FallbackSlateWriteError::Storage)?;
            let channel = c_string(channel_id, "fallback slate channel identity")
                .map_err(FallbackSlateWriteError::Storage)?;
            let image = c_string(&asset.asset_id, "fallback slate image identity")
                .map_err(FallbackSlateWriteError::Storage)?;
            let actor_value = c_string(actor, "fallback slate actor")
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &channel)
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_text(self.database, statement.raw, 2, &image)
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(revision, "fallback slate revision")
                    .map_err(FallbackSlateWriteError::Storage)?,
            )
            .map_err(FallbackSlateWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 4, created_at_unix_ms)
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 5, occurred_at_unix_ms)
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_text(self.database, statement.raw, 6, &actor_value)
                .map_err(FallbackSlateWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(FallbackSlateWriteError::Storage)?;

            let audit = self
                .prepare(
                    "INSERT INTO channel_fallback_slate_audit(\
                       audit_id, channel_id, image_asset_id, revision, actor, occurred_at_unix_ms\
                     ) VALUES(?1, ?2, ?3, ?4, ?5, ?6)",
                )
                .map_err(FallbackSlateWriteError::Storage)?;
            let audit_identity = c_string(audit_id, "fallback slate audit identity")
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_text(self.database, audit.raw, 1, &audit_identity)
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_text(self.database, audit.raw, 2, &channel)
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_text(self.database, audit.raw, 3, &image)
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_i64(
                self.database,
                audit.raw,
                4,
                checked_i64(revision, "fallback slate audit revision")
                    .map_err(FallbackSlateWriteError::Storage)?,
            )
            .map_err(FallbackSlateWriteError::Storage)?;
            bind_text(self.database, audit.raw, 5, &actor_value)
                .map_err(FallbackSlateWriteError::Storage)?;
            bind_i64(self.database, audit.raw, 6, occurred_at_unix_ms)
                .map_err(FallbackSlateWriteError::Storage)?;
            expect_done(self.database, audit.raw).map_err(FallbackSlateWriteError::Storage)?;

            self.read_channel_fallback_slate(channel_id)
                .map_err(FallbackSlateWriteError::Storage)?
                .ok_or_else(|| {
                    FallbackSlateWriteError::Storage(
                        "fallback slate disappeared during its write transaction".into(),
                    )
                })
        })();
        match outcome {
            Ok(selection) => match self.execute("COMMIT") {
                Ok(()) => Ok(selection),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(FallbackSlateWriteError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn list_channel_fallback_slate_audit(
        &self,
        channel_id: &str,
    ) -> Result<Vec<ChannelFallbackSlateAuditEvent>, String> {
        if !is_canonical_uuid(channel_id) {
            return Err("fallback slate channel identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT audit_id, channel_id, image_asset_id, revision, actor, occurred_at_unix_ms \
             FROM channel_fallback_slate_audit WHERE channel_id=?1 \
             ORDER BY revision, audit_id",
        )?;
        let channel = c_string(channel_id, "fallback slate channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel)?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let event = ChannelFallbackSlateAuditEvent {
                        audit_id: column_text(statement.raw, 0)?,
                        channel_id: column_text(statement.raw, 1)?,
                        image_asset_id: column_text(statement.raw, 2)?,
                        revision: column_u64(statement.raw, 3, "fallback slate audit revision")?,
                        actor: column_text(statement.raw, 4)?,
                        occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 5) },
                    };
                    validate_fallback_slate_audit(&event)?;
                    events.push(event);
                }
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_automation_adapter_descriptor(
        &self,
        adapter_id: &str,
    ) -> Result<Option<AutomationAdapterDescriptor>, String> {
        if !is_canonical_uuid(adapter_id) {
            return Err("automation adapter identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT adapter_id, revision, record_json FROM automation_adapter_descriptors WHERE adapter_id=?1",
        )?;
        let identity = c_string(adapter_id, "automation adapter identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let descriptor: AutomationAdapterDescriptor =
                    read_automation_json(column_text(statement.raw, 2)?, "automation adapter")?;
                if descriptor.adapter_id != column_text(statement.raw, 0)?
                    || descriptor.revision
                        != column_u64(statement.raw, 1, "automation adapter revision")?
                {
                    return Err("automation adapter row and record disagree".into());
                }
                Ok(Some(descriptor))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_automation_adapter_descriptors(
        &self,
    ) -> Result<Vec<AutomationAdapterDescriptor>, String> {
        let statement = self.prepare(
            "SELECT adapter_id, revision, record_json FROM automation_adapter_descriptors ORDER BY adapter_id LIMIT 65",
        )?;
        let mut adapters = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let descriptor: AutomationAdapterDescriptor =
                        read_automation_json(column_text(statement.raw, 2)?, "automation adapter")?;
                    if descriptor.adapter_id != column_text(statement.raw, 0)?
                        || descriptor.revision
                            != column_u64(statement.raw, 1, "automation adapter revision")?
                    {
                        return Err("automation adapter row and record disagree".into());
                    }
                    adapters.push(descriptor);
                }
                SQLITE_DONE if adapters.len() <= 64 => return Ok(adapters),
                SQLITE_DONE => return Err("automation adapter authority exceeds 64 records".into()),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_automation_adapter_descriptor(
        &self,
        adapter: &AutomationAdapterDescriptor,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), String> {
        adapter.validate()?;
        if occurred_at_unix_ms < 0 || !secret_field(changed_by, 200) {
            return Err("automation adapter change attribution is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self.read_automation_adapter_descriptor(&adapter.adapter_id)?;
            validate_automation_revision(
                current.as_ref().map(|item| item.revision),
                adapter.revision,
                expected_revision,
            )?;
            let record_json = serde_json::to_string(adapter)
                .map_err(|error| format!("serialize automation adapter: {error}"))?;
            let statement = self.prepare(
                "INSERT INTO automation_adapter_descriptors(adapter_id, revision, record_json) VALUES(?1, ?2, ?3) \
                 ON CONFLICT(adapter_id) DO UPDATE SET revision=excluded.revision, record_json=excluded.record_json",
            )?;
            let adapter_id = c_string(&adapter.adapter_id, "automation adapter identity")?;
            let record_json = c_string(&record_json, "automation adapter record")?;
            bind_text(self.database, statement.raw, 1, &adapter_id)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(adapter.revision, "automation adapter revision")?,
            )?;
            bind_text(self.database, statement.raw, 3, &record_json)?;
            expect_done(self.database, statement.raw)?;
            let history = self.prepare(
                "INSERT INTO automation_adapter_descriptor_history(adapter_id, revision, record_json, changed_by, occurred_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5)",
            )?;
            let changed_by = c_string(changed_by, "automation adapter change actor")?;
            bind_text(self.database, history.raw, 1, &adapter_id)?;
            bind_i64(
                self.database,
                history.raw,
                2,
                checked_i64(adapter.revision, "automation adapter revision")?,
            )?;
            bind_text(self.database, history.raw, 3, &record_json)?;
            bind_text(self.database, history.raw, 4, &changed_by)?;
            bind_i64(self.database, history.raw, 5, occurred_at_unix_ms)?;
            expect_done(self.database, history.raw)
        })();
        self.finish_transaction(outcome)
    }

    pub fn list_automation_adapter_descriptor_history(
        &self,
        adapter_id: &str,
        limit: usize,
    ) -> Result<Vec<AutomationAdapterDescriptorHistory>, String> {
        if !is_canonical_uuid(adapter_id) || !(1..=128).contains(&limit) {
            return Err("automation adapter history query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT adapter_id, revision, record_json, changed_by, occurred_at_unix_ms FROM automation_adapter_descriptor_history WHERE adapter_id=?1 ORDER BY revision LIMIT ?2",
        )?;
        let adapter_id = c_string(adapter_id, "automation adapter identity")?;
        bind_text(self.database, statement.raw, 1, &adapter_id)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut history = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let descriptor: AutomationAdapterDescriptor = read_automation_json(
                        column_text(statement.raw, 2)?,
                        "automation adapter history",
                    )?;
                    if descriptor.adapter_id != column_text(statement.raw, 0)?
                        || descriptor.revision
                            != column_u64(statement.raw, 1, "automation adapter revision")?
                    {
                        return Err("automation adapter history row and record disagree".into());
                    }
                    let changed_by = column_text(statement.raw, 3)?;
                    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 4) };
                    if !secret_field(&changed_by, 200) || occurred_at_unix_ms < 0 {
                        return Err("automation adapter history attribution is invalid".into());
                    }
                    history.push(AutomationAdapterDescriptorHistory {
                        descriptor,
                        changed_by,
                        occurred_at_unix_ms,
                    });
                }
                SQLITE_DONE => return Ok(history),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_automation_feature_policy(
        &self,
        policy_id: &str,
    ) -> Result<Option<AutomationFeaturePolicy>, String> {
        if !is_canonical_uuid(policy_id) {
            return Err("automation policy identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT policy_id, feature, revision, record_json FROM automation_feature_policies WHERE policy_id=?1",
        )?;
        let identity = c_string(policy_id, "automation policy identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let policy: AutomationFeaturePolicy =
                    read_automation_json(column_text(statement.raw, 3)?, "automation policy")?;
                if policy.policy_id != column_text(statement.raw, 0)?
                    || automation_feature_name(policy.feature) != column_text(statement.raw, 1)?
                    || policy.revision
                        != column_u64(statement.raw, 2, "automation policy revision")?
                {
                    return Err("automation policy row and record disagree".into());
                }
                Ok(Some(policy))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_automation_feature_policies(&self) -> Result<Vec<AutomationFeaturePolicy>, String> {
        let statement = self.prepare(
            "SELECT policy_id, feature, revision, record_json FROM automation_feature_policies ORDER BY feature LIMIT 4",
        )?;
        let mut policies = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let policy: AutomationFeaturePolicy =
                        read_automation_json(column_text(statement.raw, 3)?, "automation policy")?;
                    if policy.policy_id != column_text(statement.raw, 0)?
                        || automation_feature_name(policy.feature) != column_text(statement.raw, 1)?
                        || policy.revision
                            != column_u64(statement.raw, 2, "automation policy revision")?
                    {
                        return Err("automation policy row and record disagree".into());
                    }
                    policies.push(policy);
                }
                SQLITE_DONE if policies.len() <= 3 => return Ok(policies),
                SQLITE_DONE => return Err("automation policy authority exceeds 3 records".into()),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_automation_feature_policy(
        &self,
        policy: &AutomationFeaturePolicy,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), String> {
        policy.validate()?;
        if occurred_at_unix_ms < 0 || !secret_field(changed_by, 200) {
            return Err("automation policy change attribution is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self.read_automation_feature_policy(&policy.policy_id)?;
            validate_automation_revision(
                current.as_ref().map(|item| item.revision),
                policy.revision,
                expected_revision,
            )?;
            if current
                .as_ref()
                .is_some_and(|current| current.feature != policy.feature)
            {
                return Err(
                    "automation policy identity cannot be rebound to another feature".into(),
                );
            }
            validate_automation_policy_capabilities(
                policy,
                &self.list_automation_adapter_descriptors()?,
            )?;
            let record_json = serde_json::to_string(policy)
                .map_err(|error| format!("serialize automation policy: {error}"))?;
            let statement = self.prepare(
                "INSERT INTO automation_feature_policies(policy_id, feature, revision, record_json) VALUES(?1, ?2, ?3, ?4) \
                 ON CONFLICT(policy_id) DO UPDATE SET feature=excluded.feature, revision=excluded.revision, record_json=excluded.record_json",
            )?;
            let policy_id = c_string(&policy.policy_id, "automation policy identity")?;
            let feature = c_string(
                automation_feature_name(policy.feature),
                "automation feature",
            )?;
            let record_json = c_string(&record_json, "automation policy record")?;
            bind_text(self.database, statement.raw, 1, &policy_id)?;
            bind_text(self.database, statement.raw, 2, &feature)?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(policy.revision, "automation policy revision")?,
            )?;
            bind_text(self.database, statement.raw, 4, &record_json)?;
            expect_done(self.database, statement.raw)?;
            let history = self.prepare(
                "INSERT INTO automation_feature_policy_history(policy_id, feature, revision, record_json, changed_by, occurred_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5, ?6)",
            )?;
            let changed_by = c_string(changed_by, "automation policy change actor")?;
            bind_text(self.database, history.raw, 1, &policy_id)?;
            bind_text(self.database, history.raw, 2, &feature)?;
            bind_i64(
                self.database,
                history.raw,
                3,
                checked_i64(policy.revision, "automation policy revision")?,
            )?;
            bind_text(self.database, history.raw, 4, &record_json)?;
            bind_text(self.database, history.raw, 5, &changed_by)?;
            bind_i64(self.database, history.raw, 6, occurred_at_unix_ms)?;
            expect_done(self.database, history.raw)
        })();
        self.finish_transaction(outcome)
    }

    pub fn list_automation_feature_policy_history(
        &self,
        policy_id: &str,
        limit: usize,
    ) -> Result<Vec<AutomationFeaturePolicyHistory>, String> {
        if !is_canonical_uuid(policy_id) || !(1..=128).contains(&limit) {
            return Err("automation policy history query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT policy_id, feature, revision, record_json, changed_by, occurred_at_unix_ms FROM automation_feature_policy_history WHERE policy_id=?1 ORDER BY revision LIMIT ?2",
        )?;
        let policy_id = c_string(policy_id, "automation policy identity")?;
        bind_text(self.database, statement.raw, 1, &policy_id)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut history = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let policy: AutomationFeaturePolicy = read_automation_json(
                        column_text(statement.raw, 3)?,
                        "automation policy history",
                    )?;
                    if policy.policy_id != column_text(statement.raw, 0)?
                        || automation_feature_name(policy.feature) != column_text(statement.raw, 1)?
                        || policy.revision
                            != column_u64(statement.raw, 2, "automation policy revision")?
                    {
                        return Err("automation policy history row and record disagree".into());
                    }
                    let changed_by = column_text(statement.raw, 4)?;
                    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 5) };
                    if !secret_field(&changed_by, 200) || occurred_at_unix_ms < 0 {
                        return Err("automation policy history attribution is invalid".into());
                    }
                    history.push(AutomationFeaturePolicyHistory {
                        policy,
                        changed_by,
                        occurred_at_unix_ms,
                    });
                }
                SQLITE_DONE => return Ok(history),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn append_automation_connection_test_evidence(
        &self,
        evidence: &AutomationConnectionTestEvidence,
        current_candidate: &AutomationCandidateIdentity,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        evidence.validate()?;
        current_candidate.validate()?;
        if now_unix_ms < 0 || evidence.candidate != *current_candidate {
            return Err(
                "automation connection evidence candidate binding is stale or invalid".into(),
            );
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let policy = self
                .read_automation_feature_policy(&evidence.policy_id)?
                .ok_or_else(|| {
                    "automation connection evidence policy does not exist".to_string()
                })?;
            let adapters = self.list_automation_adapter_descriptors()?;
            if !matches!(
                derive_feature_automation_status(
                    &policy,
                    &adapters,
                    Some(evidence),
                    current_candidate,
                    now_unix_ms
                ),
                FeatureAutomationStatus::Passed | FeatureAutomationStatus::Failed
            ) {
                return Err("automation connection evidence is not current for policy, adapter, configuration, candidate, or time".into());
            }
            let record_json = serde_json::to_string(evidence)
                .map_err(|error| format!("serialize automation connection evidence: {error}"))?;
            let statement = self.prepare(
                "INSERT INTO automation_connection_test_evidence(evidence_id, policy_id, adapter_id, policy_revision, adapter_revision, configuration_hash, candidate_version, source_commit, tested_at_unix_ms, expires_at_unix_ms, record_json) \
                 VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            )?;
            let values = [
                c_string(&evidence.evidence_id, "automation evidence identity")?,
                c_string(&evidence.policy_id, "automation policy identity")?,
                c_string(&evidence.adapter_id, "automation adapter identity")?,
                c_string(
                    &evidence.configuration_hash,
                    "automation configuration hash",
                )?,
                c_string(&evidence.candidate.version, "automation candidate version")?,
                c_string(
                    &evidence.candidate.source_commit,
                    "automation source commit",
                )?,
                c_string(&record_json, "automation evidence record")?,
            ];
            bind_text(self.database, statement.raw, 1, &values[0])?;
            bind_text(self.database, statement.raw, 2, &values[1])?;
            bind_text(self.database, statement.raw, 3, &values[2])?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                checked_i64(evidence.policy_revision, "automation policy revision")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                checked_i64(evidence.adapter_revision, "automation adapter revision")?,
            )?;
            bind_text(self.database, statement.raw, 6, &values[3])?;
            bind_text(self.database, statement.raw, 7, &values[4])?;
            bind_text(self.database, statement.raw, 8, &values[5])?;
            bind_i64(self.database, statement.raw, 9, evidence.tested_at_unix_ms)?;
            bind_i64(
                self.database,
                statement.raw,
                10,
                evidence.expires_at_unix_ms,
            )?;
            bind_text(self.database, statement.raw, 11, &values[6])?;
            expect_done(self.database, statement.raw)
        })();
        self.finish_transaction(outcome)
    }

    pub fn list_automation_connection_test_evidence(
        &self,
        policy_id: &str,
        limit: usize,
    ) -> Result<Vec<AutomationConnectionTestEvidence>, String> {
        if !is_canonical_uuid(policy_id) || !(1..=128).contains(&limit) {
            return Err("automation connection evidence query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT evidence_id, policy_id, adapter_id, policy_revision, adapter_revision, configuration_hash, candidate_version, source_commit, tested_at_unix_ms, expires_at_unix_ms, record_json FROM automation_connection_test_evidence WHERE policy_id=?1 \
             ORDER BY tested_at_unix_ms DESC, evidence_id DESC LIMIT ?2",
        )?;
        let policy_id = c_string(policy_id, "automation policy identity")?;
        bind_text(self.database, statement.raw, 1, &policy_id)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut evidence = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let record: AutomationConnectionTestEvidence = read_automation_json(
                        column_text(statement.raw, 10)?,
                        "automation connection evidence",
                    )?;
                    if record.evidence_id != column_text(statement.raw, 0)?
                        || record.policy_id != column_text(statement.raw, 1)?
                        || record.adapter_id != column_text(statement.raw, 2)?
                        || record.policy_revision
                            != column_u64(statement.raw, 3, "automation evidence policy revision")?
                        || record.adapter_revision
                            != column_u64(statement.raw, 4, "automation evidence adapter revision")?
                        || record.configuration_hash != column_text(statement.raw, 5)?
                        || record.candidate.version != column_text(statement.raw, 6)?
                        || record.candidate.source_commit != column_text(statement.raw, 7)?
                        || record.tested_at_unix_ms
                            != unsafe { sqlite3_column_int64(statement.raw, 8) }
                        || record.expires_at_unix_ms
                            != unsafe { sqlite3_column_int64(statement.raw, 9) }
                    {
                        return Err("automation connection evidence row and record disagree".into());
                    }
                    evidence.push(record);
                }
                SQLITE_DONE => return Ok(evidence),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_custom_metadata_schema(
        &self,
        schema_id: &str,
    ) -> Result<Option<MetadataSchema>, String> {
        if !is_canonical_uuid(schema_id) {
            return Err("custom metadata schema identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT schema_id, revision, record_json FROM custom_metadata_schemas WHERE schema_id=?1",
        )?;
        let schema_id = c_string(schema_id, "custom metadata schema identity")?;
        bind_text(self.database, statement.raw, 1, &schema_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_custom_metadata_schema_row(
                statement.raw,
                0,
                1,
                2,
            )?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_custom_metadata_schemas(&self) -> Result<Vec<MetadataSchema>, String> {
        let statement = self.prepare(
            "SELECT schema_id, revision, record_json FROM custom_metadata_schemas ORDER BY schema_id LIMIT 65",
        )?;
        let mut schemas = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    schemas.push(read_custom_metadata_schema_row(statement.raw, 0, 1, 2)?)
                }
                SQLITE_DONE if schemas.len() <= MAX_CUSTOM_METADATA_SCHEMAS => return Ok(schemas),
                SQLITE_DONE => return Err("custom metadata authority exceeds 64 schemas".into()),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_custom_metadata_schema(
        &self,
        schema: &MetadataSchema,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), CustomMetadataSchemaWriteError> {
        schema
            .validate()
            .map_err(CustomMetadataSchemaWriteError::Invalid)?;
        if occurred_at_unix_ms < 0 || !secret_field(changed_by, 200) {
            return Err(CustomMetadataSchemaWriteError::Invalid(
                "custom metadata schema change attribution is invalid".into(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CustomMetadataSchemaWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_custom_metadata_schema(&schema.schema_id)
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            if current.is_none()
                && self
                    .list_custom_metadata_schemas()
                    .map_err(CustomMetadataSchemaWriteError::Storage)?
                    .len()
                    >= MAX_CUSTOM_METADATA_SCHEMAS
            {
                return Err(CustomMetadataSchemaWriteError::Capacity);
            }
            validate_schema_write(current.as_ref(), expected_revision, schema).map_err(
                |message| {
                    if message.contains("creation requires")
                        || message.contains("compare-and-set revision")
                    {
                        CustomMetadataSchemaWriteError::Conflict(message)
                    } else if message.contains("rebound") {
                        CustomMetadataSchemaWriteError::Rebound
                    } else {
                        CustomMetadataSchemaWriteError::Invalid(message)
                    }
                },
            )?;
            let record_json = serde_json::to_string(schema).map_err(|error| {
                CustomMetadataSchemaWriteError::Storage(format!(
                    "serialize custom metadata schema: {error}"
                ))
            })?;
            let schema_id = c_string(&schema.schema_id, "custom metadata schema identity")
                .map_err(CustomMetadataSchemaWriteError::Invalid)?;
            let record_json = c_string(&record_json, "custom metadata schema record")
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            let statement = self.prepare(
                "INSERT INTO custom_metadata_schemas(schema_id, revision, record_json) VALUES(?1, ?2, ?3) \
                 ON CONFLICT(schema_id) DO UPDATE SET revision=excluded.revision, record_json=excluded.record_json",
            ).map_err(CustomMetadataSchemaWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &schema_id)
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(schema.revision, "custom metadata schema revision")
                    .map_err(CustomMetadataSchemaWriteError::Invalid)?,
            )
            .map_err(CustomMetadataSchemaWriteError::Storage)?;
            bind_text(self.database, statement.raw, 3, &record_json)
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            expect_done(self.database, statement.raw)
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            let history = self.prepare(
                "INSERT INTO custom_metadata_schema_history(schema_id, revision, record_json, changed_by, occurred_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5)",
            ).map_err(CustomMetadataSchemaWriteError::Storage)?;
            let changed_by = c_string(changed_by, "custom metadata schema change actor")
                .map_err(CustomMetadataSchemaWriteError::Invalid)?;
            bind_text(self.database, history.raw, 1, &schema_id)
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            bind_i64(
                self.database,
                history.raw,
                2,
                checked_i64(schema.revision, "custom metadata schema revision")
                    .map_err(CustomMetadataSchemaWriteError::Invalid)?,
            )
            .map_err(CustomMetadataSchemaWriteError::Storage)?;
            bind_text(self.database, history.raw, 3, &record_json)
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            bind_text(self.database, history.raw, 4, &changed_by)
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            bind_i64(self.database, history.raw, 5, occurred_at_unix_ms)
                .map_err(CustomMetadataSchemaWriteError::Storage)?;
            expect_done(self.database, history.raw).map_err(CustomMetadataSchemaWriteError::Storage)
        })();
        self.finish_custom_metadata_schema_transaction(outcome)
    }

    pub fn list_custom_metadata_schema_history(
        &self,
        schema_id: &str,
        limit: usize,
    ) -> Result<Vec<CustomMetadataSchemaHistory>, String> {
        if !is_canonical_uuid(schema_id) || !(1..=MAX_CUSTOM_METADATA_HISTORY).contains(&limit) {
            return Err("custom metadata schema history query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT schema_id, revision, record_json, changed_by, occurred_at_unix_ms FROM custom_metadata_schema_history WHERE schema_id=?1 ORDER BY revision LIMIT ?2",
        )?;
        let schema_id = c_string(schema_id, "custom metadata schema identity")?;
        bind_text(self.database, statement.raw, 1, &schema_id)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut history = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let schema = read_custom_metadata_schema_row(statement.raw, 0, 1, 2)?;
                    let changed_by = column_text(statement.raw, 3)?;
                    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 4) };
                    if !secret_field(&changed_by, 200) || occurred_at_unix_ms < 0 {
                        return Err("custom metadata schema history attribution is invalid".into());
                    }
                    history.push(CustomMetadataSchemaHistory {
                        schema,
                        changed_by,
                        occurred_at_unix_ms,
                    });
                }
                SQLITE_DONE => return Ok(history),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_asset_metadata_record(
        &self,
        metadata_id: &str,
    ) -> Result<Option<AssetMetadataRecord>, String> {
        if !is_canonical_uuid(metadata_id) {
            return Err("asset metadata identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT metadata_id, asset_id, schema_id, schema_revision, revision, record_json FROM asset_metadata_records WHERE metadata_id=?1",
        )?;
        let metadata_id = c_string(metadata_id, "asset metadata identity")?;
        bind_text(self.database, statement.raw, 1, &metadata_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let record = read_asset_metadata_record_row(statement.raw, 0, 1, 2, 3, 4, 5)?;
                self.validate_stored_asset_metadata_record(&record)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_asset_metadata_records(
        &self,
        asset_id: &str,
    ) -> Result<Vec<AssetMetadataRecord>, String> {
        if !is_sha256_identity(asset_id) {
            return Err("asset metadata asset identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT metadata_id, asset_id, schema_id, schema_revision, revision, record_json FROM asset_metadata_records WHERE asset_id=?1 ORDER BY metadata_id LIMIT 129",
        )?;
        let asset_id = c_string(asset_id, "asset metadata asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        let mut records = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let record = read_asset_metadata_record_row(statement.raw, 0, 1, 2, 3, 4, 5)?;
                    self.validate_stored_asset_metadata_record(&record)?;
                    records.push(record);
                }
                SQLITE_DONE if records.len() <= MAX_CUSTOM_METADATA_RECORDS_PER_ASSET => {
                    return Ok(records);
                }
                SQLITE_DONE => {
                    return Err("asset metadata authority exceeds 128 records for one asset".into());
                }
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_asset_metadata_record(
        &self,
        record: &AssetMetadataRecord,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), AssetMetadataWriteError> {
        record
            .validate_shape()
            .map_err(AssetMetadataWriteError::Invalid)?;
        if occurred_at_unix_ms < 0 || !secret_field(changed_by, 200) {
            return Err(AssetMetadataWriteError::Invalid(
                "asset metadata change attribution is invalid".into(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(AssetMetadataWriteError::Storage)?;
        let outcome = (|| {
            // This read deliberately occurs after BEGIN IMMEDIATE. The API may
            // have resolved a schema before entering storage; only this exact
            // in-transaction revision is authoritative for the record CAS.
            let schema = self
                .read_custom_metadata_schema(&record.schema_id)
                .map_err(AssetMetadataWriteError::Storage)?
                .ok_or(AssetMetadataWriteError::SchemaNotFound)?;
            if schema.revision != record.schema_revision {
                return Err(AssetMetadataWriteError::StaleSchema {
                    requested_revision: record.schema_revision,
                    current_revision: schema.revision,
                });
            }
            let current = self
                .read_asset_metadata_record(&record.metadata_id)
                .map_err(AssetMetadataWriteError::Storage)?;
            if current.as_ref().is_some_and(|current| {
                current.asset_id != record.asset_id || current.schema_id != record.schema_id
            }) {
                return Err(AssetMetadataWriteError::Rebound);
            }
            let asset_records = self
                .list_asset_metadata_records(&record.asset_id)
                .map_err(AssetMetadataWriteError::Storage)?;
            if asset_records.iter().any(|existing| {
                existing.schema_id == record.schema_id && existing.metadata_id != record.metadata_id
            }) {
                return Err(AssetMetadataWriteError::DuplicateBinding);
            }
            if current.is_none() && asset_records.len() >= MAX_CUSTOM_METADATA_RECORDS_PER_ASSET {
                return Err(AssetMetadataWriteError::Conflict(
                    "asset metadata authority cannot exceed 128 records for one asset".into(),
                ));
            }
            validate_asset_metadata_write(current.as_ref(), expected_revision, record, &schema)
                .map_err(|message| {
                    if message.contains("compare-and-set revision") {
                        AssetMetadataWriteError::Conflict(message)
                    } else if message.contains("rebound") {
                        AssetMetadataWriteError::Rebound
                    } else {
                        AssetMetadataWriteError::Invalid(message)
                    }
                })?;
            let record_json = serde_json::to_string(record).map_err(|error| {
                AssetMetadataWriteError::Storage(format!(
                    "serialize asset metadata record: {error}"
                ))
            })?;
            let metadata_id = c_string(&record.metadata_id, "asset metadata identity")
                .map_err(AssetMetadataWriteError::Invalid)?;
            let asset_id = c_string(&record.asset_id, "asset metadata asset identity")
                .map_err(AssetMetadataWriteError::Invalid)?;
            let schema_id = c_string(&record.schema_id, "custom metadata schema identity")
                .map_err(AssetMetadataWriteError::Invalid)?;
            let record_json = c_string(&record_json, "asset metadata record")
                .map_err(AssetMetadataWriteError::Storage)?;
            let statement = self.prepare(
                "INSERT INTO asset_metadata_records(metadata_id, asset_id, schema_id, schema_revision, revision, record_json) VALUES(?1, ?2, ?3, ?4, ?5, ?6) \
                 ON CONFLICT(metadata_id) DO UPDATE SET asset_id=excluded.asset_id, schema_id=excluded.schema_id, schema_revision=excluded.schema_revision, revision=excluded.revision, record_json=excluded.record_json",
            ).map_err(AssetMetadataWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &metadata_id)
                .map_err(AssetMetadataWriteError::Storage)?;
            bind_text(self.database, statement.raw, 2, &asset_id)
                .map_err(AssetMetadataWriteError::Storage)?;
            bind_text(self.database, statement.raw, 3, &schema_id)
                .map_err(AssetMetadataWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                checked_i64(record.schema_revision, "asset metadata schema revision")
                    .map_err(AssetMetadataWriteError::Invalid)?,
            )
            .map_err(AssetMetadataWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                checked_i64(record.revision, "asset metadata revision")
                    .map_err(AssetMetadataWriteError::Invalid)?,
            )
            .map_err(AssetMetadataWriteError::Storage)?;
            bind_text(self.database, statement.raw, 6, &record_json)
                .map_err(AssetMetadataWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(AssetMetadataWriteError::Storage)?;
            let history = self.prepare(
                "INSERT INTO asset_metadata_record_history(metadata_id, asset_id, schema_id, schema_revision, revision, record_json, changed_by, occurred_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            ).map_err(AssetMetadataWriteError::Storage)?;
            let changed_by = c_string(changed_by, "asset metadata change actor")
                .map_err(AssetMetadataWriteError::Invalid)?;
            bind_text(self.database, history.raw, 1, &metadata_id)
                .map_err(AssetMetadataWriteError::Storage)?;
            bind_text(self.database, history.raw, 2, &asset_id)
                .map_err(AssetMetadataWriteError::Storage)?;
            bind_text(self.database, history.raw, 3, &schema_id)
                .map_err(AssetMetadataWriteError::Storage)?;
            bind_i64(
                self.database,
                history.raw,
                4,
                checked_i64(record.schema_revision, "asset metadata schema revision")
                    .map_err(AssetMetadataWriteError::Invalid)?,
            )
            .map_err(AssetMetadataWriteError::Storage)?;
            bind_i64(
                self.database,
                history.raw,
                5,
                checked_i64(record.revision, "asset metadata revision")
                    .map_err(AssetMetadataWriteError::Invalid)?,
            )
            .map_err(AssetMetadataWriteError::Storage)?;
            bind_text(self.database, history.raw, 6, &record_json)
                .map_err(AssetMetadataWriteError::Storage)?;
            bind_text(self.database, history.raw, 7, &changed_by)
                .map_err(AssetMetadataWriteError::Storage)?;
            bind_i64(self.database, history.raw, 8, occurred_at_unix_ms)
                .map_err(AssetMetadataWriteError::Storage)?;
            expect_done(self.database, history.raw).map_err(AssetMetadataWriteError::Storage)
        })();
        self.finish_asset_metadata_transaction(outcome)
    }

    pub fn list_asset_metadata_record_history(
        &self,
        metadata_id: &str,
        limit: usize,
    ) -> Result<Vec<AssetMetadataRecordHistory>, String> {
        if !is_canonical_uuid(metadata_id) || !(1..=MAX_CUSTOM_METADATA_HISTORY).contains(&limit) {
            return Err("asset metadata history query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT metadata_id, asset_id, schema_id, schema_revision, revision, record_json, changed_by, occurred_at_unix_ms FROM asset_metadata_record_history WHERE metadata_id=?1 ORDER BY revision LIMIT ?2",
        )?;
        let metadata_id = c_string(metadata_id, "asset metadata identity")?;
        bind_text(self.database, statement.raw, 1, &metadata_id)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut history = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let record = read_asset_metadata_record_row(statement.raw, 0, 1, 2, 3, 4, 5)?;
                    self.validate_stored_asset_metadata_record(&record)?;
                    let changed_by = column_text(statement.raw, 6)?;
                    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 7) };
                    if !secret_field(&changed_by, 200) || occurred_at_unix_ms < 0 {
                        return Err("asset metadata history attribution is invalid".into());
                    }
                    history.push(AssetMetadataRecordHistory {
                        record,
                        changed_by,
                        occurred_at_unix_ms,
                    });
                }
                SQLITE_DONE => return Ok(history),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn read_custom_metadata_schema_revision(
        &self,
        schema_id: &str,
        revision: u64,
    ) -> Result<Option<MetadataSchema>, String> {
        if !is_canonical_uuid(schema_id) || revision == 0 {
            return Err("custom metadata schema revision identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT schema_id, revision, record_json FROM custom_metadata_schema_history WHERE schema_id=?1 AND revision=?2",
        )?;
        let schema_id = c_string(schema_id, "custom metadata schema identity")?;
        bind_text(self.database, statement.raw, 1, &schema_id)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(revision, "custom metadata schema revision")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_custom_metadata_schema_row(
                statement.raw,
                0,
                1,
                2,
            )?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn validate_stored_asset_metadata_record(
        &self,
        record: &AssetMetadataRecord,
    ) -> Result<(), String> {
        let schema = self
            .read_custom_metadata_schema_revision(&record.schema_id, record.schema_revision)?
            .ok_or_else(|| {
                "asset metadata record references an unavailable schema revision".to_string()
            })?;
        validate_asset_metadata_record(record, &schema)
            .map_err(|error| format!("database returned invalid asset metadata binding: {error}"))
    }

    pub fn create_scheduled_recording(&self, recording: &ScheduledRecording) -> Result<(), String> {
        recording.validate()?;
        if recording.revision != 1 || recording.lifecycle != RecordingLifecycle::Draft {
            return Err(
                "scheduled recording creation requires revision-one draft authority".into(),
            );
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.recheck_scheduled_recording_schedule_binding(recording)?;
            if self
                .read_scheduled_recording(&recording.definition.recording_id)?
                .is_some()
            {
                return Err("scheduled recording identity already exists".into());
            }
            self.insert_scheduled_recording(recording)?;
            self.append_scheduled_recording_history(
                recording,
                None,
                &recording.definition.requested_by,
                recording.definition.requested_at_unix_ms,
                "created",
            )
        })();
        self.finish_transaction(outcome)
    }

    fn recheck_scheduled_recording_schedule_binding(
        &self,
        recording: &ScheduledRecording,
    ) -> Result<(), String> {
        let item = self
            .read_schedule_item(&recording.definition.schedule_item_id)?
            .ok_or_else(|| "scheduled recording schedule item does not exist".to_string())?;
        if item.state != ScheduleState::Committed {
            return Err("scheduled recording schedule item is not committed".into());
        }
        let scheduled_end_unix_ms = item.ends_at_unix_ms().map_err(|error| {
            format!("scheduled recording schedule binding is invalid: {error:?}")
        })?;
        if item.revision != recording.definition.schedule_revision
            || item.channel_id != recording.definition.channel_id
            || item.starts_at_unix_ms != recording.definition.scheduled_start_unix_ms
            || scheduled_end_unix_ms != recording.definition.scheduled_end_unix_ms
        {
            return Err("scheduled recording schedule binding is stale or changed".into());
        }
        Ok(())
    }

    pub fn write_scheduled_recording_transition(
        &self,
        recording: &ScheduledRecording,
        receipt: &RecordingTransitionReceipt,
    ) -> Result<(), String> {
        recording.validate()?;
        validate_scheduled_recording_transition_receipt(recording, receipt)?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self
                .read_scheduled_recording(&recording.definition.recording_id)?
                .ok_or_else(|| "scheduled recording does not exist".to_string())?;
            if current.definition != recording.definition
                || receipt.prior_revision != current.revision
                || receipt.resulting_revision != recording.revision
                || recording.revision != current.revision.saturating_add(1)
            {
                return Err(
                    "scheduled recording transition revision or definition authority conflicts"
                        .into(),
                );
            }
            if receipt.kind != RecordingTransitionKind::Cancel {
                self.recheck_scheduled_recording_schedule_binding(recording)?;
            }
            let record_json = serde_json::to_string(recording)
                .map_err(|error| format!("serialize scheduled recording: {error}"))?;
            let indexed_execution = scheduled_recording_execution(recording);
            let statement = self.prepare(
                "UPDATE scheduled_recordings SET revision=?1, lifecycle=?2, candidate_version=?3, \
                 source_commit=?4, channel_configuration_hash=?5, record_json=?6 \
                 WHERE recording_id=?7 AND revision=?8",
            )?;
            let values = [
                c_string(
                    scheduled_recording_lifecycle_name(&recording.lifecycle),
                    "recording lifecycle",
                )?,
                c_string(
                    indexed_execution.map_or("", |value| value.candidate.version.as_str()),
                    "recording candidate version",
                )?,
                c_string(
                    indexed_execution.map_or("", |value| value.candidate.source_commit.as_str()),
                    "recording source commit",
                )?,
                c_string(
                    indexed_execution.map_or("", |value| value.channel_configuration_hash.as_str()),
                    "recording configuration hash",
                )?,
                c_string(&record_json, "scheduled recording record")?,
                c_string(
                    &recording.definition.recording_id,
                    "scheduled recording identity",
                )?,
            ];
            bind_i64(
                self.database,
                statement.raw,
                1,
                checked_i64(recording.revision, "scheduled recording revision")?,
            )?;
            bind_text(self.database, statement.raw, 2, &values[0])?;
            bind_text(self.database, statement.raw, 3, &values[1])?;
            bind_text(self.database, statement.raw, 4, &values[2])?;
            bind_text(self.database, statement.raw, 5, &values[3])?;
            bind_text(self.database, statement.raw, 6, &values[4])?;
            bind_text(self.database, statement.raw, 7, &values[5])?;
            bind_i64(
                self.database,
                statement.raw,
                8,
                checked_i64(current.revision, "current scheduled recording revision")?,
            )?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err(
                    "scheduled recording transition lost its optimistic revision race".into(),
                );
            }
            self.append_scheduled_recording_history(
                recording,
                Some(receipt),
                &receipt.actor,
                receipt.occurred_at_unix_ms,
                scheduled_recording_transition_kind_name(receipt.kind),
            )
        })();
        self.finish_transaction(outcome)
    }

    pub fn read_scheduled_recording(
        &self,
        recording_id: &str,
    ) -> Result<Option<ScheduledRecording>, String> {
        if !is_canonical_uuid(recording_id) {
            return Err("scheduled recording identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT recording_id, channel_id, schedule_item_id, schedule_revision, scheduled_start_unix_ms, \
             scheduled_end_unix_ms, revision, lifecycle, candidate_version, source_commit, \
             channel_configuration_hash, record_json FROM scheduled_recordings WHERE recording_id=?1",
        )?;
        let recording_id = c_string(recording_id, "scheduled recording identity")?;
        bind_text(self.database, statement.raw, 1, &recording_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_scheduled_recording_row(statement.raw, 0)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_scheduled_recordings_for_channel(
        &self,
        channel_id: &str,
        limit: usize,
    ) -> Result<Vec<ScheduledRecording>, String> {
        if !is_canonical_uuid(channel_id) || !(1..=128).contains(&limit) {
            return Err("scheduled recording channel query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT recording_id, channel_id, schedule_item_id, schedule_revision, scheduled_start_unix_ms, \
             scheduled_end_unix_ms, revision, lifecycle, candidate_version, source_commit, \
             channel_configuration_hash, record_json FROM scheduled_recordings WHERE channel_id=?1 \
             ORDER BY scheduled_start_unix_ms, recording_id LIMIT ?2",
        )?;
        let channel_id = c_string(channel_id, "scheduled recording channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut recordings = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => recordings.push(read_scheduled_recording_row(statement.raw, 0)?),
                SQLITE_DONE => return Ok(recordings),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_scheduled_recording_history(
        &self,
        recording_id: &str,
        limit: usize,
    ) -> Result<Vec<ScheduledRecordingHistoryEntry>, String> {
        if !is_canonical_uuid(recording_id) || !(1..=128).contains(&limit) {
            return Err("scheduled recording history query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT recording_id, revision, transition_kind, actor_id, actor_role, occurred_at_unix_ms, \
             candidate_version, source_commit, channel_configuration_hash, receipt_json, record_json \
             FROM scheduled_recording_history WHERE recording_id=?1 ORDER BY revision LIMIT ?2",
        )?;
        let recording_id = c_string(recording_id, "scheduled recording identity")?;
        bind_text(self.database, statement.raw, 1, &recording_id)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut history = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => history.push(read_scheduled_recording_history_row(statement.raw)?),
                SQLITE_DONE => return Ok(history),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn insert_scheduled_recording(&self, recording: &ScheduledRecording) -> Result<(), String> {
        let record_json = serde_json::to_string(recording)
            .map_err(|error| format!("serialize scheduled recording: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO scheduled_recordings(recording_id, channel_id, schedule_item_id, schedule_revision, \
             scheduled_start_unix_ms, scheduled_end_unix_ms, revision, lifecycle, candidate_version, \
             source_commit, channel_configuration_hash, record_json) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, '', '', '', ?9)",
        )?;
        let values = [
            c_string(
                &recording.definition.recording_id,
                "scheduled recording identity",
            )?,
            c_string(
                &recording.definition.channel_id,
                "scheduled recording channel identity",
            )?,
            c_string(
                &recording.definition.schedule_item_id,
                "scheduled recording schedule item identity",
            )?,
            c_string(&record_json, "scheduled recording record")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            checked_i64(
                recording.definition.schedule_revision,
                "scheduled recording schedule revision",
            )?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            recording.definition.scheduled_start_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            recording.definition.scheduled_end_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            checked_i64(recording.revision, "scheduled recording revision")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            8,
            &c_string("draft", "scheduled recording lifecycle")?,
        )?;
        bind_text(self.database, statement.raw, 9, &values[3])?;
        expect_done(self.database, statement.raw)
    }

    fn append_scheduled_recording_history(
        &self,
        recording: &ScheduledRecording,
        receipt: Option<&RecordingTransitionReceipt>,
        actor: &RecordingActor,
        occurred_at_unix_ms: i64,
        transition_kind: &str,
    ) -> Result<(), String> {
        actor.validate()?;
        if occurred_at_unix_ms < 0 {
            return Err("scheduled recording history time is invalid".into());
        }
        let record_json = serde_json::to_string(recording)
            .map_err(|error| format!("serialize scheduled recording history: {error}"))?;
        let receipt_json = receipt
            .map(serde_json::to_string)
            .transpose()
            .map_err(|error| format!("serialize scheduled recording receipt: {error}"))?
            .unwrap_or_default();
        let execution = scheduled_recording_execution(recording);
        let statement = self.prepare(
            "INSERT INTO scheduled_recording_history(recording_id, revision, transition_kind, actor_id, actor_role, \
             occurred_at_unix_ms, candidate_version, source_commit, channel_configuration_hash, receipt_json, record_json) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        )?;
        let values = [
            c_string(
                &recording.definition.recording_id,
                "scheduled recording identity",
            )?,
            c_string(transition_kind, "scheduled recording transition kind")?,
            c_string(&actor.actor_id, "scheduled recording actor identity")?,
            c_string(
                scheduled_recording_actor_role_name(actor.role),
                "scheduled recording actor role",
            )?,
            c_string(
                execution.map_or("", |value| value.candidate.version.as_str()),
                "recording candidate version",
            )?,
            c_string(
                execution.map_or("", |value| value.candidate.source_commit.as_str()),
                "recording source commit",
            )?,
            c_string(
                execution.map_or("", |value| value.channel_configuration_hash.as_str()),
                "recording configuration hash",
            )?,
            c_string(&receipt_json, "scheduled recording receipt")?,
            c_string(&record_json, "scheduled recording history record")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(recording.revision, "scheduled recording revision")?,
        )?;
        bind_text(self.database, statement.raw, 3, &values[1])?;
        bind_text(self.database, statement.raw, 4, &values[2])?;
        bind_text(self.database, statement.raw, 5, &values[3])?;
        bind_i64(self.database, statement.raw, 6, occurred_at_unix_ms)?;
        bind_text(self.database, statement.raw, 7, &values[4])?;
        bind_text(self.database, statement.raw, 8, &values[5])?;
        bind_text(self.database, statement.raw, 9, &values[6])?;
        bind_text(self.database, statement.raw, 10, &values[7])?;
        bind_text(self.database, statement.raw, 11, &values[8])?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_channel_caption_policy(
        &self,
        channel_id: &str,
    ) -> Result<Option<ChannelCaptionPolicy>, String> {
        if !is_canonical_uuid(channel_id) {
            return Err("caption policy channel identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT channel_id, mode, service_number, language, revision, \
                    created_at_unix_ms, updated_at_unix_ms, updated_by \
             FROM channel_caption_policies WHERE channel_id=?1",
        )?;
        let channel = c_string(channel_id, "caption policy channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_caption_policy_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn read_asset_caption_track(
        &self,
        track_id: &str,
    ) -> Result<Option<AssetCaptionTrack>, String> {
        if !is_canonical_uuid(track_id) {
            return Err("caption track identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT t.track_id, t.asset_id, t.language, t.service_number, t.source_kind, \
                    t.label, t.revision, t.cue_count, t.cues_sha256, t.created_at_unix_ms, \
                    t.updated_at_unix_ms, t.updated_by, a.duration_ms \
             FROM asset_caption_tracks t JOIN media_assets a ON a.asset_id=t.asset_id \
             WHERE t.track_id=?1",
        )?;
        let identity = c_string(track_id, "caption track identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let cue_count =
                    usize::try_from(column_u64(statement.raw, 7, "caption track cue count")?)
                        .map_err(|_| {
                            "database returned an invalid caption cue count".to_string()
                        })?;
                let asset_duration_ms =
                    column_u64(statement.raw, 12, "caption track asset duration")?;
                let mut track = AssetCaptionTrack {
                    track_id: column_text(statement.raw, 0)?,
                    asset_id: column_text(statement.raw, 1)?,
                    language: column_text(statement.raw, 2)?,
                    service_number: u8::try_from(column_u64(
                        statement.raw,
                        3,
                        "caption track service number",
                    )?)
                    .map_err(|_| {
                        "database returned an invalid caption service number".to_string()
                    })?,
                    source_kind: CaptionTrackSourceKind::from_storage(&column_text(
                        statement.raw,
                        4,
                    )?)?,
                    label: column_text(statement.raw, 5)?,
                    cues: Vec::with_capacity(cue_count),
                    revision: column_u64(statement.raw, 6, "caption track revision")?,
                    cues_sha256: column_text(statement.raw, 8)?,
                    created_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 9) },
                    updated_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 10) },
                    updated_by: column_text(statement.raw, 11)?,
                };
                let cues = self.prepare(
                    "SELECT cue_ordinal, cue_id, start_ms, duration_ms, cue_text \
                     FROM asset_caption_cues WHERE track_id=?1 ORDER BY cue_ordinal",
                )?;
                bind_text(self.database, cues.raw, 1, &identity)?;
                loop {
                    match unsafe { sqlite3_step(cues.raw) } {
                        SQLITE_ROW => {
                            let ordinal =
                                usize::try_from(column_u64(cues.raw, 0, "caption cue ordinal")?)
                                    .map_err(|_| {
                                        "database returned an invalid caption cue ordinal"
                                            .to_string()
                                    })?;
                            if ordinal != track.cues.len() {
                                return Err(
                                    "database returned a noncontiguous caption cue sequence".into(),
                                );
                            }
                            track.cues.push(AssetCaptionCue {
                                cue_id: column_text(cues.raw, 1)?,
                                start_ms: column_u64(cues.raw, 2, "caption cue start")?,
                                duration_ms: column_u64(cues.raw, 3, "caption cue duration")?,
                                text: column_text(cues.raw, 4)?,
                            });
                        }
                        SQLITE_DONE => break,
                        _ => return Err(sqlite_error(self.database)),
                    }
                }
                if track.cues.len() != cue_count {
                    return Err("database caption cue count does not match the track".into());
                }
                track.validate(asset_duration_ms)?;
                Ok(Some(track))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_asset_caption_tracks(
        &self,
        asset_id: &str,
    ) -> Result<Vec<AssetCaptionTrack>, String> {
        if !is_sha256_identity(asset_id) {
            return Err("caption asset identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT track_id FROM asset_caption_tracks WHERE asset_id=?1 \
             ORDER BY service_number, language, track_id",
        )?;
        let identity = c_string(asset_id, "caption asset identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        let mut track_ids = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => track_ids.push(column_text(statement.raw, 0)?),
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        drop(statement);
        track_ids
            .into_iter()
            .map(|track_id| {
                self.read_asset_caption_track(&track_id)?
                    .ok_or_else(|| "caption track disappeared while listing asset authority".into())
            })
            .collect()
    }

    pub fn write_asset_caption_track(
        &self,
        write: &AssetCaptionTrackWrite,
    ) -> Result<AssetCaptionTrack, CaptionTrackWriteError> {
        write
            .validate(u64::MAX)
            .map_err(CaptionTrackWriteError::Invalid)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(CaptionTrackWriteError::Storage)?;
        let outcome = (|| {
            let asset = self
                .read_media_asset(&write.asset_id)
                .map_err(CaptionTrackWriteError::Storage)?
                .ok_or(CaptionTrackWriteError::AssetNotFound)?;
            if asset.readiness != AssetReadiness::Ready {
                return Err(CaptionTrackWriteError::AssetNotReady);
            }
            write
                .validate(asset.duration_ms)
                .map_err(CaptionTrackWriteError::Invalid)?;

            let on_air = self
                .prepare(
                    "SELECT 1 FROM channel_runtime \
                     WHERE on_air_asset_id=?1 AND state IN ('on_air','live') LIMIT 1",
                )
                .map_err(CaptionTrackWriteError::Storage)?;
            let asset_identity = c_string(&write.asset_id, "caption asset identity")
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, on_air.raw, 1, &asset_identity)
                .map_err(CaptionTrackWriteError::Storage)?;
            match unsafe { sqlite3_step(on_air.raw) } {
                SQLITE_ROW => return Err(CaptionTrackWriteError::AssetOnAir),
                SQLITE_DONE => {}
                _ => return Err(CaptionTrackWriteError::Storage(sqlite_error(self.database))),
            }

            let current = self
                .read_asset_caption_track(&write.track_id)
                .map_err(CaptionTrackWriteError::Storage)?;
            if let Some(current) = &current {
                if current.asset_id != write.asset_id
                    || current.language != write.language
                    || current.service_number != write.service_number
                {
                    return Err(CaptionTrackWriteError::Invalid(
                        "an existing caption track identity cannot be rebound to another asset, language, or service"
                            .into(),
                    ));
                }
                if current.revision != write.expected_revision {
                    return Err(CaptionTrackWriteError::Conflict(current.revision));
                }
                if write.occurred_at_unix_ms < current.updated_at_unix_ms {
                    return Err(CaptionTrackWriteError::Invalid(
                        "caption track write time precedes current authority".into(),
                    ));
                }
            } else if write.expected_revision != 0 {
                return Err(CaptionTrackWriteError::NotFound);
            }

            let language = c_string(&write.language, "caption language")
                .map_err(CaptionTrackWriteError::Storage)?;
            let slot = self
                .prepare(
                    "SELECT track_id, revision FROM asset_caption_tracks \
                     WHERE asset_id=?1 AND language=?2 AND service_number=?3",
                )
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, slot.raw, 1, &asset_identity)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, slot.raw, 2, &language)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(self.database, slot.raw, 3, i64::from(write.service_number))
                .map_err(CaptionTrackWriteError::Storage)?;
            match unsafe { sqlite3_step(slot.raw) } {
                SQLITE_ROW => {
                    let occupied_track =
                        column_text(slot.raw, 0).map_err(CaptionTrackWriteError::Storage)?;
                    if occupied_track != write.track_id {
                        return Err(CaptionTrackWriteError::SlotOccupied {
                            track_id: occupied_track,
                            revision: column_u64(slot.raw, 1, "caption slot revision")
                                .map_err(CaptionTrackWriteError::Storage)?,
                        });
                    }
                }
                SQLITE_DONE => {}
                _ => return Err(CaptionTrackWriteError::Storage(sqlite_error(self.database))),
            }

            let revision = write.expected_revision.checked_add(1).ok_or_else(|| {
                CaptionTrackWriteError::Invalid("caption track revision overflow".into())
            })?;
            let created_at_unix_ms = current
                .as_ref()
                .map_or(write.occurred_at_unix_ms, |track| track.created_at_unix_ms);
            let cue_hash = write.cues_sha256();
            let track_identity = c_string(&write.track_id, "caption track identity")
                .map_err(CaptionTrackWriteError::Storage)?;
            let source_kind = c_string(write.source_kind.storage_name(), "caption source kind")
                .map_err(CaptionTrackWriteError::Storage)?;
            let label = c_string(&write.label, "caption track label")
                .map_err(CaptionTrackWriteError::Storage)?;
            let cue_hash_value = c_string(&cue_hash, "caption cue digest")
                .map_err(CaptionTrackWriteError::Storage)?;
            let actor = c_string(&write.actor, "caption track actor")
                .map_err(CaptionTrackWriteError::Storage)?;
            let upsert = self
                .prepare(
                    "INSERT INTO asset_caption_tracks(\
                       track_id, asset_id, language, service_number, source_kind, label, revision, \
                       cue_count, cues_sha256, created_at_unix_ms, updated_at_unix_ms, updated_by\
                     ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12) \
                     ON CONFLICT(track_id) DO UPDATE SET \
                       source_kind=excluded.source_kind, label=excluded.label, revision=excluded.revision, \
                       cue_count=excluded.cue_count, cues_sha256=excluded.cues_sha256, \
                       updated_at_unix_ms=excluded.updated_at_unix_ms, updated_by=excluded.updated_by",
                )
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, upsert.raw, 1, &track_identity)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, upsert.raw, 2, &asset_identity)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, upsert.raw, 3, &language)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(
                self.database,
                upsert.raw,
                4,
                i64::from(write.service_number),
            )
            .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, upsert.raw, 5, &source_kind)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, upsert.raw, 6, &label)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(
                self.database,
                upsert.raw,
                7,
                checked_i64(revision, "caption track revision")
                    .map_err(CaptionTrackWriteError::Storage)?,
            )
            .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(
                self.database,
                upsert.raw,
                8,
                checked_i64(write.cues.len() as u64, "caption cue count")
                    .map_err(CaptionTrackWriteError::Storage)?,
            )
            .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, upsert.raw, 9, &cue_hash_value)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(self.database, upsert.raw, 10, created_at_unix_ms)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(self.database, upsert.raw, 11, write.occurred_at_unix_ms)
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, upsert.raw, 12, &actor)
                .map_err(CaptionTrackWriteError::Storage)?;
            expect_done(self.database, upsert.raw).map_err(CaptionTrackWriteError::Storage)?;

            let clear = self
                .prepare("DELETE FROM asset_caption_cues WHERE track_id=?1")
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_text(self.database, clear.raw, 1, &track_identity)
                .map_err(CaptionTrackWriteError::Storage)?;
            expect_done(self.database, clear.raw).map_err(CaptionTrackWriteError::Storage)?;
            for (ordinal, cue) in write.cues.iter().enumerate() {
                insert_caption_cue(self, "asset_caption_cues", &track_identity, ordinal, cue)
                    .map_err(CaptionTrackWriteError::Storage)?;
            }

            let audit_identity = c_string(&write.audit_id, "caption track audit identity")
                .map_err(CaptionTrackWriteError::Storage)?;
            let audit = self
                .prepare(
                    "INSERT INTO asset_caption_track_revisions(\
                       audit_id, track_id, asset_id, language, service_number, source_kind, label, \
                       revision, cue_count, cues_sha256, actor, occurred_at_unix_ms\
                     ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
                )
                .map_err(CaptionTrackWriteError::Storage)?;
            for (parameter, value) in [
                (1, &audit_identity),
                (2, &track_identity),
                (3, &asset_identity),
                (4, &language),
                (6, &source_kind),
                (7, &label),
                (10, &cue_hash_value),
                (11, &actor),
            ] {
                bind_text(self.database, audit.raw, parameter, value)
                    .map_err(CaptionTrackWriteError::Storage)?;
            }
            bind_i64(self.database, audit.raw, 5, i64::from(write.service_number))
                .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(
                self.database,
                audit.raw,
                8,
                checked_i64(revision, "caption audit revision")
                    .map_err(CaptionTrackWriteError::Storage)?,
            )
            .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(
                self.database,
                audit.raw,
                9,
                checked_i64(write.cues.len() as u64, "caption audit cue count")
                    .map_err(CaptionTrackWriteError::Storage)?,
            )
            .map_err(CaptionTrackWriteError::Storage)?;
            bind_i64(self.database, audit.raw, 12, write.occurred_at_unix_ms)
                .map_err(CaptionTrackWriteError::Storage)?;
            expect_done(self.database, audit.raw).map_err(CaptionTrackWriteError::Storage)?;
            for (ordinal, cue) in write.cues.iter().enumerate() {
                insert_caption_cue(
                    self,
                    "asset_caption_track_revision_cues",
                    &audit_identity,
                    ordinal,
                    cue,
                )
                .map_err(CaptionTrackWriteError::Storage)?;
            }
            self.read_asset_caption_track(&write.track_id)
                .map_err(CaptionTrackWriteError::Storage)?
                .ok_or_else(|| {
                    CaptionTrackWriteError::Storage(
                        "caption track disappeared during its write transaction".into(),
                    )
                })
        })();
        match outcome {
            Ok(track) => match self.execute("COMMIT") {
                Ok(()) => Ok(track),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CaptionTrackWriteError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn list_asset_caption_track_audit(
        &self,
        track_id: &str,
    ) -> Result<Vec<AssetCaptionTrackAuditEvent>, String> {
        if !is_canonical_uuid(track_id) {
            return Err("caption track identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT r.audit_id, r.track_id, r.asset_id, r.language, r.service_number, \
                    r.source_kind, r.label, r.revision, r.cue_count, r.cues_sha256, \
                    r.actor, r.occurred_at_unix_ms, a.duration_ms \
             FROM asset_caption_track_revisions r JOIN media_assets a ON a.asset_id=r.asset_id \
             WHERE r.track_id=?1 ORDER BY r.revision, r.audit_id",
        )?;
        let identity = c_string(track_id, "caption track identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let audit_id = column_text(statement.raw, 0)?;
                    let cue_count =
                        usize::try_from(column_u64(statement.raw, 8, "caption audit cue count")?)
                            .map_err(|_| {
                            "database returned an invalid caption audit cue count".to_string()
                        })?;
                    let asset_duration_ms =
                        column_u64(statement.raw, 12, "caption audit asset duration")?;
                    let mut event = AssetCaptionTrackAuditEvent {
                        audit_id: audit_id.clone(),
                        track_id: column_text(statement.raw, 1)?,
                        asset_id: column_text(statement.raw, 2)?,
                        language: column_text(statement.raw, 3)?,
                        service_number: u8::try_from(column_u64(
                            statement.raw,
                            4,
                            "caption audit service number",
                        )?)
                        .map_err(|_| {
                            "database returned an invalid caption audit service number".to_string()
                        })?,
                        source_kind: CaptionTrackSourceKind::from_storage(&column_text(
                            statement.raw,
                            5,
                        )?)?,
                        label: column_text(statement.raw, 6)?,
                        cues: Vec::with_capacity(cue_count),
                        revision: column_u64(statement.raw, 7, "caption audit revision")?,
                        cues_sha256: column_text(statement.raw, 9)?,
                        actor: column_text(statement.raw, 10)?,
                        occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 11) },
                    };
                    let cue_statement = self.prepare(
                        "SELECT cue_ordinal, cue_id, start_ms, duration_ms, cue_text \
                         FROM asset_caption_track_revision_cues \
                         WHERE audit_id=?1 ORDER BY cue_ordinal",
                    )?;
                    let audit_identity = c_string(&audit_id, "caption track audit identity")?;
                    bind_text(self.database, cue_statement.raw, 1, &audit_identity)?;
                    loop {
                        match unsafe { sqlite3_step(cue_statement.raw) } {
                            SQLITE_ROW => {
                                let ordinal = usize::try_from(column_u64(
                                    cue_statement.raw,
                                    0,
                                    "caption audit cue ordinal",
                                )?)
                                .map_err(|_| {
                                    "database returned an invalid caption audit cue ordinal"
                                        .to_string()
                                })?;
                                if ordinal != event.cues.len() {
                                    return Err(
                                        "database returned a noncontiguous caption audit cue sequence"
                                            .into(),
                                    );
                                }
                                event.cues.push(AssetCaptionCue {
                                    cue_id: column_text(cue_statement.raw, 1)?,
                                    start_ms: column_u64(
                                        cue_statement.raw,
                                        2,
                                        "caption audit cue start",
                                    )?,
                                    duration_ms: column_u64(
                                        cue_statement.raw,
                                        3,
                                        "caption audit cue duration",
                                    )?,
                                    text: column_text(cue_statement.raw, 4)?,
                                });
                            }
                            SQLITE_DONE => break,
                            _ => return Err(sqlite_error(self.database)),
                        }
                    }
                    if event.cues.len() != cue_count {
                        return Err(
                            "database caption audit cue count does not match the revision".into(),
                        );
                    }
                    event.validate(asset_duration_ms)?;
                    events.push(event);
                }
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn write_channel_caption_policy(
        &self,
        channel_id: &str,
        mode: ChannelCaptionMode,
        service_number: u8,
        language: &str,
        expected_revision: u64,
        actor: &str,
        occurred_at_unix_ms: i64,
        audit_id: &str,
    ) -> Result<ChannelCaptionPolicy, CaptionPolicyWriteError> {
        if !is_canonical_uuid(channel_id)
            || !is_canonical_uuid(audit_id)
            || !(1..=63).contains(&service_number)
            || !is_caption_language(language)
            || occurred_at_unix_ms < 0
            || !secret_field(actor, 200)
        {
            return Err(CaptionPolicyWriteError::Invalid(
                "caption policy write facts are invalid or unbounded".into(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CaptionPolicyWriteError::Storage)?;
        let outcome = (|| {
            let channel_configuration = self
                .list_channels()
                .map_err(CaptionPolicyWriteError::Storage)?
                .into_iter()
                .find(|channel| channel.channel_id == channel_id)
                .ok_or(CaptionPolicyWriteError::NotFound)?;
            if channel_configuration.enabled {
                return Err(CaptionPolicyWriteError::MustBeStopped);
            }
            let current = self
                .read_channel_caption_policy(channel_id)
                .map_err(CaptionPolicyWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CaptionPolicyWriteError::Conflict(current_revision));
            }
            if current
                .as_ref()
                .is_some_and(|value| occurred_at_unix_ms < value.updated_at_unix_ms)
            {
                return Err(CaptionPolicyWriteError::Invalid(
                    "caption policy write time precedes current authority".into(),
                ));
            }
            let revision = current_revision.checked_add(1).ok_or_else(|| {
                CaptionPolicyWriteError::Invalid("caption policy revision overflow".into())
            })?;
            let created_at_unix_ms = current
                .as_ref()
                .map_or(occurred_at_unix_ms, |value| value.created_at_unix_ms);
            let statement = self
                .prepare(
                    "INSERT INTO channel_caption_policies(\
                       channel_id, mode, service_number, language, revision, \
                       created_at_unix_ms, updated_at_unix_ms, updated_by\
                     ) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8) \
                     ON CONFLICT(channel_id) DO UPDATE SET \
                       mode=excluded.mode, service_number=excluded.service_number, \
                       language=excluded.language, revision=excluded.revision, \
                       updated_at_unix_ms=excluded.updated_at_unix_ms, updated_by=excluded.updated_by",
                )
                .map_err(CaptionPolicyWriteError::Storage)?;
            let channel = c_string(channel_id, "caption policy channel identity")
                .map_err(CaptionPolicyWriteError::Storage)?;
            let mode_value = c_string(mode.storage_name(), "caption mode")
                .map_err(CaptionPolicyWriteError::Storage)?;
            let language_value =
                c_string(language, "caption language").map_err(CaptionPolicyWriteError::Storage)?;
            let actor_value = c_string(actor, "caption policy actor")
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &channel)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, statement.raw, 2, &mode_value)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 3, i64::from(service_number))
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, statement.raw, 4, &language_value)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                checked_i64(revision, "caption policy revision")
                    .map_err(CaptionPolicyWriteError::Storage)?,
            )
            .map_err(CaptionPolicyWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 6, created_at_unix_ms)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 7, occurred_at_unix_ms)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, statement.raw, 8, &actor_value)
                .map_err(CaptionPolicyWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CaptionPolicyWriteError::Storage)?;

            let audit = self
                .prepare(
                    "INSERT INTO channel_caption_policy_audit(\
                       audit_id, channel_id, mode, service_number, language, revision, actor, occurred_at_unix_ms\
                     ) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                )
                .map_err(CaptionPolicyWriteError::Storage)?;
            let audit_identity = c_string(audit_id, "caption policy audit identity")
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, audit.raw, 1, &audit_identity)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, audit.raw, 2, &channel)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, audit.raw, 3, &mode_value)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_i64(self.database, audit.raw, 4, i64::from(service_number))
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, audit.raw, 5, &language_value)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_i64(
                self.database,
                audit.raw,
                6,
                checked_i64(revision, "caption policy audit revision")
                    .map_err(CaptionPolicyWriteError::Storage)?,
            )
            .map_err(CaptionPolicyWriteError::Storage)?;
            bind_text(self.database, audit.raw, 7, &actor_value)
                .map_err(CaptionPolicyWriteError::Storage)?;
            bind_i64(self.database, audit.raw, 8, occurred_at_unix_ms)
                .map_err(CaptionPolicyWriteError::Storage)?;
            expect_done(self.database, audit.raw).map_err(CaptionPolicyWriteError::Storage)?;
            self.read_channel_caption_policy(channel_id)
                .map_err(CaptionPolicyWriteError::Storage)?
                .ok_or_else(|| {
                    CaptionPolicyWriteError::Storage(
                        "caption policy disappeared during its write transaction".into(),
                    )
                })
        })();
        match outcome {
            Ok(policy) => match self.execute("COMMIT") {
                Ok(()) => Ok(policy),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CaptionPolicyWriteError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn list_channel_caption_policy_audit(
        &self,
        channel_id: &str,
    ) -> Result<Vec<ChannelCaptionPolicyAuditEvent>, String> {
        if !is_canonical_uuid(channel_id) {
            return Err("caption policy channel identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT audit_id, channel_id, mode, service_number, language, revision, actor, occurred_at_unix_ms \
             FROM channel_caption_policy_audit WHERE channel_id=?1 ORDER BY revision, audit_id",
        )?;
        let channel = c_string(channel_id, "caption policy channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel)?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_caption_policy_audit_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_media_assets(&self) -> Result<Vec<MediaAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, media_path, duration_ms, readiness FROM media_assets ORDER BY asset_id",
        )?;
        let mut assets = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => assets.push(read_media_asset_row(statement.raw)?),
                SQLITE_DONE => return Ok(assets),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_channel_loudness_proof_snapshot(
        &self,
        channel_id: &str,
    ) -> Result<Option<ChannelLoudnessProofSnapshot>, String> {
        self.execute("BEGIN DEFERRED")?;
        let outcome: Result<Option<ChannelLoudnessProofSnapshot>, String> = (|| {
            let Some(channel) = self
                .list_channels()?
                .into_iter()
                .find(|channel| channel.channel_id == channel_id)
            else {
                return Ok(None);
            };
            let output_sinks = channel
                .output_sinks
                .into_iter()
                .filter(|sink| {
                    sink.enabled && matches!(sink.kind, OutputSinkKind::UdpTs | OutputSinkKind::Srt)
                })
                .collect::<Vec<_>>();
            let configuration_hash = self.current_channel_output_configuration_hash(channel_id)?;
            let mut latest_proofs = Vec::with_capacity(output_sinks.len());
            for sink in &output_sinks {
                if let Some(proof) =
                    self.read_latest_sink_loudness_proof(channel_id, &sink.sink_id)?
                {
                    latest_proofs.push(proof);
                }
            }
            Ok(Some(ChannelLoudnessProofSnapshot {
                channel_id: channel_id.to_string(),
                output_sinks,
                configuration_hash,
                latest_proofs,
            }))
        })();
        match outcome {
            Ok(snapshot) => match self.execute("COMMIT") {
                Ok(()) => Ok(snapshot),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn create_output_loudness_job(&self, job: &OutputLoudnessProofJob) -> Result<bool, String> {
        job.validate()?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current_hash = self.current_channel_output_configuration_hash(&job.channel_id)?;
            if current_hash != job.configuration_hash {
                return Err("output loudness job configuration hash is stale".into());
            }
            let sink_statement = self.prepare(
                "SELECT kind, loudness_target_milli, enabled FROM channel_output_sinks WHERE channel_id=?1 AND sink_id=?2",
            )?;
            let channel_id = c_string(&job.channel_id, "output loudness channel identity")?;
            let sink_id = c_string(&job.sink_id, "output loudness sink identity")?;
            bind_text(self.database, sink_statement.raw, 1, &channel_id)?;
            bind_text(self.database, sink_statement.raw, 2, &sink_id)?;
            match unsafe { sqlite3_step(sink_statement.raw) } {
                SQLITE_ROW => {
                    let kind = column_text(sink_statement.raw, 0)?;
                    let target = unsafe { sqlite3_column_int64(sink_statement.raw, 1) };
                    let enabled = unsafe { sqlite3_column_int64(sink_statement.raw, 2) } == 1;
                    if !enabled || !matches!(kind.as_str(), "udp-ts" | "srt") {
                        return Err(
                            "output loudness job sink is not an enabled transport output".into(),
                        );
                    }
                    if target != i64::from(job.target_lufs_milli) {
                        return Err("output loudness job target does not match current sink".into());
                    }
                }
                SQLITE_DONE => {
                    return Err("output loudness job sink is not currently configured".into());
                }
                _ => return Err(sqlite_error(self.database)),
            }
            let statement = self.prepare(
                "INSERT OR IGNORE INTO output_loudness_jobs(job_id, channel_id, target_group, sink_id, target_lufs_milli, candidate_version, source_commit, configuration_hash, sample_duration_ms, max_capture_bytes, state, created_at_unix_ms, updated_at_unix_ms, lease_expires_at_unix_ms, capture_relative_path, capture_bytes, capture_sample_duration_ms, capture_sha256, completed_at_unix_ms, failed_at_unix_ms, failure_detail) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 'pending', ?11, ?11, -1, '', -1, -1, '', -1, -1, '')",
            )?;
            let values = [
                c_string(&job.job_id, "output loudness job identity")?,
                c_string(&job.channel_id, "output loudness channel identity")?,
                c_string(&job.target_group, "output loudness target group")?,
                c_string(&job.sink_id, "output loudness sink identity")?,
                c_string(&job.candidate_version, "output loudness candidate version")?,
                c_string(
                    job.source_commit.as_deref().unwrap_or_default(),
                    "output loudness source commit",
                )?,
                c_string(
                    &job.configuration_hash,
                    "output loudness configuration hash",
                )?,
            ];
            bind_text(self.database, statement.raw, 1, &values[0])?;
            bind_text(self.database, statement.raw, 2, &values[1])?;
            bind_text(self.database, statement.raw, 3, &values[2])?;
            bind_text(self.database, statement.raw, 4, &values[3])?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                i64::from(job.target_lufs_milli),
            )?;
            bind_text(self.database, statement.raw, 6, &values[4])?;
            bind_text(self.database, statement.raw, 7, &values[5])?;
            bind_text(self.database, statement.raw, 8, &values[6])?;
            bind_i64(
                self.database,
                statement.raw,
                9,
                checked_i64(job.sample_duration_ms, "output loudness sample duration")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                10,
                checked_i64(job.max_capture_bytes, "output loudness capture limit")?,
            )?;
            bind_i64(self.database, statement.raw, 11, job.created_at_unix_ms)?;
            expect_done(self.database, statement.raw)?;
            Ok(self.changes() == 1)
        })();
        match outcome {
            Ok(created) => match self.execute("COMMIT") {
                Ok(()) => Ok(created),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_output_loudness_job(
        &self,
        job_id: &str,
    ) -> Result<Option<OutputLoudnessProofJobRecord>, String> {
        validate_output_loudness_job_id(job_id)?;
        let statement = self.prepare(
            "SELECT job_id, channel_id, target_group, sink_id, target_lufs_milli, candidate_version, source_commit, configuration_hash, sample_duration_ms, max_capture_bytes, state, created_at_unix_ms, updated_at_unix_ms, lease_expires_at_unix_ms, capture_relative_path, capture_bytes, capture_sample_duration_ms, capture_sha256, completed_at_unix_ms, failed_at_unix_ms, failure_detail FROM output_loudness_jobs WHERE job_id=?1",
        )?;
        let job_id = c_string(job_id, "output loudness job identity")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_output_loudness_job_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_unfinished_output_loudness_jobs_for_channel(
        &self,
        channel_id: &str,
    ) -> Result<Vec<OutputLoudnessProofJobRecord>, String> {
        if !is_canonical_uuid(channel_id) {
            return Err("output loudness channel identity must be a canonical UUID".into());
        }
        let statement = self.prepare(
            "SELECT job_id, channel_id, target_group, sink_id, target_lufs_milli, candidate_version, source_commit, configuration_hash, sample_duration_ms, max_capture_bytes, state, created_at_unix_ms, updated_at_unix_ms, lease_expires_at_unix_ms, capture_relative_path, capture_bytes, capture_sample_duration_ms, capture_sha256, completed_at_unix_ms, failed_at_unix_ms, failure_detail FROM output_loudness_jobs WHERE channel_id=?1 AND state IN ('pending','claimed','capture_pending','analyzing') ORDER BY created_at_unix_ms, job_id",
        )?;
        let channel_id = c_string(channel_id, "output loudness channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(read_output_loudness_job_row(statement.raw)?),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn claim_output_loudness_job(
        &self,
        job_id: &str,
        now_unix_ms: i64,
        lease_duration_ms: i64,
    ) -> Result<Option<OutputLoudnessProofJobRecord>, String> {
        validate_output_loudness_lease(now_unix_ms, lease_duration_ms)?;
        validate_output_loudness_job_id(job_id)?;
        let lease_expires_at_unix_ms = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "output loudness lease time overflowed".to_string())?;
        let job_id = c_string(job_id, "output loudness job identity")?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            recover_expired_output_loudness_jobs_in_transaction(self, now_unix_ms)?;
            let statement = self.prepare(
                "UPDATE output_loudness_jobs SET state='claimed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=?2 WHERE job_id=?3 AND state='pending'",
            )?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_i64(self.database, statement.raw, 2, lease_expires_at_unix_ms)?;
            bind_text(self.database, statement.raw, 3, &job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Ok(None);
            }
            self.read_output_loudness_job(&job_id.to_string_lossy())
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn claim_next_output_loudness_job(
        &self,
        channel_id: &str,
        now_unix_ms: i64,
        lease_duration_ms: i64,
    ) -> Result<Option<OutputLoudnessProofJobRecord>, String> {
        validate_output_loudness_lease(now_unix_ms, lease_duration_ms)?;
        if !is_canonical_uuid(channel_id) {
            return Err("output loudness channel identity must be a canonical UUID".into());
        }
        let lease_expires_at_unix_ms = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "output loudness lease time overflowed".to_string())?;
        let channel_id = c_string(channel_id, "output loudness channel identity")?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            recover_expired_output_loudness_jobs_in_transaction(self, now_unix_ms)?;
            let select = self.prepare(
                "SELECT job_id FROM output_loudness_jobs WHERE channel_id=?1 AND state='pending' ORDER BY created_at_unix_ms, job_id LIMIT 1",
            )?;
            bind_text(self.database, select.raw, 1, &channel_id)?;
            let job_id = match unsafe { sqlite3_step(select.raw) } {
                SQLITE_ROW => column_text(select.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            drop(select);
            let job_id_value = c_string(&job_id, "output loudness job identity")?;
            let update = self.prepare(
                "UPDATE output_loudness_jobs SET state='claimed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=?2 WHERE job_id=?3 AND channel_id=?4 AND state='pending'",
            )?;
            bind_i64(self.database, update.raw, 1, now_unix_ms)?;
            bind_i64(self.database, update.raw, 2, lease_expires_at_unix_ms)?;
            bind_text(self.database, update.raw, 3, &job_id_value)?;
            bind_text(self.database, update.raw, 4, &channel_id)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("output loudness next-job claim lost its write lock".into());
            }
            self.read_output_loudness_job(&job_id)
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn advance_output_loudness_job(
        &self,
        job_id: &str,
        expected_state: OutputLoudnessProofJobState,
        next_state: OutputLoudnessProofJobState,
        now_unix_ms: i64,
        lease_duration_ms: i64,
        capture: Option<&OutputLoudnessCaptureFacts>,
    ) -> Result<OutputLoudnessProofJobRecord, String> {
        validate_output_loudness_lease(now_unix_ms, lease_duration_ms)?;
        validate_output_loudness_job_id(job_id)?;
        if !valid_output_loudness_transition(expected_state, next_state) {
            return Err("output loudness job transition is not allowed".into());
        }
        if capture.is_some() && next_state != OutputLoudnessProofJobState::Analyzing {
            return Err("capture facts may only be attached when analysis begins".into());
        }
        let lease_expires_at_unix_ms = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "output loudness lease time overflowed".to_string())?;
        let job_id_value = c_string(job_id, "output loudness job identity")?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self
                .read_output_loudness_job(&job_id_value.to_string_lossy())?
                .ok_or_else(|| "output loudness job does not exist".to_string())?;
            if next_state == OutputLoudnessProofJobState::Analyzing && capture.is_none() {
                return Err("analysis cannot begin without bounded capture facts".into());
            }
            if next_state == OutputLoudnessProofJobState::Completed && current.capture.is_none() {
                return Err("output loudness job cannot complete without capture facts".into());
            }
            if let Some(capture) = capture {
                capture.validate(current.max_capture_bytes, current.sample_duration_ms)?;
            }
            let next_state_value = c_string(
                output_loudness_job_state_name(next_state),
                "output loudness job state",
            )?;
            let (capture_path, capture_bytes, capture_duration, capture_hash) = capture
                .map(|value| {
                    (
                        value.artifact_relative_path.as_str(),
                        value.capture_bytes as i64,
                        value.sample_duration_ms as i64,
                        value.capture_sha256.as_str(),
                    )
                })
                .unwrap_or(("", -1, -1, ""));
            let capture_path = c_string(capture_path, "output loudness capture path")?;
            let capture_hash = c_string(capture_hash, "output loudness capture hash")?;
            let expected_state_value = c_string(
                output_loudness_job_state_name(expected_state),
                "output loudness expected state",
            )?;
            let statement = self.prepare(
                "UPDATE output_loudness_jobs SET state=?1, updated_at_unix_ms=?2, lease_expires_at_unix_ms=?3, capture_relative_path=CASE WHEN ?4='' THEN capture_relative_path ELSE ?4 END, capture_bytes=CASE WHEN ?5<0 THEN capture_bytes ELSE ?5 END, capture_sample_duration_ms=CASE WHEN ?6<0 THEN capture_sample_duration_ms ELSE ?6 END, capture_sha256=CASE WHEN ?7='' THEN capture_sha256 ELSE ?7 END, completed_at_unix_ms=CASE WHEN ?1='completed' THEN ?2 ELSE completed_at_unix_ms END WHERE job_id=?8 AND state=?9 AND state NOT IN ('completed','failed') AND lease_expires_at_unix_ms>?2",
            )?;
            bind_text(self.database, statement.raw, 1, &next_state_value)?;
            bind_i64(self.database, statement.raw, 2, now_unix_ms)?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                if next_state == OutputLoudnessProofJobState::Completed {
                    -1
                } else {
                    lease_expires_at_unix_ms
                },
            )?;
            bind_text(self.database, statement.raw, 4, &capture_path)?;
            bind_i64(self.database, statement.raw, 5, capture_bytes)?;
            bind_i64(self.database, statement.raw, 6, capture_duration)?;
            bind_text(self.database, statement.raw, 7, &capture_hash)?;
            bind_text(self.database, statement.raw, 8, &job_id_value)?;
            bind_text(self.database, statement.raw, 9, &expected_state_value)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("output loudness job compare-and-set failed or lease expired".into());
            }
            self.read_output_loudness_job(&job_id_value.to_string_lossy())?
                .ok_or_else(|| "advanced output loudness job disappeared".into())
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn renew_output_loudness_job_lease(
        &self,
        job_id: &str,
        expected_state: OutputLoudnessProofJobState,
        now_unix_ms: i64,
        lease_duration_ms: i64,
    ) -> Result<OutputLoudnessProofJobRecord, String> {
        validate_output_loudness_lease(now_unix_ms, lease_duration_ms)?;
        validate_output_loudness_job_id(job_id)?;
        if !matches!(
            expected_state,
            OutputLoudnessProofJobState::Claimed
                | OutputLoudnessProofJobState::CapturePending
                | OutputLoudnessProofJobState::Analyzing
        ) {
            return Err("only an active output loudness job lease can be renewed".into());
        }
        let lease_expires_at_unix_ms = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "output loudness lease time overflowed".to_string())?;
        let job_id = c_string(job_id, "output loudness job identity")?;
        let expected_state = c_string(
            output_loudness_job_state_name(expected_state),
            "output loudness expected state",
        )?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE output_loudness_jobs SET updated_at_unix_ms=?1, lease_expires_at_unix_ms=?2 WHERE job_id=?3 AND state=?4 AND state NOT IN ('completed','failed') AND lease_expires_at_unix_ms>?1",
            )?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_i64(self.database, statement.raw, 2, lease_expires_at_unix_ms)?;
            bind_text(self.database, statement.raw, 3, &job_id)?;
            bind_text(self.database, statement.raw, 4, &expected_state)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err(
                    "output loudness lease renewal compare-and-set failed or lease expired".into(),
                );
            }
            self.read_output_loudness_job(&job_id.to_string_lossy())?
                .ok_or_else(|| "renewed output loudness job disappeared".into())
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn fail_output_loudness_job(
        &self,
        job_id: &str,
        expected_state: OutputLoudnessProofJobState,
        now_unix_ms: i64,
        error_detail: &str,
    ) -> Result<OutputLoudnessProofJobRecord, String> {
        validate_output_loudness_lease(now_unix_ms, 1_000)?;
        validate_output_loudness_job_id(job_id)?;
        if !matches!(
            expected_state,
            OutputLoudnessProofJobState::Claimed
                | OutputLoudnessProofJobState::CapturePending
                | OutputLoudnessProofJobState::Analyzing
        ) {
            return Err("only an active output loudness job can fail".into());
        }
        if !secret_field(error_detail, 4_000) {
            return Err("output loudness failure detail is invalid or unbounded".into());
        }
        let job_id = c_string(job_id, "output loudness job identity")?;
        let expected_state = c_string(
            output_loudness_job_state_name(expected_state),
            "output loudness expected state",
        )?;
        let error_detail = c_string(error_detail, "output loudness failure detail")?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE output_loudness_jobs SET state='failed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=-1, failed_at_unix_ms=?1, failure_detail=?2 WHERE job_id=?3 AND state=?4 AND lease_expires_at_unix_ms>?1",
            )?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &error_detail)?;
            bind_text(self.database, statement.raw, 3, &job_id)?;
            bind_text(self.database, statement.raw, 4, &expected_state)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("output loudness job compare-and-set failed or lease expired".into());
            }
            self.read_output_loudness_job(&job_id.to_string_lossy())?
                .ok_or_else(|| "failed output loudness job disappeared".into())
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn fail_abandoned_output_loudness_job(
        &self,
        job_id: &str,
        expected_state: OutputLoudnessProofJobState,
        now_unix_ms: i64,
        error_detail: &str,
    ) -> Result<OutputLoudnessProofJobRecord, String> {
        if now_unix_ms < 0 {
            return Err("output loudness abandonment time is invalid".into());
        }
        validate_output_loudness_job_id(job_id)?;
        if !matches!(
            expected_state,
            OutputLoudnessProofJobState::Pending
                | OutputLoudnessProofJobState::Claimed
                | OutputLoudnessProofJobState::CapturePending
                | OutputLoudnessProofJobState::Analyzing
        ) {
            return Err("only an unfinished output loudness job can be abandoned".into());
        }
        if !secret_field(error_detail, 4_000) {
            return Err("output loudness failure detail is invalid or unbounded".into());
        }
        let job_id = c_string(job_id, "output loudness job identity")?;
        let expected_state = c_string(
            output_loudness_job_state_name(expected_state),
            "output loudness expected state",
        )?;
        let error_detail = c_string(error_detail, "output loudness failure detail")?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE output_loudness_jobs SET state='failed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=-1, failed_at_unix_ms=?1, failure_detail=?2 WHERE job_id=?3 AND state=?4 AND state IN ('pending','claimed','capture_pending','analyzing')",
            )?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &error_detail)?;
            bind_text(self.database, statement.raw, 3, &job_id)?;
            bind_text(self.database, statement.raw, 4, &expected_state)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("output loudness abandonment compare-and-set failed".into());
            }
            self.read_output_loudness_job(&job_id.to_string_lossy())?
                .ok_or_else(|| "abandoned output loudness job disappeared".into())
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn complete_output_loudness_job_with_proof(
        &self,
        job_id: &str,
        now_unix_ms: i64,
        proof: &SinkLoudnessProof,
        artifact_relative_path: Option<&str>,
    ) -> Result<OutputLoudnessProofJobRecord, String> {
        validate_output_loudness_lease(now_unix_ms, 1_000)?;
        validate_output_loudness_job_id(job_id)?;
        let job_id_value = c_string(job_id, "output loudness job identity")?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self
                .read_output_loudness_job(job_id)?
                .ok_or_else(|| "output loudness job does not exist".to_string())?;
            if current.state != OutputLoudnessProofJobState::Analyzing
                || current
                    .lease_expires_at_unix_ms
                    .is_none_or(|expires| expires <= now_unix_ms)
            {
                return Err("output loudness job is not analyzing under a current lease".into());
            }
            let capture = current
                .capture
                .as_ref()
                .ok_or_else(|| "output loudness job has no captured evidence".to_string())?;
            if proof.proof_id != current.job_id
                || proof.channel_id != current.channel_id
                || proof.sink_id != current.sink_id
                || proof.target_lufs_milli != current.target_lufs_milli
                || proof.candidate_version != current.candidate_version
                || proof.source_commit != current.source_commit
                || proof.configuration_hash != current.configuration_hash
                || proof.sample_duration_ms != capture.sample_duration_ms
                || proof.capture_bytes != capture.capture_bytes
                || proof.capture_sha256 != capture.capture_sha256
                || artifact_relative_path != Some(capture.artifact_relative_path.as_str())
            {
                return Err("output loudness proof does not match its durable job facts".into());
            }
            self.append_sink_loudness_proof_in_transaction(proof, artifact_relative_path)?;
            let statement = self.prepare(
                "UPDATE output_loudness_jobs SET state='completed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=-1, completed_at_unix_ms=?1 WHERE job_id=?2 AND state='analyzing' AND lease_expires_at_unix_ms>?1",
            )?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &job_id_value)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("output loudness completion compare-and-set failed".into());
            }
            self.read_output_loudness_job(job_id)?
                .ok_or_else(|| "completed output loudness job disappeared".into())
        })();
        match outcome {
            Ok(record) => match self.execute("COMMIT") {
                Ok(()) => Ok(record),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn recover_expired_output_loudness_jobs(&self, now_unix_ms: i64) -> Result<u64, String> {
        if now_unix_ms < 0 {
            return Err("output loudness recovery time is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = recover_expired_output_loudness_jobs_in_transaction(self, now_unix_ms);
        match outcome {
            Ok(recovered) => match self.execute("COMMIT") {
                Ok(()) => Ok(recovered),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn current_channel_output_configuration_hash(
        &self,
        channel_id: &str,
    ) -> Result<String, String> {
        let statement = self.prepare(
            "SELECT channels.output_profile, channels.enabled, channel_output_sinks.sink_order, channel_output_sinks.sink_id, channel_output_sinks.label, channel_output_sinks.kind, channel_output_sinks.destination, channel_output_sinks.loudness_target_milli, channel_output_sinks.required, channel_output_sinks.enabled, channel_output_sinks.headend_profile, channel_output_sinks.headend_muxrate_kbps, channel_output_sinks.headend_packet_size FROM channels JOIN channel_output_sinks ON channel_output_sinks.channel_id=channels.channel_id WHERE channels.channel_id=?1 ORDER BY channel_output_sinks.sink_order, channel_output_sinks.sink_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut digest = Sha256::new();
        digest.update(b"townlight-channel-output-configuration-v1");
        hash_field(
            &mut digest,
            channel_id.as_c_str().to_string_lossy().as_ref(),
        );
        let mut row_count = 0_u32;
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    if row_count == 0 {
                        hash_field(&mut digest, &column_text(statement.raw, 0)?);
                        hash_field(
                            &mut digest,
                            if unsafe { sqlite3_column_int64(statement.raw, 1) } == 1 {
                                "enabled"
                            } else {
                                "disabled"
                            },
                        );
                    }
                    for column in [2, 3, 4, 5, 6, 10] {
                        hash_field(&mut digest, &column_text(statement.raw, column)?);
                    }
                    for column in [7, 8, 9, 11, 12] {
                        hash_field(
                            &mut digest,
                            &unsafe { sqlite3_column_int64(statement.raw, column) }.to_string(),
                        );
                    }
                    row_count = row_count
                        .checked_add(1)
                        .ok_or("channel output sink count exceeds hash range")?;
                }
                SQLITE_DONE => {
                    if row_count == 0 {
                        return Err("channel has no current output sinks".into());
                    }
                    return Ok(format!("{:x}", digest.finalize()));
                }
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    /// Append a new target configuration revision. Credentials are deliberately
    /// out of this authority; the executor resolves them from a separate protected
    /// store and only records the redacted fact that credentials are configured.
    pub fn write_transport_target_config(
        &self,
        channel_id: &str,
        sink_id: &str,
        expected_revision: Option<u64>,
        config: &TargetDeviceConfig,
    ) -> Result<TargetDeviceConfig, String> {
        config.validate()?;
        if channel_id.is_empty() || sink_id.is_empty() {
            return Err("transport target channel and sink are required".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let sink = self
                .prepare("SELECT 1 FROM channel_output_sinks WHERE channel_id=?1 AND sink_id=?2")?;
            bind_text(
                self.database,
                sink.raw,
                1,
                &c_string(channel_id, "transport channel")?,
            )?;
            bind_text(
                self.database,
                sink.raw,
                2,
                &c_string(sink_id, "transport sink")?,
            )?;
            if unsafe { sqlite3_step(sink.raw) } != SQLITE_ROW {
                return Err("transport target sink is not configured".into());
            }
            let current = self.read_transport_target_config(channel_id, sink_id)?;
            let current_revision = current.as_ref().map(|value| value.revision);
            if current_revision != expected_revision {
                return Err(format!(
                    "transport target revision conflict: expected {expected_revision:?}, current {current_revision:?}"
                ));
            }
            let mut stored = config.clone();
            stored.revision = current_revision
                .unwrap_or(0)
                .checked_add(1)
                .ok_or_else(|| "transport target revision overflow".to_string())?;
            stored.created_at_unix_ms = current
                .as_ref()
                .map_or(stored.created_at_unix_ms, |value| value.created_at_unix_ms);
            stored.validate()?;
            let configuration = serde_json::to_string(&stored)
                .map_err(|error| format!("serialize target configuration: {error}"))?;
            let statement = self.prepare(
                "INSERT INTO transport_target_configs(channel_id, sink_id, target_id, revision, record_json) VALUES(?1, ?2, ?3, ?4, ?5)",
            )?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(channel_id, "transport channel")?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                2,
                &c_string(sink_id, "transport sink")?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                3,
                &c_string(&stored.target_id, "transport target")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                i64::try_from(stored.revision)
                    .map_err(|_| "transport target revision exceeds SQLite range".to_string())?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                5,
                &c_string(&configuration, "transport target record")?,
            )?;
            expect_done(self.database, statement.raw)?;
            Ok(stored)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn read_transport_target_config(
        &self,
        channel_id: &str,
        sink_id: &str,
    ) -> Result<Option<TargetDeviceConfig>, String> {
        let statement = self.prepare(
            "SELECT record_json FROM transport_target_configs WHERE channel_id=?1 AND sink_id=?2 ORDER BY revision DESC LIMIT 1",
        )?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "transport channel")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            2,
            &c_string(sink_id, "transport sink")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let config: TargetDeviceConfig =
                    serde_json::from_str(&column_text(statement.raw, 0)?).map_err(|error| {
                        format!("database returned invalid target configuration: {error}")
                    })?;
                config.validate()?;
                Ok(Some(config))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn append_transport_analysis_receipt(
        &self,
        receipt: &TransportAnalysisReceipt,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        receipt.validate_fresh_at(now_unix_ms)?;
        self.validate_transport_proof_binding(TransportProofBinding {
            channel_id: &receipt.channel_id,
            sink_id: &receipt.sink_id,
            target_id: &receipt.target_id,
            target_revision: receipt.target_revision,
            candidate_version: &receipt.candidate_version,
            source_commit: &receipt.source_commit,
            configuration_hash: &receipt.configuration_hash,
        })?;
        let record = serde_json::to_string(receipt)
            .map_err(|error| format!("serialize transport analysis receipt: {error}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "INSERT INTO transport_analysis_receipts(proof_id, channel_id, sink_id, target_id, target_revision, candidate_version, source_commit, configuration_hash, outcome, captured_at_unix_ms, recorded_at_unix_ms, record_json) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            )?;
            let values = [
                (&receipt.proof_id, 1),
                (&receipt.channel_id, 2),
                (&receipt.sink_id, 3),
                (&receipt.target_id, 4),
                (&receipt.candidate_version, 6),
                (&receipt.source_commit, 7),
                (&receipt.configuration_hash, 8),
            ];
            for (value, index) in values {
                bind_text(
                    self.database,
                    statement.raw,
                    index,
                    &c_string(value, "transport analysis field")?,
                )?;
            }
            bind_text(
                self.database,
                statement.raw,
                9,
                &c_string(
                    match receipt.outcome {
                        ProofOutcome::Pass => "pass",
                        ProofOutcome::Fail => "fail",
                    },
                    "transport analysis outcome",
                )?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                i64::try_from(receipt.target_revision)
                    .map_err(|_| "target revision exceeds SQLite range".to_string())?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                10,
                receipt.captured_at_unix_ms,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                11,
                receipt.recorded_at_unix_ms,
            )?;
            bind_text(
                self.database,
                statement.raw,
                12,
                &c_string(&record, "transport analysis record")?,
            )?;
            expect_done(self.database, statement.raw)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn append_target_device_reachability_receipt(
        &self,
        receipt: &TargetDeviceReachabilityReceipt,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        receipt.validate_fresh_at(now_unix_ms)?;
        self.validate_transport_proof_binding(TransportProofBinding {
            channel_id: &receipt.channel_id,
            sink_id: &receipt.sink_id,
            target_id: &receipt.target_id,
            target_revision: receipt.target_revision,
            candidate_version: &receipt.candidate_version,
            source_commit: &receipt.source_commit,
            configuration_hash: &receipt.configuration_hash,
        })?;
        let target = self
            .read_transport_target_config(&receipt.channel_id, &receipt.sink_id)?
            .ok_or_else(|| "target device is not configured".to_string())?;
        if target.target_id != receipt.target_id || target.revision != receipt.target_revision {
            return Err(
                "target device reachability proof is stale for the configured target".into(),
            );
        }
        let record = serde_json::to_string(receipt)
            .map_err(|error| format!("serialize target reachability receipt: {error}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "INSERT INTO target_device_reachability_receipts(proof_id, channel_id, sink_id, target_id, target_revision, candidate_version, source_commit, configuration_hash, outcome, observed_at_unix_ms, recorded_at_unix_ms, record_json) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            )?;
            for (value, index) in [
                (&receipt.proof_id, 1),
                (&receipt.channel_id, 2),
                (&receipt.sink_id, 3),
                (&receipt.target_id, 4),
                (&receipt.candidate_version, 6),
                (&receipt.source_commit, 7),
                (&receipt.configuration_hash, 8),
            ] {
                bind_text(
                    self.database,
                    statement.raw,
                    index,
                    &c_string(value, "reachability field")?,
                )?;
            }
            bind_i64(
                self.database,
                statement.raw,
                5,
                i64::try_from(receipt.target_revision)
                    .map_err(|_| "target revision exceeds SQLite range".to_string())?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                9,
                &c_string(
                    match receipt.outcome {
                        ProofOutcome::Pass => "pass",
                        ProofOutcome::Fail => "fail",
                    },
                    "reachability outcome",
                )?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                10,
                receipt.observed_at_unix_ms,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                11,
                receipt.recorded_at_unix_ms,
            )?;
            bind_text(
                self.database,
                statement.raw,
                12,
                &c_string(&record, "reachability record")?,
            )?;
            expect_done(self.database, statement.raw)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn read_transport_proof_history(
        &self,
        channel_id: &str,
        sink_id: Option<&str>,
        limit: u32,
    ) -> Result<TransportProofHistory, String> {
        let limit = limit.clamp(1, 1_000);
        let target = sink_id
            .map(|sink| self.read_transport_target_config(channel_id, sink))
            .transpose()?
            .flatten();
        let mut analysis = Vec::new();
        let mut reachability = Vec::new();
        let statement = self.prepare("SELECT record_json FROM transport_analysis_receipts WHERE channel_id=?1 AND (?2='' OR sink_id=?2) ORDER BY recorded_at_unix_ms DESC, rowid DESC LIMIT ?3")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "transport channel")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            2,
            &c_string(sink_id.unwrap_or(""), "transport sink")?,
        )?;
        bind_i64(self.database, statement.raw, 3, i64::from(limit))?;
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let row: TransportAnalysisReceipt =
                        serde_json::from_str(&column_text(statement.raw, 0)?).map_err(|error| {
                            format!("database returned invalid transport analysis receipt: {error}")
                        })?;
                    row.validate()?;
                    analysis.push(row);
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        let statement = self.prepare("SELECT record_json FROM target_device_reachability_receipts WHERE channel_id=?1 AND (?2='' OR sink_id=?2) ORDER BY recorded_at_unix_ms DESC, rowid DESC LIMIT ?3")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(channel_id, "transport channel")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            2,
            &c_string(sink_id.unwrap_or(""), "transport sink")?,
        )?;
        bind_i64(self.database, statement.raw, 3, i64::from(limit))?;
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let row: TargetDeviceReachabilityReceipt =
                        serde_json::from_str(&column_text(statement.raw, 0)?).map_err(|error| {
                            format!("database returned invalid reachability receipt: {error}")
                        })?;
                    row.validate()?;
                    reachability.push(row);
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        Ok(TransportProofHistory {
            target,
            analysis,
            reachability,
        })
    }

    pub fn read_current_cable_transport_validation(
        &self,
        channel_id: &str,
        sink_id: &str,
        candidate_version: &str,
        source_commit: &str,
        now_unix_ms: i64,
    ) -> Result<Option<CableTransportValidation>, String> {
        self.execute("BEGIN DEFERRED")?;
        let outcome = self.read_current_cable_transport_validation_in_snapshot(
            channel_id,
            sink_id,
            candidate_version,
            source_commit,
            now_unix_ms,
        );
        self.finish_value_transaction(outcome)
    }

    /// Read every enabled required sink under one SQLite snapshot. Commissioning
    /// uses this rather than per-sink reads that could span an authority change.
    pub fn read_current_cable_transport_validations(
        &self,
        candidate_version: &str,
        source_commit: &str,
        now_unix_ms: i64,
    ) -> Result<Vec<CableTransportValidationSnapshot>, String> {
        self.execute("BEGIN DEFERRED")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT channels.channel_id,channel_output_sinks.sink_id \
                 FROM channels JOIN channel_output_sinks ON channel_output_sinks.channel_id=channels.channel_id \
                 WHERE channels.enabled=1 AND channel_output_sinks.enabled=1 AND channel_output_sinks.required=1 \
                 ORDER BY channels.channel_id,channel_output_sinks.sink_order,channel_output_sinks.sink_id",
            )?;
            let mut sinks = Vec::new();
            loop {
                match unsafe { sqlite3_step(statement.raw) } {
                    SQLITE_ROW => sinks.push((
                        column_text(statement.raw, 0)?,
                        column_text(statement.raw, 1)?,
                    )),
                    SQLITE_DONE => break,
                    _ => return Err(sqlite_error(self.database)),
                }
            }
            drop(statement);
            let mut validations = Vec::with_capacity(sinks.len());
            for (channel_id, sink_id) in sinks {
                validations.push(CableTransportValidationSnapshot {
                    validation: self.read_current_cable_transport_validation_in_snapshot(
                        &channel_id,
                        &sink_id,
                        candidate_version,
                        source_commit,
                        now_unix_ms,
                    )?,
                    channel_id,
                    sink_id,
                });
            }
            Ok(validations)
        })();
        self.finish_value_transaction(outcome)
    }

    fn read_current_cable_transport_validation_in_snapshot(
        &self,
        channel_id: &str,
        sink_id: &str,
        candidate_version: &str,
        source_commit: &str,
        now_unix_ms: i64,
    ) -> Result<Option<CableTransportValidation>, String> {
        let Some(target) = self.read_transport_target_config(channel_id, sink_id)? else {
            return Ok(None);
        };
        let config_hash = self.current_channel_output_configuration_hash(channel_id)?;
        // Pick the newest matching record before considering outcome or
        // freshness. A later failure or future-dated record blocks older pass.
        let analysis = self.read_newest_transport_analysis_receipt(
            channel_id,
            sink_id,
            &target,
            candidate_version,
            source_commit,
            &config_hash,
        )?;
        let reachability = self.read_newest_target_device_reachability_receipt(
            channel_id,
            sink_id,
            &target,
            candidate_version,
            source_commit,
            &config_hash,
        )?;
        Ok(Some(CableTransportValidation::derive(
            CableTransportValidationInput {
                channel_id,
                sink_id,
                target: &target,
                candidate_version,
                source_commit,
                configuration_hash: &config_hash,
                now_unix_ms,
                analysis,
                reachability,
            },
        )?))
    }

    fn read_newest_transport_analysis_receipt(
        &self,
        channel_id: &str,
        sink_id: &str,
        target: &TargetDeviceConfig,
        candidate_version: &str,
        source_commit: &str,
        configuration_hash: &str,
    ) -> Result<Option<TransportAnalysisReceipt>, String> {
        let statement = self.prepare(
            "SELECT record_json FROM transport_analysis_receipts \
             WHERE channel_id=?1 AND sink_id=?2 AND target_id=?3 AND target_revision=?4 \
             AND candidate_version=?5 AND source_commit=?6 AND configuration_hash=?7 \
             ORDER BY recorded_at_unix_ms DESC,rowid DESC LIMIT 1",
        )?;
        bind_transport_receipt_lookup(
            self.database,
            statement.raw,
            TransportReceiptLookup {
                channel_id,
                sink_id,
                target,
                candidate_version,
                source_commit,
                configuration_hash,
            },
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let receipt: TransportAnalysisReceipt =
                    serde_json::from_str(&column_text(statement.raw, 0)?).map_err(|error| {
                        format!("database returned invalid transport analysis receipt: {error}")
                    })?;
                receipt.validate()?;
                Ok(Some(receipt))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_newest_target_device_reachability_receipt(
        &self,
        channel_id: &str,
        sink_id: &str,
        target: &TargetDeviceConfig,
        candidate_version: &str,
        source_commit: &str,
        configuration_hash: &str,
    ) -> Result<Option<TargetDeviceReachabilityReceipt>, String> {
        let statement = self.prepare(
            "SELECT record_json FROM target_device_reachability_receipts \
             WHERE channel_id=?1 AND sink_id=?2 AND target_id=?3 AND target_revision=?4 \
             AND candidate_version=?5 AND source_commit=?6 AND configuration_hash=?7 \
             ORDER BY recorded_at_unix_ms DESC,rowid DESC LIMIT 1",
        )?;
        bind_transport_receipt_lookup(
            self.database,
            statement.raw,
            TransportReceiptLookup {
                channel_id,
                sink_id,
                target,
                candidate_version,
                source_commit,
                configuration_hash,
            },
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let receipt: TargetDeviceReachabilityReceipt =
                    serde_json::from_str(&column_text(statement.raw, 0)?).map_err(|error| {
                        format!("database returned invalid reachability receipt: {error}")
                    })?;
                receipt.validate()?;
                Ok(Some(receipt))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn validate_transport_proof_binding(
        &self,
        binding: TransportProofBinding<'_>,
    ) -> Result<(), String> {
        let TransportProofBinding {
            channel_id,
            sink_id,
            target_id,
            target_revision,
            candidate_version,
            source_commit,
            configuration_hash,
        } = binding;
        let target = self
            .read_transport_target_config(channel_id, sink_id)?
            .ok_or_else(|| "target device is not configured".to_string())?;
        if target.target_id != target_id
            || target.revision != target_revision
            || self.current_channel_output_configuration_hash(channel_id)? != configuration_hash
            || candidate_version.is_empty()
            || source_commit.len() != 40
        {
            return Err(
                "transport proof is stale or does not match current output authority".into(),
            );
        }
        Ok(())
    }

    fn validate_caption_egress_job_current(
        &self,
        job: &CaptionEgressProofJob,
    ) -> Result<(), String> {
        let report = self
            .read_commit_report(&job.report_id)?
            .ok_or_else(|| "caption proof job schedule report does not exist".to_string())?;
        if report.channel_id != job.channel_id || report.asset_id != job.asset_id {
            return Err("caption proof job schedule report binding is stale".into());
        }
        let asset = self
            .read_media_asset(&job.asset_id)?
            .ok_or_else(|| "caption proof job media asset does not exist".to_string())?;
        if asset.readiness != AssetReadiness::Ready {
            return Err("caption proof job asset is not ready".into());
        }
        let policy = self
            .read_channel_caption_policy(&job.channel_id)?
            .ok_or_else(|| "caption proof job channel has no caption policy".to_string())?;
        if policy.mode != ChannelCaptionMode::Cea708
            || policy.revision != job.caption_policy_revision
            || policy.language != job.language
            || policy.service_number != job.service_number
        {
            return Err("caption proof job channel policy binding is stale".into());
        }
        let track = self
            .read_asset_caption_track(&job.track_id)?
            .ok_or_else(|| "caption proof job asset track does not exist".to_string())?;
        if track.asset_id != job.asset_id
            || track.revision != job.track_revision
            || track.cues_sha256 != job.cues_sha256
            || track.language != job.language
            || track.service_number != job.service_number
        {
            return Err("caption proof job asset track binding is stale".into());
        }
        let expected = expected_caption_evidence_for_window(
            &track,
            asset.duration_ms,
            job.capture_started_asset_offset_ms,
            job.capture_duration_ms,
        )?;
        if expected.cue_count != job.expected_cue_count
            || expected.text_sha256 != job.expected_text_sha256
        {
            return Err("caption proof job expected evidence binding is stale".into());
        }
        let current_hash = self.current_channel_output_configuration_hash(&job.channel_id)?;
        if current_hash != job.configuration_hash {
            return Err("caption proof job output configuration binding is stale".into());
        }
        let sink = self.prepare(
            "SELECT kind, enabled FROM channel_output_sinks WHERE channel_id=?1 AND sink_id=?2",
        )?;
        let channel_id = c_string(&job.channel_id, "caption proof job channel identity")?;
        let sink_id = c_string(&job.sink_id, "caption proof job sink identity")?;
        bind_text(self.database, sink.raw, 1, &channel_id)?;
        bind_text(self.database, sink.raw, 2, &sink_id)?;
        match unsafe { sqlite3_step(sink.raw) } {
            SQLITE_ROW => {
                let kind = column_text(sink.raw, 0)?;
                let enabled = unsafe { sqlite3_column_int64(sink.raw, 1) } == 1;
                if !enabled || !matches!(kind.as_str(), "udp-ts" | "srt") {
                    return Err("caption proof job sink is not an enabled transport output".into());
                }
            }
            SQLITE_DONE => return Err("caption proof job sink is not configured".into()),
            _ => return Err(sqlite_error(self.database)),
        }
        Ok(())
    }

    pub fn create_caption_egress_job(&self, job: &CaptionEgressProofJob) -> Result<bool, String> {
        job.validate()?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.validate_caption_egress_job_current(job)?;
            let job_json = serde_json::to_string(job)
                .map_err(|error| format!("serialize caption proof job: {error}"))?;
            let statement = self.prepare(
                "INSERT OR IGNORE INTO caption_egress_jobs(job_id, channel_id, sink_id, state, created_at_unix_ms, updated_at_unix_ms, lease_expires_at_unix_ms, job_json, capture_json, completed_at_unix_ms, failed_at_unix_ms, failure_detail) VALUES(?1, ?2, ?3, 'pending', ?4, ?4, -1, ?5, '', -1, -1, '')",
            )?;
            let values = [
                c_string(&job.job_id, "caption proof job identity")?,
                c_string(&job.channel_id, "caption proof job channel identity")?,
                c_string(&job.sink_id, "caption proof job sink identity")?,
                c_string(&job_json, "caption proof job record")?,
            ];
            bind_text(self.database, statement.raw, 1, &values[0])?;
            bind_text(self.database, statement.raw, 2, &values[1])?;
            bind_text(self.database, statement.raw, 3, &values[2])?;
            bind_i64(self.database, statement.raw, 4, job.created_at_unix_ms)?;
            bind_text(self.database, statement.raw, 5, &values[3])?;
            expect_done(self.database, statement.raw)?;
            Ok(self.changes() == 1)
        })();
        match outcome {
            Ok(created) => match self.execute("COMMIT") {
                Ok(()) => Ok(created),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_caption_egress_job(
        &self,
        job_id: &str,
    ) -> Result<Option<CaptionEgressProofJobRecord>, String> {
        let statement = self.prepare(
            "SELECT job_json, state, updated_at_unix_ms, lease_expires_at_unix_ms, capture_json, completed_at_unix_ms, failed_at_unix_ms, failure_detail FROM caption_egress_jobs WHERE job_id=?1",
        )?;
        let job_id = c_string(job_id, "caption proof job identity")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => read_caption_egress_job_row(statement.raw).map(Some),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_unfinished_caption_egress_jobs_for_channel(
        &self,
        channel_id: &str,
    ) -> Result<Vec<CaptionEgressProofJobRecord>, String> {
        if !is_canonical_uuid(channel_id) {
            return Err("caption proof channel identity must be a canonical UUID".into());
        }
        let statement = self.prepare(
            "SELECT job_json, state, updated_at_unix_ms, lease_expires_at_unix_ms, capture_json, completed_at_unix_ms, failed_at_unix_ms, failure_detail FROM caption_egress_jobs WHERE channel_id=?1 AND state IN ('pending','claimed','capture_pending','analyzing') ORDER BY created_at_unix_ms, job_id",
        )?;
        let channel_id = c_string(channel_id, "caption proof channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(read_caption_egress_job_row(statement.raw)?),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn claim_next_caption_egress_job(
        &self,
        channel_id: &str,
        now_unix_ms: i64,
        lease_duration_ms: u64,
    ) -> Result<Option<CaptionEgressProofJobRecord>, String> {
        let lease_duration_ms = checked_i64(lease_duration_ms, "caption proof job lease duration")?;
        validate_output_loudness_lease(now_unix_ms, lease_duration_ms)?;
        let lease_expires = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "caption proof job lease overflowed".to_string())?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            recover_expired_caption_egress_jobs_in_transaction(self, now_unix_ms)?;
            let selection = self.prepare(
                "SELECT job_id FROM caption_egress_jobs WHERE channel_id=?1 AND state='pending' ORDER BY created_at_unix_ms, job_id LIMIT 1",
            )?;
            let channel_id_value = c_string(channel_id, "caption proof job channel identity")?;
            bind_text(self.database, selection.raw, 1, &channel_id_value)?;
            let job_id = match unsafe { sqlite3_step(selection.raw) } {
                SQLITE_ROW => column_text(selection.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            drop(selection);
            let statement = self.prepare(
                "UPDATE caption_egress_jobs SET state='claimed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=?2 WHERE job_id=?3 AND state='pending'",
            )?;
            let job_id_value = c_string(&job_id, "caption proof job identity")?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_i64(self.database, statement.raw, 2, lease_expires)?;
            bind_text(self.database, statement.raw, 3, &job_id_value)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("caption proof job claim compare-and-set failed".into());
            }
            self.read_caption_egress_job(&job_id)
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn advance_caption_egress_job(
        &self,
        job_id: &str,
        expected_state: CaptionEgressProofJobState,
        next_state: CaptionEgressProofJobState,
        now_unix_ms: i64,
        lease_duration_ms: u64,
        capture: Option<&CaptionProofCaptureFacts>,
    ) -> Result<CaptionEgressProofJobRecord, String> {
        let lease_duration_ms = checked_i64(lease_duration_ms, "caption proof job lease duration")?;
        validate_output_loudness_lease(now_unix_ms, lease_duration_ms)?;
        if !matches!(
            (expected_state, next_state),
            (
                CaptionEgressProofJobState::Claimed,
                CaptionEgressProofJobState::CapturePending
            ) | (
                CaptionEgressProofJobState::CapturePending,
                CaptionEgressProofJobState::Analyzing
            )
        ) || (next_state == CaptionEgressProofJobState::Analyzing) != capture.is_some()
        {
            return Err("caption proof job transition is invalid".into());
        }
        let current = self
            .read_caption_egress_job(job_id)?
            .ok_or_else(|| "caption proof job does not exist".to_string())?;
        if let Some(capture) = capture {
            capture.validate(&current.job)?;
        }
        let lease_expires = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "caption proof job lease overflowed".to_string())?;
        let capture_json = capture
            .map(serde_json::to_string)
            .transpose()
            .map_err(|error| format!("serialize caption proof capture: {error}"))?
            .unwrap_or_default();
        let statement = self.prepare(
            "UPDATE caption_egress_jobs SET state=?1, updated_at_unix_ms=?2, lease_expires_at_unix_ms=?3, capture_json=CASE WHEN ?4='' THEN capture_json ELSE ?4 END WHERE job_id=?5 AND state=?6 AND lease_expires_at_unix_ms>?2",
        )?;
        let values = [
            c_string(
                caption_egress_job_state_name(next_state),
                "caption proof next state",
            )?,
            c_string(&capture_json, "caption proof capture record")?,
            c_string(job_id, "caption proof job identity")?,
            c_string(
                caption_egress_job_state_name(expected_state),
                "caption proof expected state",
            )?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_i64(self.database, statement.raw, 2, now_unix_ms)?;
        bind_i64(self.database, statement.raw, 3, lease_expires)?;
        bind_text(self.database, statement.raw, 4, &values[1])?;
        bind_text(self.database, statement.raw, 5, &values[2])?;
        bind_text(self.database, statement.raw, 6, &values[3])?;
        expect_done(self.database, statement.raw)?;
        if self.changes() != 1 {
            return Err("caption proof job transition compare-and-set failed".into());
        }
        self.read_caption_egress_job(job_id)?
            .ok_or_else(|| "caption proof job disappeared after transition".into())
    }

    pub fn renew_caption_egress_job_lease(
        &self,
        job_id: &str,
        expected_state: CaptionEgressProofJobState,
        now_unix_ms: i64,
        lease_duration_ms: u64,
    ) -> Result<CaptionEgressProofJobRecord, String> {
        let lease_duration_ms = checked_i64(lease_duration_ms, "caption proof job lease duration")?;
        validate_output_loudness_lease(now_unix_ms, lease_duration_ms)?;
        if !matches!(
            expected_state,
            CaptionEgressProofJobState::Claimed
                | CaptionEgressProofJobState::CapturePending
                | CaptionEgressProofJobState::Analyzing
        ) {
            return Err("only an active caption proof job lease can be renewed".into());
        }
        let lease_expires_at_unix_ms = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "caption proof job lease time overflowed".to_string())?;
        let job_id = c_string(job_id, "caption proof job identity")?;
        let expected_state = c_string(
            caption_egress_job_state_name(expected_state),
            "caption proof expected state",
        )?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE caption_egress_jobs SET updated_at_unix_ms=?1, lease_expires_at_unix_ms=?2 WHERE job_id=?3 AND state=?4 AND state NOT IN ('completed','failed') AND lease_expires_at_unix_ms>?1",
            )?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_i64(self.database, statement.raw, 2, lease_expires_at_unix_ms)?;
            bind_text(self.database, statement.raw, 3, &job_id)?;
            bind_text(self.database, statement.raw, 4, &expected_state)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err(
                    "caption proof lease renewal compare-and-set failed or lease expired".into(),
                );
            }
            self.read_caption_egress_job(&job_id.to_string_lossy())?
                .ok_or_else(|| "renewed caption proof job disappeared".into())
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn fail_caption_egress_job(
        &self,
        job_id: &str,
        expected_state: CaptionEgressProofJobState,
        now_unix_ms: i64,
        detail: &str,
    ) -> Result<CaptionEgressProofJobRecord, String> {
        if now_unix_ms < 0 || !secret_field(detail, 4_000) {
            return Err("caption proof job failure facts are invalid".into());
        }
        if matches!(
            expected_state,
            CaptionEgressProofJobState::Completed | CaptionEgressProofJobState::Failed
        ) {
            return Err("terminal caption proof job cannot be failed".into());
        }
        let statement = self.prepare(
            "UPDATE caption_egress_jobs SET state='failed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=-1, failed_at_unix_ms=?1, failure_detail=?2 WHERE job_id=?3 AND state=?4 AND lease_expires_at_unix_ms>?1",
        )?;
        let values = [
            c_string(detail, "caption proof job failure detail")?,
            c_string(job_id, "caption proof job identity")?,
            c_string(
                caption_egress_job_state_name(expected_state),
                "caption proof expected state",
            )?,
        ];
        bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &values[0])?;
        bind_text(self.database, statement.raw, 3, &values[1])?;
        bind_text(self.database, statement.raw, 4, &values[2])?;
        expect_done(self.database, statement.raw)?;
        if self.changes() != 1 {
            return Err("caption proof job failure compare-and-set failed or lease expired".into());
        }
        self.read_caption_egress_job(job_id)?
            .ok_or_else(|| "caption proof job disappeared after failure".into())
    }

    pub fn fail_abandoned_caption_egress_job(
        &self,
        job_id: &str,
        expected_state: CaptionEgressProofJobState,
        now_unix_ms: i64,
        detail: &str,
    ) -> Result<CaptionEgressProofJobRecord, String> {
        if now_unix_ms < 0 || !secret_field(detail, 4_000) {
            return Err("caption proof job abandonment facts are invalid".into());
        }
        if !matches!(
            expected_state,
            CaptionEgressProofJobState::Pending
                | CaptionEgressProofJobState::Claimed
                | CaptionEgressProofJobState::CapturePending
                | CaptionEgressProofJobState::Analyzing
        ) {
            return Err("only an unfinished caption proof job can be abandoned".into());
        }
        let statement = self.prepare(
            "UPDATE caption_egress_jobs SET state='failed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=-1, failed_at_unix_ms=?1, failure_detail=?2 WHERE job_id=?3 AND state=?4",
        )?;
        let values = [
            c_string(detail, "caption proof job abandonment detail")?,
            c_string(job_id, "caption proof job identity")?,
            c_string(
                caption_egress_job_state_name(expected_state),
                "caption proof expected state",
            )?,
        ];
        bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &values[0])?;
        bind_text(self.database, statement.raw, 3, &values[1])?;
        bind_text(self.database, statement.raw, 4, &values[2])?;
        expect_done(self.database, statement.raw)?;
        if self.changes() != 1 {
            return Err("caption proof job abandonment compare-and-set failed".into());
        }
        self.read_caption_egress_job(job_id)?
            .ok_or_else(|| "abandoned caption proof job disappeared".into())
    }

    pub fn recover_expired_caption_egress_jobs(&self, now_unix_ms: i64) -> Result<u64, String> {
        if now_unix_ms < 0 {
            return Err("caption proof recovery time is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = recover_expired_caption_egress_jobs_in_transaction(self, now_unix_ms);
        match outcome {
            Ok(count) => match self.execute("COMMIT") {
                Ok(()) => Ok(count),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn complete_caption_egress_job_with_proof(
        &self,
        job_id: &str,
        now_unix_ms: i64,
        proof: &CaptionEgressProof,
    ) -> Result<CaptionEgressProofJobRecord, String> {
        if now_unix_ms < 0 {
            return Err("caption proof completion time is invalid".into());
        }
        proof.validate()?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self
                .read_caption_egress_job(job_id)?
                .ok_or_else(|| "caption proof job does not exist".to_string())?;
            if current.state != CaptionEgressProofJobState::Analyzing
                || current
                    .lease_expires_at_unix_ms
                    .is_none_or(|expires| expires <= now_unix_ms)
            {
                return Err("caption proof job is not analyzing under a current lease".into());
            }
            let capture = current
                .capture
                .as_ref()
                .ok_or_else(|| "caption proof job has no captured evidence".to_string())?;
            let job = &current.job;
            if proof.proof_id != job.job_id
                || proof.channel_id != job.channel_id
                || proof.sink_id != job.sink_id
                || proof.report_id != job.report_id
                || proof.asset_id != job.asset_id
                || proof.track_id != job.track_id
                || proof.track_revision != job.track_revision
                || proof.cues_sha256 != job.cues_sha256
                || proof.caption_policy_revision != job.caption_policy_revision
                || proof.language != job.language
                || proof.service_number != job.service_number
                || proof.candidate_version != job.candidate_version
                || proof.source_commit != job.source_commit
                || proof.configuration_hash != job.configuration_hash
                || proof.capture_started_asset_offset_ms != job.capture_started_asset_offset_ms
                || proof.capture_duration_ms != capture.capture_duration_ms
                || proof.capture_bytes != capture.capture_bytes
                || proof.capture_sha256 != capture.capture_sha256
                || proof.artifact_relative_path != capture.artifact_relative_path
                || proof.expected_cue_count != job.expected_cue_count
                || proof.expected_text_sha256 != job.expected_text_sha256
            {
                return Err("caption proof does not match its durable job facts".into());
            }
            self.append_caption_egress_proof_in_transaction(proof)?;
            let statement = self.prepare(
                "UPDATE caption_egress_jobs SET state='completed', updated_at_unix_ms=?1, lease_expires_at_unix_ms=-1, completed_at_unix_ms=?1 WHERE job_id=?2 AND state='analyzing' AND lease_expires_at_unix_ms>?1",
            )?;
            let job_id = c_string(job_id, "caption proof job identity")?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("caption proof completion compare-and-set failed".into());
            }
            self.read_caption_egress_job(&current.job.job_id)?
                .ok_or_else(|| "completed caption proof job disappeared".into())
        })();
        match outcome {
            Ok(record) => match self.execute("COMMIT") {
                Ok(()) => Ok(record),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn append_caption_egress_proof(&self, proof: &CaptionEgressProof) -> Result<(), String> {
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = self.append_caption_egress_proof_in_transaction(proof);
        self.finish_transaction(outcome)
    }

    fn append_caption_egress_proof_in_transaction(
        &self,
        proof: &CaptionEgressProof,
    ) -> Result<(), String> {
        proof.validate()?;
        let report = self
            .read_commit_report(&proof.report_id)?
            .ok_or_else(|| "caption proof schedule report does not exist".to_string())?;
        if report.channel_id != proof.channel_id || report.asset_id != proof.asset_id {
            return Err("caption proof schedule report binding is stale".into());
        }
        let asset = self
            .read_media_asset(&proof.asset_id)?
            .ok_or_else(|| "caption proof media asset does not exist".to_string())?;
        let capture_end = proof
            .capture_started_asset_offset_ms
            .checked_add(proof.capture_duration_ms)
            .ok_or_else(|| "caption proof capture window overflowed".to_string())?;
        if asset.readiness != AssetReadiness::Ready || capture_end > asset.duration_ms {
            return Err("caption proof capture window is outside the ready scheduled asset".into());
        }
        let policy = self
            .read_channel_caption_policy(&proof.channel_id)?
            .ok_or_else(|| "caption proof channel has no caption policy".to_string())?;
        if policy.mode != ChannelCaptionMode::Cea708
            || policy.revision != proof.caption_policy_revision
            || policy.language != proof.language
            || policy.service_number != proof.service_number
        {
            return Err("caption proof channel policy binding is stale".into());
        }
        let track = self
            .read_asset_caption_track(&proof.track_id)?
            .ok_or_else(|| "caption proof asset track does not exist".to_string())?;
        if track.asset_id != proof.asset_id
            || track.revision != proof.track_revision
            || track.cues_sha256 != proof.cues_sha256
            || track.language != proof.language
            || track.service_number != proof.service_number
        {
            return Err("caption proof asset track binding is stale".into());
        }
        let expected = expected_caption_evidence_for_window(
            &track,
            asset.duration_ms,
            proof.capture_started_asset_offset_ms,
            proof.capture_duration_ms,
        )?;
        if expected.cue_count != proof.expected_cue_count
            || expected.text_sha256 != proof.expected_text_sha256
        {
            return Err("caption proof expected evidence binding is stale".into());
        }
        let current_hash = self.current_channel_output_configuration_hash(&proof.channel_id)?;
        if current_hash != proof.configuration_hash {
            return Err("caption proof output configuration binding is stale".into());
        }
        let sink = self.prepare(
            "SELECT kind, enabled FROM channel_output_sinks WHERE channel_id=?1 AND sink_id=?2",
        )?;
        let channel_id = c_string(&proof.channel_id, "caption proof channel identity")?;
        let sink_id = c_string(&proof.sink_id, "caption proof sink identity")?;
        bind_text(self.database, sink.raw, 1, &channel_id)?;
        bind_text(self.database, sink.raw, 2, &sink_id)?;
        match unsafe { sqlite3_step(sink.raw) } {
            SQLITE_ROW => {
                let kind = column_text(sink.raw, 0)?;
                let enabled = unsafe { sqlite3_column_int64(sink.raw, 1) } == 1;
                if !enabled || !matches!(kind.as_str(), "udp-ts" | "srt") {
                    return Err("caption proof sink is not an enabled transport output".into());
                }
            }
            SQLITE_DONE => return Err("caption proof sink is not currently configured".into()),
            _ => return Err(sqlite_error(self.database)),
        }
        let record_json = serde_json::to_string(proof)
            .map_err(|error| format!("serialize caption egress proof: {error}"))?;
        let statement = self.prepare(
                "INSERT INTO caption_egress_proofs(proof_id, channel_id, sink_id, report_id, asset_id, track_id, track_revision, caption_policy_revision, cues_sha256, candidate_version, source_commit, configuration_hash, outcome, captured_at_unix_ms, record_json) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
            )?;
        let values = [
            c_string(&proof.proof_id, "caption proof identity")?,
            c_string(&proof.channel_id, "caption proof channel identity")?,
            c_string(&proof.sink_id, "caption proof sink identity")?,
            c_string(&proof.report_id, "caption proof report identity")?,
            c_string(&proof.asset_id, "caption proof asset identity")?,
            c_string(&proof.track_id, "caption proof track identity")?,
            c_string(&proof.cues_sha256, "caption proof cue digest")?,
            c_string(&proof.candidate_version, "caption proof candidate version")?,
            c_string(&proof.source_commit, "caption proof source commit")?,
            c_string(
                &proof.configuration_hash,
                "caption proof configuration hash",
            )?,
            c_string(
                match proof.outcome {
                    CaptionProofOutcome::Verified => "verified",
                    CaptionProofOutcome::Mismatch => "mismatch",
                },
                "caption proof outcome",
            )?,
            c_string(&record_json, "caption proof record")?,
        ];
        for (index, value) in values[..6].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            7,
            checked_i64(proof.track_revision, "caption proof track revision")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            8,
            checked_i64(
                proof.caption_policy_revision,
                "caption proof policy revision",
            )?,
        )?;
        for (index, value) in values[6..11].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 9) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 14, proof.captured_at_unix_ms)?;
        bind_text(self.database, statement.raw, 15, &values[11])?;
        expect_done(self.database, statement.raw)
    }

    pub fn list_caption_egress_proofs(
        &self,
        channel_id: &str,
        sink_id: Option<&str>,
        limit: u32,
    ) -> Result<Vec<CaptionEgressProof>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("caption proof listing limit must be between 1 and 1000".into());
        }
        let statement = if sink_id.is_some() {
            self.prepare(
                "SELECT record_json FROM caption_egress_proofs WHERE channel_id=?1 AND sink_id=?2 ORDER BY captured_at_unix_ms DESC, proof_id DESC LIMIT ?3",
            )?
        } else {
            self.prepare(
                "SELECT record_json FROM caption_egress_proofs WHERE channel_id=?1 ORDER BY captured_at_unix_ms DESC, proof_id DESC LIMIT ?2",
            )?
        };
        let channel_id = c_string(channel_id, "caption proof channel identity")?;
        let sink_id = sink_id
            .map(|value| c_string(value, "caption proof sink identity"))
            .transpose()?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        if let Some(sink_id) = &sink_id {
            bind_text(self.database, statement.raw, 2, sink_id)?;
            bind_i64(self.database, statement.raw, 3, i64::from(limit))?;
        } else {
            bind_i64(self.database, statement.raw, 2, i64::from(limit))?;
        }
        let mut proofs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let json = column_text(statement.raw, 0)?;
                    let proof: CaptionEgressProof = serde_json::from_str(&json)
                        .map_err(|error| format!("decode caption egress proof: {error}"))?;
                    proof.validate()?;
                    proofs.push(proof);
                }
                SQLITE_DONE => return Ok(proofs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn append_sink_loudness_proof(
        &self,
        proof: &SinkLoudnessProof,
        artifact_relative_path: Option<&str>,
    ) -> Result<(), String> {
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = self.append_sink_loudness_proof_in_transaction(proof, artifact_relative_path);
        self.finish_transaction(outcome)
    }

    fn append_sink_loudness_proof_in_transaction(
        &self,
        proof: &SinkLoudnessProof,
        artifact_relative_path: Option<&str>,
    ) -> Result<(), String> {
        proof.validate().map_err(|error| format!("{error:?}"))?;
        if !matches!(
            proof.receiver_acceptance,
            ReceiverAcceptance::NotAttempted | ReceiverAcceptance::Unknown
        ) {
            return Err("local loudness proof cannot claim independent receiver acceptance".into());
        }
        if let Some(path) = artifact_relative_path {
            validate_artifact_relative_path(path)?;
        }
        (|| {
            let current_hash = self.current_channel_output_configuration_hash(&proof.channel_id)?;
            if proof.configuration_hash != current_hash {
                return Err("loudness proof configuration hash is stale".into());
            }
            let statement = self.prepare(
                "SELECT loudness_target_milli FROM channel_output_sinks WHERE channel_id=?1 AND sink_id=?2",
            )?;
            let channel_id = c_string(&proof.channel_id, "loudness proof channel identity")?;
            let sink_id = c_string(&proof.sink_id, "loudness proof sink identity")?;
            bind_text(self.database, statement.raw, 1, &channel_id)?;
            bind_text(self.database, statement.raw, 2, &sink_id)?;
            let target = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => unsafe { sqlite3_column_int64(statement.raw, 0) },
                SQLITE_DONE => return Err("loudness proof sink is not currently configured".into()),
                _ => return Err(sqlite_error(self.database)),
            };
            if target != i64::from(proof.target_lufs_milli) {
                return Err("loudness proof target does not match current sink".into());
            }
            let statement = self.prepare(
                "INSERT INTO channel_output_loudness_proofs(proof_id, channel_id, sink_id, target_lufs_milli, measured_lufs_milli, tolerance_lufs_milli, outcome, receiver_acceptance, captured_at_unix_ms, sample_duration_ms, capture_bytes, capture_sha256, candidate_version, source_commit, configuration_hash, measurement_engine, measurement_method, detail, artifact_relative_path) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19)",
            )?;
            let values = [
                c_string(&proof.proof_id, "loudness proof identity")?,
                c_string(&proof.channel_id, "loudness proof channel identity")?,
                c_string(&proof.sink_id, "loudness proof sink identity")?,
                c_string(outcome_name(proof.outcome), "loudness proof outcome")?,
                c_string(
                    receiver_acceptance_name(proof.receiver_acceptance),
                    "loudness proof receiver acceptance",
                )?,
                c_string(&proof.capture_sha256, "loudness proof capture hash")?,
                c_string(&proof.candidate_version, "loudness proof candidate version")?,
                c_string(
                    proof.source_commit.as_deref().unwrap_or_default(),
                    "loudness proof source commit",
                )?,
                c_string(
                    &proof.configuration_hash,
                    "loudness proof configuration hash",
                )?,
                c_string(
                    &proof.measurement_engine,
                    "loudness proof measurement engine",
                )?,
                c_string(
                    &proof.measurement_method,
                    "loudness proof measurement method",
                )?,
                c_string(&proof.detail, "loudness proof detail")?,
                c_string(
                    artifact_relative_path.unwrap_or_default(),
                    "loudness proof artifact path",
                )?,
            ];
            for (index, value) in values[..3].iter().enumerate() {
                bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
            }
            bind_i64(
                self.database,
                statement.raw,
                4,
                i64::from(proof.target_lufs_milli),
            )?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                proof
                    .measured_lufs_milli
                    .map_or(i64::from(i32::MIN), i64::from),
            )?;
            bind_i64(
                self.database,
                statement.raw,
                6,
                i64::from(proof.tolerance_lufs_milli),
            )?;
            bind_text(self.database, statement.raw, 7, &values[3])?;
            bind_text(self.database, statement.raw, 8, &values[4])?;
            bind_i64(self.database, statement.raw, 9, proof.captured_at_unix_ms)?;
            bind_i64(
                self.database,
                statement.raw,
                10,
                checked_i64(proof.sample_duration_ms, "loudness proof sample duration")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                11,
                checked_i64(proof.capture_bytes, "loudness proof capture size")?,
            )?;
            for (index, value) in values[5..].iter().enumerate() {
                bind_text(self.database, statement.raw, (index + 12) as c_int, value)?;
            }
            expect_done(self.database, statement.raw)
        })()
    }

    pub fn read_latest_sink_loudness_proof(
        &self,
        channel_id: &str,
        sink_id: &str,
    ) -> Result<Option<StoredSinkLoudnessProof>, String> {
        Ok(self
            .list_sink_loudness_proofs(channel_id, Some(sink_id), 1)?
            .into_iter()
            .next())
    }

    pub fn list_sink_loudness_proofs(
        &self,
        channel_id: &str,
        sink_id: Option<&str>,
        limit: u32,
    ) -> Result<Vec<StoredSinkLoudnessProof>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("loudness proof listing limit must be between 1 and 1000".into());
        }
        let statement = if sink_id.is_some() {
            self.prepare(
                "SELECT proof_id, channel_id, sink_id, target_lufs_milli, measured_lufs_milli, tolerance_lufs_milli, outcome, receiver_acceptance, captured_at_unix_ms, sample_duration_ms, capture_bytes, capture_sha256, candidate_version, source_commit, configuration_hash, measurement_engine, measurement_method, detail, artifact_relative_path FROM channel_output_loudness_proofs WHERE channel_id=? AND sink_id=? ORDER BY captured_at_unix_ms DESC, proof_id DESC LIMIT 1000",
            )?
        } else {
            self.prepare(
                "SELECT proof_id, channel_id, sink_id, target_lufs_milli, measured_lufs_milli, tolerance_lufs_milli, outcome, receiver_acceptance, captured_at_unix_ms, sample_duration_ms, capture_bytes, capture_sha256, candidate_version, source_commit, configuration_hash, measurement_engine, measurement_method, detail, artifact_relative_path FROM channel_output_loudness_proofs WHERE channel_id=? ORDER BY captured_at_unix_ms DESC, proof_id DESC LIMIT 1000",
            )?
        };
        let channel_id = c_string(channel_id, "loudness proof channel identity")?;
        let sink_id = sink_id
            .map(|sink_id| c_string(sink_id, "loudness proof sink identity"))
            .transpose()?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        if let Some(sink_id) = &sink_id {
            bind_text(self.database, statement.raw, 2, sink_id)?;
        }
        let mut proofs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    proofs.push(read_sink_loudness_proof_row(statement.raw)?);
                    if proofs.len() >= limit as usize {
                        return Ok(proofs);
                    }
                }
                SQLITE_DONE => return Ok(proofs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn put_asset_loudness(&self, analysis: &AssetLoudnessAnalysis) -> Result<(), String> {
        analysis.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO asset_loudness_analysis(asset_id, status, measured_lufs_milli, \
             target_lufs_milli, detail) VALUES(?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(asset_id) DO UPDATE SET status=excluded.status, \
             measured_lufs_milli=excluded.measured_lufs_milli, target_lufs_milli=excluded.target_lufs_milli, \
             detail=excluded.detail",
        )?;
        let asset_id = c_string(&analysis.asset_id, "loudness asset identity")?;
        let status = c_string(loudness_status_name(analysis.status), "loudness status")?;
        let detail = c_string(
            analysis.detail.as_deref().unwrap_or_default(),
            "loudness detail",
        )?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        bind_text(self.database, statement.raw, 2, &status)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            analysis
                .measured_lufs_milli
                .map_or(i64::from(i32::MIN), i64::from),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            i64::from(analysis.target_lufs_milli),
        )?;
        bind_text(self.database, statement.raw, 5, &detail)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_asset_loudness(
        &self,
        asset_id: &str,
    ) -> Result<Option<AssetLoudnessAnalysis>, String> {
        let statement = self.prepare(
            "SELECT asset_id, status, measured_lufs_milli, target_lufs_milli, detail \
             FROM asset_loudness_analysis WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "loudness asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_asset_loudness_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_asset_loudness(&self) -> Result<Vec<AssetLoudnessAnalysis>, String> {
        let statement = self.prepare(
            "SELECT asset_id, status, measured_lufs_milli, target_lufs_milli, detail \
             FROM asset_loudness_analysis ORDER BY asset_id",
        )?;
        let mut analyses = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => analyses.push(read_asset_loudness_row(statement.raw)?),
                SQLITE_DONE => return Ok(analyses),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_ingest_jobs(&self) -> Result<Vec<MediaIngestJob>, String> {
        let statement = self.prepare(
             "SELECT job_id, source_kind, source_path, status, progress_percent, created_at_unix_ms, \
             started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms \
             FROM media_ingest_jobs ORDER BY created_at_unix_ms DESC, job_id",
        )?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(read_ingest_job_row(statement.raw)?),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn put_watch_folder(&self, config: &WatchFolderConfig) -> Result<(), String> {
        config.validate().map_err(|error| format!("{error:?}"))?;
        if config.last_scan_at_unix_ms.is_some() || config.last_error.is_some() {
            return Err("watch-folder scan state is read-only".into());
        }
        let statement = self.prepare(
            "INSERT INTO watch_folder_configs(config_id, display_name, monitor_path, extensions_json, \
             settle_seconds, enabled, last_scan_at_unix_ms, last_error) VALUES(?1, ?2, ?3, ?4, ?5, ?6, -1, '') \
             ON CONFLICT(config_id) DO UPDATE SET display_name=excluded.display_name, \
             monitor_path=excluded.monitor_path, extensions_json=excluded.extensions_json, \
             settle_seconds=excluded.settle_seconds, enabled=excluded.enabled",
        )?;
        let values = [
            c_string(&config.config_id, "watch-folder identity")?,
            c_string(&config.display_name, "watch-folder display name")?,
            c_string(&config.monitor_path, "watch-folder path")?,
            c_string(
                &serde_json::to_string(&config.extensions).map_err(|error| {
                    format!("could not serialize watch-folder extensions: {error}")
                })?,
                "watch-folder extensions",
            )?,
        ];
        for (index, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            5,
            i64::from(config.settle_seconds),
        )?;
        bind_i64(self.database, statement.raw, 6, i64::from(config.enabled))?;
        expect_done(self.database, statement.raw)
    }

    pub fn list_watch_folders(&self) -> Result<Vec<WatchFolderConfig>, String> {
        let statement = self.prepare(
            "SELECT config_id, display_name, monitor_path, extensions_json, settle_seconds, enabled, \
             last_scan_at_unix_ms, last_error FROM watch_folder_configs ORDER BY display_name, config_id",
        )?;
        let mut configs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => configs.push(read_watch_folder_row(statement.raw)?),
                SQLITE_DONE => return Ok(configs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn record_watch_folder_scan(
        &self,
        config_id: &str,
        scanned_at_unix_ms: i64,
        error_detail: Option<&str>,
    ) -> Result<(), String> {
        if scanned_at_unix_ms < 0
            || error_detail.is_some_and(|value| {
                value.trim().is_empty()
                    || value.len() > 4_000
                    || value.chars().any(char::is_control)
            })
        {
            return Err("watch-folder scan result is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE watch_folder_configs SET last_scan_at_unix_ms=?1, last_error=?2 WHERE config_id=?3",
        )?;
        let error = c_string(error_detail.unwrap_or_default(), "watch-folder scan error")?;
        let config_id = c_string(config_id, "watch-folder identity")?;
        bind_i64(self.database, statement.raw, 1, scanned_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &error)?;
        bind_text(self.database, statement.raw, 3, &config_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("watch-folder configuration does not exist".into())
        }
    }

    pub fn observe_watch_file(
        &self,
        config_id: &str,
        source_path: &str,
        size_bytes: u64,
        modified_at_unix_ms: i64,
        observed_at_unix_ms: i64,
    ) -> Result<WatchFileObservation, String> {
        if source_path.trim().is_empty()
            || source_path.chars().any(char::is_control)
            || modified_at_unix_ms < 0
            || observed_at_unix_ms < 0
        {
            return Err("watch-file observation is invalid".into());
        }
        let existing = self.read_watch_file_observation(config_id, source_path)?;
        let unchanged = existing.as_ref().is_some_and(|value| {
            value.size_bytes == size_bytes && value.modified_at_unix_ms == modified_at_unix_ms
        });
        let unchanged_since = if unchanged {
            existing
                .as_ref()
                .expect("unchanged requires an observation")
                .unchanged_since_unix_ms
        } else {
            observed_at_unix_ms
        };
        let observation_count = if unchanged {
            existing
                .as_ref()
                .expect("unchanged requires an observation")
                .observation_count
                .saturating_add(1)
        } else {
            1
        };
        let queued_job_id = if unchanged {
            existing.and_then(|value| value.queued_job_id)
        } else {
            None
        };
        let statement = self.prepare(
            "INSERT INTO watch_file_observations(config_id, source_path, size_bytes, modified_at_unix_ms, \
             unchanged_since_unix_ms, observation_count, queued_job_id) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(config_id, source_path) DO UPDATE SET size_bytes=excluded.size_bytes, \
             modified_at_unix_ms=excluded.modified_at_unix_ms, unchanged_since_unix_ms=excluded.unchanged_since_unix_ms, \
             observation_count=excluded.observation_count, queued_job_id=excluded.queued_job_id",
        )?;
        let config = c_string(config_id, "watch-folder identity")?;
        let source = c_string(source_path, "watch-file path")?;
        let queued = c_string(
            queued_job_id.as_deref().unwrap_or_default(),
            "watch-file job",
        )?;
        bind_text(self.database, statement.raw, 1, &config)?;
        bind_text(self.database, statement.raw, 2, &source)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            checked_i64(size_bytes, "watch-file size")?,
        )?;
        bind_i64(self.database, statement.raw, 4, modified_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 5, unchanged_since)?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            i64::from(observation_count),
        )?;
        bind_text(self.database, statement.raw, 7, &queued)?;
        expect_done(self.database, statement.raw)?;
        Ok(WatchFileObservation {
            config_id: config_id.into(),
            source_path: source_path.into(),
            size_bytes,
            modified_at_unix_ms,
            unchanged_since_unix_ms: unchanged_since,
            observation_count,
            queued_job_id,
        })
    }

    pub fn mark_watch_file_queued(
        &self,
        config_id: &str,
        source_path: &str,
        job_id: &str,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "UPDATE watch_file_observations SET queued_job_id=?1 WHERE config_id=?2 AND source_path=?3 \
             AND (queued_job_id='' OR queued_job_id=?1)",
        )?;
        let values = [
            c_string(job_id, "watch-file job identity")?,
            c_string(config_id, "watch-folder identity")?,
            c_string(source_path, "watch-file path")?,
        ];
        for (index, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("watch-file observation was not queueable".into())
        }
    }

    fn read_watch_file_observation(
        &self,
        config_id: &str,
        source_path: &str,
    ) -> Result<Option<WatchFileObservation>, String> {
        let statement = self.prepare(
            "SELECT config_id, source_path, size_bytes, modified_at_unix_ms, unchanged_since_unix_ms, \
             observation_count, queued_job_id FROM watch_file_observations WHERE config_id=?1 AND source_path=?2",
        )?;
        let config = c_string(config_id, "watch-folder identity")?;
        let source = c_string(source_path, "watch-file path")?;
        bind_text(self.database, statement.raw, 1, &config)?;
        bind_text(self.database, statement.raw, 2, &source)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_watch_file_observation_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn write_schedule_item(&self, write: &ScheduleItemWrite) -> Result<(), String> {
        write
            .item
            .validate()
            .map_err(|error| format!("{error:?}"))?;
        if !secret_field(&write.changed_by, 200)
            || write.occurred_at_unix_ms <= 0
            || write.changed_by == "legacy-migration"
        {
            return Err("schedule item change attribution is invalid or unbounded".into());
        }
        if write.item.state != ScheduleState::Draft {
            return Err(
                "public schedule item writes require draft state; use the audited commit gate"
                    .into(),
            );
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = self.write_schedule_item_in_transaction(write);
        self.finish_transaction(outcome)
    }

    fn write_schedule_item_in_transaction(&self, write: &ScheduleItemWrite) -> Result<(), String> {
        let current = self.read_schedule_item(&write.item.item_id)?;
        match current {
            None if write.item.revision == 1 => self.insert_schedule_item(&write.item)?,
            Some(current) if current.revision.checked_add(1) == Some(write.item.revision) => {
                self.update_schedule_item(&write.item, current.revision)?;
            }
            _ => return Err("schedule item compare-and-set revision conflicts".into()),
        }
        self.append_schedule_item_history(&write.item, &write.changed_by, write.occurred_at_unix_ms)
    }

    fn insert_schedule_item(&self, item: &ScheduleItem) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        )?;
        let values = [
            c_string(&item.item_id, "schedule item identity")?,
            c_string(&item.channel_id, "channel identity")?,
            c_string(&item.asset_id, "asset identity")?,
            c_string(&item.title, "schedule title")?,
            c_string(schedule_state_name(item.state), "schedule state")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(item.revision, "schedule item revision")?,
        )?;
        for (index, value) in values[1..4].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 3) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 6, item.starts_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            checked_i64(item.duration_ms, "schedule duration")?,
        )?;
        bind_text(self.database, statement.raw, 8, &values[4])?;
        expect_done(self.database, statement.raw)
    }

    fn update_schedule_item(
        &self,
        item: &ScheduleItem,
        expected_revision: u64,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "UPDATE schedule_items SET revision=?1, channel_id=?2, asset_id=?3, title=?4, starts_at_unix_ms=?5, duration_ms=?6, state=?7 \
             WHERE item_id=?8 AND revision=?9",
        )?;
        let values = [
            c_string(&item.channel_id, "channel identity")?,
            c_string(&item.asset_id, "asset identity")?,
            c_string(&item.title, "schedule title")?,
            c_string(schedule_state_name(item.state), "schedule state")?,
            c_string(&item.item_id, "schedule item identity")?,
        ];
        bind_i64(
            self.database,
            statement.raw,
            1,
            checked_i64(item.revision, "schedule item revision")?,
        )?;
        bind_text(self.database, statement.raw, 2, &values[0])?;
        bind_text(self.database, statement.raw, 3, &values[1])?;
        bind_text(self.database, statement.raw, 4, &values[2])?;
        bind_i64(self.database, statement.raw, 5, item.starts_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            checked_i64(item.duration_ms, "schedule duration")?,
        )?;
        bind_text(self.database, statement.raw, 7, &values[3])?;
        bind_text(self.database, statement.raw, 8, &values[4])?;
        bind_i64(
            self.database,
            statement.raw,
            9,
            checked_i64(expected_revision, "schedule item expected revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("schedule item compare-and-set revision conflicts".into())
        }
    }

    fn append_schedule_item_history(
        &self,
        item: &ScheduleItem,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), String> {
        let record_json = serde_json::to_string(item)
            .map_err(|error| format!("serialize schedule item history: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO schedule_item_history(item_id, revision, record_json, changed_by, occurred_at_unix_ms) \
             VALUES(?1, ?2, ?3, ?4, ?5)",
        )?;
        let item_id = c_string(&item.item_id, "schedule item history identity")?;
        let record_json = c_string(&record_json, "schedule item history record")?;
        let changed_by = c_string(changed_by, "schedule item history actor")?;
        bind_text(self.database, statement.raw, 1, &item_id)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(item.revision, "schedule item history revision")?,
        )?;
        bind_text(self.database, statement.raw, 3, &record_json)?;
        bind_text(self.database, statement.raw, 4, &changed_by)?;
        bind_i64(self.database, statement.raw, 5, occurred_at_unix_ms)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_schedule_item(&self, item_id: &str) -> Result<Option<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE item_id=?1",
        )?;
        let item_id = c_string(item_id, "schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &item_id)?;
        let item = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Some(read_schedule_item_row(statement.raw)?),
            SQLITE_DONE => None,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        let Some(item) = item else {
            return Ok(None);
        };
        self.validate_schedule_item_current_history(&item)?;
        Ok(Some(item))
    }

    fn validate_schedule_item_current_history(&self, item: &ScheduleItem) -> Result<(), String> {
        if let Some(history) = self.read_latest_schedule_item_history(&item.item_id)?
            && history.item != *item
        {
            return Err("schedule item current row and latest history disagree".into());
        }
        Ok(())
    }

    fn validate_schedule_item_list_current_history(
        &self,
        items: Vec<ScheduleItem>,
    ) -> Result<Vec<ScheduleItem>, String> {
        for item in &items {
            self.validate_schedule_item_current_history(item)?;
        }
        Ok(items)
    }

    fn read_latest_schedule_item_history(
        &self,
        item_id: &str,
    ) -> Result<Option<ScheduleItemHistoryEntry>, String> {
        let statement = self.prepare(
            "SELECT item_id, revision, record_json, changed_by, occurred_at_unix_ms \
             FROM schedule_item_history WHERE item_id=?1 ORDER BY revision DESC LIMIT 1",
        )?;
        let item_id_value = c_string(item_id, "schedule item history identity")?;
        bind_text(self.database, statement.raw, 1, &item_id_value)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_schedule_item_history_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_schedule_item_history(
        &self,
        item_id: &str,
        limit: usize,
    ) -> Result<Vec<ScheduleItemHistoryEntry>, String> {
        if !is_canonical_uuid(item_id) || !(1..=128).contains(&limit) {
            return Err("schedule item history query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT item_id, revision, record_json, changed_by, occurred_at_unix_ms \
             FROM schedule_item_history WHERE item_id=?1 ORDER BY revision LIMIT ?2",
        )?;
        let item_id_value = c_string(item_id, "schedule item history identity")?;
        bind_text(self.database, statement.raw, 1, &item_id_value)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut history = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    history.push(read_schedule_item_history_row(statement.raw)?);
                }
                SQLITE_DONE => return Ok(history),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_channel_schedule(&self, channel_id: &str) -> Result<Vec<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE channel_id=?1 ORDER BY starts_at_unix_ms, item_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        drop(statement);
        self.validate_schedule_item_list_current_history(items)
    }

    pub fn list_schedule_window(
        &self,
        starts_at_unix_ms: i64,
        ends_before_unix_ms: i64,
    ) -> Result<Vec<ScheduleItem>, String> {
        if starts_at_unix_ms < 0 || ends_before_unix_ms <= starts_at_unix_ms {
            return Err("schedule window is invalid".into());
        }
        let statement = self.prepare(
            "SELECT item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE state='committed' AND starts_at_unix_ms>=?1 \
             AND starts_at_unix_ms<?2 ORDER BY starts_at_unix_ms, item_id",
        )?;
        bind_i64(self.database, statement.raw, 1, starts_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 2, ends_before_unix_ms)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        drop(statement);
        self.validate_schedule_item_list_current_history(items)
    }

    /// Returns a bounded, current-authority subset of committed schedule items
    /// whose starts are strictly after the supplied station time. Callers must
    /// supply a bounded time window; this method never enumerates the station's
    /// full schedule history.
    pub fn list_recording_schedule_candidates(
        &self,
        channel_id: &str,
        starts_at_or_after_unix_ms: i64,
        ends_before_unix_ms: i64,
        limit: usize,
    ) -> Result<Vec<ScheduleItem>, String> {
        const MAX_WINDOW_MS: i64 = 31 * 24 * 60 * 60 * 1_000;
        if !is_canonical_uuid(channel_id)
            || !(1..=128).contains(&limit)
            || starts_at_or_after_unix_ms < 0
            || ends_before_unix_ms <= starts_at_or_after_unix_ms
            || ends_before_unix_ms - starts_at_or_after_unix_ms > MAX_WINDOW_MS
        {
            return Err("recording schedule candidate query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE channel_id=?1 AND state='committed' \
             AND starts_at_unix_ms>?2 AND starts_at_unix_ms<?3 \
             ORDER BY starts_at_unix_ms, item_id LIMIT ?4",
        )?;
        let channel_id = c_string(channel_id, "recording schedule candidate channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_i64(self.database, statement.raw, 2, starts_at_or_after_unix_ms)?;
        bind_i64(self.database, statement.raw, 3, ends_before_unix_ms)?;
        bind_i64(self.database, statement.raw, 4, limit as i64)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        drop(statement);
        self.validate_schedule_item_list_current_history(items)
    }

    pub fn prepare_schedule_commit(
        &self,
        plan_id: &str,
        item_id: &str,
    ) -> Result<CommitPlan, CommitWriteError> {
        let item = self
            .read_schedule_item(item_id)
            .map_err(CommitWriteError::Storage)?
            .ok_or(CommitWriteError::NotFound)?;
        let mut asset = self
            .read_media_asset(&item.asset_id)
            .map_err(CommitWriteError::Storage)?;
        if let Some(asset) = asset.as_mut()
            && asset.readiness == AssetReadiness::Ready
            && !Path::new(&asset.media_path).is_file()
        {
            asset.readiness = AssetReadiness::Missing;
        }
        let channel_items = self
            .list_channel_schedule(&item.channel_id)
            .map_err(CommitWriteError::Storage)?;
        prepare_commit(plan_id, &item, asset.as_ref(), &channel_items)
            .map_err(|error| CommitWriteError::Storage(format!("{error:?}")))
    }

    pub fn commit_schedule(
        &self,
        report_id: &str,
        plan_id: &str,
        item_id: &str,
        approved_by: &str,
        approved_at_unix_ms: i64,
        operator_notes: &str,
    ) -> Result<CommitReport, CommitWriteError> {
        if report_id.trim().is_empty()
            || plan_id.trim().is_empty()
            || approved_by.trim().is_empty()
            || approved_by.chars().any(char::is_control)
            || operator_notes.len() > 4_000
            || operator_notes.chars().any(char::is_control)
            || approved_at_unix_ms < 0
        {
            return Err(CommitWriteError::Invalid(
                "commit identity, operator, timestamp, or notes are invalid",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CommitWriteError::Storage)?;
        let outcome = (|| {
            let plan = self.prepare_schedule_commit(plan_id, item_id)?;
            if !plan.dry_run_passed {
                return Err(CommitWriteError::GateFailed(Box::new(plan)));
            }
            let item = self
                .read_schedule_item(item_id)
                .map_err(CommitWriteError::Storage)?
                .ok_or(CommitWriteError::NotFound)?;
            let schedule_item_revision = item.revision.checked_add(1).ok_or_else(|| {
                CommitWriteError::Storage("schedule item revision overflow".into())
            })?;
            let report = CommitReport {
                report_id: report_id.to_string(),
                plan_id: plan_id.to_string(),
                channel_id: item.channel_id.clone(),
                schedule_item_id: item.item_id.clone(),
                schedule_item_revision,
                asset_id: item.asset_id.clone(),
                approved_by: approved_by.to_string(),
                approved_at_unix_ms,
                dispatch_status: DispatchStatus::Pending,
                operator_notes: operator_notes.to_string(),
            };
            self.insert_commit_report(&report)
                .map_err(CommitWriteError::Storage)?;
            let committed_item = ScheduleItem {
                revision: schedule_item_revision,
                state: ScheduleState::Committed,
                ..item
            };
            self.write_schedule_item_in_transaction(&ScheduleItemWrite {
                item: committed_item,
                changed_by: approved_by.to_string(),
                occurred_at_unix_ms: approved_at_unix_ms,
            })
            .map_err(CommitWriteError::Storage)?;
            Ok(report)
        })();
        match outcome {
            Ok(report) => match self.execute("COMMIT") {
                Ok(()) => Ok(report),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CommitWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_commit_report(&self, report_id: &str) -> Result<Option<CommitReport>, String> {
        let statement = self.prepare(
            "SELECT report_id, plan_id, channel_id, schedule_item_id, schedule_item_revision, \
             asset_id, approved_by, approved_at_unix_ms, dispatch_status, operator_notes \
             FROM commit_reports WHERE report_id=?1",
        )?;
        let report_id = c_string(report_id, "commit report identity")?;
        bind_text(self.database, statement.raw, 1, &report_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_commit_report_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_commit_reports(&self, channel_id: &str) -> Result<Vec<CommitReport>, String> {
        let statement = self.prepare(
            "SELECT report_id, plan_id, channel_id, schedule_item_id, schedule_item_revision, \
             asset_id, approved_by, approved_at_unix_ms, dispatch_status, operator_notes FROM commit_reports \
             WHERE channel_id=?1 ORDER BY approved_at_unix_ms, report_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut reports = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => reports.push(read_commit_report_row(statement.raw)?),
                SQLITE_DONE => return Ok(reports),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn append_external_receiver_acceptance_receipt(
        &self,
        receipt: &ExternalReceiverAcceptanceReceipt,
    ) -> Result<(), String> {
        receipt.validate()?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let channel = self
                .list_channels()?
                .into_iter()
                .find(|channel| channel.channel_id == receipt.channel_id)
                .ok_or_else(|| "external receiver receipt channel is not configured".to_string())?;
            if !channel
                .output_sinks
                .iter()
                .any(|sink| sink.sink_id == receipt.sink_id && sink.enabled)
            {
                return Err(
                    "external receiver receipt requires a currently configured enabled sink".into(),
                );
            }
            let current_hash =
                self.current_channel_output_configuration_hash(&receipt.channel_id)?;
            if receipt.configuration_hash != current_hash {
                return Err(
                    "external receiver receipt configuration does not match the current channel output"
                        .into(),
                );
            }

            let statement = self.prepare(
                "INSERT INTO external_receiver_acceptance_receipts(\
               receipt_id, channel_id, sink_id, outcome, method, receiver_label, evidence_reference, detail, \
               observed_at_unix_ms, recorded_at_unix_ms, recorded_by, candidate_version, source_commit, configuration_hash\
             ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)",
            )?;
            let text_values = [
                (&receipt.receipt_id, "external receiver receipt identity"),
                (&receipt.channel_id, "external receiver receipt channel"),
                (&receipt.sink_id, "external receiver receipt sink"),
            ]
            .into_iter()
            .map(|(value, field)| c_string(value, field))
            .collect::<Result<Vec<_>, _>>()?;
            let outcome = c_string(
                receiver_acceptance_outcome_name(receipt.outcome),
                "receiver outcome",
            )?;
            let method = c_string(
                receiver_evidence_method_name(receipt.method),
                "receiver method",
            )?;
            let receiver_label = c_string(&receipt.receiver_label, "receiver label")?;
            let evidence_reference =
                c_string(&receipt.evidence_reference, "receiver evidence reference")?;
            let detail = c_string(&receipt.detail, "receiver evidence detail")?;
            let recorded_by = c_string(&receipt.recorded_by, "receiver receipt actor")?;
            let candidate_version =
                c_string(&receipt.candidate_version, "receiver candidate version")?;
            let source_commit = c_string(&receipt.source_commit, "receiver source commit")?;
            let configuration_hash =
                c_string(&receipt.configuration_hash, "receiver configuration hash")?;
            bind_text(self.database, statement.raw, 1, &text_values[0])?;
            bind_text(self.database, statement.raw, 2, &text_values[1])?;
            bind_text(self.database, statement.raw, 3, &text_values[2])?;
            bind_text(self.database, statement.raw, 4, &outcome)?;
            bind_text(self.database, statement.raw, 5, &method)?;
            bind_text(self.database, statement.raw, 6, &receiver_label)?;
            bind_text(self.database, statement.raw, 7, &evidence_reference)?;
            bind_text(self.database, statement.raw, 8, &detail)?;
            bind_i64(self.database, statement.raw, 9, receipt.observed_at_unix_ms)?;
            bind_i64(
                self.database,
                statement.raw,
                10,
                receipt.recorded_at_unix_ms,
            )?;
            bind_text(self.database, statement.raw, 11, &recorded_by)?;
            bind_text(self.database, statement.raw, 12, &candidate_version)?;
            bind_text(self.database, statement.raw, 13, &source_commit)?;
            bind_text(self.database, statement.raw, 14, &configuration_hash)?;
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_DONE => Ok(()),
                _ => Err(sqlite_error(self.database)),
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn read_current_external_receiver_acceptance_receipt(
        &self,
        channel_id: &str,
        sink_id: &str,
        candidate_version: &str,
        source_commit: &str,
    ) -> Result<Option<ExternalReceiverAcceptanceReceipt>, String> {
        if !is_canonical_uuid(channel_id)
            || !is_canonical_uuid(sink_id)
            || !secret_field(candidate_version, 200)
            || !is_source_commit_identity(source_commit)
        {
            return Err("current external receiver receipt lookup is invalid".into());
        }
        let channel = self
            .list_channels()?
            .into_iter()
            .find(|channel| channel.channel_id == channel_id);
        let Some(channel) = channel else {
            return Ok(None);
        };
        if !channel
            .output_sinks
            .iter()
            .any(|sink| sink.sink_id == sink_id && sink.enabled)
        {
            return Ok(None);
        }
        let configuration_hash = self.current_channel_output_configuration_hash(channel_id)?;
        let statement = self.prepare(
            "SELECT receipt_id, channel_id, sink_id, outcome, method, receiver_label, evidence_reference, detail, \
                    observed_at_unix_ms, recorded_at_unix_ms, recorded_by, candidate_version, source_commit, configuration_hash \
             FROM external_receiver_acceptance_receipts \
             WHERE channel_id=?1 AND sink_id=?2 AND candidate_version=?3 AND source_commit=?4 AND configuration_hash=?5 \
             ORDER BY recorded_at_unix_ms DESC, rowid DESC LIMIT 1",
        )?;
        let channel_id = c_string(channel_id, "external receiver channel identity")?;
        let sink_id = c_string(sink_id, "external receiver sink identity")?;
        let candidate_version = c_string(candidate_version, "external receiver candidate version")?;
        let source_commit = c_string(source_commit, "external receiver source commit")?;
        let configuration_hash =
            c_string(&configuration_hash, "external receiver configuration hash")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_text(self.database, statement.raw, 2, &sink_id)?;
        bind_text(self.database, statement.raw, 3, &candidate_version)?;
        bind_text(self.database, statement.raw, 4, &source_commit)?;
        bind_text(self.database, statement.raw, 5, &configuration_hash)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_external_receiver_acceptance_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_external_receiver_acceptance_receipts(
        &self,
        channel_id: &str,
        sink_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<ExternalReceiverAcceptanceReceipt>, String> {
        if !is_canonical_uuid(channel_id)
            || sink_id.is_some_and(|value| !is_canonical_uuid(value))
            || !(1..=1_000).contains(&limit)
        {
            return Err("external receiver receipt query is invalid or unbounded".into());
        }
        let sql = if sink_id.is_some() {
            "SELECT receipt_id, channel_id, sink_id, outcome, method, receiver_label, evidence_reference, detail, \
                    observed_at_unix_ms, recorded_at_unix_ms, recorded_by, candidate_version, source_commit, configuration_hash \
             FROM external_receiver_acceptance_receipts WHERE channel_id=?1 AND sink_id=?2 \
             ORDER BY recorded_at_unix_ms DESC, rowid DESC LIMIT ?3"
        } else {
            "SELECT receipt_id, channel_id, sink_id, outcome, method, receiver_label, evidence_reference, detail, \
                    observed_at_unix_ms, recorded_at_unix_ms, recorded_by, candidate_version, source_commit, configuration_hash \
             FROM external_receiver_acceptance_receipts WHERE channel_id=?1 \
             ORDER BY recorded_at_unix_ms DESC, rowid DESC LIMIT ?2"
        };
        let statement = self.prepare(sql)?;
        let channel_id = c_string(channel_id, "external receiver channel identity")?;
        let sink_id = sink_id
            .map(|value| c_string(value, "external receiver sink identity"))
            .transpose()?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        if let Some(sink_id) = &sink_id {
            bind_text(self.database, statement.raw, 2, sink_id)?;
            bind_i64(self.database, statement.raw, 3, limit as i64)?;
        } else {
            bind_i64(self.database, statement.raw, 2, limit as i64)?;
        }
        let mut receipts = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => receipts.push(read_external_receiver_acceptance_row(statement.raw)?),
                SQLITE_DONE => return Ok(receipts),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn append_commissioning_support_bundle_receipt(
        &self,
        receipt: &CommissioningSupportBundleReceipt,
    ) -> Result<(), String> {
        receipt.validate()?;
        let report = self
            .read_commit_report(&receipt.report_id)?
            .ok_or_else(|| {
                "commissioning support bundle requires an existing commit report".to_string()
            })?;
        if report.dispatch_status != DispatchStatus::Completed {
            return Err("commissioning support bundle requires a completed commit report".into());
        }

        let statement = self.prepare(
            "INSERT INTO commissioning_support_bundle_receipts(\
               bundle_id, report_id, archive_sha256, archive_size_bytes, created_at_unix_ms, created_by\
             ) VALUES(?1, ?2, ?3, ?4, ?5, ?6)",
        )?;
        let bundle_id = c_string(&receipt.bundle_id, "support bundle identity")?;
        let report_id = c_string(&receipt.report_id, "commit report identity")?;
        let archive_sha256 = c_string(&receipt.archive_sha256, "support bundle hash")?;
        let created_by = c_string(&receipt.created_by, "support bundle actor")?;
        bind_text(self.database, statement.raw, 1, &bundle_id)?;
        bind_text(self.database, statement.raw, 2, &report_id)?;
        bind_text(self.database, statement.raw, 3, &archive_sha256)?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            i64::try_from(receipt.archive_size_bytes)
                .map_err(|_| "support bundle size is invalid".to_string())?,
        )?;
        bind_i64(self.database, statement.raw, 5, receipt.created_at_unix_ms)?;
        bind_text(self.database, statement.raw, 6, &created_by)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(()),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_commissioning_support_bundle_receipts(
        &self,
        limit: usize,
    ) -> Result<Vec<CommissioningSupportBundleReceipt>, String> {
        if !(1..=100).contains(&limit) {
            return Err(
                "commissioning support bundle receipt limit must be between 1 and 100".into(),
            );
        }
        let statement = self.prepare(
            "SELECT bundle_id, report_id, archive_sha256, archive_size_bytes, created_at_unix_ms, created_by \
             FROM commissioning_support_bundle_receipts \
             ORDER BY created_at_unix_ms DESC, bundle_id DESC LIMIT ?1",
        )?;
        bind_i64(
            self.database,
            statement.raw,
            1,
            i64::try_from(limit).map_err(|_| "receipt limit is invalid".to_string())?,
        )?;
        let mut receipts = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let receipt = CommissioningSupportBundleReceipt {
                        bundle_id: column_text(statement.raw, 0)?,
                        report_id: column_text(statement.raw, 1)?,
                        archive_sha256: column_text(statement.raw, 2)?,
                        archive_size_bytes: column_u64(statement.raw, 3, "support bundle size")?,
                        created_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 4) },
                        created_by: column_text(statement.raw, 5)?,
                    };
                    receipt.validate()?;
                    receipts.push(receipt);
                }
                SQLITE_DONE => return Ok(receipts),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_dispatch_jobs(&self, channel_id: &str) -> Result<Vec<DispatchJob>, String> {
        let statement = self.prepare(
            "SELECT r.report_id, r.plan_id, r.channel_id, r.schedule_item_id, r.schedule_item_revision, \
             r.asset_id, r.approved_by, r.approved_at_unix_ms, r.dispatch_status, r.operator_notes, \
             s.item_id, s.revision, s.channel_id, s.asset_id, s.title, s.starts_at_unix_ms, s.duration_ms, s.state, \
             a.asset_id, a.media_path, a.duration_ms, a.readiness \
             FROM commit_reports r JOIN schedule_items s ON s.item_id=r.schedule_item_id \
             JOIN media_assets a ON a.asset_id=r.asset_id WHERE r.channel_id=?1 \
             AND r.dispatch_status IN ('pending','queued','acknowledged') \
             ORDER BY s.starts_at_unix_ms, r.approved_at_unix_ms, r.report_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let report = read_commit_report_row(statement.raw)?;
                    let item = ScheduleItem {
                        item_id: column_text(statement.raw, 10)?,
                        revision: column_u64(statement.raw, 11, "schedule item revision")?,
                        channel_id: column_text(statement.raw, 12)?,
                        asset_id: column_text(statement.raw, 13)?,
                        title: column_text(statement.raw, 14)?,
                        starts_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 15) },
                        duration_ms: column_u64(statement.raw, 16, "schedule duration")?,
                        state: parse_schedule_state(&column_text(statement.raw, 17)?)?,
                    };
                    if report.schedule_item_revision != item.revision {
                        return Err(format!(
                            "commit report {} is bound to stale schedule item revision {} instead of {}",
                            report.report_id, report.schedule_item_revision, item.revision
                        ));
                    }
                    jobs.push(DispatchJob {
                        report,
                        item,
                        asset: MediaAsset {
                            asset_id: column_text(statement.raw, 18)?,
                            media_path: column_text(statement.raw, 19)?,
                            duration_ms: column_u64(statement.raw, 20, "asset duration")?,
                            readiness: parse_readiness(&column_text(statement.raw, 21)?)?,
                        },
                    });
                }
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn advance_dispatch_status(
        &self,
        report_id: &str,
        expected: DispatchStatus,
        next: DispatchStatus,
    ) -> Result<bool, String> {
        if !valid_dispatch_transition(expected, next) {
            return Err("invalid dispatch status transition".into());
        }
        let statement = self.prepare(
            "UPDATE commit_reports SET dispatch_status=?1 WHERE report_id=?2 AND dispatch_status=?3",
        )?;
        let next = c_string(dispatch_status_name(next), "next dispatch status")?;
        let report_id = c_string(report_id, "report identity")?;
        let expected = c_string(dispatch_status_name(expected), "expected dispatch status")?;
        bind_text(self.database, statement.raw, 1, &next)?;
        bind_text(self.database, statement.raw, 2, &report_id)?;
        bind_text(self.database, statement.raw, 3, &expected)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    fn insert_commit_report(&self, report: &CommitReport) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO commit_reports(report_id, plan_id, channel_id, schedule_item_id, schedule_item_revision, \
             asset_id, approved_by, approved_at_unix_ms, dispatch_status, operator_notes) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        )?;
        let report_id = c_string(&report.report_id, "report identity")?;
        let plan_id = c_string(&report.plan_id, "plan identity")?;
        let channel_id = c_string(&report.channel_id, "channel identity")?;
        let schedule_item_id = c_string(&report.schedule_item_id, "schedule item identity")?;
        let asset_id = c_string(&report.asset_id, "asset identity")?;
        let approved_by = c_string(&report.approved_by, "operator identity")?;
        let dispatch_status = c_string(
            dispatch_status_name(report.dispatch_status),
            "dispatch status",
        )?;
        let operator_notes = c_string(&report.operator_notes, "operator notes")?;
        bind_text(self.database, statement.raw, 1, &report_id)?;
        bind_text(self.database, statement.raw, 2, &plan_id)?;
        bind_text(self.database, statement.raw, 3, &channel_id)?;
        bind_text(self.database, statement.raw, 4, &schedule_item_id)?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            checked_i64(
                report.schedule_item_revision,
                "commit report schedule item revision",
            )?,
        )?;
        bind_text(self.database, statement.raw, 6, &asset_id)?;
        bind_text(self.database, statement.raw, 7, &approved_by)?;
        bind_i64(self.database, statement.raw, 8, report.approved_at_unix_ms)?;
        bind_text(self.database, statement.raw, 9, &dispatch_status)?;
        bind_text(self.database, statement.raw, 10, &operator_notes)?;
        expect_done(self.database, statement.raw)
    }

    fn execute(&self, sql: &str) -> Result<(), String> {
        let sql = CString::new(sql).map_err(|_| "SQL contains a null byte".to_string())?;
        // SAFETY: the database handle is live, SQL is a valid C string, and no callback is used.
        let status = unsafe {
            sqlite3_exec(
                self.database,
                sql.as_ptr(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
            )
        };
        if status == SQLITE_OK {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn initialize_reference_station_tier_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS reference_station_tier_selection (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               catalog_version INTEGER NOT NULL CHECK (catalog_version >= 0),\
               tier TEXT NOT NULL CHECK (tier IN (\
                 'tier_0_batch_stream','tier_1_streaming','development_validation','cable_sdi','tier_2_multi_stream')),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               updated_by TEXT NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 200)\
             );\
             CREATE TABLE IF NOT EXISTS reference_station_tier_audit (\
               audit_id INTEGER PRIMARY KEY AUTOINCREMENT,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               catalog_version INTEGER NOT NULL CHECK (catalog_version >= 0),\
               tier TEXT NOT NULL CHECK (tier IN (\
                 'tier_0_batch_stream','tier_1_streaming','development_validation','cable_sdi','tier_2_multi_stream')),\
               actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               CHECK (actor = trim(actor))\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS reference_station_tier_audit_revision \
               ON reference_station_tier_audit(revision);\
             CREATE INDEX IF NOT EXISTS reference_station_tier_audit_time \
               ON reference_station_tier_audit(occurred_at_unix_ms, audit_id);\
             CREATE TRIGGER IF NOT EXISTS reference_station_tier_audit_no_update \
               BEFORE UPDATE ON reference_station_tier_audit \
               BEGIN SELECT RAISE(ABORT, 'reference station tier audit is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS reference_station_tier_audit_no_delete \
               BEFORE DELETE ON reference_station_tier_audit \
               BEGIN SELECT RAISE(ABORT, 'reference station tier audit is append-only'); END;",
        )
    }

    fn initialize_cg_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS cg_boards (\
               board_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL UNIQUE,\
               template_id TEXT NOT NULL,\
               active INTEGER NOT NULL CHECK (active IN (0,1)),\
               background_audio_asset_id TEXT NOT NULL DEFAULT '',\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               created_by TEXT NOT NULL,\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS cg_zone_configs (\
               zone_id TEXT PRIMARY KEY,\
               board_id TEXT NOT NULL,\
               template_zone_key TEXT NOT NULL,\
               zone_kind TEXT NOT NULL,\
               content_source_json TEXT NOT NULL,\
               rotation_seconds INTEGER NOT NULL DEFAULT -1 CHECK (rotation_seconds BETWEEN -1 AND 3600),\
               approval_required INTEGER NOT NULL CHECK (approval_required IN (0,1)),\
               allowed_tags_json TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               UNIQUE(board_id, template_zone_key),\
               FOREIGN KEY(board_id) REFERENCES cg_boards(board_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS cg_feed_sources (\
               feed_source_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
             kind TEXT NOT NULL CHECK (kind IN ('rss','ical','caldav','weather','social')),\
             label TEXT NOT NULL,\
             source_url TEXT NOT NULL,\
             trust_tier TEXT NOT NULL CHECK (trust_tier IN ('station','trusted','moderated')),\
             tags_json TEXT NOT NULL DEFAULT '[]',\
             refresh_seconds INTEGER NOT NULL CHECK (refresh_seconds BETWEEN 60 AND 86400),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               created_by TEXT NOT NULL,\
               last_fetched_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (last_fetched_at_unix_ms >= -1),\
               last_fetch_error TEXT NOT NULL DEFAULT '',\
               UNIQUE(channel_id, label COLLATE NOCASE),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS cg_bulletins (\
               bulletin_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               content_json TEXT NOT NULL,\
               state TEXT NOT NULL CHECK (state IN ('submitted','needs_changes','accepted','scheduled','declined','expired')),\
               tags_json TEXT NOT NULL,\
               requested_start_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (requested_start_at_unix_ms >= -1),\
               requested_end_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (requested_end_at_unix_ms >= -1),\
               display_seconds INTEGER NOT NULL CHECK (display_seconds BETWEEN 3 AND 300),\
               audio_asset_id TEXT NOT NULL DEFAULT '',\
               submitted_by TEXT NOT NULL,\
               moderation_notes TEXT NOT NULL DEFAULT '',\
               moderated_by TEXT NOT NULL DEFAULT '',\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE INDEX IF NOT EXISTS cg_bulletins_channel_state_time \
               ON cg_bulletins(channel_id, state, requested_start_at_unix_ms, requested_end_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS cg_feed_item_approvals (\
               feed_source_id TEXT NOT NULL,\
               item_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL CHECK (approved_at_unix_ms >= 0),\
               PRIMARY KEY(feed_source_id, item_id),\
               FOREIGN KEY(feed_source_id) REFERENCES cg_feed_sources(feed_source_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS cg_audit_events (\
               audit_id TEXT PRIMARY KEY,\
               board_id TEXT NOT NULL DEFAULT '',\
               channel_id TEXT NOT NULL,\
               event_kind TEXT NOT NULL,\
               operator_id TEXT NOT NULL,\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               details_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS cg_audit_events_channel_time \
               ON cg_audit_events(channel_id, occurred_at_unix_ms DESC, audit_id);",
        )?;
        self.execute(
            "CREATE TABLE IF NOT EXISTS cg_image_assets (\
               asset_id TEXT PRIMARY KEY,\
               mime_type TEXT NOT NULL CHECK (mime_type IN ('image/png','image/jpeg')),\
               byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 8388608),\
               width INTEGER NOT NULL CHECK (width BETWEEN 1 AND 4096),\
               height INTEGER NOT NULL CHECK (height BETWEEN 1 AND 4096),\
               readiness TEXT NOT NULL CHECK (readiness IN ('ready','missing','rejected','processing'))\
             );\
             CREATE INDEX IF NOT EXISTS cg_image_assets_readiness ON cg_image_assets(readiness, asset_id);",
        )
    }

    fn initialize_fallback_slate_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS channel_fallback_slates (\
               channel_id TEXT PRIMARY KEY,\
               image_asset_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               updated_by TEXT NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 200),\
               CHECK (updated_by = trim(updated_by)),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id),\
               FOREIGN KEY(image_asset_id) REFERENCES cg_image_assets(asset_id)\
             );\
             CREATE TABLE IF NOT EXISTS channel_fallback_slate_audit (\
               audit_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               image_asset_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               CHECK (actor = trim(actor)),\
               UNIQUE(channel_id, revision),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id),\
               FOREIGN KEY(image_asset_id) REFERENCES cg_image_assets(asset_id)\
             );\
             CREATE INDEX IF NOT EXISTS channel_fallback_slate_audit_time \
               ON channel_fallback_slate_audit(channel_id, occurred_at_unix_ms, audit_id);\
             CREATE TRIGGER IF NOT EXISTS channel_fallback_slate_audit_no_update \
               BEFORE UPDATE ON channel_fallback_slate_audit \
               BEGIN SELECT RAISE(ABORT, 'channel fallback slate audit is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS channel_fallback_slate_audit_no_delete \
               BEFORE DELETE ON channel_fallback_slate_audit \
               BEGIN SELECT RAISE(ABORT, 'channel fallback slate audit is append-only'); END;",
        )
    }

    fn initialize_caption_policy_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS channel_caption_policies (\
               channel_id TEXT PRIMARY KEY,\
               mode TEXT NOT NULL CHECK (mode IN ('disabled','cea708')),\
               service_number INTEGER NOT NULL CHECK (service_number BETWEEN 1 AND 63),\
               language TEXT NOT NULL CHECK (length(language) BETWEEN 2 AND 35),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               updated_by TEXT NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 200),\
               CHECK (updated_by = trim(updated_by)),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id)\
             );\
             CREATE TABLE IF NOT EXISTS channel_caption_policy_audit (\
               audit_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               mode TEXT NOT NULL CHECK (mode IN ('disabled','cea708')),\
               service_number INTEGER NOT NULL CHECK (service_number BETWEEN 1 AND 63),\
               language TEXT NOT NULL CHECK (length(language) BETWEEN 2 AND 35),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               CHECK (actor = trim(actor)),\
               UNIQUE(channel_id, revision),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id)\
             );\
             CREATE INDEX IF NOT EXISTS channel_caption_policy_audit_time \
               ON channel_caption_policy_audit(channel_id, occurred_at_unix_ms, audit_id);\
             CREATE TRIGGER IF NOT EXISTS channel_caption_policy_audit_no_update \
               BEFORE UPDATE ON channel_caption_policy_audit \
               BEGIN SELECT RAISE(ABORT, 'channel caption policy audit is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS channel_caption_policy_audit_no_delete \
               BEFORE DELETE ON channel_caption_policy_audit \
               BEGIN SELECT RAISE(ABORT, 'channel caption policy audit is append-only'); END;",
        )
    }

    fn initialize_asset_caption_track_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS asset_caption_tracks (\
               track_id TEXT PRIMARY KEY,\
               asset_id TEXT NOT NULL,\
               language TEXT NOT NULL CHECK (length(language) BETWEEN 2 AND 35),\
               service_number INTEGER NOT NULL CHECK (service_number BETWEEN 1 AND 63),\
               source_kind TEXT NOT NULL CHECK (source_kind IN ('embedded','sidecar','generated','external')),\
               label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               cue_count INTEGER NOT NULL CHECK (cue_count >= 1),\
               cues_sha256 TEXT NOT NULL CHECK (length(cues_sha256) = 64),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               updated_by TEXT NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 200),\
               CHECK (label = trim(label)),\
               CHECK (updated_by = trim(updated_by)),\
               UNIQUE(asset_id, language, service_number),\
               FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id)\
             );\
             CREATE TABLE IF NOT EXISTS asset_caption_cues (\
               track_id TEXT NOT NULL,\
               cue_ordinal INTEGER NOT NULL CHECK (cue_ordinal >= 0),\
               cue_id TEXT NOT NULL,\
               start_ms INTEGER NOT NULL CHECK (start_ms >= 0),\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               cue_text TEXT NOT NULL CHECK (length(cue_text) BETWEEN 1 AND 2048),\
               PRIMARY KEY(track_id, cue_ordinal),\
               UNIQUE(track_id, cue_id),\
               FOREIGN KEY(track_id) REFERENCES asset_caption_tracks(track_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS asset_caption_track_revisions (\
               audit_id TEXT PRIMARY KEY,\
               track_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               language TEXT NOT NULL CHECK (length(language) BETWEEN 2 AND 35),\
               service_number INTEGER NOT NULL CHECK (service_number BETWEEN 1 AND 63),\
               source_kind TEXT NOT NULL CHECK (source_kind IN ('embedded','sidecar','generated','external')),\
               label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               cue_count INTEGER NOT NULL CHECK (cue_count >= 1),\
               cues_sha256 TEXT NOT NULL CHECK (length(cues_sha256) = 64),\
               actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               CHECK (label = trim(label)),\
               CHECK (actor = trim(actor)),\
               UNIQUE(track_id, revision),\
               FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id)\
             );\
             CREATE TABLE IF NOT EXISTS asset_caption_track_revision_cues (\
               audit_id TEXT NOT NULL,\
               cue_ordinal INTEGER NOT NULL CHECK (cue_ordinal >= 0),\
               cue_id TEXT NOT NULL,\
               start_ms INTEGER NOT NULL CHECK (start_ms >= 0),\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               cue_text TEXT NOT NULL CHECK (length(cue_text) BETWEEN 1 AND 2048),\
               PRIMARY KEY(audit_id, cue_ordinal),\
               UNIQUE(audit_id, cue_id),\
               FOREIGN KEY(audit_id) REFERENCES asset_caption_track_revisions(audit_id)\
             );\
             CREATE INDEX IF NOT EXISTS asset_caption_tracks_asset \
               ON asset_caption_tracks(asset_id, service_number, language);\
             CREATE INDEX IF NOT EXISTS asset_caption_track_revisions_time \
               ON asset_caption_track_revisions(track_id, revision, audit_id);\
             CREATE TRIGGER IF NOT EXISTS asset_caption_track_revisions_no_update \
               BEFORE UPDATE ON asset_caption_track_revisions \
               BEGIN SELECT RAISE(ABORT, 'asset caption track revisions are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_caption_track_revisions_no_delete \
               BEFORE DELETE ON asset_caption_track_revisions \
               BEGIN SELECT RAISE(ABORT, 'asset caption track revisions are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_caption_track_revision_cues_no_update \
               BEFORE UPDATE ON asset_caption_track_revision_cues \
               BEGIN SELECT RAISE(ABORT, 'asset caption revision cues are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_caption_track_revision_cues_no_delete \
               BEFORE DELETE ON asset_caption_track_revision_cues \
               BEGIN SELECT RAISE(ABORT, 'asset caption revision cues are append-only'); END;",
        )
    }

    fn initialize_caption_egress_proof_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS caption_egress_proofs (\
               proof_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               report_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               track_id TEXT NOT NULL,\
               track_revision INTEGER NOT NULL CHECK (track_revision >= 1),\
               caption_policy_revision INTEGER NOT NULL CHECK (caption_policy_revision >= 1),\
               cues_sha256 TEXT NOT NULL CHECK (length(cues_sha256) = 64),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL CHECK (length(source_commit) = 40),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash) = 64),\
               outcome TEXT NOT NULL CHECK (outcome IN ('verified','mismatch')),\
               captured_at_unix_ms INTEGER NOT NULL CHECK (captured_at_unix_ms >= 0),\
               record_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS caption_egress_proofs_channel_time \
               ON caption_egress_proofs(channel_id, captured_at_unix_ms DESC, proof_id DESC);\
             CREATE TRIGGER IF NOT EXISTS caption_egress_proofs_no_update \
               BEFORE UPDATE ON caption_egress_proofs \
               BEGIN SELECT RAISE(ABORT, 'caption egress proofs are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS caption_egress_proofs_no_delete \
               BEFORE DELETE ON caption_egress_proofs \
               BEGIN SELECT RAISE(ABORT, 'caption egress proofs are append-only'); END;",
        )
    }

    fn initialize_caption_egress_job_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS caption_egress_jobs (\
               job_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               state TEXT NOT NULL CHECK (state IN ('pending','claimed','capture_pending','analyzing','completed','failed')),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               lease_expires_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (lease_expires_at_unix_ms >= -1),\
               job_json TEXT NOT NULL,\
               capture_json TEXT NOT NULL DEFAULT '',\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               failed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (failed_at_unix_ms >= -1),\
               failure_detail TEXT NOT NULL DEFAULT ''\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS caption_egress_jobs_active_sink \
               ON caption_egress_jobs(channel_id, sink_id) WHERE state NOT IN ('completed','failed');\
             CREATE INDEX IF NOT EXISTS caption_egress_jobs_state_time \
               ON caption_egress_jobs(state, created_at_unix_ms, job_id);",
        )
    }

    fn migrate_caption_egress_history_sink_foreign_keys(&self) -> Result<(), String> {
        let proof_has_mutable_sink_fk =
            self.caption_egress_history_has_mutable_sink_fk("caption_egress_proofs")?;
        let job_has_mutable_sink_fk =
            self.caption_egress_history_has_mutable_sink_fk("caption_egress_jobs")?;
        if !proof_has_mutable_sink_fk && !job_has_mutable_sink_fk {
            return Ok(());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if proof_has_mutable_sink_fk {
                self.execute(
                    "CREATE TABLE caption_egress_proofs_replacement (\
                       proof_id TEXT PRIMARY KEY,\
                       channel_id TEXT NOT NULL,\
                       sink_id TEXT NOT NULL,\
                       report_id TEXT NOT NULL,\
                       asset_id TEXT NOT NULL,\
                       track_id TEXT NOT NULL,\
                       track_revision INTEGER NOT NULL CHECK (track_revision >= 1),\
                       caption_policy_revision INTEGER NOT NULL CHECK (caption_policy_revision >= 1),\
                       cues_sha256 TEXT NOT NULL CHECK (length(cues_sha256) = 64),\
                       candidate_version TEXT NOT NULL,\
                       source_commit TEXT NOT NULL CHECK (length(source_commit) = 40),\
                       configuration_hash TEXT NOT NULL CHECK (length(configuration_hash) = 64),\
                       outcome TEXT NOT NULL CHECK (outcome IN ('verified','mismatch')),\
                       captured_at_unix_ms INTEGER NOT NULL CHECK (captured_at_unix_ms >= 0),\
                       record_json TEXT NOT NULL\
                     );\
                     INSERT INTO caption_egress_proofs_replacement \
                     SELECT * FROM caption_egress_proofs;",
                )
                .map_err(|message| format!("replace legacy caption proof history: {message}"))?;
                self.execute(
                    "DROP TRIGGER IF EXISTS caption_egress_proofs_no_update;\
                     DROP TRIGGER IF EXISTS caption_egress_proofs_no_delete;\
                     DROP INDEX IF EXISTS caption_egress_proofs_channel_time;\
                     DROP TABLE caption_egress_proofs;\
                     ALTER TABLE caption_egress_proofs_replacement RENAME TO caption_egress_proofs;",
                )
                .map_err(|message| format!("install migrated caption proof history: {message}"))?;
                self.initialize_caption_egress_proof_schema()
                    .map_err(|message| {
                        format!("restore migrated caption proof history indexes: {message}")
                    })?;
            }
            if job_has_mutable_sink_fk {
                self.execute(
                    "CREATE TABLE caption_egress_jobs_replacement (\
                       job_id TEXT PRIMARY KEY,\
                       channel_id TEXT NOT NULL,\
                       sink_id TEXT NOT NULL,\
                       state TEXT NOT NULL CHECK (state IN ('pending','claimed','capture_pending','analyzing','completed','failed')),\
                       created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
                       updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
                       lease_expires_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (lease_expires_at_unix_ms >= -1),\
                       job_json TEXT NOT NULL,\
                       capture_json TEXT NOT NULL DEFAULT '',\
                       completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
                       failed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (failed_at_unix_ms >= -1),\
                       failure_detail TEXT NOT NULL DEFAULT ''\
                     );\
                     INSERT INTO caption_egress_jobs_replacement \
                     SELECT * FROM caption_egress_jobs;\
                     DROP INDEX IF EXISTS caption_egress_jobs_active_sink;\
                     DROP INDEX IF EXISTS caption_egress_jobs_state_time;\
                     DROP TABLE caption_egress_jobs;\
                     ALTER TABLE caption_egress_jobs_replacement RENAME TO caption_egress_jobs;",
                )
                .map_err(|message| format!("replace legacy caption proof job history: {message}"))?;
                self.initialize_caption_egress_job_schema()
                    .map_err(|message| {
                        format!("restore migrated caption proof job indexes: {message}")
                    })?;
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    fn caption_egress_history_has_mutable_sink_fk(&self, table: &str) -> Result<bool, String> {
        let statement =
            self.prepare("SELECT sql FROM sqlite_master WHERE type='table' AND name=?1")?;
        let table = c_string(table, "caption egress history table")?;
        bind_text(self.database, statement.raw, 1, &table)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(column_text(statement.raw, 0)?
                .contains("REFERENCES channel_output_sinks(channel_id, sink_id)")),
            SQLITE_DONE => Err("caption egress history schema is missing".into()),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn migrate_cg_feed_cache_schema(&self) -> Result<(), String> {
        let statement = self.prepare("PRAGMA table_info(cg_feed_sources)")?;
        let mut has_tags = false;
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    if column_text(statement.raw, 1)? == "tags_json" {
                        has_tags = true;
                    }
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        drop(statement);
        if !has_tags {
            self.execute(
                "ALTER TABLE cg_feed_sources ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'",
            )?;
        }
        self.execute(
            r#"CREATE TABLE IF NOT EXISTS cg_feed_cache_meta (
               feed_source_id TEXT PRIMARY KEY,
               fetched_at_unix_ms INTEGER NOT NULL CHECK (fetched_at_unix_ms >= 0),
               expires_at_unix_ms INTEGER NOT NULL CHECK (expires_at_unix_ms >= fetched_at_unix_ms),
               etag TEXT NOT NULL DEFAULT '',
               last_modified TEXT NOT NULL DEFAULT '',
               last_error TEXT NOT NULL DEFAULT '',
               FOREIGN KEY(feed_source_id) REFERENCES cg_feed_sources(feed_source_id) ON DELETE CASCADE
             );
             CREATE TABLE IF NOT EXISTS cg_feed_cache_items (
               feed_source_id TEXT NOT NULL,
               item_id TEXT NOT NULL,
               title TEXT NOT NULL,
               summary TEXT NOT NULL DEFAULT '',
               starts_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (starts_at_unix_ms >= -1),
               url TEXT NOT NULL DEFAULT '',
               tags_json TEXT NOT NULL DEFAULT '[]',
               fetched_at_unix_ms INTEGER NOT NULL CHECK (fetched_at_unix_ms >= 0),
               PRIMARY KEY(feed_source_id, item_id),
               FOREIGN KEY(feed_source_id) REFERENCES cg_feed_sources(feed_source_id) ON DELETE CASCADE
             );
             CREATE INDEX IF NOT EXISTS cg_feed_cache_items_source_start
               ON cg_feed_cache_items(feed_source_id, starts_at_unix_ms, item_id);"#,
        )
    }

    fn migrate_commit_report_statuses(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='commit_reports'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("commit_reports schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'missed'") {
            return Ok(());
        }
        // Keep child foreign keys aimed at the replacement `commit_reports`
        // table while the legacy parent is renamed. Modern SQLite otherwise
        // rewrites them to `commit_reports_legacy`, which makes dropping the
        // copied parent corrupt or fail an upgrade with existing receipts.
        // SQLite rewrites child-table REFERENCES clauses during a parent
        // rename whenever foreign-key enforcement is enabled, even with
        // `legacy_alter_table`. Disable enforcement only for this bounded
        // schema transaction, then re-enable it and prove the rebuilt graph.
        self.execute("PRAGMA foreign_keys=OFF")?;
        self.execute("PRAGMA legacy_alter_table=ON")?;
        let migration = self.execute(
            "BEGIN IMMEDIATE;\
             DROP INDEX IF EXISTS commit_reports_channel_time;\
             ALTER TABLE commit_reports RENAME TO commit_reports_legacy;\
             CREATE TABLE commit_reports (\
               report_id TEXT PRIMARY KEY,\
               plan_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               schedule_item_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL,\
               dispatch_status TEXT NOT NULL CHECK (dispatch_status IN ('pending','queued','acknowledged','missed','completed','error','cancelled')),\
               operator_notes TEXT NOT NULL\
             );\
             INSERT INTO commit_reports SELECT * FROM commit_reports_legacy;\
             DROP TABLE commit_reports_legacy;\
             CREATE INDEX commit_reports_channel_time ON commit_reports(channel_id, approved_at_unix_ms);\
             COMMIT;",
        );
        if migration.is_err() {
            let _ = self.execute("ROLLBACK");
        }
        let legacy_reset = self.execute("PRAGMA legacy_alter_table=OFF");
        let foreign_key_reset = self.execute("PRAGMA foreign_keys=ON");
        migration.and(legacy_reset).and(foreign_key_reset)?;

        let statement = self.prepare("PRAGMA foreign_keys")?;
        let enforcement_enabled = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => (unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if !enforcement_enabled {
            return Err("commit report migration did not restore foreign-key enforcement".into());
        }
        let statement = self.prepare("PRAGMA foreign_key_check")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(()),
            SQLITE_ROW => Err("commit report migration produced a foreign-key violation".into()),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn migrate_schedule_item_revision(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('schedule_items') WHERE name='revision'",
        )?;
        let has_revision = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => (unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if !has_revision {
            self.execute(
                "ALTER TABLE schedule_items ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1)",
            )?;
        }
        Ok(())
    }

    fn migrate_commit_report_schedule_item_revision(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('commit_reports') \
             WHERE name='schedule_item_revision'",
        )?;
        let has_revision = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => (unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if has_revision {
            return Ok(());
        }

        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let history_exists = self.table_exists("schedule_item_history")?;
            let reports = self.read_legacy_commit_report_revision_bindings()?;
            let mut resolved_revisions = Vec::with_capacity(reports.len());
            for report in reports {
                let current = self
                    .read_schedule_item_for_legacy_commit_report_migration(
                        &report.schedule_item_id,
                    )?
                    .ok_or_else(|| {
                        format!(
                            "cannot migrate commit reports: report {} schedule binding is missing",
                            report.report_id
                        )
                    })?;

                let historical_matches = if history_exists {
                    self.read_schedule_item_history_for_legacy_commit_report_migration(
                        &report.schedule_item_id,
                    )?
                    .into_iter()
                    .filter(|entry| {
                        entry.item.channel_id == report.channel_id
                            && entry.item.asset_id == report.asset_id
                            && entry.item.state == ScheduleState::Committed
                            // A modern commit writes these two attributions in the
                            // same transaction as its report.  Requiring both avoids
                            // binding an old report to a later recommit by the same
                            // channel and asset.
                            && entry.changed_by == report.approved_by
                            && entry.occurred_at_unix_ms == report.approved_at_unix_ms
                    })
                    .map(|entry| entry.item.revision)
                    .collect::<Vec<_>>()
                } else {
                    Vec::new()
                };

                let revision = match historical_matches.as_slice() {
                    [revision] => *revision,
                    [] if current.revision == 1
                        && current.channel_id == report.channel_id
                        && current.asset_id == report.asset_id
                        && current.state == ScheduleState::Committed =>
                    {
                        // No history means a revision-1 committed current row is the
                        // sole possible version of this item.  It is the only safe
                        // fallback for databases from before schedule history.
                        1
                    }
                    [] => {
                        return Err(format!(
                            "cannot migrate commit reports: report {} has no exact committed schedule-history binding",
                            report.report_id
                        ));
                    }
                    _ => {
                        return Err(format!(
                            "cannot migrate commit reports: report {} has ambiguous committed schedule-history bindings",
                            report.report_id
                        ));
                    }
                };
                resolved_revisions.push((report.report_id, revision));
            }

            self.execute(
                "ALTER TABLE commit_reports ADD COLUMN \
                 schedule_item_revision INTEGER NOT NULL DEFAULT 1 \
                 CHECK (schedule_item_revision >= 1)",
            )?;
            for (report_id, revision) in resolved_revisions {
                let statement = self.prepare(
                    "UPDATE commit_reports SET schedule_item_revision=?1 WHERE report_id=?2",
                )?;
                bind_i64(
                    self.database,
                    statement.raw,
                    1,
                    checked_i64(revision, "legacy commit report schedule item revision")?,
                )?;
                let report_id = c_string(&report_id, "legacy commit report identity")?;
                bind_text(self.database, statement.raw, 2, &report_id)?;
                expect_done(self.database, statement.raw)?;
                if self.changes() != 1 {
                    return Err("legacy commit report disappeared during migration".into());
                }
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    fn table_exists(&self, table_name: &str) -> Result<bool, String> {
        let statement =
            self.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1 LIMIT 1")?;
        let table_name = c_string(table_name, "SQLite table name")?;
        bind_text(self.database, statement.raw, 1, &table_name)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(true),
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_legacy_commit_report_revision_bindings(
        &self,
    ) -> Result<Vec<LegacyCommitReportRevisionBinding>, String> {
        let statement = self.prepare(
            "SELECT report_id, channel_id, schedule_item_id, asset_id, approved_by, approved_at_unix_ms \
             FROM commit_reports ORDER BY report_id",
        )?;
        let mut reports = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => reports.push(LegacyCommitReportRevisionBinding {
                    report_id: column_text(statement.raw, 0)?,
                    channel_id: column_text(statement.raw, 1)?,
                    schedule_item_id: column_text(statement.raw, 2)?,
                    asset_id: column_text(statement.raw, 3)?,
                    approved_by: column_text(statement.raw, 4)?,
                    approved_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 5) },
                }),
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        Ok(reports)
    }

    fn read_schedule_item_for_legacy_commit_report_migration(
        &self,
        item_id: &str,
    ) -> Result<Option<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE item_id=?1",
        )?;
        let item_id = c_string(item_id, "legacy commit report schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &item_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_schedule_item_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_schedule_item_history_for_legacy_commit_report_migration(
        &self,
        item_id: &str,
    ) -> Result<Vec<ScheduleItemHistoryEntry>, String> {
        let statement = self.prepare(
            "SELECT item_id, revision, record_json, changed_by, occurred_at_unix_ms \
             FROM schedule_item_history WHERE item_id=?1 ORDER BY revision",
        )?;
        let item_id = c_string(item_id, "legacy schedule history item identity")?;
        bind_text(self.database, statement.raw, 1, &item_id)?;
        let mut history = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => history.push(read_schedule_item_history_row(statement.raw)?),
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        Ok(history)
    }

    fn initialize_schedule_item_history_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS schedule_item_history (\
               item_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(item_id, revision),\
               FOREIGN KEY(item_id) REFERENCES schedule_items(item_id) ON DELETE RESTRICT\
             );\
             CREATE INDEX IF NOT EXISTS schedule_item_history_item_revision \
               ON schedule_item_history(item_id, revision);\
             CREATE TRIGGER IF NOT EXISTS schedule_item_history_no_update \
               BEFORE UPDATE ON schedule_item_history BEGIN SELECT RAISE(ABORT, 'schedule item history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_item_history_no_delete \
               BEFORE DELETE ON schedule_item_history BEGIN SELECT RAISE(ABORT, 'schedule item history is append-only'); END;",
        )?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
                 FROM schedule_items ORDER BY item_id",
            )?;
            let mut items = Vec::new();
            loop {
                match unsafe { sqlite3_step(statement.raw) } {
                    SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                    SQLITE_DONE => break,
                    _ => return Err(sqlite_error(self.database)),
                }
            }
            drop(statement);
            for item in items {
                let record_json = serde_json::to_string(&item)
                    .map_err(|error| format!("serialize legacy schedule history: {error}"))?;
                let statement = self.prepare(
                    "INSERT OR IGNORE INTO schedule_item_history(item_id, revision, record_json, changed_by, occurred_at_unix_ms) \
                     VALUES(?1, ?2, ?3, 'legacy-migration', 0)",
                )?;
                let item_id = c_string(&item.item_id, "legacy schedule item identity")?;
                let record_json = c_string(&record_json, "legacy schedule item record")?;
                bind_text(self.database, statement.raw, 1, &item_id)?;
                bind_i64(
                    self.database,
                    statement.raw,
                    2,
                    checked_i64(item.revision, "legacy schedule item revision")?,
                )?;
                bind_text(self.database, statement.raw, 3, &record_json)?;
                expect_done(self.database, statement.raw)?;
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    fn migrate_channel_runtime_live_state(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='channel_runtime'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("channel_runtime schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'live'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE channel_runtime RENAME TO channel_runtime_legacy;\
             CREATE TABLE channel_runtime (\
               channel_id TEXT PRIMARY KEY,\
               state TEXT NOT NULL CHECK (state IN ('starting','healthy_fallback','on_air','live','error','stopped')),\
               on_air_asset_id TEXT NOT NULL DEFAULT '',\
               state_started_at_unix_ms INTEGER NOT NULL CHECK (state_started_at_unix_ms >= 0),\
               heartbeat_at_unix_ms INTEGER NOT NULL CHECK (heartbeat_at_unix_ms >= state_started_at_unix_ms),\
               detail TEXT NOT NULL DEFAULT '',\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             INSERT INTO channel_runtime SELECT * FROM channel_runtime_legacy;\
             DROP TABLE channel_runtime_legacy;\
             COMMIT;",
        )
        .inspect_err(|_| {
            let _ = self.execute("ROLLBACK");
        })
    }

    fn migrate_alert_delivery_realerts(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_deliveries'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("alert_deliveries schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'realert'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             DROP INDEX IF EXISTS alert_deliveries_due;\
             ALTER TABLE alert_deliveries RENAME TO alert_deliveries_legacy;\
             CREATE TABLE alert_deliveries (\
               delivery_id TEXT PRIMARY KEY,\
               event_id TEXT NOT NULL,\
               alert_channel_id TEXT NOT NULL,\
               notification_kind TEXT NOT NULL CHECK (notification_kind IN ('firing','realert','resolved','test')),\
               state TEXT NOT NULL CHECK (state IN ('pending','leased','sent','dead_letter')),\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL CHECK (next_attempt_at_unix_ms >= 0),\
               lease_expires_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (lease_expires_at_unix_ms >= -1),\
               last_error TEXT NOT NULL DEFAULT '',\
               signature TEXT NOT NULL DEFAULT '',\
               payload_json TEXT NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               sent_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (sent_at_unix_ms >= -1),\
               FOREIGN KEY(event_id) REFERENCES alert_events(event_id),\
               FOREIGN KEY(alert_channel_id) REFERENCES alert_channels(channel_id)\
             );\
             INSERT INTO alert_deliveries SELECT * FROM alert_deliveries_legacy;\
             DROP TABLE alert_deliveries_legacy;\
             CREATE UNIQUE INDEX alert_deliveries_one_initial_kind \
               ON alert_deliveries(event_id, alert_channel_id, notification_kind) \
               WHERE notification_kind IN ('firing','resolved','test');\
             CREATE INDEX alert_deliveries_due \
               ON alert_deliveries(state, next_attempt_at_unix_ms, created_at_unix_ms);\
             COMMIT;",
        )
        .inspect_err(|_| {
            let _ = self.execute("ROLLBACK");
        })
    }

    fn seed_default_alert_rules(&self) -> Result<(), String> {
        for rule in default_alert_rules() {
            if self.read_alert_rule(&rule.rule_id)?.is_none() {
                self.put_alert_rule(&rule)?;
            }
        }
        Ok(())
    }

    fn seed_default_self_test_schedules(&self) -> Result<(), String> {
        for schedule in [
            SelfTestSchedule {
                kind: SelfTestKind::Daily,
                enabled: true,
                local_hour: 2,
                local_minute: 0,
                weekday: None,
                updated_at_unix_ms: 0,
                updated_by: "system-seed".into(),
            },
            SelfTestSchedule {
                kind: SelfTestKind::Weekly,
                enabled: true,
                local_hour: 3,
                local_minute: 0,
                weekday: Some(0),
                updated_at_unix_ms: 0,
                updated_by: "system-seed".into(),
            },
        ] {
            if self.read_self_test_schedule(schedule.kind)?.is_none() {
                self.put_self_test_schedule(&schedule)?;
            }
        }
        Ok(())
    }

    fn migrate_ingest_retry_schedule(&self) -> Result<(), String> {
        if self.ingest_retry_column_exists()? {
            return Ok(());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if !self.ingest_retry_column_exists()? {
                self.execute(
                    "ALTER TABLE media_ingest_jobs ADD COLUMN next_attempt_at_unix_ms INTEGER NOT NULL \
                     DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)",
                )?;
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    fn migrate_channel_output_profile(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('channels') WHERE name='output_profile'",
        )?;
        let exists = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => (unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if !exists {
            self.execute(
                "ALTER TABLE channels ADD COLUMN output_profile TEXT NOT NULL \
                 DEFAULT 'proof-360p30'",
            )?;
        }
        Ok(())
    }

    fn migrate_channel_output_sink_kinds(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='channel_output_sinks'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("channel_output_sinks schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'srt'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE channel_output_sinks RENAME TO channel_output_sinks_udp_legacy;\
             CREATE TABLE channel_output_sinks (\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               label TEXT NOT NULL,\
               kind TEXT NOT NULL CHECK (kind IN ('udp-ts','srt')),\
               destination TEXT NOT NULL,\
               loudness_target_milli INTEGER NOT NULL,\
               required INTEGER NOT NULL CHECK (required IN (0,1)),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               PRIMARY KEY(channel_id, sink_id),\
               UNIQUE(channel_id, label COLLATE NOCASE),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             ); \
             INSERT INTO channel_output_sinks(\
               channel_id, sink_id, label, kind, destination, loudness_target_milli, required, enabled) \
             SELECT channel_id, sink_id, label, kind, destination, loudness_target_milli, required, enabled \
             FROM channel_output_sinks_udp_legacy; \
             DROP TABLE channel_output_sinks_udp_legacy; \
             COMMIT",
        )
    }

    fn migrate_headend_sink_contract(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('channel_output_sinks') WHERE name='headend_profile'",
        )?;
        let exists = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => (unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if exists {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE channel_output_sinks ADD COLUMN headend_profile TEXT NOT NULL DEFAULT '';\
             ALTER TABLE channel_output_sinks ADD COLUMN headend_muxrate_kbps INTEGER NOT NULL DEFAULT 0 CHECK (headend_muxrate_kbps >= 0);\
             ALTER TABLE channel_output_sinks ADD COLUMN headend_packet_size INTEGER NOT NULL DEFAULT 0 CHECK (headend_packet_size >= 0);\
             COMMIT",
        )
    }

    fn migrate_channel_output_sink_order(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('channel_output_sinks') WHERE name='sink_order'",
        )?;
        let exists = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => (unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if exists {
            return self.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS channel_output_sinks_channel_order \
                 ON channel_output_sinks(channel_id, sink_order)",
            );
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE channel_output_sinks ADD COLUMN sink_order INTEGER NOT NULL DEFAULT 0 CHECK (sink_order >= 0);\
             UPDATE channel_output_sinks SET sink_order=(\
               SELECT COUNT(*) FROM channel_output_sinks AS ordered \
               WHERE ordered.channel_id=channel_output_sinks.channel_id \
                 AND ordered.sink_id < channel_output_sinks.sink_id\
             );\
             CREATE UNIQUE INDEX channel_output_sinks_channel_order \
               ON channel_output_sinks(channel_id, sink_order);\
             COMMIT",
        )
    }

    fn migrate_channel_output_sinks(&self) -> Result<(), String> {
        self.execute(
            "INSERT INTO channel_output_sinks(\
               channel_id, sink_id, label, kind, destination, loudness_target_milli, required, enabled, sink_order) \
             SELECT channel_id, channel_id, 'Primary transport', 'udp-ts', udp_destination,\
               CASE output_profile WHEN 'atsc-720p5994' THEN -24000 ELSE -16000 END, 1, 1, 0 \
             FROM channels WHERE NOT EXISTS (\
               SELECT 1 FROM channel_output_sinks WHERE channel_output_sinks.channel_id=channels.channel_id)",
        )
    }

    fn migrate_sink_loudness_proofs(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS channel_output_loudness_proofs (\
               proof_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               target_lufs_milli INTEGER NOT NULL CHECK (target_lufs_milli IN (-24000,-16000)),\
               measured_lufs_milli INTEGER NOT NULL DEFAULT -2147483648,\
               tolerance_lufs_milli INTEGER NOT NULL CHECK (tolerance_lufs_milli = 2000),\
               outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail','not_run','invalid')),\
               receiver_acceptance TEXT NOT NULL CHECK (receiver_acceptance IN ('not_attempted','unknown','verified','rejected')),\
               captured_at_unix_ms INTEGER NOT NULL CHECK (captured_at_unix_ms >= 0),\
               sample_duration_ms INTEGER NOT NULL CHECK (sample_duration_ms BETWEEN 1 AND 600000),\
               capture_bytes INTEGER NOT NULL CHECK (capture_bytes BETWEEN 1 AND 1073741824),\
               capture_sha256 TEXT NOT NULL,\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL DEFAULT '',\
               configuration_hash TEXT NOT NULL,\
               measurement_engine TEXT NOT NULL,\
               measurement_method TEXT NOT NULL,\
               detail TEXT NOT NULL,\
               artifact_relative_path TEXT NOT NULL DEFAULT ''\
             );",
        )?;
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='channel_output_loudness_proofs'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("channel_output_loudness_proofs schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if !definition.contains("600000") || !definition.contains("1073741824") {
            self.execute(
                "BEGIN IMMEDIATE;\
                 DROP TRIGGER IF EXISTS channel_output_loudness_proofs_no_update;\
                 DROP TRIGGER IF EXISTS channel_output_loudness_proofs_no_delete;\
                 DROP INDEX IF EXISTS channel_output_loudness_proofs_sink_time;\
                 ALTER TABLE channel_output_loudness_proofs RENAME TO channel_output_loudness_proofs_legacy;\
                 CREATE TABLE channel_output_loudness_proofs (\
                   proof_id TEXT PRIMARY KEY,\
                   channel_id TEXT NOT NULL,\
                   sink_id TEXT NOT NULL,\
                   target_lufs_milli INTEGER NOT NULL CHECK (target_lufs_milli IN (-24000,-16000)),\
                   measured_lufs_milli INTEGER NOT NULL DEFAULT -2147483648,\
                   tolerance_lufs_milli INTEGER NOT NULL CHECK (tolerance_lufs_milli = 2000),\
                   outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail','not_run','invalid')),\
                   receiver_acceptance TEXT NOT NULL CHECK (receiver_acceptance IN ('not_attempted','unknown','verified','rejected')),\
                   captured_at_unix_ms INTEGER NOT NULL CHECK (captured_at_unix_ms >= 0),\
                   sample_duration_ms INTEGER NOT NULL CHECK (sample_duration_ms BETWEEN 1 AND 600000),\
                   capture_bytes INTEGER NOT NULL CHECK (capture_bytes BETWEEN 1 AND 1073741824),\
                   capture_sha256 TEXT NOT NULL,\
                   candidate_version TEXT NOT NULL,\
                   source_commit TEXT NOT NULL DEFAULT '',\
                   configuration_hash TEXT NOT NULL,\
                   measurement_engine TEXT NOT NULL,\
                   measurement_method TEXT NOT NULL,\
                   detail TEXT NOT NULL,\
                   artifact_relative_path TEXT NOT NULL DEFAULT ''\
                 );\
                 INSERT INTO channel_output_loudness_proofs SELECT * FROM channel_output_loudness_proofs_legacy;\
                 DROP TABLE channel_output_loudness_proofs_legacy;\
                 COMMIT;",
            )
            .inspect_err(|_| {
                let _ = self.execute("ROLLBACK");
            })?;
        }
        self.execute(
            "CREATE INDEX IF NOT EXISTS channel_output_loudness_proofs_sink_time ON channel_output_loudness_proofs(channel_id, sink_id, captured_at_unix_ms, proof_id)",
        )
        .map_err(|error| format!("create loudness proof index: {error}"))?;
        self.execute(
            "CREATE TRIGGER IF NOT EXISTS channel_output_loudness_proofs_no_update BEFORE UPDATE ON channel_output_loudness_proofs BEGIN SELECT RAISE(ABORT, 'loudness proofs are append-only'); END;",
        )
        .map_err(|error| format!("create loudness proof update trigger: {error}"))?;
        self.execute(
            "CREATE TRIGGER IF NOT EXISTS channel_output_loudness_proofs_no_delete BEFORE DELETE ON channel_output_loudness_proofs BEGIN SELECT RAISE(ABORT, 'loudness proofs are append-only'); END;",
        )
        .map_err(|error| format!("create loudness proof delete trigger: {error}"))
    }

    fn migrate_output_loudness_jobs(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS output_loudness_jobs (\
               job_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               target_group TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               target_lufs_milli INTEGER NOT NULL CHECK (target_lufs_milli IN (-24000,-16000)),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL DEFAULT '',\
               configuration_hash TEXT NOT NULL,\
               sample_duration_ms INTEGER NOT NULL CHECK (sample_duration_ms BETWEEN 3000 AND 600000),\
               max_capture_bytes INTEGER NOT NULL CHECK (max_capture_bytes BETWEEN 1 AND 1073741824),\
               state TEXT NOT NULL CHECK (state IN ('pending','claimed','capture_pending','analyzing','completed','failed')),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               lease_expires_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (lease_expires_at_unix_ms >= -1),\
               capture_relative_path TEXT NOT NULL DEFAULT '',\
               capture_bytes INTEGER NOT NULL DEFAULT -1 CHECK (capture_bytes >= -1),\
               capture_sample_duration_ms INTEGER NOT NULL DEFAULT -1 CHECK (capture_sample_duration_ms >= -1),\
               capture_sha256 TEXT NOT NULL DEFAULT '',\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               failed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (failed_at_unix_ms >= -1),\
               failure_detail TEXT NOT NULL DEFAULT ''\
             );",
        )?;
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='output_loudness_jobs'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("output_loudness_jobs schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if !definition.contains("600000") || !definition.contains("1073741824") {
            self.execute(
                "BEGIN IMMEDIATE;\
                 DROP INDEX IF EXISTS output_loudness_jobs_active_group;\
                 DROP INDEX IF EXISTS output_loudness_jobs_state_time;\
                 ALTER TABLE output_loudness_jobs RENAME TO output_loudness_jobs_legacy;\
                 CREATE TABLE output_loudness_jobs (\
                   job_id TEXT PRIMARY KEY,\
                   channel_id TEXT NOT NULL,\
                   target_group TEXT NOT NULL,\
                   sink_id TEXT NOT NULL,\
                   target_lufs_milli INTEGER NOT NULL CHECK (target_lufs_milli IN (-24000,-16000)),\
                   candidate_version TEXT NOT NULL,\
                   source_commit TEXT NOT NULL DEFAULT '',\
                   configuration_hash TEXT NOT NULL,\
                   sample_duration_ms INTEGER NOT NULL CHECK (sample_duration_ms BETWEEN 3000 AND 600000),\
                   max_capture_bytes INTEGER NOT NULL CHECK (max_capture_bytes BETWEEN 1 AND 1073741824),\
                   state TEXT NOT NULL CHECK (state IN ('pending','claimed','capture_pending','analyzing','completed','failed')),\
                   created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
                   updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
                   lease_expires_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (lease_expires_at_unix_ms >= -1),\
                   capture_relative_path TEXT NOT NULL DEFAULT '',\
                   capture_bytes INTEGER NOT NULL DEFAULT -1 CHECK (capture_bytes >= -1),\
                   capture_sample_duration_ms INTEGER NOT NULL DEFAULT -1 CHECK (capture_sample_duration_ms >= -1),\
                   capture_sha256 TEXT NOT NULL DEFAULT '',\
                   completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
                   failed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (failed_at_unix_ms >= -1),\
                   failure_detail TEXT NOT NULL DEFAULT ''\
                 );\
                 INSERT INTO output_loudness_jobs SELECT * FROM output_loudness_jobs_legacy;\
                 DROP TABLE output_loudness_jobs_legacy;\
                 COMMIT;",
            )
            .inspect_err(|_| {
                let _ = self.execute("ROLLBACK");
            })?;
        }
        self.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS output_loudness_jobs_active_group ON output_loudness_jobs(channel_id, target_group) WHERE state NOT IN ('completed','failed')",
        )?;
        self.execute(
            "CREATE INDEX IF NOT EXISTS output_loudness_jobs_state_time ON output_loudness_jobs(state, created_at_unix_ms, job_id)",
        )
    }

    fn migrate_ingest_transcode_status(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_ingest_jobs'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("media_ingest_jobs schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'awaiting_transcode'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE media_ingest_jobs RENAME TO media_ingest_jobs_legacy;\
             CREATE TABLE media_ingest_jobs (\
               job_id TEXT PRIMARY KEY,\
               source_kind TEXT NOT NULL CHECK (source_kind IN ('operator_path','watch_folder','live_finalization')),\
               source_path TEXT NOT NULL,\
               status TEXT NOT NULL CHECK (status IN ('pending','running','awaiting_transcode','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               error_detail TEXT NOT NULL DEFAULT '',\
               asset_id TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             INSERT INTO media_ingest_jobs SELECT * FROM media_ingest_jobs_legacy;\
             DROP TABLE media_ingest_jobs_legacy;\
             CREATE INDEX media_ingest_jobs_status_time ON media_ingest_jobs(status, created_at_unix_ms);\
             COMMIT;",
        )
        .inspect_err(|_| {
            let _ = self.execute("ROLLBACK");
        })
    }

    fn ingest_retry_column_exists(&self) -> Result<bool, String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('media_ingest_jobs') \
             WHERE name='next_attempt_at_unix_ms'",
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok((unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn upsert_reference_station_tier_selection(
        &self,
        selection: &ReferenceStationTierSelection,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO reference_station_tier_selection(\
               singleton,catalog_version,tier,revision,created_at_unix_ms,updated_at_unix_ms,updated_by) \
             VALUES(1,?1,?2,?3,?4,?5,?6) \
             ON CONFLICT(singleton) DO UPDATE SET \
               catalog_version=excluded.catalog_version,tier=excluded.tier,revision=excluded.revision,\
               updated_at_unix_ms=excluded.updated_at_unix_ms,updated_by=excluded.updated_by",
        )?;
        let tier = c_string(selection.tier.persisted_name(), "reference station tier")?;
        let actor = c_string(&selection.updated_by, "reference station tier actor")?;
        bind_i64(
            self.database,
            statement.raw,
            1,
            i64::from(selection.catalog_version),
        )?;
        bind_text(self.database, statement.raw, 2, &tier)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            i64::try_from(selection.revision)
                .map_err(|_| "reference station tier revision exceeds SQLite range")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            selection.created_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            selection.updated_at_unix_ms,
        )?;
        bind_text(self.database, statement.raw, 6, &actor)?;
        expect_done(self.database, statement.raw)
    }

    fn append_reference_station_tier_audit(
        &self,
        audit: &ReferenceStationTierAuditEvent,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO reference_station_tier_audit(\
               revision,catalog_version,tier,actor,occurred_at_unix_ms) VALUES(?1,?2,?3,?4,?5)",
        )?;
        let tier = c_string(
            audit.tier.persisted_name(),
            "reference station tier audit tier",
        )?;
        let actor = c_string(&audit.actor, "reference station tier audit actor")?;
        bind_i64(
            self.database,
            statement.raw,
            1,
            i64::try_from(audit.revision)
                .map_err(|_| "reference station tier audit revision exceeds SQLite range")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            i64::from(audit.catalog_version),
        )?;
        bind_text(self.database, statement.raw, 3, &tier)?;
        bind_text(self.database, statement.raw, 4, &actor)?;
        bind_i64(self.database, statement.raw, 5, audit.occurred_at_unix_ms)?;
        expect_done(self.database, statement.raw)
    }

    fn upsert_profile(&self, profile: &StationProfile) -> Result<(), String> {
        let sql = "INSERT INTO station_profile(singleton, station_id, display_name, timezone) VALUES(1, ?1, ?2, ?3) \
                   ON CONFLICT(singleton) DO UPDATE SET station_id=excluded.station_id, display_name=excluded.display_name, \
                   timezone=excluded.timezone, revision=station_profile.revision+1";
        let statement = self.prepare(sql)?;
        let values = [
            CString::new(profile.station_id.as_str()),
            CString::new(profile.display_name.as_str()),
            CString::new(profile.timezone.as_str()),
        ];
        for (offset, value) in values.iter().enumerate() {
            let value = value
                .as_ref()
                .map_err(|_| "profile contains a null byte".to_string())?;
            // SAFETY: the statement is live and each CString outlives `sqlite3_step` below.
            let status = unsafe {
                sqlite3_bind_text(
                    statement.raw,
                    (offset + 1) as c_int,
                    value.as_ptr(),
                    -1,
                    ptr::null_mut(),
                )
            };
            if status != SQLITE_OK {
                return Err(sqlite_error(self.database));
            }
        }
        // SAFETY: all parameters are bound and the statement is live.
        let status = unsafe { sqlite3_step(statement.raw) };
        if status == SQLITE_DONE {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn prepare(&self, sql: &str) -> Result<Statement, String> {
        let sql = CString::new(sql).map_err(|_| "SQL contains a null byte".to_string())?;
        let mut statement = ptr::null_mut();
        // SAFETY: the database handle is live and the SQL is a valid C string.
        let status = unsafe {
            sqlite3_prepare_v2(
                self.database,
                sql.as_ptr(),
                -1,
                &mut statement,
                ptr::null_mut(),
            )
        };
        if status == SQLITE_OK {
            Ok(Statement { raw: statement })
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn changes(&self) -> i64 {
        let statement = self
            .prepare("SELECT changes()")
            .expect("SELECT changes is valid");
        let status = unsafe { sqlite3_step(statement.raw) };
        debug_assert_eq!(status, SQLITE_ROW);
        unsafe { sqlite3_column_int64(statement.raw, 0) }
    }

    fn finish_transaction(&self, outcome: Result<(), String>) -> Result<(), String> {
        match outcome {
            Ok(()) => match self.execute("COMMIT") {
                Ok(()) => Ok(()),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    fn finish_asset_metadata_transaction(
        &self,
        outcome: Result<(), AssetMetadataWriteError>,
    ) -> Result<(), AssetMetadataWriteError> {
        match outcome {
            Ok(()) => match self.execute("COMMIT") {
                Ok(()) => Ok(()),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(AssetMetadataWriteError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    fn finish_custom_metadata_schema_transaction(
        &self,
        outcome: Result<(), CustomMetadataSchemaWriteError>,
    ) -> Result<(), CustomMetadataSchemaWriteError> {
        match outcome {
            Ok(()) => match self.execute("COMMIT") {
                Ok(()) => Ok(()),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CustomMetadataSchemaWriteError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    fn finish_value_transaction<T>(&self, outcome: Result<T, String>) -> Result<T, String> {
        match outcome {
            Ok(value) => match self.execute("COMMIT") {
                Ok(()) => Ok(value),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }
}

struct TransportProofBinding<'a> {
    channel_id: &'a str,
    sink_id: &'a str,
    target_id: &'a str,
    target_revision: u64,
    candidate_version: &'a str,
    source_commit: &'a str,
    configuration_hash: &'a str,
}

struct TransportReceiptLookup<'a> {
    channel_id: &'a str,
    sink_id: &'a str,
    target: &'a TargetDeviceConfig,
    candidate_version: &'a str,
    source_commit: &'a str,
    configuration_hash: &'a str,
}

fn bind_transport_receipt_lookup(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    lookup: TransportReceiptLookup<'_>,
) -> Result<(), String> {
    let TransportReceiptLookup {
        channel_id,
        sink_id,
        target,
        candidate_version,
        source_commit,
        configuration_hash,
    } = lookup;
    for (value, index, field) in [
        (channel_id, 1, "transport channel"),
        (sink_id, 2, "transport sink"),
        (&target.target_id, 3, "transport target"),
        (candidate_version, 5, "transport candidate"),
        (source_commit, 6, "transport source commit"),
        (configuration_hash, 7, "transport configuration hash"),
    ] {
        bind_text(database, statement, index, &c_string(value, field)?)?;
    }
    bind_i64(
        database,
        statement,
        4,
        i64::try_from(target.revision)
            .map_err(|_| "transport target revision exceeds SQLite range".to_string())?,
    )
}

fn migrate_legacy_transport_target_config(record_json: &str) -> Result<Option<String>, String> {
    let mut value: serde_json::Value = match serde_json::from_str(record_json) {
        Ok(value) => value,
        // A corrupt legacy record must not make process startup unavailable. It
        // stays unreadable, which is the fail-closed authority outcome.
        Err(_) => return Ok(None),
    };
    let Some(record) = value.as_object_mut() else {
        return Ok(None);
    };
    let Some(endpoint) = record.get("endpoint").and_then(serde_json::Value::as_str) else {
        return Ok(None);
    };
    let Some(protocol) = record.get("protocol").and_then(serde_json::Value::as_str) else {
        return Ok(None);
    };
    let Some((host, port)) = legacy_transport_endpoint_host_port(protocol, endpoint) else {
        return Ok(None);
    };
    record.remove("endpoint");
    record.insert("host".into(), serde_json::Value::String(host));
    record.insert("port".into(), serde_json::Value::Number(port.into()));
    let rewritten = serde_json::to_string(&value)
        .map_err(|error| format!("serialize migrated transport target: {error}"))?;
    let config: TargetDeviceConfig = match serde_json::from_str(&rewritten) {
        Ok(config) => config,
        Err(_) => return Ok(None),
    };
    if config.validate().is_err() {
        return Ok(None);
    }
    Ok(Some(rewritten))
}

fn legacy_transport_endpoint_host_port(protocol: &str, endpoint: &str) -> Option<(String, u16)> {
    let (scheme, default_port) = match protocol {
        "https_api" => ("https://", 443),
        "snmp_tls" => ("snmp+tls://", 10_161),
        // `fake_authenticated_v1` was intentionally test-only. It has no
        // production structured equivalent and must remain blocked.
        _ => return None,
    };
    let authority = endpoint.strip_prefix(scheme)?;
    if authority.is_empty()
        || authority.contains(['/', '?', '#', '@'])
        || authority.chars().any(char::is_whitespace)
    {
        return None;
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() && !port.is_empty() => {
            (host, port.parse::<u16>().ok()?)
        }
        Some(_) => return None,
        None => (authority, default_port),
    };
    (port != 0).then(|| (host.to_owned(), port))
}

fn station_data_root(database_path: &Path) -> Result<PathBuf, String> {
    match database_path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
    {
        Some(path) => Ok(path.to_path_buf()),
        None => std::env::current_dir().map_err(|error| format!("resolve data root: {error}")),
    }
}

impl Drop for StationStore {
    fn drop(&mut self) {
        // SAFETY: `StationStore` owns this live handle and drops it exactly once.
        unsafe { sqlite3_close(self.database) };
    }
}

fn finish_cg_transaction<T>(
    store: &StationStore,
    outcome: Result<T, CgWriteError>,
) -> Result<T, CgWriteError> {
    match outcome {
        Ok(value) => match store.execute("COMMIT") {
            Ok(()) => Ok(value),
            Err(error) => {
                let _ = store.execute("ROLLBACK");
                Err(CgWriteError::Storage(error))
            }
        },
        Err(error) => {
            let _ = store.execute("ROLLBACK");
            Err(error)
        }
    }
}

struct Statement {
    raw: *mut SqliteStatement,
}

impl Drop for Statement {
    fn drop(&mut self) {
        // SAFETY: `Statement` owns this live handle and drops it exactly once.
        unsafe { sqlite3_finalize(self.raw) };
    }
}

fn bind_text(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: &CString,
) -> Result<(), String> {
    let status = unsafe {
        sqlite3_bind_text(
            statement,
            index,
            value.as_ptr(),
            -1,
            sqlite_transient_destructor(),
        )
    };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn bind_i64(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: i64,
) -> Result<(), String> {
    let status = unsafe { sqlite3_bind_int64(statement, index, value) };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn bind_blob(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: &[u8],
) -> Result<(), String> {
    let byte_count = c_int::try_from(value.len())
        .map_err(|_| "encrypted alert configuration exceeds SQLite's blob limit".to_string())?;
    let status = unsafe {
        sqlite3_bind_blob(
            statement,
            index,
            value.as_ptr().cast(),
            byte_count,
            sqlite_transient_destructor(),
        )
    };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn sqlite_transient_destructor() -> *mut c_void {
    // SQLite defines the all-bits-one destructor sentinel as SQLITE_TRANSIENT: it copies the
    // bound bytes before returning. Generic bind helpers cannot require every caller's buffer to
    // outlive a later sqlite3_step, and digest accessors commonly return references to temporaries.
    ptr::without_provenance_mut(usize::MAX)
}

fn expect_done(database: *mut Sqlite3, statement: *mut SqliteStatement) -> Result<(), String> {
    if unsafe { sqlite3_step(statement) } == SQLITE_DONE {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn c_string(value: &str, field: &str) -> Result<CString, String> {
    CString::new(value).map_err(|_| format!("{field} contains a null byte"))
}

fn hash_field(digest: &mut Sha256, value: &str) {
    digest.update((value.len() as u64).to_le_bytes());
    digest.update(value.as_bytes());
}

fn validate_artifact_relative_path(value: &str) -> Result<(), String> {
    if value.is_empty() || value.len() > 2_048 || value.chars().any(char::is_control) {
        return Err("loudness proof artifact path must be a bounded visible relative path".into());
    }
    let path = Path::new(value);
    if path.is_absolute()
        || path.components().any(|component| {
            matches!(
                component,
                Component::Prefix(_)
                    | Component::RootDir
                    | Component::ParentDir
                    | Component::CurDir
            )
        })
    {
        return Err("loudness proof artifact path must not escape its managed root".into());
    }
    Ok(())
}

fn checked_i64(value: u64, field: &str) -> Result<i64, String> {
    i64::try_from(value).map_err(|_| format!("{field} exceeds SQLite's integer range"))
}

fn read_reference_station_tier_selection_row(
    statement: *mut SqliteStatement,
) -> Result<ReferenceStationTierSelection, String> {
    let catalog_version = u32::try_from(unsafe { sqlite3_column_int64(statement, 0) })
        .map_err(|_| "database returned invalid reference station catalog version".to_string())?;
    let selection = ReferenceStationTierSelection {
        catalog_version,
        tier: ReferenceStationTier::parse_persisted_name(&column_text(statement, 1)?)
            .map_err(|error| format!("database returned {error}"))?,
        revision: u64::try_from(unsafe { sqlite3_column_int64(statement, 2) })
            .map_err(|_| "database returned invalid reference station tier revision".to_string())?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 3) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        updated_by: column_text(statement, 5)?,
    };
    selection.validate().map_err(|error| {
        format!("database returned invalid reference station tier selection: {error}")
    })?;
    Ok(selection)
}

fn read_reference_station_tier_audit_row(
    statement: *mut SqliteStatement,
) -> Result<ReferenceStationTierAuditEvent, String> {
    let event = ReferenceStationTierAuditEvent {
        revision: u64::try_from(unsafe { sqlite3_column_int64(statement, 0) }).map_err(|_| {
            "database returned invalid reference station tier audit revision".to_string()
        })?,
        catalog_version: u32::try_from(unsafe { sqlite3_column_int64(statement, 1) }).map_err(
            |_| "database returned invalid reference station audit catalog version".to_string(),
        )?,
        tier: ReferenceStationTier::parse_persisted_name(&column_text(statement, 2)?)
            .map_err(|error| format!("database returned {error}"))?,
        actor: column_text(statement, 3)?,
        occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
    };
    event.validate().map_err(|error| {
        format!("database returned invalid reference station tier audit: {error}")
    })?;
    Ok(event)
}

fn read_media_asset_row(statement: *mut SqliteStatement) -> Result<MediaAsset, String> {
    Ok(MediaAsset {
        asset_id: column_text(statement, 0)?,
        media_path: column_text(statement, 1)?,
        duration_ms: column_u64(statement, 2, "asset duration")?,
        readiness: parse_readiness(&column_text(statement, 3)?)?,
    })
}

fn insert_caption_cue(
    store: &StationStore,
    table: &str,
    parent_identity: &CString,
    ordinal: usize,
    cue: &AssetCaptionCue,
) -> Result<(), String> {
    let sql = match table {
        "asset_caption_cues" => {
            "INSERT INTO asset_caption_cues(\
               track_id, cue_ordinal, cue_id, start_ms, duration_ms, cue_text\
             ) VALUES(?1,?2,?3,?4,?5,?6)"
        }
        "asset_caption_track_revision_cues" => {
            "INSERT INTO asset_caption_track_revision_cues(\
               audit_id, cue_ordinal, cue_id, start_ms, duration_ms, cue_text\
             ) VALUES(?1,?2,?3,?4,?5,?6)"
        }
        _ => return Err("caption cue storage target is invalid".into()),
    };
    let statement = store.prepare(sql)?;
    let cue_identity = c_string(&cue.cue_id, "caption cue identity")?;
    let cue_text = c_string(&cue.text, "caption cue text")?;
    bind_text(store.database, statement.raw, 1, parent_identity)?;
    bind_i64(
        store.database,
        statement.raw,
        2,
        checked_i64(ordinal as u64, "caption cue ordinal")?,
    )?;
    bind_text(store.database, statement.raw, 3, &cue_identity)?;
    bind_i64(
        store.database,
        statement.raw,
        4,
        checked_i64(cue.start_ms, "caption cue start")?,
    )?;
    bind_i64(
        store.database,
        statement.raw,
        5,
        checked_i64(cue.duration_ms, "caption cue duration")?,
    )?;
    bind_text(store.database, statement.raw, 6, &cue_text)?;
    expect_done(store.database, statement.raw)
}

fn read_cg_image_asset_row(statement: *mut SqliteStatement) -> Result<CgImageAsset, String> {
    read_cg_image_asset_columns(statement, 0)
}

fn read_caption_policy_row(
    statement: *mut SqliteStatement,
) -> Result<ChannelCaptionPolicy, String> {
    let service_number =
        u8::try_from(column_u64(statement, 2, "caption policy service number")?)
            .map_err(|_| "database returned an invalid caption service number".to_string())?;
    let policy = ChannelCaptionPolicy {
        channel_id: column_text(statement, 0)?,
        mode: ChannelCaptionMode::from_storage(&column_text(statement, 1)?)?,
        service_number,
        language: column_text(statement, 3)?,
        revision: column_u64(statement, 4, "caption policy revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        updated_by: column_text(statement, 7)?,
    };
    validate_caption_policy(&policy)?;
    Ok(policy)
}

fn read_caption_policy_audit_row(
    statement: *mut SqliteStatement,
) -> Result<ChannelCaptionPolicyAuditEvent, String> {
    let service_number = u8::try_from(column_u64(
        statement,
        3,
        "caption policy audit service number",
    )?)
    .map_err(|_| "database returned an invalid caption audit service number".to_string())?;
    let event = ChannelCaptionPolicyAuditEvent {
        audit_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        mode: ChannelCaptionMode::from_storage(&column_text(statement, 2)?)?,
        service_number,
        language: column_text(statement, 4)?,
        revision: column_u64(statement, 5, "caption policy audit revision")?,
        actor: column_text(statement, 6)?,
        occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement, 7) },
    };
    if !is_canonical_uuid(&event.audit_id)
        || !is_canonical_uuid(&event.channel_id)
        || !(1..=63).contains(&event.service_number)
        || !is_caption_language(&event.language)
        || event.revision == 0
        || !secret_field(&event.actor, 200)
        || event.occurred_at_unix_ms < 0
    {
        return Err("database returned an invalid caption policy audit event".into());
    }
    Ok(event)
}

fn validate_caption_policy(policy: &ChannelCaptionPolicy) -> Result<(), String> {
    if !is_canonical_uuid(&policy.channel_id)
        || !(1..=63).contains(&policy.service_number)
        || !is_caption_language(&policy.language)
        || policy.revision == 0
        || policy.created_at_unix_ms < 0
        || policy.updated_at_unix_ms < policy.created_at_unix_ms
        || !secret_field(&policy.updated_by, 200)
    {
        return Err("database returned an invalid channel caption policy".into());
    }
    Ok(())
}

fn read_cg_image_asset_columns(
    statement: *mut SqliteStatement,
    first_column: c_int,
) -> Result<CgImageAsset, String> {
    let asset = CgImageAsset {
        asset_id: column_text(statement, first_column)?,
        mime_type: column_text(statement, first_column + 1)?,
        byte_size: u64::try_from(unsafe { sqlite3_column_int64(statement, first_column + 2) })
            .map_err(|_| "database returned an invalid CG image byte size".to_string())?,
        width: u32::try_from(unsafe { sqlite3_column_int64(statement, first_column + 3) })
            .map_err(|_| "database returned an invalid CG image width".to_string())?,
        height: u32::try_from(unsafe { sqlite3_column_int64(statement, first_column + 4) })
            .map_err(|_| "database returned an invalid CG image height".to_string())?,
        readiness: cg_image_readiness_from_name(&column_text(statement, first_column + 5)?)?,
    };
    asset.validate().map_err(str::to_string)?;
    Ok(asset)
}

fn validate_fallback_slate_selection(selection: &ChannelFallbackSlate) -> Result<(), String> {
    if !is_canonical_uuid(&selection.channel_id)
        || selection.revision == 0
        || selection.created_at_unix_ms < 0
        || selection.updated_at_unix_ms < selection.created_at_unix_ms
        || !secret_field(&selection.updated_by, 200)
    {
        return Err("database returned invalid fallback slate authority".into());
    }
    selection.image_asset.validate().map_err(str::to_string)
}

fn validate_fallback_slate_audit(event: &ChannelFallbackSlateAuditEvent) -> Result<(), String> {
    if !is_canonical_uuid(&event.audit_id)
        || !is_canonical_uuid(&event.channel_id)
        || !is_sha256_identity(&event.image_asset_id)
        || event.revision == 0
        || !secret_field(&event.actor, 200)
        || event.occurred_at_unix_ms < 0
    {
        return Err("database returned invalid fallback slate audit event".into());
    }
    Ok(())
}

fn read_channel_runtime_row(
    statement: *mut SqliteStatement,
) -> Result<ChannelRuntimeObservation, String> {
    let asset_id = column_text(statement, 2)?;
    let detail = column_text(statement, 5)?;
    let observation = ChannelRuntimeObservation {
        channel_id: column_text(statement, 0)?,
        state: parse_channel_runtime_state(&column_text(statement, 1)?)?,
        on_air_asset_id: (!asset_id.is_empty()).then_some(asset_id),
        state_started_at_unix_ms: unsafe { sqlite3_column_int64(statement, 3) },
        heartbeat_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        detail: (!detail.is_empty()).then_some(detail),
    };
    observation.validate().map_err(str::to_string)?;
    Ok(observation)
}

fn read_cg_board_row(statement: *mut SqliteStatement) -> Result<CgBoard, String> {
    let background_audio = column_text(statement, 4)?;
    let board = CgBoard {
        board_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        template_id: column_text(statement, 2)?,
        active: unsafe { sqlite3_column_int64(statement, 3) } == 1,
        background_audio_asset_id: (!background_audio.is_empty()).then_some(background_audio),
        revision: column_u64(statement, 5, "CG board revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 7) },
        created_by: column_text(statement, 8)?,
    };
    board.validate().map_err(str::to_string)?;
    Ok(board)
}

fn read_cg_zone_row(statement: *mut SqliteStatement) -> Result<CgZoneConfig, String> {
    let content_source_json = column_text(statement, 4)?;
    let tags_json = column_text(statement, 7)?;
    let rotation = unsafe { sqlite3_column_int64(statement, 5) };
    let zone = CgZoneConfig {
        zone_id: column_text(statement, 0)?,
        board_id: column_text(statement, 1)?,
        template_zone_key: column_text(statement, 2)?,
        zone_kind: parse_cg_zone_kind(&column_text(statement, 3)?)?,
        content_source: serde_json::from_str::<CgContentSource>(&content_source_json)
            .map_err(|error| format!("database returned an invalid CG content source: {error}"))?,
        rotation_seconds: (rotation >= 0)
            .then(|| u32::try_from(rotation))
            .transpose()
            .map_err(|_| "database returned an invalid CG rotation interval".to_string())?,
        approval_required: unsafe { sqlite3_column_int64(statement, 6) } == 1,
        allowed_tags: serde_json::from_str(&tags_json)
            .map_err(|error| format!("database returned invalid CG zone tags: {error}"))?,
        revision: column_u64(statement, 8, "CG zone revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 10) },
    };
    zone.validate().map_err(str::to_string)?;
    Ok(zone)
}

fn read_cg_feed_source_row(statement: *mut SqliteStatement) -> Result<CgFeedSource, String> {
    let last_fetch_error = column_text(statement, 14)?;
    let last_fetched = unsafe { sqlite3_column_int64(statement, 13) };
    let feed = CgFeedSource {
        feed_source_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        kind: parse_cg_feed_kind(&column_text(statement, 2)?)?,
        label: column_text(statement, 3)?,
        source_url: column_text(statement, 4)?,
        trust_tier: parse_cg_feed_trust_tier(&column_text(statement, 5)?)?,
        tags: serde_json::from_str(&column_text(statement, 6)?)
            .map_err(|error| format!("database returned invalid CG feed tags: {error}"))?,
        refresh_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 7) })
            .map_err(|_| "database returned an invalid CG feed refresh interval".to_string())?,
        enabled: unsafe { sqlite3_column_int64(statement, 8) } == 1,
        revision: column_u64(statement, 9, "CG feed revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 10) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 11) },
        created_by: column_text(statement, 12)?,
        last_fetched_at_unix_ms: (last_fetched >= 0).then_some(last_fetched),
        last_fetch_error: (!last_fetch_error.is_empty()).then_some(last_fetch_error),
    };
    feed.validate().map_err(str::to_string)?;
    Ok(feed)
}

fn read_cg_bulletin_row(statement: *mut SqliteStatement) -> Result<CgBulletin, String> {
    let content_json = column_text(statement, 2)?;
    let tags_json = column_text(statement, 4)?;
    let requested_start = unsafe { sqlite3_column_int64(statement, 5) };
    let requested_end = unsafe { sqlite3_column_int64(statement, 6) };
    let audio_asset_id = column_text(statement, 8)?;
    let moderation_notes = column_text(statement, 10)?;
    let moderated_by = column_text(statement, 11)?;
    let bulletin = CgBulletin {
        bulletin_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        content: serde_json::from_str::<CgBulletinContent>(&content_json)
            .map_err(|error| format!("database returned invalid CG bulletin content: {error}"))?,
        state: parse_cg_bulletin_state(&column_text(statement, 3)?)?,
        tags: serde_json::from_str(&tags_json)
            .map_err(|error| format!("database returned invalid CG bulletin tags: {error}"))?,
        requested_start_at_unix_ms: (requested_start >= 0).then_some(requested_start),
        requested_end_at_unix_ms: (requested_end >= 0).then_some(requested_end),
        display_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 7) })
            .map_err(|_| "database returned an invalid CG bulletin duration".to_string())?,
        audio_asset_id: (!audio_asset_id.is_empty()).then_some(audio_asset_id),
        submitted_by: column_text(statement, 9)?,
        moderation_notes: (!moderation_notes.is_empty()).then_some(moderation_notes),
        moderated_by: (!moderated_by.is_empty()).then_some(moderated_by),
        revision: column_u64(statement, 12, "CG bulletin revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 13) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 14) },
    };
    bulletin.validate().map_err(str::to_string)?;
    Ok(bulletin)
}

fn read_cg_feed_item_approval_row(
    statement: *mut SqliteStatement,
) -> Result<CgFeedItemApproval, String> {
    let approval = CgFeedItemApproval {
        feed_source_id: column_text(statement, 0)?,
        item_id: column_text(statement, 1)?,
        approved_by: column_text(statement, 2)?,
        approved_at_unix_ms: unsafe { sqlite3_column_int64(statement, 3) },
    };
    approval.validate().map_err(str::to_string)?;
    Ok(approval)
}

fn read_cg_audit_row(statement: *mut SqliteStatement) -> Result<CgAuditEvent, String> {
    let board_id = column_text(statement, 1)?;
    let details_json = column_text(statement, 6)?;
    let audit = CgAuditEvent {
        audit_id: column_text(statement, 0)?,
        board_id: (!board_id.is_empty()).then_some(board_id),
        channel_id: column_text(statement, 2)?,
        event_kind: parse_cg_audit_event_kind(&column_text(statement, 3)?)?,
        operator_id: column_text(statement, 4)?,
        occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        details: serde_json::from_str::<BTreeMap<String, String>>(&details_json)
            .map_err(|error| format!("database returned invalid CG audit details: {error}"))?,
    };
    audit.validate().map_err(str::to_string)?;
    Ok(audit)
}

fn read_system_resource_sample_row(
    statement: *mut SqliteStatement,
) -> Result<SystemResourceSample, String> {
    let sample = SystemResourceSample {
        sample_id: Some(column_u64(statement, 0, "system resource sample identity")?),
        sampled_at_unix_ms: unsafe { sqlite3_column_int64(statement, 1) },
        cpu_percent: unscaled_metric(unsafe { sqlite3_column_int64(statement, 2) }),
        ram_used_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 3) }),
        ram_total_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 4) }),
        gpu_percent: unscaled_metric(unsafe { sqlite3_column_int64(statement, 5) }),
        gpu_vram_used_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 6) }),
        media_volume_free_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 7) }),
        backup_volume_free_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 8) }),
        database_reachable: unsafe { sqlite3_column_int64(statement, 9) } == 1,
        backup_volume_writable: unsafe { sqlite3_column_int64(statement, 10) } == 1,
        service_running: unsafe { sqlite3_column_int64(statement, 11) } == 1,
        clock_skew_seconds: unscaled_signed_metric(unsafe { sqlite3_column_int64(statement, 12) }),
    };
    sample.validate().map_err(str::to_string)?;
    Ok(sample)
}

fn read_self_test_schedule_row(
    statement: *mut SqliteStatement,
) -> Result<SelfTestSchedule, String> {
    let weekday = unsafe { sqlite3_column_int64(statement, 4) };
    let schedule = SelfTestSchedule {
        kind: parse_self_test_kind(&column_text(statement, 0)?)?,
        enabled: unsafe { sqlite3_column_int64(statement, 1) } == 1,
        local_hour: u8::try_from(unsafe { sqlite3_column_int64(statement, 2) })
            .map_err(|_| "database returned an invalid self-test hour".to_string())?,
        local_minute: u8::try_from(unsafe { sqlite3_column_int64(statement, 3) })
            .map_err(|_| "database returned an invalid self-test minute".to_string())?,
        weekday: (weekday >= 0)
            .then(|| u8::try_from(weekday))
            .transpose()
            .map_err(|_| "database returned an invalid self-test weekday".to_string())?,
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        updated_by: column_text(statement, 6)?,
    };
    schedule.validate().map_err(str::to_string)?;
    Ok(schedule)
}

fn read_system_self_test_row(statement: *mut SqliteStatement) -> Result<SystemSelfTest, String> {
    let period_key = column_text(statement, 3)?;
    let evidence_path = column_text(statement, 9)?;
    let checks_json = column_text(statement, 7)?;
    let record = SystemSelfTest {
        self_test_id: column_text(statement, 0)?,
        kind: parse_self_test_kind(&column_text(statement, 1)?)?,
        trigger: parse_self_test_trigger(&column_text(statement, 2)?)?,
        schedule_period_key: (!period_key.is_empty()).then_some(period_key),
        started_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        finished_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        status: parse_self_test_status(&column_text(statement, 6)?)?,
        checks: serde_json::from_str(&checks_json)
            .map_err(|error| format!("database returned invalid self-test checks: {error}"))?,
        summary: column_text(statement, 8)?,
        evidence_path: (!evidence_path.is_empty()).then_some(evidence_path),
    };
    record.validate().map_err(str::to_string)?;
    Ok(record)
}

fn read_alert_event_row(statement: *mut SqliteStatement) -> Result<AlertEvent, String> {
    let resolved = unsafe { sqlite3_column_int64(statement, 10) };
    let acknowledged = unsafe { sqlite3_column_int64(statement, 12) };
    let acknowledged_by = column_text(statement, 13)?;
    let event = AlertEvent {
        event_id: column_text(statement, 0)?,
        condition: parse_alert_condition(&column_text(statement, 1)?)?,
        severity: parse_alert_severity(&column_text(statement, 2)?)?,
        state: parse_alert_event_state(&column_text(statement, 3)?)?,
        resource_ref: column_text(statement, 4)?,
        summary: column_text(statement, 5)?,
        detail: column_text(statement, 6)?,
        source_section: column_text(statement, 7)?,
        first_observed_at_unix_ms: unsafe { sqlite3_column_int64(statement, 8) },
        last_observed_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
        resolved_at_unix_ms: (resolved >= 0).then_some(resolved),
        occurrence_count: column_u64(statement, 11, "alert occurrence count")?,
        acknowledged_at_unix_ms: (acknowledged >= 0).then_some(acknowledged),
        acknowledged_by: (!acknowledged_by.is_empty()).then_some(acknowledged_by),
    };
    event.validate().map_err(str::to_string)?;
    Ok(event)
}

fn read_alert_rule_row(statement: *mut SqliteStatement) -> Result<AlertRule, String> {
    let channel_ids_json = column_text(statement, 4)?;
    let channel_ids = serde_json::from_str(&channel_ids_json)
        .map_err(|error| format!("database returned invalid alert rule destinations: {error}"))?;
    let scope_channel_id = column_text(statement, 7)?;
    let rule = AlertRule {
        rule_id: column_text(statement, 0)?,
        condition: parse_alert_condition(&column_text(statement, 1)?)?,
        enabled: unsafe { sqlite3_column_int64(statement, 2) } == 1,
        severity: parse_alert_severity(&column_text(statement, 3)?)?,
        channel_ids,
        dedupe_window_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 5) })
            .map_err(|_| "database returned an invalid alert-rule dedupe window".to_string())?,
        re_alert_after_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 6) })
            .map_err(|_| "database returned an invalid alert-rule re-alert window".to_string())?,
        scope_channel_id: (!scope_channel_id.is_empty()).then_some(scope_channel_id),
        notify_on_resolve: unsafe { sqlite3_column_int64(statement, 8) } == 1,
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
        updated_by: column_text(statement, 10)?,
    };
    rule.validate().map_err(str::to_string)?;
    Ok(rule)
}

fn read_alert_channel_row(statement: *mut SqliteStatement) -> Result<AlertChannel, String> {
    let quiet_start = column_text(statement, 6)?;
    let quiet_end = column_text(statement, 7)?;
    let channel = AlertChannel {
        channel_id: column_text(statement, 0)?,
        kind: parse_alert_channel_kind(&column_text(statement, 1)?)?,
        label: column_text(statement, 2)?,
        enabled: unsafe { sqlite3_column_int64(statement, 3) } == 1,
        target_redacted: column_text(statement, 4)?,
        credential_configured: unsafe { sqlite3_column_int64(statement, 5) } == 1,
        quiet_hours_start_utc: (!quiet_start.is_empty()).then_some(quiet_start),
        quiet_hours_end_utc: (!quiet_end.is_empty()).then_some(quiet_end),
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 8) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
    };
    channel.validate().map_err(str::to_string)?;
    Ok(channel)
}

fn read_alert_delivery_row(statement: *mut SqliteStatement) -> Result<AlertDelivery, String> {
    let lease_expires = unsafe { sqlite3_column_int64(statement, 7) };
    let signature = column_text(statement, 9)?;
    let sent_at = unsafe { sqlite3_column_int64(statement, 12) };
    let delivery = AlertDelivery {
        delivery_id: column_text(statement, 0)?,
        event_id: column_text(statement, 1)?,
        alert_channel_id: column_text(statement, 2)?,
        notification_kind: parse_alert_notification_kind(&column_text(statement, 3)?)?,
        state: parse_alert_delivery_state(&column_text(statement, 4)?)?,
        attempts: u32::try_from(unsafe { sqlite3_column_int64(statement, 5) })
            .map_err(|_| "database returned an invalid alert delivery attempt count".to_string())?,
        next_attempt_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        lease_expires_at_unix_ms: (lease_expires >= 0).then_some(lease_expires),
        last_error: column_text(statement, 8)?,
        signature: (!signature.is_empty()).then_some(signature),
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 10) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 11) },
        sent_at_unix_ms: (sent_at >= 0).then_some(sent_at),
    };
    delivery.validate().map_err(str::to_string)?;
    Ok(delivery)
}

fn validate_new_alert_observation(proposed: &AlertEvent) -> Result<(), String> {
    proposed.validate().map_err(str::to_string)?;
    if proposed.state != AlertEventState::Firing
        || proposed.occurrence_count != 1
        || proposed.first_observed_at_unix_ms != proposed.last_observed_at_unix_ms
        || proposed.acknowledged_at_unix_ms.is_some()
        || proposed.acknowledged_by.is_some()
    {
        return Err("a new alert observation must be an unacknowledged first firing".into());
    }
    Ok(())
}

fn scaled_metric(value: Option<f64>) -> Result<i64, String> {
    match value {
        None => Ok(-1),
        Some(value) if value.is_finite() && value >= 0.0 => {
            let scaled = value * 1_000.0;
            if scaled > i64::MAX as f64 {
                Err("system resource metric is too large".into())
            } else {
                Ok(scaled.round() as i64)
            }
        }
        Some(_) => Err("system resource metric is invalid".into()),
    }
}

fn unscaled_metric(value: i64) -> Option<f64> {
    (value >= 0).then_some(value as f64 / 1_000.0)
}

fn scaled_signed_metric(value: Option<f64>) -> Result<i64, String> {
    match value {
        None => Ok(i64::MIN),
        Some(value) if value.is_finite() => {
            let scaled = value * 1_000.0;
            if scaled <= i64::MIN as f64 || scaled > i64::MAX as f64 {
                Err("signed system resource metric is too large".into())
            } else {
                Ok(scaled.round() as i64)
            }
        }
        Some(_) => Err("signed system resource metric is invalid".into()),
    }
}

fn unscaled_signed_metric(value: i64) -> Option<f64> {
    (value != i64::MIN).then_some(value as f64 / 1_000.0)
}

fn deterministic_alert_delivery_id(
    event_id: &str,
    alert_channel_id: &str,
    notification_kind: AlertNotificationKind,
    observed_at_unix_ms: i64,
) -> String {
    let mut digest = Sha256::new();
    digest.update(event_id.as_bytes());
    digest.update([0]);
    digest.update(alert_channel_id.as_bytes());
    digest.update([0]);
    digest.update(alert_notification_kind_name(notification_kind).as_bytes());
    if notification_kind == AlertNotificationKind::Realert {
        digest.update([0]);
        digest.update(observed_at_unix_ms.to_le_bytes());
    }
    format!("alert-delivery-{:x}", digest.finalize())
}

fn deterministic_cg_expiration_audit_id(
    channel_id: &str,
    bulletin_id: &str,
    now_unix_ms: i64,
) -> String {
    let mut digest = Sha256::new();
    digest.update(b"townlight-cg-bulletin-expired");
    digest.update([0]);
    digest.update(channel_id.as_bytes());
    digest.update([0]);
    digest.update(bulletin_id.as_bytes());
    digest.update([0]);
    digest.update(now_unix_ms.to_le_bytes());
    let hex = format!("{:x}", digest.finalize());
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

fn quiet_hours_release(
    channel: &AlertChannel,
    severity: AlertSeverity,
    observed_at_unix_ms: i64,
) -> Result<i64, String> {
    if severity == AlertSeverity::Critical {
        return Ok(observed_at_unix_ms);
    }
    let (Some(start), Some(end)) = (
        channel.quiet_hours_start_utc.as_deref(),
        channel.quiet_hours_end_utc.as_deref(),
    ) else {
        return Ok(observed_at_unix_ms);
    };
    let start_ms = i64::from(parse_utc_minutes(start)?) * 60_000;
    let end_ms = i64::from(parse_utc_minutes(end)?) * 60_000;
    const DAY_MS: i64 = 86_400_000;
    let current_ms = observed_at_unix_ms.rem_euclid(DAY_MS);
    let release_delta = if start_ms < end_ms && (start_ms..end_ms).contains(&current_ms) {
        Some(end_ms - current_ms)
    } else if start_ms > end_ms && current_ms >= start_ms {
        Some(DAY_MS - current_ms + end_ms)
    } else if start_ms > end_ms && current_ms < end_ms {
        Some(end_ms - current_ms)
    } else {
        None
    };
    match release_delta {
        Some(delta) => observed_at_unix_ms
            .checked_add(delta)
            .ok_or_else(|| "quiet-hours release time overflowed".to_string()),
        None => Ok(observed_at_unix_ms),
    }
}

fn parse_utc_minutes(value: &str) -> Result<u16, String> {
    let (hours, minutes) = value
        .split_once(':')
        .ok_or_else(|| "quiet-hours clock is invalid".to_string())?;
    let hours = hours
        .parse::<u16>()
        .map_err(|_| "quiet-hours clock is invalid".to_string())?;
    let minutes = minutes
        .parse::<u16>()
        .map_err(|_| "quiet-hours clock is invalid".to_string())?;
    if hours >= 24 || minutes >= 60 {
        return Err("quiet-hours clock is invalid".into());
    }
    Ok(hours * 60 + minutes)
}

fn read_asset_loudness_row(
    statement: *mut SqliteStatement,
) -> Result<AssetLoudnessAnalysis, String> {
    let measured = unsafe { sqlite3_column_int64(statement, 2) };
    let detail = column_text(statement, 4)?;
    let analysis = AssetLoudnessAnalysis {
        asset_id: column_text(statement, 0)?,
        status: parse_loudness_status(&column_text(statement, 1)?)?,
        measured_lufs_milli: if measured == i64::from(i32::MIN) {
            None
        } else {
            Some(
                i32::try_from(measured)
                    .map_err(|_| "database returned invalid measured loudness".to_string())?,
            )
        },
        target_lufs_milli: i32::try_from(unsafe { sqlite3_column_int64(statement, 3) })
            .map_err(|_| "database returned invalid target loudness".to_string())?,
        detail: (!detail.is_empty()).then_some(detail),
    };
    analysis.validate().map_err(|error| format!("{error:?}"))?;
    Ok(analysis)
}

fn read_sink_loudness_proof_row(
    statement: *mut SqliteStatement,
) -> Result<StoredSinkLoudnessProof, String> {
    let measured = unsafe { sqlite3_column_int64(statement, 4) };
    let source_commit = column_text(statement, 13)?;
    let artifact_relative_path = column_text(statement, 18)?;
    if !artifact_relative_path.is_empty() {
        validate_artifact_relative_path(&artifact_relative_path)?;
    }
    let proof =
        SinkLoudnessProof {
            proof_id: column_text(statement, 0)?,
            channel_id: column_text(statement, 1)?,
            sink_id: column_text(statement, 2)?,
            target_lufs_milli: i32::try_from(unsafe { sqlite3_column_int64(statement, 3) })
                .map_err(|_| "database returned invalid loudness proof target".to_string())?,
            measured_lufs_milli: if measured == i64::from(i32::MIN) {
                None
            } else {
                Some(i32::try_from(measured).map_err(|_| {
                    "database returned invalid loudness proof measurement".to_string()
                })?)
            },
            tolerance_lufs_milli: i32::try_from(unsafe { sqlite3_column_int64(statement, 5) })
                .map_err(|_| "database returned invalid loudness proof tolerance".to_string())?,
            outcome: parse_loudness_proof_outcome(&column_text(statement, 6)?)?,
            receiver_acceptance: parse_receiver_acceptance(&column_text(statement, 7)?)?,
            captured_at_unix_ms: unsafe { sqlite3_column_int64(statement, 8) },
            sample_duration_ms: column_u64(statement, 9, "loudness proof sample duration")?,
            capture_bytes: column_u64(statement, 10, "loudness proof capture size")?,
            capture_sha256: column_text(statement, 11)?,
            candidate_version: column_text(statement, 12)?,
            source_commit: (!source_commit.is_empty()).then_some(source_commit),
            configuration_hash: column_text(statement, 14)?,
            measurement_engine: column_text(statement, 15)?,
            measurement_method: column_text(statement, 16)?,
            detail: column_text(statement, 17)?,
        };
    proof.validate().map_err(|error| format!("{error:?}"))?;
    Ok(StoredSinkLoudnessProof {
        proof,
        artifact_relative_path: (!artifact_relative_path.is_empty())
            .then_some(artifact_relative_path),
    })
}

fn read_output_loudness_job_row(
    statement: *mut SqliteStatement,
) -> Result<OutputLoudnessProofJobRecord, String> {
    let source_commit = column_text(statement, 6)?;
    let capture_path = column_text(statement, 14)?;
    let capture_hash = column_text(statement, 17)?;
    let max_capture_bytes = column_u64(statement, 9, "output loudness capture limit")?;
    let sample_duration_ms = column_u64(statement, 8, "output loudness sample duration")?;
    let capture = if capture_path.is_empty() {
        if unsafe { sqlite3_column_int64(statement, 15) } != -1
            || unsafe { sqlite3_column_int64(statement, 16) } != -1
            || !capture_hash.is_empty()
        {
            return Err("database returned incomplete output loudness capture facts".into());
        }
        None
    } else {
        let capture = OutputLoudnessCaptureFacts {
            artifact_relative_path: capture_path,
            capture_bytes: column_u64(statement, 15, "output loudness capture size")?,
            sample_duration_ms: column_u64(statement, 16, "output loudness capture duration")?,
            capture_sha256: capture_hash,
        };
        capture.validate(max_capture_bytes, sample_duration_ms)?;
        Some(capture)
    };
    let state = parse_output_loudness_job_state(&column_text(statement, 10)?)?;
    let completed_at = optional_nonnegative_i64(statement, 18, "output loudness completion time")?;
    let failed_at = optional_nonnegative_i64(statement, 19, "output loudness failure time")?;
    let failure_detail = column_text(statement, 20)?;
    let record = OutputLoudnessProofJobRecord {
        job_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        target_group: column_text(statement, 2)?,
        sink_id: column_text(statement, 3)?,
        target_lufs_milli: i32::try_from(unsafe { sqlite3_column_int64(statement, 4) })
            .map_err(|_| "database returned invalid output loudness target".to_string())?,
        candidate_version: column_text(statement, 5)?,
        source_commit: (!source_commit.is_empty()).then_some(source_commit),
        configuration_hash: column_text(statement, 7)?,
        sample_duration_ms,
        max_capture_bytes,
        state,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 11) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 12) },
        lease_expires_at_unix_ms: optional_nonnegative_i64(statement, 13, "output loudness lease")?,
        capture,
        completed_at_unix_ms: completed_at,
        failed_at_unix_ms: failed_at,
        failure_detail: (!failure_detail.is_empty()).then_some(failure_detail),
    };
    OutputLoudnessProofJob {
        job_id: record.job_id.clone(),
        channel_id: record.channel_id.clone(),
        target_group: record.target_group.clone(),
        sink_id: record.sink_id.clone(),
        target_lufs_milli: record.target_lufs_milli,
        candidate_version: record.candidate_version.clone(),
        source_commit: record.source_commit.clone(),
        configuration_hash: record.configuration_hash.clone(),
        sample_duration_ms: record.sample_duration_ms,
        max_capture_bytes: record.max_capture_bytes,
        created_at_unix_ms: record.created_at_unix_ms,
    }
    .validate()?;
    if record.updated_at_unix_ms < record.created_at_unix_ms {
        return Err("database returned an output loudness job updated before creation".into());
    }
    if matches!(record.state, OutputLoudnessProofJobState::Completed)
        && (record.capture.is_none() || record.completed_at_unix_ms.is_none())
    {
        return Err("completed output loudness job is missing terminal facts".into());
    }
    if matches!(record.state, OutputLoudnessProofJobState::Failed)
        && (record.failed_at_unix_ms.is_none() || record.failure_detail.is_none())
    {
        return Err("failed output loudness job is missing terminal facts".into());
    }
    if !matches!(record.state, OutputLoudnessProofJobState::Completed)
        && record.completed_at_unix_ms.is_some()
    {
        return Err("non-terminal output loudness job has completion facts".into());
    }
    if !matches!(record.state, OutputLoudnessProofJobState::Failed)
        && (record.failed_at_unix_ms.is_some() || record.failure_detail.is_some())
    {
        return Err("non-terminal output loudness job has failure facts".into());
    }
    if matches!(
        record.state,
        OutputLoudnessProofJobState::Completed | OutputLoudnessProofJobState::Failed
    ) && record.lease_expires_at_unix_ms.is_some()
    {
        return Err("terminal output loudness job still has a lease".into());
    }
    if record.capture.is_some()
        && !matches!(
            record.state,
            OutputLoudnessProofJobState::Analyzing
                | OutputLoudnessProofJobState::Completed
                | OutputLoudnessProofJobState::Failed
        )
    {
        return Err("output loudness capture facts are attached to an invalid state".into());
    }
    Ok(record)
}

fn optional_nonnegative_i64(
    statement: *mut SqliteStatement,
    column: c_int,
    field: &str,
) -> Result<Option<i64>, String> {
    let value = unsafe { sqlite3_column_int64(statement, column) };
    if value < -1 {
        return Err(format!("database returned invalid {field}"));
    }
    Ok((value >= 0).then_some(value))
}

fn read_ingest_job_row(statement: *mut SqliteStatement) -> Result<MediaIngestJob, String> {
    let started = unsafe { sqlite3_column_int64(statement, 6) };
    let completed = unsafe { sqlite3_column_int64(statement, 7) };
    let error_detail = column_text(statement, 8)?;
    let asset_id = column_text(statement, 9)?;
    let next_attempt = unsafe { sqlite3_column_int64(statement, 11) };
    Ok(MediaIngestJob {
        job_id: column_text(statement, 0)?,
        source_kind: parse_ingest_source_kind(&column_text(statement, 1)?)?,
        source_path: column_text(statement, 2)?,
        status: parse_ingest_job_status(&column_text(statement, 3)?)?,
        progress_percent: u8::try_from(unsafe { sqlite3_column_int64(statement, 4) })
            .map_err(|_| "database returned invalid ingest progress".to_string())?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        started_at_unix_ms: (started >= 0).then_some(started),
        completed_at_unix_ms: (completed >= 0).then_some(completed),
        error_detail: (!error_detail.is_empty()).then_some(error_detail),
        asset_id: (!asset_id.is_empty()).then_some(asset_id),
        attempts: u32::try_from(unsafe { sqlite3_column_int64(statement, 10) })
            .map_err(|_| "database returned invalid ingest attempts".to_string())?,
        next_attempt_at_unix_ms: (next_attempt >= 0).then_some(next_attempt),
    })
}

fn read_transcode_job_row(statement: *mut SqliteStatement) -> Result<TranscodeJob, String> {
    let started = unsafe { sqlite3_column_int64(statement, 8) };
    let completed = unsafe { sqlite3_column_int64(statement, 9) };
    let output_asset_id = column_text(statement, 10)?;
    let error_detail = column_text(statement, 11)?;
    let next_attempt = unsafe { sqlite3_column_int64(statement, 13) };
    let job = TranscodeJob {
        job_id: column_text(statement, 0)?,
        ingest_job_id: column_text(statement, 1)?,
        source_asset_id: column_text(statement, 2)?,
        source_path: column_text(statement, 3)?,
        output_format: parse_transcode_format(&column_text(statement, 4)?)?,
        status: parse_transcode_job_status(&column_text(statement, 5)?)?,
        progress_percent: u8::try_from(unsafe { sqlite3_column_int64(statement, 6) })
            .map_err(|_| "database returned invalid transcode progress".to_string())?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 7) },
        started_at_unix_ms: (started >= 0).then_some(started),
        completed_at_unix_ms: (completed >= 0).then_some(completed),
        output_asset_id: (!output_asset_id.is_empty()).then_some(output_asset_id),
        error_detail: (!error_detail.is_empty()).then_some(error_detail),
        attempts: u32::try_from(unsafe { sqlite3_column_int64(statement, 12) })
            .map_err(|_| "database returned invalid transcode attempts".to_string())?,
        next_attempt_at_unix_ms: (next_attempt >= 0).then_some(next_attempt),
    };
    job.validate().map_err(|error| format!("{error:?}"))?;
    Ok(job)
}

fn read_watch_folder_row(statement: *mut SqliteStatement) -> Result<WatchFolderConfig, String> {
    let last_scan = unsafe { sqlite3_column_int64(statement, 6) };
    let last_error = column_text(statement, 7)?;
    let config = WatchFolderConfig {
        config_id: column_text(statement, 0)?,
        display_name: column_text(statement, 1)?,
        monitor_path: column_text(statement, 2)?,
        extensions: serde_json::from_str(&column_text(statement, 3)?).map_err(|error| {
            format!("database returned invalid watch-folder extensions: {error}")
        })?,
        settle_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 4) })
            .map_err(|_| "database returned invalid watch-folder settle time".to_string())?,
        enabled: unsafe { sqlite3_column_int64(statement, 5) } == 1,
        last_scan_at_unix_ms: (last_scan >= 0).then_some(last_scan),
        last_error: (!last_error.is_empty()).then_some(last_error),
    };
    config.validate().map_err(|error| format!("{error:?}"))?;
    Ok(config)
}

fn read_watch_file_observation_row(
    statement: *mut SqliteStatement,
) -> Result<WatchFileObservation, String> {
    let queued = column_text(statement, 6)?;
    Ok(WatchFileObservation {
        config_id: column_text(statement, 0)?,
        source_path: column_text(statement, 1)?,
        size_bytes: column_u64(statement, 2, "watch-file size")?,
        modified_at_unix_ms: unsafe { sqlite3_column_int64(statement, 3) },
        unchanged_since_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        observation_count: u32::try_from(unsafe { sqlite3_column_int64(statement, 5) })
            .map_err(|_| "database returned invalid watch-file observation count".to_string())?,
        queued_job_id: (!queued.is_empty()).then_some(queued),
    })
}

fn read_schedule_item_row(statement: *mut SqliteStatement) -> Result<ScheduleItem, String> {
    let item = ScheduleItem {
        item_id: column_text(statement, 0)?,
        revision: column_u64(statement, 1, "schedule item revision")?,
        channel_id: column_text(statement, 2)?,
        asset_id: column_text(statement, 3)?,
        title: column_text(statement, 4)?,
        starts_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        duration_ms: column_u64(statement, 6, "schedule duration")?,
        state: parse_schedule_state(&column_text(statement, 7)?)?,
    };
    item.validate().map_err(|error| format!("{error:?}"))?;
    Ok(item)
}

fn read_schedule_item_history_row(
    statement: *mut SqliteStatement,
) -> Result<ScheduleItemHistoryEntry, String> {
    let item: ScheduleItem =
        serde_json::from_str(&column_text(statement, 2)?).map_err(|error| {
            format!("database returned invalid schedule item history JSON: {error}")
        })?;
    item.validate()
        .map_err(|error| format!("database returned invalid schedule item history: {error:?}"))?;
    if item.item_id != column_text(statement, 0)?
        || item.revision != column_u64(statement, 1, "schedule item history revision")?
    {
        return Err("schedule item history row and record disagree".into());
    }
    let changed_by = column_text(statement, 3)?;
    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement, 4) };
    if !secret_field(&changed_by, 200)
        || occurred_at_unix_ms < 0
        || (occurred_at_unix_ms == 0 && changed_by != "legacy-migration")
    {
        return Err("schedule item history attribution is invalid".into());
    }
    Ok(ScheduleItemHistoryEntry {
        item,
        changed_by,
        occurred_at_unix_ms,
    })
}

fn read_live_source_row(statement: *mut SqliteStatement) -> Result<LiveSource, String> {
    let source = LiveSource {
        source_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        label: column_text(statement, 2)?,
        enabled: unsafe { sqlite3_column_int64(statement, 3) } != 0,
        endpoint: station_live::LiveEndpoint::from_storage(
            &column_text(statement, 4)?,
            column_text(statement, 5)?,
            u16::try_from(unsafe { sqlite3_column_int64(statement, 6) })
                .map_err(|_| "database returned invalid live source port".to_string())?,
        )?,
        revision: u64::try_from(unsafe { sqlite3_column_int64(statement, 7) })
            .map_err(|_| "database returned invalid live source revision".to_string())?,
    };
    source.validate()?;
    Ok(source)
}

fn read_takeover_session_row(statement: *mut SqliteStatement) -> Result<TakeoverSession, String> {
    let reason = column_text(statement, 4)?;
    let failure_detail = column_text(statement, 12)?;
    let session = TakeoverSession {
        session_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        source_id: column_text(statement, 2)?,
        operator_id: column_text(statement, 3)?,
        reason: (!reason.is_empty()).then_some(reason),
        requested_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        taken_at_unix_ms: optional_i64(statement, 6),
        returned_at_unix_ms: optional_i64(statement, 7),
        source_revision: u64::try_from(unsafe { sqlite3_column_int64(statement, 8) })
            .map_err(|_| "database returned invalid source revision".to_string())?,
        authority_revision: u64::try_from(unsafe { sqlite3_column_int64(statement, 9) })
            .map_err(|_| "database returned invalid authority revision".to_string())?,
        expires_at_unix_ms: unsafe { sqlite3_column_int64(statement, 10) },
        state: TakeoverState::parse(&column_text(statement, 11)?)?,
        failure_detail: (!failure_detail.is_empty()).then_some(failure_detail),
    };
    session.validate()?;
    Ok(session)
}

fn read_takeover_audit_row(statement: *mut SqliteStatement) -> Result<TakeoverAuditEvent, String> {
    let detail = column_text(statement, 7)?;
    let audit = TakeoverAuditEvent {
        audit_id: column_text(statement, 0)?,
        session_id: column_text(statement, 1)?,
        channel_id: column_text(statement, 2)?,
        source_id: column_text(statement, 3)?,
        operator_id: column_text(statement, 4)?,
        state: TakeoverState::parse(&column_text(statement, 5)?)?,
        occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        detail: (!detail.is_empty()).then_some(detail),
    };
    audit.validate()?;
    Ok(audit)
}

fn optional_i64(statement: *mut SqliteStatement, column: c_int) -> Option<i64> {
    let value = unsafe { sqlite3_column_int64(statement, column) };
    (value >= 0).then_some(value)
}

fn read_external_receiver_acceptance_row(
    statement: *mut SqliteStatement,
) -> Result<ExternalReceiverAcceptanceReceipt, String> {
    let receipt = ExternalReceiverAcceptanceReceipt {
        receipt_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        sink_id: column_text(statement, 2)?,
        outcome: parse_receiver_acceptance_outcome(&column_text(statement, 3)?)?,
        method: parse_receiver_evidence_method(&column_text(statement, 4)?)?,
        receiver_label: column_text(statement, 5)?,
        evidence_reference: column_text(statement, 6)?,
        detail: column_text(statement, 7)?,
        observed_at_unix_ms: unsafe { sqlite3_column_int64(statement, 8) },
        recorded_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
        recorded_by: column_text(statement, 10)?,
        candidate_version: column_text(statement, 11)?,
        source_commit: column_text(statement, 12)?,
        configuration_hash: column_text(statement, 13)?,
    };
    receipt.validate()?;
    Ok(receipt)
}

fn read_commit_report_row(statement: *mut SqliteStatement) -> Result<CommitReport, String> {
    Ok(CommitReport {
        report_id: column_text(statement, 0)?,
        plan_id: column_text(statement, 1)?,
        channel_id: column_text(statement, 2)?,
        schedule_item_id: column_text(statement, 3)?,
        schedule_item_revision: column_u64(statement, 4, "commit report schedule item revision")?,
        asset_id: column_text(statement, 5)?,
        approved_by: column_text(statement, 6)?,
        approved_at_unix_ms: unsafe { sqlite3_column_int64(statement, 7) },
        dispatch_status: parse_dispatch_status(&column_text(statement, 8)?)?,
        operator_notes: column_text(statement, 9)?,
    })
}

fn column_u64(statement: *mut SqliteStatement, column: c_int, field: &str) -> Result<u64, String> {
    u64::try_from(unsafe { sqlite3_column_int64(statement, column) })
        .map_err(|_| format!("database returned an invalid {field}"))
}

fn readiness_name(value: AssetReadiness) -> &'static str {
    match value {
        AssetReadiness::Ready => "ready",
        AssetReadiness::Missing => "missing",
        AssetReadiness::Rejected => "rejected",
        AssetReadiness::Processing => "processing",
    }
}

fn cg_image_readiness_name(value: CgImageReadiness) -> &'static str {
    match value {
        CgImageReadiness::Ready => "ready",
        CgImageReadiness::Missing => "missing",
        CgImageReadiness::Rejected => "rejected",
        CgImageReadiness::Processing => "processing",
    }
}

fn cg_image_readiness_from_name(value: &str) -> Result<CgImageReadiness, String> {
    match value {
        "ready" => Ok(CgImageReadiness::Ready),
        "missing" => Ok(CgImageReadiness::Missing),
        "rejected" => Ok(CgImageReadiness::Rejected),
        "processing" => Ok(CgImageReadiness::Processing),
        _ => Err("database returned an invalid CG image readiness".into()),
    }
}

fn output_sink_kind_name(value: OutputSinkKind) -> &'static str {
    match value {
        OutputSinkKind::UdpTs => "udp-ts",
        OutputSinkKind::Srt => "srt",
    }
}

fn cg_zone_kind_name(value: CgZoneKind) -> &'static str {
    match value {
        CgZoneKind::Primary => "primary",
        CgZoneKind::Sidebar => "sidebar",
        CgZoneKind::LowerThird => "lower_third",
        CgZoneKind::Ticker => "ticker",
        CgZoneKind::Schedule => "schedule",
        CgZoneKind::Sponsor => "sponsor",
        CgZoneKind::Logo => "logo",
        CgZoneKind::Alert => "alert",
        CgZoneKind::Clock => "clock",
        CgZoneKind::LiveVideo => "live_video",
    }
}

fn parse_cg_zone_kind(value: &str) -> Result<CgZoneKind, String> {
    match value {
        "primary" => Ok(CgZoneKind::Primary),
        "sidebar" => Ok(CgZoneKind::Sidebar),
        "lower_third" => Ok(CgZoneKind::LowerThird),
        "ticker" => Ok(CgZoneKind::Ticker),
        "schedule" => Ok(CgZoneKind::Schedule),
        "sponsor" => Ok(CgZoneKind::Sponsor),
        "logo" => Ok(CgZoneKind::Logo),
        "alert" => Ok(CgZoneKind::Alert),
        "clock" => Ok(CgZoneKind::Clock),
        "live_video" => Ok(CgZoneKind::LiveVideo),
        _ => Err(format!("database returned unknown CG zone kind: {value}")),
    }
}

fn cg_feed_kind_name(value: CgFeedKind) -> &'static str {
    match value {
        CgFeedKind::Rss => "rss",
        CgFeedKind::Ical => "ical",
        CgFeedKind::Caldav => "caldav",
        CgFeedKind::Weather => "weather",
        CgFeedKind::Social => "social",
    }
}

fn parse_cg_feed_kind(value: &str) -> Result<CgFeedKind, String> {
    match value {
        "rss" => Ok(CgFeedKind::Rss),
        "ical" => Ok(CgFeedKind::Ical),
        "caldav" => Ok(CgFeedKind::Caldav),
        "weather" => Ok(CgFeedKind::Weather),
        "social" => Ok(CgFeedKind::Social),
        _ => Err(format!("database returned unknown CG feed kind: {value}")),
    }
}

fn cg_feed_trust_tier_name(value: CgFeedTrustTier) -> &'static str {
    match value {
        CgFeedTrustTier::Station => "station",
        CgFeedTrustTier::Trusted => "trusted",
        CgFeedTrustTier::Moderated => "moderated",
    }
}

fn parse_cg_feed_trust_tier(value: &str) -> Result<CgFeedTrustTier, String> {
    match value {
        "station" => Ok(CgFeedTrustTier::Station),
        "trusted" => Ok(CgFeedTrustTier::Trusted),
        "moderated" => Ok(CgFeedTrustTier::Moderated),
        _ => Err(format!(
            "database returned unknown CG feed trust tier: {value}"
        )),
    }
}

fn cg_bulletin_state_name(value: CgBulletinState) -> &'static str {
    match value {
        CgBulletinState::Submitted => "submitted",
        CgBulletinState::NeedsChanges => "needs_changes",
        CgBulletinState::Accepted => "accepted",
        CgBulletinState::Scheduled => "scheduled",
        CgBulletinState::Declined => "declined",
        CgBulletinState::Expired => "expired",
    }
}

fn parse_cg_bulletin_state(value: &str) -> Result<CgBulletinState, String> {
    match value {
        "submitted" => Ok(CgBulletinState::Submitted),
        "needs_changes" => Ok(CgBulletinState::NeedsChanges),
        "accepted" => Ok(CgBulletinState::Accepted),
        "scheduled" => Ok(CgBulletinState::Scheduled),
        "declined" => Ok(CgBulletinState::Declined),
        "expired" => Ok(CgBulletinState::Expired),
        _ => Err(format!(
            "database returned unknown CG bulletin state: {value}"
        )),
    }
}

fn valid_cg_bulletin_transition(from: CgBulletinState, to: CgBulletinState) -> bool {
    matches!(
        (from, to),
        (CgBulletinState::Submitted, CgBulletinState::Submitted)
            | (CgBulletinState::Submitted, CgBulletinState::NeedsChanges)
            | (CgBulletinState::Submitted, CgBulletinState::Accepted)
            | (CgBulletinState::Submitted, CgBulletinState::Scheduled)
            | (CgBulletinState::Submitted, CgBulletinState::Declined)
            | (CgBulletinState::NeedsChanges, CgBulletinState::Submitted)
            | (CgBulletinState::Accepted, CgBulletinState::Accepted)
            | (CgBulletinState::Accepted, CgBulletinState::Scheduled)
            | (CgBulletinState::Accepted, CgBulletinState::Declined)
            | (CgBulletinState::Accepted, CgBulletinState::Expired)
            | (CgBulletinState::Scheduled, CgBulletinState::Accepted)
            | (CgBulletinState::Scheduled, CgBulletinState::Scheduled)
            | (CgBulletinState::Scheduled, CgBulletinState::Declined)
            | (CgBulletinState::Scheduled, CgBulletinState::Expired)
    )
}

fn audit_matches_cg_bulletin_state(event_kind: CgAuditEventKind, state: CgBulletinState) -> bool {
    matches!(
        (event_kind, state),
        (
            CgAuditEventKind::BulletinSubmitted,
            CgBulletinState::Submitted
        ) | (
            CgAuditEventKind::BulletinModerated,
            CgBulletinState::NeedsChanges
                | CgBulletinState::Accepted
                | CgBulletinState::Scheduled
                | CgBulletinState::Declined
        ) | (CgAuditEventKind::BulletinExpired, CgBulletinState::Expired)
    )
}

fn cg_audit_event_kind_name(value: CgAuditEventKind) -> &'static str {
    match value {
        CgAuditEventKind::BoardCreated => "board_created",
        CgAuditEventKind::BoardUpdated => "board_updated",
        CgAuditEventKind::ZoneCreated => "zone_created",
        CgAuditEventKind::ZoneUpdated => "zone_updated",
        CgAuditEventKind::ZoneRemoved => "zone_removed",
        CgAuditEventKind::FeedCreated => "feed_created",
        CgAuditEventKind::FeedUpdated => "feed_updated",
        CgAuditEventKind::FeedRemoved => "feed_removed",
        CgAuditEventKind::BulletinSubmitted => "bulletin_submitted",
        CgAuditEventKind::BulletinModerated => "bulletin_moderated",
        CgAuditEventKind::BulletinExpired => "bulletin_expired",
        CgAuditEventKind::FeedItemApproved => "feed_item_approved",
    }
}

fn parse_cg_audit_event_kind(value: &str) -> Result<CgAuditEventKind, String> {
    match value {
        "board_created" => Ok(CgAuditEventKind::BoardCreated),
        "board_updated" => Ok(CgAuditEventKind::BoardUpdated),
        "zone_created" => Ok(CgAuditEventKind::ZoneCreated),
        "zone_updated" => Ok(CgAuditEventKind::ZoneUpdated),
        "zone_removed" => Ok(CgAuditEventKind::ZoneRemoved),
        "feed_created" => Ok(CgAuditEventKind::FeedCreated),
        "feed_updated" => Ok(CgAuditEventKind::FeedUpdated),
        "feed_removed" => Ok(CgAuditEventKind::FeedRemoved),
        "bulletin_submitted" => Ok(CgAuditEventKind::BulletinSubmitted),
        "bulletin_moderated" => Ok(CgAuditEventKind::BulletinModerated),
        "bulletin_expired" => Ok(CgAuditEventKind::BulletinExpired),
        "feed_item_approved" => Ok(CgAuditEventKind::FeedItemApproved),
        _ => Err(format!(
            "database returned unknown CG audit event kind: {value}"
        )),
    }
}

fn parse_output_sink_kind(value: &str) -> Result<OutputSinkKind, String> {
    match value {
        "udp-ts" => Ok(OutputSinkKind::UdpTs),
        "srt" => Ok(OutputSinkKind::Srt),
        _ => Err(format!("unknown output sink kind: {value}")),
    }
}

fn channel_runtime_state_name(value: ChannelRuntimeState) -> &'static str {
    match value {
        ChannelRuntimeState::Starting => "starting",
        ChannelRuntimeState::HealthyFallback => "healthy_fallback",
        ChannelRuntimeState::OnAir => "on_air",
        ChannelRuntimeState::Live => "live",
        ChannelRuntimeState::Error => "error",
        ChannelRuntimeState::Stopped => "stopped",
    }
}

fn alert_channel_kind_name(value: AlertChannelKind) -> &'static str {
    match value {
        AlertChannelKind::Email => "email",
        AlertChannelKind::Sms => "sms",
        AlertChannelKind::Webhook => "webhook",
    }
}

fn parse_alert_channel_kind(value: &str) -> Result<AlertChannelKind, String> {
    match value {
        "email" => Ok(AlertChannelKind::Email),
        "sms" => Ok(AlertChannelKind::Sms),
        "webhook" => Ok(AlertChannelKind::Webhook),
        _ => Err(format!(
            "database returned invalid alert channel kind {value}"
        )),
    }
}

fn alert_notification_kind_name(value: AlertNotificationKind) -> &'static str {
    match value {
        AlertNotificationKind::Firing => "firing",
        AlertNotificationKind::Realert => "realert",
        AlertNotificationKind::Resolved => "resolved",
        AlertNotificationKind::Test => "test",
    }
}

fn parse_alert_notification_kind(value: &str) -> Result<AlertNotificationKind, String> {
    match value {
        "firing" => Ok(AlertNotificationKind::Firing),
        "realert" => Ok(AlertNotificationKind::Realert),
        "resolved" => Ok(AlertNotificationKind::Resolved),
        "test" => Ok(AlertNotificationKind::Test),
        _ => Err(format!(
            "database returned invalid alert notification kind {value}"
        )),
    }
}

fn parse_alert_delivery_state(value: &str) -> Result<AlertDeliveryState, String> {
    match value {
        "pending" => Ok(AlertDeliveryState::Pending),
        "leased" => Ok(AlertDeliveryState::Leased),
        "sent" => Ok(AlertDeliveryState::Sent),
        "dead_letter" => Ok(AlertDeliveryState::DeadLetter),
        _ => Err(format!(
            "database returned invalid alert delivery state {value}"
        )),
    }
}

fn parse_channel_runtime_state(value: &str) -> Result<ChannelRuntimeState, String> {
    match value {
        "starting" => Ok(ChannelRuntimeState::Starting),
        "healthy_fallback" => Ok(ChannelRuntimeState::HealthyFallback),
        "on_air" => Ok(ChannelRuntimeState::OnAir),
        "live" => Ok(ChannelRuntimeState::Live),
        "error" => Ok(ChannelRuntimeState::Error),
        "stopped" => Ok(ChannelRuntimeState::Stopped),
        _ => Err(format!(
            "database returned invalid channel runtime state {value}"
        )),
    }
}

fn self_test_kind_name(value: SelfTestKind) -> &'static str {
    match value {
        SelfTestKind::Daily => "daily",
        SelfTestKind::Weekly => "weekly",
    }
}

fn parse_self_test_kind(value: &str) -> Result<SelfTestKind, String> {
    match value {
        "daily" => Ok(SelfTestKind::Daily),
        "weekly" => Ok(SelfTestKind::Weekly),
        _ => Err(format!("database returned invalid self-test kind {value}")),
    }
}

fn self_test_trigger_name(value: SelfTestTrigger) -> &'static str {
    match value {
        SelfTestTrigger::Scheduled => "scheduled",
        SelfTestTrigger::Manual => "manual",
    }
}

fn parse_self_test_trigger(value: &str) -> Result<SelfTestTrigger, String> {
    match value {
        "scheduled" => Ok(SelfTestTrigger::Scheduled),
        "manual" => Ok(SelfTestTrigger::Manual),
        _ => Err(format!(
            "database returned invalid self-test trigger {value}"
        )),
    }
}

fn self_test_status_name(value: SelfTestStatus) -> &'static str {
    match value {
        SelfTestStatus::Pass => "pass",
        SelfTestStatus::Warn => "warn",
        SelfTestStatus::Fail => "fail",
    }
}

fn update_check_trigger_name(value: UpdateCheckTrigger) -> &'static str {
    match value {
        UpdateCheckTrigger::Automatic => "automatic",
        UpdateCheckTrigger::Manual => "manual",
    }
}

fn update_check_outcome_name(value: UpdateCheckOutcome) -> &'static str {
    match value {
        UpdateCheckOutcome::UpToDate => "up_to_date",
        UpdateCheckOutcome::UpdateAvailable => "update_available",
        UpdateCheckOutcome::UpdaterTooOld => "updater_too_old",
        UpdateCheckOutcome::Failed => "failed",
    }
}

fn parse_self_test_status(value: &str) -> Result<SelfTestStatus, String> {
    match value {
        "pass" => Ok(SelfTestStatus::Pass),
        "warn" => Ok(SelfTestStatus::Warn),
        "fail" => Ok(SelfTestStatus::Fail),
        _ => Err(format!(
            "database returned invalid self-test status {value}"
        )),
    }
}

fn alert_condition_name(value: AlertConditionKind) -> &'static str {
    match value {
        AlertConditionKind::OffAir => "off-air",
        AlertConditionKind::OutputStall => "output-stall",
        AlertConditionKind::EncoderDeath => "encoder-death",
        AlertConditionKind::ServerCrash => "server-crash",
        AlertConditionKind::SchemaDrift => "schema-drift",
        AlertConditionKind::RelayBlocked => "relay-blocked",
        AlertConditionKind::ComplianceProbeFail => "compliance-probe-fail",
        AlertConditionKind::MissingMedia => "missing-media",
        AlertConditionKind::ScheduleNoMatch => "schedule-no-match",
        AlertConditionKind::CommitFailure => "commit-failure",
        AlertConditionKind::TakeoverStuck2h => "takeover-stuck-2h",
        AlertConditionKind::AiRuntimeDown => "ai-runtime-down",
        AlertConditionKind::DiskLow => "disk-low",
        AlertConditionKind::ClockSkew => "clock-skew",
        AlertConditionKind::DatabaseUnreachable => "database-unreachable",
        AlertConditionKind::ServiceDown => "service-down",
        AlertConditionKind::SelfTestFail => "self-test-fail",
        AlertConditionKind::UpdateAvailable => "update-available",
        AlertConditionKind::UpdateCheckFailed => "update-check-failed",
        AlertConditionKind::UpdateApplyFailed => "update-apply-failed",
        AlertConditionKind::ScheduleMaterializationFailure => "schedule-materialization-failure",
    }
}

fn parse_alert_condition(value: &str) -> Result<AlertConditionKind, String> {
    match value {
        "off-air" => Ok(AlertConditionKind::OffAir),
        "output-stall" => Ok(AlertConditionKind::OutputStall),
        "encoder-death" => Ok(AlertConditionKind::EncoderDeath),
        "server-crash" => Ok(AlertConditionKind::ServerCrash),
        "schema-drift" => Ok(AlertConditionKind::SchemaDrift),
        "relay-blocked" => Ok(AlertConditionKind::RelayBlocked),
        "compliance-probe-fail" => Ok(AlertConditionKind::ComplianceProbeFail),
        "missing-media" => Ok(AlertConditionKind::MissingMedia),
        "schedule-no-match" => Ok(AlertConditionKind::ScheduleNoMatch),
        "commit-failure" => Ok(AlertConditionKind::CommitFailure),
        "takeover-stuck-2h" => Ok(AlertConditionKind::TakeoverStuck2h),
        "ai-runtime-down" => Ok(AlertConditionKind::AiRuntimeDown),
        "disk-low" => Ok(AlertConditionKind::DiskLow),
        "clock-skew" => Ok(AlertConditionKind::ClockSkew),
        "database-unreachable" => Ok(AlertConditionKind::DatabaseUnreachable),
        "service-down" => Ok(AlertConditionKind::ServiceDown),
        "self-test-fail" => Ok(AlertConditionKind::SelfTestFail),
        "update-available" => Ok(AlertConditionKind::UpdateAvailable),
        "update-check-failed" => Ok(AlertConditionKind::UpdateCheckFailed),
        "update-apply-failed" => Ok(AlertConditionKind::UpdateApplyFailed),
        "schedule-materialization-failure" => {
            Ok(AlertConditionKind::ScheduleMaterializationFailure)
        }
        _ => Err(format!("database returned invalid alert condition {value}")),
    }
}

fn default_alert_rules() -> Vec<AlertRule> {
    let definitions = [
        (AlertConditionKind::OffAir, AlertSeverity::Critical, 3_600),
        (
            AlertConditionKind::OutputStall,
            AlertSeverity::Critical,
            3_600,
        ),
        (
            AlertConditionKind::EncoderDeath,
            AlertSeverity::Critical,
            3_600,
        ),
        (AlertConditionKind::ServerCrash, AlertSeverity::Critical, 0),
        (
            AlertConditionKind::SchemaDrift,
            AlertSeverity::Warning,
            21_600,
        ),
        (
            AlertConditionKind::RelayBlocked,
            AlertSeverity::Critical,
            1_800,
        ),
        (
            AlertConditionKind::ComplianceProbeFail,
            AlertSeverity::Warning,
            3_600,
        ),
        (AlertConditionKind::MissingMedia, AlertSeverity::Critical, 0),
        (
            AlertConditionKind::ScheduleNoMatch,
            AlertSeverity::Warning,
            21_600,
        ),
        (
            AlertConditionKind::CommitFailure,
            AlertSeverity::Warning,
            1_800,
        ),
        (
            AlertConditionKind::TakeoverStuck2h,
            AlertSeverity::Warning,
            7_200,
        ),
        (
            AlertConditionKind::AiRuntimeDown,
            AlertSeverity::Info,
            21_600,
        ),
        (AlertConditionKind::DiskLow, AlertSeverity::Warning, 3_600),
        (AlertConditionKind::ClockSkew, AlertSeverity::Warning, 3_600),
        (
            AlertConditionKind::DatabaseUnreachable,
            AlertSeverity::Critical,
            1_800,
        ),
        (
            AlertConditionKind::ServiceDown,
            AlertSeverity::Critical,
            1_800,
        ),
        (
            AlertConditionKind::SelfTestFail,
            AlertSeverity::Warning,
            86_400,
        ),
        (
            AlertConditionKind::UpdateAvailable,
            AlertSeverity::Info,
            86_400,
        ),
        (
            AlertConditionKind::UpdateCheckFailed,
            AlertSeverity::Warning,
            21_600,
        ),
        (
            AlertConditionKind::UpdateApplyFailed,
            AlertSeverity::Critical,
            3_600,
        ),
        (
            AlertConditionKind::ScheduleMaterializationFailure,
            AlertSeverity::Warning,
            1_800,
        ),
    ];
    definitions
        .into_iter()
        .map(|(condition, severity, re_alert_after_seconds)| AlertRule {
            rule_id: format!("default-{}", alert_condition_name(condition)),
            condition,
            enabled: true,
            severity,
            channel_ids: Vec::new(),
            dedupe_window_seconds: 900,
            re_alert_after_seconds,
            scope_channel_id: None,
            notify_on_resolve: true,
            updated_at_unix_ms: 0,
            updated_by: "system-seed".into(),
        })
        .collect()
}

fn schedule_materialization_failure_detail(
    failure_class: ScheduleMaterializationFailureClass,
) -> &'static str {
    match failure_class {
        ScheduleMaterializationFailureClass::ChannelDisabled => {
            "Typed automatic materialization failure class: channel-disabled."
        }
        ScheduleMaterializationFailureClass::StaleAuthority => {
            "Typed automatic materialization failure class: stale-authority."
        }
        ScheduleMaterializationFailureClass::InvalidDerivedInput => {
            "Typed automatic materialization failure class: invalid-derived-input."
        }
        ScheduleMaterializationFailureClass::Capacity => {
            "Typed automatic materialization failure class: capacity."
        }
        ScheduleMaterializationFailureClass::Integrity => {
            "Typed automatic materialization failure class: integrity."
        }
        ScheduleMaterializationFailureClass::Storage => {
            "Typed automatic materialization failure class: storage."
        }
    }
}

fn alert_severity_name(value: AlertSeverity) -> &'static str {
    match value {
        AlertSeverity::Critical => "critical",
        AlertSeverity::Warning => "warning",
        AlertSeverity::Info => "info",
    }
}

fn parse_alert_severity(value: &str) -> Result<AlertSeverity, String> {
    match value {
        "critical" => Ok(AlertSeverity::Critical),
        "warning" => Ok(AlertSeverity::Warning),
        "info" => Ok(AlertSeverity::Info),
        _ => Err(format!("database returned invalid alert severity {value}")),
    }
}

fn alert_event_state_name(value: AlertEventState) -> &'static str {
    match value {
        AlertEventState::Firing => "firing",
        AlertEventState::Resolved => "resolved",
    }
}

fn parse_alert_event_state(value: &str) -> Result<AlertEventState, String> {
    match value {
        "firing" => Ok(AlertEventState::Firing),
        "resolved" => Ok(AlertEventState::Resolved),
        _ => Err(format!(
            "database returned invalid alert event state {value}"
        )),
    }
}

fn loudness_status_name(value: LoudnessGateStatus) -> &'static str {
    match value {
        LoudnessGateStatus::Ok => "ok",
        LoudnessGateStatus::Failed => "failed",
        LoudnessGateStatus::NotChecked => "not_checked",
    }
}

fn parse_loudness_status(value: &str) -> Result<LoudnessGateStatus, String> {
    match value {
        "ok" => Ok(LoudnessGateStatus::Ok),
        "failed" => Ok(LoudnessGateStatus::Failed),
        "not_checked" => Ok(LoudnessGateStatus::NotChecked),
        _ => Err(format!("database returned invalid loudness status {value}")),
    }
}

fn outcome_name(value: LoudnessProofOutcome) -> &'static str {
    match value {
        LoudnessProofOutcome::Pass => "pass",
        LoudnessProofOutcome::Fail => "fail",
        LoudnessProofOutcome::NotRun => "not_run",
        LoudnessProofOutcome::Invalid => "invalid",
    }
}

fn parse_loudness_proof_outcome(value: &str) -> Result<LoudnessProofOutcome, String> {
    match value {
        "pass" => Ok(LoudnessProofOutcome::Pass),
        "fail" => Ok(LoudnessProofOutcome::Fail),
        "not_run" => Ok(LoudnessProofOutcome::NotRun),
        "invalid" => Ok(LoudnessProofOutcome::Invalid),
        _ => Err(format!(
            "database returned invalid loudness proof outcome {value}"
        )),
    }
}

fn receiver_acceptance_outcome_name(value: ReceiverAcceptanceOutcome) -> &'static str {
    match value {
        ReceiverAcceptanceOutcome::Accepted => "accepted",
        ReceiverAcceptanceOutcome::Rejected => "rejected",
    }
}

fn parse_receiver_acceptance_outcome(value: &str) -> Result<ReceiverAcceptanceOutcome, String> {
    match value {
        "accepted" => Ok(ReceiverAcceptanceOutcome::Accepted),
        "rejected" => Ok(ReceiverAcceptanceOutcome::Rejected),
        _ => Err("external receiver receipt outcome is invalid".into()),
    }
}

fn receiver_evidence_method_name(value: ReceiverEvidenceMethod) -> &'static str {
    match value {
        ReceiverEvidenceMethod::HeadendOperatorConfirmation => "headend_operator_confirmation",
        ReceiverEvidenceMethod::ReceiverTelemetry => "receiver_telemetry",
        ReceiverEvidenceMethod::ReturnPathObservation => "return_path_observation",
        ReceiverEvidenceMethod::VisualAudioObservation => "visual_audio_observation",
    }
}

fn parse_receiver_evidence_method(value: &str) -> Result<ReceiverEvidenceMethod, String> {
    match value {
        "headend_operator_confirmation" => Ok(ReceiverEvidenceMethod::HeadendOperatorConfirmation),
        "receiver_telemetry" => Ok(ReceiverEvidenceMethod::ReceiverTelemetry),
        "return_path_observation" => Ok(ReceiverEvidenceMethod::ReturnPathObservation),
        "visual_audio_observation" => Ok(ReceiverEvidenceMethod::VisualAudioObservation),
        _ => Err("external receiver receipt evidence method is invalid".into()),
    }
}

fn receiver_acceptance_name(value: ReceiverAcceptance) -> &'static str {
    match value {
        ReceiverAcceptance::NotAttempted => "not_attempted",
        ReceiverAcceptance::Unknown => "unknown",
        ReceiverAcceptance::Verified => "verified",
        ReceiverAcceptance::Rejected => "rejected",
    }
}

fn parse_receiver_acceptance(value: &str) -> Result<ReceiverAcceptance, String> {
    match value {
        "not_attempted" => Ok(ReceiverAcceptance::NotAttempted),
        "unknown" => Ok(ReceiverAcceptance::Unknown),
        "verified" => Ok(ReceiverAcceptance::Verified),
        "rejected" => Ok(ReceiverAcceptance::Rejected),
        _ => Err(format!(
            "database returned invalid loudness proof receiver acceptance {value}"
        )),
    }
}

fn output_loudness_job_state_name(value: OutputLoudnessProofJobState) -> &'static str {
    match value {
        OutputLoudnessProofJobState::Pending => "pending",
        OutputLoudnessProofJobState::Claimed => "claimed",
        OutputLoudnessProofJobState::CapturePending => "capture_pending",
        OutputLoudnessProofJobState::Analyzing => "analyzing",
        OutputLoudnessProofJobState::Completed => "completed",
        OutputLoudnessProofJobState::Failed => "failed",
    }
}

fn parse_output_loudness_job_state(value: &str) -> Result<OutputLoudnessProofJobState, String> {
    match value {
        "pending" => Ok(OutputLoudnessProofJobState::Pending),
        "claimed" => Ok(OutputLoudnessProofJobState::Claimed),
        "capture_pending" => Ok(OutputLoudnessProofJobState::CapturePending),
        "analyzing" => Ok(OutputLoudnessProofJobState::Analyzing),
        "completed" => Ok(OutputLoudnessProofJobState::Completed),
        "failed" => Ok(OutputLoudnessProofJobState::Failed),
        _ => Err(format!(
            "database returned invalid output loudness job state {value}"
        )),
    }
}

fn valid_output_loudness_transition(
    expected: OutputLoudnessProofJobState,
    next: OutputLoudnessProofJobState,
) -> bool {
    matches!(
        (expected, next),
        (
            OutputLoudnessProofJobState::Claimed,
            OutputLoudnessProofJobState::CapturePending
        ) | (
            OutputLoudnessProofJobState::CapturePending,
            OutputLoudnessProofJobState::Analyzing
        ) | (
            OutputLoudnessProofJobState::Analyzing,
            OutputLoudnessProofJobState::Completed
        )
    )
}

fn validate_output_loudness_lease(now_unix_ms: i64, lease_duration_ms: i64) -> Result<(), String> {
    if now_unix_ms < 0 || !(1_000..=300_000).contains(&lease_duration_ms) {
        return Err("output loudness lease request is invalid".into());
    }
    Ok(())
}

fn validate_output_loudness_job_id(value: &str) -> Result<(), String> {
    if is_canonical_uuid(value) {
        Ok(())
    } else {
        Err("output loudness job identity must be a canonical UUID".into())
    }
}

fn recover_expired_output_loudness_jobs_in_transaction(
    store: &StationStore,
    now_unix_ms: i64,
) -> Result<u64, String> {
    let statement = store.prepare(
        "UPDATE output_loudness_jobs SET state='pending', updated_at_unix_ms=?1, lease_expires_at_unix_ms=-1, capture_relative_path='', capture_bytes=-1, capture_sample_duration_ms=-1, capture_sha256='' WHERE state IN ('claimed','capture_pending','analyzing') AND lease_expires_at_unix_ms<=?1",
    )?;
    bind_i64(store.database, statement.raw, 1, now_unix_ms)?;
    expect_done(store.database, statement.raw)?;
    Ok(store.changes() as u64)
}

fn recover_expired_caption_egress_jobs_in_transaction(
    store: &StationStore,
    now_unix_ms: i64,
) -> Result<u64, String> {
    let statement = store.prepare(
        "UPDATE caption_egress_jobs SET state='pending', updated_at_unix_ms=?1, lease_expires_at_unix_ms=-1, capture_json='' WHERE state IN ('claimed','capture_pending','analyzing') AND lease_expires_at_unix_ms<=?1",
    )?;
    bind_i64(store.database, statement.raw, 1, now_unix_ms)?;
    expect_done(store.database, statement.raw)?;
    Ok(store.changes() as u64)
}

fn caption_egress_job_state_name(state: CaptionEgressProofJobState) -> &'static str {
    match state {
        CaptionEgressProofJobState::Pending => "pending",
        CaptionEgressProofJobState::Claimed => "claimed",
        CaptionEgressProofJobState::CapturePending => "capture_pending",
        CaptionEgressProofJobState::Analyzing => "analyzing",
        CaptionEgressProofJobState::Completed => "completed",
        CaptionEgressProofJobState::Failed => "failed",
    }
}

fn caption_egress_job_state(value: &str) -> Result<CaptionEgressProofJobState, String> {
    match value {
        "pending" => Ok(CaptionEgressProofJobState::Pending),
        "claimed" => Ok(CaptionEgressProofJobState::Claimed),
        "capture_pending" => Ok(CaptionEgressProofJobState::CapturePending),
        "analyzing" => Ok(CaptionEgressProofJobState::Analyzing),
        "completed" => Ok(CaptionEgressProofJobState::Completed),
        "failed" => Ok(CaptionEgressProofJobState::Failed),
        _ => Err("database returned an invalid caption proof job state".into()),
    }
}

fn read_caption_egress_job_row(
    statement: *mut SqliteStatement,
) -> Result<CaptionEgressProofJobRecord, String> {
    let job_json = column_text(statement, 0)?;
    let job: CaptionEgressProofJob = serde_json::from_str(&job_json)
        .map_err(|error| format!("decode caption proof job: {error}"))?;
    job.validate()?;
    let state = caption_egress_job_state(&column_text(statement, 1)?)?;
    let updated_at_unix_ms = unsafe { sqlite3_column_int64(statement, 2) };
    let lease = unsafe { sqlite3_column_int64(statement, 3) };
    let capture_json = column_text(statement, 4)?;
    let capture = if capture_json.is_empty() {
        None
    } else {
        let capture: CaptionProofCaptureFacts = serde_json::from_str(&capture_json)
            .map_err(|error| format!("decode caption proof capture: {error}"))?;
        capture.validate(&job)?;
        Some(capture)
    };
    let completed = unsafe { sqlite3_column_int64(statement, 5) };
    let failed = unsafe { sqlite3_column_int64(statement, 6) };
    let failure_detail = column_text(statement, 7)?;
    Ok(CaptionEgressProofJobRecord {
        job,
        state,
        updated_at_unix_ms,
        lease_expires_at_unix_ms: (lease >= 0).then_some(lease),
        capture,
        completed_at_unix_ms: (completed >= 0).then_some(completed),
        failed_at_unix_ms: (failed >= 0).then_some(failed),
        failure_detail: (!failure_detail.is_empty()).then_some(failure_detail),
    })
}

fn ingest_source_kind_name(value: IngestSourceKind) -> &'static str {
    match value {
        IngestSourceKind::OperatorPath => "operator_path",
        IngestSourceKind::WatchFolder => "watch_folder",
        IngestSourceKind::LiveFinalization => "live_finalization",
    }
}

fn parse_ingest_source_kind(value: &str) -> Result<IngestSourceKind, String> {
    match value {
        "operator_path" => Ok(IngestSourceKind::OperatorPath),
        "watch_folder" => Ok(IngestSourceKind::WatchFolder),
        "live_finalization" => Ok(IngestSourceKind::LiveFinalization),
        _ => Err(format!(
            "database returned invalid ingest source kind {value}"
        )),
    }
}

fn parse_ingest_job_status(value: &str) -> Result<IngestJobStatus, String> {
    match value {
        "pending" => Ok(IngestJobStatus::Pending),
        "running" => Ok(IngestJobStatus::Running),
        "awaiting_transcode" => Ok(IngestJobStatus::AwaitingTranscode),
        "completed" => Ok(IngestJobStatus::Completed),
        "failed" => Ok(IngestJobStatus::Failed),
        "cancelled" => Ok(IngestJobStatus::Cancelled),
        _ => Err(format!(
            "database returned invalid ingest job status {value}"
        )),
    }
}

fn transcode_format_name(value: TranscodeFormat) -> &'static str {
    match value {
        TranscodeFormat::H264_720p4mbpsAac192kTs => "h264_720p4mbps_aac192k_ts",
    }
}

fn parse_transcode_format(value: &str) -> Result<TranscodeFormat, String> {
    match value {
        "h264_720p4mbps_aac192k_ts" => Ok(TranscodeFormat::H264_720p4mbpsAac192kTs),
        _ => Err(format!(
            "database returned invalid transcode format {value}"
        )),
    }
}

fn parse_transcode_job_status(value: &str) -> Result<TranscodeJobStatus, String> {
    match value {
        "pending" => Ok(TranscodeJobStatus::Pending),
        "running" => Ok(TranscodeJobStatus::Running),
        "completed" => Ok(TranscodeJobStatus::Completed),
        "failed" => Ok(TranscodeJobStatus::Failed),
        "cancelled" => Ok(TranscodeJobStatus::Cancelled),
        _ => Err(format!(
            "database returned invalid transcode job status {value}"
        )),
    }
}

trait ValidateAutomationRecord {
    fn validate_automation_record(&self) -> Result<(), String>;
}

impl ValidateAutomationRecord for AutomationAdapterDescriptor {
    fn validate_automation_record(&self) -> Result<(), String> {
        self.validate()
    }
}

impl ValidateAutomationRecord for AutomationFeaturePolicy {
    fn validate_automation_record(&self) -> Result<(), String> {
        self.validate()
    }
}

impl ValidateAutomationRecord for AutomationConnectionTestEvidence {
    fn validate_automation_record(&self) -> Result<(), String> {
        self.validate()
    }
}

fn read_automation_json<T>(record_json: String, record_name: &str) -> Result<T, String>
where
    T: DeserializeOwned + ValidateAutomationRecord,
{
    let record = serde_json::from_str::<T>(&record_json)
        .map_err(|error| format!("database returned invalid {record_name} JSON: {error}"))?;
    record
        .validate_automation_record()
        .map_err(|error| format!("database returned invalid {record_name}: {error}"))?;
    Ok(record)
}

fn read_custom_metadata_schema_row(
    statement: *mut SqliteStatement,
    schema_id_column: c_int,
    revision_column: c_int,
    json_column: c_int,
) -> Result<MetadataSchema, String> {
    let schema: MetadataSchema = serde_json::from_str(&column_text(statement, json_column)?)
        .map_err(|error| {
            format!("database returned invalid custom metadata schema JSON: {error}")
        })?;
    schema
        .validate()
        .map_err(|error| format!("database returned invalid custom metadata schema: {error}"))?;
    if schema.schema_id != column_text(statement, schema_id_column)?
        || schema.revision
            != column_u64(
                statement,
                revision_column,
                "custom metadata schema revision",
            )?
    {
        return Err("custom metadata schema row and record disagree".into());
    }
    Ok(schema)
}

fn read_asset_metadata_record_row(
    statement: *mut SqliteStatement,
    metadata_id_column: c_int,
    asset_id_column: c_int,
    schema_id_column: c_int,
    schema_revision_column: c_int,
    revision_column: c_int,
    json_column: c_int,
) -> Result<AssetMetadataRecord, String> {
    let record: AssetMetadataRecord = serde_json::from_str(&column_text(statement, json_column)?)
        .map_err(|error| {
        format!("database returned invalid asset metadata record JSON: {error}")
    })?;
    record
        .validate_shape()
        .map_err(|error| format!("database returned invalid asset metadata record: {error}"))?;
    if record.metadata_id != column_text(statement, metadata_id_column)?
        || record.asset_id != column_text(statement, asset_id_column)?
        || record.schema_id != column_text(statement, schema_id_column)?
        || record.schema_revision
            != column_u64(
                statement,
                schema_revision_column,
                "asset metadata schema revision",
            )?
        || record.revision != column_u64(statement, revision_column, "asset metadata revision")?
    {
        return Err("asset metadata record row and record disagree".into());
    }
    Ok(record)
}

fn validate_automation_revision(
    current_revision: Option<u64>,
    incoming_revision: u64,
    expected_revision: Option<u64>,
) -> Result<(), String> {
    match (current_revision, expected_revision) {
        (None, None) if incoming_revision == 1 => Ok(()),
        (Some(current), Some(expected))
            if current == expected && incoming_revision == current.saturating_add(1) =>
        {
            Ok(())
        }
        _ => Err("automation authority revision conflict".into()),
    }
}

fn validate_automation_policy_capabilities(
    policy: &AutomationFeaturePolicy,
    adapters: &[AutomationAdapterDescriptor],
) -> Result<(), String> {
    let supports_feature = |adapter_id: &str| {
        adapters.iter().any(|adapter| {
            adapter.adapter_id == adapter_id
                && adapter.validate().is_ok()
                && adapter.supported_features.contains(&policy.feature)
        })
    };
    if policy
        .operator_selected_adapter_id
        .as_deref()
        .is_some_and(|adapter_id| !supports_feature(adapter_id))
    {
        return Err("operator-selected automation adapter is missing or unsupported".into());
    }
    if let Some(hint) = &policy.adaptive_default
        && (!supports_feature(&hint.preferred_adapter_id)
            || !supports_feature(&hint.fallback_adapter_id))
    {
        return Err("adaptive automation adapter capability is missing or unsupported".into());
    }
    Ok(())
}

fn automation_feature_name(feature: AutomationFeature) -> &'static str {
    match feature {
        AutomationFeature::Captions => "captions",
        AutomationFeature::Summaries => "summaries",
        AutomationFeature::Translation => "translation",
    }
}

fn scheduled_recording_lifecycle_name(lifecycle: &RecordingLifecycle) -> &'static str {
    match lifecycle {
        RecordingLifecycle::Draft => "draft",
        RecordingLifecycle::Armed { .. } => "armed",
        RecordingLifecycle::Capturing { .. } => "capturing",
        RecordingLifecycle::Finalizing { .. } => "finalizing",
        RecordingLifecycle::Completed { .. } => "completed",
        RecordingLifecycle::Failed { .. } => "failed",
        RecordingLifecycle::CancelledBeforeArm { .. } => "cancelled_before_arm",
        RecordingLifecycle::CancelledAfterArm { .. } => "cancelled_after_arm",
    }
}

fn scheduled_recording_transition_kind_name(kind: RecordingTransitionKind) -> &'static str {
    match kind {
        RecordingTransitionKind::Arm => "arm",
        RecordingTransitionKind::StartCapture => "start_capture",
        RecordingTransitionKind::BeginFinalization => "begin_finalization",
        RecordingTransitionKind::RecoverForFinalization => "recover_for_finalization",
        RecordingTransitionKind::FinalizeSuccess => "finalize_success",
        RecordingTransitionKind::Fail => "fail",
        RecordingTransitionKind::Cancel => "cancel",
    }
}

fn parse_scheduled_recording_actor_role(value: &str) -> Result<RecordingActorRole, String> {
    match value {
        "operator" => Ok(RecordingActorRole::Operator),
        "supervisor" => Ok(RecordingActorRole::Supervisor),
        "recording_worker" => Ok(RecordingActorRole::RecordingWorker),
        _ => Err("database returned an invalid scheduled recording actor role".into()),
    }
}

fn scheduled_recording_actor_role_name(role: RecordingActorRole) -> &'static str {
    match role {
        RecordingActorRole::Operator => "operator",
        RecordingActorRole::Supervisor => "supervisor",
        RecordingActorRole::RecordingWorker => "recording_worker",
    }
}

fn scheduled_recording_execution(
    recording: &ScheduledRecording,
) -> Option<&RecordingExecutionAuthority> {
    match &recording.lifecycle {
        RecordingLifecycle::Draft | RecordingLifecycle::CancelledBeforeArm { .. } => None,
        RecordingLifecycle::Armed { execution, .. }
        | RecordingLifecycle::Capturing { execution, .. }
        | RecordingLifecycle::Finalizing { execution, .. }
        | RecordingLifecycle::Completed { execution, .. }
        | RecordingLifecycle::Failed { execution, .. }
        | RecordingLifecycle::CancelledAfterArm { execution, .. } => Some(execution),
    }
}

fn read_scheduled_recording_json(
    record_json: String,
    record_name: &str,
) -> Result<ScheduledRecording, String> {
    let recording = serde_json::from_str::<ScheduledRecording>(&record_json)
        .map_err(|error| format!("database returned invalid {record_name} JSON: {error}"))?;
    recording
        .validate()
        .map_err(|error| format!("database returned invalid {record_name}: {error}"))?;
    Ok(recording)
}

fn read_scheduled_recording_row(
    statement: *mut SqliteStatement,
    offset: c_int,
) -> Result<ScheduledRecording, String> {
    let recording =
        read_scheduled_recording_json(column_text(statement, offset + 11)?, "scheduled recording")?;
    let expected_execution = scheduled_recording_execution(&recording);
    if recording.definition.recording_id != column_text(statement, offset)?
        || recording.definition.channel_id != column_text(statement, offset + 1)?
        || recording.definition.schedule_item_id != column_text(statement, offset + 2)?
        || recording.definition.schedule_revision
            != column_u64(
                statement,
                offset + 3,
                "scheduled recording schedule revision",
            )?
        || recording.definition.scheduled_start_unix_ms
            != unsafe { sqlite3_column_int64(statement, offset + 4) }
        || recording.definition.scheduled_end_unix_ms
            != unsafe { sqlite3_column_int64(statement, offset + 5) }
        || recording.revision != column_u64(statement, offset + 6, "scheduled recording revision")?
        || scheduled_recording_lifecycle_name(&recording.lifecycle)
            != column_text(statement, offset + 7)?
        || expected_execution.map_or("", |value| value.candidate.version.as_str())
            != column_text(statement, offset + 8)?
        || expected_execution.map_or("", |value| value.candidate.source_commit.as_str())
            != column_text(statement, offset + 9)?
        || expected_execution.map_or("", |value| value.channel_configuration_hash.as_str())
            != column_text(statement, offset + 10)?
    {
        return Err("scheduled recording row and record disagree".into());
    }
    Ok(recording)
}

fn validate_scheduled_recording_transition_receipt(
    recording: &ScheduledRecording,
    receipt: &RecordingTransitionReceipt,
) -> Result<(), String> {
    receipt.actor.validate()?;
    if receipt.recording_id != recording.definition.recording_id
        || receipt.prior_revision == 0
        || receipt.resulting_revision != recording.revision
        || receipt.prior_revision.checked_add(1) != Some(receipt.resulting_revision)
        || receipt.occurred_at_unix_ms < 0
        || receipt.execution.as_ref() != scheduled_recording_execution(recording)
        || !scheduled_recording_receipt_matches_lifecycle(receipt.kind, &recording.lifecycle)
        || !scheduled_recording_receipt_time_matches_lifecycle(
            receipt.occurred_at_unix_ms,
            &recording.lifecycle,
        )
    {
        return Err(
            "scheduled recording transition receipt does not match resulting authority".into(),
        );
    }
    Ok(())
}

fn scheduled_recording_receipt_time_matches_lifecycle(
    occurred_at_unix_ms: i64,
    lifecycle: &RecordingLifecycle,
) -> bool {
    match lifecycle {
        RecordingLifecycle::Draft => false,
        RecordingLifecycle::Armed {
            armed_at_unix_ms, ..
        } => occurred_at_unix_ms == *armed_at_unix_ms,
        RecordingLifecycle::Capturing {
            capture_started_at_unix_ms,
            ..
        } => occurred_at_unix_ms == *capture_started_at_unix_ms,
        RecordingLifecycle::Finalizing {
            finalization_started_at_unix_ms,
            ..
        } => occurred_at_unix_ms == *finalization_started_at_unix_ms,
        RecordingLifecycle::Completed {
            finalized_at_unix_ms,
            ..
        } => occurred_at_unix_ms == *finalized_at_unix_ms,
        RecordingLifecycle::Failed {
            failed_at_unix_ms, ..
        } => occurred_at_unix_ms == *failed_at_unix_ms,
        RecordingLifecycle::CancelledBeforeArm {
            cancelled_at_unix_ms,
            ..
        }
        | RecordingLifecycle::CancelledAfterArm {
            cancelled_at_unix_ms,
            ..
        } => occurred_at_unix_ms == *cancelled_at_unix_ms,
    }
}

fn scheduled_recording_receipt_matches_lifecycle(
    kind: RecordingTransitionKind,
    lifecycle: &RecordingLifecycle,
) -> bool {
    matches!(
        (kind, lifecycle),
        (
            RecordingTransitionKind::Arm,
            RecordingLifecycle::Armed { .. }
        ) | (
            RecordingTransitionKind::StartCapture,
            RecordingLifecycle::Capturing { .. }
        ) | (
            RecordingTransitionKind::BeginFinalization,
            RecordingLifecycle::Finalizing {
                recovered_after_interruption: false,
                ..
            }
        ) | (
            RecordingTransitionKind::RecoverForFinalization,
            RecordingLifecycle::Finalizing {
                recovered_after_interruption: true,
                ..
            }
        ) | (
            RecordingTransitionKind::FinalizeSuccess,
            RecordingLifecycle::Completed { .. }
        ) | (
            RecordingTransitionKind::Fail,
            RecordingLifecycle::Failed { .. }
        ) | (
            RecordingTransitionKind::Cancel,
            RecordingLifecycle::CancelledBeforeArm { .. }
        ) | (
            RecordingTransitionKind::Cancel,
            RecordingLifecycle::CancelledAfterArm { .. }
        )
    )
}

fn read_scheduled_recording_history_row(
    statement: *mut SqliteStatement,
) -> Result<ScheduledRecordingHistoryEntry, String> {
    let recording =
        read_scheduled_recording_json(column_text(statement, 10)?, "scheduled recording history")?;
    let recording_id = column_text(statement, 0)?;
    let revision = column_u64(statement, 1, "scheduled recording history revision")?;
    let transition_kind = column_text(statement, 2)?;
    let actor = RecordingActor {
        actor_id: column_text(statement, 3)?,
        role: parse_scheduled_recording_actor_role(&column_text(statement, 4)?)?,
    };
    actor.validate()?;
    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement, 5) };
    let receipt_json = column_text(statement, 9)?;
    let receipt = if receipt_json.is_empty() {
        None
    } else {
        let receipt =
            serde_json::from_str::<RecordingTransitionReceipt>(&receipt_json).map_err(|error| {
                format!("database returned invalid scheduled recording receipt JSON: {error}")
            })?;
        validate_scheduled_recording_transition_receipt(&recording, &receipt)?;
        Some(receipt)
    };
    let execution = scheduled_recording_execution(&recording);
    if recording.definition.recording_id != recording_id
        || recording.revision != revision
        || occurred_at_unix_ms < 0
        || execution.map_or("", |value| value.candidate.version.as_str())
            != column_text(statement, 6)?
        || execution.map_or("", |value| value.candidate.source_commit.as_str())
            != column_text(statement, 7)?
        || execution.map_or("", |value| value.channel_configuration_hash.as_str())
            != column_text(statement, 8)?
    {
        return Err("scheduled recording history row and record disagree".into());
    }
    match receipt {
        Some(ref receipt)
            if receipt.actor == actor
                && receipt.occurred_at_unix_ms == occurred_at_unix_ms
                && scheduled_recording_transition_kind_name(receipt.kind) == transition_kind => {}
        None if transition_kind == "created"
            && actor == recording.definition.requested_by
            && occurred_at_unix_ms == recording.definition.requested_at_unix_ms
            && recording.revision == 1
            && recording.lifecycle == RecordingLifecycle::Draft => {}
        _ => return Err("scheduled recording history receipt and record disagree".into()),
    }
    Ok(ScheduledRecordingHistoryEntry {
        recording,
        receipt,
        actor,
        occurred_at_unix_ms,
    })
}

fn is_sha256_identity(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_source_commit_identity(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_canonical_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte),
        })
}

fn is_caption_language(value: &str) -> bool {
    let bytes = value.as_bytes();
    (2..=35).contains(&bytes.len())
        && bytes.first().is_some_and(u8::is_ascii_alphabetic)
        && bytes.last().is_some_and(u8::is_ascii_alphanumeric)
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || *byte == b'-')
        && !value.contains("--")
}

fn parse_readiness(value: &str) -> Result<AssetReadiness, String> {
    match value {
        "ready" => Ok(AssetReadiness::Ready),
        "missing" => Ok(AssetReadiness::Missing),
        "rejected" => Ok(AssetReadiness::Rejected),
        "processing" => Ok(AssetReadiness::Processing),
        _ => Err(format!("database returned invalid asset readiness {value}")),
    }
}

fn schedule_state_name(value: ScheduleState) -> &'static str {
    match value {
        ScheduleState::Draft => "draft",
        ScheduleState::Committed => "committed",
        ScheduleState::Cancelled => "cancelled",
    }
}

fn parse_schedule_state(value: &str) -> Result<ScheduleState, String> {
    match value {
        "draft" => Ok(ScheduleState::Draft),
        "committed" => Ok(ScheduleState::Committed),
        "cancelled" => Ok(ScheduleState::Cancelled),
        _ => Err(format!("database returned invalid schedule state {value}")),
    }
}

fn dispatch_status_name(value: DispatchStatus) -> &'static str {
    match value {
        DispatchStatus::Pending => "pending",
        DispatchStatus::Queued => "queued",
        DispatchStatus::Acknowledged => "acknowledged",
        DispatchStatus::Missed => "missed",
        DispatchStatus::Completed => "completed",
        DispatchStatus::Error => "error",
        DispatchStatus::Cancelled => "cancelled",
    }
}

fn parse_dispatch_status(value: &str) -> Result<DispatchStatus, String> {
    match value {
        "pending" => Ok(DispatchStatus::Pending),
        "queued" => Ok(DispatchStatus::Queued),
        "acknowledged" => Ok(DispatchStatus::Acknowledged),
        "missed" => Ok(DispatchStatus::Missed),
        "completed" => Ok(DispatchStatus::Completed),
        "error" => Ok(DispatchStatus::Error),
        "cancelled" => Ok(DispatchStatus::Cancelled),
        _ => Err(format!("database returned invalid dispatch status {value}")),
    }
}

fn valid_dispatch_transition(expected: DispatchStatus, next: DispatchStatus) -> bool {
    matches!(
        (expected, next),
        (DispatchStatus::Pending, DispatchStatus::Queued)
            | (DispatchStatus::Pending, DispatchStatus::Error)
            | (DispatchStatus::Pending, DispatchStatus::Cancelled)
            | (DispatchStatus::Pending, DispatchStatus::Missed)
            | (DispatchStatus::Queued, DispatchStatus::Acknowledged)
            | (DispatchStatus::Queued, DispatchStatus::Error)
            | (DispatchStatus::Queued, DispatchStatus::Cancelled)
            | (DispatchStatus::Queued, DispatchStatus::Missed)
            | (DispatchStatus::Acknowledged, DispatchStatus::Completed)
            | (DispatchStatus::Acknowledged, DispatchStatus::Error)
    )
}

fn column_text(statement: *mut SqliteStatement, column: c_int) -> Result<String, String> {
    // SAFETY: caller provides a live statement positioned on a row and a valid column index.
    let value = unsafe { sqlite3_column_text(statement, column) };
    if value.is_null() {
        return Err("database returned an unexpected null value".into());
    }
    // SAFETY: SQLite returns a null-terminated UTF-8 buffer valid until the next step/finalize.
    Ok(unsafe { CStr::from_ptr(value.cast()) }
        .to_string_lossy()
        .into_owned())
}

fn optional_nonnegative_column(
    statement: *mut SqliteStatement,
    column: c_int,
) -> Result<Option<i64>, String> {
    let value = unsafe { sqlite3_column_int64(statement, column) };
    match value {
        -1 => Ok(None),
        0.. => Ok(Some(value)),
        _ => Err("database returned an invalid optional timestamp".into()),
    }
}

fn credential_version_column(
    statement: *mut SqliteStatement,
    column: c_int,
) -> Result<CredentialVersion, String> {
    let value = u64::try_from(unsafe { sqlite3_column_int64(statement, column) })
        .map_err(|_| "database returned invalid credential version".to_string())?;
    CredentialVersion::new(value)
        .map_err(|error| format!("database returned invalid credential version: {error}"))
}

fn secret_digest_column(
    statement: *mut SqliteStatement,
    column: c_int,
) -> Result<SecretDigest, String> {
    let bytes = column_blob(statement, column)?;
    let digest: [u8; 32] = bytes
        .try_into()
        .map_err(|_| "database returned an invalid secret digest".to_string())?;
    Ok(SecretDigest::from_bytes(digest))
}

fn column_blob(statement: *mut SqliteStatement, column: c_int) -> Result<Vec<u8>, String> {
    let byte_count = unsafe { sqlite3_column_bytes(statement, column) };
    if byte_count <= 0 {
        return Err("database returned an empty encrypted secret".into());
    }
    let value = unsafe { sqlite3_column_blob(statement, column) };
    if value.is_null() {
        return Err("database returned an invalid encrypted secret".into());
    }
    let length = usize::try_from(byte_count)
        .map_err(|_| "database returned an oversized encrypted secret".to_string())?;
    Ok(unsafe { std::slice::from_raw_parts(value.cast::<u8>(), length) }.to_vec())
}

fn protect_local_machine(plaintext: &[u8]) -> Result<Vec<u8>, String> {
    let byte_count =
        u32::try_from(plaintext.len()).map_err(|_| "station secret is too large".to_string())?;
    let mut input = DataBlob {
        byte_count,
        data: plaintext.as_ptr().cast_mut(),
    };
    let mut output = DataBlob {
        byte_count: 0,
        data: ptr::null_mut(),
    };
    let status = unsafe {
        CryptProtectData(
            &mut input,
            ptr::null(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE,
            &mut output,
        )
    };
    if status == 0 {
        return Err(format!(
            "Windows could not protect the station secret (error {}).",
            unsafe { GetLastError() }
        ));
    }
    if output.data.is_null() || output.byte_count == 0 {
        return Err("Windows returned an empty protected station secret.".into());
    }
    let protected = unsafe {
        std::slice::from_raw_parts(output.data.cast_const(), output.byte_count as usize).to_vec()
    };
    unsafe {
        LocalFree(output.data.cast());
    }
    Ok(protected)
}

fn unprotect_local_machine(protected: &[u8]) -> Result<Vec<u8>, String> {
    let byte_count = u32::try_from(protected.len())
        .map_err(|_| "encrypted station secret is too large".to_string())?;
    let mut input = DataBlob {
        byte_count,
        data: protected.as_ptr().cast_mut(),
    };
    let mut output = DataBlob {
        byte_count: 0,
        data: ptr::null_mut(),
    };
    let status = unsafe {
        CryptUnprotectData(
            &mut input,
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if status == 0 {
        return Err(format!(
            "Windows could not unlock the station secret (error {}).",
            unsafe { GetLastError() }
        ));
    }
    if output.data.is_null() || output.byte_count == 0 {
        return Err("Windows returned an empty unlocked station secret.".into());
    }
    let plaintext = unsafe {
        std::slice::from_raw_parts(output.data.cast_const(), output.byte_count as usize).to_vec()
    };
    unsafe {
        LocalFree(output.data.cast());
    }
    Ok(plaintext)
}

fn secret_field(value: &str, maximum: usize) -> bool {
    value.trim() == value
        && !value.is_empty()
        && value.len() <= maximum
        && !value.chars().any(char::is_control)
}

fn validate_srt_passphrase(value: &str) -> Result<(), String> {
    if !(10..=79).contains(&value.len()) || !value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
    {
        return Err(
            "SRT passphrases must contain 10 to 79 printable ASCII characters without spaces"
                .into(),
        );
    }
    Ok(())
}

fn validate_channel_passphrases(
    channel: &ChannelConfiguration,
    passphrases: &HashMap<String, String>,
) -> Result<(), String> {
    for (sink_id, passphrase) in passphrases {
        let valid_sink = channel.output_sinks.iter().any(|sink| {
            sink.sink_id == *sink_id
                && sink.kind == OutputSinkKind::Srt
                && sink.encryption_configured
        });
        if !valid_sink {
            return Err("SRT passphrases must identify an encrypted SRT output sink".into());
        }
        validate_srt_passphrase(passphrase)?;
    }
    Ok(())
}

fn valid_smtp_endpoint(value: &str) -> bool {
    if value.trim() != value
        || value.is_empty()
        || value.len() > 2048
        || value.chars().any(char::is_control)
    {
        return false;
    }
    let (secure, remainder) = if let Some(value) = value.strip_prefix("smtps://") {
        (true, value)
    } else if let Some(value) = value.strip_prefix("smtp://") {
        (false, value)
    } else {
        return false;
    };
    if remainder.contains(['/', '?', '#']) {
        return false;
    }
    let authority = remainder;
    let host = authority.split(':').next().unwrap_or_default();
    !authority.is_empty()
        && !authority.contains('@')
        && !authority.chars().any(char::is_whitespace)
        && (secure || host.eq_ignore_ascii_case("localhost") || host == "127.0.0.1")
}

fn sqlite_error(database: *mut Sqlite3) -> String {
    if database.is_null() {
        return "SQLite failed before creating a database handle".into();
    }
    // SAFETY: SQLite owns a null-terminated error buffer associated with this live handle.
    unsafe { CStr::from_ptr(sqlite3_errmsg(database)) }
        .to_string_lossy()
        .into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

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

    fn row_count(store: &StationStore, sql: &str) -> i64 {
        let statement = store.prepare(sql).unwrap();
        assert_eq!(unsafe { sqlite3_step(statement.raw) }, SQLITE_ROW);
        unsafe { sqlite3_column_int64(statement.raw, 0) }
    }

    fn insert_schedule_history_for_legacy_migration(
        store: &StationStore,
        item: &ScheduleItem,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) {
        let record_json = serde_json::to_string(item).unwrap();
        let statement = store
            .prepare(
                "INSERT INTO schedule_item_history(item_id, revision, record_json, changed_by, occurred_at_unix_ms) \
                 VALUES(?1, ?2, ?3, ?4, ?5)",
            )
            .unwrap();
        let item_id = c_string(
            &item.item_id,
            "legacy schedule-history fixture item identity",
        )
        .unwrap();
        let record_json = c_string(&record_json, "legacy schedule-history fixture record").unwrap();
        let changed_by = c_string(changed_by, "legacy schedule-history fixture actor").unwrap();
        bind_text(store.database, statement.raw, 1, &item_id).unwrap();
        bind_i64(
            store.database,
            statement.raw,
            2,
            checked_i64(item.revision, "legacy schedule-history fixture revision").unwrap(),
        )
        .unwrap();
        bind_text(store.database, statement.raw, 3, &record_json).unwrap();
        bind_text(store.database, statement.raw, 4, &changed_by).unwrap();
        bind_i64(store.database, statement.raw, 5, occurred_at_unix_ms).unwrap();
        expect_done(store.database, statement.raw).unwrap();
    }

    fn create_legacy_schedule_items_database(database: &Path) {
        let filename = c_string(
            &database.to_string_lossy(),
            "legacy schedule test database path",
        )
        .unwrap();
        let mut handle = ptr::null_mut();
        assert_eq!(
            unsafe {
                sqlite3_open_v2(
                    filename.as_ptr(),
                    &mut handle,
                    SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
                    ptr::null(),
                )
            },
            SQLITE_OK
        );
        let sql = c_string(
            &format!(
                "CREATE TABLE schedule_items(\
                   item_id TEXT PRIMARY KEY,\
                   channel_id TEXT NOT NULL,\
                   asset_id TEXT NOT NULL,\
                   title TEXT NOT NULL,\
                   starts_at_unix_ms INTEGER NOT NULL,\
                   duration_ms INTEGER NOT NULL,\
                   state TEXT NOT NULL\
                 );\
                 INSERT INTO schedule_items(item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state)\
                 VALUES('11111111-2222-4333-8444-555555555555',\
                        '22222222-3333-4444-8555-666666666666',\
                        '{}', 'Legacy schedule item', 1000, 60000, 'committed');",
                "a".repeat(64)
            ),
            "legacy schedule test schema",
        )
        .unwrap();
        assert_eq!(
            unsafe {
                sqlite3_exec(
                    handle,
                    sql.as_ptr(),
                    ptr::null_mut(),
                    ptr::null_mut(),
                    ptr::null_mut(),
                )
            },
            SQLITE_OK
        );
        assert_eq!(unsafe { sqlite3_close(handle) }, SQLITE_OK);
    }

    #[test]
    fn automation_json_mismatch_is_fail_closed_on_every_authority_read() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-automation-row-mismatch-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        let adapter = AutomationAdapterDescriptor {
            adapter_id: "11111111-2222-4333-8444-555555555555".into(),
            location: station_automation_policy::AdapterLocation::Local,
            model_key: "model".into(),
            model_identifier: "model:1".into(),
            supported_features: vec![AutomationFeature::Summaries],
            credential_availability: station_automation_policy::CredentialAvailability::NotRequired,
            revision: 1,
        };
        let policy = AutomationFeaturePolicy {
            policy_id: "22222222-2222-4333-8444-555555555555".into(),
            feature: AutomationFeature::Summaries,
            operator_selected_adapter_id: Some(adapter.adapter_id.clone()),
            adaptive_default: None,
            configuration_hash: "a".repeat(64),
            revision: 1,
        };
        let candidate = AutomationCandidateIdentity {
            version: "0.1.0".into(),
            source_commit: "b".repeat(40),
        };
        let evidence = AutomationConnectionTestEvidence {
            evidence_id: "33333333-2222-4333-8444-555555555555".into(),
            policy_id: policy.policy_id.clone(),
            adapter_id: adapter.adapter_id.clone(),
            policy_revision: 1,
            adapter_revision: 1,
            configuration_hash: policy.configuration_hash.clone(),
            candidate: candidate.clone(),
            outcome: station_automation_policy::ConnectionTestOutcome::Passed,
            tested_at_unix_ms: 10,
            expires_at_unix_ms: 100,
            detail: "passed".into(),
        };
        store
            .write_automation_adapter_descriptor(&adapter, None, "setup-admin", 1)
            .unwrap();
        store
            .write_automation_feature_policy(&policy, None, "setup-admin", 2)
            .unwrap();
        store
            .append_automation_connection_test_evidence(&evidence, &candidate, 20)
            .unwrap();

        let mut mismatched_adapter = adapter.clone();
        mismatched_adapter.revision = 2;
        let statement = store
            .prepare("UPDATE automation_adapter_descriptors SET record_json=?1 WHERE adapter_id=?2")
            .unwrap();
        let json = c_string(
            &serde_json::to_string(&mismatched_adapter).unwrap(),
            "test JSON",
        )
        .unwrap();
        let identity = c_string(&adapter.adapter_id, "test adapter").unwrap();
        bind_text(store.database, statement.raw, 1, &json).unwrap();
        bind_text(store.database, statement.raw, 2, &identity).unwrap();
        expect_done(store.database, statement.raw).unwrap();
        assert!(
            store
                .read_automation_adapter_descriptor(&adapter.adapter_id)
                .is_err()
        );

        let mut mismatched_policy = policy.clone();
        mismatched_policy.revision = 2;
        let statement = store
            .prepare("UPDATE automation_feature_policies SET record_json=?1 WHERE policy_id=?2")
            .unwrap();
        let json = c_string(
            &serde_json::to_string(&mismatched_policy).unwrap(),
            "test JSON",
        )
        .unwrap();
        let identity = c_string(&policy.policy_id, "test policy").unwrap();
        bind_text(store.database, statement.raw, 1, &json).unwrap();
        bind_text(store.database, statement.raw, 2, &identity).unwrap();
        expect_done(store.database, statement.raw).unwrap();
        assert!(
            store
                .read_automation_feature_policy(&policy.policy_id)
                .is_err()
        );

        let mut mismatched_evidence = evidence.clone();
        mismatched_evidence.adapter_revision = 2;
        let statement = store
            .prepare("UPDATE automation_connection_test_evidence SET record_json=?1 WHERE evidence_id=?2")
            .unwrap();
        let json = c_string(
            &serde_json::to_string(&mismatched_evidence).unwrap(),
            "test JSON",
        )
        .unwrap();
        let identity = c_string(&evidence.evidence_id, "test evidence").unwrap();
        bind_text(store.database, statement.raw, 1, &json).unwrap();
        bind_text(store.database, statement.raw, 2, &identity).unwrap();
        assert!(expect_done(store.database, statement.raw).is_err());

        let statement = store
            .prepare("DELETE FROM automation_feature_policy_history WHERE policy_id=?1")
            .unwrap();
        let identity = c_string(&policy.policy_id, "test policy").unwrap();
        bind_text(store.database, statement.raw, 1, &identity).unwrap();
        assert!(expect_done(store.database, statement.raw).is_err());

        assert!(store.execute(
            "INSERT INTO automation_adapter_descriptor_history(adapter_id, revision, record_json, changed_by, occurred_at_unix_ms) VALUES('99999999-2222-4333-8444-555555555555', 1, '{}', '', 0)",
        ).is_err());

        drop(store);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn legacy_schedule_items_are_backfilled_to_revision_one() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-legacy-schedule-{nonce}.db"));
        create_legacy_schedule_items_database(&database);
        let store = StationStore::open(&database).unwrap();
        let item = store
            .read_schedule_item("11111111-2222-4333-8444-555555555555")
            .unwrap()
            .unwrap();
        assert_eq!(item.revision, 1);
        let mut updated = item.clone();
        updated.revision = 2;
        updated.title = "Legacy schedule revision two".into();
        store.execute("BEGIN IMMEDIATE").unwrap();
        let update_result = store.write_schedule_item_in_transaction(&ScheduleItemWrite {
            item: updated.clone(),
            changed_by: "test-harness".into(),
            occurred_at_unix_ms: 1,
        });
        assert!(update_result.is_ok());
        store.execute("COMMIT").unwrap();
        assert_eq!(
            store
                .read_schedule_item("11111111-2222-4333-8444-555555555555")
                .unwrap()
                .unwrap(),
            updated
        );
        let history = store
            .list_schedule_item_history("11111111-2222-4333-8444-555555555555", 8)
            .unwrap();
        assert_eq!(history.len(), 2);
        assert_eq!(history[0].item.revision, 1);
        assert_eq!(history[0].changed_by, "legacy-migration");
        assert_eq!(history[0].occurred_at_unix_ms, 0);
        assert_eq!(history[1].item.revision, 2);
        drop(store);
        let reopened = StationStore::open(&database).unwrap();
        assert_eq!(
            reopened
                .list_schedule_item_history("11111111-2222-4333-8444-555555555555", 8)
                .unwrap()
                .len(),
            2
        );
        drop(reopened);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn scheduled_recording_storage_is_cas_bound_append_only_and_fails_closed_on_tampering() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-recording-authority-{nonce}.db"));
        let media_path = database.with_extension("ts");
        std::fs::write(&media_path, b"scheduled recording authority media").unwrap();
        let store = StationStore::open(&database).unwrap();
        let operator = RecordingActor {
            actor_id: "11111111-2222-4333-8444-555555555555".into(),
            role: RecordingActorRole::Operator,
        };
        let schedule_item = ScheduleItem {
            item_id: "55555555-6666-4777-8888-999999999999".into(),
            revision: 1,
            channel_id: "44444444-5555-4666-8777-888888888888".into(),
            asset_id: "a".repeat(64),
            title: "Scheduled recording source".into(),
            starts_at_unix_ms: 10_000,
            duration_ms: 60_000,
            state: ScheduleState::Draft,
        };
        store.put_schedule_item(&schedule_item).unwrap();
        store
            .put_media_asset(&MediaAsset {
                asset_id: schedule_item.asset_id.clone(),
                media_path: media_path.to_string_lossy().into_owned(),
                duration_ms: schedule_item.duration_ms,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        store
            .commit_schedule(
                "recording-source-report",
                "recording-source-plan",
                &schedule_item.item_id,
                "test-harness",
                1,
                "scheduled recording fixture",
            )
            .unwrap();
        let schedule_item = store
            .read_schedule_item(&schedule_item.item_id)
            .unwrap()
            .unwrap();
        let schedule_history = store
            .list_schedule_item_history(&schedule_item.item_id, 8)
            .unwrap();
        assert_eq!(schedule_history.len(), 2);
        assert_eq!(schedule_history[1].item, schedule_item);
        assert_eq!(schedule_history[1].changed_by, "test-harness");
        assert_eq!(
            store
                .list_recording_schedule_candidates(&schedule_item.channel_id, 1, 100_000, 1)
                .unwrap(),
            vec![schedule_item.clone()]
        );
        assert!(
            store
                .list_recording_schedule_candidates(
                    &schedule_item.channel_id,
                    schedule_item.starts_at_unix_ms,
                    100_000,
                    1,
                )
                .unwrap()
                .is_empty()
        );
        assert!(
            store
                .list_recording_schedule_candidates(&schedule_item.channel_id, 1, 100_000, 0)
                .is_err()
        );
        let maximum_candidate_window_ms = 31 * 24 * 60 * 60 * 1_000;
        assert!(
            store
                .list_recording_schedule_candidates(
                    &schedule_item.channel_id,
                    1,
                    1 + maximum_candidate_window_ms,
                    128,
                )
                .is_ok()
        );
        assert!(
            store
                .list_recording_schedule_candidates(
                    &schedule_item.channel_id,
                    1,
                    2 + maximum_candidate_window_ms,
                    1,
                )
                .is_err()
        );
        assert!(
            store
                .list_recording_schedule_candidates(&schedule_item.channel_id, 1, 100_000, 129)
                .is_err()
        );
        let draft = ScheduledRecording::new(station_recording::RecordingDefinition {
            recording_id: "33333333-4444-4555-8666-777777777777".into(),
            channel_id: "44444444-5555-4666-8777-888888888888".into(),
            schedule_item_id: "55555555-6666-4777-8888-999999999999".into(),
            schedule_revision: 2,
            scheduled_start_unix_ms: 10_000,
            scheduled_end_unix_ms: 70_000,
            requested_by: operator.clone(),
            requested_at_unix_ms: 1_000,
        })
        .unwrap();
        store.create_scheduled_recording(&draft).unwrap();
        assert_eq!(
            store
                .read_scheduled_recording(&draft.definition.recording_id)
                .unwrap(),
            Some(draft.clone())
        );
        assert_eq!(
            store
                .list_scheduled_recordings_for_channel(&draft.definition.channel_id, 1)
                .unwrap(),
            vec![draft.clone()]
        );
        let created = store
            .list_scheduled_recording_history(&draft.definition.recording_id, 8)
            .unwrap();
        assert_eq!(created.len(), 1);
        assert_eq!(created[0].receipt, None);
        assert_eq!(created[0].actor, operator);

        let armed = draft
            .transition(station_recording::RecordingTransitionRequest {
                expected_revision: 1,
                actor: operator,
                occurred_at_unix_ms: 2_000,
                execution: Some(RecordingExecutionAuthority {
                    candidate: station_recording::RecordingCandidateIdentity {
                        version: "0.1.0".into(),
                        source_commit: "a".repeat(40),
                    },
                    channel_configuration_hash: "b".repeat(64),
                }),
                event: station_recording::RecordingEvent::Arm,
            })
            .unwrap();
        store
            .write_scheduled_recording_transition(&armed.recording, &armed.receipt)
            .unwrap();
        assert_eq!(
            store
                .read_scheduled_recording(&draft.definition.recording_id)
                .unwrap(),
            Some(armed.recording.clone())
        );
        let history = store
            .list_scheduled_recording_history(&draft.definition.recording_id, 8)
            .unwrap();
        assert_eq!(history.len(), 2);
        assert_eq!(history[1].receipt, Some(armed.receipt.clone()));
        assert_eq!(history[1].actor, armed.receipt.actor);

        let mut changed_schedule_item = schedule_item.clone();
        changed_schedule_item.revision = 3;
        changed_schedule_item.state = ScheduleState::Draft;
        changed_schedule_item.title = "Changed after recording request".into();
        store.put_schedule_item(&changed_schedule_item).unwrap();
        assert_eq!(
            store
                .list_schedule_item_history(&changed_schedule_item.item_id, 8)
                .unwrap()
                .len(),
            3
        );
        let start_capture = armed
            .recording
            .transition(station_recording::RecordingTransitionRequest {
                expected_revision: 2,
                actor: RecordingActor {
                    actor_id: "00000000-0000-4000-8000-000000000021".into(),
                    role: RecordingActorRole::RecordingWorker,
                },
                occurred_at_unix_ms: 10_000,
                execution: scheduled_recording_execution(&armed.recording).cloned(),
                event: station_recording::RecordingEvent::StartCapture,
            })
            .unwrap();
        assert!(
            store
                .write_scheduled_recording_transition(
                    &start_capture.recording,
                    &start_capture.receipt,
                )
                .unwrap_err()
                .contains("schedule item is not committed")
        );
        let mut stale_schedule_recording = draft.clone();
        stale_schedule_recording.definition.recording_id =
            "77777777-8888-4999-8aaa-bbbbbbbbbbbb".into();
        assert!(
            store
                .create_scheduled_recording(&stale_schedule_recording)
                .unwrap_err()
                .contains("schedule item is not committed")
        );

        assert!(
            store
                .write_scheduled_recording_transition(&armed.recording, &armed.receipt)
                .is_err()
        );
        store
            .commit_schedule(
                "recording-source-report-two",
                "recording-source-plan-two",
                &schedule_item.item_id,
                "test-harness",
                4,
                "recommitted fixture",
            )
            .unwrap();
        let statement = store
            .prepare(
                "UPDATE scheduled_recording_history SET actor_id='tampered' WHERE recording_id=?1",
            )
            .unwrap();
        let recording_id =
            c_string(&draft.definition.recording_id, "test recording identity").unwrap();
        bind_text(store.database, statement.raw, 1, &recording_id).unwrap();
        assert!(expect_done(store.database, statement.raw).is_err());

        let statement = store
            .prepare("UPDATE schedule_items SET title='tampered' WHERE item_id=?1")
            .unwrap();
        let schedule_item_id =
            c_string(&schedule_item.item_id, "test schedule item identity").unwrap();
        bind_text(store.database, statement.raw, 1, &schedule_item_id).unwrap();
        expect_done(store.database, statement.raw).unwrap();
        assert!(store.read_schedule_item(&schedule_item.item_id).is_err());
        assert!(
            store
                .list_channel_schedule(&schedule_item.channel_id)
                .is_err()
        );
        assert!(store.list_schedule_window(1, 100_000).is_err());
        assert!(
            store
                .list_recording_schedule_candidates(&schedule_item.channel_id, 1, 100_000, 1)
                .is_err()
        );

        let mut mismatched = armed.recording.clone();
        mismatched.revision = 3;
        let statement = store
            .prepare("UPDATE scheduled_recordings SET record_json=?1 WHERE recording_id=?2")
            .unwrap();
        let record_json = c_string(
            &serde_json::to_string(&mismatched).unwrap(),
            "test scheduled recording JSON",
        )
        .unwrap();
        bind_text(store.database, statement.raw, 1, &record_json).unwrap();
        bind_text(store.database, statement.raw, 2, &recording_id).unwrap();
        expect_done(store.database, statement.raw).unwrap();
        assert!(
            store
                .read_scheduled_recording(&draft.definition.recording_id)
                .is_err()
        );
        drop(store);
        let _ = std::fs::remove_file(database);
        let _ = std::fs::remove_file(media_path);
    }

    #[test]
    fn completed_caption_history_survives_output_sink_reconfiguration_and_removal() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-caption-history-sink-reconfiguration-{nonce}.db"
        ));
        let media_path = database.with_extension("ts");
        std::fs::write(&media_path, b"caption history media").unwrap();
        let store = StationStore::open(&database).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let proof_sink_id = "22222222-3333-4444-8555-666666666666";
        let retained_sink_id = "33333333-4444-4555-8666-777777777777";
        let asset_id = "a".repeat(64);
        let track_id = "44444444-5555-4666-8777-888888888888";
        let job_id = "55555555-6666-4777-8888-999999999999";
        let proof_id = job_id;
        let report_id = "caption-history-report";
        let item_id = "77777777-8888-4999-8aaa-bbbbbbbbbbbb";
        let now = 10_000;
        let mut channel = ChannelConfiguration {
            channel_id: channel_id.into(),
            display_name: "Caption history".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![
                OutputSinkConfiguration::required_udp(
                    proof_sink_id,
                    "Proof sink",
                    "127.0.0.1:5500",
                    -16.0,
                ),
                OutputSinkConfiguration::required_udp(
                    retained_sink_id,
                    "Retained sink",
                    "127.0.0.1:5501",
                    -16.0,
                ),
            ],
            enabled: false,
        };
        store.put_channel(&channel).unwrap();
        store
            .put_media_asset(&MediaAsset {
                asset_id: asset_id.clone(),
                media_path: media_path.to_string_lossy().into_owned(),
                duration_ms: 10_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        let track = store
            .write_asset_caption_track(&AssetCaptionTrackWrite {
                track_id: track_id.into(),
                asset_id: asset_id.clone(),
                language: "en-US".into(),
                service_number: 1,
                source_kind: CaptionTrackSourceKind::External,
                label: "Reviewed English".into(),
                cues: vec![AssetCaptionCue {
                    cue_id: "88888888-9999-4aaa-8bbb-cccccccccccc".into(),
                    start_ms: 1_000,
                    duration_ms: 3_000,
                    text: "Council meeting".into(),
                }],
                expected_revision: 0,
                actor: "setup-admin".into(),
                occurred_at_unix_ms: now - 4_000,
                audit_id: "99999999-aaaa-4bbb-8ccc-dddddddddddd".into(),
            })
            .unwrap();
        let policy = store
            .write_channel_caption_policy(
                channel_id,
                ChannelCaptionMode::Cea708,
                1,
                "en-US",
                0,
                "setup-admin",
                now - 4_000,
                "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            )
            .unwrap();
        store
            .put_schedule_item(&ScheduleItem {
                item_id: item_id.into(),
                revision: 1,
                channel_id: channel_id.into(),
                asset_id: asset_id.clone(),
                title: "Caption history program".into(),
                starts_at_unix_ms: now - 3_000,
                duration_ms: 10_000,
                state: ScheduleState::Draft,
            })
            .unwrap();
        store
            .commit_schedule(
                report_id,
                "caption-history-plan",
                item_id,
                "meeting-operator",
                now - 3_000,
                "caption history fixture",
            )
            .unwrap();
        assert!(
            store
                .advance_dispatch_status(report_id, DispatchStatus::Pending, DispatchStatus::Queued)
                .unwrap()
        );
        assert!(
            store
                .advance_dispatch_status(
                    report_id,
                    DispatchStatus::Queued,
                    DispatchStatus::Acknowledged,
                )
                .unwrap()
        );
        channel.enabled = true;
        store.put_channel(&channel).unwrap();
        let configuration_hash = store
            .current_channel_output_configuration_hash(channel_id)
            .unwrap();
        let expected = expected_caption_evidence_for_window(&track, 10_000, 1_000, 3_000).unwrap();
        let job = CaptionEgressProofJob {
            job_id: job_id.into(),
            channel_id: channel_id.into(),
            sink_id: proof_sink_id.into(),
            report_id: report_id.into(),
            asset_id: asset_id.clone(),
            track_id: track_id.into(),
            track_revision: track.revision,
            cues_sha256: track.cues_sha256.clone(),
            caption_policy_revision: policy.revision,
            language: policy.language.clone(),
            service_number: policy.service_number,
            candidate_version: "0.1.0".into(),
            source_commit: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            configuration_hash: configuration_hash.clone(),
            capture_started_asset_offset_ms: 1_000,
            capture_duration_ms: 3_000,
            max_capture_bytes: 1_000_000,
            expected_cue_count: expected.cue_count,
            expected_text_sha256: expected.text_sha256.clone(),
            created_at_unix_ms: now - 2_000,
        };
        assert!(store.create_caption_egress_job(&job).unwrap());
        store
            .claim_next_caption_egress_job(channel_id, now - 1_900, 10_000)
            .unwrap()
            .unwrap();
        store
            .advance_caption_egress_job(
                job_id,
                CaptionEgressProofJobState::Claimed,
                CaptionEgressProofJobState::CapturePending,
                now - 1_800,
                10_000,
                None,
            )
            .unwrap();
        let capture = CaptionProofCaptureFacts {
            artifact_relative_path: format!("caption-proofs/{job_id}.ts"),
            capture_bytes: 1_000,
            capture_duration_ms: 3_000,
            capture_sha256: "b".repeat(64),
        };
        store
            .advance_caption_egress_job(
                job_id,
                CaptionEgressProofJobState::CapturePending,
                CaptionEgressProofJobState::Analyzing,
                now - 1_700,
                10_000,
                Some(&capture),
            )
            .unwrap();
        let proof = CaptionEgressProof {
            proof_id: proof_id.into(),
            channel_id: channel_id.into(),
            sink_id: proof_sink_id.into(),
            report_id: report_id.into(),
            asset_id: asset_id.clone(),
            track_id: track_id.into(),
            track_revision: track.revision,
            cues_sha256: track.cues_sha256,
            caption_policy_revision: policy.revision,
            language: policy.language,
            service_number: policy.service_number,
            candidate_version: "0.1.0".into(),
            source_commit: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            configuration_hash,
            capture_started_asset_offset_ms: 1_000,
            capture_duration_ms: capture.capture_duration_ms,
            capture_bytes: capture.capture_bytes,
            capture_sha256: capture.capture_sha256,
            artifact_relative_path: capture.artifact_relative_path,
            expected_cue_count: expected.cue_count,
            decoded_cue_count: expected.cue_count,
            expected_text_sha256: expected.text_sha256.clone(),
            decoded_text_sha256: expected.text_sha256,
            decoder_engine: "GStreamer 1.28.6".into(),
            decoder_method: "independent MPEG-TS CEA-708 decode-back".into(),
            outcome: CaptionProofOutcome::Verified,
            captured_at_unix_ms: now - 1_600,
        };
        let completed = store
            .complete_caption_egress_job_with_proof(job_id, now - 1_500, &proof)
            .unwrap();
        assert_eq!(completed.state, CaptionEgressProofJobState::Completed);

        channel.output_sinks = vec![OutputSinkConfiguration::required_udp(
            retained_sink_id,
            "Reconfigured retained sink",
            "127.0.0.1:5502",
            -16.0,
        )];
        store.put_channel(&channel).unwrap();
        assert_eq!(
            store
                .read_caption_egress_job(job_id)
                .unwrap()
                .unwrap()
                .state,
            CaptionEgressProofJobState::Completed
        );
        assert_eq!(
            store
                .list_caption_egress_proofs(channel_id, None, 10)
                .unwrap(),
            vec![proof]
        );
        assert!(store
            .execute(&format!(
                "UPDATE caption_egress_proofs SET outcome='mismatch' WHERE proof_id='{proof_id}'"
            ))
            .is_err());
        assert!(
            store
                .execute(&format!(
                    "DELETE FROM caption_egress_proofs WHERE proof_id='{proof_id}'"
                ))
                .is_err()
        );

        drop(store);
        for candidate in [
            database.clone(),
            media_path,
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn legacy_caption_sink_foreign_keys_migrate_without_losing_history_or_triggers() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-caption-history-legacy-sink-foreign-key-{nonce}.db"
        ));
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let proof_sink_id = "22222222-3333-4444-8555-666666666666";
        let retained_sink_id = "33333333-4444-4555-8666-777777777777";
        let job_id = "44444444-5555-4666-8777-888888888888";
        let proof_id = "55555555-6666-4777-8888-999999999999";
        let mut channel = ChannelConfiguration {
            channel_id: channel_id.into(),
            display_name: "Legacy caption history".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![
                OutputSinkConfiguration::required_udp(
                    proof_sink_id,
                    "Legacy proof sink",
                    "127.0.0.1:5500",
                    -16.0,
                ),
                OutputSinkConfiguration::required_udp(
                    retained_sink_id,
                    "Legacy retained sink",
                    "127.0.0.1:5501",
                    -16.0,
                ),
            ],
            enabled: true,
        };
        let proof = CaptionEgressProof {
            proof_id: proof_id.into(),
            channel_id: channel_id.into(),
            sink_id: proof_sink_id.into(),
            report_id: "legacy-caption-report".into(),
            asset_id: "a".repeat(64),
            track_id: "66666666-7777-4888-8999-aaaaaaaaaaaa".into(),
            track_revision: 1,
            cues_sha256: "b".repeat(64),
            caption_policy_revision: 1,
            language: "en-US".into(),
            service_number: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            configuration_hash: "c".repeat(64),
            capture_started_asset_offset_ms: 1_000,
            capture_duration_ms: 3_000,
            capture_bytes: 1_000,
            capture_sha256: "d".repeat(64),
            artifact_relative_path: format!("caption-proofs/{proof_id}.ts"),
            expected_cue_count: 1,
            decoded_cue_count: 1,
            expected_text_sha256: "e".repeat(64),
            decoded_text_sha256: "e".repeat(64),
            decoder_engine: "GStreamer 1.28.6".into(),
            decoder_method: "independent MPEG-TS CEA-708 decode-back".into(),
            outcome: CaptionProofOutcome::Verified,
            captured_at_unix_ms: 3_000,
        };
        let job = CaptionEgressProofJob {
            job_id: job_id.into(),
            channel_id: channel_id.into(),
            sink_id: proof_sink_id.into(),
            report_id: proof.report_id.clone(),
            asset_id: proof.asset_id.clone(),
            track_id: proof.track_id.clone(),
            track_revision: proof.track_revision,
            cues_sha256: proof.cues_sha256.clone(),
            caption_policy_revision: proof.caption_policy_revision,
            language: proof.language.clone(),
            service_number: proof.service_number,
            candidate_version: proof.candidate_version.clone(),
            source_commit: proof.source_commit.clone(),
            configuration_hash: proof.configuration_hash.clone(),
            capture_started_asset_offset_ms: proof.capture_started_asset_offset_ms,
            capture_duration_ms: proof.capture_duration_ms,
            max_capture_bytes: 1_000_000,
            expected_cue_count: proof.expected_cue_count,
            expected_text_sha256: proof.expected_text_sha256.clone(),
            created_at_unix_ms: 1_000,
        };
        let store = StationStore::open(&database).unwrap();
        store.put_channel(&channel).unwrap();
        let proof_json = serde_json::to_string(&proof).unwrap();
        let job_json = serde_json::to_string(&job).unwrap();
        store
            .execute(&format!(
                "DROP TRIGGER caption_egress_proofs_no_update;\
                 DROP TRIGGER caption_egress_proofs_no_delete;\
                 DROP INDEX caption_egress_proofs_channel_time;\
                 DROP INDEX caption_egress_jobs_active_sink;\
                 DROP INDEX caption_egress_jobs_state_time;\
                 DROP TABLE caption_egress_proofs;\
                 DROP TABLE caption_egress_jobs;\
                 CREATE TABLE caption_egress_proofs(\
                   proof_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, sink_id TEXT NOT NULL,\
                   report_id TEXT NOT NULL, asset_id TEXT NOT NULL, track_id TEXT NOT NULL,\
                   track_revision INTEGER NOT NULL, caption_policy_revision INTEGER NOT NULL,\
                   cues_sha256 TEXT NOT NULL, candidate_version TEXT NOT NULL, source_commit TEXT NOT NULL,\
                   configuration_hash TEXT NOT NULL, outcome TEXT NOT NULL, captured_at_unix_ms INTEGER NOT NULL,\
                   record_json TEXT NOT NULL,\
                   FOREIGN KEY(channel_id, sink_id) REFERENCES channel_output_sinks(channel_id, sink_id)\
                 );\
                 CREATE TABLE caption_egress_jobs(\
                   job_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, sink_id TEXT NOT NULL, state TEXT NOT NULL,\
                   created_at_unix_ms INTEGER NOT NULL, updated_at_unix_ms INTEGER NOT NULL,\
                   lease_expires_at_unix_ms INTEGER NOT NULL, job_json TEXT NOT NULL, capture_json TEXT NOT NULL,\
                   completed_at_unix_ms INTEGER NOT NULL, failed_at_unix_ms INTEGER NOT NULL, failure_detail TEXT NOT NULL,\
                   FOREIGN KEY(channel_id, sink_id) REFERENCES channel_output_sinks(channel_id, sink_id)\
                 );\
                 INSERT INTO caption_egress_proofs VALUES(\
                   '{proof_id}','{channel_id}','{proof_sink_id}','{}','{}','{}',1,1,'{}','{}','{}','{}','verified',3000,'{}'\
                 );\
                 INSERT INTO caption_egress_jobs VALUES(\
                   '{job_id}','{channel_id}','{proof_sink_id}','completed',1000,3000,-1,'{}','',3000,-1,''\
                 );",
                proof.report_id,
                proof.asset_id,
                proof.track_id,
                proof.cues_sha256,
                proof.candidate_version,
                proof.source_commit,
                proof.configuration_hash,
                proof_json,
                job_json,
            ))
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        assert_eq!(
            migrated
                .read_caption_egress_job(job_id)
                .unwrap()
                .unwrap()
                .state,
            CaptionEgressProofJobState::Completed
        );
        assert_eq!(
            migrated
                .list_caption_egress_proofs(channel_id, None, 10)
                .unwrap(),
            vec![proof]
        );
        assert_eq!(
            row_count(
                &migrated,
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type='table' AND name IN ('caption_egress_proofs','caption_egress_jobs') \
                 AND sql LIKE '%REFERENCES channel_output_sinks%'"
            ),
            0
        );
        assert_eq!(
            row_count(
                &migrated,
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN (\
                   'caption_egress_proofs_no_update','caption_egress_proofs_no_delete'\
                 )"
            ),
            2
        );
        assert_eq!(
            row_count(
                &migrated,
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name IN (\
                   'caption_egress_proofs_channel_time','caption_egress_jobs_active_sink',\
                   'caption_egress_jobs_state_time'\
                 )"
            ),
            3
        );
        channel.output_sinks = vec![OutputSinkConfiguration::required_udp(
            retained_sink_id,
            "Migrated retained sink",
            "127.0.0.1:5502",
            -16.0,
        )];
        migrated.put_channel(&channel).unwrap();
        assert!(migrated
            .execute(&format!(
                "UPDATE caption_egress_proofs SET outcome='mismatch' WHERE proof_id='{proof_id}'"
            ))
            .is_err());
        assert!(
            migrated
                .execute(&format!(
                    "DELETE FROM caption_egress_proofs WHERE proof_id='{proof_id}'"
                ))
                .is_err()
        );

        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn opening_store_creates_asset_caption_authority_schema() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-asset-caption-authority-schema-{nonce}.db"
        ));
        let store = StationStore::open(&database).unwrap();

        assert_eq!(
            row_count(
                &store,
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (\
                   'asset_caption_tracks',\
                   'asset_caption_cues',\
                   'asset_caption_track_revisions',\
                   'asset_caption_track_revision_cues'\
                 )",
            ),
            4,
            "opening the authoritative store must create the complete caption-track schema"
        );

        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn reads_an_exact_asset_caption_track_and_its_cues() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-asset-caption-read-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        let asset_id = "a".repeat(64);
        let track_id = "65a26343-0339-4654-a92c-4bf9384f18df";
        let cue = station_captions::AssetCaptionCue {
            cue_id: "bf0aa2ca-1488-42d2-8c1b-5d9179ee5d30".into(),
            start_ms: 1_000,
            duration_ms: 2_000,
            text: "Town Council".into(),
        };
        let cue_hash = station_captions::cues_sha256(std::slice::from_ref(&cue));
        store
            .execute(&format!(
                "INSERT INTO media_assets VALUES('{asset_id}','C:\\\\media.ts',10000,'ready');\
                 INSERT INTO asset_caption_tracks VALUES(\
                   '{track_id}','{asset_id}','en-US',1,'external','Reviewed English',1,1,\
                   '{cue_hash}',1000,1000,'setup-admin'\
                 );\
                 INSERT INTO asset_caption_cues VALUES(\
                   '{track_id}',0,'{}',1000,2000,'Town Council'\
                 );",
                cue.cue_id
            ))
            .unwrap();

        assert_eq!(
            store.read_asset_caption_track(track_id).unwrap(),
            Some(AssetCaptionTrack {
                track_id: track_id.into(),
                asset_id,
                language: "en-US".into(),
                service_number: 1,
                source_kind: CaptionTrackSourceKind::External,
                label: "Reviewed English".into(),
                cues: vec![cue],
                revision: 1,
                cues_sha256: cue_hash,
                created_at_unix_ms: 1_000,
                updated_at_unix_ms: 1_000,
                updated_by: "setup-admin".into(),
            })
        );

        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn asset_caption_revision_headers_and_cues_reject_tampering() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-asset-caption-tamper-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        let asset_id = "a".repeat(64);
        store
            .put_media_asset(&MediaAsset {
                asset_id: asset_id.clone(),
                media_path: r"C:\TownLight\media\tamper.ts".into(),
                duration_ms: 10_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        let track_id = "65a26343-0339-4654-a92c-4bf9384f18df";
        let audit_id = "54691f24-e034-45bd-9ed5-31d9227a78aa";
        store
            .write_asset_caption_track(&AssetCaptionTrackWrite {
                track_id: track_id.into(),
                asset_id,
                language: "en-US".into(),
                service_number: 1,
                source_kind: CaptionTrackSourceKind::External,
                label: "Reviewed English".into(),
                cues: vec![AssetCaptionCue {
                    cue_id: "bf0aa2ca-1488-42d2-8c1b-5d9179ee5d30".into(),
                    start_ms: 1_000,
                    duration_ms: 2_000,
                    text: "Town Council".into(),
                }],
                expected_revision: 0,
                actor: "setup-admin".into(),
                occurred_at_unix_ms: 1_000,
                audit_id: audit_id.into(),
            })
            .unwrap();

        assert!(
            store
                .execute(&format!(
                    "UPDATE asset_caption_track_revisions SET actor='tampered' WHERE audit_id='{audit_id}'"
                ))
                .is_err()
        );
        assert!(
            store
                .execute(&format!(
                    "DELETE FROM asset_caption_track_revisions WHERE audit_id='{audit_id}'"
                ))
                .is_err()
        );
        assert!(
            store
                .execute(&format!(
                    "UPDATE asset_caption_track_revision_cues SET cue_text='tampered' WHERE audit_id='{audit_id}'"
                ))
                .is_err()
        );
        assert!(
            store
                .execute(&format!(
                    "DELETE FROM asset_caption_track_revision_cues WHERE audit_id='{audit_id}'"
                ))
                .is_err()
        );
        assert_eq!(
            store
                .list_asset_caption_track_audit(track_id)
                .unwrap()
                .len(),
            1
        );

        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn reference_station_tier_audit_rejects_update_and_delete_tampering() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-reference-tier-tamper-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .write_reference_station_tier_selection(&ReferenceStationTierWrite {
                expected_revision: None,
                catalog_version: REFERENCE_STATION_CATALOG_VERSION,
                tier: ReferenceStationTier::Tier0BatchStream,
                actor: "setup-admin".into(),
                occurred_at_unix_ms: 1,
            })
            .unwrap();
        assert!(
            store
                .execute(
                    "UPDATE reference_station_tier_audit SET actor='tampered' WHERE audit_id=1"
                )
                .is_err()
        );
        assert!(
            store
                .execute("DELETE FROM reference_station_tier_audit WHERE audit_id=1")
                .is_err()
        );
        assert_eq!(store.list_reference_station_tier_audit().unwrap().len(), 1);
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn legacy_catalog_selection_remains_visible_but_is_not_reported_as_current() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-reference-tier-legacy-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "INSERT INTO reference_station_tier_selection(\
                   singleton,catalog_version,tier,revision,created_at_unix_ms,updated_at_unix_ms,updated_by) \
                 VALUES(1,0,'tier_0_batch_stream',1,1,1,'legacy-import')",
            )
            .unwrap();
        let selection = store
            .read_reference_station_tier_selection()
            .unwrap()
            .unwrap();
        assert_eq!(selection.catalog_version, 0);
        assert!(!selection.uses_current_catalog());
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn auth_schema_migrates_without_creating_an_account_or_bootstrap_default() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute("INSERT INTO media_assets VALUES('preserved','C:\\preserved.ts',1000,'ready')")
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        assert_eq!(
            row_count(&migrated, "SELECT COUNT(*) FROM operator_accounts"),
            0
        );
        assert_eq!(
            row_count(&migrated, "SELECT COUNT(*) FROM operator_bootstrap"),
            0
        );
        assert_eq!(
            row_count(&migrated, "SELECT COUNT(*) FROM operator_sessions"),
            0
        );
        assert_eq!(
            row_count(
                &migrated,
                "SELECT COUNT(*) FROM media_assets WHERE asset_id='preserved'",
            ),
            1
        );
        drop(migrated);

        let reopened = StationStore::open(&database).unwrap();
        assert_eq!(
            row_count(&reopened, "SELECT COUNT(*) FROM operator_accounts"),
            0
        );
        assert_eq!(
            row_count(&reopened, "SELECT COUNT(*) FROM operator_auth_audit"),
            0
        );
        drop(reopened);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn auth_roles_are_constrained_and_audit_rows_are_append_only() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-constraints-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "INSERT INTO operator_accounts(account_id,username,display_name,password_phc,credential_version,enabled,failed_attempts,first_failure_at_unix_ms,locked_until_unix_ms,created_at_unix_ms,updated_at_unix_ms)\
                 VALUES('account-1','operator','Operator','$argon2id$redacted',1,1,0,-1,-1,1,1)",
            )
            .unwrap();
        assert!(
            store
                .execute("INSERT INTO account_roles(account_id,role) VALUES('account-1','owner')")
                .is_err()
        );
        store
            .execute(
                "INSERT INTO operator_auth_audit(event_kind,account_id,session_digest,occurred_at_unix_ms,detail)\
                 VALUES('bootstrap_consumed','account-1',NULL,1,'created')",
            )
            .unwrap();
        assert!(
            store
                .execute("UPDATE operator_auth_audit SET detail='tampered' WHERE audit_id=1")
                .is_err()
        );
        assert!(
            store
                .execute("DELETE FROM operator_auth_audit WHERE audit_id=1")
                .is_err()
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn operator_login_authority_round_trips_roles_and_throttle_state() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-login-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .insert_operator_account(
                &OperatorAccount::new(
                    AccountId::try_from("11111111-2222-4333-8444-555555555555").unwrap(),
                    OperatorUsername::try_from("meeting.operator").unwrap(),
                    OperatorDisplayName::try_from("Meeting Operator").unwrap(),
                    test_password_phc(),
                    BTreeSet::from([OperatorRole::MeetingOperator, OperatorRole::RecordsClerk]),
                    CredentialVersion::new(1).unwrap(),
                    true,
                    1,
                    1,
                )
                .unwrap(),
            )
            .unwrap();
        let username = OperatorUsername::try_from("meeting.operator").unwrap();
        let initial = store.read_operator_login(&username).unwrap().unwrap();
        assert_eq!(initial.account.username(), &username);
        assert_eq!(initial.account.roles().len(), 2);
        assert_eq!(initial.failed_attempts, 0);
        assert_eq!(initial.locked_until_unix_ms, None);

        assert!(
            store
                .record_operator_login_failure(initial.account.account_id(), 10, Some(30))
                .unwrap()
        );
        assert!(
            store
                .record_operator_login_failure(initial.account.account_id(), 11, Some(40))
                .unwrap()
        );
        let throttled = store.read_operator_login(&username).unwrap().unwrap();
        assert_eq!(throttled.failed_attempts, 2);
        assert_eq!(throttled.first_failure_at_unix_ms, Some(10));
        assert_eq!(throttled.locked_until_unix_ms, Some(40));

        assert!(
            store
                .clear_operator_login_failures(initial.account.account_id(), 41)
                .unwrap()
        );
        let cleared = store.read_operator_login(&username).unwrap().unwrap();
        assert_eq!(cleared.failed_attempts, 0);
        assert_eq!(cleared.first_failure_at_unix_ms, None);
        assert_eq!(cleared.locked_until_unix_ms, None);
        assert_eq!(
            row_count(
                &store,
                "SELECT COUNT(*) FROM operator_auth_audit WHERE event_kind IN ('login_failed','login_succeeded')",
            ),
            3
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    fn test_password_phc() -> PasswordPhc {
        station_auth::hash_password(
            &station_auth::OperatorPassword::new("Correct Horse Battery Staple").unwrap(),
        )
        .unwrap()
    }

    fn test_operator_account(version: u64) -> OperatorAccount {
        OperatorAccount::new(
            AccountId::try_from("11111111-2222-4333-8444-555555555555").unwrap(),
            OperatorUsername::try_from("setup.admin").unwrap(),
            OperatorDisplayName::try_from("Setup Administrator").unwrap(),
            test_password_phc(),
            BTreeSet::from([OperatorRole::SetupAdmin]),
            CredentialVersion::new(version).unwrap(),
            true,
            10,
            10,
        )
        .unwrap()
    }

    fn test_operator_session(version: u64, suffix: u8) -> OperatorSession {
        OperatorSession::new(
            SecretDigest::from_bytes([suffix; 32]),
            SecretDigest::from_bytes([suffix.wrapping_add(1); 32]),
            AccountId::try_from("11111111-2222-4333-8444-555555555555").unwrap(),
            CredentialVersion::new(version).unwrap(),
            SecretDigest::from_bytes([99; 32]),
            10,
            10,
            110,
            210,
        )
        .unwrap()
    }

    #[test]
    fn automatic_materialization_actor_is_reserved_from_operator_creation_and_existing_migration() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-reserved-actor-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        let reserved = OperatorAccount::new(
            AccountId::try_from(AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID).unwrap(),
            OperatorUsername::try_from("system.actor").unwrap(),
            OperatorDisplayName::try_from("System Actor").unwrap(),
            test_password_phc(),
            BTreeSet::from([OperatorRole::SetupAdmin]),
            CredentialVersion::new(1).unwrap(),
            true,
            10,
            10,
        )
        .unwrap();
        assert!(store.insert_operator_account(&reserved).is_err());
        assert!(!store.has_operator_accounts().unwrap());

        store
            .execute(
                "DROP TRIGGER operator_accounts_no_reserved_materialization_actor_insert;\
                 DROP TRIGGER operator_accounts_no_reserved_materialization_actor_update;\
                 INSERT INTO operator_accounts(account_id,username,display_name,password_phc,credential_version,enabled,created_at_unix_ms,updated_at_unix_ms) \
                 VALUES('00000000-0000-4000-8000-000000000022','old.system','Old System','x',1,1,1,1)",
            )
            .unwrap();
        drop(store);
        assert!(
            StationStore::open(&database).is_err(),
            "an existing database with a reserved operator identity must fail migration"
        );
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn bootstrap_is_atomic_one_time_and_recovery_rotates_all_session_authority() {
        use std::sync::{Arc, Barrier};

        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-bootstrap-{nonce}.db"));
        let bootstrap =
            BootstrapCredential::new(SecretDigest::from_bytes([7; 32]), 1, 100, None).unwrap();
        let store = StationStore::open(&database).unwrap();
        assert!(!store.has_operator_accounts().unwrap());
        assert!(store.provision_operator_bootstrap(&bootstrap).unwrap());
        drop(store);

        let barrier = Arc::new(Barrier::new(2));
        let mut contenders = Vec::new();
        for _ in 0..2 {
            let path = database.clone();
            let barrier = Arc::clone(&barrier);
            contenders.push(std::thread::spawn(move || {
                let store = StationStore::open(path).unwrap();
                let installation = OperatorBootstrapInstallation {
                    account: test_operator_account(1),
                    session: test_operator_session(1, 20),
                    recovery_digests: vec![
                        SecretDigest::from_bytes([40; 32]),
                        SecretDigest::from_bytes([41; 32]),
                    ],
                };
                barrier.wait();
                store
                    .consume_operator_bootstrap(
                        &SecretDigest::from_bytes([7; 32]),
                        &installation,
                        20,
                    )
                    .unwrap()
            }));
        }
        let outcomes = contenders
            .into_iter()
            .map(|contender| contender.join().unwrap())
            .collect::<Vec<_>>();
        assert_eq!(outcomes.iter().filter(|outcome| **outcome).count(), 1);

        let store = StationStore::open(&database).unwrap();
        assert!(store.has_operator_accounts().unwrap());
        assert_eq!(
            row_count(&store, "SELECT COUNT(*) FROM operator_accounts"),
            1
        );
        assert_eq!(
            row_count(&store, "SELECT COUNT(*) FROM operator_sessions"),
            1
        );
        assert_eq!(
            row_count(&store, "SELECT COUNT(*) FROM operator_recovery_hashes"),
            2
        );
        assert_eq!(
            row_count(
                &store,
                "SELECT COUNT(*) FROM account_roles WHERE role='setup_admin'",
            ),
            1
        );
        assert!(
            store
                .validate_operator_session(
                    &SecretDigest::from_bytes([20; 32]),
                    &SecretDigest::from_bytes([21; 32]),
                    30,
                    &SecretDigest::from_bytes([99; 32]),
                )
                .unwrap()
                .is_some()
        );

        let rotated = store
            .consume_operator_recovery(
                test_operator_account(1).account_id(),
                &SecretDigest::from_bytes([40; 32]),
                &test_password_phc(),
                40,
            )
            .unwrap();
        assert_eq!(rotated.unwrap().get(), 2);
        assert!(
            store
                .consume_operator_recovery(
                    test_operator_account(1).account_id(),
                    &SecretDigest::from_bytes([40; 32]),
                    &test_password_phc(),
                    41,
                )
                .unwrap()
                .is_none()
        );
        assert_eq!(
            row_count(&store, "SELECT COUNT(*) FROM operator_sessions"),
            0
        );
        assert_eq!(
            store
                .read_operator_login(&OperatorUsername::try_from("setup.admin").unwrap())
                .unwrap()
                .unwrap()
                .account
                .credential_version()
                .get(),
            2
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn expired_bootstrap_is_fail_closed_and_can_be_reissued_only_without_accounts() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-auth-bootstrap-expiry-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        let expired =
            BootstrapCredential::new(SecretDigest::from_bytes([1; 32]), 1, 10, None).unwrap();
        assert!(store.provision_operator_bootstrap(&expired).unwrap());
        let installation = OperatorBootstrapInstallation {
            account: test_operator_account(1),
            session: test_operator_session(1, 2),
            recovery_digests: vec![SecretDigest::from_bytes([4; 32])],
        };
        assert!(
            !store
                .consume_operator_bootstrap(
                    &expired.token_digest(),
                    &installation,
                    expired.expires_at_unix_ms(),
                )
                .unwrap()
        );
        let premature =
            BootstrapCredential::new(SecretDigest::from_bytes([5; 32]), 9, 20, None).unwrap();
        assert!(!store.provision_operator_bootstrap(&premature).unwrap());
        let replacement =
            BootstrapCredential::new(SecretDigest::from_bytes([6; 32]), 10, 20, None).unwrap();
        assert!(store.provision_operator_bootstrap(&replacement).unwrap());
        assert!(
            store
                .consume_operator_bootstrap(&replacement.token_digest(), &installation, 11)
                .unwrap()
        );
        let later =
            BootstrapCredential::new(SecretDigest::from_bytes([7; 32]), 20, 30, None).unwrap();
        assert!(!store.provision_operator_bootstrap(&later).unwrap());
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn account_session_capacity_is_bounded_at_eight_active_sessions() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-session-cap-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .insert_operator_account(&test_operator_account(1))
            .unwrap();
        for suffix in 80..88 {
            store
                .create_operator_session(&test_operator_session(1, suffix), 20)
                .unwrap();
        }
        assert!(
            store
                .create_operator_session(&test_operator_session(1, 88), 20)
                .unwrap_err()
                .contains("maximum of 8 active sessions")
        );
        assert_eq!(
            row_count(&store, "SELECT COUNT(*) FROM operator_sessions"),
            8
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn plaintext_password_tokens_and_recovery_codes_never_reach_sqlite() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-no-plaintext-{nonce}.db"));
        let password_plaintext = "A unique station password 9!";
        let password = station_auth::OperatorPassword::new(password_plaintext).unwrap();
        let password_phc = station_auth::hash_password(&password).unwrap();
        let bootstrap_token = station_auth::generate_secret_token().unwrap();
        let session_token = station_auth::generate_secret_token().unwrap();
        let csrf_token = station_auth::generate_secret_token().unwrap();
        let recovery = station_auth::generate_recovery_codes().unwrap();
        let recovery_plaintexts = recovery
            .display_codes
            .iter()
            .map(|code| code.expose_secret().to_owned())
            .collect::<Vec<_>>();
        let account = OperatorAccount::new(
            AccountId::try_from("11111111-2222-4333-8444-555555555555").unwrap(),
            OperatorUsername::try_from("secure.admin").unwrap(),
            OperatorDisplayName::try_from("Secure Administrator").unwrap(),
            password_phc,
            BTreeSet::from([OperatorRole::SetupAdmin]),
            CredentialVersion::new(1).unwrap(),
            true,
            10,
            10,
        )
        .unwrap();
        let session = OperatorSession::new(
            station_auth::digest_secret(&session_token),
            station_auth::digest_secret(&csrf_token),
            account.account_id().clone(),
            account.credential_version(),
            SecretDigest::from_bytes([99; 32]),
            10,
            10,
            110,
            210,
        )
        .unwrap();
        let store = StationStore::open(&database).unwrap();
        store
            .provision_operator_bootstrap(
                &BootstrapCredential::new(
                    station_auth::digest_secret(&bootstrap_token),
                    1,
                    100,
                    None,
                )
                .unwrap(),
            )
            .unwrap();
        assert!(
            store
                .consume_operator_bootstrap(
                    &station_auth::digest_secret(&bootstrap_token),
                    &OperatorBootstrapInstallation {
                        account,
                        session,
                        recovery_digests: recovery.storage_digests.to_vec(),
                    },
                    20,
                )
                .unwrap()
        );
        drop(store);

        let mut forbidden = vec![
            password_plaintext.to_owned(),
            bootstrap_token.expose_secret().to_owned(),
            session_token.expose_secret().to_owned(),
            csrf_token.expose_secret().to_owned(),
        ];
        forbidden.extend(recovery_plaintexts);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            if let Ok(bytes) = std::fs::read(&candidate) {
                for secret in &forbidden {
                    assert!(
                        !bytes
                            .windows(secret.len())
                            .any(|window| window == secret.as_bytes()),
                        "plaintext auth secret was found in {}",
                        candidate.display()
                    );
                }
            }
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn sessions_touch_revoke_and_purge_by_boot_or_credential_version() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-sessions-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .insert_operator_account(&test_operator_account(1))
            .unwrap();
        let first = test_operator_session(1, 50);
        store.create_operator_session(&first, 11).unwrap();
        assert!(
            store
                .touch_operator_session(&first.session_digest(), -1, 120)
                .is_err()
        );
        assert!(
            store
                .revoke_operator_session(&first.session_digest(), -1)
                .is_err()
        );
        assert!(
            store
                .purge_operator_sessions_for_boot(&first.boot_id(), -1)
                .is_err()
        );
        assert!(
            store
                .purge_operator_sessions_for_credential_version(
                    first.account_id(),
                    CredentialVersion::new(1).unwrap(),
                    -1,
                )
                .is_err()
        );
        assert!(
            store
                .touch_operator_session(&first.session_digest(), 20, 120)
                .unwrap()
        );
        assert!(
            store
                .revoke_operator_session(&first.session_digest(), 21)
                .unwrap()
        );
        assert!(
            store
                .validate_operator_session(
                    &first.session_digest(),
                    &first.csrf_digest(),
                    22,
                    &first.boot_id(),
                )
                .unwrap()
                .is_none()
        );

        let other_boot = OperatorSession::new(
            SecretDigest::from_bytes([60; 32]),
            SecretDigest::from_bytes([61; 32]),
            first.account_id().clone(),
            CredentialVersion::new(1).unwrap(),
            SecretDigest::from_bytes([98; 32]),
            10,
            10,
            110,
            210,
        )
        .unwrap();
        store.create_operator_session(&other_boot, 12).unwrap();
        assert_eq!(
            store
                .purge_operator_sessions_for_boot(&first.boot_id(), 30)
                .unwrap(),
            1
        );
        let current_boot_old_credential = test_operator_session(1, 70);
        store
            .create_operator_session(&current_boot_old_credential, 31)
            .unwrap();
        assert_eq!(
            store
                .purge_operator_sessions_for_credential_version(
                    first.account_id(),
                    CredentialVersion::new(2).unwrap(),
                    32,
                )
                .unwrap(),
            2
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn loudness_proof_history_is_rejected_by_update_and_delete_triggers() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-proof-trigger-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "INSERT INTO channel_output_loudness_proofs(\
                 proof_id, channel_id, sink_id, target_lufs_milli, measured_lufs_milli,\
                 tolerance_lufs_milli, outcome, receiver_acceptance, captured_at_unix_ms,\
                 sample_duration_ms, capture_bytes, capture_sha256, candidate_version, source_commit,\
                 configuration_hash, measurement_engine, measurement_method, detail, artifact_relative_path)\
                 VALUES('11111111-2222-4333-8444-555555555555',\
                   '22222222-3333-4444-8555-666666666666',\
                   '33333333-4444-4555-8666-777777777777', -24000, -24000, 2000,\
                   'pass', 'unknown', 1, 1, 1,\
                   'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',\
                   '0.3.0', '',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',\
                   'test', 'test', '', '')",
            )
            .unwrap();
        assert!(
            store
                .execute(
                    "UPDATE channel_output_loudness_proofs SET detail='tampered'\
                 WHERE proof_id='11111111-2222-4333-8444-555555555555'",
                )
                .is_err()
        );
        assert!(
            store
                .execute(
                    "DELETE FROM channel_output_loudness_proofs\
                 WHERE proof_id='11111111-2222-4333-8444-555555555555'",
                )
                .is_err()
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn bare_database_filename_uses_the_current_directory_as_its_data_root() {
        assert_eq!(
            station_data_root(Path::new("station.db")).unwrap(),
            std::env::current_dir().unwrap()
        );
    }

    #[test]
    fn adds_the_default_output_profile_to_existing_channels_without_losing_rows() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-profile-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE channels;\
                 CREATE TABLE channels (\
                   channel_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,\
                   udp_destination TEXT NOT NULL, enabled INTEGER NOT NULL CHECK (enabled IN (0,1)));\
                 INSERT INTO channels VALUES(\
                   '8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e','Government','127.0.0.1:5500',1);",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let channels = migrated.list_channels().unwrap();
        assert_eq!(channels.len(), 1);
        assert_eq!(channels[0].display_name, "Government");
        assert_eq!(channels[0].output_profile, OutputProfileId::Proof360p30);
        assert_eq!(channels[0].output_sinks.len(), 1);
        assert_eq!(channels[0].output_sinks[0].sink_id, channels[0].channel_id);
        assert_eq!(channels[0].output_sinks[0].destination, "127.0.0.1:5500");
        assert_eq!(channels[0].output_sinks[0].loudness_target_lufs, -16.0);
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn expands_the_existing_udp_sink_table_for_srt_without_losing_rows() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-srt-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE channel_output_sinks;\
                 CREATE TABLE channel_output_sinks (\
                   channel_id TEXT NOT NULL, sink_id TEXT NOT NULL, label TEXT NOT NULL,\
                   kind TEXT NOT NULL CHECK (kind IN ('udp-ts')), destination TEXT NOT NULL,\
                   loudness_target_milli INTEGER NOT NULL,\
                   required INTEGER NOT NULL CHECK (required IN (0,1)),\
                   enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
                   PRIMARY KEY(channel_id, sink_id), UNIQUE(channel_id, label),\
                   FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE);\
                 INSERT INTO channels(channel_id, display_name, udp_destination, output_profile, enabled)\
                   VALUES('11111111-2222-4333-8444-555555555555','Preserved UDP',\
                     '127.0.0.1:5500','atsc-720p5994',0);\
                 INSERT INTO channel_output_sinks VALUES(\
                   '11111111-2222-4333-8444-555555555555',\
                   '11111111-2222-4333-8444-555555555555',\
                   'Primary transport','udp-ts','127.0.0.1:5500',-24000,1,1)",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let srt = ChannelConfiguration {
            channel_id: "22222222-3333-4444-8555-666666666666".into(),
            display_name: "SRT output".into(),
            output_profile: OutputProfileId::Atsc720p5994,
            output_sinks: vec![OutputSinkConfiguration::required_srt(
                "33333333-4444-4555-8666-777777777777",
                "SRT caller",
                "srt://127.0.0.1:5600",
                -24.0,
            )],
            enabled: false,
        };
        migrated.put_channel(&srt).unwrap();
        let channels = migrated.list_channels().unwrap();
        assert_eq!(channels.len(), 2);
        assert!(channels.iter().any(|channel| {
            channel.display_name == "Preserved UDP"
                && channel.output_sinks[0].kind == OutputSinkKind::UdpTs
        }));
        assert!(channels.iter().any(|channel| channel == &srt));
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn adds_headend_contract_columns_without_losing_existing_output_sinks() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-headend-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE srt_output_secrets;\
                 DROP TABLE channel_output_sinks;\
                 CREATE TABLE channel_output_sinks (\
                   channel_id TEXT NOT NULL, sink_id TEXT NOT NULL, label TEXT NOT NULL,\
                   kind TEXT NOT NULL CHECK (kind IN ('udp-ts','srt')), destination TEXT NOT NULL,\
                   loudness_target_milli INTEGER NOT NULL,\
                   required INTEGER NOT NULL CHECK (required IN (0,1)),\
                   enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
                   PRIMARY KEY(channel_id, sink_id), UNIQUE(channel_id, label),\
                   FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE);\
                 INSERT INTO channels(channel_id, display_name, udp_destination, output_profile, enabled)\
                   VALUES('11111111-2222-4333-8444-555555555555','Preserved output',\
                     '127.0.0.1:5500','atsc-720p5994',0);\
                 INSERT INTO channel_output_sinks VALUES(\
                   '11111111-2222-4333-8444-555555555555',\
                   '11111111-2222-4333-8444-555555555555',\
                   'Primary transport','udp-ts','127.0.0.1:5500',-24000,1,1)",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let channel = migrated
            .list_channels()
            .unwrap()
            .into_iter()
            .find(|channel| channel.display_name == "Preserved output")
            .unwrap();
        assert_eq!(channel.output_sinks.len(), 1);
        assert_eq!(channel.output_sinks[0].destination, "127.0.0.1:5500");
        assert!(channel.output_sinks[0].headend.is_none());
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn migrates_the_pre_completion_dispatch_constraint_without_losing_rows() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE commissioning_support_bundle_receipts;\
                 DROP TABLE commit_reports;\
                 CREATE TABLE commit_reports (\
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,\
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK \
                   (dispatch_status IN ('pending','queued','acknowledged','error','cancelled')),\
                   operator_notes TEXT NOT NULL);\
                 INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
                   VALUES('22222222-2222-4222-8222-222222222222',1,'11111111-1111-4111-8111-111111111111',\
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','Legacy item',0,1000,'committed');\
                 INSERT INTO commit_reports VALUES('legacy','plan','11111111-1111-4111-8111-111111111111',\
                   '22222222-2222-4222-8222-222222222222',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','operator',1,'acknowledged','kept');",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let report = migrated.read_commit_report("legacy").unwrap().unwrap();
        assert_eq!(report.operator_notes, "kept");
        assert_eq!(report.schedule_item_revision, 1);
        assert!(
            migrated
                .advance_dispatch_status(
                    "legacy",
                    DispatchStatus::Acknowledged,
                    DispatchStatus::Completed,
                )
                .unwrap()
        );
        assert_eq!(
            migrated
                .read_commit_report("legacy")
                .unwrap()
                .unwrap()
                .dispatch_status,
            DispatchStatus::Completed
        );
        migrated
            .append_commissioning_support_bundle_receipt(&CommissioningSupportBundleReceipt {
                bundle_id: "support-bundle-after-migration".into(),
                report_id: "legacy".into(),
                archive_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                    .into(),
                archive_size_bytes: 4_096,
                created_at_unix_ms: 2,
                created_by: "setup-admin".into(),
            })
            .unwrap();
        assert_eq!(
            migrated
                .list_commissioning_support_bundle_receipts(10)
                .unwrap()
                .len(),
            1
        );
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn fails_closed_migrating_legacy_commit_reports_with_invalid_schedule_bindings() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-invalid-commit-report-migration-{nonce}.db"
        ));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE commissioning_support_bundle_receipts;\
                 DROP TABLE commit_reports;\
                 CREATE TABLE commit_reports (\
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,\
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK \
                   (dispatch_status IN ('pending','queued','acknowledged','error','cancelled')),\
                   operator_notes TEXT NOT NULL);\
                 INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
                   VALUES('22222222-2222-4222-8222-222222222222',1,'11111111-1111-4111-8111-111111111111',\
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','Legacy item',0,1000,'committed');\
                 INSERT INTO commit_reports VALUES('legacy-missing','plan','11111111-1111-4111-8111-111111111111',\
                   '33333333-3333-4333-8333-333333333333',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','operator',1,'pending','missing item');\
                 INSERT INTO commit_reports VALUES('legacy-mismatched','plan','44444444-4444-4444-8444-444444444444',\
                   '22222222-2222-4222-8222-222222222222',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','operator',2,'pending','mismatched channel');",
            )
            .unwrap();
        drop(store);

        let error = match StationStore::open(&database) {
            Ok(_) => panic!("legacy commit-report migration accepted invalid schedule bindings"),
            Err(error) => error,
        };
        assert!(
            error.starts_with("cannot migrate commit reports: report legacy-")
                && (error.contains("schedule binding is missing")
                    || error.contains("no exact committed schedule-history binding")),
            "unexpected migration error: {error}"
        );

        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn legacy_commit_report_migration_binds_the_exact_historical_commit_after_recommit() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-historical-commit-report-migration-{nonce}.db"
        ));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE commissioning_support_bundle_receipts;\
                 DROP TABLE commit_reports;\
                 CREATE TABLE commit_reports (\
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,\
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK \
                   (dispatch_status IN ('pending','queued','acknowledged','error','cancelled')),\
                   operator_notes TEXT NOT NULL);\
                 INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
                   VALUES('22222222-2222-4222-8222-222222222222',4,'11111111-1111-4111-8111-111111111111',\
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','Recommitted item',0,1000,'committed');\
                 INSERT INTO commit_reports VALUES('legacy-first-commit','plan','11111111-1111-4111-8111-111111111111',\
                   '22222222-2222-4222-8222-222222222222',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','operator-one',100,'acknowledged','first commit');",
            )
            .unwrap();
        let initial = ScheduleItem {
            item_id: "22222222-2222-4222-8222-222222222222".into(),
            revision: 1,
            channel_id: "11111111-1111-4111-8111-111111111111".into(),
            asset_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            title: "Recommitted item".into(),
            starts_at_unix_ms: 0,
            duration_ms: 1_000,
            state: ScheduleState::Draft,
        };
        let first_commit = ScheduleItem {
            revision: 2,
            state: ScheduleState::Committed,
            ..initial.clone()
        };
        let revised_draft = ScheduleItem {
            revision: 3,
            state: ScheduleState::Draft,
            ..initial.clone()
        };
        let second_commit = ScheduleItem {
            revision: 4,
            state: ScheduleState::Committed,
            ..initial.clone()
        };
        insert_schedule_history_for_legacy_migration(&store, &initial, "editor", 50);
        insert_schedule_history_for_legacy_migration(&store, &first_commit, "operator-one", 100);
        insert_schedule_history_for_legacy_migration(&store, &revised_draft, "editor", 150);
        insert_schedule_history_for_legacy_migration(&store, &second_commit, "operator-two", 200);
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        assert_eq!(
            migrated
                .read_commit_report("legacy-first-commit")
                .unwrap()
                .unwrap()
                .schedule_item_revision,
            2
        );
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn legacy_commit_report_migration_rejects_ambiguous_historical_bindings() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-ambiguous-commit-report-migration-{nonce}.db"
        ));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE commissioning_support_bundle_receipts;\
                 DROP TABLE commit_reports;\
                 CREATE TABLE commit_reports (\
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,\
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK \
                   (dispatch_status IN ('pending','queued','acknowledged','error','cancelled')),\
                   operator_notes TEXT NOT NULL);\
                 INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
                   VALUES('22222222-2222-4222-8222-222222222222',3,'11111111-1111-4111-8111-111111111111',\
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','Ambiguous item',0,1000,'committed');\
                 INSERT INTO commit_reports VALUES('legacy-ambiguous','plan','11111111-1111-4111-8111-111111111111',\
                   '22222222-2222-4222-8222-222222222222',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','operator',100,'pending','ambiguous');",
            )
            .unwrap();
        let initial = ScheduleItem {
            item_id: "22222222-2222-4222-8222-222222222222".into(),
            revision: 1,
            channel_id: "11111111-1111-4111-8111-111111111111".into(),
            asset_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            title: "Ambiguous item".into(),
            starts_at_unix_ms: 0,
            duration_ms: 1_000,
            state: ScheduleState::Draft,
        };
        let first_commit = ScheduleItem {
            revision: 2,
            state: ScheduleState::Committed,
            ..initial.clone()
        };
        let second_commit = ScheduleItem {
            revision: 3,
            state: ScheduleState::Committed,
            ..initial.clone()
        };
        insert_schedule_history_for_legacy_migration(&store, &initial, "editor", 50);
        insert_schedule_history_for_legacy_migration(&store, &first_commit, "operator", 100);
        insert_schedule_history_for_legacy_migration(&store, &second_commit, "operator", 100);
        drop(store);

        let error = match StationStore::open(&database) {
            Ok(_) => panic!("legacy commit-report migration accepted ambiguous history"),
            Err(error) => error,
        };
        assert!(error.contains(
            "cannot migrate commit reports: report legacy-ambiguous has ambiguous committed schedule-history bindings"
        ));
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn legacy_commit_report_migration_rejects_missing_historical_binding_after_revision_one() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-missing-history-commit-report-migration-{nonce}.db"
        ));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE commissioning_support_bundle_receipts;\
                 DROP TABLE commit_reports;\
                 CREATE TABLE commit_reports (\
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,\
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK \
                   (dispatch_status IN ('pending','queued','acknowledged','error','cancelled')),\
                   operator_notes TEXT NOT NULL);\
                 INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
                   VALUES('22222222-2222-4222-8222-222222222222',2,'11111111-1111-4111-8111-111111111111',\
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','Missing history item',0,1000,'committed');\
                 INSERT INTO commit_reports VALUES('legacy-missing-history','plan','11111111-1111-4111-8111-111111111111',\
                   '22222222-2222-4222-8222-222222222222',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','operator',100,'pending','missing');",
            )
            .unwrap();
        let initial = ScheduleItem {
            item_id: "22222222-2222-4222-8222-222222222222".into(),
            revision: 1,
            channel_id: "11111111-1111-4111-8111-111111111111".into(),
            asset_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            title: "Missing history item".into(),
            starts_at_unix_ms: 0,
            duration_ms: 1_000,
            state: ScheduleState::Draft,
        };
        insert_schedule_history_for_legacy_migration(&store, &initial, "editor", 50);
        drop(store);

        let error = match StationStore::open(&database) {
            Ok(_) => panic!("legacy commit-report migration accepted missing history"),
            Err(error) => error,
        };
        assert!(error.contains(
            "cannot migrate commit reports: report legacy-missing-history has no exact committed schedule-history binding"
        ));
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn migrates_completed_dispatch_schema_to_missed_without_losing_rows() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-missed-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE commissioning_support_bundle_receipts;\
                 DROP TABLE commit_reports;\
                 CREATE TABLE commit_reports (\
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,\
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK \
                   (dispatch_status IN ('pending','queued','acknowledged','completed','error','cancelled')),\
                   operator_notes TEXT NOT NULL);\
                 CREATE INDEX commit_reports_channel_time ON commit_reports(channel_id, approved_at_unix_ms);\
                 INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
                   VALUES('22222222-2222-4222-8222-222222222222',1,'11111111-1111-4111-8111-111111111111',\
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','Legacy pending',0,1000,'committed');\
                 INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
                   VALUES('33333333-3333-4333-8333-333333333333',1,'11111111-1111-4111-8111-111111111111',\
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','Legacy completed',1000,1000,'committed');\
                 INSERT INTO commit_reports VALUES('legacy-pending','plan','11111111-1111-4111-8111-111111111111',\
                   '22222222-2222-4222-8222-222222222222',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','operator',1,'pending','kept pending');\
                 INSERT INTO commit_reports VALUES('legacy-completed','plan','11111111-1111-4111-8111-111111111111',\
                   '33333333-3333-4333-8333-333333333333',\
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','operator',2,'completed','kept completed');",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        assert!(
            migrated
                .advance_dispatch_status(
                    "legacy-pending",
                    DispatchStatus::Pending,
                    DispatchStatus::Missed,
                )
                .unwrap()
        );
        assert_eq!(
            migrated
                .read_commit_report("legacy-completed")
                .unwrap()
                .unwrap()
                .dispatch_status,
            DispatchStatus::Completed
        );
        drop(migrated);

        let reopened = StationStore::open(&database).unwrap();
        assert_eq!(
            reopened
                .read_commit_report("legacy-pending")
                .unwrap()
                .unwrap()
                .dispatch_status,
            DispatchStatus::Missed
        );
        assert_eq!(
            reopened
                .read_commit_report("legacy-completed")
                .unwrap()
                .unwrap()
                .dispatch_status,
            DispatchStatus::Completed
        );
        drop(reopened);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn commit_report_status_migration_preserves_existing_support_receipt_foreign_keys() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-commit-report-fk-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "PRAGMA foreign_keys=OFF;
                 DROP TABLE commit_reports;
                 CREATE TABLE commit_reports (
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK
                   (dispatch_status IN ('pending','queued','acknowledged','completed','error','cancelled')),
                   operator_notes TEXT NOT NULL);
                 INSERT INTO schedule_items(item_id, revision, channel_id, asset_id, title,
                   starts_at_unix_ms, duration_ms, state)
                   VALUES('22222222-2222-4222-8222-222222222222',1,
                     '11111111-1111-4111-8111-111111111111',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                     'Legacy report item',0,1000,'committed');
                 INSERT INTO commit_reports VALUES('legacy-fk-report','plan','11111111-1111-4111-8111-111111111111',
                   '22222222-2222-4222-8222-222222222222',
                   'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                   'operator',1,'completed','kept');
                 INSERT INTO commissioning_support_bundle_receipts VALUES(
                   'support-bundle-before-migration','legacy-fk-report',
                   'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',4096,2,'setup-admin');
                 PRAGMA foreign_keys=ON;",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        assert_eq!(
            migrated
                .read_commit_report("legacy-fk-report")
                .unwrap()
                .unwrap()
                .schedule_item_revision,
            1
        );
        let receipts = migrated
            .list_commissioning_support_bundle_receipts(10)
            .unwrap();
        assert_eq!(receipts.len(), 1);
        assert_eq!(receipts[0].bundle_id, "support-bundle-before-migration");
        migrated
            .append_commissioning_support_bundle_receipt(&CommissioningSupportBundleReceipt {
                bundle_id: "support-bundle-after-migration".into(),
                report_id: "legacy-fk-report".into(),
                archive_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                    .into(),
                archive_size_bytes: 4096,
                created_at_unix_ms: 3,
                created_by: "setup-admin".into(),
            })
            .unwrap();
        assert_eq!(
            migrated
                .list_commissioning_support_bundle_receipts(10)
                .unwrap()
                .len(),
            2
        );
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn adds_retry_scheduling_to_an_existing_ingest_database_without_losing_jobs() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-ingest-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE media_ingest_jobs;\
                 CREATE TABLE media_ingest_jobs (\
                   job_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_path TEXT NOT NULL,\
                   status TEXT NOT NULL, progress_percent INTEGER NOT NULL, created_at_unix_ms INTEGER NOT NULL,\
                   started_at_unix_ms INTEGER NOT NULL, completed_at_unix_ms INTEGER NOT NULL,\
                   error_detail TEXT NOT NULL, asset_id TEXT NOT NULL, attempts INTEGER NOT NULL);\
                 INSERT INTO media_ingest_jobs VALUES(\
                   '11111111-2222-4333-8444-555555555555','watch_folder','C:\\incoming\\meeting.ts',\
                   'pending',0,1,-1,-1,'','','0');",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let job = migrated
            .read_ingest_job("11111111-2222-4333-8444-555555555555")
            .unwrap()
            .unwrap();
        assert_eq!(job.source_kind, IngestSourceKind::WatchFolder);
        assert_eq!(job.next_attempt_at_unix_ms, None);
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn migrates_existing_alert_deliveries_and_seeds_rules_without_losing_history() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-alert-rule-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "INSERT INTO alert_events VALUES(\
                   'legacy-event','off-air','critical','resolved','legacy-channel','Legacy alert','Kept','S8',1,2,2,1,-1,'');\
                 INSERT INTO alert_channels VALUES(\
                   '5eb469af-f3c7-4cc5-b26e-865807b3e934','webhook','Legacy pager',0,\
                   'https://alerts.example.org/…',1,'','',X'01',1,1);\
                 DROP INDEX alert_deliveries_due;\
                 DROP TABLE alert_deliveries;\
                 CREATE TABLE alert_deliveries (\
                   delivery_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, alert_channel_id TEXT NOT NULL,\
                   notification_kind TEXT NOT NULL CHECK (notification_kind IN ('firing','resolved','test')),\
                   state TEXT NOT NULL CHECK (state IN ('pending','leased','sent','dead_letter')), attempts INTEGER NOT NULL,\
                   next_attempt_at_unix_ms INTEGER NOT NULL, lease_expires_at_unix_ms INTEGER NOT NULL,\
                   last_error TEXT NOT NULL, signature TEXT NOT NULL, payload_json TEXT NOT NULL,\
                   created_at_unix_ms INTEGER NOT NULL, updated_at_unix_ms INTEGER NOT NULL, sent_at_unix_ms INTEGER NOT NULL,\
                   UNIQUE(event_id, alert_channel_id, notification_kind));\
                 INSERT INTO alert_deliveries VALUES(\
                   'legacy-delivery','legacy-event','5eb469af-f3c7-4cc5-b26e-865807b3e934','resolved','sent',1,2,-1,'','proof',\
                   '{\"notification_kind\":\"resolved\",\"event_id\":\"legacy-event\",\"condition\":\"off-air\",\"severity\":\"critical\",\"resource_ref\":\"legacy-channel\",\"summary\":\"Legacy alert\",\"detail\":\"Kept\",\"source_section\":\"S8\",\"first_observed_at_unix_ms\":1,\"last_observed_at_unix_ms\":2,\"resolved_at_unix_ms\":2,\"occurrence_count\":1}',2,2,2);",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let delivery = migrated
            .read_alert_delivery("legacy-delivery")
            .unwrap()
            .unwrap();
        assert_eq!(delivery.notification_kind, AlertNotificationKind::Resolved);
        assert_eq!(delivery.state, AlertDeliveryState::Sent);
        assert_eq!(migrated.list_alert_rules().unwrap().len(), 21);
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn password_rotation_is_version_cas_and_invalidates_every_session() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-auth-password-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        let account = test_operator_account(1);
        store.insert_operator_account(&account).unwrap();
        store
            .create_operator_session(&test_operator_session(1, 90), 11)
            .unwrap();
        let next = store
            .rotate_operator_password(
                account.account_id(),
                CredentialVersion::new(1).unwrap(),
                &test_password_phc(),
                20,
            )
            .unwrap()
            .unwrap();
        assert_eq!(next.get(), 2);
        assert_eq!(
            row_count(&store, "SELECT COUNT(*) FROM operator_sessions"),
            0
        );
        assert!(
            store
                .rotate_operator_password(
                    account.account_id(),
                    CredentialVersion::new(1).unwrap(),
                    &test_password_phc(),
                    21,
                )
                .unwrap()
                .is_none()
        );
        assert_eq!(
            row_count(
                &store,
                "SELECT COUNT(*) FROM operator_auth_audit WHERE event_kind='password_changed'",
            ),
            1
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn commissioning_support_bundle_receipts_reject_update_and_delete_tampering() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-support-receipt-trigger-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "INSERT INTO commit_reports VALUES(\
                   'report-guided','plan-guided','channel-guided','item-guided',1,'asset-guided',\
                   'operator',1,'completed','');\
                 INSERT INTO commissioning_support_bundle_receipts VALUES(\
                   'support-bundle-1','report-guided',\
                   'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',\
                   4096,2,'setup-admin');",
            )
            .unwrap();
        assert!(
            store
                .execute("UPDATE commissioning_support_bundle_receipts SET created_by='attacker'")
                .unwrap_err()
                .contains("append-only")
        );
        assert!(
            store
                .execute("DELETE FROM commissioning_support_bundle_receipts")
                .unwrap_err()
                .contains("append-only")
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn custom_metadata_rows_and_history_fail_closed_on_tampering() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-custom-metadata-tamper-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        let schema = MetadataSchema {
            schema_id: "11111111-2222-4333-8444-555555555555".into(),
            revision: 1,
            fields: vec![station_metadata::MetadataFieldDefinition {
                field_id: "22222222-2222-4333-8444-555555555555".into(),
                key: "title".into(),
                label: "Title".into(),
                description: None,
                required: true,
                public: false,
                faceted: false,
                field_type: station_metadata::MetadataFieldType::Text {
                    min_chars: 1,
                    max_chars: 100,
                },
            }],
        };
        let record = AssetMetadataRecord {
            metadata_id: "33333333-2222-4333-8444-555555555555".into(),
            asset_id: "a".repeat(64),
            schema_id: schema.schema_id.clone(),
            schema_revision: 1,
            revision: 1,
            values: vec![station_metadata::AssetMetadataValue {
                field_id: schema.fields[0].field_id.clone(),
                value: station_metadata::MetadataFieldValue::Text("meeting".into()),
            }],
        };
        store
            .write_custom_metadata_schema(&schema, None, "setup-admin", 1)
            .unwrap();
        store
            .write_asset_metadata_record(&record, None, "setup-admin", 2)
            .unwrap();
        let tampered = serde_json::to_string(&MetadataSchema {
            schema_id: "44444444-2222-4333-8444-555555555555".into(),
            ..schema.clone()
        })
        .unwrap();
        let statement = store
            .prepare("UPDATE custom_metadata_schemas SET record_json=?1 WHERE schema_id=?2")
            .unwrap();
        let tampered = c_string(&tampered, "tampered schema").unwrap();
        let schema_id = c_string(&schema.schema_id, "schema identity").unwrap();
        bind_text(store.database, statement.raw, 1, &tampered).unwrap();
        bind_text(store.database, statement.raw, 2, &schema_id).unwrap();
        expect_done(store.database, statement.raw).unwrap();
        assert!(
            store
                .read_custom_metadata_schema(&schema.schema_id)
                .is_err()
        );
        let tampered_record = serde_json::to_string(&AssetMetadataRecord {
            metadata_id: "55555555-2222-4333-8444-555555555555".into(),
            ..record.clone()
        })
        .unwrap();
        let statement = store
            .prepare("UPDATE asset_metadata_records SET record_json=?1 WHERE metadata_id=?2")
            .unwrap();
        let tampered_record = c_string(&tampered_record, "tampered asset metadata record").unwrap();
        let metadata_id = c_string(&record.metadata_id, "asset metadata identity").unwrap();
        bind_text(store.database, statement.raw, 1, &tampered_record).unwrap();
        bind_text(store.database, statement.raw, 2, &metadata_id).unwrap();
        expect_done(store.database, statement.raw).unwrap();
        assert!(
            store
                .read_asset_metadata_record(&record.metadata_id)
                .is_err()
        );
        assert!(
            store
                .execute("DELETE FROM custom_metadata_schemas")
                .unwrap_err()
                .contains("cannot be deleted")
        );
        assert!(
            store
                .execute("UPDATE custom_metadata_schema_history SET changed_by='attacker'")
                .unwrap_err()
                .contains("append-only")
        );
        assert!(
            store
                .execute("DELETE FROM asset_metadata_records")
                .unwrap_err()
                .contains("cannot be deleted")
        );
        assert!(
            store
                .execute("DELETE FROM asset_metadata_record_history")
                .unwrap_err()
                .contains("append-only")
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn migrates_parent_transport_schema_and_fails_closed_for_unrepresentable_legacy_records() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-transport-migration-{nonce}.db"));
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        let store = StationStore::open(&database).unwrap();
        store
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Legacy transport proof".into(),
                output_profile: OutputProfileId::Proof360p30,
                output_sinks: vec![OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Legacy sink",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
        let legacy_target = serde_json::json!({
            "target_id": "target-1", "label": "Legacy receiver", "protocol": "https_api",
            "endpoint": "https://receiver.example.test:8443", "credential_configured": true,
            "revision": 1, "created_at_unix_ms": 1, "updated_at_unix_ms": 1,
            "updated_by": "operator"
        });
        let legacy_fake_target = serde_json::json!({
            "target_id": "fake-target", "label": "Legacy fake", "protocol": "fake_authenticated_v1",
            "endpoint": "fake://receiver", "credential_configured": true,
            "revision": 1, "created_at_unix_ms": 1, "updated_at_unix_ms": 1,
            "updated_by": "operator"
        });
        let legacy_analysis = serde_json::json!({
            "proof_id": "legacy-analysis", "channel_id": channel_id, "sink_id": sink_id,
            "target_id": "target-1", "candidate_version": "0.1.0", "source_commit": "a".repeat(40),
            "configuration_hash": "b".repeat(64), "analyzer_id": "legacy-analyzer",
            "analyzer_version": "1", "analyzer_sha256": "c".repeat(64), "outcome": "pass",
            "findings": {"packet_size_bytes":188,"packet_count":1,"capture_duration_ms":1,
                "bitrate_bps":1,"pat_program_count":1,"pmt_stream_count":1,"continuity_errors":0,
                "pcr_errors":0,"analyzer_output_sha256":"d".repeat(64)},
            "captured_at_unix_ms": 1, "recorded_at_unix_ms": 1, "recorded_by": "worker", "detail": "legacy"
        });
        // This is the exact parent (66c85ae) transport schema: analysis
        // receipts do not have target_revision and target JSON has endpoint.
        store.execute(&format!(
            "DROP TABLE target_device_reachability_receipts;\
             DROP TABLE transport_analysis_receipts;\
             DROP TABLE transport_target_configs;\
             CREATE TABLE transport_target_configs (\
               channel_id TEXT NOT NULL,sink_id TEXT NOT NULL,target_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),record_json TEXT NOT NULL,\
               PRIMARY KEY(channel_id,sink_id,revision));\
             CREATE TABLE transport_analysis_receipts (\
               proof_id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,sink_id TEXT NOT NULL,target_id TEXT NOT NULL,\
               candidate_version TEXT NOT NULL,source_commit TEXT NOT NULL,configuration_hash TEXT NOT NULL,\
               outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail')),\
               captured_at_unix_ms INTEGER NOT NULL CHECK (captured_at_unix_ms >= 0),\
               recorded_at_unix_ms INTEGER NOT NULL CHECK (recorded_at_unix_ms >= captured_at_unix_ms),record_json TEXT NOT NULL);\
             CREATE INDEX transport_analysis_current ON transport_analysis_receipts(channel_id,sink_id,target_id,candidate_version,source_commit,configuration_hash,recorded_at_unix_ms DESC,proof_id DESC);\
             CREATE TABLE target_device_reachability_receipts (\
               proof_id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,sink_id TEXT NOT NULL,target_id TEXT NOT NULL,\
               target_revision INTEGER NOT NULL CHECK (target_revision >= 1),candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,configuration_hash TEXT NOT NULL,outcome TEXT NOT NULL CHECK (outcome IN ('pass','fail')),\
               observed_at_unix_ms INTEGER NOT NULL CHECK (observed_at_unix_ms >= 0),\
               recorded_at_unix_ms INTEGER NOT NULL CHECK (recorded_at_unix_ms >= observed_at_unix_ms),record_json TEXT NOT NULL);\
             CREATE INDEX target_device_reachability_current ON target_device_reachability_receipts(channel_id,sink_id,target_id,target_revision,candidate_version,source_commit,configuration_hash,recorded_at_unix_ms DESC,proof_id DESC);\
             CREATE INDEX transport_analysis_history ON transport_analysis_receipts(channel_id,recorded_at_unix_ms DESC,proof_id DESC);\
             CREATE INDEX target_device_reachability_history ON target_device_reachability_receipts(channel_id,recorded_at_unix_ms DESC,proof_id DESC);\
             CREATE TRIGGER transport_target_configs_no_delete BEFORE DELETE ON transport_target_configs BEGIN SELECT RAISE(ABORT, 'transport target configurations are not deletable'); END;\
             CREATE TRIGGER transport_target_configs_no_update BEFORE UPDATE ON transport_target_configs BEGIN SELECT RAISE(ABORT, 'transport target configurations are append-only'); END;\
             CREATE TRIGGER transport_analysis_no_update BEFORE UPDATE ON transport_analysis_receipts BEGIN SELECT RAISE(ABORT, 'transport analysis receipts are append-only'); END;\
             CREATE TRIGGER transport_analysis_no_delete BEFORE DELETE ON transport_analysis_receipts BEGIN SELECT RAISE(ABORT, 'transport analysis receipts are append-only'); END;\
             CREATE TRIGGER target_device_reachability_no_update BEFORE UPDATE ON target_device_reachability_receipts BEGIN SELECT RAISE(ABORT, 'target device reachability receipts are append-only'); END;\
             CREATE TRIGGER target_device_reachability_no_delete BEFORE DELETE ON target_device_reachability_receipts BEGIN SELECT RAISE(ABORT, 'target device reachability receipts are append-only'); END;\
             INSERT INTO transport_target_configs VALUES('{channel_id}','{sink_id}','target-1',1,'{legacy_target}');\
             INSERT INTO transport_target_configs VALUES('{channel_id}','fake-sink','fake-target',1,'{legacy_fake_target}');\
             INSERT INTO transport_analysis_receipts VALUES('legacy-analysis','{channel_id}','{sink_id}','target-1','0.1.0','{}','{}','pass',1,1,'{legacy_analysis}');",
            "a".repeat(40),
            "b".repeat(64),
        )).unwrap();
        assert!(
            store
                .sqlite_table_has_column("transport_analysis_receipts", "proof_id")
                .unwrap()
        );
        drop(store);

        let migrated = StationStore::open(&database).expect("legacy data must not prevent startup");
        assert!(
            migrated
                .sqlite_table_has_column("transport_analysis_receipts", "target_revision")
                .unwrap()
        );
        let target = migrated
            .read_transport_target_config(channel_id, sink_id)
            .unwrap()
            .unwrap();
        assert_eq!(target.host, "receiver.example.test");
        assert_eq!(target.port, 8443);
        assert!(
            migrated
                .read_transport_target_config(channel_id, "fake-sink")
                .is_err()
        );
        // A former analysis receipt is auditable but cannot be upgraded into a
        // target-revision-bound authority fact without information it never had.
        assert!(
            migrated
                .read_transport_proof_history(channel_id, Some(sink_id), 10)
                .is_err()
        );
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn transport_proofs_require_both_current_receipts_and_target_revision() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-transport-proof-{nonce}.db"));
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        let store = StationStore::open(&database).unwrap();
        store
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Transport proof".into(),
                output_profile: OutputProfileId::Proof360p30,
                output_sinks: vec![OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Proof sink",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: true,
            })
            .unwrap();
        let target = TargetDeviceConfig {
            target_id: "target-1".into(),
            label: "Lab target".into(),
            protocol: TargetProtocol::HttpsApi,
            host: "target-1.example.test".into(),
            port: 443,
            credential_configured: true,
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
            updated_by: "operator".into(),
        };
        store
            .write_transport_target_config(channel_id, sink_id, None, &target)
            .unwrap();
        let hash = store
            .current_channel_output_configuration_hash(channel_id)
            .unwrap();
        let analysis = TransportAnalysisReceipt {
            proof_id: "analysis-1".into(),
            channel_id: channel_id.into(),
            sink_id: sink_id.into(),
            target_id: "target-1".into(),
            target_revision: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            configuration_hash: hash.clone(),
            analyzer_id: "fake-analyzer".into(),
            analyzer_version: "1".into(),
            analyzer_sha256: "d".repeat(64),
            outcome: ProofOutcome::Pass,
            findings: TransportFindings {
                packet_size_bytes: 188,
                packet_count: 10,
                capture_duration_ms: 1_000,
                bitrate_bps: 1_000_000,
                pat_program_count: 1,
                pmt_stream_count: 2,
                continuity_errors: 0,
                pcr_errors: 0,
                analyzer_output_sha256: "c".repeat(64),
            },
            captured_at_unix_ms: 2,
            recorded_at_unix_ms: 2,
            recorded_by: "worker".into(),
            detail: "analysis passed".into(),
        };
        store
            .append_transport_analysis_receipt(&analysis, 2)
            .unwrap();
        assert_eq!(
            store
                .read_current_cable_transport_validation(
                    channel_id,
                    sink_id,
                    "0.1.0",
                    &"a".repeat(40),
                    2
                )
                .unwrap()
                .unwrap()
                .outcome,
            None
        );
        let reachability = TargetDeviceReachabilityReceipt {
            proof_id: "reach-1".into(),
            channel_id: channel_id.into(),
            sink_id: sink_id.into(),
            target_id: "target-1".into(),
            target_revision: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            configuration_hash: hash,
            adapter_id: "fake-adapter".into(),
            adapter_version: "1".into(),
            auth_method: "challenge".into(),
            outcome: ProofOutcome::Pass,
            response_identity_sha256: "e".repeat(64),
            latency_ms: 4,
            observed_at_unix_ms: 2,
            recorded_at_unix_ms: 2,
            recorded_by: "worker".into(),
            detail: "target authenticated".into(),
        };
        store
            .append_target_device_reachability_receipt(&reachability, 2)
            .unwrap();
        assert_eq!(
            store
                .read_current_cable_transport_validation(
                    channel_id,
                    sink_id,
                    "0.1.0",
                    &"a".repeat(40),
                    2
                )
                .unwrap()
                .unwrap()
                .outcome,
            Some(ProofOutcome::Pass)
        );
        let snapshot = store
            .read_current_cable_transport_validations("0.1.0", &"a".repeat(40), 2)
            .unwrap();
        assert_eq!(snapshot.len(), 1);
        assert_eq!(snapshot[0].channel_id, channel_id);
        assert_eq!(snapshot[0].sink_id, sink_id);
        assert_eq!(
            snapshot[0].validation.as_ref().unwrap().outcome,
            Some(ProofOutcome::Pass)
        );
        // The later append wins even when the caller chooses a lexically lower
        // proof ID at the same millisecond.
        let mut same_millisecond_failure = analysis.clone();
        same_millisecond_failure.proof_id = "000-analysis-failure".into();
        same_millisecond_failure.outcome = ProofOutcome::Fail;
        store
            .append_transport_analysis_receipt(&same_millisecond_failure, 2)
            .unwrap();
        let blocked = store
            .read_current_cable_transport_validation(
                channel_id,
                sink_id,
                "0.1.0",
                &"a".repeat(40),
                2,
            )
            .unwrap()
            .unwrap();
        assert_eq!(blocked.outcome, None);
        assert_eq!(
            blocked.transport_analysis.unwrap().proof_id,
            "000-analysis-failure"
        );
        // A latest future-dated record also blocks; selection must occur before
        // freshness evaluation so an older pass cannot make the roll-up green.
        let mut future_failure = same_millisecond_failure;
        future_failure.proof_id = "future-analysis-failure".into();
        future_failure.captured_at_unix_ms = 3;
        future_failure.recorded_at_unix_ms = 3;
        store
            .append_transport_analysis_receipt(&future_failure, 3)
            .unwrap();
        let blocked = store
            .read_current_cable_transport_validation(
                channel_id,
                sink_id,
                "0.1.0",
                &"a".repeat(40),
                2,
            )
            .unwrap()
            .unwrap();
        assert_eq!(blocked.outcome, None);
        assert_eq!(
            blocked.transport_analysis.unwrap().proof_id,
            "future-analysis-failure"
        );
        let mut changed = target;
        changed.host = "target-2.example.test".into();
        changed.updated_at_unix_ms = 3;
        store
            .write_transport_target_config(channel_id, sink_id, Some(1), &changed)
            .unwrap();
        let mut current_reachability = reachability;
        current_reachability.proof_id = "reach-2".into();
        current_reachability.target_revision = 2;
        current_reachability.recorded_at_unix_ms = 3;
        current_reachability.observed_at_unix_ms = 3;
        store
            .append_target_device_reachability_receipt(&current_reachability, 3)
            .unwrap();
        assert_eq!(
            store
                .read_current_cable_transport_validation(
                    channel_id,
                    sink_id,
                    "0.1.0",
                    &"a".repeat(40),
                    3
                )
                .unwrap()
                .unwrap()
                .outcome,
            None
        );
        assert_eq!(
            store
                .read_current_cable_transport_validation(
                    channel_id,
                    sink_id,
                    "0.1.0",
                    &"a".repeat(40),
                    station_transport_proof::TRANSPORT_PROOF_FRESHNESS_MS + 4,
                )
                .unwrap()
                .unwrap()
                .outcome,
            None
        );
        let mut expired_analysis = analysis;
        expired_analysis.proof_id = "analysis-expired".into();
        assert!(
            store
                .append_transport_analysis_receipt(
                    &expired_analysis,
                    station_transport_proof::TRANSPORT_PROOF_FRESHNESS_MS + 4,
                )
                .is_err()
        );
        assert!(
            store
                .execute("UPDATE transport_analysis_receipts SET record_json='tampered'")
                .is_err()
        );
        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }
}
