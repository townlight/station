use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;
use std::fs;
use std::io::Read;
use std::num::NonZeroU16;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
    mpsc::{self, Receiver},
};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use station_captions::{
    AssetCaptionTrack, CaptionEgressProof, CaptionEgressProofJobRecord, CaptionEgressProofJobState,
    CaptionProofCaptureFacts, CaptionProofOutcome,
};
use station_cg::{CgBulletinContent, CgResolvedContent, CgResolvedSnapshot};
use station_domain::{
    AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity, CaptionCueMissEvidence,
    CaptionCueMissReason, ChannelRuntimeObservation, ChannelRuntimeState,
};
use station_live::{LiveEndpoint, TakeoverSession, TakeoverState};
use station_media_assets::{
    DecodedCaptionEvidence, decode_output_captions, measure_output_loudness,
};
use station_media_journal::inspect_journal;
use station_media_protocol::{
    CaptionCue as WorkerCaptionCue, CgImageFormat, CgOverlay, CgOverlayContent, CgRect, CgSnapshot,
    ChannelCommand, LiveArmToken, LiveInputConfiguration, LiveReturnTarget, LoudnessTarget,
    MAX_CAPTION_START_AFTER_MS, MAX_CG_SLOTS, MAX_CG_TEXT_BYTES, MAX_OUTPUT_PROOF_DURATION_MS,
    MAX_OUTPUT_PROOF_SAMPLE_DURATION_MS, OUTPUT_PROOF_CAPTURE_HEADROOM_MS, SrtPassphrase,
    WorkerCaptionMode, WorkerCaptionPolicy, WorkerEvent, WorkerFallbackSlate,
    WorkerHeadendProfileId, WorkerHeadendTransportContract, WorkerOutputConfiguration,
};
use station_readiness::derive_loudness_outcome;
use station_readiness::{
    CandidateIdentity, LoudnessProofOutcome, ReceiverAcceptance, SinkLoudnessProof,
};
use station_runtime::{SupervisorError, WorkerSupervisor};
use station_schedule::{
    ChannelConfiguration, DispatchJob, DispatchStatus, HeadendProfileId, HeadendSinkContract,
    OutputProfileId, OutputSinkKind, ScheduleWindow,
};
use station_storage::{
    CgSnapshotReadError, CgWriteError, ChannelCaptionMode, OutputLoudnessCaptureFacts,
    OutputLoudnessProofJobRecord, OutputLoudnessProofJobState, ScheduledTakeAcknowledgement,
    ScheduledTakeAcknowledgementBinding, ScheduledTakeApplyOutcome, StationStore,
};

static ALERT_SEQUENCE: AtomicU64 = AtomicU64::new(1);
const HEARTBEAT_INTERVAL_MS: i64 = 5_000;
const MINIMUM_WORKER_BOOTSTRAP_TIMEOUT: Duration = Duration::from_secs(30);
const MEDIA_LOAD_TIMEOUT: Duration = Duration::from_secs(25);
const MEDIA_TEARDOWN_TIMEOUT: Duration = Duration::from_secs(30);
const CG_EXPIRER_OPERATOR_ID: &str = "stationd-cg-expirer";
const CG_SYNC_INTERVAL_MS: i64 = 1_000;
const MAX_CAPTION_CUES_PER_TICK: usize = 64;

#[derive(Debug, PartialEq, Eq)]
struct CaptionDispatchPlan {
    due: Vec<WorkerCaptionCue>,
    missed_cue_ids: Vec<String>,
}

fn due_caption_cues(
    track: &AssetCaptionTrack,
    media_offset_ms: u64,
    accepted_cue_ids: &HashSet<String>,
    already_missed_cue_ids: &HashSet<String>,
) -> CaptionDispatchPlan {
    let mut due = Vec::new();
    let mut newly_missed_cue_ids = Vec::new();
    for cue in track.cues.iter().filter(|cue| {
        !accepted_cue_ids.contains(&cue.cue_id) && !already_missed_cue_ids.contains(&cue.cue_id)
    }) {
        let Some(end_ms) = cue.start_ms.checked_add(cue.duration_ms) else {
            newly_missed_cue_ids.push(cue.cue_id.clone());
            continue;
        };
        if end_ms <= media_offset_ms {
            newly_missed_cue_ids.push(cue.cue_id.clone());
            continue;
        }
        let (start_after_ms, duration_ms) = if cue.start_ms <= media_offset_ms {
            (0, end_ms - media_offset_ms)
        } else {
            let start_after_ms = cue.start_ms - media_offset_ms;
            if start_after_ms > MAX_CAPTION_START_AFTER_MS {
                // The worker contract cannot represent this cue yet. Keep it
                // under controller authority for a later tick.
                continue;
            }
            (start_after_ms, cue.duration_ms)
        };
        // Bounded future cues are deliberately prequeued so ordinary control
        // tick cadence cannot late-admit short valid cues. Every source-role
        // transition synchronously clears worker caption state first.
        if due.len() == MAX_CAPTION_CUES_PER_TICK {
            continue;
        }
        due.push(WorkerCaptionCue {
            cue_id: cue.cue_id.clone(),
            start_after_ms,
            duration_ms,
            text: cue.text.clone(),
        });
        if due.len() == MAX_CAPTION_CUES_PER_TICK {
            break;
        }
    }
    CaptionDispatchPlan {
        due,
        missed_cue_ids: newly_missed_cue_ids,
    }
}

fn observe_caption_cue_misses(
    store: &StationStore,
    channel_id: &str,
    report_id: &str,
    track: &AssetCaptionTrack,
    media_offset_ms: u64,
    reason: CaptionCueMissReason,
    cue_ids: &[String],
) -> Result<bool, OrchestrationError> {
    if cue_ids.is_empty() {
        return Err(OrchestrationError::CaptionAuthority(
            "caption miss evidence is empty".into(),
        ));
    }
    let observed_at_unix_ms = system_time_millis()?;
    store
        .observe_caption_cue_misses_and_route(
            &cue_ids
                .iter()
                .map(|cue_id| CaptionCueMissEvidence {
                    channel_id: channel_id.into(),
                    report_id: report_id.into(),
                    track_id: track.track_id.clone(),
                    track_revision: track.revision,
                    cue_id: cue_id.clone(),
                    reason,
                    media_offset_ms,
                    observed_at_unix_ms,
                })
                .collect::<Vec<_>>(),
        )
        .map_err(OrchestrationError::Storage)
}

fn resolve_caption_cue_miss_after_admission(
    store: &StationStore,
    channel_id: &str,
    accepted_cue_count: usize,
    batch_had_miss: bool,
) -> Result<(), OrchestrationError> {
    // A previous incident is not healed by time passing, an operator
    // acknowledgment, an empty tick, or a mixed batch. The recovery boundary
    // is one later complete dispatch batch which admitted at least one cue and
    // had no expired or non-acknowledged cue.
    if accepted_cue_count == 0 || batch_had_miss {
        return Ok(());
    }
    store
        .resolve_alert(
            AlertConditionKind::CaptionCueMissed,
            channel_id,
            system_time_millis()?,
        )
        .map_err(OrchestrationError::Storage)?;
    Ok(())
}
pub fn managed_cg_image_root(database_path: &Path) -> PathBuf {
    let parent = database_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf();
    let parent = if parent.is_absolute() {
        parent
    } else {
        std::env::current_dir().map_or(parent.clone(), |current| current.join(parent))
    };
    parent.join(station_cg::CG_IMAGE_SUBDIRECTORY)
}

pub fn managed_output_proof_root(database_path: &Path) -> PathBuf {
    let parent = database_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf();
    let parent = if parent.is_absolute() {
        parent
    } else {
        std::env::current_dir().map_or(parent.clone(), |current| current.join(parent))
    };
    parent.join("output-proofs")
}

pub fn managed_caption_proof_root(database_path: &Path) -> PathBuf {
    let parent = database_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf();
    let parent = if parent.is_absolute() {
        parent
    } else {
        std::env::current_dir().map_or(parent.clone(), |current| current.join(parent))
    };
    parent.join("caption-proofs")
}

fn expire_due_bulletins(
    store: &StationStore,
    channel_id: &str,
    now_unix_ms: i64,
) -> Result<(), OrchestrationError> {
    match store.expire_due_cg_bulletins(channel_id, now_unix_ms, CG_EXPIRER_OPERATOR_ID) {
        Ok(_) | Err(CgWriteError::NotFound) | Err(CgWriteError::Conflict(_)) => Ok(()),
        Err(CgWriteError::Invalid(message)) => Err(OrchestrationError::Storage(format!(
            "CG bulletin expiration rejected: {message}"
        ))),
        Err(CgWriteError::Storage(message)) => Err(OrchestrationError::Storage(format!(
            "CG bulletin expiration storage failure: {message}"
        ))),
    }
}

fn cg_read_requires_clear(error: &CgSnapshotReadError) -> bool {
    matches!(
        error,
        CgSnapshotReadError::BoardNotFound | CgSnapshotReadError::BoardInactive
    )
}

