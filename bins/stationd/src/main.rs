mod auth_handoff;
mod config;

use std::ffi::OsString;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc, Mutex, OnceLock,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;

use config::{DaemonConfig, LaunchMode};
use station_api::{OrchestrationHealth, OrchestrationHealthStatus};
use station_materialization_control::{
    AutomaticMaterializationBackend, AutomaticMaterializationSupervisor, BackendReadError,
    ChannelCheckpoint, ChannelMaterializationError, ClockFailure, MaterializationAlertError,
    MaterializationApplyOutcome, SYSTEM_ACTOR_ID, SupervisorConfig, UtcClock,
    ValidatedRunCheckpoint,
};
use station_orchestration::CandidateAuthority;
use station_readiness::CandidateIdentity;
use station_source_evidence::StationSourceEvidenceAuthority;
use station_storage::{
    ScheduleMaterializationApplyOutcome, ScheduleMaterializationFailureClass,
    ScheduleMaterializationRun, ScheduleMaterializationWriteError, StationStore,
};
use windows_service::define_windows_service;
use windows_service::service::{
    ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus, ServiceType,
};
use windows_service::service_control_handler::{
    self, ServiceControlHandlerResult, ServiceStatusHandle,
};
use windows_service::service_dispatcher;

const SERVICE_NAME: &str = "TownLightStation";
const SERVICE_TYPE: ServiceType = ServiceType::OWN_PROCESS;
const ORCHESTRATION_STALE_AFTER: Duration = Duration::from_secs(45);
const ORCHESTRATION_STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const ORCHESTRATION_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(35);
const WORKER_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(35);
static SERVICE_CONFIG: OnceLock<DaemonConfig> = OnceLock::new();

struct InstalledCandidateAuthority {
    data_directory: PathBuf,
    application_root: PathBuf,
}

#[derive(Clone, Copy)]
struct SystemUtcClock;

impl UtcClock for SystemUtcClock {
    fn now_utc_ms(&self) -> Result<i64, ClockFailure> {
        let elapsed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| ClockFailure::Unavailable)?;
        i64::try_from(elapsed.as_millis()).map_err(|_| ClockFailure::Overflow)
    }
}

#[derive(Clone)]
struct StationAutomaticMaterializationBackend {
    database: PathBuf,
}

impl AutomaticMaterializationBackend<StationSourceEvidenceAuthority>
    for StationAutomaticMaterializationBackend
{
    fn enabled_channel_checkpoints(&self) -> Result<Vec<ChannelCheckpoint>, BackendReadError> {
        let store =
            StationStore::open(&self.database).map_err(|_| BackendReadError::Unavailable)?;
        store
            .list_automatic_materialization_channel_checkpoints()
            .map_err(|_| BackendReadError::CorruptNewestCheckpoint)?
            .into_iter()
            .map(|checkpoint| {
                Ok(ChannelCheckpoint {
                    channel_id: checkpoint.channel_id.clone(),
                    last_successful_run: checkpoint
                        .latest_run
                        .map(|run| checkpoint_from_materialization_run(&checkpoint.channel_id, run))
                        .transpose()
                        .map_err(|_| BackendReadError::CorruptNewestCheckpoint)?,
                })
            })
            .collect()
    }

    fn materialize_channel(
        &self,
        channel_id: &str,
        actor_id: &str,
        boot_authority: &Arc<StationSourceEvidenceAuthority>,
    ) -> Result<MaterializationApplyOutcome, ChannelMaterializationError> {
        if actor_id != SYSTEM_ACTOR_ID {
            return Err(ChannelMaterializationError::InvalidDerivedInput);
        }
        let store =
            StationStore::open(&self.database).map_err(|_| ChannelMaterializationError::Storage)?;
        let outcome = store
            .materialize_scheduled_channel(channel_id, actor_id, boot_authority.as_ref())
            .map_err(map_materialization_write_error)?;
        match outcome {
            ScheduleMaterializationApplyOutcome::Applied(run) => {
                checkpoint_from_materialization_run(channel_id, run)
                    .map(MaterializationApplyOutcome::Applied)
            }
            ScheduleMaterializationApplyOutcome::AlreadyApplied(run) => {
                checkpoint_from_materialization_run(channel_id, run)
                    .map(MaterializationApplyOutcome::AlreadyApplied)
            }
        }
    }

    fn observe_materialization_failure(
        &self,
        channel_id: &str,
        error: ChannelMaterializationError,
        observed_at_utc_ms: i64,
    ) -> Result<(), MaterializationAlertError> {
        let store =
            StationStore::open(&self.database).map_err(|_| MaterializationAlertError::Storage)?;
        store
            .observe_schedule_materialization_failure_and_route(
                channel_id,
                materialization_failure_class(error),
                observed_at_utc_ms,
            )
            .map(|_| ())
            .map_err(|_| MaterializationAlertError::Storage)
    }

    fn resolve_materialization_failure(
        &self,
        channel_id: &str,
        resolved_at_utc_ms: i64,
    ) -> Result<(), MaterializationAlertError> {
        let store =
            StationStore::open(&self.database).map_err(|_| MaterializationAlertError::Storage)?;
        store
            .resolve_alert(
                station_domain::AlertConditionKind::ScheduleMaterializationFailure,
                channel_id,
                resolved_at_utc_ms,
            )
            .map(|_| ())
            .map_err(|_| MaterializationAlertError::Storage)
    }
}

