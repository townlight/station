use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use station_media_protocol::{ChannelCommand, WorkerEvent};
use station_runtime::{SupervisorError, WorkerSupervisor};
use station_schedule::{ChannelConfiguration, DispatchStatus};
use station_storage::StationStore;

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
    Clock(String),
}

pub struct ChannelController {
    supervisor: WorkerSupervisor,
    database_path: PathBuf,
    channel_id: String,
    command_timeout: Duration,
    loaded_asset_id: Option<String>,
    on_air_asset_id: Option<String>,
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
        let destination: SocketAddr = channel
            .udp_destination
            .parse()
            .map_err(|_| OrchestrationError::InvalidDestination(channel.udp_destination.clone()))?;
        let worker_id = format!("station-worker-{}", channel.channel_id);
        let supervisor = WorkerSupervisor::launch(
            worker_executable,
            &worker_id,
            &channel.channel_id,
            journal_path,
            destination,
            timeout,
        )
        .map_err(OrchestrationError::Worker)?;
        Ok(Self {
            supervisor,
            database_path: database_path.to_path_buf(),
            channel_id: channel.channel_id.clone(),
            command_timeout: timeout,
            loaded_asset_id: None,
            on_air_asset_id: None,
        })
    }

    pub fn run_once(
        &mut self,
        now_unix_ms: i64,
    ) -> Result<Option<DispatchTransition>, OrchestrationError> {
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
    while !stop.load(Ordering::Acquire) {
        let store = StationStore::open(database_path).map_err(OrchestrationError::Storage)?;
        let channels = store.list_channels().map_err(OrchestrationError::Storage)?;
        drop(store);
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
                let _ = controller.shutdown(Duration::from_secs(3));
            }
        }
        for channel in channels.into_iter().filter(|channel| channel.enabled) {
            if !controllers.contains_key(&channel.channel_id) {
                let journal = journal_root.join(format!("{}.tlj", channel.channel_id));
                let controller = match ChannelController::launch(
                    worker_executable,
                    database_path,
                    &journal,
                    &channel,
                    Duration::from_secs(7),
                ) {
                    Ok(controller) => controller,
                    Err(_) => continue,
                };
                controllers.insert(channel.channel_id.clone(), controller);
            }
        }
        let now = system_time_millis()?;
        let mut failed = Vec::new();
        for (channel_id, controller) in &mut controllers {
            if controller.run_once(now).is_err() {
                failed.push(channel_id.clone());
            }
        }
        for channel_id in failed {
            controllers.remove(&channel_id);
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    for (_, controller) in controllers {
        controller.shutdown(Duration::from_secs(3))?;
    }
    Ok(())
}

fn system_time_millis() -> Result<i64, OrchestrationError> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| OrchestrationError::Clock(error.to_string()))?
        .as_millis();
    i64::try_from(millis).map_err(|_| OrchestrationError::Clock("system time exceeds i64".into()))
}