fn ambiguous_worker_command_failure(
    operation: impl Into<String>,
    cause: impl Into<String>,
    retire: impl FnOnce() -> Result<(), SupervisorError>,
) -> OrchestrationError {
    let termination_failure = retire().err().map(|error| format!("{error:?}"));
    OrchestrationError::WorkerSessionAmbiguous {
        operation: operation.into(),
        cause: cause.into(),
        termination_failure,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DispatchTransition {
    Loaded { report_id: String },
    OnAir { report_id: String },
    Completed { report_id: String },
    Missed { report_id: String },
    LiveArmed { session_id: String },
    LiveOnAir { session_id: String },
    LiveCompleted { session_id: String },
}

#[derive(Debug)]
pub enum OrchestrationError {
    Storage(String),
    InvalidDestination(String),
    Worker(SupervisorError),
    UnexpectedWorkerEvent,
    ConcurrentTransition,
    RequiredOutputStalled {
        sink_id: String,
        label: String,
        detail: String,
    },
    OutputGraphUnhealthy {
        detail: String,
    },
    LiveInputStalled {
        detail: String,
    },
    LiveControl(String),
    CaptionAuthority(String),
    /// A worker command may have taken effect before its reply was lost or
    /// malformed. The session is retired before this error is returned, so a
    /// later command can never reuse an ambiguous protected-pipe sequence.
    WorkerSessionAmbiguous {
        operation: String,
        cause: String,
        termination_failure: Option<String>,
    },
    Clock(String),
    InvalidCgSnapshot(String),
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CgSnapshotState {
    last_valid: Option<(CgSnapshot, [u8; 32])>,
    last_applied: Option<(u64, [u8; 32])>,
    clear_required: bool,
}

impl CgSnapshotState {
    pub fn accept(
        &mut self,
        snapshot: &CgResolvedSnapshot,
    ) -> Result<CgSnapshot, OrchestrationError> {
        let protocol = resolved_snapshot_to_protocol(snapshot)?;
        let hash = snapshot_hash(&protocol)?;
        self.clear_required = false;
        self.last_valid = Some((protocol.clone(), hash));
        Ok(protocol)
    }

    /// Forget the authoritative snapshot and request one clear if this state was
    /// previously acknowledged by the worker. A failed clear keeps the request
    /// pending so the next CG sync retries it; an already-cleared state is a no-op.
    pub fn invalidate_authority(&mut self) {
        self.clear_required |= self.last_applied.is_some();
        self.last_valid = None;
        self.last_applied = None;
    }

    pub fn clear_required(&self) -> bool {
        self.clear_required
    }

    pub fn mark_cleared(&mut self) {
        self.clear_required = false;
    }

    pub fn mark_applied(&mut self, board_id: &str, board_revision: u64) {
        if self.last_valid.as_ref().is_some_and(|(snapshot, _)| {
            snapshot.board_id == board_id && snapshot.board_revision == board_revision
        }) {
            let hash = self.last_valid.as_ref().map(|(_, hash)| *hash).unwrap();
            self.last_applied = Some((board_revision, hash));
        }
    }

    pub fn worker_ready(&mut self) {
        self.last_applied = None;
        if self.last_valid.is_none() {
            self.clear_required = false;
        }
    }

    pub fn resend_after_ready(&self) -> Option<CgSnapshot> {
        let (snapshot, hash) = self.last_valid.as_ref()?;
        let applied = self.last_applied.as_ref();
        (applied != Some(&(snapshot.board_revision, *hash))).then_some(snapshot.clone())
    }

    pub fn last_valid(&self) -> Option<&CgSnapshot> {
        self.last_valid.as_ref().map(|(snapshot, _)| snapshot)
    }

    pub fn last_applied(&self) -> Option<(u64, [u8; 32])> {
        self.last_applied
    }
}

pub fn resolved_snapshot_to_protocol(
    snapshot: &CgResolvedSnapshot,
) -> Result<CgSnapshot, OrchestrationError> {
    snapshot
        .validate()
        .map_err(|error| OrchestrationError::InvalidCgSnapshot(error.into()))?;
    let mut zones = snapshot.zones.iter().collect::<Vec<_>>();
    zones.sort_by(|left, right| {
        left.template_zone_key
            .cmp(&right.template_zone_key)
            .then_with(|| left.zone_id.cmp(&right.zone_id))
    });
    let mut overlays = Vec::new();
    for zone in zones {
        let contents = match &zone.content {
            CgResolvedContent::ManualText { text } => vec![Some(normalize_cg_text(text))],
            CgResolvedContent::Clock { format } => {
                let format = normalize_cg_text(format);
                validate_cg_text(&format)?;
                overlays.push(CgOverlay {
                    slot: checked_slot(overlays.len())?,
                    rect: protocol_rect(zone),
                    z_index: protocol_z_index(zone)?,
                    content: CgOverlayContent::Clock { format },
                });
                continue;
            }
            CgResolvedContent::Bulletin { content, .. } => match content {
                CgBulletinContent::Text { title, body } => {
                    vec![Some(normalize_cg_text(&format!("{title}\n{body}")))]
                }
                _ => vec![None],
            },
            CgResolvedContent::FeedItems { items, .. } => {
                let mut items = items.clone();
                items.sort_by(|left, right| {
                    left.starts_at_unix_ms
                        .cmp(&right.starts_at_unix_ms)
                        .then_with(|| left.item_id.cmp(&right.item_id))
                });
                items
                    .into_iter()
                    .next()
                    .map(|item| {
                        let title = item.title;
                        Some(normalize_cg_text(&item.summary.map_or_else(
                            || title.clone(),
                            |summary| format!("{title}\n{summary}"),
                        )))
                    })
                    .into_iter()
                    .collect()
            }
            CgResolvedContent::Image { asset } => {
                let format = match asset.mime_type.as_str() {
                    "image/png" => CgImageFormat::Png,
                    "image/jpeg" => CgImageFormat::Jpeg,
                    _ => {
                        return Err(OrchestrationError::InvalidCgSnapshot(
                            "CG image format is not supported by the worker protocol".into(),
                        ));
                    }
                };
                if asset.readiness != station_cg::CgImageReadiness::Ready {
                    return Err(OrchestrationError::InvalidCgSnapshot(
                        "CG image snapshot references a non-ready asset".into(),
                    ));
                }
                overlays.push(CgOverlay {
                    slot: checked_slot(overlays.len())?,
                    rect: protocol_rect(zone),
                    z_index: protocol_z_index(zone)?,
                    content: CgOverlayContent::Image {
                        asset_id: asset.asset_id.clone(),
                        format,
                    },
                });
                continue;
            }
            CgResolvedContent::Placeholder { .. } => vec![None],
        };
        for text in contents.into_iter().flatten() {
            validate_cg_text(&text)?;
            overlays.push(CgOverlay {
                slot: checked_slot(overlays.len())?,
                rect: protocol_rect(zone),
                z_index: protocol_z_index(zone)?,
                content: CgOverlayContent::Text { text },
            });
        }
    }
    Ok(CgSnapshot {
        board_id: snapshot.board_id.clone(),
        board_revision: snapshot.board_revision,
        overlays,
    })
}

fn checked_slot(index: usize) -> Result<u8, OrchestrationError> {
    (index < MAX_CG_SLOTS)
        .then_some(index as u8)
        .ok_or_else(|| OrchestrationError::InvalidCgSnapshot("CG snapshot exceeds 8 slots".into()))
}

fn protocol_rect(zone: &station_cg::CgResolvedZone) -> CgRect {
    CgRect {
        x: zone.bounds.x,
        y: zone.bounds.y,
        width: zone.bounds.width,
        height: zone.bounds.height,
    }
}

fn protocol_z_index(zone: &station_cg::CgResolvedZone) -> Result<i16, OrchestrationError> {
    i16::try_from(zone.z_index).map_err(|_| {
        OrchestrationError::InvalidCgSnapshot("CG z-index exceeds protocol range".into())
    })
}

fn validate_cg_text(text: &str) -> Result<(), OrchestrationError> {
    if text.is_empty()
        || text.len() > MAX_CG_TEXT_BYTES
        || text
            .chars()
            .any(|character| character.is_control() && character != '\n')
    {
        Err(OrchestrationError::InvalidCgSnapshot(
            "CG overlay text violates protocol limits".into(),
        ))
    } else {
        Ok(())
    }
}

fn normalize_cg_text(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

fn snapshot_hash(snapshot: &CgSnapshot) -> Result<[u8; 32], OrchestrationError> {
    let bytes = serde_json::to_vec(snapshot).map_err(|error| {
        OrchestrationError::InvalidCgSnapshot(format!("CG snapshot hash failed: {error}"))
    })?;
    Ok(Sha256::digest(bytes).into())
}

struct OutputExpectation {
    sink_id: String,
    label: String,
    required: bool,
}

const OUTPUT_PROOF_LEASE_MS: i64 = 300_000;
const OUTPUT_PROOF_POLL_INTERVAL_MS: i64 = 250;
const OUTPUT_PROOF_LEASE_RENEW_MARGIN_MS: i64 = 30_000;
const OUTPUT_PROOF_TOLERANCE_MILLI: i32 = 2_000;
const CAPTION_PROOF_START_LATE_TOLERANCE_MS: i64 = 500;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CaptionCaptureTiming {
    Waiting,
    Due,
    Missed,
}

fn caption_capture_timing(
    schedule_starts_at_unix_ms: i64,
    capture_asset_offset_ms: u64,
    now_unix_ms: i64,
) -> CaptionCaptureTiming {
    let Ok(capture_asset_offset_ms) = i64::try_from(capture_asset_offset_ms) else {
        return CaptionCaptureTiming::Missed;
    };
    let Some(capture_starts_at_unix_ms) =
        schedule_starts_at_unix_ms.checked_add(capture_asset_offset_ms)
    else {
        return CaptionCaptureTiming::Missed;
    };
    if now_unix_ms < capture_starts_at_unix_ms {
        CaptionCaptureTiming::Waiting
    } else if now_unix_ms
        <= capture_starts_at_unix_ms.saturating_add(CAPTION_PROOF_START_LATE_TOLERANCE_MS)
    {
        CaptionCaptureTiming::Due
    } else {
        CaptionCaptureTiming::Missed
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProofOutputBinding {
    sink_id: String,
    target_lufs_milli: i32,
}

#[derive(Debug)]
struct OutputProofAnalysis {
    measured_lufs_milli: i32,
    analyzed_duration_ms: u64,
}

#[derive(Debug)]
struct OutputProofAnalyzing {
    job: OutputLoudnessProofJobRecord,
    artifact_relative_path: String,
    worker_instance_id: String,
    graph_revision: u64,
    worker_capture_duration_ms: u64,
    analysis: Receiver<Result<OutputProofAnalysis, String>>,
}

#[derive(Debug, Default)]
enum OutputProofState {
    #[default]
    Idle,
    Capturing {
        job: OutputLoudnessProofJobRecord,
        output_index: u16,
        capture_duration_ms: u64,
        artifact_relative_path: String,
        worker_instance_id: String,
        graph_revision: u64,
        next_poll_at_unix_ms: i64,
    },
    Analyzing(OutputProofAnalyzing),
}

#[derive(Debug)]
struct CaptionProofWaiting {
    job: CaptionEgressProofJobRecord,
    schedule_starts_at_unix_ms: i64,
}

#[derive(Debug)]
struct CaptionProofAnalyzing {
    job: CaptionEgressProofJobRecord,
    artifact_relative_path: String,
    worker_instance_id: String,
    graph_revision: u64,
    worker_capture_duration_ms: u64,
    captured_at_unix_ms: i64,
    analysis: Receiver<Result<DecodedCaptionEvidence, String>>,
}

#[derive(Debug, Default)]
enum CaptionProofState {
    #[default]
    Idle,
    Waiting(CaptionProofWaiting),
    Capturing {
        job: CaptionEgressProofJobRecord,
        output_index: u16,
        target: LoudnessTarget,
        artifact_relative_path: String,
        worker_instance_id: String,
        graph_revision: u64,
        captured_at_unix_ms: i64,
        next_poll_at_unix_ms: i64,
    },
    Analyzing(CaptionProofAnalyzing),
}

fn proof_artifact_path(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let relative_path = Path::new(relative);
    if relative.is_empty()
        || relative_path.is_absolute()
        || relative_path
            .components()
            .any(|component| !matches!(component, std::path::Component::Normal(_)))
    {
        return Err("worker returned an unsafe output proof artifact path".into());
    }
    let root =
        fs::canonicalize(root).map_err(|error| format!("canonicalize proof root: {error}"))?;
    let path = root.join(relative_path);
    let parent = path
        .parent()
        .ok_or_else(|| "output proof artifact has no parent".to_string())?;
    let parent = fs::canonicalize(parent)
        .map_err(|error| format!("canonicalize output proof artifact parent: {error}"))?;
    if parent != root {
        return Err("output proof artifact escaped its managed root".into());
    }
    Ok(path)
}

fn hash_capture(path: &Path, max_bytes: u64) -> Result<(u64, String), String> {
    let link_metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("inspect output proof artifact: {error}"))?;
    if !link_metadata.file_type().is_file() {
        return Err("output proof artifact is not a regular file".into());
    }
    if link_metadata.len() == 0 || link_metadata.len() > max_bytes {
        return Err("output proof artifact exceeds its durable size bound".into());
    }
    let mut file =
        fs::File::open(path).map_err(|error| format!("open output proof artifact: {error}"))?;
    let mut hasher = Sha256::new();
    let mut bytes = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("read output proof artifact: {error}"))?;
        if count == 0 {
            break;
        }
        bytes = bytes
            .checked_add(count as u64)
            .ok_or_else(|| "output proof artifact size overflowed".to_string())?;
        if bytes > max_bytes {
            return Err(
                "output proof artifact exceeded its durable size bound while hashing".into(),
            );
        }
        hasher.update(&buffer[..count]);
    }
    Ok((bytes, format!("{:x}", hasher.finalize())))
}

fn proof_bindings_match(expected: &[ProofOutputBinding], actual: &[ProofOutputBinding]) -> bool {
    expected == actual
}

fn capture_duration_for_sample(sample_duration_ms: u64) -> Option<u64> {
    (3_000..=MAX_OUTPUT_PROOF_SAMPLE_DURATION_MS)
        .contains(&sample_duration_ms)
        .then(|| sample_duration_ms + OUTPUT_PROOF_CAPTURE_HEADROOM_MS)
}

fn ready_duration_is_bounded(actual_ms: u64, requested_capture_ms: u64) -> bool {
    actual_ms >= requested_capture_ms && actual_ms <= MAX_OUTPUT_PROOF_DURATION_MS
}

fn lease_renewal_due(now_unix_ms: i64, expires_at_unix_ms: Option<i64>) -> bool {
    expires_at_unix_ms.is_none_or(|expires| {
        now_unix_ms.saturating_add(OUTPUT_PROOF_LEASE_RENEW_MARGIN_MS) >= expires
    })
}

fn remove_managed_file(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("inspect managed output proof artifact: {error}"))?;
    if !metadata.file_type().is_file() {
        return Err("managed output proof artifact is not a regular file".into());
    }
    fs::remove_file(path).map_err(|error| format!("remove managed output proof artifact: {error}"))
}

fn cleanup_orphan_output_proof_artifacts(root: &Path, job_id: &str) -> Result<(), String> {
    match fs::symlink_metadata(root) {
        Ok(metadata) if metadata.file_type().is_dir() => {}
        Ok(_) => return Err("managed output proof root is not a directory".into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("inspect managed output proof root: {error}")),
    }
    for suffix in [".ts", ".ts.part"] {
        let relative = format!("{job_id}{suffix}");
        let path = proof_artifact_path(root, &relative)?;
        match fs::symlink_metadata(&path) {
            Ok(_) => remove_managed_file(&path)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(format!("inspect orphan output proof artifact: {error}")),
        }
    }
    Ok(())
}

fn cleanup_orphan_caption_proof_artifacts(
    database_path: &Path,
    job_id: &str,
) -> Result<(), String> {
    cleanup_orphan_output_proof_artifacts(&managed_output_proof_root(database_path), job_id)?;
    cleanup_orphan_output_proof_artifacts(&managed_caption_proof_root(database_path), job_id)
}

fn promote_caption_proof_artifact(
    database_path: &Path,
    job_id: &str,
    worker_relative_path: &str,
) -> Result<(PathBuf, String), String> {
    if worker_relative_path != format!("{job_id}.ts") {
        return Err("caption proof worker artifact path did not match the durable job".into());
    }
    let worker_root = managed_output_proof_root(database_path);
    let source = proof_artifact_path(&worker_root, worker_relative_path)?;
    let caption_root = managed_caption_proof_root(database_path);
    match fs::create_dir(&caption_root) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            let metadata = fs::symlink_metadata(&caption_root)
                .map_err(|inspect| format!("inspect managed caption proof root: {inspect}"))?;
            if !metadata.file_type().is_dir() {
                return Err("managed caption proof root is not a directory".into());
            }
        }
        Err(error) => return Err(format!("create managed caption proof root: {error}")),
    }
    let destination = proof_artifact_path(&caption_root, worker_relative_path)?;
    match fs::symlink_metadata(&destination) {
        Ok(_) => return Err("caption proof destination artifact already exists".into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(format!("inspect caption proof destination: {error}")),
    }
    fs::rename(&source, &destination)
        .map_err(|error| format!("atomically promote caption proof artifact: {error}"))?;
    Ok((
        destination,
        format!("caption-proofs/{worker_relative_path}"),
    ))
}

fn fail_output_proofs_without_controller(
    store: &StationStore,
    database_path: &Path,
    channel_id: &str,
    now_unix_ms: i64,
    detail: &str,
) -> Result<(), OrchestrationError> {
    let jobs = store
        .list_unfinished_output_loudness_jobs_for_channel(channel_id)
        .map_err(OrchestrationError::Storage)?;
    let proof_root = managed_output_proof_root(database_path);
    for job in jobs {
        let failure_detail = match cleanup_orphan_output_proof_artifacts(&proof_root, &job.job_id) {
            Ok(()) => detail.to_string(),
            Err(error) => format!("{detail}; managed artifact cleanup also failed: {error}"),
        };
        store
            .fail_abandoned_output_loudness_job(
                &job.job_id,
                job.state,
                now_unix_ms,
                &failure_detail,
            )
            .map_err(OrchestrationError::Storage)?;
    }
    Ok(())
}

fn fail_caption_proofs_without_controller(
    store: &StationStore,
    database_path: &Path,
    channel_id: &str,
    now_unix_ms: i64,
    detail: &str,
) -> Result<(), OrchestrationError> {
    let jobs = store
        .list_unfinished_caption_egress_jobs_for_channel(channel_id)
        .map_err(OrchestrationError::Storage)?;
    for job in jobs {
        let failure_detail =
            match cleanup_orphan_caption_proof_artifacts(database_path, &job.job.job_id) {
                Ok(()) => detail.to_string(),
                Err(error) => format!("{detail}; managed artifact cleanup also failed: {error}"),
            };
        store
            .fail_abandoned_caption_egress_job(
                &job.job.job_id,
                job.state,
                now_unix_ms,
                &failure_detail,
            )
            .map_err(OrchestrationError::Storage)?;
    }
    Ok(())
}

fn is_abort_confirmation(event: &WorkerEvent, proof_id: &str) -> bool {
    matches!(
        event,
        WorkerEvent::OutputProofFailed {
            proof_id: event_proof_id,
            code,
            ..
        } if event_proof_id == proof_id && code == "capture_aborted"
    )
}

/// Convert the durable channel sink sequence into the exact ordered worker
/// bootstrap outputs. The channel is the authority for ordering and loudness;
/// neither value is inferred or defaulted here. Encrypted SRT material is
/// resolved only through the station store and is never taken from the public
/// channel configuration.
pub fn worker_output_configurations(
    channel: &ChannelConfiguration,
    store: &StationStore,
) -> Result<Vec<WorkerOutputConfiguration>, OrchestrationError> {
    channel
        .validate()
        .map_err(|error| OrchestrationError::InvalidDestination(format!("{error:?}")))?;
    channel
        .output_sinks
        .iter()
        .filter(|sink| sink.enabled)
        .map(|sink| {
            let milli_lufs = (f64::from(sink.loudness_target_lufs) * 1_000.0).round() as i32;
            let loudness_target = LoudnessTarget::from_milli_lufs(milli_lufs)
                .map_err(|error| OrchestrationError::InvalidDestination(error.into()))?;
            match sink.kind {
                OutputSinkKind::UdpTs => {
                    let headend = sink
                        .headend
                        .as_ref()
                        .map(|contract| {
                            worker_headend_contract(
                                channel.output_profile,
                                loudness_target,
                                contract,
                            )
                        })
                        .transpose()?;
                    Ok(WorkerOutputConfiguration::UdpTs {
                        sink_id: sink.sink_id.clone(),
                        destination: sink.destination.clone(),
                        loudness_target_lufs: loudness_target,
                        headend,
                    })
                }
                OutputSinkKind::Srt => {
                    let passphrase = if sink.encryption_configured {
                        let secret = store
                            .read_srt_output_secret(&channel.channel_id, &sink.sink_id)
                            .map_err(OrchestrationError::Storage)?
                            .ok_or_else(|| {
                                OrchestrationError::Storage(
                                    "encrypted SRT output secret is missing".into(),
                                )
                            })?;
                        Some(SrtPassphrase::new(secret.expose().to_string()).map_err(
                            |message| OrchestrationError::InvalidDestination(message.into()),
                        )?)
                    } else {
                        None
                    };
                    Ok(WorkerOutputConfiguration::Srt {
                        sink_id: sink.sink_id.clone(),
                        uri: sink.destination.clone(),
                        passphrase,
                        loudness_target_lufs: loudness_target,
                    })
                }
            }
        })
        .collect()
}

fn worker_headend_contract(
    output_profile: OutputProfileId,
    loudness_target: LoudnessTarget,
    contract: &HeadendSinkContract,
) -> Result<WorkerHeadendTransportContract, OrchestrationError> {
    let profile_id = match contract.profile_id {
        HeadendProfileId::GenericUdpSpts => WorkerHeadendProfileId::GenericUdpSpts,
        HeadendProfileId::TelvueHypercasterIp => WorkerHeadendProfileId::TelvueHypercasterIp,
        _ => {
            return Err(OrchestrationError::InvalidDestination(
                "headend profile has no executable worker contract".into(),
            ));
        }
    };
    if output_profile != OutputProfileId::Atsc720p5994
        || loudness_target != LoudnessTarget::CABLE
        || contract.muxrate_kbps != 8_000
        || contract.packet_size_bytes != 1_316
    {
        return Err(OrchestrationError::InvalidDestination(
            "headend worker contract must use atsc-720p5994, -24 LUFS, 8000 kbps, and 1316-byte packets"
                .into(),
        ));
    }
    Ok(WorkerHeadendTransportContract {
        profile_id,
        muxrate_kbps: contract.muxrate_kbps,
        packet_size_bytes: contract.packet_size_bytes,
    })
}

struct LiveControlState {
    session_id: String,
    arm_token: Option<LiveArmToken>,
}

struct LiveRunOutcome {
    block_schedule: bool,
    transition: Option<DispatchTransition>,
}

struct LiveHandbackPlan {
    target: LiveReturnTarget,
    active_report: Option<(String, DispatchStatus)>,
    expired_acknowledged_reports: Vec<String>,
    detail: String,
}

struct CaptionDispatchState {
    report_id: String,
    track_id: String,
    track_revision: u64,
    cues_sha256: String,
    accepted_cue_ids: HashSet<String>,
    missed_cue_ids: HashSet<String>,
}

/// Supplies the candidate identity that is actually running on the station.
/// Proof jobs fail closed when this authority has not been connected by the
/// service host; a job's originally requested identity is never treated as
/// current merely because it was durable.
pub trait CandidateAuthority: Send + Sync {
    fn current_candidate(&self) -> Result<CandidateIdentity, String>;
}

pub struct ChannelController {
    supervisor: WorkerSupervisor,
    database_path: PathBuf,
    channel_id: String,
    command_timeout: Duration,
    loaded_report_id: Option<String>,
    on_air_asset_id: Option<String>,
    runtime_state: ChannelRuntimeState,
    state_started_at_unix_ms: i64,
    last_heartbeat_at_unix_ms: i64,
    output_expectations: Vec<OutputExpectation>,
    proof_output_bindings: Vec<ProofOutputBinding>,
    output_proof: OutputProofState,
    caption_proof: CaptionProofState,
    candidate_authority: Option<Arc<dyn CandidateAuthority>>,
    caption_decoder_executable: Option<PathBuf>,
    last_output_bytes: Option<Vec<u64>>,
    last_output_audio_buffers: Option<Vec<u64>>,
    cg_state: CgSnapshotState,
    next_cg_sync_at_unix_ms: i64,
    live_control: Option<LiveControlState>,
    caption_dispatch: Option<CaptionDispatchState>,
    caption_dispatch_detail: Option<String>,
}

impl ChannelController {
    pub fn launch(
        worker_executable: &Path,
        database_path: &Path,
        journal_path: &Path,
        channel: &ChannelConfiguration,
        timeout: Duration,
    ) -> Result<Self, OrchestrationError> {
        channel
            .validate()
            .map_err(|error| OrchestrationError::InvalidDestination(format!("{error:?}")))?;
        let store = StationStore::open(database_path).map_err(OrchestrationError::Storage)?;
        let outputs = worker_output_configurations(channel, &store)?;
        let fallback_slate = store
            .read_channel_fallback_slate(&channel.channel_id)
            .map_err(OrchestrationError::Storage)?
            .map(|selection| WorkerFallbackSlate {
                asset_id: selection.image_asset.asset_id,
                format: match selection.image_asset.mime_type.as_str() {
                    "image/png" => CgImageFormat::Png,
                    "image/jpeg" => CgImageFormat::Jpeg,
                    _ => unreachable!("storage validates managed fallback slate image MIME"),
                },
            });
        let caption_policy = store
            .read_channel_caption_policy(&channel.channel_id)
            .map_err(OrchestrationError::Storage)?
            .map(|policy| WorkerCaptionPolicy {
                mode: match policy.mode {
                    ChannelCaptionMode::Disabled => WorkerCaptionMode::Disabled,
                    ChannelCaptionMode::Cea708 => WorkerCaptionMode::Cea708,
                },
                service_number: policy.service_number,
                language: policy.language,
                revision: policy.revision,
            });
        let output_expectations = channel
            .output_sinks
            .iter()
            .filter(|sink| sink.enabled)
            .map(|sink| OutputExpectation {
                sink_id: sink.sink_id.clone(),
                label: sink.label.clone(),
                required: sink.required,
            })
            .collect();
        let proof_output_bindings = channel
            .output_sinks
            .iter()
            .filter(|sink| sink.enabled)
            .map(|sink| {
                let target_lufs_milli =
                    (f64::from(sink.loudness_target_lufs) * 1_000.0).round() as i32;
                ProofOutputBinding {
                    sink_id: sink.sink_id.clone(),
                    target_lufs_milli,
                }
            })
            .collect();
        let worker_id = format!("station-worker-{}", channel.channel_id);
        let supervisor = WorkerSupervisor::launch_with_outputs_roots_and_authorities(
            worker_executable,
            &worker_id,
            &channel.channel_id,
            journal_path,
            &outputs,
            &managed_cg_image_root(database_path),
            &managed_output_proof_root(database_path),
            fallback_slate,
            caption_policy,
            channel.output_profile,
            timeout.max(MINIMUM_WORKER_BOOTSTRAP_TIMEOUT),
        )
        .map_err(OrchestrationError::Worker)?;
        let started_at_unix_ms = system_time_millis()?;
        Ok(Self {
            supervisor,
            database_path: database_path.to_path_buf(),
            channel_id: channel.channel_id.clone(),
            command_timeout: timeout,
            loaded_report_id: None,
            on_air_asset_id: None,
            runtime_state: ChannelRuntimeState::HealthyFallback,
            state_started_at_unix_ms: started_at_unix_ms,
            last_heartbeat_at_unix_ms: started_at_unix_ms,
            output_expectations,
            proof_output_bindings,
            output_proof: OutputProofState::Idle,
            caption_proof: CaptionProofState::Idle,
            candidate_authority: None,
            caption_decoder_executable: None,
            last_output_bytes: None,
            last_output_audio_buffers: None,
            cg_state: CgSnapshotState::default(),
            next_cg_sync_at_unix_ms: 0,
            live_control: None,
            caption_dispatch: None,
            caption_dispatch_detail: None,
        })
    }

    pub fn accept_cg_snapshot(
        &mut self,
        snapshot: &CgResolvedSnapshot,
    ) -> Result<CgSnapshot, OrchestrationError> {
        self.cg_state.accept(snapshot)
    }

    pub fn cg_snapshot_after_worker_ready(&self) -> Option<CgSnapshot> {
        self.cg_state.resend_after_ready()
    }

    pub fn mark_cg_worker_ready(&mut self) {
        self.cg_state.worker_ready();
    }

    pub fn mark_cg_applied(&mut self, board_id: &str, board_revision: u64) {
        self.cg_state.mark_applied(board_id, board_revision);
    }

    pub fn seed_cg_state(&mut self, mut state: CgSnapshotState) {
        state.worker_ready();
        self.cg_state = state;
    }

    pub fn cg_state(&self) -> CgSnapshotState {
        self.cg_state.clone()
    }

    pub fn set_candidate_authority(&mut self, authority: Arc<dyn CandidateAuthority>) {
        self.candidate_authority = Some(authority);
    }

    pub fn set_caption_decoder_executable(&mut self, executable: PathBuf) {
        self.caption_decoder_executable = Some(executable);
    }

    fn fail_output_proof_job(
        &self,
        store: &StationStore,
        job: &OutputLoudnessProofJobRecord,
        now_unix_ms: i64,
        detail: impl Into<String>,
    ) -> Result<(), OrchestrationError> {
        store
            .fail_output_loudness_job(&job.job_id, job.state, now_unix_ms, &detail.into())
            .map(|_| ())
            .map_err(OrchestrationError::Storage)
    }

    fn fail_output_proof_job_after_cleanup(
        &self,
        store: &StationStore,
        job: &OutputLoudnessProofJobRecord,
        now_unix_ms: i64,
        detail: impl Into<String>,
    ) -> Result<(), OrchestrationError> {
        let detail = detail.into();
        let detail = match cleanup_orphan_output_proof_artifacts(
            &managed_output_proof_root(&self.database_path),
            &job.job_id,
        ) {
            Ok(()) => detail,
            Err(cleanup_error) => {
                format!("{detail}; managed artifact cleanup also failed: {cleanup_error}")
            }
        };
        self.fail_output_proof_job(store, job, now_unix_ms, detail)
    }

    fn abort_output_proof(&mut self, proof_id: &str) -> Result<(), OrchestrationError> {
        let response = self
            .supervisor
            .command(
                &format!("output-proof-abort-{proof_id}"),
                ChannelCommand::AbortOutputProof {
                    proof_id: proof_id.to_string(),
                },
                self.command_timeout,
            )
            .map_err(OrchestrationError::Worker)?;
        if is_abort_confirmation(&response.event, proof_id) {
            Ok(())
        } else {
            Err(OrchestrationError::UnexpectedWorkerEvent)
        }
    }

    fn abort_or_terminate_output_proof(&mut self, proof_id: &str) -> Option<String> {
        match self.abort_output_proof(proof_id) {
            Ok(()) => None,
            Err(error) => {
                let termination = match self.supervisor.force_terminate() {
                    Ok(()) => "worker terminated".to_string(),
                    Err(stop_error) => {
                        format!(
                            "worker termination failed and the session was retired: {stop_error:?}"
                        )
                    }
                };
                Some(format!(
                    "worker proof abort was ambiguous; {termination}: {error:?}"
                ))
            }
        }
    }

    fn verify_current_candidate(&self, job: &OutputLoudnessProofJobRecord) -> Result<(), String> {
        let authority = self
            .candidate_authority
            .as_ref()
            .ok_or_else(|| "output proof candidate authority is not connected".to_string())?;
        let current = authority.current_candidate()?;
        if current.version != job.candidate_version || current.source_commit != job.source_commit {
            return Err("output proof candidate identity is stale".into());
        }
        Ok(())
    }

    fn fail_analyzing_output_proof_job(
        &self,
        store: &StationStore,
        analyzing: &OutputProofAnalyzing,
        now_unix_ms: i64,
        detail: impl Into<String>,
    ) -> Result<(), OrchestrationError> {
        let path = proof_artifact_path(
            &managed_output_proof_root(&self.database_path),
            &analyzing.artifact_relative_path,
        )
        .map_err(OrchestrationError::Storage)?;
        match fs::symlink_metadata(&path) {
            Ok(_) => remove_managed_file(&path).map_err(OrchestrationError::Storage)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(OrchestrationError::Storage(format!(
                    "inspect output proof artifact for cleanup: {error}"
                )));
            }
        }
        self.fail_output_proof_job(store, &analyzing.job, now_unix_ms, detail)
    }

    fn proof_binding(
        &self,
        store: &StationStore,
        job: &OutputLoudnessProofJobRecord,
    ) -> Result<(u16, LoudnessTarget), String> {
        let snapshot = store
            .read_channel_loudness_proof_snapshot(&self.channel_id)?
            .ok_or_else(|| "output proof channel no longer exists".to_string())?;
        if snapshot.configuration_hash != job.configuration_hash {
            return Err("output proof configuration hash is stale".into());
        }
        let snapshot_bindings = snapshot
            .output_sinks
            .iter()
            .map(|sink| ProofOutputBinding {
                sink_id: sink.sink_id.clone(),
                target_lufs_milli: (f64::from(sink.loudness_target_lufs) * 1_000.0).round() as i32,
            })
            .collect::<Vec<_>>();
        if !proof_bindings_match(&self.proof_output_bindings, &snapshot_bindings) {
            return Err("output proof sink ordering or target changed".into());
        }
        let (index, sink) = snapshot
            .output_sinks
            .iter()
            .enumerate()
            .find(|(_, sink)| sink.sink_id == job.sink_id)
            .ok_or_else(|| "output proof sink is no longer configured".to_string())?;
        if sink.loudness_target_lufs.mul_add(1_000.0, 0.0).round() as i32 != job.target_lufs_milli {
            return Err("output proof sink target changed".into());
        }
        let binding = self
            .proof_output_bindings
            .get(index)
            .ok_or_else(|| "output proof sink is absent from the worker bootstrap".to_string())?;
        if binding.sink_id != sink.sink_id || binding.target_lufs_milli != job.target_lufs_milli {
            return Err("output proof worker binding is stale".into());
        }
        let target =
            LoudnessTarget::from_milli_lufs(job.target_lufs_milli).map_err(str::to_string)?;
        Ok((
            u16::try_from(index).map_err(|_| "output proof index exceeds protocol range")?,
            target,
        ))
    }

    fn begin_output_proof(
        &mut self,
        store: &StationStore,
        job: OutputLoudnessProofJobRecord,
        now_unix_ms: i64,
    ) -> Result<OutputProofState, OrchestrationError> {
        let (output_index, target) = match self.proof_binding(store, &job) {
            Ok(binding) => binding,
            Err(detail) => {
                self.fail_output_proof_job(store, &job, now_unix_ms, detail)?;
                return Ok(OutputProofState::Idle);
            }
        };
        if let Err(detail) = self.verify_current_candidate(&job) {
            self.fail_output_proof_job(store, &job, now_unix_ms, detail)?;
            return Ok(OutputProofState::Idle);
        }
        if let Err(detail) = cleanup_orphan_output_proof_artifacts(
            &managed_output_proof_root(&self.database_path),
            &job.job_id,
        ) {
            self.fail_output_proof_job(store, &job, now_unix_ms, detail)?;
            return Ok(OutputProofState::Idle);
        }
        let Some(capture_duration_ms) = capture_duration_for_sample(job.sample_duration_ms) else {
            self.fail_output_proof_job(
                store,
                &job,
                now_unix_ms,
                "output proof sample duration is outside the bounded range",
            )?;
            return Ok(OutputProofState::Idle);
        };
        let response = match self.supervisor.command(
            &format!("output-proof-start-{}", job.job_id),
            ChannelCommand::StartOutputProof {
                proof_id: job.job_id.clone(),
                output_index,
                sink_id: job.sink_id.clone(),
                loudness_target_lufs: target,
                duration_ms: capture_duration_ms,
                max_bytes: job.max_capture_bytes,
            },
            self.command_timeout,
        ) {
            Ok(response) => response,
            Err(error) => {
                // The worker may have accepted StartOutputProof before its
                // reply was lost. Sequence state is ambiguous, so no later
                // pipe command can safely reconcile it.
                let termination = match self.supervisor.force_terminate() {
                    Ok(()) => "worker terminated".to_string(),
                    Err(stop_error) => format!(
                        "worker termination failed and the session was retired: {stop_error:?}"
                    ),
                };
                self.fail_output_proof_job_after_cleanup(
                    store,
                    &job,
                    now_unix_ms,
                    format!("worker output proof start was ambiguous; {termination}: {error:?}"),
                )?;
                return Ok(OutputProofState::Idle);
            }
        };
        let WorkerEvent::OutputProofStarted {
            proof_id,
            output_index: event_index,
            sink_id,
            loudness_target_lufs,
            duration_ms,
            max_bytes,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
        } = response.event
        else {
            let termination = match self.supervisor.force_terminate() {
                Ok(()) => "worker terminated".to_string(),
                Err(stop_error) => {
                    format!("worker termination failed and the session was retired: {stop_error:?}")
                }
            };
            self.fail_output_proof_job_after_cleanup(
                store,
                &job,
                now_unix_ms,
                format!("worker returned an invalid output proof start response; {termination}"),
            )?;
            return Ok(OutputProofState::Idle);
        };
        if proof_id != job.job_id
            || event_index != output_index
            || sink_id != job.sink_id
            || loudness_target_lufs != target
            || duration_ms != capture_duration_ms
            || max_bytes != job.max_capture_bytes
            || worker_instance_id.is_empty()
        {
            let abort_detail = self.abort_or_terminate_output_proof(&job.job_id);
            self.fail_output_proof_job_after_cleanup(
                store,
                &job,
                now_unix_ms,
                abort_detail.map_or_else(
                    || {
                        "worker output proof start identity or bounds did not match the durable job"
                            .to_string()
                    },
                    |abort| {
                        format!(
                            "worker output proof start identity or bounds did not match the durable job; {abort}"
                        )
                    },
                ),
            )?;
            return Ok(OutputProofState::Idle);
        }
        if let Err(error) = proof_artifact_path(
            &managed_output_proof_root(&self.database_path),
            &artifact_relative_path,
        ) {
            let abort_detail = self.abort_or_terminate_output_proof(&job.job_id);
            let detail = abort_detail.map_or(error.clone(), |abort| format!("{error}; {abort}"));
            self.fail_output_proof_job_after_cleanup(store, &job, now_unix_ms, detail)?;
            return Ok(OutputProofState::Idle);
        }
        let capture_pending_job = store
            .advance_output_loudness_job(
                &job.job_id,
                OutputLoudnessProofJobState::Claimed,
                OutputLoudnessProofJobState::CapturePending,
                now_unix_ms,
                OUTPUT_PROOF_LEASE_MS,
                None,
            )
            .map_err(OrchestrationError::Storage)?;
        Ok(OutputProofState::Capturing {
            job: capture_pending_job,
            output_index,
            capture_duration_ms,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
            next_poll_at_unix_ms: now_unix_ms,
        })
    }

    fn poll_output_proof(
        &mut self,
        store: &StationStore,
        mut capture: OutputProofState,
        now_unix_ms: i64,
    ) -> Result<OutputProofState, OrchestrationError> {
        let OutputProofState::Capturing {
            job,
            output_index,
            capture_duration_ms,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
            next_poll_at_unix_ms,
        } = &mut capture
        else {
            return Ok(capture);
        };
        if lease_renewal_due(now_unix_ms, job.lease_expires_at_unix_ms) {
            *job = store
                .renew_output_loudness_job_lease(
                    &job.job_id,
                    OutputLoudnessProofJobState::CapturePending,
                    now_unix_ms,
                    OUTPUT_PROOF_LEASE_MS,
                )
                .map_err(OrchestrationError::Storage)?;
        }
        if now_unix_ms < *next_poll_at_unix_ms {
            return Ok(capture);
        }
        let response = match self.supervisor.command(
            &format!("output-proof-poll-{}-{now_unix_ms}", job.job_id),
            ChannelCommand::PollOutputProof {
                proof_id: job.job_id.clone(),
            },
            self.command_timeout,
        ) {
            Ok(response) => response,
            Err(error) => {
                let abort_detail = self.abort_or_terminate_output_proof(&job.job_id);
                let detail = abort_detail.map_or_else(
                    || format!("worker could not poll output proof: {error:?}"),
                    |abort| format!("worker could not poll output proof: {error:?}; {abort}"),
                );
                self.fail_output_proof_job_after_cleanup(store, job, now_unix_ms, detail)?;
                return Ok(OutputProofState::Idle);
            }
        };
        match response.event {
            WorkerEvent::OutputProofPending {
                proof_id,
                artifact_relative_path: event_path,
                worker_instance_id: event_worker,
                graph_revision: event_graph,
            } if proof_id == job.job_id
                && event_path == *artifact_relative_path
                && event_worker == *worker_instance_id
                && event_graph == *graph_revision =>
            {
                *next_poll_at_unix_ms = now_unix_ms.saturating_add(OUTPUT_PROOF_POLL_INTERVAL_MS);
                Ok(capture)
            }
            WorkerEvent::OutputProofReady {
                proof_id,
                output_index: event_index,
                sink_id,
                loudness_target_lufs,
                artifact_relative_path: event_path,
                worker_instance_id: event_worker,
                graph_revision: event_graph,
                size_bytes,
                capture_sha256,
                duration_ms,
            } if proof_id == job.job_id
                && event_index == *output_index
                && sink_id == job.sink_id
                && loudness_target_lufs.milli_lufs() == job.target_lufs_milli
                && event_path == *artifact_relative_path
                && event_worker == *worker_instance_id
                && event_graph == *graph_revision
                && ready_duration_is_bounded(duration_ms, *capture_duration_ms) =>
            {
                let path = match proof_artifact_path(
                    &managed_output_proof_root(&self.database_path),
                    artifact_relative_path,
                ) {
                    Ok(path) => path,
                    Err(error) => {
                        self.fail_output_proof_job_after_cleanup(store, job, now_unix_ms, error)?;
                        return Ok(OutputProofState::Idle);
                    }
                };
                let (actual_bytes, actual_hash) = match hash_capture(&path, job.max_capture_bytes) {
                    Ok(facts) => facts,
                    Err(error) => {
                        self.fail_output_proof_job_after_cleanup(store, job, now_unix_ms, error)?;
                        return Ok(OutputProofState::Idle);
                    }
                };
                if actual_bytes != size_bytes
                    || actual_bytes > job.max_capture_bytes
                    || actual_hash != capture_sha256
                {
                    self.fail_output_proof_job_after_cleanup(
                        store,
                        job,
                        now_unix_ms,
                        "output proof artifact identity did not match the worker report",
                    )?;
                    return Ok(OutputProofState::Idle);
                }
                let facts = OutputLoudnessCaptureFacts {
                    artifact_relative_path: artifact_relative_path.clone(),
                    capture_bytes: actual_bytes,
                    // The durable proof claims the requested lower-bound
                    // sample. The worker's wall-clock capture duration may be
                    // slightly longer and is retained separately for audit.
                    sample_duration_ms: job.sample_duration_ms,
                    capture_sha256: actual_hash,
                };
                let analyzing_job = store
                    .advance_output_loudness_job(
                        &job.job_id,
                        OutputLoudnessProofJobState::CapturePending,
                        OutputLoudnessProofJobState::Analyzing,
                        now_unix_ms,
                        OUTPUT_PROOF_LEASE_MS,
                        Some(&facts),
                    )
                    .map_err(OrchestrationError::Storage)?;
                let (sender, receiver) = mpsc::channel();
                let path_for_analysis = path.clone();
                let minimum_duration_ms = job.sample_duration_ms;
                thread::spawn(move || {
                    let result = measure_output_loudness(&path_for_analysis, minimum_duration_ms)
                        .map(|measurement| OutputProofAnalysis {
                            measured_lufs_milli: (measurement.integrated_lufs * 1_000.0).round()
                                as i32,
                            analyzed_duration_ms: measurement.analyzed_duration_ms,
                        })
                        .map_err(|error| {
                            format!("independent output loudness analysis failed: {error:?}")
                        });
                    let _ = sender.send(result);
                });
                Ok(OutputProofState::Analyzing(OutputProofAnalyzing {
                    job: analyzing_job,
                    artifact_relative_path: artifact_relative_path.clone(),
                    worker_instance_id: worker_instance_id.clone(),
                    graph_revision: *graph_revision,
                    worker_capture_duration_ms: duration_ms,
                    analysis: receiver,
                }))
            }
            WorkerEvent::OutputProofReady { proof_id, .. } if proof_id == job.job_id => {
                self.fail_output_proof_job_after_cleanup(
                    store,
                    job,
                    now_unix_ms,
                    "worker output proof ready identity, bounds, or duration was invalid",
                )?;
                Ok(OutputProofState::Idle)
            }
            WorkerEvent::OutputProofFailed {
                proof_id,
                worker_instance_id: event_worker,
                graph_revision: event_graph,
                code,
                message,
                ..
            } if proof_id == job.job_id
                && event_worker == *worker_instance_id
                && event_graph == *graph_revision =>
            {
                self.fail_output_proof_job_after_cleanup(
                    store,
                    job,
                    now_unix_ms,
                    format!("worker output proof failed ({code}): {message}"),
                )?;
                Ok(OutputProofState::Idle)
            }
            _ => {
                let abort_detail = self.abort_or_terminate_output_proof(&job.job_id);
                let detail = abort_detail.map_or_else(
                    || "worker output proof poll identity or state was invalid".to_string(),
                    |abort| {
                        format!("worker output proof poll identity or state was invalid; {abort}")
                    },
                );
                self.fail_output_proof_job_after_cleanup(store, job, now_unix_ms, detail)?;
                Ok(OutputProofState::Idle)
            }
        }
    }

    fn finish_output_proof(
        &mut self,
        store: &StationStore,
        mut analyzing: OutputProofAnalyzing,
        now_unix_ms: i64,
    ) -> Result<OutputProofState, OrchestrationError> {
        if lease_renewal_due(now_unix_ms, analyzing.job.lease_expires_at_unix_ms) {
            analyzing.job = store
                .renew_output_loudness_job_lease(
                    &analyzing.job.job_id,
                    OutputLoudnessProofJobState::Analyzing,
                    now_unix_ms,
                    OUTPUT_PROOF_LEASE_MS,
                )
                .map_err(OrchestrationError::Storage)?;
        }
        let result = match analyzing.analysis.try_recv() {
            Ok(result) => result,
            Err(mpsc::TryRecvError::Empty) => {
                return Ok(OutputProofState::Analyzing(analyzing));
            }
            Err(mpsc::TryRecvError::Disconnected) => {
                self.fail_analyzing_output_proof_job(
                    store,
                    &analyzing,
                    now_unix_ms,
                    "output proof analysis worker disconnected without a result",
                )?;
                return Ok(OutputProofState::Idle);
            }
        };
        let analysis = match result {
            Ok(analysis) => analysis,
            Err(error) => {
                self.fail_analyzing_output_proof_job(store, &analyzing, now_unix_ms, error)?;
                return Ok(OutputProofState::Idle);
            }
        };
        if analysis.analyzed_duration_ms < analyzing.job.sample_duration_ms {
            self.fail_analyzing_output_proof_job(
                store,
                &analyzing,
                now_unix_ms,
                "independent output proof analysis covered less time than requested",
            )?;
            return Ok(OutputProofState::Idle);
        }
        if let Err(detail) = self.verify_current_candidate(&analyzing.job) {
            self.fail_analyzing_output_proof_job(store, &analyzing, now_unix_ms, detail)?;
            return Ok(OutputProofState::Idle);
        }
        let current_hash = store
            .current_channel_output_configuration_hash(&self.channel_id)
            .map_err(OrchestrationError::Storage)?;
        if current_hash != analyzing.job.configuration_hash {
            self.fail_analyzing_output_proof_job(
                store,
                &analyzing,
                now_unix_ms,
                "output proof configuration changed during analysis",
            )?;
            return Ok(OutputProofState::Idle);
        }
        let outcome = derive_loudness_outcome(
            analyzing.job.target_lufs_milli,
            Some(analysis.measured_lufs_milli),
            OUTPUT_PROOF_TOLERANCE_MILLI,
        );
        if matches!(outcome, LoudnessProofOutcome::Invalid) {
            self.fail_analyzing_output_proof_job(
                store,
                &analyzing,
                now_unix_ms,
                "independent output proof produced an unsupported loudness measurement",
            )?;
            return Ok(OutputProofState::Idle);
        }
        let capture = analyzing.job.capture.as_ref().ok_or_else(|| {
            OrchestrationError::Storage("analyzing output proof has no capture facts".into())
        })?;
        let proof = SinkLoudnessProof {
            proof_id: analyzing.job.job_id.clone(),
            channel_id: analyzing.job.channel_id.clone(),
            sink_id: analyzing.job.sink_id.clone(),
            target_lufs_milli: analyzing.job.target_lufs_milli,
            measured_lufs_milli: Some(analysis.measured_lufs_milli),
            tolerance_lufs_milli: OUTPUT_PROOF_TOLERANCE_MILLI,
            outcome,
            receiver_acceptance: ReceiverAcceptance::NotAttempted,
            captured_at_unix_ms: now_unix_ms,
            sample_duration_ms: capture.sample_duration_ms,
            capture_bytes: capture.capture_bytes,
            capture_sha256: capture.capture_sha256.clone(),
            candidate_version: analyzing.job.candidate_version.clone(),
            source_commit: analyzing.job.source_commit.clone(),
            configuration_hash: analyzing.job.configuration_hash.clone(),
            measurement_engine: "gstreamer-ebur128".into(),
            measurement_method: "station-media-assets::measure_output_loudness".into(),
            detail: format!(
                "worker_instance_id={} graph_revision={} capture_window_ms={} analyzed_duration_ms={}",
                analyzing.worker_instance_id,
                analyzing.graph_revision,
                analyzing.worker_capture_duration_ms,
                analysis.analyzed_duration_ms
            ),
        };
        // Formal slot activation stops stationd before replacing candidate
        // receipts. Recheck at the final write boundary as well so any
        // out-of-band receipt change during analysis rolls the job back to an
        // explicit failure instead of appending evidence for stale identity.
        if let Err(detail) = self.verify_current_candidate(&analyzing.job) {
            self.fail_analyzing_output_proof_job(store, &analyzing, now_unix_ms, detail)?;
            return Ok(OutputProofState::Idle);
        }
        store
            .complete_output_loudness_job_with_proof(
                &analyzing.job.job_id,
                now_unix_ms,
                &proof,
                Some(&analyzing.artifact_relative_path),
            )
            .map_err(|error| {
                OrchestrationError::Storage(format!("complete output proof: {error}"))
            })?;
        Ok(OutputProofState::Idle)
    }

    fn output_proof_tick(
        &mut self,
        store: &StationStore,
        now_unix_ms: i64,
    ) -> Result<(), OrchestrationError> {
        let state = std::mem::take(&mut self.output_proof);
        self.output_proof = match state {
            OutputProofState::Idle => {
                let job = store
                    .claim_next_output_loudness_job(
                        &self.channel_id,
                        now_unix_ms,
                        OUTPUT_PROOF_LEASE_MS,
                    )
                    .map_err(OrchestrationError::Storage)?;
                match job {
                    Some(job) => self.begin_output_proof(store, job, now_unix_ms)?,
                    None => OutputProofState::Idle,
                }
            }
            state @ OutputProofState::Capturing { .. } => {
                self.poll_output_proof(store, state, now_unix_ms)?
            }
            OutputProofState::Analyzing(analyzing) => {
                self.finish_output_proof(store, analyzing, now_unix_ms)?
            }
        };
        Ok(())
    }

    fn fail_caption_proof_job_after_cleanup(
        &self,
        store: &StationStore,
        job: &CaptionEgressProofJobRecord,
        now_unix_ms: i64,
        detail: impl Into<String>,
    ) -> Result<(), OrchestrationError> {
        let detail = detail.into();
        let detail =
            match cleanup_orphan_caption_proof_artifacts(&self.database_path, &job.job.job_id) {
                Ok(()) => detail,
                Err(cleanup_error) => {
                    format!("{detail}; managed artifact cleanup also failed: {cleanup_error}")
                }
            };
        store
            .fail_caption_egress_job(&job.job.job_id, job.state, now_unix_ms, &detail)
            .map(|_| ())
            .map_err(OrchestrationError::Storage)
    }

    fn verify_current_caption_candidate(
        &self,
        job: &CaptionEgressProofJobRecord,
    ) -> Result<(), String> {
        let authority = self
            .candidate_authority
            .as_ref()
            .ok_or_else(|| "caption proof candidate authority is not connected".to_string())?;
        let current = authority.current_candidate()?;
        if current.version != job.job.candidate_version
            || current.source_commit.as_deref() != Some(job.job.source_commit.as_str())
        {
            return Err("caption proof candidate identity is stale".into());
        }
        Ok(())
    }

    fn caption_proof_binding(
        &self,
        store: &StationStore,
        job: &CaptionEgressProofJobRecord,
    ) -> Result<(u16, LoudnessTarget, i64), String> {
        let report = store
            .read_commit_report(&job.job.report_id)?
            .ok_or_else(|| "caption proof schedule report no longer exists".to_string())?;
        if report.channel_id != self.channel_id
            || report.asset_id != job.job.asset_id
            || report.dispatch_status != DispatchStatus::Acknowledged
        {
            return Err(
                "caption proof schedule report is not the acknowledged on-air authority".into(),
            );
        }
        let item = store
            .read_schedule_item(&report.schedule_item_id)?
            .ok_or_else(|| "caption proof schedule item no longer exists".to_string())?;
        if item.revision != report.schedule_item_revision
            || item.channel_id != self.channel_id
            || item.asset_id != job.job.asset_id
        {
            return Err("caption proof schedule item binding is stale".into());
        }
        let snapshot = store
            .read_channel_loudness_proof_snapshot(&self.channel_id)?
            .ok_or_else(|| "caption proof channel no longer exists".to_string())?;
        if snapshot.configuration_hash != job.job.configuration_hash {
            return Err("caption proof configuration hash is stale".into());
        }
        let snapshot_bindings = snapshot
            .output_sinks
            .iter()
            .map(|sink| ProofOutputBinding {
                sink_id: sink.sink_id.clone(),
                target_lufs_milli: (f64::from(sink.loudness_target_lufs) * 1_000.0).round() as i32,
            })
            .collect::<Vec<_>>();
        if !proof_bindings_match(&self.proof_output_bindings, &snapshot_bindings) {
            return Err("caption proof sink ordering or target changed".into());
        }
        let (index, sink) = snapshot
            .output_sinks
            .iter()
            .enumerate()
            .find(|(_, sink)| sink.sink_id == job.job.sink_id)
            .ok_or_else(|| "caption proof sink is no longer configured".to_string())?;
        let binding = self
            .proof_output_bindings
            .get(index)
            .ok_or_else(|| "caption proof sink is absent from the worker bootstrap".to_string())?;
        if binding.sink_id != sink.sink_id {
            return Err("caption proof worker binding is stale".into());
        }
        let target =
            LoudnessTarget::from_milli_lufs(binding.target_lufs_milli).map_err(str::to_string)?;
        Ok((
            u16::try_from(index).map_err(|_| "caption proof index exceeds protocol range")?,
            target,
            item.starts_at_unix_ms,
        ))
    }

    fn begin_caption_proof(
        &mut self,
        store: &StationStore,
        waiting: CaptionProofWaiting,
        now_unix_ms: i64,
    ) -> Result<CaptionProofState, OrchestrationError> {
        let mut job = waiting.job;
        if lease_renewal_due(now_unix_ms, job.lease_expires_at_unix_ms) {
            job = store
                .renew_caption_egress_job_lease(
                    &job.job.job_id,
                    CaptionEgressProofJobState::Claimed,
                    now_unix_ms,
                    OUTPUT_PROOF_LEASE_MS as u64,
                )
                .map_err(OrchestrationError::Storage)?;
        }
        match caption_capture_timing(
            waiting.schedule_starts_at_unix_ms,
            job.job.capture_started_asset_offset_ms,
            now_unix_ms,
        ) {
            CaptionCaptureTiming::Waiting => {
                return Ok(CaptionProofState::Waiting(CaptionProofWaiting {
                    job,
                    schedule_starts_at_unix_ms: waiting.schedule_starts_at_unix_ms,
                }));
            }
            CaptionCaptureTiming::Missed => {
                self.fail_caption_proof_job_after_cleanup(
                    store,
                    &job,
                    now_unix_ms,
                    "caption proof capture start missed its bounded on-air window",
                )?;
                return Ok(CaptionProofState::Idle);
            }
            CaptionCaptureTiming::Due => {}
        }
        if self.runtime_state != ChannelRuntimeState::OnAir
            || self.on_air_asset_id.as_deref() != Some(job.job.asset_id.as_str())
        {
            self.fail_caption_proof_job_after_cleanup(
                store,
                &job,
                now_unix_ms,
                "caption proof capture did not find the exact scheduled asset on air",
            )?;
            return Ok(CaptionProofState::Idle);
        }
        let (output_index, target, schedule_starts_at_unix_ms) =
            match self.caption_proof_binding(store, &job) {
                Ok(binding) => binding,
                Err(detail) => {
                    self.fail_caption_proof_job_after_cleanup(store, &job, now_unix_ms, detail)?;
                    return Ok(CaptionProofState::Idle);
                }
            };
        if schedule_starts_at_unix_ms != waiting.schedule_starts_at_unix_ms {
            self.fail_caption_proof_job_after_cleanup(
                store,
                &job,
                now_unix_ms,
                "caption proof scheduled start changed while awaiting capture",
            )?;
            return Ok(CaptionProofState::Idle);
        }
        if let Err(detail) = self.verify_current_caption_candidate(&job) {
            self.fail_caption_proof_job_after_cleanup(store, &job, now_unix_ms, detail)?;
            return Ok(CaptionProofState::Idle);
        }
        let Some(decoder) = self.caption_decoder_executable.as_ref() else {
            self.fail_caption_proof_job_after_cleanup(
                store,
                &job,
                now_unix_ms,
                "caption proof decoder authority is not connected",
            )?;
            return Ok(CaptionProofState::Idle);
        };
        if !decoder.is_absolute() || !decoder.is_file() {
            self.fail_caption_proof_job_after_cleanup(
                store,
                &job,
                now_unix_ms,
                "caption proof decoder executable is missing from its exact installed path",
            )?;
            return Ok(CaptionProofState::Idle);
        }
        if let Err(detail) =
            cleanup_orphan_caption_proof_artifacts(&self.database_path, &job.job.job_id)
        {
            self.fail_caption_proof_job_after_cleanup(store, &job, now_unix_ms, detail)?;
            return Ok(CaptionProofState::Idle);
        }
        let response = match self.supervisor.command(
            &format!("caption-proof-start-{}", job.job.job_id),
            ChannelCommand::StartOutputProof {
                proof_id: job.job.job_id.clone(),
                output_index,
                sink_id: job.job.sink_id.clone(),
                loudness_target_lufs: target,
                duration_ms: job.job.capture_duration_ms,
                max_bytes: job.job.max_capture_bytes,
            },
            self.command_timeout,
        ) {
            Ok(response) => response,
            Err(error) => {
                let termination = match self.supervisor.force_terminate() {
                    Ok(()) => "worker terminated".to_string(),
                    Err(stop_error) => format!(
                        "worker termination failed and the session was retired: {stop_error:?}"
                    ),
                };
                self.fail_caption_proof_job_after_cleanup(
                    store,
                    &job,
                    now_unix_ms,
                    format!("worker caption proof start was ambiguous; {termination}: {error:?}"),
                )?;
                return Ok(CaptionProofState::Idle);
            }
        };
        let WorkerEvent::OutputProofStarted {
            proof_id,
            output_index: event_index,
            sink_id,
            loudness_target_lufs,
            duration_ms,
            max_bytes,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
        } = response.event
        else {
            let termination = match self.supervisor.force_terminate() {
                Ok(()) => "worker terminated".to_string(),
                Err(stop_error) => {
                    format!("worker termination failed and the session was retired: {stop_error:?}")
                }
            };
            self.fail_caption_proof_job_after_cleanup(
                store,
                &job,
                now_unix_ms,
                format!("worker returned an invalid caption proof start response; {termination}"),
            )?;
            return Ok(CaptionProofState::Idle);
        };
        if proof_id != job.job.job_id
            || event_index != output_index
            || sink_id != job.job.sink_id
            || loudness_target_lufs != target
            || duration_ms != job.job.capture_duration_ms
            || max_bytes != job.job.max_capture_bytes
            || artifact_relative_path != format!("{}.ts", job.job.job_id)
            || worker_instance_id.is_empty()
        {
            let abort_detail = self.abort_or_terminate_output_proof(&job.job.job_id);
            let detail = abort_detail.map_or_else(
                || "worker caption proof start identity or bounds did not match the durable job".to_string(),
                |abort| format!("worker caption proof start identity or bounds did not match the durable job; {abort}"),
            );
            self.fail_caption_proof_job_after_cleanup(store, &job, now_unix_ms, detail)?;
            return Ok(CaptionProofState::Idle);
        }
        if let Err(error) = proof_artifact_path(
            &managed_output_proof_root(&self.database_path),
            &artifact_relative_path,
        ) {
            let abort_detail = self.abort_or_terminate_output_proof(&job.job.job_id);
            let detail = abort_detail.map_or(error.clone(), |abort| format!("{error}; {abort}"));
            self.fail_caption_proof_job_after_cleanup(store, &job, now_unix_ms, detail)?;
            return Ok(CaptionProofState::Idle);
        }
        let capture_accepted_at_unix_ms = system_time_millis()?;
        if caption_capture_timing(
            schedule_starts_at_unix_ms,
            job.job.capture_started_asset_offset_ms,
            capture_accepted_at_unix_ms,
        ) != CaptionCaptureTiming::Due
        {
            let abort_detail = self.abort_or_terminate_output_proof(&job.job.job_id);
            let detail = abort_detail.map_or_else(
                || "worker acknowledged caption capture after the bounded start window".to_string(),
                |abort| {
                    format!(
                        "worker acknowledged caption capture after the bounded start window; {abort}"
                    )
                },
            );
            self.fail_caption_proof_job_after_cleanup(
                store,
                &job,
                capture_accepted_at_unix_ms,
                detail,
            )?;
            return Ok(CaptionProofState::Idle);
        }
        let capture_pending_job = store
            .advance_caption_egress_job(
                &job.job.job_id,
                CaptionEgressProofJobState::Claimed,
                CaptionEgressProofJobState::CapturePending,
                capture_accepted_at_unix_ms,
                OUTPUT_PROOF_LEASE_MS as u64,
                None,
            )
            .map_err(OrchestrationError::Storage)?;
        Ok(CaptionProofState::Capturing {
            job: capture_pending_job,
            output_index,
            target,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
            captured_at_unix_ms: capture_accepted_at_unix_ms,
            next_poll_at_unix_ms: capture_accepted_at_unix_ms,
        })
    }

    fn poll_caption_proof(
        &mut self,
        store: &StationStore,
        mut capture: CaptionProofState,
        now_unix_ms: i64,
    ) -> Result<CaptionProofState, OrchestrationError> {
        let CaptionProofState::Capturing {
            job,
            output_index,
            target,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
            captured_at_unix_ms,
            next_poll_at_unix_ms,
        } = &mut capture
        else {
            return Ok(capture);
        };
        if lease_renewal_due(now_unix_ms, job.lease_expires_at_unix_ms) {
            *job = store
                .renew_caption_egress_job_lease(
                    &job.job.job_id,
                    CaptionEgressProofJobState::CapturePending,
                    now_unix_ms,
                    OUTPUT_PROOF_LEASE_MS as u64,
                )
                .map_err(OrchestrationError::Storage)?;
        }
        if now_unix_ms < *next_poll_at_unix_ms {
            return Ok(capture);
        }
        let response = match self.supervisor.command(
            &format!("caption-proof-poll-{}-{now_unix_ms}", job.job.job_id),
            ChannelCommand::PollOutputProof {
                proof_id: job.job.job_id.clone(),
            },
            self.command_timeout,
        ) {
            Ok(response) => response,
            Err(error) => {
                let abort_detail = self.abort_or_terminate_output_proof(&job.job.job_id);
                let detail = abort_detail.map_or_else(
                    || format!("worker could not poll caption proof: {error:?}"),
                    |abort| format!("worker could not poll caption proof: {error:?}; {abort}"),
                );
                self.fail_caption_proof_job_after_cleanup(store, job, now_unix_ms, detail)?;
                return Ok(CaptionProofState::Idle);
            }
        };
        match response.event {
            WorkerEvent::OutputProofPending {
                proof_id,
                artifact_relative_path: event_path,
                worker_instance_id: event_worker,
                graph_revision: event_graph,
            } if proof_id == job.job.job_id
                && event_path == *artifact_relative_path
                && event_worker == *worker_instance_id
                && event_graph == *graph_revision =>
            {
                *next_poll_at_unix_ms = now_unix_ms.saturating_add(OUTPUT_PROOF_POLL_INTERVAL_MS);
                Ok(capture)
            }
            WorkerEvent::OutputProofReady {
                proof_id,
                output_index: event_index,
                sink_id,
                loudness_target_lufs,
                artifact_relative_path: event_path,
                worker_instance_id: event_worker,
                graph_revision: event_graph,
                size_bytes,
                capture_sha256,
                duration_ms,
            } if proof_id == job.job.job_id
                && event_index == *output_index
                && sink_id == job.job.sink_id
                && loudness_target_lufs == *target
                && event_path == *artifact_relative_path
                && event_worker == *worker_instance_id
                && event_graph == *graph_revision
                && ready_duration_is_bounded(duration_ms, job.job.capture_duration_ms) =>
            {
                let worker_path = match proof_artifact_path(
                    &managed_output_proof_root(&self.database_path),
                    artifact_relative_path,
                ) {
                    Ok(path) => path,
                    Err(error) => {
                        self.fail_caption_proof_job_after_cleanup(store, job, now_unix_ms, error)?;
                        return Ok(CaptionProofState::Idle);
                    }
                };
                let (actual_bytes, actual_hash) =
                    match hash_capture(&worker_path, job.job.max_capture_bytes) {
                        Ok(facts) => facts,
                        Err(error) => {
                            self.fail_caption_proof_job_after_cleanup(
                                store,
                                job,
                                now_unix_ms,
                                error,
                            )?;
                            return Ok(CaptionProofState::Idle);
                        }
                    };
                if actual_bytes != size_bytes || actual_hash != capture_sha256 {
                    self.fail_caption_proof_job_after_cleanup(
                        store,
                        job,
                        now_unix_ms,
                        "caption proof artifact identity did not match the worker report",
                    )?;
                    return Ok(CaptionProofState::Idle);
                }
                let (path, durable_relative_path) = match promote_caption_proof_artifact(
                    &self.database_path,
                    &job.job.job_id,
                    artifact_relative_path,
                ) {
                    Ok(result) => result,
                    Err(error) => {
                        self.fail_caption_proof_job_after_cleanup(store, job, now_unix_ms, error)?;
                        return Ok(CaptionProofState::Idle);
                    }
                };
                let (promoted_bytes, promoted_hash) =
                    hash_capture(&path, job.job.max_capture_bytes)
                        .map_err(OrchestrationError::Storage)?;
                if promoted_bytes != actual_bytes || promoted_hash != actual_hash {
                    self.fail_caption_proof_job_after_cleanup(
                        store,
                        job,
                        now_unix_ms,
                        "caption proof artifact identity changed during atomic promotion",
                    )?;
                    return Ok(CaptionProofState::Idle);
                }
                let facts = CaptionProofCaptureFacts {
                    artifact_relative_path: durable_relative_path.clone(),
                    capture_bytes: promoted_bytes,
                    capture_duration_ms: job.job.capture_duration_ms,
                    capture_sha256: promoted_hash,
                };
                let analyzing_job = store
                    .advance_caption_egress_job(
                        &job.job.job_id,
                        CaptionEgressProofJobState::CapturePending,
                        CaptionEgressProofJobState::Analyzing,
                        now_unix_ms,
                        OUTPUT_PROOF_LEASE_MS as u64,
                        Some(&facts),
                    )
                    .map_err(OrchestrationError::Storage)?;
                let decoder = self.caption_decoder_executable.clone().ok_or_else(|| {
                    OrchestrationError::Storage(
                        "caption proof decoder authority disconnected".into(),
                    )
                })?;
                let (sender, receiver) = mpsc::channel();
                let path_for_analysis = path.clone();
                thread::spawn(move || {
                    let result =
                        decode_output_captions(&path_for_analysis, &decoder).map_err(|error| {
                            format!("independent caption decode-back failed: {error:?}")
                        });
                    let _ = sender.send(result);
                });
                Ok(CaptionProofState::Analyzing(CaptionProofAnalyzing {
                    job: analyzing_job,
                    artifact_relative_path: durable_relative_path,
                    worker_instance_id: worker_instance_id.clone(),
                    graph_revision: *graph_revision,
                    worker_capture_duration_ms: duration_ms,
                    captured_at_unix_ms: *captured_at_unix_ms,
                    analysis: receiver,
                }))
            }
            WorkerEvent::OutputProofReady { proof_id, .. } if proof_id == job.job.job_id => {
                self.fail_caption_proof_job_after_cleanup(
                    store,
                    job,
                    now_unix_ms,
                    "worker caption proof ready identity, bounds, or duration was invalid",
                )?;
                Ok(CaptionProofState::Idle)
            }
            WorkerEvent::OutputProofFailed {
                proof_id,
                worker_instance_id: event_worker,
                graph_revision: event_graph,
                code,
                message,
                ..
            } if proof_id == job.job.job_id
                && event_worker == *worker_instance_id
                && event_graph == *graph_revision =>
            {
                self.fail_caption_proof_job_after_cleanup(
                    store,
                    job,
                    now_unix_ms,
                    format!("worker caption proof failed ({code}): {message}"),
                )?;
                Ok(CaptionProofState::Idle)
            }
            _ => {
                let abort_detail = self.abort_or_terminate_output_proof(&job.job.job_id);
                let detail = abort_detail.map_or_else(
                    || "worker caption proof poll identity or state was invalid".to_string(),
                    |abort| {
                        format!("worker caption proof poll identity or state was invalid; {abort}")
                    },
                );
                self.fail_caption_proof_job_after_cleanup(store, job, now_unix_ms, detail)?;
                Ok(CaptionProofState::Idle)
            }
        }
    }

    fn finish_caption_proof(
        &self,
        store: &StationStore,
        mut analyzing: CaptionProofAnalyzing,
        now_unix_ms: i64,
    ) -> Result<CaptionProofState, OrchestrationError> {
        if lease_renewal_due(now_unix_ms, analyzing.job.lease_expires_at_unix_ms) {
            analyzing.job = store
                .renew_caption_egress_job_lease(
                    &analyzing.job.job.job_id,
                    CaptionEgressProofJobState::Analyzing,
                    now_unix_ms,
                    OUTPUT_PROOF_LEASE_MS as u64,
                )
                .map_err(OrchestrationError::Storage)?;
        }
        let decoded = match analyzing.analysis.try_recv() {
            Ok(Ok(decoded)) => decoded,
            Ok(Err(error)) => {
                self.fail_caption_proof_job_after_cleanup(
                    store,
                    &analyzing.job,
                    now_unix_ms,
                    error,
                )?;
                return Ok(CaptionProofState::Idle);
            }
            Err(mpsc::TryRecvError::Empty) => {
                return Ok(CaptionProofState::Analyzing(analyzing));
            }
            Err(mpsc::TryRecvError::Disconnected) => {
                self.fail_caption_proof_job_after_cleanup(
                    store,
                    &analyzing.job,
                    now_unix_ms,
                    "caption proof analysis worker disconnected without a result",
                )?;
                return Ok(CaptionProofState::Idle);
            }
        };
        if decoded.cue_count == 0 {
            self.fail_caption_proof_job_after_cleanup(
                store,
                &analyzing.job,
                now_unix_ms,
                "independent caption decode-back produced no caption cues",
            )?;
            return Ok(CaptionProofState::Idle);
        }
        if let Err(detail) = self.verify_current_caption_candidate(&analyzing.job) {
            self.fail_caption_proof_job_after_cleanup(store, &analyzing.job, now_unix_ms, detail)?;
            return Ok(CaptionProofState::Idle);
        }
        let current_hash = store
            .current_channel_output_configuration_hash(&self.channel_id)
            .map_err(OrchestrationError::Storage)?;
        if current_hash != analyzing.job.job.configuration_hash {
            self.fail_caption_proof_job_after_cleanup(
                store,
                &analyzing.job,
                now_unix_ms,
                "caption proof configuration changed during analysis",
            )?;
            return Ok(CaptionProofState::Idle);
        }
        let capture = analyzing.job.capture.as_ref().ok_or_else(|| {
            OrchestrationError::Storage("analyzing caption proof has no capture facts".into())
        })?;
        let outcome = if decoded.cue_count == analyzing.job.job.expected_cue_count
            && decoded.text_sha256 == analyzing.job.job.expected_text_sha256
        {
            CaptionProofOutcome::Verified
        } else {
            CaptionProofOutcome::Mismatch
        };
        let proof = CaptionEgressProof {
            proof_id: analyzing.job.job.job_id.clone(),
            channel_id: analyzing.job.job.channel_id.clone(),
            sink_id: analyzing.job.job.sink_id.clone(),
            report_id: analyzing.job.job.report_id.clone(),
            asset_id: analyzing.job.job.asset_id.clone(),
            track_id: analyzing.job.job.track_id.clone(),
            track_revision: analyzing.job.job.track_revision,
            cues_sha256: analyzing.job.job.cues_sha256.clone(),
            caption_policy_revision: analyzing.job.job.caption_policy_revision,
            language: analyzing.job.job.language.clone(),
            service_number: analyzing.job.job.service_number,
            candidate_version: analyzing.job.job.candidate_version.clone(),
            source_commit: analyzing.job.job.source_commit.clone(),
            configuration_hash: analyzing.job.job.configuration_hash.clone(),
            capture_started_asset_offset_ms: analyzing.job.job.capture_started_asset_offset_ms,
            capture_duration_ms: capture.capture_duration_ms,
            capture_bytes: capture.capture_bytes,
            capture_sha256: capture.capture_sha256.clone(),
            artifact_relative_path: analyzing.artifact_relative_path.clone(),
            expected_cue_count: analyzing.job.job.expected_cue_count,
            decoded_cue_count: decoded.cue_count,
            expected_text_sha256: analyzing.job.job.expected_text_sha256.clone(),
            decoded_text_sha256: decoded.text_sha256,
            decoder_engine: decoded.decoder_engine,
            decoder_method: decoded.decoder_method,
            outcome,
            captured_at_unix_ms: analyzing.captured_at_unix_ms,
        };
        if let Err(detail) = self.verify_current_caption_candidate(&analyzing.job) {
            self.fail_caption_proof_job_after_cleanup(store, &analyzing.job, now_unix_ms, detail)?;
            return Ok(CaptionProofState::Idle);
        }
        store
            .complete_caption_egress_job_with_proof(&analyzing.job.job.job_id, now_unix_ms, &proof)
            .map_err(|error| {
                OrchestrationError::Storage(format!("complete caption proof: {error}"))
            })?;
        let _ = (
            analyzing.worker_instance_id,
            analyzing.graph_revision,
            analyzing.worker_capture_duration_ms,
        );
        Ok(CaptionProofState::Idle)
    }

    fn caption_proof_tick(
        &mut self,
        store: &StationStore,
        now_unix_ms: i64,
    ) -> Result<(), OrchestrationError> {
        let state = std::mem::take(&mut self.caption_proof);
        self.caption_proof = match state {
            CaptionProofState::Idle => {
                let job = store
                    .claim_next_caption_egress_job(
                        &self.channel_id,
                        now_unix_ms,
                        OUTPUT_PROOF_LEASE_MS as u64,
                    )
                    .map_err(OrchestrationError::Storage)?;
                match job {
                    Some(job) => {
                        let (_, _, schedule_starts_at_unix_ms) =
                            match self.caption_proof_binding(store, &job) {
                                Ok(binding) => binding,
                                Err(detail) => {
                                    self.fail_caption_proof_job_after_cleanup(
                                        store,
                                        &job,
                                        now_unix_ms,
                                        detail,
                                    )?;
                                    return Ok(());
                                }
                            };
                        self.begin_caption_proof(
                            store,
                            CaptionProofWaiting {
                                job,
                                schedule_starts_at_unix_ms,
                            },
                            now_unix_ms,
                        )?
                    }
                    None => CaptionProofState::Idle,
                }
            }
            CaptionProofState::Waiting(waiting) => {
                self.begin_caption_proof(store, waiting, now_unix_ms)?
            }
            state @ CaptionProofState::Capturing { .. } => {
                self.poll_caption_proof(store, state, now_unix_ms)?
            }
            CaptionProofState::Analyzing(analyzing) => {
                self.finish_caption_proof(store, analyzing, now_unix_ms)?
            }
        };
        Ok(())
    }

    fn poll_health(&mut self, now_unix_ms: i64) -> Result<(), OrchestrationError> {
        let response = self
            .supervisor
            .command(
                &format!("heartbeat-{now_unix_ms}"),
                ChannelCommand::Ping,
                self.command_timeout,
            )
            .map_err(OrchestrationError::Worker)?;
        let outputs = match response.event {
            WorkerEvent::Heartbeat { outputs, .. } => outputs,
            WorkerEvent::CommandRejected { code, message, .. }
                if code == "output_health_failed" =>
            {
                return Err(OrchestrationError::OutputGraphUnhealthy { detail: message });
            }
            WorkerEvent::CommandRejected { code, .. } if code == "live_input_stalling" => {
                // One unadvanced interval is a bounded grace observation. The
                // worker retains the baseline and escalates a repeated/aged
                // stall; do not report output health from this grace poll.
                return Ok(());
            }
            WorkerEvent::CommandRejected { code, message, .. } if code == "live_input_stalled" => {
                return Err(OrchestrationError::LiveInputStalled { detail: message });
            }
            _ => return Err(OrchestrationError::UnexpectedWorkerEvent),
        };
        if outputs.len() != self.output_expectations.len()
            || outputs
                .iter()
                .enumerate()
                .any(|(index, output)| usize::from(output.output_index) != index)
        {
            return Err(OrchestrationError::UnexpectedWorkerEvent);
        }
        let current = outputs
            .iter()
            .map(|output| output.bytes_sent_total)
            .collect::<Vec<_>>();
        let current_audio = outputs
            .iter()
            .map(|output| output.audio_buffers_total)
            .collect::<Vec<_>>();
        if let Some(index) = stalled_required_output(
            &self.output_expectations,
            self.last_output_bytes.as_deref(),
            &current,
        ) {
            let expectation = &self.output_expectations[index];
            return Err(OrchestrationError::RequiredOutputStalled {
                sink_id: expectation.sink_id.clone(),
                label: expectation.label.clone(),
                detail: "The local transport byte counter did not advance between heartbeat polls."
                    .into(),
            });
        }
        if let Some(index) = stalled_required_output(
            &self.output_expectations,
            self.last_output_audio_buffers.as_deref(),
            &current_audio,
        ) {
            let expectation = &self.output_expectations[index];
            return Err(OrchestrationError::RequiredOutputStalled {
                sink_id: expectation.sink_id.clone(),
                label: expectation.label.clone(),
                detail: "The encoded audio buffer counter did not advance between heartbeat polls."
                    .into(),
            });
        }
        self.last_output_bytes = Some(current);
        self.last_output_audio_buffers = Some(current_audio);
        Ok(())
    }

    fn dispatch_due_caption_cues(
        &mut self,
        store: &StationStore,
        job: &DispatchJob,
        media_offset_ms: u64,
    ) -> Result<(), OrchestrationError> {
        let Some(policy) = store
            .read_channel_caption_policy(&self.channel_id)
            .map_err(OrchestrationError::Storage)?
            .filter(|policy| policy.mode == ChannelCaptionMode::Cea708)
        else {
            self.caption_dispatch = None;
            self.caption_dispatch_detail = None;
            return Ok(());
        };
        let track = store
            .list_asset_caption_tracks(&job.asset.asset_id)
            .map_err(OrchestrationError::Storage)?
            .into_iter()
            .find(|track| {
                track.language == policy.language && track.service_number == policy.service_number
            });
        let Some(track) = track else {
            self.caption_dispatch = None;
            self.caption_dispatch_detail = None;
            return Ok(());
        };
        if self.caption_dispatch.as_ref().is_none_or(|state| {
            state.report_id != job.report.report_id
                || state.track_id != track.track_id
                || state.track_revision != track.revision
                || state.cues_sha256 != track.cues_sha256
        }) {
            self.caption_dispatch = Some(CaptionDispatchState {
                report_id: job.report.report_id.clone(),
                track_id: track.track_id.clone(),
                track_revision: track.revision,
                cues_sha256: track.cues_sha256.clone(),
                accepted_cue_ids: HashSet::new(),
                missed_cue_ids: HashSet::new(),
            });
            self.caption_dispatch_detail = None;
        }
        let Some(dispatch_state) = self.caption_dispatch.as_ref() else {
            return Err(OrchestrationError::CaptionAuthority(
                "caption dispatch state was not initialized".into(),
            ));
        };
        let plan = due_caption_cues(
            &track,
            media_offset_ms,
            &dispatch_state.accepted_cue_ids,
            &dispatch_state.missed_cue_ids,
        );
        let batch_had_miss = !plan.missed_cue_ids.is_empty();
        if let Some(first_missed_cue_id) = plan.missed_cue_ids.first() {
            let newly_observed = observe_caption_cue_misses(
                store,
                &self.channel_id,
                &job.report.report_id,
                &track,
                media_offset_ms,
                CaptionCueMissReason::ExpiredBeforeAdmission,
                &plan.missed_cue_ids,
            )?;
            let Some(state) = self.caption_dispatch.as_mut() else {
                return Err(OrchestrationError::CaptionAuthority(
                    "caption dispatch state ended while recording missed cues".into(),
                ));
            };
            state
                .missed_cue_ids
                .extend(plan.missed_cue_ids.iter().cloned());
            self.caption_dispatch_detail = Some(format!(
                "{} caption cue(s) missed their scheduled on-air window for report {} (first cue {}; durable evidence {}).",
                plan.missed_cue_ids.len(),
                job.report.report_id,
                first_missed_cue_id,
                if newly_observed {
                    "recorded"
                } else {
                    "already recorded"
                },
            ));
        }
        let mut accepted_cue_count = 0usize;
        for cue in plan.due {
            let event = self
                .supervisor
                .command(
                    &format!(
                        "caption-{}-{}-{}",
                        job.report.report_id, track.revision, cue.cue_id
                    ),
                    ChannelCommand::PushCaptionCue { cue: cue.clone() },
                    self.command_timeout,
                )
                .map_err(OrchestrationError::Worker)?;
            if !matches!(event.event, WorkerEvent::CaptionCueAccepted { ref cue_id } if cue_id == &cue.cue_id)
            {
                let cue_ids = vec![cue.cue_id.clone()];
                let newly_observed = observe_caption_cue_misses(
                    store,
                    &self.channel_id,
                    &job.report.report_id,
                    &track,
                    media_offset_ms,
                    CaptionCueMissReason::WorkerDidNotAcknowledge,
                    &cue_ids,
                )?;
                self.caption_dispatch_detail = Some(format!(
                    "Caption cue {} was not acknowledged by the worker for report {} (durable evidence {}).",
                    cue.cue_id,
                    job.report.report_id,
                    if newly_observed {
                        "recorded"
                    } else {
                        "already recorded"
                    },
                ));
                return Err(OrchestrationError::CaptionAuthority(format!(
                    "worker did not accept caption cue {} from track {} revision {}",
                    cue.cue_id, track.track_id, track.revision
                )));
            }
            let Some(state) = self.caption_dispatch.as_mut() else {
                return Err(OrchestrationError::CaptionAuthority(
                    "caption dispatch state ended after worker acceptance".into(),
                ));
            };
            state.accepted_cue_ids.insert(cue.cue_id);
            accepted_cue_count = accepted_cue_count.checked_add(1).ok_or_else(|| {
                OrchestrationError::CaptionAuthority("caption acceptance count overflowed".into())
            })?;
        }
        // An incident never clears merely because time advanced or because an
        // operator acknowledged it. A later, completely clean worker
        // admission is the recovery condition for this channel-scoped alert.
        if accepted_cue_count > 0 {
            resolve_caption_cue_miss_after_admission(
                store,
                &self.channel_id,
                accepted_cue_count,
                batch_had_miss,
            )?;
        }
        Ok(())
    }

    fn clear_caption_cues_before_source_transition(
        &mut self,
        transition_id: &str,
    ) -> Result<(), OrchestrationError> {
        let event = self
            .supervisor
            .command(
                &format!("caption-clear-{transition_id}"),
                ChannelCommand::ClearCaptionCues,
                self.command_timeout,
            )
            .map_err(OrchestrationError::Worker)?;
        if !matches!(event.event, WorkerEvent::CaptionCuesCleared) {
            return Err(OrchestrationError::CaptionAuthority(format!(
                "worker did not acknowledge caption clear before source transition {transition_id}"
            )));
        }
        // Accepted IDs are valid only for the source generation that just
        // ended. A later return/catch-up must re-admit only its still-live cue.
        self.caption_dispatch = None;
        self.caption_dispatch_detail = None;
        Ok(())
    }

    fn ensure_live_armed(
        &mut self,
        store: &StationStore,
        session: &TakeoverSession,
    ) -> Result<(), OrchestrationError> {
        if let Some(control) = &self.live_control {
            if control.session_id == session.session_id && control.arm_token.is_some() {
                return Ok(());
            }
            return Err(OrchestrationError::LiveControl(
                "The worker live-input authority does not match the durable takeover session."
                    .into(),
            ));
        }
        let source = store
            .read_live_source(&session.source_id)
            .map_err(OrchestrationError::Storage)?
            .ok_or_else(|| {
                OrchestrationError::LiveControl(
                    "The durable takeover references a live source that no longer exists.".into(),
                )
            })?;
        if !source.enabled
            || source.channel_id != self.channel_id
            || source.revision != session.source_revision
        {
            return Err(OrchestrationError::LiveControl(
                "The live source changed, was disabled, or belongs to another channel.".into(),
            ));
        }
        let (bind_address, port) = match source.endpoint {
            LiveEndpoint::UdpMpegTs { bind_ip, port } => (bind_ip, port),
        };
        let port = NonZeroU16::new(port).ok_or_else(|| {
            OrchestrationError::LiveControl("The live source port is invalid.".into())
        })?;
        let arm_token = generate_live_arm_token()?;
        let input = LiveInputConfiguration::new(
            session.session_id.clone(),
            session.source_id.clone(),
            session.source_revision,
            bind_address,
            port,
        )
        .map_err(|message| OrchestrationError::LiveControl(message.into()))?;
        let response = self
            .supervisor
            .command(
                &format!("live-arm-{}", session.session_id),
                ChannelCommand::ArmLive {
                    input,
                    arm_token: arm_token.clone(),
                },
                self.command_timeout,
            )
            .map_err(OrchestrationError::Worker)?;
        if !matches!(
            response.event,
            WorkerEvent::LiveArmed {
                ref session_id,
                ref source_id,
                source_revision,
            } if session_id == &session.session_id
                && source_id == &session.source_id
                && source_revision == session.source_revision
        ) {
            return Err(OrchestrationError::UnexpectedWorkerEvent);
        }
        self.live_control = Some(LiveControlState {
            session_id: session.session_id.clone(),
            arm_token: Some(arm_token),
        });
        Ok(())
    }

    fn retire_missed_dispatches(
        &self,
        store: &StationStore,
        now_unix_ms: i64,
    ) -> Result<Option<DispatchTransition>, OrchestrationError> {
        let jobs = store
            .list_dispatch_jobs(&self.channel_id)
            .map_err(OrchestrationError::Storage)?;
        let mut first = None;
        for job in jobs {
            let window = job
                .item
                .window_at(now_unix_ms)
                .map_err(|error| OrchestrationError::Storage(format!("{error:?}")))?;
            if window == ScheduleWindow::Ended
                && matches!(
                    job.report.dispatch_status,
                    DispatchStatus::Pending | DispatchStatus::Queued
                )
            {
                self.advance(
                    store,
                    &job.report.report_id,
                    job.report.dispatch_status,
                    DispatchStatus::Missed,
                )?;
                first.get_or_insert(DispatchTransition::Missed {
                    report_id: job.report.report_id,
                });
            }
        }
        Ok(first)
    }

    fn load_dispatch_job(
        &mut self,
        store: &StationStore,
        job: &DispatchJob,
    ) -> Result<DispatchStatus, OrchestrationError> {
        if self.loaded_report_id.as_deref() != Some(job.report.report_id.as_str()) {
            let event = self
                .supervisor
                .command(
                    &format!("load-{}", job.report.report_id),
                    ChannelCommand::LoadAsset {
                        asset_id: job.asset.asset_id.clone(),
                        media_path: job.asset.media_path.clone(),
                    },
                    self.command_timeout.max(MEDIA_LOAD_TIMEOUT),
                )
                .map_err(OrchestrationError::Worker)?;
            if !matches!(event.event, WorkerEvent::AssetLoaded { ref asset_id } if asset_id == &job.asset.asset_id)
            {
                self.advance(
                    store,
                    &job.report.report_id,
                    job.report.dispatch_status,
                    DispatchStatus::Error,
                )?;
                return Err(OrchestrationError::UnexpectedWorkerEvent);
            }
            self.loaded_report_id = Some(job.report.report_id.clone());
        }
        if job.report.dispatch_status == DispatchStatus::Pending {
            self.advance(
                store,
                &job.report.report_id,
                DispatchStatus::Pending,
                DispatchStatus::Queued,
            )?;
            Ok(DispatchStatus::Queued)
        } else {
            Ok(job.report.dispatch_status)
        }
    }

    fn prepare_live_handback(
        &mut self,
        store: &StationStore,
        now_unix_ms: i64,
    ) -> Result<LiveHandbackPlan, OrchestrationError> {
        let preparation_started = Instant::now();
        let mut expired_acknowledged_reports = Vec::new();
        for _ in 0..16 {
            let elapsed_ms =
                i64::try_from(preparation_started.elapsed().as_millis()).unwrap_or(i64::MAX);
            let decision_now = now_unix_ms.saturating_add(elapsed_ms);
            self.retire_missed_dispatches(store, decision_now)?;
            let jobs = store
                .list_dispatch_jobs(&self.channel_id)
                .map_err(OrchestrationError::Storage)?;
            let mut active = None;
            for job in jobs {
                match job
                    .item
                    .window_at(decision_now)
                    .map_err(|error| OrchestrationError::Storage(format!("{error:?}")))?
                {
                    ScheduleWindow::Active { media_offset_ms } if active.is_none() => {
                        active = Some((job, media_offset_ms));
                    }
                    ScheduleWindow::Ended
                        if job.report.dispatch_status == DispatchStatus::Acknowledged =>
                    {
                        if !expired_acknowledged_reports.contains(&job.report.report_id) {
                            expired_acknowledged_reports.push(job.report.report_id);
                        }
                    }
                    ScheduleWindow::Future
                    | ScheduleWindow::Active { .. }
                    | ScheduleWindow::Ended => {}
                }
            }
            let Some((job, _)) = active else {
                return Ok(LiveHandbackPlan {
                    target: LiveReturnTarget::Fallback,
                    active_report: None,
                    expired_acknowledged_reports,
                    detail: "Returned to healthy fallback because no committed schedule window was active."
                        .into(),
                });
            };
            let dispatch_status = self.load_dispatch_job(store, &job)?;
            let elapsed_ms =
                i64::try_from(preparation_started.elapsed().as_millis()).unwrap_or(i64::MAX);
            let switch_now = now_unix_ms.saturating_add(elapsed_ms);
            match job
                .item
                .window_at(switch_now)
                .map_err(|error| OrchestrationError::Storage(format!("{error:?}")))?
            {
                ScheduleWindow::Active { media_offset_ms } => {
                    return Ok(LiveHandbackPlan {
                        target: LiveReturnTarget::Program {
                            asset_id: job.asset.asset_id.clone(),
                            media_offset_ms,
                        },
                        active_report: Some((job.report.report_id, dispatch_status)),
                        expired_acknowledged_reports,
                        detail: format!(
                            "Returned to scheduled asset {} at media offset {} ms.",
                            job.asset.asset_id, media_offset_ms
                        ),
                    });
                }
                ScheduleWindow::Ended => continue,
                ScheduleWindow::Future => {
                    return Err(OrchestrationError::Clock(
                        "The station clock moved backward while preparing live handback.".into(),
                    ));
                }
            }
        }
        Err(OrchestrationError::LiveControl(
            "The schedule crossed too many boundaries while live handback was being prepared."
                .into(),
        ))
    }

    fn reconcile_live(
        &mut self,
        store: &StationStore,
        now_unix_ms: i64,
    ) -> Result<LiveRunOutcome, OrchestrationError> {
        let Some(session) = store
            .read_active_takeover(&self.channel_id)
            .map_err(OrchestrationError::Storage)?
        else {
            if self.live_control.is_some() {
                return Err(OrchestrationError::LiveControl(
                    "The worker retains live-input authority without an active durable session."
                        .into(),
                ));
            }
            return Ok(LiveRunOutcome {
                block_schedule: false,
                transition: None,
            });
        };
        if matches!(
            session.state,
            TakeoverState::Arming | TakeoverState::Armed | TakeoverState::TakeRequested
        ) && now_unix_ms >= session.expires_at_unix_ms
        {
            if self
                .live_control
                .as_ref()
                .is_some_and(|control| control.session_id == session.session_id)
            {
                let response = self
                    .supervisor
                    .command(
                        &format!("live-expired-disarm-{}", session.session_id),
                        ChannelCommand::DisarmLive {
                            session_id: session.session_id.clone(),
                        },
                        self.command_timeout.max(MEDIA_TEARDOWN_TIMEOUT),
                    )
                    .map_err(OrchestrationError::Worker)?;
                if !matches!(response.event, WorkerEvent::LiveDisarmed { ref session_id } if session_id == &session.session_id)
                {
                    return Err(OrchestrationError::UnexpectedWorkerEvent);
                }
                self.live_control = None;
            }
            store
                .transition_takeover(
                    &session.session_id,
                    session.state,
                    TakeoverState::Failed,
                    now_unix_ms,
                    Some("The live takeover intent expired before going on air."),
                    "stationd-live-controller",
                    session.authority_revision,
                )
                .map_err(OrchestrationError::Storage)?;
            return Ok(LiveRunOutcome {
                block_schedule: false,
                transition: None,
            });
        }
        match session.state {
            TakeoverState::Arming => {
                self.ensure_live_armed(store, &session)?;
                store
                    .transition_takeover(
                        &session.session_id,
                        TakeoverState::Arming,
                        TakeoverState::Armed,
                        now_unix_ms,
                        None,
                        "stationd-live-controller",
                        session.authority_revision,
                    )
                    .map_err(OrchestrationError::Storage)?;
                Ok(LiveRunOutcome {
                    block_schedule: false,
                    transition: Some(DispatchTransition::LiveArmed {
                        session_id: session.session_id,
                    }),
                })
            }
            TakeoverState::Armed => {
                self.ensure_live_armed(store, &session)?;
                Ok(LiveRunOutcome {
                    block_schedule: false,
                    transition: None,
                })
            }
            TakeoverState::TakeRequested => {
                self.ensure_live_armed(store, &session)?;
                let arm_token = self
                    .live_control
                    .as_ref()
                    .and_then(|control| control.arm_token.clone())
                    .ok_or_else(|| {
                        OrchestrationError::LiveControl(
                            "The live take capability has already been consumed.".into(),
                        )
                    })?;
                self.clear_caption_cues_before_source_transition(&format!(
                    "live-take-{}",
                    session.session_id
                ))?;
                let response = self
                    .supervisor
                    .command(
                        &format!("live-take-{}", session.session_id),
                        ChannelCommand::TakeLive {
                            session_id: session.session_id.clone(),
                            arm_token,
                        },
                        self.command_timeout,
                    )
                    .map_err(OrchestrationError::Worker)?;
                if !matches!(
                    response.event,
                    WorkerEvent::LiveOnAir {
                        ref session_id,
                        ref source_id,
                        source_revision,
                    } if session_id == &session.session_id
                        && source_id == &session.source_id
                        && source_revision == session.source_revision
                ) {
                    return Err(OrchestrationError::UnexpectedWorkerEvent);
                }
                if let Some(control) = self.live_control.as_mut() {
                    control.arm_token = None;
                }
                store
                    .transition_takeover(
                        &session.session_id,
                        TakeoverState::TakeRequested,
                        TakeoverState::OnAir,
                        now_unix_ms,
                        None,
                        "stationd-live-controller",
                        session.authority_revision,
                    )
                    .map_err(OrchestrationError::Storage)?;
                self.on_air_asset_id = None;
                self.caption_dispatch = None;
                self.mark_state(ChannelRuntimeState::Live, now_unix_ms);
                Ok(LiveRunOutcome {
                    block_schedule: true,
                    transition: Some(DispatchTransition::LiveOnAir {
                        session_id: session.session_id,
                    }),
                })
            }
            TakeoverState::OnAir => {
                if !self.live_control.as_ref().is_some_and(|control| {
                    control.session_id == session.session_id && control.arm_token.is_none()
                }) {
                    store
                        .transition_takeover(
                            &session.session_id,
                            TakeoverState::OnAir,
                            TakeoverState::Failed,
                            now_unix_ms,
                            Some("The channel worker restarted while live was on air; output returned to its safe startup source."),
                            "stationd-live-controller",
                            session.authority_revision,
                        )
                        .map_err(OrchestrationError::Storage)?;
                    return Err(OrchestrationError::LiveControl(
                        "The live on-air session could not survive a worker restart.".into(),
                    ));
                }
                self.mark_state(ChannelRuntimeState::Live, now_unix_ms);
                Ok(LiveRunOutcome {
                    block_schedule: true,
                    transition: None,
                })
            }
            TakeoverState::ReturnRequested => {
                if !self.live_control.as_ref().is_some_and(|control| {
                    control.session_id == session.session_id && control.arm_token.is_none()
                }) {
                    return Err(OrchestrationError::LiveControl(
                        "The return request has no matching on-air worker authority.".into(),
                    ));
                }
                let returning = store
                    .transition_takeover(
                        &session.session_id,
                        TakeoverState::ReturnRequested,
                        TakeoverState::Returning,
                        now_unix_ms,
                        None,
                        "stationd-live-controller",
                        session.authority_revision,
                    )
                    .map_err(OrchestrationError::Storage)?;
                let handback = self.prepare_live_handback(store, now_unix_ms)?;
                self.clear_caption_cues_before_source_transition(&format!(
                    "live-return-{}",
                    session.session_id
                ))?;
                let response = self
                    .supervisor
                    .command(
                        &format!("live-return-{}", session.session_id),
                        ChannelCommand::ReturnFromLive {
                            session_id: session.session_id.clone(),
                            target: handback.target.clone(),
                        },
                        self.command_timeout,
                    )
                    .map_err(OrchestrationError::Worker)?;
                let confirmed = match (&handback.target, &response.event) {
                    (
                        LiveReturnTarget::Fallback,
                        WorkerEvent::LiveReturned {
                            session_id,
                            source_kind,
                            source_id,
                        },
                    ) => {
                        session_id == &session.session_id
                            && source_kind == "fallback"
                            && source_id == "station-fallback"
                    }
                    (
                        LiveReturnTarget::Program { asset_id, .. },
                        WorkerEvent::LiveReturned {
                            session_id,
                            source_kind,
                            source_id,
                        },
                    ) => {
                        session_id == &session.session_id
                            && source_kind == "asset"
                            && source_id == asset_id
                    }
                    _ => false,
                };
                if !confirmed {
                    return Err(OrchestrationError::UnexpectedWorkerEvent);
                }
                for report_id in &handback.expired_acknowledged_reports {
                    self.advance(
                        store,
                        report_id,
                        DispatchStatus::Acknowledged,
                        DispatchStatus::Completed,
                    )?;
                }
                if let Some((report_id, DispatchStatus::Queued)) = &handback.active_report {
                    self.advance(
                        store,
                        report_id,
                        DispatchStatus::Queued,
                        DispatchStatus::Acknowledged,
                    )?;
                }
                self.on_air_asset_id = match &handback.target {
                    LiveReturnTarget::Fallback => None,
                    LiveReturnTarget::Program { asset_id, .. } => Some(asset_id.clone()),
                };
                let response = self
                    .supervisor
                    .command(
                        &format!("live-disarm-{}", session.session_id),
                        ChannelCommand::DisarmLive {
                            session_id: session.session_id.clone(),
                        },
                        self.command_timeout.max(MEDIA_TEARDOWN_TIMEOUT),
                    )
                    .map_err(OrchestrationError::Worker)?;
                if !matches!(response.event, WorkerEvent::LiveDisarmed { ref session_id } if session_id == &session.session_id)
                {
                    return Err(OrchestrationError::UnexpectedWorkerEvent);
                }
                self.live_control = None;
                store
                    .transition_takeover(
                        &session.session_id,
                        TakeoverState::Returning,
                        TakeoverState::Completed,
                        now_unix_ms,
                        Some(&handback.detail),
                        "stationd-live-controller",
                        returning.authority_revision,
                    )
                    .map_err(OrchestrationError::Storage)?;
                self.mark_state(
                    if matches!(handback.target, LiveReturnTarget::Program { .. }) {
                        ChannelRuntimeState::OnAir
                    } else {
                        ChannelRuntimeState::HealthyFallback
                    },
                    now_unix_ms,
                );
                Ok(LiveRunOutcome {
                    block_schedule: false,
                    transition: Some(DispatchTransition::LiveCompleted {
                        session_id: session.session_id,
                    }),
                })
            }
            TakeoverState::Returning => {
                store
                    .transition_takeover(
                        &session.session_id,
                        TakeoverState::Returning,
                        TakeoverState::Failed,
                        now_unix_ms,
                        Some("The channel worker restarted before the live return completed."),
                        "stationd-live-controller",
                        session.authority_revision,
                    )
                    .map_err(OrchestrationError::Storage)?;
                Err(OrchestrationError::LiveControl(
                    "The live return did not survive a worker restart.".into(),
                ))
            }
            TakeoverState::Completed | TakeoverState::Failed => {
                Err(OrchestrationError::LiveControl(
                    "Storage returned a terminal takeover as active.".into(),
                ))
            }
        }
    }

    pub fn run_once(
        &mut self,
        now_unix_ms: i64,
    ) -> Result<Option<DispatchTransition>, OrchestrationError> {
        self.supervisor
            .ensure_running()
            .map_err(OrchestrationError::Worker)?;
        let store = StationStore::open(&self.database_path).map_err(OrchestrationError::Storage)?;
        if matches!(self.output_proof, OutputProofState::Idle) {
            self.caption_proof_tick(&store, now_unix_ms)?;
        }
        if matches!(self.caption_proof, CaptionProofState::Idle) {
            self.output_proof_tick(&store, now_unix_ms)?;
        }
        if now_unix_ms >= self.next_cg_sync_at_unix_ms {
            self.next_cg_sync_at_unix_ms = now_unix_ms.saturating_add(CG_SYNC_INTERVAL_MS);
            expire_due_bulletins(&store, &self.channel_id, now_unix_ms)?;
            self.sync_cg(&store, now_unix_ms)?;
        }
        let live = self.reconcile_live(&store, now_unix_ms)?;
        if live.block_schedule {
            return Ok(live.transition);
        }
        let live_transition = live.transition;
        let missed_transition = self.retire_missed_dispatches(&store, now_unix_ms)?;
        let Some(job) = store
            .list_dispatch_jobs(&self.channel_id)
            .map_err(OrchestrationError::Storage)?
            .into_iter()
            .next()
        else {
            return Ok(live_transition.or(missed_transition));
        };
        let report_id = job.report.report_id.clone();
        let job_window = job
            .item
            .window_at(now_unix_ms)
            .map_err(|error| OrchestrationError::Storage(format!("{error:?}")))?;
        if matches!(
            job.report.dispatch_status,
            DispatchStatus::Queued | DispatchStatus::Acknowledged
        ) && job_window != ScheduleWindow::Ended
            && self.loaded_report_id.as_deref() != Some(job.report.report_id.as_str())
        {
            self.load_dispatch_job(&store, &job)?;
            return Ok(Some(DispatchTransition::Loaded { report_id }));
        }
        let transition = match (job.report.dispatch_status, job_window) {
            (DispatchStatus::Pending, ScheduleWindow::Future | ScheduleWindow::Active { .. }) => {
                self.load_dispatch_job(&store, &job)?;
                Some(DispatchTransition::Loaded { report_id })
            }
            (DispatchStatus::Queued, ScheduleWindow::Active { media_offset_ms }) => {
                self.clear_caption_cues_before_source_transition(&format!("take-{report_id}"))?;
                let event = self
                    .supervisor
                    .command(
                        &format!("take-{report_id}"),
                        ChannelCommand::TakeScheduledAsset {
                            report_id: report_id.clone(),
                            schedule_item_id: job.report.schedule_item_id.clone(),
                            schedule_item_revision: job.report.schedule_item_revision,
                            asset_id: job.asset.asset_id.clone(),
                            media_offset_ms,
                        },
                        self.command_timeout,
                    )
                    .map_err(OrchestrationError::Worker)?;
                let WorkerEvent::ScheduledAssetOnAir {
                    report_id: confirmed_report_id,
                    schedule_item_id,
                    schedule_item_revision,
                    asset_id,
                    media_offset_ms: confirmed_offset_ms,
                    worker_boot_id,
                    actual_on_air_at_unix_ms,
                } = &event.event
                else {
                    self.supervisor
                        .force_terminate()
                        .map_err(OrchestrationError::Worker)?;
                    return self.reject_worker_event(&store, &report_id, DispatchStatus::Queued);
                };
                if confirmed_report_id != &report_id
                    || schedule_item_id != &job.report.schedule_item_id
                    || *schedule_item_revision != job.report.schedule_item_revision
                    || asset_id != &job.asset.asset_id
                    || *confirmed_offset_ms != media_offset_ms
                {
                    self.supervisor
                        .force_terminate()
                        .map_err(OrchestrationError::Worker)?;
                    return Err(OrchestrationError::UnexpectedWorkerEvent);
                }
                let acknowledgement = match ScheduledTakeAcknowledgement::from_binding(
                    ScheduledTakeAcknowledgementBinding {
                        report_id: confirmed_report_id.clone(),
                        channel_id: event.channel_id.clone(),
                        schedule_item_id: schedule_item_id.clone(),
                        schedule_item_revision: *schedule_item_revision,
                        asset_sha256: asset_id.clone(),
                        aired_at_utc_ms: *actual_on_air_at_unix_ms,
                        media_offset_ms: *confirmed_offset_ms,
                        worker_boot_id: worker_boot_id.clone(),
                        worker_sequence: event.sequence,
                        worker_id: event.worker_id.clone(),
                    },
                ) {
                    Ok(acknowledgement) => acknowledgement,
                    Err(error) => {
                        self.supervisor
                            .force_terminate()
                            .map_err(OrchestrationError::Worker)?;
                        return Err(OrchestrationError::Storage(error));
                    }
                };
                let apply_outcome = match store
                    .apply_scheduled_take_acknowledgement(&acknowledgement)
                {
                    Ok(outcome) => outcome,
                    Err(error) => {
                        self.supervisor
                            .force_terminate()
                            .map_err(OrchestrationError::Worker)?;
                        return Err(OrchestrationError::Storage(format!(
                            "scheduled take acknowledgement was not persisted atomically: {error:?}"
                        )));
                    }
                };
                if apply_outcome == ScheduledTakeApplyOutcome::AppliedHistorical {
                    let event = self
                        .supervisor
                        .command(
                            &format!("complete-historical-{report_id}"),
                            ChannelCommand::ReturnToSchedule,
                            self.command_timeout.max(MEDIA_TEARDOWN_TIMEOUT),
                        )
                        .map_err(OrchestrationError::Worker)?;
                    if !matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, .. }
                        if source_kind == "fallback")
                    {
                        return self.reject_worker_event(
                            &store,
                            &report_id,
                            DispatchStatus::Acknowledged,
                        );
                    }
                    self.advance(
                        &store,
                        &report_id,
                        DispatchStatus::Acknowledged,
                        DispatchStatus::Completed,
                    )?;
                    self.on_air_asset_id = None;
                    self.caption_dispatch = None;
                    self.mark_state(ChannelRuntimeState::HealthyFallback, system_time_millis()?);
                    return Ok(Some(DispatchTransition::Completed { report_id }));
                }
                self.dispatch_due_caption_cues(&store, &job, media_offset_ms)?;
                self.mark_on_air_asset(job.asset.asset_id, *actual_on_air_at_unix_ms);
                Some(DispatchTransition::OnAir { report_id })
            }
            (DispatchStatus::Acknowledged, ScheduleWindow::Active { media_offset_ms })
                if self.on_air_asset_id.as_deref() != Some(job.asset.asset_id.as_str()) =>
            {
                self.clear_caption_cues_before_source_transition(&format!(
                    "reconcile-take-{report_id}"
                ))?;
                let event = self
                    .supervisor
                    .command(
                        &format!("reconcile-take-{report_id}"),
                        ChannelCommand::TakeAsset {
                            asset_id: job.asset.asset_id.clone(),
                            media_offset_ms,
                        },
                        self.command_timeout,
                    )
                    .map_err(OrchestrationError::Worker)?;
                if !matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, ref source_id }
                    if source_kind == "asset" && source_id == &job.asset.asset_id)
                {
                    return self.reject_worker_event(
                        &store,
                        &report_id,
                        DispatchStatus::Acknowledged,
                    );
                }
                self.dispatch_due_caption_cues(&store, &job, media_offset_ms)?;
                self.mark_on_air_asset(job.asset.asset_id, now_unix_ms);
                Some(DispatchTransition::OnAir { report_id })
            }
            (DispatchStatus::Acknowledged, ScheduleWindow::Active { media_offset_ms }) => {
                self.dispatch_due_caption_cues(&store, &job, media_offset_ms)?;
                None
            }
            (DispatchStatus::Acknowledged, ScheduleWindow::Ended) => {
                self.clear_caption_cues_before_source_transition(&format!("complete-{report_id}"))?;
                let event = self
                    .supervisor
                    .command(
                        &format!("complete-{report_id}"),
                        ChannelCommand::ReturnToSchedule,
                        self.command_timeout.max(MEDIA_TEARDOWN_TIMEOUT),
                    )
                    .map_err(OrchestrationError::Worker)?;
                if !matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, .. }
                    if source_kind == "fallback")
                {
                    return self.reject_worker_event(
                        &store,
                        &report_id,
                        DispatchStatus::Acknowledged,
                    );
                }
                self.advance(
                    &store,
                    &report_id,
                    DispatchStatus::Acknowledged,
                    DispatchStatus::Completed,
                )?;
                self.on_air_asset_id = None;
                self.caption_dispatch = None;
                self.mark_state(ChannelRuntimeState::HealthyFallback, now_unix_ms);
                Some(DispatchTransition::Completed { report_id })
            }
            (DispatchStatus::Queued | DispatchStatus::Acknowledged, ScheduleWindow::Future) => None,
            _ => None,
        };
        Ok(transition.or(live_transition).or(missed_transition))
    }

    fn sync_cg(
        &mut self,
        store: &StationStore,
        now_unix_ms: i64,
    ) -> Result<(), OrchestrationError> {
        let inputs = match store.load_cg_snapshot_inputs(&self.channel_id, now_unix_ms) {
            Ok(inputs) => inputs,
            Err(error) if cg_read_requires_clear(&error) => {
                return self.clear_cg_if_required(now_unix_ms);
            }
            Err(_) => return Ok(()),
        };
        let resolved = match inputs.resolve() {
            Ok(snapshot) => snapshot,
            Err(station_cg::CgSnapshotError::NoZones) => {
                return self.clear_cg_if_required(now_unix_ms);
            }
            Err(_) => return Ok(()),
        };
        let mut candidate = self.cg_state.clone();
        if candidate.accept(&resolved).is_err() {
            return Ok(());
        }
        let protocol = match candidate.resend_after_ready() {
            Some(protocol) => protocol,
            None => return Ok(()),
        };
        let event = match self.supervisor.command(
            &format!("cg-apply-{}-{}", protocol.board_revision, now_unix_ms),
            ChannelCommand::ApplyCgSnapshot {
                snapshot: protocol.clone(),
            },
            self.command_timeout,
        ) {
            Ok(event) => event,
            Err(error) => {
                return Err(self.retire_ambiguous_worker_session(
                    "apply CG snapshot",
                    format!("worker command failed before a bound acknowledgment: {error:?}"),
                ));
            }
        };
        if !matches!(
            event.event,
            WorkerEvent::CgApplied {
                ref board_id,
                board_revision
            } if board_id == &protocol.board_id && board_revision == protocol.board_revision
        ) {
            return Err(self.retire_ambiguous_worker_session(
                "apply CG snapshot",
                "worker returned an acknowledgement that did not bind the requested board revision",
            ));
        }
        candidate.mark_applied(&protocol.board_id, protocol.board_revision);
        self.cg_state = candidate;
        Ok(())
    }

    fn clear_cg_if_required(&mut self, now_unix_ms: i64) -> Result<(), OrchestrationError> {
        let mut candidate = self.cg_state.clone();
        candidate.invalidate_authority();
        if !candidate.clear_required() {
            self.cg_state = candidate;
            return Ok(());
        }
        let event = match self.supervisor.command(
            &format!("cg-clear-{now_unix_ms}"),
            ChannelCommand::ClearCgSnapshot,
            self.command_timeout,
        ) {
            Ok(event) => event,
            Err(error) => {
                return Err(self.retire_ambiguous_worker_session(
                    "clear CG snapshot",
                    format!("worker command failed before a bound acknowledgment: {error:?}"),
                ));
            }
        };
        if !matches!(event.event, WorkerEvent::CgCleared) {
            return Err(self.retire_ambiguous_worker_session(
                "clear CG snapshot",
                "worker returned an acknowledgement that did not confirm the requested clear",
            ));
        }
        candidate.mark_cleared();
        self.cg_state = candidate;
        Ok(())
    }

    fn retire_ambiguous_worker_session(
        &mut self,
        operation: impl Into<String>,
        cause: impl Into<String>,
    ) -> OrchestrationError {
        ambiguous_worker_command_failure(operation, cause, || self.supervisor.force_terminate())
    }

    pub fn shutdown(self, timeout: Duration) -> Result<(), OrchestrationError> {
        self.supervisor
            .shutdown("orchestrator-shutdown", timeout)
            .map(|_| ())
            .map_err(OrchestrationError::Worker)
    }

    pub fn runtime_observation(&self, now_unix_ms: i64) -> ChannelRuntimeObservation {
        ChannelRuntimeObservation {
            channel_id: self.channel_id.clone(),
            state: self.runtime_state,
            on_air_asset_id: self.on_air_asset_id.clone(),
            state_started_at_unix_ms: self.state_started_at_unix_ms,
            heartbeat_at_unix_ms: now_unix_ms,
            detail: self.caption_dispatch_detail.clone(),
        }
    }

    fn heartbeat_due(&self, now_unix_ms: i64) -> bool {
        now_unix_ms.saturating_sub(self.last_heartbeat_at_unix_ms) >= HEARTBEAT_INTERVAL_MS
    }

    fn mark_heartbeat(&mut self, now_unix_ms: i64) {
        self.last_heartbeat_at_unix_ms = now_unix_ms;
    }

    fn mark_state(&mut self, state: ChannelRuntimeState, now_unix_ms: i64) {
        if self.runtime_state != state {
            self.runtime_state = state;
            self.state_started_at_unix_ms = now_unix_ms;
        }
    }

    fn mark_on_air_asset(&mut self, asset_id: String, actual_started_at_unix_ms: i64) {
        self.state_started_at_unix_ms = on_air_source_started_at(
            self.runtime_state,
            self.on_air_asset_id.as_deref(),
            self.state_started_at_unix_ms,
            &asset_id,
            actual_started_at_unix_ms,
        );
        self.runtime_state = ChannelRuntimeState::OnAir;
        self.on_air_asset_id = Some(asset_id);
    }

    fn advance(
        &self,
        store: &StationStore,
        report_id: &str,
        expected: DispatchStatus,
        next: DispatchStatus,
    ) -> Result<(), OrchestrationError> {
        match store.advance_dispatch_status(report_id, expected, next) {
            Ok(true) => Ok(()),
            Ok(false) => Err(OrchestrationError::ConcurrentTransition),
            Err(message) => Err(OrchestrationError::Storage(message)),
        }
    }

    fn reject_worker_event(
        &self,
        store: &StationStore,
        report_id: &str,
        expected: DispatchStatus,
    ) -> Result<Option<DispatchTransition>, OrchestrationError> {
        self.advance(store, report_id, expected, DispatchStatus::Error)?;
        Err(OrchestrationError::UnexpectedWorkerEvent)
    }
}

