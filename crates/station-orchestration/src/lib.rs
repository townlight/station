use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use station_domain::{
    AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity, ChannelRuntimeObservation,
    ChannelRuntimeState,
};
use station_media_protocol::{
    ChannelCommand, SrtPassphrase, WorkerEvent, WorkerOutputConfiguration,
};
use station_runtime::{SupervisorError, WorkerSupervisor};
use station_schedule::{ChannelConfiguration, DispatchStatus, OutputSinkKind};
use station_storage::StationStore;

static ALERT_SEQUENCE: AtomicU64 = AtomicU64::new(1);
const HEARTBEAT_INTERVAL_MS: i64 = 5_000;

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
        let supervisor = WorkerSupervisor::launch_with_outputs(
            worker_executable,
            &worker_id,
            &channel.channel_id,
            journal_path,
            &outputs,
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
        })
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
            controllers.remove(&channel_id);
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
    use super::{OutputExpectation, stalled_required_output};

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
}