fn checkpoint_from_materialization_run(
    channel_id: &str,
    run: ScheduleMaterializationRun,
) -> Result<ValidatedRunCheckpoint, ChannelMaterializationError> {
    if run.plans.iter().any(|plan| plan.channel_id != channel_id)
        || run
            .outcomes
            .iter()
            .any(|outcome| outcome.channel_id != channel_id)
    {
        return Err(ChannelMaterializationError::Integrity);
    }
    Ok(ValidatedRunCheckpoint {
        channel_id: channel_id.into(),
        run_id: run.run_id,
        result_sha256: run.result_sha256,
        completed_at_utc_ms: run.occurred_at_unix_ms,
    })
}

fn map_materialization_write_error(
    error: ScheduleMaterializationWriteError,
) -> ChannelMaterializationError {
    match error {
        ScheduleMaterializationWriteError::Invalid(_) => {
            ChannelMaterializationError::InvalidDerivedInput
        }
        ScheduleMaterializationWriteError::StaleAuthority(_) => {
            ChannelMaterializationError::StaleAuthority
        }
        ScheduleMaterializationWriteError::Collision(_) => ChannelMaterializationError::Integrity,
        ScheduleMaterializationWriteError::Capacity => ChannelMaterializationError::Capacity,
        ScheduleMaterializationWriteError::Storage(_) => ChannelMaterializationError::Storage,
    }
}

fn materialization_failure_class(
    error: ChannelMaterializationError,
) -> ScheduleMaterializationFailureClass {
    match error {
        ChannelMaterializationError::ChannelDisabled => {
            ScheduleMaterializationFailureClass::ChannelDisabled
        }
        ChannelMaterializationError::StaleAuthority => {
            ScheduleMaterializationFailureClass::StaleAuthority
        }
        ChannelMaterializationError::InvalidDerivedInput => {
            ScheduleMaterializationFailureClass::InvalidDerivedInput
        }
        ChannelMaterializationError::Capacity => ScheduleMaterializationFailureClass::Capacity,
        ChannelMaterializationError::Integrity => ScheduleMaterializationFailureClass::Integrity,
        ChannelMaterializationError::Storage => ScheduleMaterializationFailureClass::Storage,
    }
}

impl CandidateAuthority for InstalledCandidateAuthority {
    fn current_candidate(&self) -> Result<CandidateIdentity, String> {
        let identity = station_maintenance::read_verified_candidate_identity(
            &self.data_directory,
            &self.application_root,
        )?;
        Ok(CandidateIdentity {
            version: identity.version,
            source_commit: Some(identity.source_commit),
        })
    }
}

define_windows_service!(ffi_service_main, service_main);

fn main() {
    let config =
        config::parse(std::env::args_os().skip(1)).unwrap_or_else(|message| fatal(&message));
    let result = match config.mode.clone() {
        LaunchMode::Console => run_console(config),
        LaunchMode::Service => run_dispatcher(config),
        LaunchMode::AuthIssueBootstrapHandoff { handoff } => {
            auth_handoff::issue_bootstrap_handoff(&config.database, &handoff)
        }
        LaunchMode::ReleaseWriteRuntimeManifest {
            runtime_root,
            output,
            version,
            source_commit,
            runtime_package_sha256,
        } => station_maintenance::write_installed_runtime_manifest(
            &runtime_root,
            &output,
            &version,
            &source_commit,
            &runtime_package_sha256,
        )
        .map(|_| ()),
    };
    if let Err(message) = result {
        fatal(&message);
    }
}