fn on_air_source_started_at(
    current_state: ChannelRuntimeState,
    current_asset_id: Option<&str>,
    current_started_at_unix_ms: i64,
    next_asset_id: &str,
    actual_started_at_unix_ms: i64,
) -> i64 {
    if current_state != ChannelRuntimeState::OnAir || current_asset_id != Some(next_asset_id) {
        actual_started_at_unix_ms
    } else {
        current_started_at_unix_ms
    }
}

fn stalled_required_output(
    expectations: &[OutputExpectation],
    previous: Option<&[u64]>,
    current: &[u64],
) -> Option<usize> {
    expectations
        .iter()
        .enumerate()
        .find(|(index, expectation)| {
            expectation.required
                && previous.map_or_else(
                    || current[*index] == 0,
                    |prior| current[*index] <= prior[*index],
                )
        })
        .map(|(index, _)| index)
}

fn generate_live_arm_token() -> Result<LiveArmToken, OrchestrationError> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|error| {
        OrchestrationError::LiveControl(format!(
            "The operating system could not generate a live arm capability: {error:?}"
        ))
    })?;
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(&mut encoded, "{byte:02x}").expect("writing hexadecimal to a String cannot fail");
    }
    LiveArmToken::new(encoded).map_err(|message| OrchestrationError::LiveControl(message.into()))
}

