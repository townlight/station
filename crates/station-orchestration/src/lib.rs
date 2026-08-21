use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use station_cg::{CgBulletinContent, CgResolvedContent, CgResolvedSnapshot};
use station_domain::{
    AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity, ChannelRuntimeObservation,
    ChannelRuntimeState,
};
use station_media_protocol::{
    CgImageFormat, CgOverlay, CgOverlayContent, CgRect, CgSnapshot, ChannelCommand, MAX_CG_SLOTS,
    MAX_CG_TEXT_BYTES, SrtPassphrase, WorkerEvent, WorkerOutputConfiguration,
};
use station_runtime::{SupervisorError, WorkerSupervisor};
use station_schedule::{ChannelConfiguration, DispatchStatus, OutputSinkKind};
use station_storage::{CgWriteError, StationStore};

static ALERT_SEQUENCE: AtomicU64 = AtomicU64::new(1);
const HEARTBEAT_INTERVAL_MS: i64 = 5_000;
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DispatchTransition {
    Loaded { report_id: String },
    OnAir { report_id: String },
    Completed { report_id: String },
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
    Clock(String),
    InvalidCgSnapshot(String),
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CgSnapshotState {
    last_valid: Option<(CgSnapshot, [u8; 32])>,
    last_applied: Option<(u64, [u8; 32])>,
}

impl CgSnapshotState {
    pub fn accept(
        &mut self,
        snapshot: &CgResolvedSnapshot,
    ) -> Result<CgSnapshot, OrchestrationError> {
        let protocol = resolved_snapshot_to_protocol(snapshot)?;
        let hash = snapshot_hash(&protocol)?;
        self.last_valid = Some((protocol.clone(), hash));
        Ok(protocol)
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

pub struct ChannelController {
    supervisor: WorkerSupervisor,
    database_path: PathBuf,
    channel_id: String,
    command_timeout: Duration,
    loaded_asset_id: Option<String>,
    on_air_asset_id: Option<String>,
    runtime_state: ChannelRuntimeState,
    state_started_at_unix_ms: i64,
    last_heartbeat_at_unix_ms: i64,
    output_expectations: Vec<OutputExpectation>,
    last_output_bytes: Option<Vec<u64>>,
    cg_state: CgSnapshotState,
    next_cg_sync_at_unix_ms: i64,
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
        let outputs = channel
            .output_sinks
            .iter()
            .filter(|sink| sink.enabled)
            .map(|sink| match sink.kind {
                OutputSinkKind::UdpTs => Ok(WorkerOutputConfiguration::UdpTs {
                    destination: sink.destination.clone(),
                }),
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
                        uri: sink.destination.clone(),
                        passphrase,
                    })
                }
            })
            .collect::<Result<Vec<_>, OrchestrationError>>()?;
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
        let worker_id = format!("station-worker-{}", channel.channel_id);
        let supervisor = WorkerSupervisor::launch_with_outputs_and_cg_image_root(
            worker_executable,
            &worker_id,
            &channel.channel_id,
            journal_path,
            &outputs,
            &managed_cg_image_root(database_path),
            channel.output_profile,
            timeout,
        )
        .map_err(OrchestrationError::Worker)?;
        let started_at_unix_ms = system_time_millis()?;
        Ok(Self {
            supervisor,
            database_path: database_path.to_path_buf(),
            channel_id: channel.channel_id.clone(),
            command_timeout: timeout,
            loaded_asset_id: None,
            on_air_asset_id: None,
            runtime_state: ChannelRuntimeState::HealthyFallback,
            state_started_at_unix_ms: started_at_unix_ms,
            last_heartbeat_at_unix_ms: started_at_unix_ms,
            output_expectations,
            last_output_bytes: None,
            cg_state: CgSnapshotState::default(),
            next_cg_sync_at_unix_ms: 0,
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
            .into_iter()
            .map(|output| output.bytes_sent_total)
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
        self.last_output_bytes = Some(current);
        Ok(())
    }

    pub fn run_once(
        &mut self,
        now_unix_ms: i64,
    ) -> Result<Option<DispatchTransition>, OrchestrationError> {
        self.supervisor
            .ensure_running()
            .map_err(OrchestrationError::Worker)?;
        let store = StationStore::open(&self.database_path).map_err(OrchestrationError::Storage)?;
        if now_unix_ms >= self.next_cg_sync_at_unix_ms {
            self.next_cg_sync_at_unix_ms = now_unix_ms.saturating_add(CG_SYNC_INTERVAL_MS);
            expire_due_bulletins(&store, &self.channel_id, now_unix_ms)?;
            self.sync_cg(&store, now_unix_ms)?;
        }
        let Some(job) = store
            .list_dispatch_jobs(&self.channel_id)
            .map_err(OrchestrationError::Storage)?
            .into_iter()
            .next()
        else {
            return Ok(None);
        };
        let report_id = job.report.report_id.clone();
        if matches!(
            job.report.dispatch_status,
            DispatchStatus::Queued | DispatchStatus::Acknowledged
        ) && self.loaded_asset_id.as_deref() != Some(job.asset.asset_id.as_str())
        {
            let event = self
                .supervisor
                .command(
                    &format!("reconcile-load-{report_id}"),
                    ChannelCommand::LoadAsset {
                        asset_id: job.asset.asset_id.clone(),
                        media_path: job.asset.media_path.clone(),
                    },
                    self.command_timeout,
                )
                .map_err(OrchestrationError::Worker)?;
            if !matches!(event.event, WorkerEvent::AssetLoaded { ref asset_id } if asset_id == &job.asset.asset_id)
            {
                return self.reject_worker_event(&store, &report_id, job.report.dispatch_status);
            }
            self.loaded_asset_id = Some(job.asset.asset_id);
            return Ok(Some(DispatchTransition::Loaded { report_id }));
        }
        let transition = match job.report.dispatch_status {
            DispatchStatus::Pending => {
                let event = self
                    .supervisor
                    .command(
                        &format!("load-{report_id}"),
                        ChannelCommand::LoadAsset {
                            asset_id: job.asset.asset_id.clone(),
                            media_path: job.asset.media_path.clone(),
                        },
                        self.command_timeout,
                    )
                    .map_err(OrchestrationError::Worker)?;
                if !matches!(event.event, WorkerEvent::AssetLoaded { ref asset_id } if asset_id == &job.asset.asset_id)
                {
                    return self.reject_worker_event(&store, &report_id, DispatchStatus::Pending);
                }
                self.advance(
                    &store,
                    &report_id,
                    DispatchStatus::Pending,
                    DispatchStatus::Queued,
                )?;
                self.loaded_asset_id = Some(job.asset.asset_id);
                Some(DispatchTransition::Loaded { report_id })
            }
            DispatchStatus::Queued if now_unix_ms >= job.item.starts_at_unix_ms => {
                let event = self
                    .supervisor
                    .command(
                        &format!("take-{report_id}"),
                        ChannelCommand::TakeAsset {
                            asset_id: job.asset.asset_id.clone(),
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
            DispatchStatus::Acknowledged
                if now_unix_ms
                    < job
                        .item
                        .ends_at_unix_ms()
                        .map_err(|error| OrchestrationError::Storage(format!("{error:?}")))?
                    && self.on_air_asset_id.as_deref() != Some(job.asset.asset_id.as_str()) =>
            {
                let event = self
                    .supervisor
                    .command(
                        &format!("reconcile-take-{report_id}"),
                        ChannelCommand::TakeAsset {
                            asset_id: job.asset.asset_id.clone(),
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
            DispatchStatus::Acknowledged
                if now_unix_ms
                    >= job
                        .item
                        .ends_at_unix_ms()
                        .map_err(|error| OrchestrationError::Storage(format!("{error:?}")))? =>
            {
                let event = self
                    .supervisor
                    .command(
                        &format!("complete-{report_id}"),
                        ChannelCommand::ReturnToSchedule,
                        self.command_timeout,
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
            DispatchStatus::Queued | DispatchStatus::Acknowledged => None,
            _ => None,
        };
        Ok(transition)
    }

    fn sync_cg(
        &mut self,
        store: &StationStore,
        now_unix_ms: i64,
    ) -> Result<(), OrchestrationError> {
        let inputs = match store.load_cg_snapshot_inputs(&self.channel_id, now_unix_ms) {
            Ok(inputs) => inputs,
            Err(_) => return Ok(()),
        };
        let resolved = match inputs.resolve() {
            Ok(snapshot) => snapshot,
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

    pub fn shutdown(self, timeout: Duration) -> Result<(), OrchestrationError> {
        self.supervisor
            .shutdown("orchestrator-shutdown", timeout)
            .map(|_| ())
            .map_err(OrchestrationError::Worker)
    }

    fn runtime_observation(&self, now_unix_ms: i64) -> ChannelRuntimeObservation {
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

pub fn run_until(
    database_path: &Path,
    worker_executable: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), OrchestrationError> {
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
            }
            restart_attempts.remove(&channel_id);
            restart_after.remove(&channel_id);
        }
        for channel in channels.into_iter().filter(|channel| channel.enabled) {
            if !controllers.contains_key(&channel.channel_id) {
                if restart_after
                    .get(&channel.channel_id)
                    .is_some_and(|retry_at| now < *retry_at)
                {
                    continue;
                }
                let journal = journal_root.join(format!("{}.tlj", channel.channel_id));
                let controller = match ChannelController::launch(
                    worker_executable,
                    database_path,
                    &journal,
                    &channel,
                    Duration::from_secs(7),
                ) {
                    Ok(controller) => controller,
                    Err(error) => {
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
    ) {
        vec![
            (
                AlertConditionKind::OutputStall,
                "A required channel output stopped advancing.",
            ),
            (
                AlertConditionKind::OffAir,
                "Enabled channel output is not on verified fallback or scheduled media.",
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
                "Enabled channel output is not on verified fallback or scheduled media.",
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
        CgSnapshotState, OrchestrationError, OutputExpectation, expire_due_bulletins,
        managed_cg_image_root, resolved_snapshot_to_protocol, stalled_required_output,
    };
    use station_cg::{
        CgAuditEvent, CgAuditEventKind, CgBoard, CgBulletin, CgBulletinContent, CgBulletinState,
        CgZoneConfig,
    };
    use station_cg::{
        CgContentSource, CgRect, CgResolvedContent, CgResolvedSnapshot, CgResolvedZone,
        CgTemplateKind, CgZoneKind,
    };
    use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
    use station_storage::StationStore;
    use std::collections::BTreeMap;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    const CHANNEL_ID: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const OTHER_CHANNEL_ID: &str = "9d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const BOARD_ID: &str = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";
    const ZONE_ID: &str = "8f4f0f70-5c30-48c6-819a-fb22b34353c5";
    const BULLETIN_ID: &str = "597f52ae-f16c-4ff8-ab32-60379cd950c4";
    const OTHER_BULLETIN_ID: &str = "697f52ae-f16c-48f8-ab32-60379cd950c4";

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

    fn remove_database(path: &std::path::Path) {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{}", path.display(), suffix));
        }
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