fn run_console(config: DaemonConfig) -> Result<(), String> {
    let listener = bind(&config)?;
    initialize_database(&config.database)?;
    let source_evidence_authority =
        Arc::new(StationSourceEvidenceAuthority::new().map_err(|error| error.to_string())?);
    let worker_executable = worker_executable()?;
    let update_application = application_directory()?;
    let api_application = update_application.clone();
    let caption_decoder = caption_decoder_executable(&update_application);
    let proof_candidate_authority =
        installed_candidate_authority(&config.database, update_application.clone())?;
    let stop = Arc::new(AtomicBool::new(false));
    let orchestration_health = Arc::new(OrchestrationHealth::new(ORCHESTRATION_STALE_AFTER));
    let orchestration_database = config.database.clone();
    let orchestration = spawn_supervised_orchestration(
        Arc::clone(&stop),
        Arc::clone(&orchestration_health),
        None,
        move |orchestration_stop, health| {
            let reconciled_health = Arc::clone(&health);
            station_orchestration::run_until_with_proof_authorities_and_liveness(
                &orchestration_database,
                &worker_executable,
                orchestration_stop,
                Some(proof_candidate_authority),
                Some(caption_decoder),
                move || health.mark_alive(),
                move || reconciled_health.mark_progress(),
            )
            .map_err(|error| format!("channel orchestration stopped: {error:?}"))
        },
    )?;
    if let Err(startup_error) =
        wait_for_orchestration_ready(&orchestration_health, &stop, ORCHESTRATION_STARTUP_TIMEOUT)
    {
        stop.store(true, Ordering::Release);
        let cleanup = join_worker(
            "channel orchestration",
            orchestration,
            orchestration_join_timeout(&orchestration_health),
        );
        return Err(append_cleanup_error(startup_error, cleanup.result));
    }
    let feed_receipt = config.database.with_file_name("feed-fetch-error.txt");
    let feed_fetch =
        match spawn_feed_fetch_worker(&config.database, Arc::clone(&stop), Some(feed_receipt)) {
            Ok(worker) => worker,
            Err(error) => {
                stop.store(true, Ordering::Release);
                let cleanup = join_worker(
                    "channel orchestration",
                    orchestration,
                    orchestration_join_timeout(&orchestration_health),
                );
                return Err(append_cleanup_error(error, cleanup.result));
            }
        };
    let automatic_receipt = config
        .database
        .with_file_name("automatic-materialization-error.txt");
    let automatic_materialization = match spawn_station_automatic_materialization(
        &config.database,
        Arc::clone(&stop),
        Arc::clone(&source_evidence_authority),
        Some(automatic_receipt),
    ) {
        Ok(worker) => worker,
        Err(error) => {
            stop.store(true, Ordering::Release);
            let cleanup = combine_worker_joins(vec![
                join_worker(
                    "channel orchestration",
                    orchestration,
                    orchestration_join_timeout(&orchestration_health),
                ),
                join_worker("feed fetch worker", feed_fetch, WORKER_SHUTDOWN_TIMEOUT),
            ]);
            return Err(append_cleanup_error(error, cleanup.result));
        }
    };
    let transport_queue = spawn_transport_proof_queue_controller(
        &config.database,
        &update_application,
        Arc::clone(&stop),
    )?;
    let ingest_database = config.database.clone();
    let ingest_library = media_library(&config.database);
    let ingest_stop = Arc::clone(&stop);
    let ingest = std::thread::spawn(move || {
        station_ingest::run_until(&ingest_database, &ingest_library, ingest_stop)
    });
    let alerting_database = config.database.clone();
    let alerting_stop = Arc::clone(&stop);
    let alerting =
        std::thread::spawn(move || station_alerting::run_until(&alerting_database, alerting_stop));
    let update_database = config.database.clone();
    let update_stop = Arc::clone(&stop);
    let updates = std::thread::spawn(move || {
        station_update_coordinator::run_until(&update_database, &update_application, update_stop)
    });
    println!(
        "TownLight Station is listening on http://{}",
        config.address
    );
    let result = station_api::serve_until_with_application_directory_and_health_and_source_evidence_authority(
        listener,
        &config.database,
        api_application,
        Arc::clone(&stop),
        Arc::clone(&orchestration_health),
        source_evidence_authority,
    );
    stop.store(true, Ordering::Release);
    let worker_cleanup = combine_worker_joins(vec![
        join_worker(
            "channel orchestration",
            orchestration,
            orchestration_join_timeout(&orchestration_health),
        ),
        join_worker("media ingest", ingest, WORKER_SHUTDOWN_TIMEOUT),
        join_worker("operator alerting", alerting, WORKER_SHUTDOWN_TIMEOUT),
        join_worker("update monitor", updates, WORKER_SHUTDOWN_TIMEOUT),
        join_worker(
            "transport proof queue",
            transport_queue,
            WORKER_SHUTDOWN_TIMEOUT,
        ),
        join_worker("feed fetch worker", feed_fetch, WORKER_SHUTDOWN_TIMEOUT),
        join_worker(
            "automatic materialization",
            automatic_materialization,
            WORKER_SHUTDOWN_TIMEOUT,
        ),
    ]);
    result.and(worker_cleanup.result)
}

fn run_dispatcher(config: DaemonConfig) -> Result<(), String> {
    SERVICE_CONFIG
        .set(config)
        .map_err(|_| "service configuration was initialized twice".to_string())?;
    service_dispatcher::start(SERVICE_NAME, ffi_service_main).map_err(|error| error.to_string())
}

fn service_main(_arguments: Vec<OsString>) {
    let config = SERVICE_CONFIG
        .get()
        .expect("service configuration is set before dispatch")
        .clone();
    if let Err(message) = run_service(config.clone()) {
        let receipt = config.database.with_file_name("stationd-startup-error.txt");
        let _ = std::fs::write(receipt, message);
        std::process::exit(1);
    }
}

