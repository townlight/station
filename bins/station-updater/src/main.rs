mod config;

use config::{LaunchMode, UpdaterConfig};
use station_maintenance::{
    ActiveApplication, MaintenanceHost, MaintenancePhase, archive_completed_request,
    pending_request_paths, process_maintenance_request,
};
use std::ffi::{OsStr, OsString};
use std::os::windows::ffi::OsStringExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use ureq::Agent;
use ureq::tls::{RootCerts, TlsConfig};
use windows_service::define_windows_service;
use windows_service::service::{
    ServiceAccess, ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceInfo,
    ServiceState, ServiceStatus, ServiceType,
};
use windows_service::service_control_handler::{
    self, ServiceControlHandlerResult, ServiceStatusHandle,
};
use windows_service::service_dispatcher;
use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
use windows_sys::Win32::Foundation::CloseHandle;
use windows_sys::Win32::System::Threading::{
    OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, QueryFullProcessImageNameW,
};

const UPDATER_SERVICE_NAME: &str = "TownLightStationUpdater";
const STATION_SERVICE_NAME: &str = "TownLightStation";
const SERVICE_TYPE: ServiceType = ServiceType::OWN_PROCESS;
static SERVICE_CONFIG: OnceLock<UpdaterConfig> = OnceLock::new();

define_windows_service!(ffi_service_main, service_main);

fn main() {
    let config =
        config::parse(std::env::args_os().skip(1)).unwrap_or_else(|message| fatal(&message));
    let result = match config.mode {
        LaunchMode::Service => run_dispatcher(config),
        LaunchMode::Once => process_once(&config).map(|_| ()),
    };
    if let Err(message) = result {
        fatal(&message);
    }
}

fn run_dispatcher(config: UpdaterConfig) -> Result<(), String> {
    SERVICE_CONFIG
        .set(config)
        .map_err(|_| "updater service configuration was initialized twice".to_string())?;
    service_dispatcher::start(UPDATER_SERVICE_NAME, ffi_service_main)
        .map_err(|error| error.to_string())
}

fn service_main(_arguments: Vec<OsString>) {
    let config = SERVICE_CONFIG
        .get()
        .expect("updater service configuration is set before dispatch")
        .clone();
    if let Err(message) = run_service(config.clone()) {
        let _ = std::fs::write(
            config
                .database
                .with_file_name("maintenance-updater-error.txt"),
            sanitize(&message),
        );
    }
}

