mod config;

use std::ffi::OsString;
use std::net::TcpListener;
use std::sync::{
    Arc, Mutex, OnceLock,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;

use config::{DaemonConfig, LaunchMode};
use station_storage::StationStore;
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
static SERVICE_CONFIG: OnceLock<DaemonConfig> = OnceLock::new();

define_windows_service!(ffi_service_main, service_main);

fn main() {
    let config =
        config::parse(std::env::args_os().skip(1)).unwrap_or_else(|message| fatal(&message));
    let result = match config.mode {
        LaunchMode::Console => run_console(config),
        LaunchMode::Service => run_dispatcher(config),
    };
    if let Err(message) = result {
        fatal(&message);
    }
}

fn run_console(config: DaemonConfig) -> Result<(), String> {
    let listener = bind(&config)?;
    initialize_database(&config.database)?;
    let worker_executable = worker_executable()?;
    let update_application = application_directory()?;
    let stop = Arc::new(AtomicBool::new(false));
    let orchestration_database = config.database.clone();
    let orchestration_stop = Arc::clone(&stop);
    let orchestration = std::thread::spawn(move || {
        station_orchestration::run_until(
            &orchestration_database,
            &worker_executable,
            orchestration_stop,
        )
    });
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
    let result = station_api::serve_until(listener, &config.database, Arc::clone(&stop));
    stop.store(true, Ordering::Release);
    let orchestration_result = orchestration
        .join()
        .map_err(|_| "channel orchestration thread panicked".to_string())?
        .map_err(|error| format!("channel orchestration stopped: {error:?}"));
    let ingest_result = ingest
        .join()
        .map_err(|_| "media ingest thread panicked".to_string())?
        .map_err(|error| format!("media ingest stopped: {error:?}"));
    let alerting_result = alerting
        .join()
        .map_err(|_| "operator alerting thread panicked".to_string())?
        .map_err(|error| format!("operator alerting stopped: {error}"));
    let update_result = updates
        .join()
        .map_err(|_| "update monitor thread panicked".to_string())?
        .map_err(|error| format!("update monitor stopped: {error}"));
    result
        .and(orchestration_result)
        .and(ingest_result)
        .and(alerting_result)
        .and(update_result)
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
    }
}

fn run_service(config: DaemonConfig) -> Result<(), String> {
    let stop = Arc::new(AtomicBool::new(false));
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
            Duration::from_secs(10),
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
    let worker_executable = worker_executable()?;
    let update_application = application_directory()?;
    let orchestration_database = config.database.clone();
    let orchestration_stop = Arc::clone(&stop);
    let orchestration_receipt = config.database.with_file_name("orchestration-error.txt");
    let orchestration = std::thread::spawn(move || {
        let result = station_orchestration::run_until(
            &orchestration_database,
            &worker_executable,
            orchestration_stop,
        );
        if let Err(error) = &result {
            let _ = std::fs::write(&orchestration_receipt, format!("{error:?}"));
        }
        result
    });
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
    status
        .set_service_status(service_status(
            ServiceState::Running,
            ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
            0,
            Duration::ZERO,
            ServiceExitCode::NO_ERROR,
        ))
        .map_err(|error| error.to_string())?;
    let result = station_api::serve_until(listener, &config.database, Arc::clone(&stop));
    stop.store(true, Ordering::Release);
    let orchestration_result = orchestration
        .join()
        .map_err(|_| "channel orchestration thread panicked".to_string())?
        .map_err(|error| format!("channel orchestration stopped: {error:?}"));
    let ingest_result = ingest
        .join()
        .map_err(|_| "media ingest thread panicked".to_string())?
        .map_err(|error| format!("media ingest stopped: {error:?}"));
    let alerting_result = alerting
        .join()
        .map_err(|_| "operator alerting thread panicked".to_string())?
        .map_err(|error| format!("operator alerting stopped: {error}"));
    let update_result = updates
        .join()
        .map_err(|_| "update monitor thread panicked".to_string())?
        .map_err(|error| format!("update monitor stopped: {error}"));
    let result = result
        .and(orchestration_result)
        .and(ingest_result)
        .and(alerting_result)
        .and(update_result);
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
    std::env::current_exe()
        .map_err(|error| format!("could not locate station application: {error}"))?
        .parent()
        .map(std::path::Path::to_path_buf)
        .ok_or_else(|| "station executable has no application directory".into())
}

fn media_library(database: &std::path::Path) -> std::path::PathBuf {
    database
        .parent()
        .unwrap_or_else(|| std::path::Path::new("."))
        .join("media")
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
}