fn run_service(config: DaemonConfig) -> Result<(), String> {
    let stop = Arc::new(AtomicBool::new(false));
    let orchestration_health = Arc::new(OrchestrationHealth::new(ORCHESTRATION_STALE_AFTER));
    let handler_stop = Arc::clone(&stop);
    let status_slot: Arc<Mutex<Option<ServiceStatusHandle>>> = Arc::new(Mutex::new(None));
    let handler_status = Arc::clone(&status_slot);
    let event_handler = move |event| match event {
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        ServiceControl::Stop | ServiceControl::Shutdown => {
            if let Ok(status) = handler_status.lock()
                && let Some(status) = status.as_ref()
            {
                let _ = status.set_service_status(service_status(
                    ServiceState::StopPending,
                    ServiceControlAccept::empty(),
                    1,
                    Duration::from_secs(7),
                    ServiceExitCode::NO_ERROR,
                ));
            }
            handler_stop.store(true, Ordering::Release);
            ServiceControlHandlerResult::NoError
        }
        _ => ServiceControlHandlerResult::NotImplemented,
    };
    let status = service_control_handler::register(SERVICE_NAME, event_handler)
        .map_err(|error| error.to_string())?;
    *status_slot
        .lock()
        .map_err(|_| "service status lock was poisoned".to_string())? = Some(status);
    status
        .set_service_status(service_status(
            ServiceState::StartPending,
            ServiceControlAccept::empty(),
            1,
            ORCHESTRATION_STARTUP_TIMEOUT + Duration::from_secs(5),
            ServiceExitCode::NO_ERROR,
        ))
        .map_err(|error| error.to_string())?;

    let listener = match bind(&config) {
        Ok(listener) => listener,
        Err(message) => {
            let _ = status.set_service_status(service_status(
                ServiceState::Stopped,
                ServiceControlAccept::empty(),
                0,
                Duration::ZERO,
                ServiceExitCode::ServiceSpecific(1),
            ));
            return Err(message);
        }
    };
    initialize_database(&config.database)?;
    let source_evidence_authority =
        Arc::new(StationSourceEvidenceAuthority::new().map_err(|error| error.to_string())?);
    let worker_executable = worker_executable()?;
    let update_application = application_directory()?;
    let api_application = update_application.clone();
    let caption_decoder = caption_decoder_executable(&update_application);
    let proof_candidate_authority =
        installed_candidate_authority(&config.database, update_application.clone())?;
    let orchestration_database = config.database.clone();
    let orchestration_receipt = config.database.with_file_name("orchestration-error.txt");
    let orchestration = spawn_supervised_orchestration(
        Arc::clone(&stop),
        Arc::clone(&orchestration_health),
        Some(orchestration_receipt),
        move |orchestration_stop, health| {
            let reconciled_health = Arc::clone(&health);
            station_orchestration::run_until_with_proof_authorities_and_liveness(
                &orchestration_database,
                &worker_executable,
                orchestration_stop,
                Some(proof_candidate_authority),
                Some(caption_decoder),
                move || health.mark_alive(),
                move || reconciled_health.mark_progress(),
            )
            .map_err(|error| format!("channel orchestration stopped: {error:?}"))
        },
    )?;
    if let Err(startup_error) =
        wait_for_orchestration_ready(&orchestration_health, &stop, ORCHESTRATION_STARTUP_TIMEOUT)
    {
        stop.store(true, Ordering::Release);
        let cleanup = join_worker(
            "channel orchestration",
            orchestration,
            orchestration_join_timeout(&orchestration_health),
        );
        if cleanup.joined {
            let _ = status.set_service_status(service_status(
                ServiceState::Stopped,
                ServiceControlAccept::empty(),
                0,
                Duration::ZERO,
                ServiceExitCode::ServiceSpecific(1),
            ));
        }
        return Err(append_cleanup_error(startup_error, cleanup.result));
    }
    let feed_receipt = config.database.with_file_name("feed-fetch-error.txt");
    let feed_fetch =
        match spawn_feed_fetch_worker(&config.database, Arc::clone(&stop), Some(feed_receipt)) {
            Ok(worker) => worker,
            Err(error) => {
                stop.store(true, Ordering::Release);
                let cleanup = join_worker(
                    "channel orchestration",
                    orchestration,
                    orchestration_join_timeout(&orchestration_health),
                );
                if cleanup.joined {
                    let _ = status.set_service_status(service_status(
                        ServiceState::Stopped,
                        ServiceControlAccept::empty(),
                        0,
                        Duration::ZERO,
                        ServiceExitCode::ServiceSpecific(1),
                    ));
                }
                return Err(append_cleanup_error(error, cleanup.result));
            }
        };
    let automatic_receipt = config
        .database
        .with_file_name("automatic-materialization-error.txt");
    let automatic_materialization = match spawn_station_automatic_materialization(
        &config.database,
        Arc::clone(&stop),
        Arc::clone(&source_evidence_authority),
        Some(automatic_receipt),
    ) {
        Ok(worker) => worker,
        Err(error) => {
            stop.store(true, Ordering::Release);
            let cleanup = combine_worker_joins(vec![
                join_worker(
                    "channel orchestration",
                    orchestration,
                    orchestration_join_timeout(&orchestration_health),
                ),
                join_worker("feed fetch worker", feed_fetch, WORKER_SHUTDOWN_TIMEOUT),
            ]);
            if cleanup.joined {
                let _ = status.set_service_status(service_status(
                    ServiceState::Stopped,
                    ServiceControlAccept::empty(),
                    0,
                    Duration::ZERO,
                    ServiceExitCode::ServiceSpecific(1),
                ));
            }
            return Err(append_cleanup_error(error, cleanup.result));
        }
    };
    let transport_queue = spawn_transport_proof_queue_controller(
        &config.database,
        &update_application,
        Arc::clone(&stop),
    )?;
    let ingest_database = config.database.clone();
    let ingest_library = media_library(&config.database);
    let ingest_stop = Arc::clone(&stop);
    let ingest_receipt = config.database.with_file_name("ingest-error.txt");
    let ingest = std::thread::spawn(move || {
        let result = station_ingest::run_until(&ingest_database, &ingest_library, ingest_stop);
        if let Err(error) = &result {
            let _ = std::fs::write(&ingest_receipt, format!("{error:?}"));
        }
        result
    });
    let alerting_database = config.database.clone();
    let alerting_stop = Arc::clone(&stop);
    let alerting_receipt = config.database.with_file_name("alerting-error.txt");
    let alerting = std::thread::spawn(move || {
        let result = station_alerting::run_until(&alerting_database, alerting_stop);
        if let Err(error) = &result {
            let _ = std::fs::write(&alerting_receipt, error);
        }
        result
    });
    let update_database = config.database.clone();
    let update_stop = Arc::clone(&stop);
    let update_receipt = config.database.with_file_name("update-monitor-error.txt");
    let updates = std::thread::spawn(move || {
        let result = station_update_coordinator::run_until(
            &update_database,
            &update_application,
            update_stop,
        );
        if let Err(error) = &result {
            let _ = std::fs::write(&update_receipt, error);
        }
        result
    });
    if let Err(error) = status.set_service_status(service_status(
        ServiceState::Running,
        ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
        0,
        Duration::ZERO,
        ServiceExitCode::NO_ERROR,
    )) {
        stop.store(true, Ordering::Release);
        let cleanup = combine_worker_joins(vec![
            join_worker(
                "channel orchestration",
                orchestration,
                orchestration_join_timeout(&orchestration_health),
            ),
            join_worker("media ingest", ingest, WORKER_SHUTDOWN_TIMEOUT),
            join_worker("operator alerting", alerting, WORKER_SHUTDOWN_TIMEOUT),
            join_worker("update monitor", updates, WORKER_SHUTDOWN_TIMEOUT),
            join_worker(
                "transport proof queue",
                transport_queue,
                WORKER_SHUTDOWN_TIMEOUT,
            ),
            join_worker("feed fetch worker", feed_fetch, WORKER_SHUTDOWN_TIMEOUT),
            join_worker(
                "automatic materialization",
                automatic_materialization,
                WORKER_SHUTDOWN_TIMEOUT,
            ),
        ]);
        if cleanup.joined {
            let _ = status.set_service_status(service_status(
                ServiceState::Stopped,
                ServiceControlAccept::empty(),
                0,
                Duration::ZERO,
                ServiceExitCode::ServiceSpecific(1),
            ));
        }
        return Err(append_cleanup_error(error.to_string(), cleanup.result));
    }
    let result = station_api::serve_until_with_application_directory_and_health_and_source_evidence_authority(
        listener,
        &config.database,
        api_application,
        Arc::clone(&stop),
        Arc::clone(&orchestration_health),
        source_evidence_authority,
    );
    stop.store(true, Ordering::Release);
    let worker_cleanup = combine_worker_joins(vec![
        join_worker(
            "channel orchestration",
            orchestration,
            orchestration_join_timeout(&orchestration_health),
        ),
        join_worker("media ingest", ingest, WORKER_SHUTDOWN_TIMEOUT),
        join_worker("operator alerting", alerting, WORKER_SHUTDOWN_TIMEOUT),
        join_worker("update monitor", updates, WORKER_SHUTDOWN_TIMEOUT),
        join_worker(
            "transport proof queue",
            transport_queue,
            WORKER_SHUTDOWN_TIMEOUT,
        ),
        join_worker("feed fetch worker", feed_fetch, WORKER_SHUTDOWN_TIMEOUT),
        join_worker(
            "automatic materialization",
            automatic_materialization,
            WORKER_SHUTDOWN_TIMEOUT,
        ),
    ]);
    let result = result.and(worker_cleanup.result);
    if !worker_cleanup.joined {
        return result;
    }
    let exit_code = if result.is_ok() {
        ServiceExitCode::NO_ERROR
    } else {
        ServiceExitCode::ServiceSpecific(2)
    };
    status
        .set_service_status(service_status(
            ServiceState::Stopped,
            ServiceControlAccept::empty(),
            0,
            Duration::ZERO,
            exit_code,
        ))
        .map_err(|error| error.to_string())?;
    result
}