pub fn run_until(
    database_path: &Path,
    worker_executable: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), OrchestrationError> {
    run_until_with_candidate_authority(database_path, worker_executable, stop, None)
}

pub fn run_until_with_candidate_authority(
    database_path: &Path,
    worker_executable: &Path,
    stop: Arc<AtomicBool>,
    candidate_authority: Option<Arc<dyn CandidateAuthority>>,
) -> Result<(), OrchestrationError> {
    run_until_with_candidate_authority_and_progress(
        database_path,
        worker_executable,
        stop,
        candidate_authority,
        || {},
    )
}

pub fn run_until_with_candidate_authority_and_progress<F>(
    database_path: &Path,
    worker_executable: &Path,
    stop: Arc<AtomicBool>,
    candidate_authority: Option<Arc<dyn CandidateAuthority>>,
    on_progress: F,
) -> Result<(), OrchestrationError>
where
    F: FnMut(),
{
    run_until_with_candidate_authority_and_liveness(
        database_path,
        worker_executable,
        stop,
        candidate_authority,
        || {},
        on_progress,
    )
}

pub fn run_until_with_candidate_authority_and_liveness<A, R>(
    database_path: &Path,
    worker_executable: &Path,
    stop: Arc<AtomicBool>,
    candidate_authority: Option<Arc<dyn CandidateAuthority>>,
    on_alive: A,
    on_reconciled: R,
) -> Result<(), OrchestrationError>
where
    A: FnMut(),
    R: FnMut(),
{
    run_until_with_proof_authorities_and_liveness(
        database_path,
        worker_executable,
        stop,
        candidate_authority,
        None,
        on_alive,
        on_reconciled,
    )
}