fn run_service(config: UpdaterConfig) -> Result<(), String> {
    let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
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
                    Duration::from_secs(5),
                    ServiceExitCode::NO_ERROR,
                ));
            }
            handler_stop.store(true, std::sync::atomic::Ordering::Release);
            ServiceControlHandlerResult::NoError
        }
        _ => ServiceControlHandlerResult::NotImplemented,
    };
    let status = service_control_handler::register(UPDATER_SERVICE_NAME, event_handler)
        .map_err(|error| error.to_string())?;
    *status_slot
        .lock()
        .map_err(|_| "updater service status lock was poisoned".to_string())? = Some(status);
    status
        .set_service_status(service_status(
            ServiceState::Running,
            ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
            0,
            Duration::ZERO,
            ServiceExitCode::NO_ERROR,
        ))
        .map_err(|error| error.to_string())?;

    while !stop.load(std::sync::atomic::Ordering::Acquire) {
        match process_once(&config) {
            Ok(_) => {
                let _ = std::fs::remove_file(
                    config
                        .database
                        .with_file_name("maintenance-updater-error.txt"),
                );
            }
            Err(error) => {
                std::fs::write(
                    config
                        .database
                        .with_file_name("maintenance-updater-error.txt"),
                    sanitize(&error),
                )
                .map_err(|write_error| {
                    format!("maintenance failed ({error}); error receipt failed ({write_error})")
                })?;
            }
        }
        for _ in 0..4 {
            if stop.load(std::sync::atomic::Ordering::Acquire) {
                break;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
    }
    status
        .set_service_status(service_status(
            ServiceState::Stopped,
            ServiceControlAccept::empty(),
            0,
            Duration::ZERO,
            ServiceExitCode::NO_ERROR,
        ))
        .map_err(|error| error.to_string())
}

fn process_once(config: &UpdaterConfig) -> Result<bool, String> {
    let data_directory = config
        .database
        .parent()
        .ok_or_else(|| "station database has no data directory".to_string())?;
    let Some(request_path) = pending_request_paths(data_directory)?.into_iter().next() else {
        return Ok(false);
    };
    let mut host = WindowsMaintenanceHost::new(config.application_root.clone());
    let result = process_maintenance_request(
        &request_path,
        &config.database,
        &config.application_root,
        system_time_millis()?,
        &mut host,
    )?;
    if matches!(
        result.phase,
        MaintenancePhase::Committed | MaintenancePhase::RolledBack
    ) {
        if result.phase == MaintenancePhase::Committed {
            let store = station_storage::StationStore::open(&config.database)?;
            store.clear_update_check()?;
            store.clear_staged_package()?;
            store.clear_prepared_slot()?;
        }
        archive_completed_request(data_directory, &request_path)?;
    }
    Ok(true)
}

struct WindowsMaintenanceHost {
    application_root: PathBuf,
    configured_stationd: Option<PathBuf>,
}

impl WindowsMaintenanceHost {
    fn new(application_root: PathBuf) -> Self {
        Self {
            application_root,
            configured_stationd: None,
        }
    }
}

impl MaintenanceHost for WindowsMaintenanceHost {
    fn stop_station(&mut self, timeout: Duration) -> Result<(), String> {
        let service = open_station(ServiceAccess::QUERY_STATUS | ServiceAccess::STOP)?;
        let state = service
            .query_status()
            .map_err(|error| format!("could not query station service: {error}"))?
            .current_state;
        if state != ServiceState::Stopped && state != ServiceState::StopPending {
            service
                .stop()
                .map_err(|error| format!("could not stop station service: {error}"))?;
        }
        wait_for_state(&service, ServiceState::Stopped, timeout)
    }

    fn configure_station(
        &mut self,
        stationd_path: &Path,
        database_path: &Path,
    ) -> Result<(), String> {
        if !stationd_path.is_absolute() || !database_path.is_absolute() || !stationd_path.is_file()
        {
            return Err("station service configuration paths are invalid".into());
        }
        let service = open_station(ServiceAccess::QUERY_CONFIG | ServiceAccess::CHANGE_CONFIG)?;
        let existing = service
            .query_config()
            .map_err(|error| format!("could not query station service configuration: {error}"))?;
        service
            .change_config(&ServiceInfo {
                name: OsString::from(STATION_SERVICE_NAME),
                display_name: existing.display_name,
                service_type: existing.service_type,
                start_type: existing.start_type,
                error_control: existing.error_control,
                executable_path: stationd_path.to_path_buf(),
                launch_arguments: vec![
                    OsString::from("service"),
                    OsString::from("--database"),
                    database_path.as_os_str().to_os_string(),
                    OsString::from("--address"),
                    OsString::from("127.0.0.1:4070"),
                ],
                dependencies: existing.dependencies,
                account_name: existing.account_name,
                account_password: None,
            })
            .map_err(|error| format!("could not configure station service candidate: {error}"))?;
        self.configured_stationd = Some(stationd_path.to_path_buf());
        Ok(())
    }

    fn start_station(&mut self) -> Result<(), String> {
        let service = open_station(ServiceAccess::QUERY_STATUS | ServiceAccess::START)?;
        if service
            .query_status()
            .map_err(|error| format!("could not query station service: {error}"))?
            .current_state
            == ServiceState::Stopped
        {
            service
                .start::<&OsStr>(&[])
                .map_err(|error| format!("could not start station service: {error}"))?;
        }
        Ok(())
    }

    fn prove_station(
        &mut self,
        expected: &ActiveApplication,
        timeout: Duration,
    ) -> Result<(), String> {
        let expected_path = expected
            .directory(&self.application_root)?
            .join("stationd.exe");
        let configured = self
            .configured_stationd
            .as_ref()
            .ok_or_else(|| "station service target was not configured".to_string())?;
        if !same_path(configured, &expected_path)? {
            return Err("configured station service target does not match proof identity".into());
        }
        let service = open_station(ServiceAccess::QUERY_STATUS)?;
        let agent = Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(2)))
            .max_redirects(0)
            .tls_config(
                TlsConfig::builder()
                    .root_certs(RootCerts::PlatformVerifier)
                    .build(),
            )
            .build()
            .new_agent();
        let deadline = Instant::now() + timeout;
        let mut last_error = "candidate did not reach running state".to_string();
        while Instant::now() < deadline {
            let status = service
                .query_status()
                .map_err(|error| format!("could not query station service: {error}"))?;
            if status.current_state == ServiceState::Running
                && let Some(process_id) = status.process_id
            {
                match process_image_path(process_id).and_then(|path| {
                    if same_path(&path, configured)? {
                        Ok(())
                    } else {
                        Err("running station process is not the configured candidate".into())
                    }
                }) {
                    Ok(()) => match probe_http(&agent) {
                        Ok(()) => return Ok(()),
                        Err(error) => last_error = error,
                    },
                    Err(error) => last_error = error,
                }
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        Err(format!("post-update station proof timed out: {last_error}"))
    }
}

fn probe_http(agent: &Agent) -> Result<(), String> {
    let health = read_json(agent, "http://127.0.0.1:4070/health", "candidate health")?;
    if health["status"] != "ready" || health["database"] != "ready" {
        return Err("candidate health response was not ready".into());
    }
    let runtime = read_json(
        agent,
        "http://127.0.0.1:4070/api/v1/runtime-safe-to-air",
        "candidate runtime proof",
    )?;
    if runtime["color"] != "green" {
        return Err("candidate runtime did not recover to safe-to-air green".into());
    }
    Ok(())
}

fn read_json(agent: &Agent, url: &str, label: &str) -> Result<serde_json::Value, String> {
    let bytes = agent
        .get(url)
        .call()
        .map_err(|_| format!("{label} endpoint did not respond"))?
        .body_mut()
        .with_config()
        .limit(1024 * 1024)
        .read_to_vec()
        .map_err(|_| format!("{label} response exceeded its bound"))?;
    serde_json::from_slice(&bytes).map_err(|_| format!("{label} response was invalid"))
}

fn open_station(access: ServiceAccess) -> Result<windows_service::service::Service, String> {
    let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)
        .map_err(|error| format!("could not open Windows service manager: {error}"))?;
    manager
        .open_service(STATION_SERVICE_NAME, access)
        .map_err(|error| format!("could not open station service: {error}"))
}