fn worker_executable() -> Result<std::path::PathBuf, String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("could not locate stationd: {error}"))?
        .with_file_name("channel-worker.exe");
    if executable.is_file() {
        Ok(executable)
    } else {
        Err(format!(
            "channel worker is missing: {}",
            executable.display()
        ))
    }
}

fn application_directory() -> Result<std::path::PathBuf, String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("could not locate station application: {error}"))?;
    application_root_for_executable(&executable)
}

fn caption_decoder_executable(application_root: &Path) -> PathBuf {
    application_root
        .join("runtime")
        .join("caption-decoder")
        .join("ffmpeg.exe")
}

fn installed_candidate_authority(
    database: &Path,
    application_root: PathBuf,
) -> Result<Arc<dyn CandidateAuthority>, String> {
    let data_directory = database
        .parent()
        .ok_or_else(|| "station database has no data directory".to_string())?
        .to_path_buf();
    Ok(Arc::new(InstalledCandidateAuthority {
        data_directory,
        application_root,
    }))
}

fn application_root_for_executable(
    executable: &std::path::Path,
) -> Result<std::path::PathBuf, String> {
    let directory = executable
        .parent()
        .ok_or_else(|| "station executable has no application directory".to_string())?;
    let slot_parent = directory.parent();
    if directory.join("slot-manifest.json").is_file()
        && slot_parent
            .and_then(std::path::Path::file_name)
            .is_some_and(|name| name.eq_ignore_ascii_case("slots"))
    {
        return slot_parent
            .and_then(std::path::Path::parent)
            .map(std::path::Path::to_path_buf)
            .ok_or_else(|| "station slot has no application root".into());
    }
    Ok(directory.to_path_buf())
}

fn media_library(database: &std::path::Path) -> std::path::PathBuf {
    database
        .parent()
        .unwrap_or_else(|| std::path::Path::new("."))
        .join("media")
}

fn spawn_supervised_orchestration<F>(
    stop: Arc<AtomicBool>,
    health: Arc<OrchestrationHealth>,
    receipt: Option<PathBuf>,
    run: F,
) -> Result<std::thread::JoinHandle<Result<(), String>>, String>
where
    F: FnOnce(Arc<AtomicBool>, Arc<OrchestrationHealth>) -> Result<(), String> + Send + 'static,
{
    let worker_stop = Arc::clone(&stop);
    let worker_health = Arc::clone(&health);
    std::thread::Builder::new()
        .name("station-orchestration".into())
        .spawn(move || {
            let result = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                run(Arc::clone(&worker_stop), Arc::clone(&worker_health))
            })) {
                Ok(Ok(())) if worker_stop.load(Ordering::Acquire) => Ok(()),
                Ok(Ok(())) => Err("channel orchestration stopped unexpectedly".into()),
                Ok(Err(error)) => Err(error),
                Err(_) => Err("channel orchestration thread panicked".into()),
            };
            if let Err(error) = &result {
                worker_health.mark_failed();
                worker_stop.store(true, Ordering::Release);
                if let Some(receipt) = receipt {
                    let _ = std::fs::write(receipt, error);
                }
            }
            result
        })
        .map_err(|error| format!("could not start channel orchestration thread: {error}"))
}