pub fn run_until_with_proof_authorities_and_liveness<A, R>(
    database_path: &Path,
    worker_executable: &Path,
    stop: Arc<AtomicBool>,
    candidate_authority: Option<Arc<dyn CandidateAuthority>>,
    caption_decoder_executable: Option<PathBuf>,
    mut on_alive: A,
    mut on_reconciled: R,
) -> Result<(), OrchestrationError>
where
    A: FnMut(),
    R: FnMut(),
{
    let journal_root = database_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("channel-journals");
    std::fs::create_dir_all(&journal_root).map_err(|error| {
        OrchestrationError::Storage(format!(
            "could not create channel journal directory: {error}"
        ))
    })?;
    let mut controllers: HashMap<String, ChannelController> = HashMap::new();
    let mut cg_states: HashMap<String, CgSnapshotState> = HashMap::new();
    let mut restart_attempts: HashMap<String, u32> = HashMap::new();
    let mut restart_after: HashMap<String, i64> = HashMap::new();
    while !stop.load(Ordering::Acquire) {
        let now = system_time_millis()?;
        let store = StationStore::open(database_path).map_err(OrchestrationError::Storage)?;
        let channels = store.list_channels().map_err(OrchestrationError::Storage)?;
        for channel in channels.iter().filter(|channel| !channel.enabled) {
            fail_active_takeover(
                &store,
                &channel.channel_id,
                now,
                &OrchestrationError::LiveControl(
                    "The channel was disabled while a live takeover was active.".into(),
                ),
            )?;
        }
        let enabled: HashSet<_> = channels
            .iter()
            .filter(|channel| channel.enabled)
            .map(|channel| channel.channel_id.clone())
            .collect();
        let disabled: Vec<_> = controllers
            .keys()
            .filter(|channel_id| !enabled.contains(*channel_id))
            .cloned()
            .collect();
        for channel_id in disabled {
            if let Some(controller) = controllers.remove(&channel_id) {
                let stopped = ChannelRuntimeObservation {
                    channel_id: channel_id.clone(),
                    state: ChannelRuntimeState::Stopped,
                    on_air_asset_id: None,
                    state_started_at_unix_ms: now,
                    heartbeat_at_unix_ms: now,
                    detail: Some("Channel output was disabled by configuration.".into()),
                };
                let _ = store.put_channel_runtime(&stopped);
                let _ = controller.shutdown(Duration::from_secs(3));
                on_alive();
            }
            restart_attempts.remove(&channel_id);
            restart_after.remove(&channel_id);
        }
        for channel in channels.iter().filter(|channel| channel.enabled) {
            if !controllers.contains_key(&channel.channel_id) {
                if restart_after
                    .get(&channel.channel_id)
                    .is_some_and(|retry_at| now < *retry_at)
                {
                    continue;
                }
                let journal = journal_root.join(format!("{}.tlj", channel.channel_id));
                let launch_result =
                    replay_scheduled_take_journal(&store, &journal, &channel.channel_id).and_then(
                        |()| {
                            ChannelController::launch(
                                worker_executable,
                                database_path,
                                &journal,
                                channel,
                                Duration::from_secs(7),
                            )
                        },
                    );
                on_alive();
                let controller = match launch_result {
                    Ok(controller) => controller,
                    Err(error) => {
                        fail_active_takeover(&store, &channel.channel_id, now, &error)?;
                        record_channel_failure(&store, &channel.channel_id, now, &error)?;
                        let attempts = restart_attempts
                            .entry(channel.channel_id.clone())
                            .or_default();
                        *attempts = attempts.saturating_add(1);
                        restart_after.insert(
                            channel.channel_id.clone(),
                            now.saturating_add(restart_backoff_ms(*attempts)),
                        );
                        continue;
                    }
                };
                let mut controller = controller;
                if let Some(authority) = candidate_authority.as_ref() {
                    controller.set_candidate_authority(Arc::clone(authority));
                }
                if let Some(executable) = caption_decoder_executable.as_ref() {
                    controller.set_caption_decoder_executable(executable.clone());
                }
                if let Some(state) = cg_states.remove(&channel.channel_id) {
                    controller.seed_cg_state(state);
                }
                let activated_at = system_time_millis()?;
                store
                    .put_channel_runtime(&controller.runtime_observation(activated_at))
                    .map_err(OrchestrationError::Storage)?;
                resolve_channel_failure(&store, &channel.channel_id, activated_at)?;
                restart_attempts.remove(&channel.channel_id);
                restart_after.remove(&channel.channel_id);
                controllers.insert(channel.channel_id.clone(), controller);
            }
        }
        let dispatch_now = system_time_millis()?;
        let mut failed = Vec::new();
        for (channel_id, controller) in &mut controllers {
            let heartbeat_due = controller.heartbeat_due(dispatch_now);
            let result = (|| {
                if heartbeat_due {
                    controller.poll_health(dispatch_now)?;
                }
                controller.run_once(dispatch_now)
            })();
            on_alive();
            match result {
                Ok(transition) => {
                    if transition.is_some() || heartbeat_due {
                        // Commands may block while media changes state. Persist
                        // the post-command observation with a fresh station
                        // clock value rather than the pre-dispatch loop time.
                        let observed_at = system_time_millis()?;
                        store
                            .put_channel_runtime(&controller.runtime_observation(observed_at))
                            .map_err(OrchestrationError::Storage)?;
                        controller.mark_heartbeat(observed_at);
                    }
                }
                Err(error) => {
                    // Stop uncertain output before any recovery bookkeeping can
                    // block on the same database that caused the failure.
                    controller
                        .supervisor
                        .force_terminate()
                        .map_err(OrchestrationError::Worker)?;
                    fail_active_takeover(&store, channel_id, dispatch_now, &error)?;
                    record_channel_failure(&store, channel_id, dispatch_now, &error)?;
                    let attempts = restart_attempts.entry(channel_id.clone()).or_default();
                    *attempts = attempts.saturating_add(1);
                    restart_after.insert(
                        channel_id.clone(),
                        dispatch_now.saturating_add(restart_backoff_ms(*attempts)),
                    );
                    failed.push(channel_id.clone());
                }
            }
        }
        for channel_id in failed {
            if let Some(controller) = controllers.remove(&channel_id) {
                cg_states.insert(channel_id, controller.cg_state());
            }
        }
        for channel in &channels {
            if !channel.enabled || !controllers.contains_key(&channel.channel_id) {
                let output_detail = if channel.enabled {
                    "Output loudness proof failed because the channel has no active controller; restore channel output and submit a new proof job."
                } else {
                    "Output loudness proof failed because the channel was disabled; enable channel output and submit a new proof job."
                };
                fail_output_proofs_without_controller(
                    &store,
                    database_path,
                    &channel.channel_id,
                    dispatch_now,
                    output_detail,
                )?;
                let caption_detail = if channel.enabled {
                    "Caption proof failed because the channel has no active controller; restore channel output and submit a new proof job."
                } else {
                    "Caption proof failed because the channel was disabled; enable channel output and submit a new proof job."
                };
                fail_caption_proofs_without_controller(
                    &store,
                    database_path,
                    &channel.channel_id,
                    dispatch_now,
                    caption_detail,
                )?;
            }
        }
        on_reconciled();
        std::thread::sleep(Duration::from_millis(50));
    }
    let stopped_at = system_time_millis()?;
    let store = StationStore::open(database_path).map_err(OrchestrationError::Storage)?;
    for (channel_id, controller) in controllers {
        let stopped = ChannelRuntimeObservation {
            channel_id,
            state: ChannelRuntimeState::Stopped,
            on_air_asset_id: None,
            state_started_at_unix_ms: stopped_at,
            heartbeat_at_unix_ms: stopped_at,
            detail: Some("Station service stopped cleanly.".into()),
        };
        store
            .put_channel_runtime(&stopped)
            .map_err(OrchestrationError::Storage)?;
        controller.shutdown(Duration::from_secs(3))?;
    }
    Ok(())
}

