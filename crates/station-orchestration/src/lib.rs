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
use station_cg::{CgBulletinContent, CgResolvedContent, CgResolvedSnapshot};
use station_domain::{
    AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity, ChannelRuntimeObservation,
    ChannelRuntimeState,
};
use station_live::{LiveEndpoint, TakeoverSession, TakeoverState};
use station_media_assets::measure_output_loudness;
use station_media_protocol::{
    CgImageFormat, CgOverlay, CgOverlayContent, CgRect, CgSnapshot, ChannelCommand, LiveArmToken,
    LiveInputConfiguration, LiveReturnTarget, LoudnessTarget, MAX_CG_SLOTS, MAX_CG_TEXT_BYTES,
    MAX_OUTPUT_PROOF_DURATION_MS, MAX_OUTPUT_PROOF_SAMPLE_DURATION_MS,
    OUTPUT_PROOF_CAPTURE_HEADROOM_MS, SrtPassphrase, WorkerEvent, WorkerHeadendProfileId,
    WorkerHeadendTransportContract, WorkerOutputConfiguration,
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
    CgSnapshotReadError, CgWriteError, OutputLoudnessCaptureFacts, OutputLoudnessProofJobRecord,
    OutputLoudnessProofJobState, StationStore,
};

static ALERT_SEQUENCE: AtomicU64 = AtomicU64::new(1);
const HEARTBEAT_INTERVAL_MS: i64 = 5_000;
const MINIMUM_WORKER_BOOTSTRAP_TIMEOUT: Duration = Duration::from_secs(30);
const MEDIA_LOAD_TIMEOUT: Duration = Duration::from_secs(25);
const MEDIA_TEARDOWN_TIMEOUT: Duration = Duration::from_secs(30);
const CG_EXPIRER_OPERATOR_ID: &str = "stationd-cg-expirer";
const CG_SYNC_INTERVAL_MS: i64 = 1_000;
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
    candidate_authority: Option<Arc<dyn CandidateAuthority>>,
    last_output_bytes: Option<Vec<u64>>,
    last_output_audio_buffers: Option<Vec<u64>>,
    cg_state: CgSnapshotState,
    next_cg_sync_at_unix_ms: i64,
    live_control: Option<LiveControlState>,
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
        let supervisor = WorkerSupervisor::launch_with_outputs_and_roots(
            worker_executable,
            &worker_id,
            &channel.channel_id,
            journal_path,
            &outputs,
            &managed_cg_image_root(database_path),
            &managed_output_proof_root(database_path),
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
            candidate_authority: None,
            last_output_bytes: None,
            last_output_audio_buffers: None,
            cg_state: CgSnapshotState::default(),
            next_cg_sync_at_unix_ms: 0,
            live_control: None,
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
                self.supervisor.force_terminate();
                Some(format!(
                    "worker proof abort was ambiguous and the worker was terminated: {error:?}"
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
                self.supervisor.force_terminate();
                self.fail_output_proof_job_after_cleanup(
                    store,
                    &job,
                    now_unix_ms,
                    format!(
                        "worker output proof start was ambiguous; worker terminated: {error:?}"
                    ),
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
            self.supervisor.force_terminate();
            self.fail_output_proof_job_after_cleanup(
                store,
                &job,
                now_unix_ms,
                "worker returned an invalid output proof start response and was terminated",
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
        self.output_proof_tick(&store, now_unix_ms)?;
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
                let event = self
                    .supervisor
                    .command(
                        &format!("take-{report_id}"),
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
                    return self.reject_worker_event(&store, &report_id, DispatchStatus::Queued);
                }
                self.advance(
                    &store,
                    &report_id,
                    DispatchStatus::Queued,
                    DispatchStatus::Acknowledged,
                )?;
                self.on_air_asset_id = Some(job.asset.asset_id);
                self.mark_state(ChannelRuntimeState::OnAir, now_unix_ms);
                Some(DispatchTransition::OnAir { report_id })
            }
            (DispatchStatus::Acknowledged, ScheduleWindow::Active { media_offset_ms })
                if self.on_air_asset_id.as_deref() != Some(job.asset.asset_id.as_str()) =>
            {
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
                self.on_air_asset_id = Some(job.asset.asset_id);
                self.mark_state(ChannelRuntimeState::OnAir, now_unix_ms);
                Some(DispatchTransition::OnAir { report_id })
            }
            (DispatchStatus::Acknowledged, ScheduleWindow::Ended) => {
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
                self.mark_state(ChannelRuntimeState::HealthyFallback, now_unix_ms);
                Some(DispatchTransition::Completed { report_id })
            }
            (
                DispatchStatus::Queued | DispatchStatus::Acknowledged,
                ScheduleWindow::Future | ScheduleWindow::Active { .. },
            ) => None,
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
            Err(_) => return Ok(()),
        };
        if !matches!(
            event.event,
            WorkerEvent::CgApplied {
                ref board_id,
                board_revision
            } if board_id == &protocol.board_id && board_revision == protocol.board_revision
        ) {
            return Ok(());
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
            Err(_) => {
                self.cg_state = candidate;
                return Ok(());
            }
        };
        if !matches!(event.event, WorkerEvent::CgCleared) {
            self.cg_state = candidate;
            return Ok(());
        }
        candidate.mark_cleared();
        self.cg_state = candidate;
        Ok(())
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
            detail: None,
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
                let launch_result = ChannelController::launch(
                    worker_executable,
                    database_path,
                    &journal,
                    channel,
                    Duration::from_secs(7),
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
                        store
                            .put_channel_runtime(&controller.runtime_observation(dispatch_now))
                            .map_err(OrchestrationError::Storage)?;
                        controller.mark_heartbeat(dispatch_now);
                    }
                }
                Err(error) => {
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
                let detail = if channel.enabled {
                    "Output loudness proof failed because the channel has no active controller; restore channel output and submit a new proof job."
                } else {
                    "Output loudness proof failed because the channel was disabled; enable channel output and submit a new proof job."
                };
                fail_output_proofs_without_controller(
                    &store,
                    database_path,
                    &channel.channel_id,
                    dispatch_now,
                    detail,
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

#[cfg(test)]
mod tests {
    use super::{
        CgSnapshotState, OrchestrationError, OutputExpectation, capture_duration_for_sample,
        cleanup_orphan_output_proof_artifacts, expire_due_bulletins, generate_live_arm_token,
        hash_capture, is_abort_confirmation, lease_renewal_due, managed_cg_image_root,
        proof_artifact_path, proof_bindings_match, ready_duration_is_bounded,
        resolved_snapshot_to_protocol, stalled_required_output,
    };
    use station_cg::{
        CgAuditEvent, CgAuditEventKind, CgBoard, CgBulletin, CgBulletinContent, CgBulletinState,
        CgZoneConfig,
    };
    use station_cg::{
        CgContentSource, CgRect, CgResolvedContent, CgResolvedSnapshot, CgResolvedZone,
        CgTemplateKind, CgZoneKind,
    };
    use station_media_protocol::WorkerEvent;
    use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
    use station_storage::{OutputLoudnessProofJob, OutputLoudnessProofJobState, StationStore};
    use std::collections::BTreeMap;
    use std::path::PathBuf;
    use std::sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    };
    use std::time::{SystemTime, UNIX_EPOCH};

    const CHANNEL_ID: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const OTHER_CHANNEL_ID: &str = "9d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const BOARD_ID: &str = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";
    const ZONE_ID: &str = "8f4f0f70-5c30-48c6-819a-fb22b34353c5";
    const BULLETIN_ID: &str = "597f52ae-f16c-4ff8-ab32-60379cd950c4";
    const OTHER_BULLETIN_ID: &str = "697f52ae-f16c-48f8-ab32-60379cd950c4";
    const OUTPUT_PROOF_JOB_ID: &str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
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
        assert_eq!(capture_duration_for_sample(300_000), Some(304_000));
        assert_eq!(capture_duration_for_sample(300_001), None);

        assert!(!ready_duration_is_bounded(6_999, 7_000));
        assert!(ready_duration_is_bounded(7_000, 7_000));
        assert!(ready_duration_is_bounded(7_250, 7_000));
        assert!(!ready_duration_is_bounded(304_001, 7_000));
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
}