fn spawn_supervised_automatic_materialization<A, F>(
    stop: Arc<AtomicBool>,
    authority: Arc<A>,
    receipt: Option<PathBuf>,
    run: F,
) -> Result<std::thread::JoinHandle<Result<(), String>>, String>
where
    A: Send + Sync + 'static,
    F: FnOnce(Arc<AtomicBool>, Arc<A>) -> Result<(), String> + Send + 'static,
{
    let worker_stop = Arc::clone(&stop);
    std::thread::Builder::new()
        .name("station-automatic-materialization".into())
        .spawn(move || {
            let result = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                run(Arc::clone(&worker_stop), authority)
            })) {
                Ok(Ok(())) if worker_stop.load(Ordering::Acquire) => Ok(()),
                Ok(Ok(())) => Err("automatic materialization stopped unexpectedly".into()),
                Ok(Err(_)) => Err("automatic materialization controller failed".into()),
                Err(_) => Err("automatic materialization thread panicked".into()),
            };
            if let Err(error) = &result {
                worker_stop.store(true, Ordering::Release);
                if let Some(receipt) = receipt {
                    let _ = std::fs::write(receipt, error);
                }
            }
            result
        })
        .map_err(|error| format!("could not start automatic materialization thread: {error}"))
}

fn spawn_station_automatic_materialization(
    database: &Path,
    stop: Arc<AtomicBool>,
    authority: Arc<StationSourceEvidenceAuthority>,
    receipt: Option<PathBuf>,
) -> Result<std::thread::JoinHandle<Result<(), String>>, String> {
    let backend = StationAutomaticMaterializationBackend {
        database: database.to_path_buf(),
    };
    spawn_supervised_automatic_materialization(
        stop,
        authority,
        receipt,
        move |worker_stop, boot_authority| {
            let mut supervisor = AutomaticMaterializationSupervisor::new(
                SystemUtcClock,
                backend,
                boot_authority,
                SupervisorConfig::default(),
            )
            .map_err(|_| "automatic materialization configuration failed".to_string())?;
            supervisor
                .run_until_stopped(&worker_stop)
                .map_err(|_| "automatic materialization controller failed".to_string())
        },
    )
}