fn fail_active_takeover(
    store: &StationStore,
    channel_id: &str,
    observed_at_unix_ms: i64,
    error: &OrchestrationError,
) -> Result<(), OrchestrationError> {
    let Some(session) = store
        .read_active_takeover(channel_id)
        .map_err(OrchestrationError::Storage)?
    else {
        return Ok(());
    };
    let detail = operator_failure_detail(error);
    store
        .transition_takeover(
            &session.session_id,
            session.state,
            TakeoverState::Failed,
            observed_at_unix_ms,
            Some(&detail),
            "stationd-live-controller",
            session.authority_revision,
        )
        .map(|_| ())
        .map_err(OrchestrationError::Storage)
}

fn record_channel_failure(
    store: &StationStore,
    channel_id: &str,
    observed_at_unix_ms: i64,
    error: &OrchestrationError,
) -> Result<(), OrchestrationError> {
    let detail = operator_failure_detail(error);
    let state_started_at_unix_ms = store
        .read_channel_runtime(channel_id)
        .map_err(OrchestrationError::Storage)?
        .filter(|runtime| runtime.state == ChannelRuntimeState::Error)
        .map_or(observed_at_unix_ms, |runtime| {
            runtime.state_started_at_unix_ms
        });
    store
        .put_channel_runtime(&ChannelRuntimeObservation {
            channel_id: channel_id.into(),
            state: ChannelRuntimeState::Error,
            on_air_asset_id: None,
            state_started_at_unix_ms,
            heartbeat_at_unix_ms: observed_at_unix_ms,
            detail: Some(detail.clone()),
        })
        .map_err(OrchestrationError::Storage)?;
    let conditions = if matches!(
        error,
        OrchestrationError::RequiredOutputStalled { .. }
            | OrchestrationError::OutputGraphUnhealthy { .. }
            | OrchestrationError::LiveInputStalled { .. }
    ) {
        vec![
            (
                AlertConditionKind::OutputStall,
                "A required channel output stopped advancing.",
            ),
            (
                AlertConditionKind::OffAir,
                "Enabled channel output is not on verified fallback, scheduled media, or live input.",
            ),
        ]
    } else {
        vec![
            (
                AlertConditionKind::EncoderDeath,
                "Channel worker is not running.",
            ),
            (
                AlertConditionKind::OffAir,
                "Enabled channel output is not on verified fallback, scheduled media, or live input.",
            ),
        ]
    };
    for (condition, summary) in conditions {
        store
            .observe_alert_and_route(&AlertEvent {
                event_id: format!(
                    "alert-{observed_at_unix_ms}-{}",
                    ALERT_SEQUENCE.fetch_add(1, Ordering::Relaxed)
                ),
                condition,
                severity: AlertSeverity::Critical,
                state: AlertEventState::Firing,
                resource_ref: channel_id.into(),
                summary: summary.into(),
                detail: detail.clone(),
                source_section: "S8".into(),
                first_observed_at_unix_ms: observed_at_unix_ms,
                last_observed_at_unix_ms: observed_at_unix_ms,
                resolved_at_unix_ms: None,
                occurrence_count: 1,
                acknowledged_at_unix_ms: None,
                acknowledged_by: None,
            })
            .map_err(OrchestrationError::Storage)?;
    }
    Ok(())
}

fn operator_failure_detail(error: &OrchestrationError) -> String {
    match error {
        OrchestrationError::Worker(SupervisorError::ProcessExit(code)) => format!(
            "The channel worker exited unexpectedly{}; automatic restart attempts are active with bounded backoff.",
            code.map_or_else(String::new, |value| format!(" with code {value}"))
        ),
        OrchestrationError::Worker(SupervisorError::ProcessExitTimedOut) =>
            "The channel worker did not stop within its safety deadline; it was terminated and automatic restart attempts are active.".into(),
        OrchestrationError::Worker(_) =>
            "The channel worker stopped responding to its private control channel; automatic restart attempts are active with bounded backoff.".into(),
        OrchestrationError::Storage(_) =>
            "TownLight Station could not read or persist channel execution state; inspect storage health before returning this output to service.".into(),
        OrchestrationError::InvalidDestination(_) =>
            "The configured output destination is invalid; correct the channel destination to restart output.".into(),
        OrchestrationError::UnexpectedWorkerEvent =>
            "The channel worker returned an unexpected acknowledgment; output was stopped rather than accepting uncertain on-air state.".into(),
        OrchestrationError::ConcurrentTransition =>
            "The schedule changed during dispatch; TownLight Station stopped the transition rather than overwrite newer authority.".into(),
        OrchestrationError::RequiredOutputStalled { sink_id, label, detail } => format!(
            "Required output '{label}' ({sink_id}) stopped advancing; the worker was stopped and automatic restart attempts are active. {detail}"
        ),
        OrchestrationError::OutputGraphUnhealthy { detail } => format!(
            "The output graph reported an unhealthy transport; the worker was stopped and automatic restart attempts are active. {detail}"
        ),
        OrchestrationError::LiveInputStalled { detail } => format!(
            "The selected live input stopped delivering decoded audio or video; the worker was stopped and the takeover was failed safely. {detail}"
        ),
        OrchestrationError::LiveControl(detail) => format!(
            "The live takeover could not be reconciled safely; the worker was stopped rather than accept uncertain on-air authority. {detail}"
        ),
        OrchestrationError::CaptionAuthority(detail) => format!(
            "The scheduled caption authority was not acknowledged; the worker was stopped rather than claim uncertain caption output. {detail}"
        ),
        OrchestrationError::WorkerSessionAmbiguous {
            operation,
            cause,
            termination_failure,
        } => {
            let termination = termination_failure.as_ref().map_or_else(
                || "The ambiguous worker session was retired before restart.".to_string(),
                |detail| format!(
                    "The ambiguous worker session was retired and its kill-on-close containment was released, but worker exit was not confirmed by the bounded deadline: {detail}"
                ),
            );
            format!(
                "The worker could not safely confirm '{operation}', so TownLight Station stopped using that control session rather than reuse a protected-pipe sequence. {termination} Cause: {cause}"
            )
        }
        OrchestrationError::Clock(_) =>
            "The station clock could not provide a valid dispatch time; verify Windows time service before returning this output to service.".into(),
        OrchestrationError::InvalidCgSnapshot(detail) => {
            format!("The CG snapshot exceeded the worker protocol contract: {detail}")
        }
    }
}

fn resolve_channel_failure(
    store: &StationStore,
    channel_id: &str,
    resolved_at_unix_ms: i64,
) -> Result<(), OrchestrationError> {
    for condition in [
        AlertConditionKind::EncoderDeath,
        AlertConditionKind::OutputStall,
        AlertConditionKind::OffAir,
    ] {
        store
            .resolve_alert(condition, channel_id, resolved_at_unix_ms)
            .map_err(OrchestrationError::Storage)?;
    }
    Ok(())
}

fn restart_backoff_ms(attempt: u32) -> i64 {
    match attempt {
        0 | 1 => 1_000,
        2 => 5_000,
        3 => 15_000,
        _ => 60_000,
    }
}

fn system_time_millis() -> Result<i64, OrchestrationError> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| OrchestrationError::Clock(error.to_string()))?
        .as_millis();
    i64::try_from(millis).map_err(|_| OrchestrationError::Clock("system time exceeds i64".into()))
}