fn wait_for_state(
    service: &windows_service::service::Service,
    required: ServiceState,
    timeout: Duration,
) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if service
            .query_status()
            .map_err(|error| format!("could not query station service: {error}"))?
            .current_state
            == required
        {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(format!(
        "station service did not reach {required:?} before timeout"
    ))
}

fn process_image_path(process_id: u32) -> Result<PathBuf, String> {
    // SAFETY: requested access is query-only and the returned handle is closed below.
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, process_id) };
    if handle.is_null() {
        return Err(format!(
            "could not open running station process: {}",
            std::io::Error::last_os_error()
        ));
    }
    let mut buffer = vec![0_u16; 32_768];
    let mut length = buffer.len() as u32;
    // SAFETY: the handle is valid and buffer/length describe writable storage.
    let status = unsafe { QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut length) };
    // SAFETY: handle was returned by OpenProcess and is closed exactly once.
    unsafe { CloseHandle(handle) };
    if status == 0 {
        return Err(format!(
            "could not query running station image: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(PathBuf::from(OsString::from_wide(
        &buffer[..length as usize],
    )))
}

fn same_path(left: &Path, right: &Path) -> Result<bool, String> {
    let left = std::fs::canonicalize(left)
        .map_err(|error| format!("could not resolve application path: {error}"))?;
    let right = std::fs::canonicalize(right)
        .map_err(|error| format!("could not resolve application path: {error}"))?;
    Ok(left
        .to_string_lossy()
        .eq_ignore_ascii_case(&right.to_string_lossy()))
}

fn system_time_millis() -> Result<i64, String> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis();
    i64::try_from(millis).map_err(|_| "system time exceeds the supported range".into())
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

fn sanitize(value: &str) -> String {
    let clean = value
        .chars()
        .filter(|character| !character.is_control())
        .collect::<String>();
    clean.trim().chars().take(1_000).collect()
}

fn fatal(message: &str) -> ! {
    eprintln!("Townlight Station updater failed: {message}");
    std::process::exit(2)
}