fn wait_for_orchestration_ready(
    health: &OrchestrationHealth,
    stop: &AtomicBool,
    timeout: Duration,
) -> Result<(), String> {
    let started = std::time::Instant::now();
    loop {
        if stop.load(Ordering::Acquire) {
            return Err(
                "service stop was requested before channel orchestration became ready".into(),
            );
        }
        match health.status() {
            OrchestrationHealthStatus::Ready => return Ok(()),
            OrchestrationHealthStatus::Failed => {
                return Err("channel orchestration failed before service readiness".into());
            }
            OrchestrationHealthStatus::Stale => {
                return Err("channel orchestration became stale before service readiness".into());
            }
            OrchestrationHealthStatus::Starting => {}
        }
        if started.elapsed() >= timeout {
            health.mark_failed();
            stop.store(true, Ordering::Release);
            return Err(
                "channel orchestration did not become ready before its startup deadline".into(),
            );
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn orchestration_join_timeout(health: &OrchestrationHealth) -> Duration {
    if health.status() == OrchestrationHealthStatus::Stale {
        Duration::from_secs(1)
    } else {
        ORCHESTRATION_SHUTDOWN_TIMEOUT
    }
}

struct WorkerJoinOutcome {
    joined: bool,
    result: Result<(), String>,
}

fn combine_worker_joins(outcomes: Vec<WorkerJoinOutcome>) -> WorkerJoinOutcome {
    let joined = outcomes.iter().all(|outcome| outcome.joined);
    let result = outcomes
        .into_iter()
        .try_fold((), |(), outcome| outcome.result);
    WorkerJoinOutcome { joined, result }
}

fn append_cleanup_error(primary: String, cleanup: Result<(), String>) -> String {
    match cleanup {
        Ok(()) => primary,
        Err(cleanup_error) => format!("{primary}; {cleanup_error}"),
    }
}

fn join_worker<E>(
    name: &'static str,
    worker: std::thread::JoinHandle<Result<(), E>>,
    timeout: Duration,
) -> WorkerJoinOutcome
where
    E: std::fmt::Debug + Send + 'static,
{
    let started = std::time::Instant::now();
    while !worker.is_finished() {
        if started.elapsed() >= timeout {
            return WorkerJoinOutcome {
                joined: false,
                result: Err(format!("{name} did not stop before its shutdown deadline")),
            };
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let result = match worker.join() {
        Ok(Ok(())) => Ok(()),
        Ok(Err(error)) => Err(format!("{name} stopped: {error:?}")),
        Err(_) => Err(format!("{name} thread panicked")),
    };
    WorkerJoinOutcome {
        joined: true,
        result,
    }
}

fn spawn_feed_fetch_worker(
    database: &std::path::Path,
    stop: Arc<AtomicBool>,
    receipt: Option<std::path::PathBuf>,
) -> Result<std::thread::JoinHandle<Result<(), String>>, String> {
    let database = database.to_path_buf();
    let worker_stop = Arc::clone(&stop);
    std::thread::Builder::new()
        .name("station-feed-fetch".into())
        .spawn(move || {
            let result = station_feed_fetch::run_until(&database, stop);
            if let (Err(error), Some(receipt)) = (&result, receipt) {
                let _ = std::fs::write(receipt, error);
            }
            if result.is_err() {
                worker_stop.store(true, Ordering::Release);
            }
            result
        })
        .map_err(|error| format!("could not start feed fetch worker thread: {error}"))
}

fn spawn_transport_proof_queue_controller(
    database: &std::path::Path,
    application_root: &std::path::Path,
    stop: Arc<AtomicBool>,
) -> Result<std::thread::JoinHandle<Result<(), String>>, String> {
    let database = database.to_path_buf();
    let application_root = application_root.to_path_buf();
    std::thread::Builder::new()
        .name("station-transport-proof-queue".into())
        .spawn(move || {
            station_transport_executor::run_transport_proof_queue_controller(
                &database,
                &application_root,
                stop,
            )
        })
        .map_err(|error| format!("could not start transport proof queue controller: {error}"))
}

fn initialize_database(database: &std::path::Path) -> Result<(), String> {
    let store = StationStore::open(database)?;
    drop(store);
    Ok(())
}

fn bind(config: &DaemonConfig) -> Result<TcpListener, String> {
    if let Some(parent) = config.database.parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent).map_err(|error| {
            format!("TownLight Station could not create its data directory: {error}")
        })?;
    }
    TcpListener::bind(config.address).map_err(|error| {
        format!(
            "TownLight Station could not listen on {}: {error}",
            config.address
        )
    })
}

fn service_status(
    current_state: ServiceState,
    controls_accepted: ServiceControlAccept,
    checkpoint: u32,
    wait_hint: Duration,
    exit_code: ServiceExitCode,
) -> ServiceStatus {
    ServiceStatus {
        service_type: SERVICE_TYPE,
        current_state,
        controls_accepted,
        exit_code,
        checkpoint,
        wait_hint,
        process_id: None,
    }
}

fn fatal(message: &str) -> ! {
    eprintln!("TownLight Station stopped: {message}");
    std::process::exit(1);
}

#[cfg(test)]
mod startup_tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn orchestration_error_fails_current_boot_health_and_requests_service_stop() {
        let stop = Arc::new(AtomicBool::new(false));
        let health = Arc::new(OrchestrationHealth::new(Duration::from_secs(45)));

        let worker = spawn_supervised_orchestration(
            Arc::clone(&stop),
            Arc::clone(&health),
            None,
            |_stop, _health| Err("injected orchestration failure".into()),
        )
        .unwrap();
        let result = worker.join().unwrap();

        assert_eq!(result, Err("injected orchestration failure".into()));
        assert_eq!(health.status(), OrchestrationHealthStatus::Failed);
        assert!(stop.load(Ordering::Acquire));
    }

    #[test]
    fn orchestration_panic_fails_current_boot_health_and_requests_service_stop() {
        let stop = Arc::new(AtomicBool::new(false));
        let health = Arc::new(OrchestrationHealth::new(Duration::from_secs(45)));

        let worker = spawn_supervised_orchestration(
            Arc::clone(&stop),
            Arc::clone(&health),
            None,
            |_stop, _health| panic!("injected orchestration panic"),
        )
        .unwrap();
        let result = worker.join().unwrap();

        assert_eq!(result, Err("channel orchestration thread panicked".into()));
        assert_eq!(health.status(), OrchestrationHealthStatus::Failed);
        assert!(stop.load(Ordering::Acquire));
    }

    #[test]
    fn automatic_materialization_error_requests_global_stop_and_redacts_receipt() {
        let stop = Arc::new(AtomicBool::new(false));
        let authority = Arc::new(7_u64);
        let root = std::env::temp_dir().join(format!(
            "townlight-materialization-supervision-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let receipt = root.join("automatic-materialization-error.txt");

        let worker = spawn_supervised_automatic_materialization(
            Arc::clone(&stop),
            authority,
            Some(receipt.clone()),
            |_stop, _authority| Err("secret database detail".into()),
        )
        .unwrap();
        let result = worker.join().unwrap();

        assert_eq!(
            result,
            Err("automatic materialization controller failed".into())
        );
        assert!(stop.load(Ordering::Acquire));
        assert_eq!(
            std::fs::read_to_string(&receipt).unwrap(),
            "automatic materialization controller failed"
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn automatic_materialization_reuses_exact_boot_authority_and_accepts_requested_stop() {
        let stop = Arc::new(AtomicBool::new(false));
        let authority = Arc::new(7_u64);
        let expected = Arc::as_ptr(&authority) as usize;
        let worker = spawn_supervised_automatic_materialization(
            Arc::clone(&stop),
            Arc::clone(&authority),
            None,
            move |worker_stop, worker_authority| {
                assert_eq!(Arc::as_ptr(&worker_authority) as usize, expected);
                worker_stop.store(true, Ordering::Release);
                Ok(())
            },
        )
        .unwrap();

        assert_eq!(worker.join().unwrap(), Ok(()));
        assert!(stop.load(Ordering::Acquire));
        assert_eq!(Arc::strong_count(&authority), 1);
    }

    #[test]
    fn automatic_materialization_panic_requests_stop_and_writes_bounded_receipt() {
        let stop = Arc::new(AtomicBool::new(false));
        let root = std::env::temp_dir().join(format!(
            "townlight-materialization-panic-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let receipt = root.join("automatic-materialization-error.txt");
        let worker = spawn_supervised_automatic_materialization(
            Arc::clone(&stop),
            Arc::new(()),
            Some(receipt.clone()),
            |_stop, _authority| panic!("sensitive panic payload"),
        )
        .unwrap();

        assert_eq!(
            worker.join().unwrap(),
            Err("automatic materialization thread panicked".into())
        );
        assert!(stop.load(Ordering::Acquire));
        assert_eq!(
            std::fs::read_to_string(&receipt).unwrap(),
            "automatic materialization thread panicked"
        );
        assert!(std::fs::metadata(&receipt).unwrap().len() < 128);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn automatic_materialization_early_success_is_a_global_failure() {
        let stop = Arc::new(AtomicBool::new(false));
        let worker = spawn_supervised_automatic_materialization(
            Arc::clone(&stop),
            Arc::new(()),
            None,
            |_stop, _authority| Ok(()),
        )
        .unwrap();

        assert_eq!(
            worker.join().unwrap(),
            Err("automatic materialization stopped unexpectedly".into())
        );
        assert!(stop.load(Ordering::Acquire));
    }

    #[test]
    fn automatic_materialization_storage_adapter_reads_one_empty_eligible_snapshot() {
        let root = std::env::temp_dir().join(format!(
            "townlight-materialization-adapter-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        initialize_database(&database).unwrap();
        let backend = StationAutomaticMaterializationBackend { database };

        assert_eq!(backend.enabled_channel_checkpoints().unwrap(), Vec::new());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn automatic_materialization_adapter_persists_typed_failure_and_resolves_it() {
        let root = std::env::temp_dir().join(format!(
            "townlight-materialization-alert-adapter-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        initialize_database(&database).unwrap();
        let backend = StationAutomaticMaterializationBackend {
            database: database.clone(),
        };
        let channel_id = "10000000-0000-4000-8000-000000000001";

        backend
            .observe_materialization_failure(
                channel_id,
                ChannelMaterializationError::Storage,
                1_000,
            )
            .unwrap();
        let event = StationStore::open(&database)
            .unwrap()
            .list_alert_events()
            .unwrap()
            .into_iter()
            .find(|event| event.resource_ref == channel_id)
            .unwrap();
        assert_eq!(
            event.condition,
            station_domain::AlertConditionKind::ScheduleMaterializationFailure
        );
        assert_eq!(
            event.detail,
            "Typed automatic materialization failure class: storage."
        );

        backend
            .resolve_materialization_failure(channel_id, 2_000)
            .unwrap();
        let resolved = StationStore::open(&database)
            .unwrap()
            .read_alert_event(&event.event_id)
            .unwrap()
            .unwrap();
        assert_eq!(resolved.state, station_domain::AlertEventState::Resolved);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn automatic_materialization_adapter_rejects_any_non_system_actor_before_storage() {
        let backend = StationAutomaticMaterializationBackend {
            database: PathBuf::from(r"Z:\does-not-exist\station.db"),
        };
        let authority = Arc::new(StationSourceEvidenceAuthority::new().unwrap());

        assert_eq!(
            backend.materialize_channel(
                "10000000-0000-4000-8000-000000000001",
                "browser-supplied-actor",
                &authority,
            ),
            Err(ChannelMaterializationError::InvalidDerivedInput)
        );
    }

    #[test]
    fn system_materialization_clock_is_nonnegative_utc_milliseconds() {
        assert!(SystemUtcClock.now_utc_ms().unwrap() > 1_500_000_000_000);
    }

    #[test]
    fn automatic_materialization_write_errors_keep_typed_isolation_contract() {
        assert_eq!(
            map_materialization_write_error(ScheduleMaterializationWriteError::Invalid(
                "redacted".into()
            )),
            ChannelMaterializationError::InvalidDerivedInput
        );
        assert_eq!(
            map_materialization_write_error(ScheduleMaterializationWriteError::StaleAuthority(
                "redacted".into()
            )),
            ChannelMaterializationError::StaleAuthority
        );
        assert_eq!(
            map_materialization_write_error(ScheduleMaterializationWriteError::Collision(
                "redacted".into()
            )),
            ChannelMaterializationError::Integrity
        );
        assert_eq!(
            map_materialization_write_error(ScheduleMaterializationWriteError::Capacity),
            ChannelMaterializationError::Capacity
        );
        assert_eq!(
            map_materialization_write_error(ScheduleMaterializationWriteError::Storage(
                "redacted".into()
            )),
            ChannelMaterializationError::Storage
        );
    }

    #[test]
    fn service_readiness_requires_current_boot_orchestration_progress() {
        let starting = OrchestrationHealth::new(Duration::from_secs(45));
        let stop = AtomicBool::new(false);

        assert!(wait_for_orchestration_ready(&starting, &stop, Duration::ZERO).is_err());
        assert_eq!(starting.status(), OrchestrationHealthStatus::Failed);

        let ready = OrchestrationHealth::new(Duration::from_secs(45));
        ready.mark_progress();
        let running = AtomicBool::new(false);
        wait_for_orchestration_ready(&ready, &running, Duration::ZERO).unwrap();

        let stopped = AtomicBool::new(true);
        assert!(wait_for_orchestration_ready(&ready, &stopped, Duration::ZERO).is_err());
    }

    #[test]
    fn worker_join_is_bounded_when_the_worker_does_not_stop() {
        let worker = std::thread::spawn(|| {
            std::thread::sleep(Duration::from_millis(50));
            Ok::<(), String>(())
        });

        let outcome = join_worker("test worker", worker, Duration::ZERO);

        assert!(!outcome.joined);
        assert_eq!(
            outcome.result,
            Err("test worker did not stop before its shutdown deadline".into())
        );
    }

    #[test]
    fn initializes_a_fresh_database_before_concurrent_subsystems_start() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-stationd-startup-{nonce}"));
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("station.db");
        initialize_database(&database).unwrap();
        let store = StationStore::open(&database).unwrap();
        assert!(store.list_channels().unwrap().is_empty());
        assert!(store.list_alert_events().unwrap().is_empty());
        drop(store);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn resolves_the_shared_application_root_when_running_from_an_immutable_slot() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-stationd-slot-{nonce}"));
        let slot = root
            .join("slots")
            .join("slot-0.2.0-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
        std::fs::create_dir_all(&slot).unwrap();
        std::fs::write(slot.join("slot-manifest.json"), b"{}").unwrap();
        assert_eq!(
            application_root_for_executable(&slot.join("stationd.exe")).unwrap(),
            root
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn caption_decoder_is_bound_to_the_private_application_runtime() {
        let application = PathBuf::from(r"C:\Program Files\TownLight Station");

        assert_eq!(
            caption_decoder_executable(&application),
            application
                .join("runtime")
                .join("caption-decoder")
                .join("ffmpeg.exe")
        );
    }
}