/// Replays durable worker confirmations before a replacement worker is
/// launched.  The journal is the worker's crash boundary: a confirmation is
/// written and flushed before it is emitted over IPC, so a station crash can
/// only safely recover it from this file.  This function deliberately does
/// not create a controller or write a runtime observation.  Storage owns the
/// single atomic receipt/dispatch/runtime transition and decides whether a
/// first-time late acknowledgement is historical-only; exact replays are
/// idempotent there.
fn replay_scheduled_take_journal(
    store: &StationStore,
    journal_path: &Path,
    channel_id: &str,
) -> Result<(), OrchestrationError> {
    let expected_worker_id = format!("station-worker-{channel_id}");
    let inspection = inspect_journal(journal_path).map_err(|error| {
        OrchestrationError::Storage(format!(
            "could not read durable channel journal before worker launch: {error:?}"
        ))
    })?;
    match (
        inspection.worker_id.as_deref(),
        inspection.channel_id.as_deref(),
    ) {
        (None, None) => {}
        (Some(worker_id), Some(journal_channel_id))
            if worker_id == expected_worker_id && journal_channel_id == channel_id => {}
        _ => {
            return Err(OrchestrationError::Storage(
                "durable channel journal identity does not match the configured channel worker"
                    .into(),
            ));
        }
    }

    for envelope in inspection.retained_events {
        if envelope.worker_id != expected_worker_id || envelope.channel_id != channel_id {
            return Err(OrchestrationError::Storage(
                "durable channel journal identity does not match the configured channel worker"
                    .into(),
            ));
        }
        let WorkerEvent::ScheduledAssetOnAir {
            report_id,
            schedule_item_id,
            schedule_item_revision,
            asset_id,
            media_offset_ms,
            worker_boot_id,
            actual_on_air_at_unix_ms,
        } = envelope.event
        else {
            continue;
        };
        let acknowledgement =
            ScheduledTakeAcknowledgement::from_binding(ScheduledTakeAcknowledgementBinding {
                report_id,
                channel_id: channel_id.into(),
                schedule_item_id,
                schedule_item_revision,
                asset_sha256: asset_id,
                aired_at_utc_ms: actual_on_air_at_unix_ms,
                media_offset_ms,
                worker_boot_id,
                worker_sequence: envelope.sequence,
                worker_id: expected_worker_id.clone(),
            })
            .map_err(OrchestrationError::Storage)?;
        // The result is intentionally not projected into controller state.
        // `Applied`, `AlreadyApplied`, and any future historical-only outcome
        // are all durably resolved by the authority before launch continues.
        let _ = store
            .apply_scheduled_take_acknowledgement(&acknowledgement)
            .map_err(|error| {
                OrchestrationError::Storage(format!(
                    "durable scheduled take acknowledgement could not be replayed: {error:?}"
                ))
            })?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        CaptionCaptureTiming, CgSnapshotState, OrchestrationError, OutputExpectation,
        caption_capture_timing, capture_duration_for_sample, cleanup_orphan_output_proof_artifacts,
        due_caption_cues, expire_due_bulletins, generate_live_arm_token, hash_capture,
        is_abort_confirmation, lease_renewal_due, managed_cg_image_root,
        observe_caption_cue_misses, on_air_source_started_at, promote_caption_proof_artifact,
        proof_artifact_path, proof_bindings_match, ready_duration_is_bounded,
        resolve_caption_cue_miss_after_admission, resolved_snapshot_to_protocol,
        stalled_required_output,
    };
    use sha2::{Digest, Sha256};
    use station_captions::{
        AssetCaptionCue, AssetCaptionTrack, AssetCaptionTrackWrite, CaptionEgressProofJob,
        CaptionEgressProofJobState, CaptionTrackSourceKind, canonical_caption_text_sha256,
    };
    use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
    use station_cg::{
        CgAuditEvent, CgAuditEventKind, CgBoard, CgBulletin, CgBulletinContent, CgBulletinState,
        CgZoneConfig,
    };
    use station_cg::{
        CgContentSource, CgRect, CgResolvedContent, CgResolvedSnapshot, CgResolvedZone,
        CgTemplateKind, CgZoneKind,
    };
    use station_domain::CaptionCueMissReason;
    use station_media_journal::JournalWriter;
    use station_media_protocol::{
        ChannelCommand, CommandEnvelope, PROTOCOL_VERSION, WorkerEvent, WorkerEventEnvelope,
        decode_command_frame, encode_command_frame,
    };
    use station_schedule::{
        AssetReadiness, ChannelConfiguration, DispatchStatus, MediaAsset, OutputProfileId,
        OutputSinkConfiguration, ScheduleItem, ScheduleState,
    };
    use station_schedule_automation::{
        AssetPickStrategy, AssetPredicate, DaypartBlock, LocalDate, RecurrenceTimeZone,
        RecurringRule, SavedSearch, SavedSearchOrder, SavedSearchQuery, SourceEvidenceAuthority,
        SourceEvidenceKind, SourceEvidenceReceipt, SourceEvidenceVerifier,
    };
    use station_storage::{
        ChannelCaptionMode, OutputLoudnessProofJob, OutputLoudnessProofJobState, ScheduleItemWrite,
        ScheduleMaterializationApplyOutcome, ScheduleMaterializationOutcome, StationStore,
    };
    use std::collections::{BTreeMap, HashSet};
    use std::path::PathBuf;
    use std::sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    };
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

    const CHANNEL_ID: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const OTHER_CHANNEL_ID: &str = "9d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const BOARD_ID: &str = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";
    const ZONE_ID: &str = "8f4f0f70-5c30-48c6-819a-fb22b34353c5";
    const BULLETIN_ID: &str = "597f52ae-f16c-4ff8-ab32-60379cd950c4";
    const OTHER_BULLETIN_ID: &str = "697f52ae-f16c-48f8-ab32-60379cd950c4";
    const OUTPUT_PROOF_JOB_ID: &str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

    #[test]
    fn back_to_back_on_air_asset_uses_the_new_assets_actual_start() {
        assert_eq!(
            on_air_source_started_at(
                station_domain::ChannelRuntimeState::OnAir,
                Some("asset-a"),
                1_000,
                "asset-b",
                2_000,
            ),
            2_000
        );
        assert_eq!(
            on_air_source_started_at(
                station_domain::ChannelRuntimeState::OnAir,
                Some("asset-b"),
                2_000,
                "asset-b",
                3_000,
            ),
            2_000
        );
    }
    const CAPTION_PROOF_JOB_ID: &str = "caaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    const OUTPUT_SINK_ID: &str = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
    const SOURCE_COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

    fn database_path(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("townlight-orchestration-{label}-{nonce}.db"))
    }

    #[test]
    fn managed_cg_image_root_is_anchored_to_database_parent() {
        let database = PathBuf::from(r"C:\ProgramData\TownLight Station\station.db");
        assert_eq!(
            managed_cg_image_root(&database),
            PathBuf::from(r"C:\ProgramData\TownLight Station\cg-images")
        );
    }

    #[test]
    fn ambiguous_cg_command_failure_retires_the_session_and_preserves_failure_context() {
        let retired = Arc::new(AtomicBool::new(false));
        let observed = Arc::clone(&retired);
        let error = super::ambiguous_worker_command_failure(
            "apply CG snapshot",
            "worker command timed out before acknowledgment",
            move || {
                observed.store(true, Ordering::SeqCst);
                Ok(())
            },
        );

        assert!(retired.load(Ordering::SeqCst));
        assert!(matches!(
            error,
            OrchestrationError::WorkerSessionAmbiguous {
                ref operation,
                ref cause,
                termination_failure: None,
            } if operation == "apply CG snapshot"
                && cause.contains("timed out")
        ));
    }

    #[test]
    fn ambiguous_cg_command_failure_records_termination_failure_without_resuming_session() {
        let retired = Arc::new(AtomicBool::new(false));
        let observed = Arc::clone(&retired);
        let error = super::ambiguous_worker_command_failure(
            "clear CG snapshot",
            "worker returned a mismatched clear acknowledgment",
            move || {
                observed.store(true, Ordering::SeqCst);
                Err(station_runtime::SupervisorError::AlreadyStopped)
            },
        );

        assert!(retired.load(Ordering::SeqCst));
        assert!(matches!(
            error,
            OrchestrationError::WorkerSessionAmbiguous {
                ref operation,
                ref cause,
                termination_failure: Some(_),
            } if operation == "clear CG snapshot"
                && cause.contains("mismatched")
        ));
    }

    #[test]
    fn caption_capture_timing_waits_starts_once_and_fails_when_late() {
        assert_eq!(
            caption_capture_timing(100_000, 10_000, 109_999),
            CaptionCaptureTiming::Waiting
        );
        assert_eq!(
            caption_capture_timing(100_000, 10_000, 110_000),
            CaptionCaptureTiming::Due
        );
        assert_eq!(
            caption_capture_timing(100_000, 10_000, 110_500),
            CaptionCaptureTiming::Due
        );
        assert_eq!(
            caption_capture_timing(100_000, 10_000, 110_501),
            CaptionCaptureTiming::Missed
        );
        assert_eq!(
            caption_capture_timing(i64::MAX, 1, i64::MAX),
            CaptionCaptureTiming::Missed
        );
    }

    #[test]
    fn caption_artifact_promotion_moves_only_the_exact_owned_capture() {
        let root = database_path("caption-promote").with_extension("root");
        let database = root.join("station.db");
        let worker_root = root.join("output-proofs");
        std::fs::create_dir_all(&worker_root).unwrap();
        std::fs::write(
            worker_root.join(format!("{CAPTION_PROOF_JOB_ID}.ts")),
            b"bounded caption transport",
        )
        .unwrap();
        std::fs::write(worker_root.join("claude-proof.ts"), b"foreign artifact").unwrap();

        let (path, relative) = promote_caption_proof_artifact(
            &database,
            CAPTION_PROOF_JOB_ID,
            &format!("{CAPTION_PROOF_JOB_ID}.ts"),
        )
        .unwrap();

        assert_eq!(
            relative,
            format!("caption-proofs/{CAPTION_PROOF_JOB_ID}.ts")
        );
        assert_eq!(std::fs::read(path).unwrap(), b"bounded caption transport");
        assert!(worker_root.join("claude-proof.ts").exists());
        assert!(
            promote_caption_proof_artifact(&database, CAPTION_PROOF_JOB_ID, "claude-proof.ts")
                .is_err()
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn orchestration_does_not_report_progress_before_a_successful_reconcile() {
        let root = database_path("progress-failure").with_extension("root");
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        std::fs::create_dir_all(&database).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let progress = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&progress);

        let result = super::run_until_with_candidate_authority_and_progress(
            &database,
            PathBuf::from("missing-worker.exe").as_path(),
            stop,
            None,
            move || {
                observed.fetch_add(1, Ordering::AcqRel);
            },
        );

        assert!(result.is_err());
        assert_eq!(progress.load(Ordering::Acquire), 0);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn orchestration_reports_progress_after_a_successful_empty_reconcile() {
        let database = database_path("progress-success");
        let root = database.parent().unwrap().to_path_buf();
        let stop = Arc::new(AtomicBool::new(false));
        let callback_stop = Arc::clone(&stop);
        let progress = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&progress);

        super::run_until_with_candidate_authority_and_progress(
            &database,
            PathBuf::from("missing-worker.exe").as_path(),
            stop,
            None,
            move || {
                observed.fetch_add(1, Ordering::AcqRel);
                callback_stop.store(true, Ordering::Release);
            },
        )
        .unwrap();

        assert_eq!(progress.load(Ordering::Acquire), 1);
        let _ = std::fs::remove_file(database);
        let _ = std::fs::remove_dir(root.join("channel-journals"));
    }

    #[test]
    fn disabled_channel_reconcile_fails_expired_output_proof_and_removes_artifacts() {
        let root = database_path("disabled-output-proof").with_extension("root");
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        let store = StationStore::open(&database).unwrap();
        let channel = output_proof_channel(false);
        store.put_channel(&channel).unwrap();
        create_claimed_output_proof(&store, &database, 2_000, 1_000);
        assert_eq!(
            store.recover_expired_output_loudness_jobs(3_000).unwrap(),
            1
        );
        assert_eq!(
            store
                .read_output_loudness_job(OUTPUT_PROOF_JOB_ID)
                .unwrap()
                .unwrap()
                .state,
            OutputLoudnessProofJobState::Pending
        );
        let proof_root = super::managed_output_proof_root(&database);
        std::fs::create_dir_all(&proof_root).unwrap();
        std::fs::write(
            proof_root.join(format!("{OUTPUT_PROOF_JOB_ID}.ts")),
            b"orphan-final",
        )
        .unwrap();
        std::fs::write(
            proof_root.join(format!("{OUTPUT_PROOF_JOB_ID}.ts.part")),
            b"orphan-part",
        )
        .unwrap();
        drop(store);

        reconcile_once(&database);

        let store = StationStore::open(&database).unwrap();
        let job = store
            .read_output_loudness_job(OUTPUT_PROOF_JOB_ID)
            .unwrap()
            .unwrap();
        assert_eq!(job.state, OutputLoudnessProofJobState::Failed);
        assert!(
            job.failure_detail
                .as_deref()
                .is_some_and(|detail| detail.contains("disabled"))
        );
        assert!(
            !proof_root
                .join(format!("{OUTPUT_PROOF_JOB_ID}.ts"))
                .exists()
        );
        assert!(
            !proof_root
                .join(format!("{OUTPUT_PROOF_JOB_ID}.ts.part"))
                .exists()
        );
        assert_eq!(
            store
                .recover_expired_output_loudness_jobs(i64::MAX)
                .unwrap(),
            0
        );
        assert_eq!(
            store
                .read_output_loudness_job(OUTPUT_PROOF_JOB_ID)
                .unwrap()
                .unwrap()
                .state,
            OutputLoudnessProofJobState::Failed
        );
        drop(store);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn disabled_channel_reconcile_fails_caption_proof_without_touching_foreign_artifacts() {
        let root = database_path("disabled-caption-proof").with_extension("root");
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        let store = StationStore::open(&database).unwrap();
        store.put_channel(&output_proof_channel(false)).unwrap();
        create_caption_proof_job(&store, &root);
        let caption_root = root.join("caption-proofs");
        std::fs::create_dir_all(&caption_root).unwrap();
        std::fs::write(
            caption_root.join(format!("{CAPTION_PROOF_JOB_ID}.ts")),
            b"owned caption artifact",
        )
        .unwrap();
        std::fs::write(caption_root.join("claude-proof.ts"), b"foreign artifact").unwrap();
        drop(store);

        reconcile_once(&database);

        let store = StationStore::open(&database).unwrap();
        let job = store
            .read_caption_egress_job(CAPTION_PROOF_JOB_ID)
            .unwrap()
            .unwrap();
        assert_eq!(job.state, CaptionEgressProofJobState::Failed);
        assert!(
            job.failure_detail
                .as_deref()
                .is_some_and(|detail| detail.contains("disabled"))
        );
        assert!(
            !caption_root
                .join(format!("{CAPTION_PROOF_JOB_ID}.ts"))
                .exists()
        );
        assert!(caption_root.join("claude-proof.ts").exists());
        drop(store);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn enabled_channel_without_a_controller_fails_its_active_output_proof() {
        let root = database_path("controllerless-output-proof").with_extension("root");
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        let store = StationStore::open(&database).unwrap();
        let channel = output_proof_channel(true);
        store.put_channel(&channel).unwrap();
        create_claimed_output_proof(
            &store,
            &database,
            super::system_time_millis().unwrap(),
            300_000,
        );
        let proof_root = super::managed_output_proof_root(&database);
        std::fs::write(&proof_root, b"not-a-directory").unwrap();
        drop(store);

        reconcile_once(&database);

        let store = StationStore::open(&database).unwrap();
        let job = store
            .read_output_loudness_job(OUTPUT_PROOF_JOB_ID)
            .unwrap()
            .unwrap();
        assert_eq!(job.state, OutputLoudnessProofJobState::Failed);
        assert!(
            job.failure_detail
                .as_deref()
                .is_some_and(|detail| detail.contains("no active controller"))
        );
        assert!(
            job.failure_detail
                .as_deref()
                .is_some_and(|detail| detail.contains("artifact cleanup also failed"))
        );
        drop(store);
        std::fs::remove_dir_all(root).unwrap();
    }

    fn output_proof_channel(enabled: bool) -> ChannelConfiguration {
        ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Output proof reconcile".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                OUTPUT_SINK_ID,
                "Cable output",
                "127.0.0.1:5500",
                -24.0,
            )],
            enabled,
        }
    }

    fn create_claimed_output_proof(
        store: &StationStore,
        database: &std::path::Path,
        claimed_at_unix_ms: i64,
        lease_duration_ms: i64,
    ) {
        let configuration_hash = store
            .current_channel_output_configuration_hash(CHANNEL_ID)
            .unwrap();
        assert!(
            store
                .create_output_loudness_job(&OutputLoudnessProofJob {
                    job_id: OUTPUT_PROOF_JOB_ID.into(),
                    channel_id: CHANNEL_ID.into(),
                    target_group: "cable".into(),
                    sink_id: OUTPUT_SINK_ID.into(),
                    target_lufs_milli: -24_000,
                    candidate_version: "0.1.0".into(),
                    source_commit: Some(SOURCE_COMMIT.into()),
                    configuration_hash,
                    sample_duration_ms: 3_000,
                    max_capture_bytes: 8 * 1024 * 1024,
                    created_at_unix_ms: 1_000,
                })
                .unwrap()
        );
        store
            .claim_output_loudness_job(OUTPUT_PROOF_JOB_ID, claimed_at_unix_ms, lease_duration_ms)
            .unwrap()
            .unwrap();
        assert_eq!(
            super::managed_output_proof_root(database),
            database.parent().unwrap().join("output-proofs")
        );
    }

    fn create_caption_proof_job(store: &StationStore, root: &std::path::Path) {
        let asset_id = "d".repeat(64);
        let media = root.join("caption-asset.ts");
        std::fs::write(&media, b"caption asset fixture").unwrap();
        store
            .put_media_asset(&MediaAsset {
                asset_id: asset_id.clone(),
                media_path: media.to_string_lossy().into_owned(),
                duration_ms: 60_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        let item_id = "daaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
        let report_id = "caption-orchestration-report";
        store
            .put_schedule_item(&ScheduleItem {
                item_id: item_id.into(),
                revision: 1,
                channel_id: CHANNEL_ID.into(),
                asset_id: asset_id.clone(),
                title: "Caption proof fixture".into(),
                starts_at_unix_ms: 100_000,
                duration_ms: 60_000,
                state: ScheduleState::Draft,
            })
            .unwrap();
        store
            .commit_schedule(
                report_id,
                "caption-orchestration-plan",
                item_id,
                "setup-admin",
                90_000,
                "Caption proof fixture",
            )
            .unwrap();
        let policy = store
            .write_channel_caption_policy(
                CHANNEL_ID,
                ChannelCaptionMode::Cea708,
                1,
                "en-US",
                0,
                "setup-admin",
                90_001,
                "eaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            )
            .unwrap();
        let track = store
            .write_asset_caption_track(&AssetCaptionTrackWrite {
                track_id: "faaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
                asset_id: asset_id.clone(),
                language: "en-US".into(),
                service_number: 1,
                source_kind: CaptionTrackSourceKind::External,
                label: "Reviewed English".into(),
                cues: vec![AssetCaptionCue {
                    cue_id: "abaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
                    start_ms: 1_000,
                    duration_ms: 2_000,
                    text: "Town Council".into(),
                }],
                expected_revision: 0,
                actor: "setup-admin".into(),
                occurred_at_unix_ms: 90_002,
                audit_id: "acaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
            })
            .unwrap();
        let expected_text_sha256 = canonical_caption_text_sha256(&["Town Council".into()]).unwrap();
        let configuration_hash = store
            .current_channel_output_configuration_hash(CHANNEL_ID)
            .unwrap();
        assert!(
            store
                .create_caption_egress_job(&CaptionEgressProofJob {
                    job_id: CAPTION_PROOF_JOB_ID.into(),
                    channel_id: CHANNEL_ID.into(),
                    sink_id: OUTPUT_SINK_ID.into(),
                    report_id: report_id.into(),
                    asset_id,
                    track_id: track.track_id,
                    track_revision: track.revision,
                    cues_sha256: track.cues_sha256,
                    caption_policy_revision: policy.revision,
                    language: policy.language,
                    service_number: policy.service_number,
                    candidate_version: "0.1.0".into(),
                    source_commit: SOURCE_COMMIT.into(),
                    configuration_hash,
                    capture_started_asset_offset_ms: 0,
                    capture_duration_ms: 3_000,
                    max_capture_bytes: 8 * 1024 * 1024,
                    expected_cue_count: 1,
                    expected_text_sha256,
                    created_at_unix_ms: 95_000,
                })
                .unwrap()
        );
    }

    fn reconcile_once(database: &std::path::Path) {
        let stop = Arc::new(AtomicBool::new(false));
        let callback_stop = Arc::clone(&stop);
        super::run_until_with_candidate_authority_and_progress(
            database,
            PathBuf::from("missing-worker.exe").as_path(),
            stop,
            None,
            move || callback_stop.store(true, Ordering::Release),
        )
        .unwrap();
    }

    fn scheduled_take_replay_fixture(
        label: &str,
        historical: bool,
    ) -> (PathBuf, PathBuf, StationStore, String, i64) {
        let root = database_path(label).with_extension("station-root");
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        let media = database.with_extension("ts");
        std::fs::write(&media, b"scheduled replay fixture").unwrap();
        let store = StationStore::open(&database).unwrap();
        let now = super::system_time_millis().unwrap();
        let (starts_at_unix_ms, actual_on_air_at_unix_ms) = if historical {
            (now - 62_000, now - 61_000)
        } else {
            (now - 2_000, now - 1_000)
        };
        let asset_id = "a".repeat(64);
        let item_id = "faaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
        let report_id = format!("replay-{label}");
        store
            .put_channel(&ChannelConfiguration {
                channel_id: CHANNEL_ID.into(),
                display_name: "Crash replay channel".into(),
                output_profile: OutputProfileId::Proof360p30,
                output_sinks: vec![OutputSinkConfiguration::required_udp(
                    "f103968a-640b-42dc-8084-3fe9e847ff7f",
                    "Crash replay output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
        store
            .put_media_asset(&MediaAsset {
                asset_id: asset_id.clone(),
                media_path: media.to_string_lossy().into_owned(),
                duration_ms: 60_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        store
            .put_schedule_item(&ScheduleItem {
                item_id: item_id.into(),
                revision: 1,
                channel_id: CHANNEL_ID.into(),
                asset_id: asset_id.clone(),
                title: "Crash-replay scheduled item".into(),
                starts_at_unix_ms,
                duration_ms: 60_000,
                state: ScheduleState::Draft,
            })
            .unwrap();
        let report = store
            .commit_schedule(
                &report_id,
                "crash-replay-plan",
                item_id,
                "test-harness",
                now - 3_000,
                "Crash replay fixture",
            )
            .unwrap();
        assert!(
            store
                .advance_dispatch_status(
                    &report_id,
                    station_schedule::DispatchStatus::Pending,
                    station_schedule::DispatchStatus::Queued,
                )
                .unwrap()
        );
        let journal = root
            .join("channel-journals")
            .join(format!("{CHANNEL_ID}.tlj"));
        std::fs::create_dir_all(journal.parent().unwrap()).unwrap();
        let worker_id = format!("station-worker-{CHANNEL_ID}");
        let mut writer = JournalWriter::open(&journal, &worker_id, CHANNEL_ID).unwrap();
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id,
                channel_id: CHANNEL_ID.into(),
                sequence: 1,
                event: WorkerEvent::ScheduledAssetOnAir {
                    report_id: report.report_id,
                    schedule_item_id: report.schedule_item_id,
                    schedule_item_revision: report.schedule_item_revision,
                    asset_id,
                    media_offset_ms: 1_000,
                    worker_boot_id: "11111111-1111-4111-8111-111111111111".into(),
                    actual_on_air_at_unix_ms,
                },
            })
            .unwrap();
        drop(writer);
        (database, journal, store, report_id, now)
    }

    struct S19TestEvidenceAuthority {
        observed_at_utc_ms: i64,
        sequence: AtomicU64,
    }

    impl S19TestEvidenceAuthority {
        fn new(observed_at_utc_ms: i64) -> Self {
            Self {
                observed_at_utc_ms,
                sequence: AtomicU64::new(1),
            }
        }

        fn attestation(
            kind: SourceEvidenceKind,
            receipt_id: &str,
            scope_sha256: &str,
            payload_sha256: &str,
        ) -> String {
            let mut digest = Sha256::new();
            digest.update(match kind {
                SourceEvidenceKind::CandidateProjection => b"candidate".as_slice(),
                SourceEvidenceKind::PriorAirings => b"prior".as_slice(),
            });
            for value in [receipt_id, scope_sha256, payload_sha256] {
                digest.update((value.len() as u64).to_le_bytes());
                digest.update(value.as_bytes());
            }
            format!("{:x}", digest.finalize())
        }
    }

    impl SourceEvidenceVerifier for S19TestEvidenceAuthority {
        fn verify(
            &self,
            kind: SourceEvidenceKind,
            receipt: &SourceEvidenceReceipt,
            scope_sha256: &str,
            payload_sha256: &str,
            observed_at_utc_ms: i64,
        ) -> Result<(), String> {
            if receipt.issuer_id != "10000000-0000-4000-8000-000000000001"
                || receipt.scope_sha256 != scope_sha256
                || receipt.issued_at_utc_ms > observed_at_utc_ms
                || receipt.expires_at_utc_ms <= observed_at_utc_ms
                || receipt.attestation_sha256
                    != Self::attestation(kind, &receipt.receipt_id, scope_sha256, payload_sha256)
            {
                return Err("S19 test evidence failed exact authentication".into());
            }
            Ok(())
        }
    }

    impl SourceEvidenceAuthority for S19TestEvidenceAuthority {
        fn observed_at_utc_ms(&self) -> Result<i64, String> {
            Ok(self.observed_at_utc_ms)
        }

        fn issue_source_evidence(
            &self,
            kind: SourceEvidenceKind,
            scope_sha256: &str,
            payload_sha256: &str,
            lifetime_ms: i64,
        ) -> Result<SourceEvidenceReceipt, String> {
            let sequence = self.sequence.fetch_add(1, Ordering::Relaxed);
            let receipt_id = format!("10000000-0000-4000-8000-{sequence:012}");
            Ok(SourceEvidenceReceipt {
                issuer_id: "10000000-0000-4000-8000-000000000001".into(),
                receipt_id: receipt_id.clone(),
                issued_at_utc_ms: self.observed_at_utc_ms,
                expires_at_utc_ms: self.observed_at_utc_ms + lifetime_ms,
                scope_sha256: scope_sha256.into(),
                attestation_sha256: Self::attestation(
                    kind,
                    &receipt_id,
                    scope_sha256,
                    payload_sha256,
                ),
            })
        }
    }

    #[test]
    fn s19_draft_identity_survives_edit_then_s4_v9_worker_confirmation_and_durable_as_run() {
        const SEARCH_ID: &str = "20000000-0000-4000-8000-000000000001";
        const BLOCK_ID: &str = "20000000-0000-4000-8000-000000000002";
        const RULE_ID: &str = "20000000-0000-4000-8000-000000000003";
        const OWNER_ID: &str = "20000000-0000-4000-8000-000000000004";
        const ASSET_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        const REPORT_ID: &str = "s19-s4-v9-as-run-report";
        const PLAN_ID: &str = "s19-s4-handoff-plan";
        const WORKER_BOOT_ID: &str = "30000000-0000-4000-8000-000000000001";

        let root = database_path("s19-s4-v9-as-run").with_extension("station-root");
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        let media = root.join("council.ts");
        std::fs::write(&media, b"S19 generated schedule media").unwrap();
        let store = StationStore::open(&database).unwrap();
        store
            .put_channel(&ChannelConfiguration {
                channel_id: CHANNEL_ID.into(),
                display_name: "S19 handoff proof".into(),
                output_profile: OutputProfileId::Proof360p30,
                output_sinks: vec![OutputSinkConfiguration::required_udp(
                    OUTPUT_SINK_ID,
                    "S19 proof output",
                    "127.0.0.1:5500",
                    -24.0,
                )],
                enabled: true,
            })
            .unwrap();
        store
            .put_media_asset(&MediaAsset {
                asset_id: ASSET_ID.into(),
                media_path: media.to_string_lossy().into_owned(),
                duration_ms: 60_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        let catalog_draft = CatalogAssetRecord {
            asset_sha256: ASSET_ID.into(),
            title: "Council meeting".into(),
            meeting_body: Some("City Council".into()),
            editorial_state: CatalogEditorialState::Draft,
            published_at_utc_ms: None,
            revision: 1,
        };
        store
            .write_catalog_asset(ASSET_ID, &catalog_draft, None, "catalog", 1)
            .unwrap();
        store
            .write_catalog_asset(
                ASSET_ID,
                &CatalogAssetRecord {
                    editorial_state: CatalogEditorialState::Published,
                    published_at_utc_ms: Some(2),
                    revision: 2,
                    ..catalog_draft
                },
                Some(1),
                "catalog",
                2,
            )
            .unwrap();
        store
            .write_saved_search(
                &SavedSearch {
                    search_id: SEARCH_ID.into(),
                    name: "Council programs".into(),
                    owner_operator_id: OWNER_ID.into(),
                    created_at_utc_ms: 1,
                    query: SavedSearchQuery {
                        all_of: vec![AssetPredicate::TitleContains("Council".into())],
                        any_of: vec![],
                        order_by: SavedSearchOrder::PublishedNewest,
                        limit: 8,
                    },
                    revision: 1,
                },
                None,
                OWNER_ID,
                3,
            )
            .unwrap();
        store
            .write_daypart_block(
                &DaypartBlock {
                    block_id: BLOCK_ID.into(),
                    channel_id: CHANNEL_ID.into(),
                    label: "Daily civic".into(),
                    starts_local_minute: 0,
                    ends_local_minute: 1_440,
                    weekday_mask: 0b0111_1111,
                    season_starts_on: None,
                    season_ends_on: None,
                    allow_rule_ids: vec![RULE_ID.into()],
                    revision: 1,
                },
                None,
                OWNER_ID,
                4,
            )
            .unwrap();
        store
            .write_recurring_rule(
                &RecurringRule {
                    rule_id: RULE_ID.into(),
                    channel_id: CHANNEL_ID.into(),
                    saved_search_id: SEARCH_ID.into(),
                    saved_search_revision: 1,
                    pick_strategy: AssetPickStrategy::Top,
                    rolling_window_days: 14,
                    avoid_repeat_within_days: None,
                    enabled: true,
                    priority: 1,
                    block_id: BLOCK_ID.into(),
                    block_revision: 1,
                    weekday_mask: 0b0111_1111,
                    interval_weeks: 1,
                    anchor_date: LocalDate {
                        year: 2020,
                        month: 1,
                        day: 1,
                    },
                    timezone: RecurrenceTimeZone::Utc,
                    local_start_minute: 0,
                    duration_minutes: 1,
                    revision: 1,
                },
                None,
                OWNER_ID,
                5,
            )
            .unwrap();

        let authority = S19TestEvidenceAuthority::new(super::system_time_millis().unwrap());
        let materialization = store
            .materialize_scheduled_channel(CHANNEL_ID, OWNER_ID, &authority)
            .unwrap();
        let run = match &materialization {
            ScheduleMaterializationApplyOutcome::Applied(run) => run,
            ScheduleMaterializationApplyOutcome::AlreadyApplied(_) => {
                panic!("first S19 handoff materialization must apply")
            }
        };
        let (schedule_item_id, generated_starts_at_utc_ms) = run
            .outcomes
            .iter()
            .find_map(|record| match &record.outcome {
                ScheduleMaterializationOutcome::Proposed {
                    schedule_item_id,
                    starts_at_utc_ms,
                    ..
                } => Some((schedule_item_id.clone(), *starts_at_utc_ms)),
                _ => None,
            })
            .expect("S19 must generate at least one Draft");
        let generated = store
            .read_schedule_item(&schedule_item_id)
            .unwrap()
            .expect("the S19 Draft must be current");
        assert_eq!(generated.state, ScheduleState::Draft);
        assert_eq!(generated.starts_at_unix_ms, generated_starts_at_utc_ms);

        // Production S19 intentionally creates only future Drafts. Move this
        // same generated identity into an active test window through the
        // ordinary Draft write path; the immutable materialization run remains
        // bound to its original proposed time and item identity.
        let actual_on_air_at_unix_ms = super::system_time_millis().unwrap();
        let active_starts_at_unix_ms = actual_on_air_at_unix_ms - 1_000;
        store
            .write_schedule_item(&ScheduleItemWrite {
                item: ScheduleItem {
                    revision: generated.revision + 1,
                    starts_at_unix_ms: active_starts_at_unix_ms,
                    ..generated.clone()
                },
                changed_by: OWNER_ID.into(),
                occurred_at_unix_ms: active_starts_at_unix_ms,
            })
            .unwrap();
        assert!(
            store
                .read_schedule_materialization_run(&run.run_id)
                .unwrap()
                .unwrap()
                .outcomes
                .iter()
                .any(|record| matches!(
                    &record.outcome,
                    ScheduleMaterializationOutcome::Proposed {
                        schedule_item_id: durable_item_id,
                        starts_at_utc_ms,
                        ..
                    } if durable_item_id == &schedule_item_id
                        && *starts_at_utc_ms == generated_starts_at_utc_ms
                ))
        );

        let plan = store
            .prepare_schedule_commit(PLAN_ID, &schedule_item_id)
            .unwrap();
        assert!(plan.dry_run_passed);
        let report = store
            .commit_schedule(
                REPORT_ID,
                PLAN_ID,
                &schedule_item_id,
                OWNER_ID,
                actual_on_air_at_unix_ms - 500,
                "S19 to S4 handoff proof",
            )
            .unwrap();
        assert_eq!(report.dispatch_status, DispatchStatus::Pending);
        assert!(
            store
                .advance_dispatch_status(REPORT_ID, DispatchStatus::Pending, DispatchStatus::Queued)
                .unwrap()
        );
        let dispatch = CommandEnvelope {
            version: PROTOCOL_VERSION,
            channel_id: CHANNEL_ID.into(),
            command_id: format!("take-{REPORT_ID}"),
            expected_sequence: 1,
            command: ChannelCommand::TakeScheduledAsset {
                report_id: REPORT_ID.into(),
                schedule_item_id: schedule_item_id.clone(),
                schedule_item_revision: report.schedule_item_revision,
                asset_id: ASSET_ID.into(),
                media_offset_ms: 1_000,
            },
        };
        assert_eq!(
            decode_command_frame(&encode_command_frame(&dispatch).unwrap()).unwrap(),
            dispatch
        );
        assert_eq!(dispatch.version.major, 11);

        let journal = root
            .join("channel-journals")
            .join(format!("{CHANNEL_ID}.tlj"));
        std::fs::create_dir_all(journal.parent().unwrap()).unwrap();
        let worker_id = format!("station-worker-{CHANNEL_ID}");
        let mut writer = JournalWriter::open(&journal, &worker_id, CHANNEL_ID).unwrap();
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id,
                channel_id: CHANNEL_ID.into(),
                sequence: 1,
                event: WorkerEvent::ScheduledAssetOnAir {
                    report_id: REPORT_ID.into(),
                    schedule_item_id: schedule_item_id.clone(),
                    schedule_item_revision: report.schedule_item_revision,
                    asset_id: ASSET_ID.into(),
                    media_offset_ms: 1_000,
                    worker_boot_id: WORKER_BOOT_ID.into(),
                    actual_on_air_at_unix_ms,
                },
            })
            .unwrap();
        drop(writer);

        super::replay_scheduled_take_journal(&store, &journal, CHANNEL_ID).unwrap();
        assert_eq!(
            store
                .read_commit_report(REPORT_ID)
                .unwrap()
                .unwrap()
                .dispatch_status,
            DispatchStatus::Acknowledged
        );
        let facts = store
            .list_actual_airings(
                CHANNEL_ID,
                actual_on_air_at_unix_ms - 1,
                actual_on_air_at_unix_ms + 60_001,
                8,
            )
            .unwrap();
        assert_eq!(facts.len(), 1);
        assert_eq!(
            facts[0].schedule_item_id.as_deref(),
            Some(schedule_item_id.as_str())
        );
        assert_eq!(
            facts[0].schedule_item_revision,
            Some(report.schedule_item_revision)
        );
        assert_eq!(facts[0].report_id, REPORT_ID);
        assert_eq!(facts[0].asset_sha256, ASSET_ID);
        assert_eq!(PROTOCOL_VERSION.major, 11);

        super::replay_scheduled_take_journal(&store, &journal, CHANNEL_ID).unwrap();
        assert_eq!(
            store
                .list_actual_airings(
                    CHANNEL_ID,
                    actual_on_air_at_unix_ms - 1,
                    actual_on_air_at_unix_ms + 60_001,
                    8,
                )
                .unwrap()
                .len(),
            1
        );

        drop(store);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn prelaunch_replay_durably_recovers_confirmed_take_and_is_idempotent() {
        let (database, journal, store, report_id, now) =
            scheduled_take_replay_fixture("confirmed-take", false);

        // Crash window: the worker flushed its confirmation but stationd
        // died before it received the IPC event. The replacement must ingest
        // the durable evidence before it may launch a new worker.
        super::replay_scheduled_take_journal(&store, &journal, CHANNEL_ID).unwrap();
        assert_eq!(
            store
                .read_commit_report(&report_id)
                .unwrap()
                .unwrap()
                .dispatch_status,
            station_schedule::DispatchStatus::Acknowledged
        );
        assert_eq!(
            store
                .list_actual_airings(CHANNEL_ID, now - 10_000, now + 10_000, 8)
                .unwrap()
                .len(),
            1
        );

        // Crash window: IPC reached stationd and storage committed, but the
        // process died before retiring the worker. Replaying the same journal
        // must remain read-only and must not manufacture a second airing.
        super::replay_scheduled_take_journal(&store, &journal, CHANNEL_ID).unwrap();
        assert_eq!(
            store
                .list_actual_airings(CHANNEL_ID, now - 10_000, now + 10_000, 8)
                .unwrap()
                .len(),
            1
        );

        drop(store);
        let _ = std::fs::remove_dir_all(database.parent().unwrap());
    }

    #[test]
    fn prelaunch_replay_of_exact_receipt_does_not_reopen_or_block_an_errored_report() {
        let (database, journal, store, report_id, now) =
            scheduled_take_replay_fixture("errored-confirmed-take", false);

        super::replay_scheduled_take_journal(&store, &journal, CHANNEL_ID).unwrap();
        assert!(
            store
                .advance_dispatch_status(
                    &report_id,
                    station_schedule::DispatchStatus::Acknowledged,
                    station_schedule::DispatchStatus::Error,
                )
                .unwrap()
        );

        // A later worker launch must still be able to replay the retained
        // confirmation. The immutable fact is already authoritative, and the
        // terminal dispatch error must remain terminal.
        super::replay_scheduled_take_journal(&store, &journal, CHANNEL_ID).unwrap();
        assert_eq!(
            store
                .read_commit_report(&report_id)
                .unwrap()
                .unwrap()
                .dispatch_status,
            station_schedule::DispatchStatus::Error
        );
        assert_eq!(
            store
                .list_actual_airings(CHANNEL_ID, now - 10_000, now + 10_000, 8)
                .unwrap()
                .len(),
            1
        );

        drop(store);
        let _ = std::fs::remove_dir_all(database.parent().unwrap());
    }

    #[test]
    fn prelaunch_replay_rejects_foreign_journal_before_authoritative_writes() {
        let (database, journal, store, report_id, now) =
            scheduled_take_replay_fixture("foreign-journal", false);
        let foreign_journal = journal.with_file_name("foreign.tlj");
        let mut writer = JournalWriter::open(
            &foreign_journal,
            "station-worker-99999999-9999-4999-8999-999999999999",
            CHANNEL_ID,
        )
        .unwrap();
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: "station-worker-99999999-9999-4999-8999-999999999999".into(),
                channel_id: CHANNEL_ID.into(),
                sequence: 1,
                event: WorkerEvent::Ready { graph_revision: 1 },
            })
            .unwrap();
        drop(writer);

        assert!(
            super::replay_scheduled_take_journal(&store, &foreign_journal, CHANNEL_ID).is_err()
        );
        assert_eq!(
            store
                .read_commit_report(&report_id)
                .unwrap()
                .unwrap()
                .dispatch_status,
            station_schedule::DispatchStatus::Queued
        );
        assert!(
            store
                .list_actual_airings(CHANNEL_ID, now - 10_000, now + 10_000, 8)
                .unwrap()
                .is_empty()
        );

        drop(store);
        let _ = std::fs::remove_dir_all(database.parent().unwrap());
    }

    #[test]
    fn prelaunch_replay_of_a_late_confirmation_does_not_invent_current_on_air() {
        let (database, journal, store, report_id, _now) =
            scheduled_take_replay_fixture("historical-take", true);

        super::replay_scheduled_take_journal(&store, &journal, CHANNEL_ID).unwrap();
        assert_eq!(
            store
                .read_commit_report(&report_id)
                .unwrap()
                .unwrap()
                .dispatch_status,
            station_schedule::DispatchStatus::Acknowledged
        );
        assert_eq!(store.read_channel_runtime(CHANNEL_ID).unwrap(), None);

        drop(store);
        let _ = std::fs::remove_dir_all(database.parent().unwrap());
    }

    #[test]
    fn live_arm_capabilities_are_random_bounded_and_redacted() {
        let first = generate_live_arm_token().unwrap();
        let second = generate_live_arm_token().unwrap();
        assert_ne!(first, second);
        assert_eq!(first.expose().len(), 64);
        assert_eq!(format!("{first:?}"), "LiveArmToken([REDACTED])");
    }

    fn remove_database(path: &std::path::Path) {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{}", path.display(), suffix));
        }
    }

    #[test]
    fn proof_artifact_path_rejects_escape_and_hashes_managed_file() {
        let root = std::env::temp_dir().join(format!(
            "townlight-proof-root-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let artifact = root.join("capture.ts");
        std::fs::write(&artifact, b"proof-bytes").unwrap();
        let resolved = proof_artifact_path(&root, "capture.ts").unwrap();
        assert_eq!(resolved, std::fs::canonicalize(&artifact).unwrap());
        assert_eq!(hash_capture(&resolved, 11).unwrap().0, 11);
        assert!(proof_artifact_path(&root, "../capture.ts").is_err());
        let _ = std::fs::remove_file(artifact);
        let _ = std::fs::remove_dir(root);
    }

    #[test]
    fn proof_binding_order_is_authoritative_and_rejects_reordering() {
        let first = super::ProofOutputBinding {
            sink_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into(),
            target_lufs_milli: -24_000,
        };
        let second = super::ProofOutputBinding {
            sink_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb".into(),
            target_lufs_milli: -16_000,
        };
        assert!(proof_bindings_match(
            &[first.clone(), second.clone()],
            &[first.clone(), second.clone()]
        ));
        assert!(!proof_bindings_match(
            &[first, second.clone()],
            std::slice::from_ref(&second)
        ));
        assert!(!proof_bindings_match(
            &[second.clone(), second.clone()],
            &[second]
        ));
    }

    #[test]
    fn capture_duration_adds_bounded_decode_headroom() {
        assert_eq!(capture_duration_for_sample(2_999), None);
        assert_eq!(capture_duration_for_sample(3_000), Some(7_000));
        assert_eq!(capture_duration_for_sample(600_000), Some(604_000));
        assert_eq!(capture_duration_for_sample(600_001), None);

        assert!(!ready_duration_is_bounded(6_999, 7_000));
        assert!(ready_duration_is_bounded(7_000, 7_000));
        assert!(ready_duration_is_bounded(7_250, 7_000));
        assert!(!ready_duration_is_bounded(604_001, 7_000));
    }

    #[test]
    fn lease_renewal_is_due_only_near_expiry() {
        assert!(!lease_renewal_due(1_000, Some(100_000)));
        assert!(lease_renewal_due(70_000, Some(100_000)));
        assert!(lease_renewal_due(100_000, Some(100_000)));
        assert!(lease_renewal_due(1_000, None));
    }

    #[test]
    fn orphan_cleanup_removes_only_deterministic_regular_artifacts() {
        let root = std::env::temp_dir().join(format!(
            "townlight-proof-cleanup-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let job_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
        std::fs::write(root.join(format!("{job_id}.ts")), b"old-final").unwrap();
        std::fs::write(root.join(format!("{job_id}.ts.part")), b"old-part").unwrap();
        cleanup_orphan_output_proof_artifacts(&root, job_id).unwrap();
        assert!(!root.join(format!("{job_id}.ts")).exists());
        assert!(!root.join(format!("{job_id}.ts.part")).exists());
        let _ = std::fs::remove_dir(root);
    }

    #[test]
    fn abort_requires_the_worker_terminal_abort_confirmation() {
        let aborted = WorkerEvent::OutputProofFailed {
            proof_id: "proof-1".into(),
            artifact_relative_path: "proof-1.ts".into(),
            worker_instance_id: "worker-1".into(),
            graph_revision: 1,
            code: "capture_aborted".into(),
            message: "aborted".into(),
        };
        let rejected = WorkerEvent::CommandRejected {
            command_id: "abort".into(),
            code: "capture_aborted".into(),
            message: "not active".into(),
        };
        assert!(is_abort_confirmation(&aborted, "proof-1"));
        assert!(!is_abort_confirmation(&aborted, "proof-2"));
        assert!(!is_abort_confirmation(&rejected, "proof-1"));
    }

    fn put_channel(store: &StationStore, channel_id: &str) {
        store
            .put_channel(&ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: channel_id.into(),
                output_profile: OutputProfileId::Proof360p30,
                output_sinks: vec![OutputSinkConfiguration::required_udp(
                    "f103968a-640b-42dc-8084-3fe9e847ff7f",
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: false,
            })
            .unwrap();
    }

    fn audit(
        audit_id: &str,
        channel_id: &str,
        event_kind: CgAuditEventKind,
        occurred_at_unix_ms: i64,
    ) -> CgAuditEvent {
        CgAuditEvent {
            audit_id: audit_id.into(),
            board_id: Some(BOARD_ID.into()),
            channel_id: channel_id.into(),
            event_kind,
            operator_id: "operator".into(),
            occurred_at_unix_ms,
            details: BTreeMap::new(),
        }
    }

    fn seed_scheduled_cg(store: &StationStore, end_at_unix_ms: i64) {
        let board = CgBoard {
            board_id: BOARD_ID.into(),
            channel_id: CHANNEL_ID.into(),
            template_id: "fullscreen".into(),
            active: true,
            background_audio_asset_id: None,
            revision: 1,
            created_at_unix_ms: 0,
            updated_at_unix_ms: 0,
            created_by: "operator".into(),
        };
        store
            .write_cg_board(
                &board,
                0,
                &audit(
                    "7bb32e14-fe69-47cb-a921-dc3fc84499bf",
                    CHANNEL_ID,
                    CgAuditEventKind::BoardCreated,
                    0,
                ),
            )
            .unwrap();
        let zone = CgZoneConfig {
            zone_id: ZONE_ID.into(),
            board_id: BOARD_ID.into(),
            template_zone_key: "primary".into(),
            zone_kind: CgZoneKind::Primary,
            content_source: CgContentSource::BulletinRotation,
            rotation_seconds: Some(3),
            approval_required: false,
            allowed_tags: Vec::new(),
            revision: 1,
            created_at_unix_ms: 0,
            updated_at_unix_ms: 0,
        };
        store
            .write_cg_zone(
                &zone,
                0,
                &audit(
                    "31086e51-b940-450d-a9c2-5a31807ecc02",
                    CHANNEL_ID,
                    CgAuditEventKind::ZoneCreated,
                    0,
                ),
            )
            .unwrap();
        let submitted = CgBulletin {
            bulletin_id: BULLETIN_ID.into(),
            channel_id: CHANNEL_ID.into(),
            content: CgBulletinContent::Text {
                title: "Town notice".into(),
                body: "Scheduled notice".into(),
            },
            state: CgBulletinState::Submitted,
            tags: Vec::new(),
            requested_start_at_unix_ms: Some(0),
            requested_end_at_unix_ms: Some(end_at_unix_ms),
            display_seconds: 3,
            audio_asset_id: None,
            submitted_by: "operator".into(),
            moderation_notes: None,
            moderated_by: None,
            revision: 1,
            created_at_unix_ms: 0,
            updated_at_unix_ms: 0,
        };
        store
            .write_cg_bulletin(
                &submitted,
                0,
                &audit(
                    "42086e51-b940-450d-a9c2-5a31807ecc02",
                    CHANNEL_ID,
                    CgAuditEventKind::BulletinSubmitted,
                    0,
                ),
            )
            .unwrap();
        let scheduled = CgBulletin {
            state: CgBulletinState::Scheduled,
            moderated_by: Some("operator".into()),
            revision: 2,
            updated_at_unix_ms: 500,
            ..submitted
        };
        store
            .write_cg_bulletin(
                &scheduled,
                1,
                &audit(
                    "53086e51-b940-450d-a9c2-5a31807ecc02",
                    CHANNEL_ID,
                    CgAuditEventKind::BulletinModerated,
                    500,
                ),
            )
            .unwrap();
    }

    fn seed_unrelated_scheduled_bulletin(
        store: &StationStore,
        bulletin_id: &str,
        channel_id: &str,
        end_at_unix_ms: i64,
    ) {
        let submitted = CgBulletin {
            bulletin_id: bulletin_id.into(),
            channel_id: channel_id.into(),
            content: CgBulletinContent::Text {
                title: "Other notice".into(),
                body: "Other scheduled notice".into(),
            },
            state: CgBulletinState::Submitted,
            tags: Vec::new(),
            requested_start_at_unix_ms: Some(0),
            requested_end_at_unix_ms: Some(end_at_unix_ms),
            display_seconds: 3,
            audio_asset_id: None,
            submitted_by: "operator".into(),
            moderation_notes: None,
            moderated_by: None,
            revision: 1,
            created_at_unix_ms: 0,
            updated_at_unix_ms: 0,
        };
        store
            .write_cg_bulletin(
                &submitted,
                0,
                &audit(
                    "64086e51-b940-450d-a9c2-5a31807ecc02",
                    channel_id,
                    CgAuditEventKind::BulletinSubmitted,
                    0,
                ),
            )
            .unwrap();
        store
            .write_cg_bulletin(
                &CgBulletin {
                    state: CgBulletinState::Scheduled,
                    moderated_by: Some("operator".into()),
                    revision: 2,
                    updated_at_unix_ms: 500,
                    ..submitted
                },
                1,
                &audit(
                    "75086e51-b940-450d-a9c2-5a31807ecc02",
                    channel_id,
                    CgAuditEventKind::BulletinModerated,
                    500,
                ),
            )
            .unwrap();
    }

    #[test]
    fn expiration_precedes_same_timestamp_snapshot_and_is_restart_idempotent() {
        let path = database_path("cg-expiration-orchestration");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store, CHANNEL_ID);
        put_channel(&store, OTHER_CHANNEL_ID);
        seed_scheduled_cg(&store, 1_000);
        seed_unrelated_scheduled_bulletin(&store, OTHER_BULLETIN_ID, OTHER_CHANNEL_ID, 1_000);

        expire_due_bulletins(&store, CHANNEL_ID, 1_000).unwrap();
        let expired = store.read_cg_bulletin(BULLETIN_ID).unwrap().unwrap();
        assert_eq!(expired.state, CgBulletinState::Expired);
        assert_eq!(expired.revision, 3);
        assert_eq!(expired.updated_at_unix_ms, 1_000);
        let unrelated = store.read_cg_bulletin(OTHER_BULLETIN_ID).unwrap().unwrap();
        assert_eq!(unrelated.state, CgBulletinState::Scheduled);
        assert_eq!(unrelated.revision, 2);

        let snapshot = store
            .load_cg_snapshot_inputs(CHANNEL_ID, 1_000)
            .unwrap()
            .resolve()
            .unwrap();
        assert!(matches!(
            snapshot.zones[0].content,
            CgResolvedContent::Placeholder {
                reason: station_cg::CgPlaceholderReason::NoAirableBulletins,
                ..
            }
        ));
        assert!(
            resolved_snapshot_to_protocol(&snapshot)
                .unwrap()
                .overlays
                .is_empty()
        );
        assert_eq!(
            store
                .list_cg_audit_events(CHANNEL_ID, 100)
                .unwrap()
                .into_iter()
                .filter(|event| event.event_kind == CgAuditEventKind::BulletinExpired)
                .count(),
            1
        );

        drop(store);
        let reopened = StationStore::open(&path).unwrap();
        expire_due_bulletins(&reopened, CHANNEL_ID, 1_000).unwrap();
        assert_eq!(
            reopened
                .list_cg_audit_events(CHANNEL_ID, 100)
                .unwrap()
                .into_iter()
                .filter(|event| event.event_kind == CgAuditEventKind::BulletinExpired)
                .count(),
            1
        );
        drop(reopened);
        remove_database(&path);
    }

    #[test]
    fn expiration_leaves_not_yet_due_bulletin_airable() {
        let path = database_path("cg-expiration-not-due");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store, CHANNEL_ID);
        seed_scheduled_cg(&store, 2_000);

        expire_due_bulletins(&store, CHANNEL_ID, 1_000).unwrap();
        assert_eq!(
            store.read_cg_bulletin(BULLETIN_ID).unwrap().unwrap().state,
            CgBulletinState::Scheduled
        );
        let snapshot = store
            .load_cg_snapshot_inputs(CHANNEL_ID, 1_000)
            .unwrap()
            .resolve()
            .unwrap();
        assert!(matches!(
            snapshot.zones[0].content,
            CgResolvedContent::Bulletin { ref bulletin_id, .. }
                if bulletin_id == BULLETIN_ID
        ));
        remove_database(&path);
    }

    #[test]
    fn expiration_rejects_clock_regression_as_an_observable_tick_error() {
        let path = database_path("cg-expiration-clock-regression");
        let store = StationStore::open(&path).unwrap();
        put_channel(&store, CHANNEL_ID);
        let error = expire_due_bulletins(&store, CHANNEL_ID, -1).unwrap_err();
        assert!(
            matches!(error, OrchestrationError::Storage(message) if message.contains("expiration rejected"))
        );
        remove_database(&path);
    }

    fn output(required: bool) -> OutputExpectation {
        OutputExpectation {
            sink_id: "76ad7ff3-60cb-4ae5-862c-b26d66e45808".into(),
            label: "Test output".into(),
            required,
        }
    }

    #[test]
    fn requires_initial_bytes_and_continuous_progress_only_for_required_outputs() {
        assert_eq!(
            stalled_required_output(&[output(true)], None, &[0]),
            Some(0)
        );
        assert_eq!(stalled_required_output(&[output(true)], None, &[1]), None);
        assert_eq!(
            stalled_required_output(&[output(true)], Some(&[100]), &[100]),
            Some(0)
        );
        assert_eq!(
            stalled_required_output(&[output(true)], Some(&[100]), &[101]),
            None
        );
        assert_eq!(
            stalled_required_output(&[output(false)], Some(&[100]), &[100]),
            None
        );
    }

    fn snapshot(content: CgResolvedContent) -> CgResolvedSnapshot {
        CgResolvedSnapshot {
            board_id: "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc".into(),
            channel_id: "5d85189a-cd1f-43dc-a619-66a2aeb09ac4".into(),
            template_id: "l-bar".into(),
            template_kind: CgTemplateKind::LBar,
            board_revision: 7,
            generated_at_unix_ms: 1_000,
            zones: vec![CgResolvedZone {
                zone_id: "8f4f0f70-5c30-48c6-819a-fb22b34353c5".into(),
                template_zone_key: "ticker".into(),
                zone_kind: CgZoneKind::Ticker,
                bounds: CgRect {
                    x: 10,
                    y: 700,
                    width: 800,
                    height: 200,
                },
                z_index: 1,
                content_source: match &content {
                    CgResolvedContent::Bulletin { .. } => CgContentSource::BulletinRotation,
                    CgResolvedContent::FeedItems { feed_source_id, .. } => CgContentSource::Feed {
                        feed_source_id: feed_source_id.clone(),
                    },
                    CgResolvedContent::Image { asset } => CgContentSource::Image {
                        asset_id: asset.asset_id.clone(),
                    },
                    CgResolvedContent::Placeholder {
                        source: station_cg::CgPlaceholderSource::Image,
                        ..
                    } => CgContentSource::Image {
                        asset_id:
                            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                                .into(),
                    },
                    _ => CgContentSource::Manual { text: "x".into() },
                },
                content,
                revision: 1,
            }],
        }
    }

    #[test]
    fn maps_supported_content_and_leaves_typed_placeholders_transparent() {
        let mut state = CgSnapshotState::default();
        let resolved = snapshot(CgResolvedContent::ManualText {
            text: "hello".into(),
        });
        let protocol = state.accept(&resolved).unwrap();
        assert_eq!(protocol.overlays.len(), 1);
        assert!(state.resend_after_ready().is_some());
        state.mark_applied(&protocol.board_id, protocol.board_revision);
        assert!(state.resend_after_ready().is_none());
        state.worker_ready();
        state.mark_applied("another-board", protocol.board_revision);
        assert!(state.resend_after_ready().is_some());
        state.worker_ready();
        assert!(state.resend_after_ready().is_some());
        let transparent = snapshot(CgResolvedContent::Placeholder {
            source: station_cg::CgPlaceholderSource::Image,
            reason: station_cg::CgPlaceholderReason::ImageAssetReadinessUnknown,
        });
        assert!(
            resolved_snapshot_to_protocol(&transparent)
                .unwrap()
                .overlays
                .is_empty()
        );

        let bulletin = snapshot(CgResolvedContent::Bulletin {
            bulletin_id: "597f52ae-f16c-4ff8-ab32-60379cd950c4".into(),
            content: station_cg::CgBulletinContent::Text {
                title: "Title".into(),
                body: "Body".into(),
            },
        });
        let protocol = resolved_snapshot_to_protocol(&bulletin).unwrap();
        assert!(matches!(
            &protocol.overlays[0].content,
            station_media_protocol::CgOverlayContent::Text { text } if text == "Title\nBody"
        ));

        let image = snapshot(CgResolvedContent::Image {
            asset: station_cg::CgImageAsset {
                asset_id: "a".repeat(64),
                mime_type: "image/jpeg".into(),
                byte_size: 128,
                width: 640,
                height: 360,
                readiness: station_cg::CgImageReadiness::Ready,
            },
        });
        let protocol = resolved_snapshot_to_protocol(&image).unwrap();
        assert!(matches!(
            &protocol.overlays[0].content,
            station_media_protocol::CgOverlayContent::Image { asset_id, format }
                if asset_id == &"a".repeat(64)
                    && *format == station_media_protocol::CgImageFormat::Jpeg
        ));

        let feed = snapshot(CgResolvedContent::FeedItems {
            feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
            items: vec![
                station_cg::CgFeedItem {
                    item_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        .into(),
                    title: "First".into(),
                    summary: None,
                    starts_at_unix_ms: Some(2),
                    url: None,
                    tags: Vec::new(),
                },
                station_cg::CgFeedItem {
                    item_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                        .into(),
                    title: "Second".into(),
                    summary: None,
                    starts_at_unix_ms: Some(1),
                    url: None,
                    tags: Vec::new(),
                },
            ],
            stale: false,
            last_error: None,
        });
        let protocol = resolved_snapshot_to_protocol(&feed).unwrap();
        assert_eq!(protocol.overlays.len(), 1);
        assert!(matches!(
            &protocol.overlays[0].content,
            station_media_protocol::CgOverlayContent::Text { text } if text == "Second"
        ));
    }

    #[test]
    fn inactive_or_missing_cg_authority_requests_one_clear_and_allows_reapply() {
        assert!(super::cg_read_requires_clear(
            &station_storage::CgSnapshotReadError::BoardInactive
        ));
        assert!(super::cg_read_requires_clear(
            &station_storage::CgSnapshotReadError::BoardNotFound
        ));
        assert!(!super::cg_read_requires_clear(
            &station_storage::CgSnapshotReadError::Storage("transient".into())
        ));

        let mut state = CgSnapshotState::default();
        let resolved = snapshot(CgResolvedContent::ManualText {
            text: "visible before disable".into(),
        });
        let protocol = state.accept(&resolved).unwrap();
        state.mark_applied(&protocol.board_id, protocol.board_revision);

        state.invalidate_authority();
        assert!(state.last_valid().is_none());
        assert!(state.clear_required());

        // A successful CgCleared acknowledgment consumes the request. Repeated
        // inactive/missing reads must not emit another clear.
        state.mark_cleared();
        state.invalidate_authority();
        assert!(!state.clear_required());

        // A newly authoritative snapshot after clear is sent again, including
        // when it reuses the same board revision.
        let reapplied = state.accept(&resolved).unwrap();
        assert!(state.resend_after_ready().is_some());
        state.mark_applied(&reapplied.board_id, reapplied.board_revision);
        assert!(state.resend_after_ready().is_none());
    }

    #[test]
    fn rejects_text_that_exceeds_protocol_limit() {
        let resolved = snapshot(CgResolvedContent::Bulletin {
            bulletin_id: "597f52ae-f16c-4ff8-ab32-60379cd950c4".into(),
            content: station_cg::CgBulletinContent::Text {
                title: "title".into(),
                body: "x".repeat(3_000),
            },
        });
        assert!(matches!(
            resolved_snapshot_to_protocol(&resolved),
            Err(OrchestrationError::InvalidCgSnapshot(_))
        ));
    }

    #[test]
    fn caption_dispatch_plans_active_and_bounded_future_asset_relative_cues() {
        let track = AssetCaptionTrack {
            track_id: "65a26343-0339-4654-a92c-4bf9384f18df".into(),
            asset_id: "a".repeat(64),
            language: "en-US".into(),
            service_number: 1,
            source_kind: CaptionTrackSourceKind::Sidecar,
            label: "Reviewed English".into(),
            cues: vec![
                AssetCaptionCue {
                    cue_id: "11111111-1111-4111-8111-111111111111".into(),
                    start_ms: 0,
                    duration_ms: 500,
                    text: "Expired".into(),
                },
                AssetCaptionCue {
                    cue_id: "22222222-2222-4222-8222-222222222222".into(),
                    start_ms: 500,
                    duration_ms: 1_000,
                    text: "Resume this cue".into(),
                },
                AssetCaptionCue {
                    cue_id: "33333333-3333-4333-8333-333333333333".into(),
                    start_ms: 2_000,
                    duration_ms: 1_000,
                    text: "Near future".into(),
                },
                AssetCaptionCue {
                    cue_id: "44444444-4444-4444-8444-444444444444".into(),
                    start_ms: 61_000,
                    duration_ms: 1_000,
                    text: "At lookahead boundary".into(),
                },
                AssetCaptionCue {
                    cue_id: "55555555-5555-4555-8555-555555555555".into(),
                    start_ms: 62_000,
                    duration_ms: 1_000,
                    text: "Outside lookahead".into(),
                },
            ],
            revision: 7,
            cues_sha256: "b".repeat(64),
            created_at_unix_ms: 1,
            updated_at_unix_ms: 2,
            updated_by: "caption-reviewer".into(),
        };

        let planned = due_caption_cues(&track, 1_000, &HashSet::new(), &HashSet::new());
        assert_eq!(planned.due.len(), 3);
        assert_eq!(
            planned.due[0].cue_id,
            "22222222-2222-4222-8222-222222222222"
        );
        assert_eq!(planned.due[0].start_after_ms, 0);
        assert_eq!(planned.due[0].duration_ms, 500);
        assert_eq!(
            planned.due[1].cue_id,
            "33333333-3333-4333-8333-333333333333"
        );
        assert_eq!(planned.due[1].start_after_ms, 1_000);
        assert_eq!(planned.due[1].duration_ms, 1_000);
        assert_eq!(
            planned.due[2].cue_id,
            "44444444-4444-4444-8444-444444444444"
        );
        assert_eq!(planned.due[2].start_after_ms, 60_000);
        assert_eq!(
            planned.missed_cue_ids,
            vec!["11111111-1111-4111-8111-111111111111"]
        );
        assert!(
            !planned
                .missed_cue_ids
                .contains(&"22222222-2222-4222-8222-222222222222".into()),
            "an intentional active-cue catch-up is not an expired caption incident"
        );
        assert!(
            planned
                .due
                .iter()
                .all(|cue| cue.cue_id != "55555555-5555-4555-8555-555555555555"),
            "far-future cues stay under controller authority until their delay is representable"
        );
        let later = due_caption_cues(&track, 2_000, &HashSet::new(), &HashSet::new());
        assert!(
            later
                .due
                .iter()
                .any(|cue| cue.cue_id == "55555555-5555-4555-8555-555555555555"
                    && cue.start_after_ms == 60_000)
        );

        let accepted = HashSet::from([
            "22222222-2222-4222-8222-222222222222".into(),
            "33333333-3333-4333-8333-333333333333".into(),
        ]);
        let already_missed = HashSet::from(["11111111-1111-4111-8111-111111111111".into()]);
        let excluded = due_caption_cues(&track, 1_000, &accepted, &already_missed);
        assert_eq!(excluded.missed_cue_ids, Vec::<String>::new());
        assert!(excluded.due.iter().all(|cue| {
            cue.cue_id != "11111111-1111-4111-8111-111111111111"
                && cue.cue_id != "22222222-2222-4222-8222-222222222222"
                && cue.cue_id != "33333333-3333-4333-8333-333333333333"
        }));

        let mut dense = track.clone();
        dense.cues = (0..65)
            .map(|index| AssetCaptionCue {
                cue_id: format!("00000000-0000-4000-8000-{index:012}"),
                start_ms: 0,
                duration_ms: 10_000,
                text: format!("Cue {index}"),
            })
            .collect();
        assert_eq!(
            due_caption_cues(&dense, 0, &HashSet::new(), &HashSet::new())
                .due
                .len(),
            64
        );
    }

    #[test]
    fn caption_window_miss_is_durable_channel_scoped_and_only_a_later_admission_resolves_it() {
        let path = database_path("caption-cue-missed-alert");
        let store = StationStore::open(&path).unwrap();
        let track = AssetCaptionTrack {
            track_id: "65a26343-0339-4654-a92c-4bf9384f18df".into(),
            asset_id: "a".repeat(64),
            language: "en-US".into(),
            service_number: 1,
            source_kind: CaptionTrackSourceKind::Sidecar,
            label: "Reviewed English".into(),
            cues: Vec::new(),
            revision: 7,
            cues_sha256: "b".repeat(64),
            created_at_unix_ms: 1,
            updated_at_unix_ms: 2,
            updated_by: "caption-reviewer".into(),
        };
        let cue_id = "11111111-1111-4111-8111-111111111111";

        let cue_ids = vec![cue_id.into()];
        assert!(
            observe_caption_cue_misses(
                &store,
                CHANNEL_ID,
                "report-a",
                &track,
                1_000,
                CaptionCueMissReason::ExpiredBeforeAdmission,
                &cue_ids,
            )
            .unwrap()
        );
        assert!(
            !observe_caption_cue_misses(
                &store,
                CHANNEL_ID,
                "report-a",
                &track,
                1_100,
                CaptionCueMissReason::ExpiredBeforeAdmission,
                &cue_ids,
            )
            .unwrap(),
            "restart/replay of exact miss evidence must not re-alert"
        );

        let events = store.list_alert_events().unwrap();
        let firing = events
            .iter()
            .find(|event| {
                event.condition == station_domain::AlertConditionKind::CaptionCueMissed
                    && event.resource_ref == CHANNEL_ID
                    && event.state == station_domain::AlertEventState::Firing
            })
            .expect("the server-observed caption miss must be a durable firing alert");
        assert_eq!(firing.severity, station_domain::AlertSeverity::Warning);
        assert_eq!(firing.occurrence_count, 1);
        assert!(firing.detail.contains("report-a"));
        assert!(firing.detail.contains(cue_id));
        assert!(!firing.detail.contains("caption text"));

        assert!(
            store
                .resolve_alert(
                    station_domain::AlertConditionKind::CaptionCueMissed,
                    OTHER_CHANNEL_ID,
                    firing.last_observed_at_unix_ms,
                )
                .unwrap()
                .is_none(),
            "one channel must never resolve another channel's caption incident"
        );
        assert!(
            store
                .read_alert_event(&firing.event_id)
                .unwrap()
                .is_some_and(|event| event.state == station_domain::AlertEventState::Firing),
            "time passing and unrelated resolution must not clear the incident"
        );

        resolve_caption_cue_miss_after_admission(&store, CHANNEL_ID, 0, false).unwrap();
        resolve_caption_cue_miss_after_admission(&store, CHANNEL_ID, 1, true).unwrap();
        assert!(
            store
                .read_alert_event(&firing.event_id)
                .unwrap()
                .is_some_and(|event| event.state == station_domain::AlertEventState::Firing),
            "empty or mixed dispatch batches cannot make readiness healthy"
        );
        resolve_caption_cue_miss_after_admission(&store, CHANNEL_ID, 1, false).unwrap();
        assert!(
            store
                .read_alert_event(&firing.event_id)
                .unwrap()
                .is_some_and(|event| event.state == station_domain::AlertEventState::Resolved),
            "only the subsequent successful-admission path resolves the channel incident"
        );
    }
}
